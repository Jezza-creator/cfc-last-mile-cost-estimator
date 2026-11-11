from __future__ import annotations

import csv
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
import math
import os
from pathlib import Path
from typing import Iterable

from src.db import get_session
from src.extraction_window import EXTRACTION_END_DATE, EXTRACTION_START_DATE
from src.invoice_filter import invoice_filter_clause
from src.parcel_shape import estimate_rectangular_envelope, normalize_dimension_triplet
from src.site_config import SITE_CONFIGS
from src.stage1_moorebank_parcel_assigner import (
    ParcelBucket,
    _build_equal_split_parcels,
    _parcel_load_score,
    _rebalance_empty_parcels,
    _assignment_source,
    _source_parcel_assignment,
)


STORE_NAME = SITE_CONFIGS["TRUGANINA"].store_name
WAREHOUSE_CODE = SITE_CONFIGS["TRUGANINA"].inventory_warehouse_code
EXPORT_CSV = True
OUTPUT_DIR = Path(os.environ.get("LM_OUTPUT_DIR", "outputs"))
OUTPUT_FILE = "stage1_truganina_parcel_assignments_full.csv"

AUTOMATION_SESSION_GAP_CANDIDATES = (10, 15, 20, 30, 45, 60)
MANUAL_SESSION_GAP_CANDIDATES = (30, 45, 60, 90, 120, 180)
FALLBACK_SESSION_GAP_CANDIDATES = (30, 45, 60, 90, 120, 180)


@dataclass(frozen=True)
class PackedRow:
    invoice_number: str
    actual_parcel_count: int
    actual_carrier: str
    sku_id: str
    rbu_description: str
    product_description: str
    quantity_packed: int
    pack_timestamp: datetime | None
    height: float | None
    width: float | None
    length: float | None
    weight: float | None
    cubage: float | None
    size_category: str
    storage_type: str
    bldg: str
    pf_res: str
    cfc_location: str
    process_class: str
    zone_description: str | None
    source_shipment_id: str = ""
    source_package_id: str = ""
    source_package_detail_id: str = ""
    assignment_source: str = ""


def main() -> None:
    session = get_session()
    try:
        rows = session.sql(_build_query()).collect()
        orders = _group_by_order(rows)

        results = []
        for invoice_number, order in orders.items():
            parcels, rule_name, estimated_count = _assign_order(order["rows"], int(order["actual_parcel_count"]))
            results.append(
                {
                    "invoice_number": invoice_number,
                    "actual_carrier": order["actual_carrier"],
                    "actual_parcel_count": int(order["actual_parcel_count"]),
                    "estimated_parcel_count": estimated_count,
                    "assigned_parcel_count": len(parcels),
                    "rule_name": rule_name,
                    "assignment_source": _assignment_source(order["rows"]),
                    "pack_flow_summary": _flow_summary(order["rows"]),
                    "parcels": parcels,
                }
            )

        _print_summary(results)
        _print_examples(results)
        if EXPORT_CSV:
            _export_csv(results)
            _write_parcel_content_output(results)
    finally:
        session.close()


