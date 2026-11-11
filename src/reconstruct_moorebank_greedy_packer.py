from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from src.db import get_session


STORE_NAME = "1194 Moorebank CFC NSW AU"
DAYS_BACK = 6
USE_SAMPLE_MODE = True
SAMPLE_ORDER_LIMIT = 20
INVENTORY_DAYS_BACK = 7
SESSION_GAP_SECONDS = 10

# Starting capacities based on the sizing clauses shared by the user.
# Slightly relaxed for the toteable path so we can reduce small overcounts.
TOTE_WEIGHT_LIMIT_KG = 16.0
TOTE_CUBE_LIMIT = 55.0 * 55.0 * 55.0
MANUAL_WEIGHT_LIMIT_KG = 45.0
MANUAL_CUBE_LIMIT = 105.0 * 105.0 * 105.0


BASE_CTE = f"""
WITH shippit AS (
    SELECT
        ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number,
        ORDER_PARCEL__PARCEL_COUNT AS actual_parcel_count,
        CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME AS actual_carrier,
        GREATEST(
            ORDER_WEIGHT__DEAD_WEIGHT,
            ORDER_WEIGHT__VOLUMETRIC_WEIGHT
        ) AS chargeable_kg
    FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
    WHERE STORE_ATTRIBUTE__STORE_NAME = '{STORE_NAME}'
      AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
            BETWEEN DATEADD(day, -{DAYS_BACK}, CURRENT_DATE()) AND CURRENT_DATE()
      AND ORDER_PARCEL__PARCEL_COUNT IS NOT NULL
    {"ORDER BY ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER LIMIT " + str(SAMPLE_ORDER_LIMIT) if USE_SAMPLE_MODE else ""}
),
packed AS (
    SELECT
        REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
        s.actual_parcel_count,
        s.actual_carrier,
        s.chargeable_kg,
        p.ORDER_ID,
        p.SKU_ID,
        p.QUANTITY_PACKED,
        p.PACK_TIMESTAMP,
        ROW_NUMBER() OVER (
            ORDER BY
                REGEXP_REPLACE(p.ORDER_ID, '^KMT', ''),
                p.ORDER_ID,
                p.SKU_ID,
                p.PACK_TIMESTAMP,
                p.QUANTITY_PACKED
        ) AS packed_row_id
    FROM KSFDA.CATCH_CFC.PACKED p
    JOIN shippit s
      ON s.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
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
    )
    WHERE rn = 1
),
inventory_candidate AS (
    SELECT i.*
    FROM KSFDA.CATCH_CFC.INVENTORY i
    JOIN packed_skus ps
      ON ps.SKU_ID = i.SKU_ID
    WHERE i.WAREHOUSE_CODE = 'SYDM01'
      AND i.EXTRACT_TIMESTAMP::DATE BETWEEN DATEADD(day, -{INVENTORY_DAYS_BACK}, CURRENT_DATE()) AND CURRENT_DATE()
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
inventory_aligned AS (
    SELECT
        p.packed_row_id,
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
        END AS storage_type
    FROM packed p
    JOIN inventory_candidate i
      ON i.SKU_ID = p.SKU_ID
    LEFT JOIN bin_latest b
      ON i.BIN_LOCATION = b.BIN_LOCATION
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY p.packed_row_id
        ORDER BY ABS(DATEDIFF(second, i.EXTRACT_TIMESTAMP, p.PACK_TIMESTAMP)), i.EXTRACT_TIMESTAMP DESC
    ) = 1
),
row_level AS (
    SELECT
        p.invoice_number,
        p.actual_parcel_count,
        p.actual_carrier,
        p.SKU_ID,
        p.QUANTITY_PACKED,
        p.PACK_TIMESTAMP,
        s.HEIGHT,
        s.WIDTH,
        s.LENGTH,
        s.WEIGHT,
        s.CUBAGE,
        CASE
            WHEN LEAST(s.HEIGHT, s.WIDTH, s.LENGTH) <= 50
                 AND (s.HEIGHT + s.WIDTH + s.LENGTH
                      - LEAST(s.HEIGHT, s.WIDTH, s.LENGTH)
                      - GREATEST(s.HEIGHT, s.WIDTH, s.LENGTH)) <= 55
                 AND GREATEST(s.HEIGHT, s.WIDTH, s.LENGTH) <= 55
                 AND s.WEIGHT <= 15
                THEN 'Toteable'
            WHEN LEAST(s.HEIGHT, s.WIDTH, s.LENGTH) <= 105
                 AND (s.HEIGHT + s.WIDTH + s.LENGTH
                      - LEAST(s.HEIGHT, s.WIDTH, s.LENGTH)
                      - GREATEST(s.HEIGHT, s.WIDTH, s.LENGTH)) <= 105
                 AND GREATEST(s.HEIGHT, s.WIDTH, s.LENGTH) <= 105
                 AND s.WEIGHT <= 45
                THEN 'Non-Toteable'
            WHEN LEAST(s.HEIGHT, s.WIDTH, s.LENGTH) <= 200
                 AND (s.HEIGHT + s.WIDTH + s.LENGTH
                      - LEAST(s.HEIGHT, s.WIDTH, s.LENGTH)
                      - GREATEST(s.HEIGHT, s.WIDTH, s.LENGTH)) <= 200
                 AND GREATEST(s.HEIGHT, s.WIDTH, s.LENGTH) <= 200
                 AND s.WEIGHT <= 80
                THEN 'Bulky'
            WHEN s.HEIGHT IS NULL OR s.WIDTH IS NULL OR s.LENGTH IS NULL
                THEN 'No dims'
            ELSE 'Unknown'
        END AS size_category_sydm01,
        COALESCE(i.storage_type, 'No inventory mapping') AS storage_type
    FROM packed p
    LEFT JOIN sku_latest s
      ON s.SKU_ID = p.SKU_ID
    LEFT JOIN inventory_aligned i
      ON i.packed_row_id = p.packed_row_id
)
"""


