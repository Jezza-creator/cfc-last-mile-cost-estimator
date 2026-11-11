from __future__ import annotations

import csv
from dataclasses import dataclass, field
from dataclasses import replace
from datetime import datetime
import math
import os
import statistics
from pathlib import Path
from typing import Iterable

from src.db import get_session
from src.extraction_window import EXTRACTION_END_DATE, EXTRACTION_START_DATE
from src.invoice_filter import invoice_filter_clause
from src.parcel_shape import estimate_rectangular_envelope, normalize_dimension_triplet
from src.site_config import SITE_CONFIGS


STORE_NAME = SITE_CONFIGS["MOOREBANK"].store_name
WAREHOUSE_CODE = SITE_CONFIGS["MOOREBANK"].inventory_warehouse_code
USE_SAMPLE_MODE = False
SAMPLE_ORDER_LIMIT = 50

# Short gaps are only used for the manual-wave fallback rule.
MANUAL_SESSION_GAP_SECONDS = 30
# Toteable orders are scored across a few candidate clustering windows so we
# can keep dense AMR bursts together without over-splitting slower flows.
TOTE_SESSION_GAP_CANDIDATES = (30, 45, 60, 90, 120, 180)
EXPORT_CSV = True
OUTPUT_DIR = Path(os.environ.get("LM_OUTPUT_DIR", "outputs"))
OUTPUT_FILE = "stage1_moorebank_parcel_assignments_full.csv"


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
    source_cubage: float | None
    size_category: str
    cfc_cube_imputed: bool = False
    source_shipment_id: str = ""
    source_package_id: str = ""
    source_package_detail_id: str = ""
    assignment_source: str = ""


@dataclass(frozen=True)
class ParcelShapeEstimate:
    packed_sku_weight_kg: float
    packed_sku_cube_cm3: float
    estimated_length_cm: float | None
    estimated_width_cm: float | None
    estimated_height_cm: float | None
    estimated_cube_cm3: float
    fill_pct_vs_packed_cube: float | None
    air_ratio_pct_vs_packed_cube: float | None
    shape_source: str


@dataclass
class ParcelBucket:
    parcel_id: int
    lines: list[PackedRow] = field(default_factory=list)
    total_weight_kg: float = 0.0
    total_cube: float = 0.0
    total_units: int = 0

    def add(self, row: PackedRow, quantity: int | None = None) -> None:
        qty = max(int(quantity if quantity is not None else row.quantity_packed or 0), 0)
        if qty <= 0:
            return
        self.lines.append(replace(row, quantity_packed=qty))
        self.total_units += qty
        self.total_weight_kg += float(row.weight or 0.0) * qty
        self.total_cube += float(row.cubage or 0.0) * qty

    def take_one_unit(self) -> PackedRow | None:
        for index in range(len(self.lines) - 1, -1, -1):
            line = self.lines[index]
            qty = max(int(line.quantity_packed or 0), 0)
            if qty <= 0:
                continue
            if qty == 1:
                self.lines.pop(index)
            else:
                self.lines[index] = replace(line, quantity_packed=qty - 1)
            self.total_units -= 1
            self.total_weight_kg -= float(line.weight or 0.0)
            self.total_cube -= float(line.cubage or 0.0)
            return replace(line, quantity_packed=1)
        return None


def _is_single_sku_order(rows: list[PackedRow]) -> bool:
    sku_ids = {row.sku_id for row in rows if int(row.quantity_packed or 0) > 0}
    return len(sku_ids) == 1


def _build_equal_split_parcels(rows: list[PackedRow], target_count: int) -> list[ParcelBucket]:
    parcels = [ParcelBucket(parcel_id=index + 1) for index in range(max(target_count, 1))]
    if not rows:
        return parcels

    total_units = sum(max(int(row.quantity_packed or 0), 0) for row in rows)
    if total_units <= 0:
        return parcels

    template = max(
        rows,
        key=lambda row: (int(row.quantity_packed or 0), row.pack_timestamp or datetime.min, row.sku_id),
    )
    total_weight = sum(float(row.weight or 0.0) * max(int(row.quantity_packed or 0), 0) for row in rows)
    total_cube = sum(float(row.cubage or 0.0) * max(int(row.quantity_packed or 0), 0) for row in rows)
    per_unit_weight = total_weight / total_units if total_units else float(template.weight or 0.0)
    per_unit_cube = total_cube / total_units if total_units else float(template.cubage or 0.0)
    synthetic_row = replace(template, weight=per_unit_weight, cubage=per_unit_cube)

    base_units, remainder = divmod(total_units, len(parcels))
    for index, parcel in enumerate(parcels):
        qty = base_units + (1 if index < remainder else 0)
        if qty > 0:
            parcel.add(synthetic_row, quantity=qty)
    return parcels


def _source_parcel_assignment(
    rows: list[PackedRow], actual_parcel_count: int
) -> tuple[list[ParcelBucket], str, int] | None:
    """Use fulfillment package IDs when they provide a complete parcel mapping."""
    target_count = max(int(actual_parcel_count or 0), 1)
    if target_count <= 1 or not rows:
        return None

    for field_name, rule_name in (
        ("source_package_id", "FULFILLMENT_DETAIL_PACKAGE_ALLOCATION"),
        ("source_shipment_id", "FULFILLMENT_DETAIL_SHIPMENT_ALLOCATION"),
    ):
        source_ids = [str(getattr(row, field_name, "") or "").strip() for row in rows]
        if not source_ids or any(not source_id for source_id in source_ids):
            continue

        ordered_source_ids = list(dict.fromkeys(source_ids))
        if len(ordered_source_ids) != target_count:
            continue

        parcels = [ParcelBucket(parcel_id=index + 1) for index in range(target_count)]
        parcel_by_source_id = {
            source_id: parcels[index] for index, source_id in enumerate(ordered_source_ids)
        }
        for row, source_id in zip(rows, source_ids):
            parcel_by_source_id[source_id].add(row)
        return parcels, rule_name, target_count
    return None