def _build_query() -> str:
    invoice_filter = invoice_filter_clause("TO_VARCHAR(ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER)")
    return f"""
    WITH shippit AS (
        SELECT
            ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number,
            ORDER_PARCEL__PARCEL_COUNT AS actual_parcel_count,
            CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME AS actual_carrier
        FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
        WHERE STORE_ATTRIBUTE__STORE_NAME = '{STORE_NAME}'
          AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                BETWEEN TO_DATE('{EXTRACTION_START_DATE}') AND TO_DATE('{EXTRACTION_END_DATE}')
          AND ORDER_PARCEL__PARCEL_COUNT IS NOT NULL
          {invoice_filter}
    ),
    packed_base AS (
        SELECT
            REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
            s.actual_parcel_count,
            s.actual_carrier,
            TO_VARCHAR(p.ORDER_ID) AS ORDER_ID,
            TO_VARCHAR(p.SKU_ID) AS SKU_ID,
            p.QUANTITY_PACKED,
            p.PACK_TIMESTAMP,
            CAST(NULL AS VARCHAR) AS SOURCE_SHIPMENT_ID,
            CAST(NULL AS VARCHAR) AS SOURCE_PACKAGE_ID,
            CAST(NULL AS VARCHAR) AS SOURCE_PACKAGE_DETAIL_ID,
            'CATCH_CFC.PACKED' AS ASSIGNMENT_SOURCE
        FROM KSFDA.CATCH_CFC.PACKED p
        JOIN shippit s
          ON s.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
    ),
    fulfillment_detail AS (
        SELECT
            TO_VARCHAR(fd.FULFILLMENT_ID) AS invoice_number,
            s.actual_parcel_count,
            s.actual_carrier,
            TO_VARCHAR(fd.FULFILLMENT_ID) AS ORDER_ID,
            TO_VARCHAR(fd.ITEM_ID) AS SKU_ID,
            COALESCE(fd.QUANTITY, 0) AS QUANTITY_PACKED,
            TRY_TO_TIMESTAMP_NTZ(TO_VARCHAR(COALESCE(fd.UPDATED_TIMESTAMP, fd.CREATED_TIMESTAMP))) AS PACK_TIMESTAMP,
            TO_VARCHAR(fd.SHIPMENT_ID) AS SOURCE_SHIPMENT_ID,
            TO_VARCHAR(fd.PACKAGE_ID) AS SOURCE_PACKAGE_ID,
            TO_VARCHAR(fd.PACKAGE_DETAIL_ID) AS SOURCE_PACKAGE_DETAIL_ID,
            'ORD_FULFILLMENT_DETAIL' AS ASSIGNMENT_SOURCE
        FROM KSFPA.OMFP.ORD_FULFILLMENT_DETAIL fd
        JOIN shippit s
          ON s.invoice_number = TO_VARCHAR(fd.FULFILLMENT_ID)
        WHERE NULLIF(TRIM(TO_VARCHAR(fd.ITEM_ID)), '') IS NOT NULL
          AND COALESCE(fd.QUANTITY, 0) > 0
    ),
    fulfillment_counts AS (
        SELECT
            invoice_number,
            COUNT(
                DISTINCT COALESCE(
                    NULLIF(TRIM(SOURCE_PACKAGE_ID), ''),
                    NULLIF(TRIM(SOURCE_SHIPMENT_ID), '')
                )
            ) AS package_count
        FROM fulfillment_detail
        GROUP BY invoice_number
    ),
    eligible_fulfillment AS (
        SELECT s.invoice_number
        FROM shippit s
        JOIN fulfillment_counts f
          ON f.invoice_number = s.invoice_number
        WHERE f.package_count = s.actual_parcel_count
          AND f.package_count > 0
    ),
    source_rows AS (
        SELECT p.*
        FROM packed_base p
        WHERE NOT EXISTS (
            SELECT 1
            FROM eligible_fulfillment e
            WHERE e.invoice_number = p.invoice_number
        )
        UNION ALL
        SELECT f.*
        FROM fulfillment_detail f
        JOIN eligible_fulfillment e
          ON e.invoice_number = f.invoice_number
    ),
    packed AS (
        SELECT
            ROW_NUMBER() OVER (
                ORDER BY
                    invoice_number,
                    ORDER_ID,
                    SKU_ID,
                    PACK_TIMESTAMP,
                    QUANTITY_PACKED,
                    SOURCE_PACKAGE_ID
            ) AS packed_row_id,
            source_rows.*
        FROM source_rows
    ),
    packed_skus AS (
        SELECT DISTINCT
            SKU_ID
        FROM packed
    ),
    sku_latest AS (
        SELECT *
        FROM (
            SELECT
                sa.*,
                ROW_NUMBER() OVER (
                    PARTITION BY sa.SKU_ID
                    ORDER BY sa.EXTRACT_TIMESTAMP DESC
                ) AS rn
            FROM KSFDA.CATCH_CFC.SKU_ATTRIBUTE sa
            JOIN packed_skus ps
              ON ps.SKU_ID = TO_VARCHAR(sa.SKU_ID)
        )
        WHERE rn = 1
    ),
    hierarchy_latest AS (
        SELECT PRODUCT_SOURCE_IDENTIFIER, RBU_DESCRIPTION
        FROM (
            SELECT
                kc.PRODUCT_SOURCE_IDENTIFIER,
                kc.RBU_DESCRIPTION,
                ROW_NUMBER() OVER (
                    PARTITION BY kc.PRODUCT_SOURCE_IDENTIFIER
                    ORDER BY kc.PRODUCT_SOURCE_IDENTIFIER
                ) AS rn
            FROM KSFPA.MR2C.KEYCODE kc
            JOIN packed_skus ps
              ON ps.SKU_ID = TO_VARCHAR(kc.PRODUCT_SOURCE_IDENTIFIER)
        )
        WHERE rn = 1
    ),
    plm_latest AS (
        SELECT PRODUCT_SOURCE_IDENTIFIER, ITEM_NAME, ITEM_DESCRIPTION
        FROM (
            SELECT
                dss.PRODUCT_SOURCE_IDENTIFIER,
                dss.ITEM_NAME,
                dss.ITEM_DESCRIPTION,
                ROW_NUMBER() OVER (
                    PARTITION BY dss.PRODUCT_SOURCE_IDENTIFIER
                    ORDER BY
                        CASE
                            WHEN NULLIF(TRIM(COALESCE(dss.ITEM_DESCRIPTION, dss.ITEM_NAME)), '') IS NULL THEN 1
                            ELSE 0
                        END,
                        dss.EFFECTIVE_FROM DESC
                ) AS rn
            FROM KSFPA.MERCHANDISE_PVT.KEYCODE_CARTON_DSS_DIMENSIONS_VW dss
            JOIN packed_skus ps
              ON ps.SKU_ID = TO_VARCHAR(dss.PRODUCT_SOURCE_IDENTIFIER)
        )
        WHERE rn = 1
    ),
    inventory_candidate AS (
        SELECT i.*
        FROM KSFDA.CATCH_CFC.INVENTORY i
        JOIN packed_skus ps
          ON ps.SKU_ID = TO_VARCHAR(i.SKU_ID)
        WHERE i.WAREHOUSE_CODE = '{WAREHOUSE_CODE}'
          AND i.EXTRACT_TIMESTAMP::DATE BETWEEN TO_DATE('{EXTRACTION_START_DATE}') AND TO_DATE('{EXTRACTION_END_DATE}')
    ),
    bin_latest AS (
        SELECT *
        FROM (
            SELECT
                b.*,
                ROW_NUMBER() OVER (
                    PARTITION BY b.BIN_LOCATION
                    ORDER BY b.EXTRACT_TIMESTAMP DESC
                ) AS rn
            FROM KSFDA.CATCH_CFC.BIN_INFORMATION b
        )
        WHERE rn = 1
    ),
    zone_lookup AS (
        SELECT DISTINCT
            ZONE,
            ZONE_DESCRIPTION
        FROM KSFDA.CATCH_CFC.ZONE_ATTRIBUTE
    ),
    inventory_aligned AS (
        SELECT
            p.packed_row_id,
            i.BIN_LOCATION,
            i.EXTRACT_TIMESTAMP AS INVENTORY_TIMESTAMP,
            CASE
                WHEN b.BIN_LOCATION LIKE 'L%' THEN 'AMR'
                WHEN b.BIN_LOCATION BETWEEN '37%' AND '42%' THEN 'PFR'
                WHEN b.BIN_LOCATION BETWEEN '01%' AND '05%' THEN 'Consumables/AMR Reserve Racking'
                WHEN b.BIN_LOCATION BETWEEN '21%' AND '28%' THEN 'Hanger Racks/AMR Reserve Shelving'
                WHEN b.ZONE IN ('4') THEN 'KNAPP'
                WHEN b.ZONE IN ('6','7','8','H','U','V','C') THEN 'Shelving with Ground Pallet'
                WHEN b.ZONE IN ('F','3','2','5') THEN 'Ground Pallet'
                WHEN b.ZONE IN ('A','J','M') THEN 'VNA Shelving / Pallet'
                WHEN b.ZONE IN ('N') THEN 'Pallet Racking'
                WHEN b.ZONE IN ('G','S') THEN 'Tote Tower'
                ELSE 'No inventory mapping'
            END AS STORAGE_TYPE,
            CASE
                WHEN b.ZONE IN ('4','6','7','8','F','H','U','V','B','L','A','P','M','G','S') THEN 'T2'
                WHEN b.ZONE IN ('3','C','N','J','Q','2','5') THEN 'T3'
                ELSE 'Unknown'
            END AS BLDG,
            CASE
                WHEN b.ZONE IN ('4','6','7','8','F','H','U','V','3','2','5') THEN 'Pickface'
                WHEN b.ZONE IN ('A','C','N','J','M','G','S') THEN 'Reserve'
                ELSE 'In Process'
            END AS PF_RES,
            CASE
                WHEN b.ZONE IN ('4') THEN 'KNAPP'
                WHEN b.ZONE IN ('6','7','8','H','U','V','G') THEN 'UNDER MEZZ + CLS'
                WHEN b.ZONE IN ('F','S') THEN 'UPPER MEZZ'
                WHEN b.ZONE IN ('B','L','W','Q','PK','P','T','E','0','1','9','D','K','Y') THEN 'OTHER LOCATIONS ON SITE'
                WHEN b.ZONE IN ('3','2','5') THEN 'T3 SAI PICKFACE'
                WHEN b.ZONE IN ('C','N','J') THEN 'T3 RES '
                WHEN b.ZONE IN ('A','M') THEN 'T2 RES '
                WHEN b.ZONE IS NULL THEN 'No Zone '
                ELSE 'Unknown'
            END AS CFC_LOCATION,
            COALESCE(zl.ZONE_DESCRIPTION, 'Unknown') AS ZONE_DESCRIPTION,
            CASE
                WHEN b.ZONE IN ('4') THEN 'Automation'
                WHEN b.ZONE IS NULL
                  OR b.ZONE IN ('B','L','W','Q','PK','P','T','E','0','1','9','D','K','Y')
                  OR b.ZONE = 'Unknown'
                  THEN 'Unknown'
                ELSE 'Manual/CSR'
            END AS PROCESS_CLASS
        FROM packed p
        JOIN inventory_candidate i
          ON i.SKU_ID = p.SKU_ID
        LEFT JOIN bin_latest b
          ON i.BIN_LOCATION = b.BIN_LOCATION
        LEFT JOIN zone_lookup zl
          ON zl.ZONE = b.ZONE
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY p.packed_row_id
            ORDER BY ABS(DATEDIFF(second, i.EXTRACT_TIMESTAMP, p.PACK_TIMESTAMP)), i.EXTRACT_TIMESTAMP DESC
        ) = 1
    ),
    classified AS (
        SELECT
            p.invoice_number,
            p.actual_parcel_count,
            p.actual_carrier,
            p.ORDER_ID,
            p.SKU_ID,
            COALESCE(h.RBU_DESCRIPTION, 'Unclassified') AS RBU_DESCRIPTION,
            COALESCE(pml.ITEM_DESCRIPTION, pml.ITEM_NAME, '') AS PRODUCT_DESCRIPTION,
            p.QUANTITY_PACKED,
            p.PACK_TIMESTAMP,
            sku.HEIGHT,
            sku.WIDTH,
            sku.LENGTH,
            sku.WEIGHT,
            sku.CUBAGE,
            CASE
                WHEN sku.HEIGHT IS NULL OR sku.WIDTH IS NULL OR sku.LENGTH IS NULL THEN 'No dims'
                WHEN LEAST(sku.HEIGHT, sku.WIDTH, sku.LENGTH) <= 31
                 AND (sku.HEIGHT + sku.WIDTH + sku.LENGTH
                      - LEAST(sku.HEIGHT, sku.WIDTH, sku.LENGTH)
                      - GREATEST(sku.HEIGHT, sku.WIDTH, sku.LENGTH)) <= 35
                 AND GREATEST(sku.HEIGHT, sku.WIDTH, sku.LENGTH) <= 54
                 AND COALESCE(sku.WEIGHT, 0) <= 16
                    THEN 'Toteable'
                WHEN LEAST(sku.HEIGHT, sku.WIDTH, sku.LENGTH) <= 105
                 AND (sku.HEIGHT + sku.WIDTH + sku.LENGTH
                      - LEAST(sku.HEIGHT, sku.WIDTH, sku.LENGTH)
                      - GREATEST(sku.HEIGHT, sku.WIDTH, sku.LENGTH)) <= 105
                 AND GREATEST(sku.HEIGHT, sku.WIDTH, sku.LENGTH) <= 105
                 AND COALESCE(sku.WEIGHT, 0) <= 45
                    THEN 'Non-Toteable'
                WHEN LEAST(sku.HEIGHT, sku.WIDTH, sku.LENGTH) <= 200
                 AND (sku.HEIGHT + sku.WIDTH + sku.LENGTH
                      - LEAST(sku.HEIGHT, sku.WIDTH, sku.LENGTH)
                      - GREATEST(sku.HEIGHT, sku.WIDTH, sku.LENGTH)) <= 200
                 AND GREATEST(sku.HEIGHT, sku.WIDTH, sku.LENGTH) <= 200
                 AND COALESCE(sku.WEIGHT, 0) <= 80
                    THEN 'Bulky'
                ELSE 'Unknown'
            END AS SIZE_CATEGORY_TRUG,
            COALESCE(w.STORAGE_TYPE, 'No inventory mapping') AS STORAGE_TYPE,
            COALESCE(w.BLDG, 'Unknown') AS BLDG,
            COALESCE(w.PF_RES, 'In Process') AS PF_RES,
            COALESCE(w.CFC_LOCATION, 'Unknown') AS CFC_LOCATION,
            COALESCE(w.PROCESS_CLASS, 'Unknown') AS PROCESS_CLASS,
            COALESCE(w.ZONE_DESCRIPTION, 'Unknown') AS ZONE_DESCRIPTION,
            p.SOURCE_SHIPMENT_ID,
            p.SOURCE_PACKAGE_ID,
            p.SOURCE_PACKAGE_DETAIL_ID,
            p.ASSIGNMENT_SOURCE
        FROM packed p
        LEFT JOIN sku_latest sku
          ON TO_VARCHAR(sku.SKU_ID) = p.SKU_ID
        LEFT JOIN inventory_aligned w
          ON w.PACKED_ROW_ID = p.PACKED_ROW_ID
        LEFT JOIN hierarchy_latest h
          ON TO_VARCHAR(h.PRODUCT_SOURCE_IDENTIFIER) = p.SKU_ID
        LEFT JOIN plm_latest pml
          ON TO_VARCHAR(pml.PRODUCT_SOURCE_IDENTIFIER) = p.SKU_ID
    )
    SELECT
        invoice_number,
        actual_parcel_count,
        actual_carrier,
        ORDER_ID,
        SKU_ID,
        RBU_DESCRIPTION,
        PRODUCT_DESCRIPTION,
        QUANTITY_PACKED,
        PACK_TIMESTAMP,
        HEIGHT,
        WIDTH,
        LENGTH,
        WEIGHT,
        CUBAGE,
        SIZE_CATEGORY_TRUG,
        STORAGE_TYPE,
        BLDG,
        PF_RES,
        CFC_LOCATION,
        PROCESS_CLASS,
        ZONE_DESCRIPTION,
        SOURCE_SHIPMENT_ID,
        SOURCE_PACKAGE_ID,
        SOURCE_PACKAGE_DETAIL_ID,
        ASSIGNMENT_SOURCE
    FROM classified
    ORDER BY invoice_number, PACK_TIMESTAMP, SKU_ID
    """