@dataclass(frozen=True)
class PackedUnit:
    flow: str
    pack_timestamp: datetime
    weight_kg: float
    cube: float
    units: int


def main() -> None:
    session = get_session()
    try:
        rows = session.sql(
            f"""
            {BASE_CTE}
            SELECT
                invoice_number,
                actual_parcel_count,
                actual_carrier,
                SKU_ID,
                QUANTITY_PACKED,
                PACK_TIMESTAMP,
                HEIGHT,
                WIDTH,
                LENGTH,
                WEIGHT,
                CUBAGE,
                size_category_sydm01,
                storage_type
            FROM row_level
            ORDER BY invoice_number, PACK_TIMESTAMP, SKU_ID
            """
        ).collect()

        orders: dict[str, dict[str, object]] = {}
        for row in rows:
            invoice_number = row["INVOICE_NUMBER"]
            order = orders.setdefault(
                invoice_number,
                {
                    "invoice_number": invoice_number,
                    "actual_parcel_count": int(row["ACTUAL_PARCEL_COUNT"] or 0),
                    "actual_carrier": row["ACTUAL_CARRIER"],
                    "units": [],
                },
            )
            order["units"].append(
                _row_to_unit(row)
            )

        metrics = _score_orders(orders.values())

        print("SUMMARY")
        if USE_SAMPLE_MODE:
            print(f"MODE=SAMPLE LIMIT={SAMPLE_ORDER_LIMIT}")
        else:
            print("MODE=FULL")
        print(
            "Row("
            f"ORDER_COUNT={metrics['order_count']}, "
            f"AVG_ACTUAL_PARCEL_COUNT={metrics['avg_actual_parcel_count']:.2f}, "
            f"AVG_INFERRED_PARCEL_COUNT={metrics['avg_inferred_parcel_count']:.2f}, "
            f"MEAN_ABS_ERROR={metrics['mean_abs_error']:.2f}, "
            f"EXACT_MATCH_PCT={metrics['exact_match_pct']:.2f}, "
            f"UNDERCOUNT_PCT={metrics['undercount_pct']:.2f}, "
            f"OVERCOUNT_PCT={metrics['overcount_pct']:.2f}"
            ")"
        )

        print("\nTOP MISMATCHES")
        for mismatch in metrics["mismatches"][:50]:
            print(mismatch)
    finally:
        session.close()