def main() -> None:
    session = get_session()
    try:
        rows = session.sql(_build_query()).collect()
        orders = _group_by_order(rows)
        _write_raw_product_cube_audit(orders)

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


def _build_query_legacy() -> str:
    invoice_filter = invoice_filter_clause("TO_VARCHAR(ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER)")
    sample_clause = (
        f"ORDER BY ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER LIMIT {SAMPLE_ORDER_LIMIT}"
        if USE_SAMPLE_MODE
        else ""
    )
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
        {sample_clause}
    ),
    packed_skus AS (
        SELECT DISTINCT p.SKU_ID
        FROM KSFDA.CATCH_CFC.PACKED p
        JOIN shippit s
          ON s.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
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
              ON ps.SKU_ID = kc.PRODUCT_SOURCE_IDENTIFIER
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
              ON ps.SKU_ID = dss.PRODUCT_SOURCE_IDENTIFIER
        )
        WHERE rn = 1
    ),
    packed AS (
        SELECT
            REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
            s.actual_parcel_count,
            s.actual_carrier,
            p.SKU_ID,
            COALESCE(h.RBU_DESCRIPTION, 'Unclassified') AS rbu_description,
            COALESCE(pml.ITEM_DESCRIPTION, pml.ITEM_NAME, '') AS product_description,
            p.QUANTITY_PACKED,
            p.PACK_TIMESTAMP,
            sa.HEIGHT,
            sa.WIDTH,
            sa.LENGTH,
            sa.WEIGHT,
            sa.CUBAGE AS SOURCE_CUBAGE_CM3,
            CASE
                WHEN sa.HEIGHT IS NOT NULL AND sa.WIDTH IS NOT NULL AND sa.LENGTH IS NOT NULL
                    THEN sa.HEIGHT * sa.WIDTH * sa.LENGTH
                ELSE NULL
            END AS SKU_CUBE_CM3,
            CASE
                WHEN sa.HEIGHT IS NULL OR sa.WIDTH IS NULL OR sa.LENGTH IS NULL THEN 'No dims'
                WHEN LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 50
                 AND (sa.HEIGHT + sa.WIDTH + sa.LENGTH
                      - LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)
                      - GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)) <= 55
                 AND GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 55
                 AND COALESCE(sa.WEIGHT, 0) <= 15 THEN 'Toteable'
                WHEN LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 105
                 AND (sa.HEIGHT + sa.WIDTH + sa.LENGTH
                      - LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)
                      - GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)) <= 105
                 AND GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 105
                 AND COALESCE(sa.WEIGHT, 0) <= 45 THEN 'Non-Toteable'
                WHEN LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 200
                 AND (sa.HEIGHT + sa.WIDTH + sa.LENGTH
                      - LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)
                      - GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)) <= 200
                 AND GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 200
                 AND COALESCE(sa.WEIGHT, 0) <= 80 THEN 'Bulky'
                ELSE 'Unknown'
            END AS size_category
        FROM KSFDA.CATCH_CFC.PACKED p
        JOIN shippit s
          ON s.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
        LEFT JOIN (
            SELECT *
            FROM (
                SELECT
                    sa.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY sa.SKU_ID
                        ORDER BY sa.EXTRACT_TIMESTAMP DESC
                    ) AS rn
                FROM KSFDA.CATCH_CFC.SKU_ATTRIBUTE sa
            )
            WHERE rn = 1
        ) sa
          ON sa.SKU_ID = p.SKU_ID
        LEFT JOIN hierarchy_latest h
          ON h.PRODUCT_SOURCE_IDENTIFIER = p.SKU_ID
        LEFT JOIN plm_latest pml
          ON pml.PRODUCT_SOURCE_IDENTIFIER = p.SKU_ID
    )
    SELECT
        invoice_number,
        actual_parcel_count,
        actual_carrier,
        SKU_ID,
        rbu_description,
        product_description,
        QUANTITY_PACKED,
        PACK_TIMESTAMP,
        HEIGHT,
        WIDTH,
        LENGTH,
        WEIGHT,
        SOURCE_CUBAGE_CM3,
        SKU_CUBE_CM3,
        size_category
    FROM packed
    ORDER BY invoice_number, PACK_TIMESTAMP, SKU_ID
    """


def _build_query() -> str:
    """Build the fulfillment-detail-first query with a conservative CATCH fallback."""
    invoice_filter = invoice_filter_clause("TO_VARCHAR(ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER)")
    sample_clause = (
        f"ORDER BY TO_VARCHAR(ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER) LIMIT {SAMPLE_ORDER_LIMIT}"
        if USE_SAMPLE_MODE
        else ""
    )
    return f"""
    WITH shippit AS (
        SELECT
            TO_VARCHAR(ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER) AS invoice_number,
            ORDER_PARCEL__PARCEL_COUNT AS actual_parcel_count,
            CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME AS actual_carrier
        FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
        WHERE STORE_ATTRIBUTE__STORE_NAME = '{STORE_NAME}'
          AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                BETWEEN TO_DATE('{EXTRACTION_START_DATE}') AND TO_DATE('{EXTRACTION_END_DATE}')
          AND ORDER_PARCEL__PARCEL_COUNT IS NOT NULL
          {invoice_filter}
        {sample_clause}
    ),
    packed AS (
        SELECT
            REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
            s.actual_parcel_count,
            s.actual_carrier,
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
        FROM packed p
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
    all_skus AS (
        SELECT DISTINCT SKU_ID
        FROM source_rows
    ),
    sku_latest AS (
        SELECT *
        FROM (
            SELECT
                sa.*,
                ROW_NUMBER() OVER (
                    PARTITION BY TO_VARCHAR(sa.SKU_ID)
                    ORDER BY sa.EXTRACT_TIMESTAMP DESC
                ) AS rn
            FROM KSFDA.CATCH_CFC.SKU_ATTRIBUTE sa
            JOIN all_skus a
              ON a.SKU_ID = TO_VARCHAR(sa.SKU_ID)
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
                    PARTITION BY TO_VARCHAR(kc.PRODUCT_SOURCE_IDENTIFIER)
                    ORDER BY TO_VARCHAR(kc.PRODUCT_SOURCE_IDENTIFIER)
                ) AS rn
            FROM KSFPA.MR2C.KEYCODE kc
            JOIN all_skus a
              ON a.SKU_ID = TO_VARCHAR(kc.PRODUCT_SOURCE_IDENTIFIER)
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
                    PARTITION BY TO_VARCHAR(dss.PRODUCT_SOURCE_IDENTIFIER)
                    ORDER BY
                        CASE
                            WHEN NULLIF(TRIM(COALESCE(dss.ITEM_DESCRIPTION, dss.ITEM_NAME)), '') IS NULL THEN 1
                            ELSE 0
                        END,
                        dss.EFFECTIVE_FROM DESC
                ) AS rn
            FROM KSFPA.MERCHANDISE_PVT.KEYCODE_CARTON_DSS_DIMENSIONS_VW dss
            JOIN all_skus a
              ON a.SKU_ID = TO_VARCHAR(dss.PRODUCT_SOURCE_IDENTIFIER)
        )
        WHERE rn = 1
    ),
    classified AS (
        SELECT
            p.invoice_number,
            p.actual_parcel_count,
            p.actual_carrier,
            p.SKU_ID,
            COALESCE(h.RBU_DESCRIPTION, 'Unclassified') AS rbu_description,
            COALESCE(pml.ITEM_DESCRIPTION, pml.ITEM_NAME, '') AS product_description,
            p.QUANTITY_PACKED,
            p.PACK_TIMESTAMP,
            sa.HEIGHT,
            sa.WIDTH,
            sa.LENGTH,
            sa.WEIGHT,
            sa.CUBAGE AS SOURCE_CUBAGE_CM3,
            CASE
                WHEN sa.HEIGHT IS NOT NULL AND sa.WIDTH IS NOT NULL AND sa.LENGTH IS NOT NULL
                    THEN sa.HEIGHT * sa.WIDTH * sa.LENGTH
                ELSE NULL
            END AS SKU_CUBE_CM3,
            CASE
                WHEN sa.HEIGHT IS NULL OR sa.WIDTH IS NULL OR sa.LENGTH IS NULL THEN 'No dims'
                WHEN LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 50
                 AND (sa.HEIGHT + sa.WIDTH + sa.LENGTH
                      - LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)
                      - GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)) <= 55
                 AND GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 55
                 AND COALESCE(sa.WEIGHT, 0) <= 15 THEN 'Toteable'
                WHEN LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 105
                 AND (sa.HEIGHT + sa.WIDTH + sa.LENGTH
                      - LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)
                      - GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)) <= 105
                 AND GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 105
                 AND COALESCE(sa.WEIGHT, 0) <= 45 THEN 'Non-Toteable'
                WHEN LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 200
                 AND (sa.HEIGHT + sa.WIDTH + sa.LENGTH
                      - LEAST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)
                      - GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH)) <= 200
                 AND GREATEST(sa.HEIGHT, sa.WIDTH, sa.LENGTH) <= 200
                 AND COALESCE(sa.WEIGHT, 0) <= 80 THEN 'Bulky'
                ELSE 'Unknown'
            END AS size_category,
            p.SOURCE_SHIPMENT_ID,
            p.SOURCE_PACKAGE_ID,
            p.SOURCE_PACKAGE_DETAIL_ID,
            p.ASSIGNMENT_SOURCE
        FROM source_rows p
        LEFT JOIN sku_latest sa
          ON TO_VARCHAR(sa.SKU_ID) = p.SKU_ID
        LEFT JOIN hierarchy_latest h
          ON TO_VARCHAR(h.PRODUCT_SOURCE_IDENTIFIER) = p.SKU_ID
        LEFT JOIN plm_latest pml
          ON TO_VARCHAR(pml.PRODUCT_SOURCE_IDENTIFIER) = p.SKU_ID
    )
    SELECT
        invoice_number,
        actual_parcel_count,
        actual_carrier,
        SKU_ID,
        rbu_description,
        product_description,
        QUANTITY_PACKED,
        PACK_TIMESTAMP,
        HEIGHT,
        WIDTH,
        LENGTH,
        WEIGHT,
        SOURCE_CUBAGE_CM3,
        SKU_CUBE_CM3,
        size_category,
        SOURCE_SHIPMENT_ID,
        SOURCE_PACKAGE_ID,
        SOURCE_PACKAGE_DETAIL_ID,
        ASSIGNMENT_SOURCE
    FROM classified
    ORDER BY invoice_number, PACK_TIMESTAMP, SOURCE_PACKAGE_ID, SKU_ID
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
                source_cubage=_to_float(record["SOURCE_CUBAGE_CM3"]),
                cubage=_to_float(record["SKU_CUBE_CM3"]),
                size_category=str(record["SIZE_CATEGORY"] or "Unknown"),
                source_shipment_id=str(record.get("SOURCE_SHIPMENT_ID") or ""),
                source_package_id=str(record.get("SOURCE_PACKAGE_ID") or ""),
                source_package_detail_id=str(record.get("SOURCE_PACKAGE_DETAIL_ID") or ""),
                assignment_source=str(record.get("ASSIGNMENT_SOURCE") or ""),
            )
        )

    for order in orders.values():
        order["rows"] = _impute_invoice_cubages(list(order["rows"]))
    return orders