def _group_by_order(rows: Iterable[object]) -> dict[str, dict[str, object]]:
    orders: dict[str, dict[str, object]] = {}
    for row in rows:
        record = row.asDict()
        invoice_number = str(record["INVOICE_NUMBER"])
        order = orders.setdefault(
            invoice_number,
            {
                "actual_parcel_count": int(record["ACTUAL_PARCEL_COUNT"] or 0),
                "actual_carrier": record["ACTUAL_CARRIER"],
                "rows": [],
            },
        )
        order["rows"].append(
            PackedRow(
                invoice_number=invoice_number,
                actual_parcel_count=int(record["ACTUAL_PARCEL_COUNT"] or 0),
                actual_carrier=str(record["ACTUAL_CARRIER"] or ""),
                sku_id=str(record["SKU_ID"] or ""),
                rbu_description=str(record["RBU_DESCRIPTION"] or "Unclassified"),
                product_description=str(record["PRODUCT_DESCRIPTION"] or ""),
                quantity_packed=int(record["QUANTITY_PACKED"] or 0),
                pack_timestamp=record["PACK_TIMESTAMP"],
                height=_to_float(record["HEIGHT"]),
                width=_to_float(record["WIDTH"]),
                length=_to_float(record["LENGTH"]),
                weight=_to_float(record["WEIGHT"]),
                cubage=_to_float(record["CUBAGE"]),
                size_category=str(record["SIZE_CATEGORY_TRUG"] or "Unknown"),
                storage_type=str(record["STORAGE_TYPE"] or "No inventory mapping"),
                bldg=str(record["BLDG"] or "Unknown"),
                pf_res=str(record["PF_RES"] or "In Process"),
                cfc_location=str(record["CFC_LOCATION"] or "Unknown"),
                process_class=str(record["PROCESS_CLASS"] or "Unknown"),
                zone_description=(None if record["ZONE_DESCRIPTION"] is None else str(record["ZONE_DESCRIPTION"])),
                source_shipment_id=str(record.get("SOURCE_SHIPMENT_ID") or ""),
                source_package_id=str(record.get("SOURCE_PACKAGE_ID") or ""),
                source_package_detail_id=str(record.get("SOURCE_PACKAGE_DETAIL_ID") or ""),
                assignment_source=str(record.get("ASSIGNMENT_SOURCE") or ""),
            )
        )
    return orders