def _row_to_unit(row: dict[str, object]) -> PackedUnit:
    size_category = row["SIZE_CATEGORY_SYDM01"]
    storage_type = row["STORAGE_TYPE"] or "No inventory mapping"
    weight = float(row["WEIGHT"] or 0)
    cube = float(row["CUBAGE"] or 0)
    qty = int(row["QUANTITY_PACKED"] or 0)
    pack_timestamp = row["PACK_TIMESTAMP"]

    manual_storage_types = {
        "PFR",
        "Ground Pallet",
        "Pallet Racking",
        "VNA Shelving / Pallet",
        "Shelving with Ground Pallet",
        "KNAPP",
    }
    tote_storage_types = {
        "AMR",
        "Tote Tower",
        "Consumables/AMR Reserve Racking",
        "Hanger Racks/AMR Reserve Shelving",
    }

    if storage_type in manual_storage_types:
        flow = "MANUAL"
    elif size_category == "Bulky":
        flow = "BULKY"
    elif size_category == "Non-Toteable":
        flow = "MANUAL"
    elif size_category == "Toteable" or storage_type in tote_storage_types:
        flow = "TOTE"
    else:
        flow = "MANUAL"

    return PackedUnit(
        flow=flow,
        pack_timestamp=pack_timestamp,
        weight_kg=weight,
        cube=cube,
        units=max(qty, 0),
    )


def _count_sessions(units: Iterable[PackedUnit], session_gap_seconds: int) -> int:
    ordered_units = sorted(
        (unit for unit in units if unit.units > 0 and unit.pack_timestamp is not None),
        key=lambda item: item.pack_timestamp,
    )
    if not ordered_units:
        return 0

    sessions = 0
    previous_timestamp: datetime | None = None

    for item in ordered_units:
        if previous_timestamp is None:
            sessions += 1
        else:
            row_gap_seconds = (item.pack_timestamp - previous_timestamp).total_seconds()
            if row_gap_seconds > session_gap_seconds:
                sessions += 1
        previous_timestamp = item.pack_timestamp

    return sessions


def _score_orders(orders: Iterable[dict[str, object]]) -> dict[str, object]:
    actual_counts: list[int] = []
    inferred_counts: list[int] = []
    mismatches: list[dict[str, object]] = []

    for order in orders:
        units: list[PackedUnit] = sorted(
            order["units"], key=lambda item: (item.pack_timestamp, item.flow, item.weight_kg, item.cube)
        )
        tote_units = [u for u in units if u.flow == "TOTE"]
        manual_units = [u for u in units if u.flow == "MANUAL"]
        bulky_units = [u for u in units if u.flow == "BULKY"]

        inferred = (
            _count_sessions(tote_units, gap_seconds=SESSION_GAP_SECONDS)
            # Manual/PFR picks are clunky and usually move as their own
            # pick sessions rather than as dense multi-item packs.
            + _count_sessions(manual_units, gap_seconds=SESSION_GAP_SECONDS)
            + sum(u.units for u in bulky_units)
        )

        actual = int(order["actual_parcel_count"])
        actual_counts.append(actual)
        inferred_counts.append(inferred)

        mismatches.append(
            {
                "invoice_number": order["invoice_number"],
                "actual": actual,
                "inferred": inferred,
                "error": abs(inferred - actual),
                "direction": "EXACT" if inferred == actual else ("UNDER" if inferred < actual else "OVER"),
                "carrier": order["actual_carrier"],
            }
        )

    order_count = len(actual_counts)
    mean_abs_error = sum(item["error"] for item in mismatches) / order_count if order_count else 0.0
    exact_match_pct = sum(1 for item in mismatches if item["direction"] == "EXACT") * 100 / order_count if order_count else 0.0
    undercount_pct = sum(1 for item in mismatches if item["direction"] == "UNDER") * 100 / order_count if order_count else 0.0
    overcount_pct = sum(1 for item in mismatches if item["direction"] == "OVER") * 100 / order_count if order_count else 0.0
    avg_actual = sum(actual_counts) / order_count if order_count else 0.0
    avg_inferred = sum(inferred_counts) / order_count if order_count else 0.0

    mismatch_rows = sorted(mismatches, key=lambda item: item["error"], reverse=True)

    return {
        "order_count": order_count,
        "avg_actual_parcel_count": avg_actual,
        "avg_inferred_parcel_count": avg_inferred,
        "mean_abs_error": mean_abs_error,
        "exact_match_pct": exact_match_pct,
        "undercount_pct": undercount_pct,
        "overcount_pct": overcount_pct,
        "mismatches": mismatch_rows,
    }


if __name__ == "__main__":
    main()