def _impute_invoice_cubages(rows: list[PackedRow]) -> list[PackedRow]:
    known_units = 0
    known_cube = 0.0
    for row in rows:
        qty = max(int(row.quantity_packed or 0), 0)
        cube = _to_float(row.cubage)
        if qty > 0 and cube is not None and cube > 0:
            known_units += qty
            known_cube += cube * qty

    if known_units <= 0:
        return rows

    average_cube = known_cube / known_units
    imputed_rows: list[PackedRow] = []
    for row in rows:
        cube = _to_float(row.cubage)
        if cube is None or cube <= 0:
            imputed_rows.append(replace(row, cubage=average_cube, cfc_cube_imputed=True))
        else:
            imputed_rows.append(row)
    return imputed_rows


def _write_raw_product_cube_audit(orders: dict[str, dict[str, object]]) -> None:
    """Export the pre-assignment product-cube inputs for transparent reconciliation."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    detail_path = OUTPUT_DIR / "stage1_moorebank_raw_product_cube_audit.csv"
    summary_path = OUTPUT_DIR / "stage1_moorebank_invoice_cube_audit.csv"

    with detail_path.open("w", newline="", encoding="utf-8") as detail_file, summary_path.open(
        "w", newline="", encoding="utf-8"
    ) as summary_file:
        detail_writer = csv.writer(detail_file)
        summary_writer = csv.writer(summary_file)
        detail_writer.writerow(
            [
                "invoice_number",
                "sku_id",
                "pack_timestamp",
                "quantity_packed",
                "cfc_height_cm",
                "cfc_width_cm",
                "cfc_length_cm",
                "cfc_weight_kg",
                "source_cubage_cm3_per_unit",
                "derived_sku_cube_cm3_per_unit",
                "raw_product_cube_cm3_per_line",
                "cfc_cube_imputed",
                "size_category",
            ]
        )
        summary_writer.writerow(
            [
                "invoice_number",
                "packed_unit_count",
                "known_dimension_unit_count",
                "imputed_dimension_unit_count",
                "raw_product_cube_cm3",
                "imputed_product_cube_cm3",
                "total_product_cube_for_assignment_cm3",
            ]
        )

        for invoice_number, order in orders.items():
            rows = list(order["rows"])
            packed_units = 0
            known_units = 0
            imputed_units = 0
            raw_cube = 0.0
            imputed_cube = 0.0
            total_cube = 0.0

            for row in rows:
                qty = max(int(row.quantity_packed or 0), 0)
                cube_per_unit = float(row.cubage or 0.0)
                line_cube = cube_per_unit * qty
                packed_units += qty
                total_cube += line_cube
                if row.cfc_cube_imputed:
                    imputed_units += qty
                    imputed_cube += line_cube
                else:
                    known_units += qty
                    raw_cube += line_cube

                detail_writer.writerow(
                    [
                        invoice_number,
                        row.sku_id,
                        row.pack_timestamp.isoformat(sep=" ") if row.pack_timestamp else "",
                        qty,
                        _fmt(row.height),
                        _fmt(row.width),
                        _fmt(row.length),
                        _fmt(row.weight),
                        _fmt(row.source_cubage),
                        _fmt(row.cubage),
                        _fmt(line_cube),
                        "Y" if row.cfc_cube_imputed else "",
                        row.size_category,
                    ]
                )

            summary_writer.writerow(
                [
                    invoice_number,
                    packed_units,
                    known_units,
                    imputed_units,
                    _fmt(raw_cube),
                    _fmt(imputed_cube),
                    _fmt(total_cube),
                ]
            )

    print(f"RAW_PRODUCT_CUBE_AUDIT={detail_path}")
    print(f"INVOICE_CUBE_AUDIT={summary_path}")


def _write_parcel_content_output(results: list[dict[str, object]]) -> None:
    """Write the assigned SKU dimensions Stage 2 needs for parcel sizing."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "stage1_moorebank_parcel_assignment_lines.csv"
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
                    qty = max(int(line.quantity_packed or 0), 0)
                    if qty <= 0:
                        continue
                    cube_per_unit = float(line.cubage or 0.0)
                    writer.writerow(
                        [
                            result["invoice_number"],
                            parcel.parcel_id,
                            line.sku_id,
                            line.rbu_description,
                            line.product_description,
                            qty,
                            _fmt(line.height),
                            _fmt(line.width),
                            _fmt(line.length),
                            _fmt(line.weight),
                            _fmt(cube_per_unit),
                            _fmt(cube_per_unit * qty),
                            "Y" if line.cfc_cube_imputed else "",
                            line.size_category,
                            line.pack_timestamp.isoformat(sep=" ") if line.pack_timestamp else "",
                            line.source_shipment_id,
                            line.source_package_id,
                            line.source_package_detail_id,
                            line.assignment_source,
                        ]
                    )
    print(f"PARCEL_CONTENTS={output_path}")