def _assign_order(rows: list[PackedRow], actual_parcel_count: int) -> tuple[list[ParcelBucket], str, int]:
    if not rows:
        return [], "EMPTY", 0

    source_assignment = _source_parcel_assignment(rows, actual_parcel_count)
    if source_assignment is not None:
        return source_assignment

    target_count = max(int(actual_parcel_count or 0), 1)
    parcels = [ParcelBucket(parcel_id=index + 1) for index in range(target_count)]

    if target_count > 1 and len({row.sku_id for row in rows if int(row.quantity_packed or 0) > 0}) == 1:
        return _build_equal_split_parcels(rows, target_count), "SINGLE_SKU_EQUAL_SPLIT", target_count

    automation_rows = [row for row in rows if row.process_class == "Automation"]
    manual_rows = [row for row in rows if row.process_class == "Manual/CSR"]
    unknown_rows = [row for row in rows if row.process_class == "Unknown"]
    bulky_rows = [row for row in rows if row.size_category == "Bulky"]

    known_rows = [row for row in rows if row.process_class != "Unknown"]
    if not known_rows:
        gap_seconds = _best_fit_session_gap_seconds(rows, target_count, FALLBACK_SESSION_GAP_CANDIDATES)
        grouped_rows = _session_groups(rows, gap_seconds)
        for group in grouped_rows:
            parcel = min(parcels, key=_parcel_load_score)
            for row in group:
                parcel.add(row)
        _rebalance_empty_parcels(parcels)
        return parcels, f"UNKNOWN_TIME_CLUSTERED_{gap_seconds}S", target_count

    automation_gap = _best_fit_session_gap_seconds(automation_rows or rows, target_count, AUTOMATION_SESSION_GAP_CANDIDATES)
    manual_gap = _best_fit_session_gap_seconds(manual_rows or rows, target_count, MANUAL_SESSION_GAP_CANDIDATES)

    if automation_rows and not manual_rows and not unknown_rows:
        rule_name = f"ALL_AUTOMATION_TIME_CLUSTERED_{automation_gap}S"
        grouped_rows = _session_groups(rows, automation_gap)
    elif manual_rows and not automation_rows and not unknown_rows:
        rule_name = f"ALL_MANUAL_TIME_CLUSTERED_{manual_gap}S"
        grouped_rows = _session_groups(rows, manual_gap)
    else:
        if automation_rows and manual_rows:
            rule_name = "MIXED_AUTOMATION_MANUAL"
        elif automation_rows:
            rule_name = "AUTOMATION_WITH_UNKNOWN"
        elif manual_rows:
            rule_name = "MANUAL_WITH_UNKNOWN"
        else:
            rule_name = "MIXED_UNKNOWN"

        grouped_rows = []
        grouped_rows.extend([[row] for row in sorted(bulky_rows, key=_assignment_sort_key, reverse=True)])
        grouped_rows.extend(_session_groups(manual_rows, manual_gap))
        grouped_rows.extend(_session_groups(automation_rows, automation_gap))
        grouped_rows.extend([[row] for row in sorted(unknown_rows, key=_assignment_sort_key, reverse=True)])

    for group in grouped_rows:
        parcel = min(parcels, key=_parcel_load_score)
        for row in group:
            parcel.add(row)

    _rebalance_empty_parcels(parcels)
    return parcels, rule_name, target_count