def _estimate_parcel_shape(parcel: ParcelBucket) -> ParcelShapeEstimate:
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
        if estimate is None:
            estimated_length_cm, estimated_width_cm, estimated_height_cm = _fallback_dimensions_from_cube(
                packed_cube_cm3,
                parcel.total_units,
            )
            estimated_cube_cm3 = packed_cube_cm3
            shape_source = "CUBE_FALLBACK"
        else:
            estimated_length_cm = estimate.estimated_length_cm
            estimated_width_cm = estimate.estimated_width_cm
            estimated_height_cm = estimate.estimated_height_cm
            estimated_cube_cm3 = estimate.estimated_cube_cm3
            shape_source = estimate.shape_source
    else:
        estimated_length_cm, estimated_width_cm, estimated_height_cm = _fallback_dimensions_from_cube(
            packed_cube_cm3,
            parcel.total_units,
        )
        estimated_cube_cm3 = packed_cube_cm3
        shape_source = "CUBE_FALLBACK"

    fill_pct = (packed_cube_cm3 * 100.0 / estimated_cube_cm3) if estimated_cube_cm3 else None
    air_ratio_pct = (100.0 - fill_pct) if fill_pct is not None else None

    return ParcelShapeEstimate(
        packed_sku_weight_kg=packed_weight_kg,
        packed_sku_cube_cm3=packed_cube_cm3,
        estimated_length_cm=estimated_length_cm,
        estimated_width_cm=estimated_width_cm,
        estimated_height_cm=estimated_height_cm,
        estimated_cube_cm3=estimated_cube_cm3,
        fill_pct_vs_packed_cube=fill_pct,
        air_ratio_pct_vs_packed_cube=air_ratio_pct,
        shape_source=shape_source,
    )


def _process_path_from_rule_name(rule_name: str) -> str:
    rule = (rule_name or "").upper()
    if rule.startswith("ALL_TOTEABLE"):
        return "TOTEABLE"
    if rule.startswith("ALL_MANUAL"):
        return "MANUAL"
    if rule.startswith("CONTAINS_BULKY"):
        return "BULKY"
    if rule.startswith("SINGLE_SKU_EQUAL_SPLIT"):
        return "SINGLE_SKU"
    if rule.startswith("FULFILLMENT_DETAIL"):
        return "FULFILLMENT_DETAIL"
    if rule.startswith("FALLBACK"):
        return "FALLBACK"
    if "MIXED_MANUAL_TOTEABLE" in rule:
        return "MIXED"
    return "UNKNOWN"


def _estimated_pack_origin_path(rule_name: str) -> str:
    path = _process_path_from_rule_name(rule_name)
    return {
        "TOTEABLE": "Automated",
        "MANUAL": "Manual",
        "BULKY": "Bulky",
        "SINGLE_SKU": "Single SKU",
        "FULFILLMENT_DETAIL": "Fulfillment detail",
        "MIXED": "Mixed - Auto/ Manual",
        "FALLBACK": "Fallback",
    }.get(path, "Unknown")


def _parcel_contents_size_mix(parcel: ParcelBucket) -> str:
    categories = []
    for line in parcel.lines:
        category = str(line.size_category or "").strip()
        if category and category not in categories:
            categories.append(category)

    if not categories:
        return "Unknown"
    if len(categories) == 1:
        return f"{categories[0]} only"
    if len(categories) == 2:
        return f"Mixed {'/'.join(categories)}"
    return "Mixed " + "/".join(categories)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None

    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    if pct <= 0:
        return float(ordered[0])
    if pct >= 100:
        return float(ordered[-1])

    rank = (pct / 100.0) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[int(rank)])
    weight = rank - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _air_ratio_norm_key(result: dict[str, object], parcel: ParcelBucket) -> tuple[str, str, str]:
    return (
        _estimated_pack_origin_path(str(result.get("rule_name") or "")),
        _parcel_contents_size_mix(parcel),
        "SINGLE_UNIT" if parcel.total_units <= 1 else "MULTI_UNIT",
    )


def _air_ratio_norm_lookup(results: list[dict[str, object]]) -> dict[tuple[str, str, str], dict[str, float | int]]:
    grouped: dict[tuple[str, str, str], list[float]] = {}
    for result in results:
        if int(result.get("actual_parcel_count") or 0) != 1:
            continue
        for parcel in result["parcels"]:
            shape = _estimate_parcel_shape(parcel)
            if shape.air_ratio_pct_vs_packed_cube is None:
                continue
            key = _air_ratio_norm_key(result, parcel)
            grouped.setdefault(key, []).append(shape.air_ratio_pct_vs_packed_cube)

    summary: dict[tuple[str, str, str], dict[str, float | int]] = {}
    for key, values in grouped.items():
        non_zero_values = [value for value in values if abs(value) > 1e-9]
        summary[key] = {
            "sample_count": len(values),
            "non_zero_count": len(non_zero_values),
            "zero_count": len(values) - len(non_zero_values),
            "avg": sum(values) / len(values) if values else None,
            "p10": _percentile(values, 10),
            "p50": _percentile(values, 50),
            "p90": _percentile(values, 90),
            "min": min(values),
            "max": max(values),
        }
    return summary