def _session_groups(rows: list[PackedRow], gap_seconds: int) -> list[list[PackedRow]]:
    ordered_rows = sorted(rows, key=lambda row: row.pack_timestamp or datetime.min)
    if not ordered_rows:
        return []

    groups: list[list[PackedRow]] = []
    current_group: list[PackedRow] = [ordered_rows[0]]
    previous_timestamp = ordered_rows[0].pack_timestamp

    for row in ordered_rows[1:]:
        if row.pack_timestamp is None or previous_timestamp is None:
            gap = 0
        else:
            gap = (row.pack_timestamp - previous_timestamp).total_seconds()
        if gap > gap_seconds:
            groups.append(current_group)
            current_group = [row]
        else:
            current_group.append(row)
        previous_timestamp = row.pack_timestamp

    groups.append(current_group)
    return groups


def _best_fit_session_gap_seconds(rows: list[PackedRow], target_count: int, candidates: tuple[int, ...]) -> int:
    ordered_rows = [row for row in rows if row.pack_timestamp is not None]
    if len(ordered_rows) <= 1:
        return candidates[0]

    best_gap = candidates[0]
    best_score: tuple[float, float, int] | None = None

    for gap_seconds in candidates:
        groups = _session_groups(ordered_rows, gap_seconds)
        parcels = [ParcelBucket(parcel_id=index + 1) for index in range(max(target_count, 1))]

        for group in groups:
            parcel = min(parcels, key=_parcel_load_score)
            for row in group:
                parcel.add(row)

        loads = [_parcel_load_score(parcel) for parcel in parcels]
        if loads:
            mean_load = sum(loads) / len(loads)
            load_variance = sum((load - mean_load) ** 2 for load in loads) / len(loads)
            load_spread = max(loads) - min(loads)
        else:
            load_variance = 0.0
            load_spread = 0.0

        group_penalty = abs(len(groups) - target_count) * 1000.0
        score = (group_penalty + load_spread + load_variance, load_spread, gap_seconds)

        if best_score is None or score < best_score:
            best_gap = gap_seconds
            best_score = score

    return best_gap


def _assignment_sort_key(row: PackedRow) -> tuple[float, float, datetime, str]:
    return (
        float(row.cubage or 0.0),
        float(row.weight or 0.0),
        row.pack_timestamp or datetime.min,
        row.sku_id,
    )


def _flow_summary(rows: list[PackedRow]) -> str:
    classes = {row.process_class for row in rows if row.process_class != "Unknown"}
    if not classes:
        return "Unknown"
    if classes == {"Automation"}:
        return "All Auto"
    if classes == {"Manual/CSR"}:
        return "All Manual"
    return "Mixed"


def _estimate_parcel_shape(parcel: ParcelBucket) -> tuple[float, float | None, float | None, float | None, float, str]:
    packed_weight_kg = max(parcel.total_weight_kg, 0.0)
    packed_cube_cm3 = max(parcel.total_cube, 0.0)
    dimension_triplets: list[tuple[float, float, float]] = []

    for line in parcel.lines:
        qty = max(int(line.quantity_packed or 0), 0)
        dims = normalize_dimension_triplet(line.length, line.width, line.height)
        if dims is None:
            continue
        dimension_triplets.extend([dims] * qty)

    if dimension_triplets:
        estimate = estimate_rectangular_envelope(dimension_triplets, packed_cube_cm3)
        if estimate is not None:
            fill_pct = (packed_cube_cm3 * 100.0 / estimate.estimated_cube_cm3) if estimate.estimated_cube_cm3 else None
            air_pct = (100.0 - fill_pct) if fill_pct is not None else None
            return (
                packed_weight_kg,
                packed_cube_cm3,
                estimate.estimated_length_cm,
                estimate.estimated_width_cm,
                estimate.estimated_height_cm,
                estimate.estimated_cube_cm3,
                estimate.shape_source,
            )

    return (
        packed_weight_kg,
        packed_cube_cm3,
        None,
        None,
        None,
        packed_cube_cm3,
        "CUBE_FALLBACK",
    )


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _print_summary(results: list[dict[str, object]]) -> None:
    order_count = len(results)
    exact_count = sum(1 for result in results if result["estimated_parcel_count"] == result["actual_parcel_count"])
    under_count = sum(1 for result in results if result["estimated_parcel_count"] < result["actual_parcel_count"])
    over_count = sum(1 for result in results if result["estimated_parcel_count"] > result["actual_parcel_count"])
    avg_actual = sum(int(result["actual_parcel_count"]) for result in results) / order_count if order_count else 0.0
    avg_estimated = sum(int(result["estimated_parcel_count"]) for result in results) / order_count if order_count else 0.0

    print("STAGE 1 SUMMARY")
    print("MODE=FULL")
    print(
        "Row("
        f"ORDER_COUNT={order_count}, "
        f"AVG_ACTUAL_PARCEL_COUNT={avg_actual:.2f}, "
        f"AVG_ESTIMATED_PARCEL_COUNT={avg_estimated:.2f}, "
        f"EXACT_MATCH_PCT={(exact_count * 100 / order_count) if order_count else 0.0:.2f}, "
        f"UNDERCOUNT_PCT={(under_count * 100 / order_count) if order_count else 0.0:.2f}, "
        f"OVERCOUNT_PCT={(over_count * 100 / order_count) if order_count else 0.0:.2f}"
        ")"
    )