def _format_pct(value: float | None) -> str:
    if value is None:
        return ""
    value = 0.0 if abs(value) < 1e-9 else value
    return f"{value:.2f}"


def _write_air_ratio_norm_summary(results: list[dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[tuple[str, str, str], list[float]] = {}
    for result in results:
        if int(result.get("actual_parcel_count") or 0) != 1:
            continue
        for parcel in result["parcels"]:
            shape = _estimate_parcel_shape(parcel)
            if shape.air_ratio_pct_vs_packed_cube is None:
                continue
            grouped.setdefault(_air_ratio_norm_key(result, parcel), []).append(shape.air_ratio_pct_vs_packed_cube)

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "estimated_pack_origin_path",
                "parcel_contents_size_mix",
                "parcel_unit_band",
                "sample_count",
                "non_zero_count",
                "zero_count",
                "avg_air_ratio_pct",
                "min_air_ratio_pct",
                "p10_air_ratio_pct",
                "p50_air_ratio_pct",
                "p90_air_ratio_pct",
                "max_air_ratio_pct",
            ]
        )
        for (origin_path, size_mix, unit_band), values in sorted(grouped.items()):
            non_zero_values = [value for value in values if abs(value) > 1e-9]
            writer.writerow(
                [
                    origin_path,
                    size_mix,
                    unit_band,
                    len(values),
                    len(non_zero_values),
                    len(values) - len(non_zero_values),
                    _format_pct(sum(values) / len(values) if values else None),
                    _format_pct(min(values)),
                    _format_pct(_percentile(values, 10)),
                    _format_pct(_percentile(values, 50)),
                    _format_pct(_percentile(values, 90)),
                    _format_pct(max(values)),
                ]
            )


def _write_air_ratio_norm_samples(results: list[dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "invoice_number",
                "parcel_id",
                "estimated_pack_origin_path",
                "parcel_contents_size_mix",
                "parcel_unit_band",
                "parcel_units",
                "raw_packed_sku_cube_cm3",
                "estimated_parcel_cube_cm3",
                "fill_pct_vs_packed_cube",
                "air_ratio_pct_vs_packed_cube",
                "estimated_shape_source",
            ]
        )
        for result in results:
            if int(result.get("actual_parcel_count") or 0) != 1:
                continue
            origin_path = _estimated_pack_origin_path(str(result.get("rule_name") or ""))
            for parcel in result["parcels"]:
                shape = _estimate_parcel_shape(parcel)
                size_mix = _parcel_contents_size_mix(parcel)
                unit_band = "SINGLE_UNIT" if parcel.total_units <= 1 else "MULTI_UNIT"
                writer.writerow(
                    [
                        result["invoice_number"],
                        parcel.parcel_id,
                        origin_path,
                        size_mix,
                        unit_band,
                        parcel.total_units,
                        f"{_raw_parcel_cube(parcel):.2f}",
                        f"{shape.estimated_cube_cm3:.2f}",
                        _format_pct(shape.fill_pct_vs_packed_cube),
                        _format_pct(shape.air_ratio_pct_vs_packed_cube),
                        shape.shape_source,
                    ]
                )


def _fallback_dimensions_from_cube(parcel_cube_cm3: float, parcel_units: int) -> tuple[float | None, float | None, float | None]:
    if parcel_cube_cm3 <= 0:
        return None, None, None

    side = parcel_cube_cm3 ** (1 / 3)
    elongation = 1.10 if parcel_units <= 2 else 1.20 if parcel_units <= 5 else 1.35
    length_cm = round(side * elongation, 1)
    width_cm = round(side, 1)
    height_cm = round(parcel_cube_cm3 / (length_cm * width_cm), 1) if length_cm and width_cm else None
    return length_cm, width_cm, height_cm