def _print_examples(results: list[dict[str, object]]) -> None:
    print("\nTOP ORDER EXAMPLES")
    for result in results[:10]:
        print(
            "Order("
            f"invoice_number={result['invoice_number']}, "
            f"carrier={result['actual_carrier']}, "
            f"actual={result['actual_parcel_count']}, "
            f"estimated={result['estimated_parcel_count']}, "
            f"assigned={result['assigned_parcel_count']}, "
            f"flow={result['pack_flow_summary']}, "
            f"rule={result['rule_name']}"
            ")"
        )
        for parcel in result["parcels"][:3]:
            print(
                "  Parcel("
                f"id={parcel.parcel_id}, "
                f"units={parcel.total_units}, "
                f"weight_kg={parcel.total_weight_kg:.2f}, "
                f"cube={parcel.total_cube:.2f}, "
                f"skus={_parcel_skus(parcel)}"
                ")"
            )


def _export_csv(results: list[dict[str, object]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / OUTPUT_FILE

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "invoice_number",
                "actual_carrier",
                "actual_parcel_count",
                "estimated_parcel_count",
                "rule_name",
                "assignment_source",
                "parcel_id",
                "placeholder_parcel",
                "parcel_units",
                "parcel_weight_kg",
                "parcel_cube",
                "packed_sku_weight_kg",
                "packed_sku_cube_cm3",
                "estimated_length_cm",
                "estimated_width_cm",
                "estimated_height_cm",
                "estimated_fill_pct_vs_packed_cube",
                "estimated_air_ratio_pct_vs_packed_cube",
                "estimated_shape_source",
                "cfc_cube_imputed",
                "parcel_sku_qty_pattern",
                "parcel_packed_sku_cube_pattern_cm3",
                "sku_ids",
            ]
        )
        for result in results:
            for parcel in result["parcels"]:
                (
                    packed_weight_kg,
                    packed_cube_cm3,
                    estimated_length_cm,
                    estimated_width_cm,
                    estimated_height_cm,
                    estimated_cube_cm3,
                    shape_source,
                ) = _estimate_parcel_shape(parcel)
                fill_pct = (packed_cube_cm3 * 100.0 / estimated_cube_cm3) if estimated_cube_cm3 else None
                air_pct = (100.0 - fill_pct) if fill_pct is not None else None
                sku_qty_pattern, sku_packed_cube_pattern_cm3 = _parcel_sku_patterns(parcel)
                writer.writerow(
                    [
                        result["invoice_number"],
                        result["actual_carrier"],
                        result["actual_parcel_count"],
                        result["estimated_parcel_count"],
                        result["rule_name"],
                        result["assignment_source"],
                        parcel.parcel_id,
                        "Y" if parcel.total_units == 0 else "",
                        parcel.total_units,
                        round(parcel.total_weight_kg, 3),
                        round(parcel.total_cube, 3),
                        round(packed_weight_kg, 3),
                        round(packed_cube_cm3, 3),
                        _fmt(estimated_length_cm),
                        _fmt(estimated_width_cm),
                        _fmt(estimated_height_cm),
                        _fmt(fill_pct),
                        _fmt(air_pct),
                        shape_source,
                        "",
                        sku_qty_pattern,
                        sku_packed_cube_pattern_cm3,
                        _parcel_skus(parcel),
                    ]
                )

    print(f"\nCSV_EXPORTED={output_path}")


def _write_parcel_content_output(results: list[dict[str, object]]) -> None:
    """Write the assigned item-level contents consumed by Stage 2 and the UI."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "stage1_truganina_parcel_assignment_lines.csv"
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "invoice_number",
                "parcel_id",
                "sku_id",
                "rbu_description",
                "product_description",
                "quantity_packed",
                "cfc_height_cm",
                "cfc_width_cm",
                "cfc_length_cm",
                "cfc_weight_kg",
                "raw_sku_cube_cm3_per_unit",
                "raw_packed_sku_cube_cm3_per_line",
                "cfc_cube_imputed",
                "size_category",
                "pack_timestamp",
                "source_shipment_id",
                "source_package_id",
                "source_package_detail_id",
                "assignment_source",
            ]
        )
        for result in results:
            for parcel in result["parcels"]:
                for line in parcel.lines:
                    quantity = max(int(line.quantity_packed or 0), 0)
                    if quantity <= 0:
                        continue
                    cube_per_unit = float(line.cubage or 0.0)
                    writer.writerow(
                        [
                            result["invoice_number"],
                            parcel.parcel_id,
                            line.sku_id,
                            line.rbu_description,
                            line.product_description,
                            quantity,
                            _fmt(line.height),
                            _fmt(line.width),
                            _fmt(line.length),
                            _fmt(line.weight),
                            _fmt(cube_per_unit),
                            _fmt(cube_per_unit * quantity),
                            "",
                            line.size_category,
                            line.pack_timestamp.isoformat(sep=" ") if line.pack_timestamp else "",
                            line.source_shipment_id,
                            line.source_package_id,
                            line.source_package_detail_id,
                            line.assignment_source,
                        ]
                    )
    print(f"PARCEL_CONTENTS={output_path}")


def _parcel_skus(parcel: ParcelBucket) -> str:
    skus: list[str] = []
    for row in parcel.lines:
        sku_id = str(row.sku_id)
        if sku_id not in skus:
            skus.append(sku_id)
    if len(skus) <= 5:
        return ",".join(skus)
    return ",".join(skus[:5]) + ",..."


def _parcel_sku_patterns(parcel: ParcelBucket) -> tuple[str, str]:
    ordered_skus: list[str] = []
    aggregated: dict[str, dict[str, float | int]] = {}

    for line in parcel.lines:
        sku_id = str(line.sku_id)
        qty = max(int(line.quantity_packed or 0), 0)
        if qty <= 0:
            continue
        if sku_id not in aggregated:
            ordered_skus.append(sku_id)
            aggregated[sku_id] = {"qty": 0, "cube": 0.0}
        aggregated[sku_id]["qty"] = int(aggregated[sku_id]["qty"]) + qty
        aggregated[sku_id]["cube"] = float(aggregated[sku_id]["cube"]) + (float(line.cubage or 0.0) * qty)

    qty_pattern = "|".join(str(int(aggregated[sku_id]["qty"])) for sku_id in ordered_skus)
    cube_pattern = "|".join(f"{float(aggregated[sku_id]['cube']):.2f}" for sku_id in ordered_skus)
    return qty_pattern, cube_pattern


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return f"{value:.2f}"


if __name__ == "__main__":
    main()