def _assign_order(rows: list[PackedRow], actual_parcel_count: int) -> tuple[list[ParcelBucket], str, int]:
    if not rows:
        return [], "EMPTY", 0

    source_assignment = _source_parcel_assignment(rows, actual_parcel_count)
    if source_assignment is not None:
        return source_assignment

    tote_rows = [row for row in rows if row.size_category == "Toteable"]
    manual_rows = [row for row in rows if row.size_category == "Non-Toteable"]
    bulky_rows = [row for row in rows if row.size_category == "Bulky"]
    other_rows = [row for row in rows if row.size_category not in {"Toteable", "Non-Toteable", "Bulky"}]

    total_units = sum(max(row.quantity_packed, 0) for row in rows)
    manual_session_count = _count_sessions(manual_rows, MANUAL_SESSION_GAP_SECONDS)
    target_count = max(int(actual_parcel_count or 0), 1)
    tote_gap_seconds = _best_fit_session_gap_seconds(tote_rows or rows, target_count)

    if target_count > 1 and _is_single_sku_order(rows):
        parcels = _build_equal_split_parcels(rows, target_count)
        return parcels, "SINGLE_SKU_EQUAL_SPLIT", target_count

    if len(tote_rows) == len(rows):
        rule_name = f"ALL_TOTEABLE_TIME_CLUSTERED_{tote_gap_seconds}S"
        parcels = [ParcelBucket(parcel_id=index + 1) for index in range(target_count)]
        tote_groups = _session_groups(rows, tote_gap_seconds)
        for group in tote_groups:
            # A time cluster represents one continuous packing burst. Preserve it as
            # a parcel candidate; only _rebalance_empty_parcels may move one unit.
            parcel = min(parcels, key=_parcel_load_score)
            # Keep chronological line order so a minimal rebalance takes the
            # final packed unit, rather than an arbitrary low-cube SKU.
            for row in group:
                parcel.add(row)
        _rebalance_empty_parcels(parcels)
        return parcels, rule_name, target_count

    if bulky_rows:
        rule_name = "CONTAINS_BULKY"
    elif manual_rows and tote_rows:
        rule_name = "MIXED_MANUAL_TOTEABLE"
    else:
        rule_name = "FALLBACK_ROUND_ROBIN"

    parcels = [ParcelBucket(parcel_id=index + 1) for index in range(target_count)]

    grouped_rows: list[list[PackedRow]] = []
    grouped_rows.extend([[row] for row in sorted(bulky_rows, key=_assignment_sort_key, reverse=True)])
    grouped_rows.extend(_session_groups(manual_rows, MANUAL_SESSION_GAP_SECONDS))
    grouped_rows.extend(_session_groups(tote_rows, tote_gap_seconds))
    grouped_rows.extend([[row] for row in sorted(other_rows, key=_assignment_sort_key, reverse=True)])

    for group in grouped_rows:
        # Keep a manual or toteable packing burst intact before considering the
        # next group, rather than redistributing its rows across parcel buckets.
        parcel = min(parcels, key=_parcel_load_score)
        for row in sorted(group, key=_assignment_sort_key, reverse=True):
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


def _adaptive_session_gap_seconds(rows: list[PackedRow]) -> int:
    ordered_rows = sorted((row for row in rows if row.pack_timestamp is not None), key=lambda row: row.pack_timestamp)
    if len(ordered_rows) <= 1:
        return 30

    gaps = [
        int((current.pack_timestamp - previous.pack_timestamp).total_seconds())
        for previous, current in zip(ordered_rows, ordered_rows[1:])
        if (current.pack_timestamp - previous.pack_timestamp).total_seconds() >= 0
    ]
    if not gaps:
        return 30

    gaps.sort()
    midpoint = len(gaps) // 2
    median_gap = gaps[midpoint] if len(gaps) % 2 else (gaps[midpoint - 1] + gaps[midpoint]) / 2

    if median_gap <= 5:
        return 30
    if median_gap <= 15:
        return 45
    if median_gap <= 30:
        return 60
    if median_gap <= 60:
        return 90
    return 120


def _best_fit_session_gap_seconds(rows: list[PackedRow], target_count: int) -> int:
    ordered_rows = [row for row in rows if row.pack_timestamp is not None]
    if len(ordered_rows) <= 1:
        return 30

    best_gap = TOTE_SESSION_GAP_CANDIDATES[0]
    best_score: tuple[float, float, int] | None = None

    for gap_seconds in TOTE_SESSION_GAP_CANDIDATES:
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


def _count_sessions(rows: list[PackedRow], gap_seconds: int) -> int:
    return len(_session_groups(rows, gap_seconds))


def _parcel_load_score(parcel: ParcelBucket) -> float:
    return parcel.total_weight_kg + (parcel.total_cube / 100000.0) + (parcel.total_units * 0.01)


def _rebalance_empty_parcels(parcels: list[ParcelBucket]) -> None:
    if not parcels:
        return

    total_units = sum(parcel.total_units for parcel in parcels)
    if total_units < len(parcels):
        return

    empties = [parcel for parcel in parcels if parcel.total_units == 0]
    while empties:
        donor = max((parcel for parcel in parcels if parcel.total_units > 1), key=_parcel_load_score, default=None)
        if donor is None:
            break
        recipient = empties.pop(0)
        unit_row = donor.take_one_unit()
        if unit_row is None:
            break
        recipient.add(unit_row, quantity=1)
        empties = [parcel for parcel in parcels if parcel.total_units == 0]


def _assignment_sort_key(row: PackedRow) -> tuple[float, float, datetime, str]:
    return (
        float(row.cubage or 0.0),
        float(row.weight or 0.0),
        row.pack_timestamp or datetime.min,
        row.sku_id,
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
    if USE_SAMPLE_MODE:
        print(f"MODE=SAMPLE LIMIT={SAMPLE_ORDER_LIMIT}")
    else:
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
            f"rule={result['rule_name']}"
            ")"
        )
        for parcel in result["parcels"][:3]:
            shape = _estimate_parcel_shape(parcel)
            print(
                "  Parcel("
                f"id={parcel.parcel_id}, "
                f"units={parcel.total_units}, "
                f"weight_kg={parcel.total_weight_kg:.2f}, "
                f"packed_cube={shape.packed_sku_cube_cm3:.2f}, "
                f"estimated_cube={shape.estimated_cube_cm3:.2f}, "
                f"skus={_parcel_skus(parcel)}"
                ")"
            )


def _export_csv(results: list[dict[str, object]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / OUTPUT_FILE
    air_ratio_norms = _air_ratio_norm_lookup(results)
    _write_air_ratio_norm_summary(results, OUTPUT_DIR / "stage1_moorebank_air_ratio_norm_summary.csv")
    _write_air_ratio_norm_samples(results, OUTPUT_DIR / "stage1_moorebank_air_ratio_norm_samples.csv")

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
                "process_path",
                "estimated_pack_origin_path",
                "parcel_contents_size_mix",
                "invoice_raw_product_cube_cm3",
                "invoice_estimated_parcel_cube_cm3",
                "invoice_rectangularization_uplift_cm3",
                "air_ratio_norm_sample_count",
                "air_ratio_norm_p10_pct",
                "air_ratio_norm_p50_pct",
                "air_ratio_norm_p90_pct",
                "air_ratio_norm_unit_band",
                "parcel_id",
                "placeholder_parcel",
                "parcel_units",
                "parcel_weight_kg",
                "packed_sku_weight_kg",
                "source_cubage_cm3",
                "sku_cube_cm3",
                "packed_sku_cube_cm3",
                "parcel_cube",
                "raw_packed_sku_cube_cm3",
                "estimated_parcel_cube_cm3",
                "cube_uplift_cm3",
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
            invoice_raw_product_cube_cm3 = 0.0
            invoice_estimated_parcel_cube_cm3 = 0.0
            for parcel in result["parcels"]:
                invoice_raw_product_cube_cm3 += _raw_parcel_cube(parcel)
                invoice_estimated_parcel_cube_cm3 += max(_estimate_parcel_shape(parcel).estimated_cube_cm3, 0.0)
            invoice_rectangularization_uplift_cm3 = max(
                invoice_estimated_parcel_cube_cm3 - invoice_raw_product_cube_cm3,
                0.0,
            )
            for parcel in result["parcels"]:
                shape = _estimate_parcel_shape(parcel)
                process_path = _process_path_from_rule_name(str(result["rule_name"]))
                origin_path = _estimated_pack_origin_path(str(result["rule_name"]))
                size_mix = _parcel_contents_size_mix(parcel)
                unit_band = "SINGLE_UNIT" if parcel.total_units <= 1 else "MULTI_UNIT"
                norms = air_ratio_norms.get((origin_path, size_mix, unit_band), {})
                raw_parcel_cube_cm3 = _raw_parcel_cube(parcel)
                sku_qty_pattern, sku_packed_cube_pattern_cm3 = _parcel_sku_patterns(parcel)
                writer.writerow(
                    [
                        result["invoice_number"],
                        result["actual_carrier"],
                        result["actual_parcel_count"],
                        result["estimated_parcel_count"],
                        result["rule_name"],
                        result["assignment_source"],
                        process_path,
                        origin_path,
                        size_mix,
                        round(invoice_raw_product_cube_cm3, 3),
                        round(invoice_estimated_parcel_cube_cm3, 3),
                        round(invoice_rectangularization_uplift_cm3, 3),
                        norms.get("sample_count", ""),
                        _format_pct(norms.get("p10")),
                        _format_pct(norms.get("p50")),
                        _format_pct(norms.get("p90")),
                        unit_band,
                        parcel.parcel_id,
                        "Y" if parcel.total_units == 0 else "",
                        parcel.total_units,
                        round(parcel.total_weight_kg, 3),
                        round(shape.packed_sku_weight_kg, 3),
                        round(sum(float(line.source_cubage or 0.0) * max(int(line.quantity_packed or 0), 0) for line in parcel.lines), 3),
                        round(sum(float(line.cubage or 0.0) * max(int(line.quantity_packed or 0), 0) for line in parcel.lines), 3),
                        round(shape.packed_sku_cube_cm3, 3),
                        round(shape.estimated_cube_cm3, 3),
                        round(raw_parcel_cube_cm3, 3),
                        round(shape.estimated_cube_cm3, 3),
                        round(max(shape.estimated_cube_cm3 - shape.packed_sku_cube_cm3, 0.0), 3),
                        _fmt(shape.estimated_length_cm),
                        _fmt(shape.estimated_width_cm),
                        _fmt(shape.estimated_height_cm),
                        _fmt(shape.fill_pct_vs_packed_cube),
                        _fmt(shape.air_ratio_pct_vs_packed_cube),
                        shape.shape_source,
                        "Y" if any(line.cfc_cube_imputed for line in parcel.lines) else "",
                        sku_qty_pattern,
                        sku_packed_cube_pattern_cm3,
                        _parcel_skus(parcel),
                    ]
                )

    print(f"\nCSV_EXPORTED={output_path}")


def _parcel_skus(parcel: ParcelBucket) -> str:
    skus: list[str] = []
    for row in parcel.lines:
        if row.sku_id not in skus:
            skus.append(row.sku_id)
    if len(skus) <= 5:
        return ",".join(skus)
    return ",".join(skus[:5]) + ",..."


def _assignment_source(rows: Iterable[PackedRow]) -> str:
    sources = list(dict.fromkeys(str(getattr(row, "assignment_source", "") or "").strip() for row in rows))
    sources = [source for source in sources if source]
    return sources[0] if len(sources) == 1 else "CATCH_CFC.PACKED"


def _raw_parcel_cube(parcel: ParcelBucket) -> float:
    raw_cube = 0.0
    for line in parcel.lines:
        if line.cfc_cube_imputed:
            continue
        qty = max(int(line.quantity_packed or 0), 0)
        raw_cube += float(line.cubage or 0.0) * qty
    return raw_cube


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
