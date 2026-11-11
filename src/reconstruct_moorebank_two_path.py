from __future__ import annotations

import math

from src.db import get_session


STORE_NAME = "1194 Moorebank CFC NSW AU"
DAYS_BACK = 6
BUCKET_SECONDS = 120


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
),
packed AS (
    SELECT
        REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
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
      AND i.EXTRACT_TIMESTAMP::DATE BETWEEN DATEADD(day, -14, CURRENT_DATE()) AND CURRENT_DATE()
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
        p.SKU_ID,
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
classified AS (
    SELECT
        p.invoice_number,
        p.ORDER_ID,
        p.packed_row_id,
        p.SKU_ID,
        p.QUANTITY_PACKED,
        p.PACK_TIMESTAMP,
        DATEADD(
            second,
            FLOOR(DATEDIFF(second, '1970-01-01'::timestamp_ntz, p.PACK_TIMESTAMP) / {BUCKET_SECONDS}) * {BUCKET_SECONDS},
            '1970-01-01'::timestamp_ntz
        ) AS pack_bucket_ts,
        s.HEIGHT,
        s.WIDTH,
        s.LENGTH,
        s.WEIGHT,
        s.CUBAGE,
        s.IS_OVERSIZE_SYDM01,
        s.IS_OVERSIZE_TRUG,
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
        COALESCE(w.storage_type, 'No inventory mapping') AS storage_type
    FROM packed p
    LEFT JOIN sku_latest s
      ON s.SKU_ID = p.SKU_ID
    LEFT JOIN inventory_aligned w
      ON w.packed_row_id = p.packed_row_id
),
order_rollup AS (
    SELECT
        invoice_number,
        COUNT(*) AS packed_rows,
        SUM(QUANTITY_PACKED) AS qty_packed,
        COUNT(DISTINCT CASE WHEN size_category_sydm01 = 'Toteable' THEN pack_bucket_ts END) AS toteable_buckets,
        COUNT(DISTINCT CASE WHEN size_category_sydm01 IN ('Non-Toteable', 'Bulky') THEN pack_bucket_ts END) AS manual_buckets,
        COUNT(DISTINCT CASE WHEN size_category_sydm01 = 'Bulky' THEN pack_bucket_ts END) AS bulky_buckets,
        SUM(CASE WHEN size_category_sydm01 = 'Toteable' THEN COALESCE(WEIGHT, 0) * QUANTITY_PACKED ELSE 0 END) AS toteable_weight_kg,
        SUM(CASE WHEN size_category_sydm01 = 'Toteable' THEN COALESCE(CUBAGE, 0) * QUANTITY_PACKED ELSE 0 END) AS toteable_cubage,
        SUM(CASE WHEN size_category_sydm01 IN ('Non-Toteable', 'Bulky') THEN COALESCE(WEIGHT, 0) * QUANTITY_PACKED ELSE 0 END) AS manual_weight_kg,
        SUM(CASE WHEN size_category_sydm01 IN ('Non-Toteable', 'Bulky') THEN COALESCE(CUBAGE, 0) * QUANTITY_PACKED ELSE 0 END) AS manual_cubage,
        COUNT_IF(storage_type = 'AMR') AS amr_rows,
        COUNT_IF(storage_type = 'PFR') AS pfr_rows,
        COUNT_IF(storage_type = 'Tote Tower') AS tote_tower_rows,
        COUNT_IF(size_category_sydm01 = 'Bulky') AS bulky_rows,
        COUNT_IF(size_category_sydm01 = 'No dims') AS no_dims_rows
    FROM classified
    GROUP BY 1
)
"""


def _ceil_div(value: float, limit: float) -> int:
    if value <= 0 or limit <= 0:
        return 0
    return int(math.ceil(value / limit))


def main() -> None:
    session = get_session()
    try:
        summary_rows = session.sql(
            f"""
            {BASE_CTE}
            SELECT
                COUNT(*) AS order_count,
                ROUND(AVG(s.actual_parcel_count), 2) AS avg_actual_parcel_count,
                ROUND(AVG(
                    GREATEST(
                        COALESCE(r.toteable_buckets, 0),
                        COALESCE(r.manual_buckets, 0),
                        COALESCE(r.bulky_rows, 0)
                    )
                ), 2) AS avg_inferred_parcel_count,
                ROUND(AVG(
                    ABS(
                        GREATEST(
                            COALESCE(r.toteable_buckets, 0),
                            COALESCE(r.manual_buckets, 0),
                            COALESCE(r.bulky_rows, 0)
                        ) - s.actual_parcel_count
                    )
                ), 2) AS mean_abs_error,
                ROUND(AVG(
                    IFF(
                        GREATEST(
                            COALESCE(r.toteable_buckets, 0),
                            COALESCE(r.manual_buckets, 0),
                            COALESCE(r.bulky_rows, 0)
                        ) = s.actual_parcel_count,
                        1,
                        0
                    )
                ) * 100, 2) AS exact_match_pct,
                ROUND(AVG(
                    IFF(
                        GREATEST(
                            COALESCE(r.toteable_buckets, 0),
                            COALESCE(r.manual_buckets, 0),
                            COALESCE(r.bulky_rows, 0)
                        ) < s.actual_parcel_count,
                        1,
                        0
                    )
                ) * 100, 2) AS undercount_pct,
                ROUND(AVG(
                    IFF(
                        GREATEST(
                            COALESCE(r.toteable_buckets, 0),
                            COALESCE(r.manual_buckets, 0),
                            COALESCE(r.bulky_rows, 0)
                        ) > s.actual_parcel_count,
                        1,
                        0
                    )
                ) * 100, 2) AS overcount_pct,
                ROUND(AVG(COALESCE(r.packed_rows, 0)), 2) AS avg_packed_rows,
                ROUND(AVG(COALESCE(r.amr_rows, 0)), 2) AS avg_amr_rows,
                ROUND(AVG(COALESCE(r.pfr_rows, 0)), 2) AS avg_pfr_rows
            FROM shippit s
            LEFT JOIN order_rollup r
              ON r.invoice_number = s.invoice_number
            """
        ).collect()

        print("SUMMARY")
        for row in summary_rows:
            print(row)

        print("\nTOP MISMATCHES")
        mismatch_rows = session.sql(
            f"""
            {BASE_CTE}
            SELECT
                s.invoice_number,
                s.actual_carrier,
                s.actual_parcel_count,
                COALESCE(r.packed_rows, 0) AS packed_rows,
                COALESCE(r.toteable_buckets, 0) AS toteable_buckets,
                COALESCE(r.manual_buckets, 0) AS manual_buckets,
                COALESCE(r.bulky_rows, 0) AS bulky_rows,
                COALESCE(r.amr_rows, 0) AS amr_rows,
                COALESCE(r.pfr_rows, 0) AS pfr_rows,
                COALESCE(r.no_dims_rows, 0) AS no_dims_rows,
                COALESCE(r.toteable_weight_kg, 0) AS toteable_weight_kg,
                COALESCE(r.manual_weight_kg, 0) AS manual_weight_kg,
                COALESCE(r.toteable_cubage, 0) AS toteable_cubage,
                COALESCE(r.manual_cubage, 0) AS manual_cubage,
                COALESCE(r.tote_tower_rows, 0) AS tote_tower_rows,
                GREATEST(
                    COALESCE(r.toteable_buckets, 0),
                    COALESCE(r.manual_buckets, 0),
                    COALESCE(r.bulky_rows, 0)
                ) AS inferred_parcel_count,
                ABS(
                    GREATEST(
                        COALESCE(r.toteable_buckets, 0),
                        COALESCE(r.manual_buckets, 0),
                        COALESCE(r.bulky_rows, 0)
                    ) - s.actual_parcel_count
                ) AS parcel_count_error,
                CASE
                    WHEN GREATEST(
                        COALESCE(r.toteable_buckets, 0),
                        COALESCE(r.manual_buckets, 0),
                        COALESCE(r.bulky_rows, 0)
                    ) = s.actual_parcel_count THEN 'EXACT'
                    WHEN GREATEST(
                        COALESCE(r.toteable_buckets, 0),
                        COALESCE(r.manual_buckets, 0),
                        COALESCE(r.bulky_rows, 0)
                    ) < s.actual_parcel_count THEN 'UNDER'
                    ELSE 'OVER'
                END AS error_direction
            FROM shippit s
            LEFT JOIN order_rollup r
              ON r.invoice_number = s.invoice_number
            ORDER BY parcel_count_error DESC, s.actual_parcel_count DESC, s.invoice_number
            LIMIT 50
            """
        ).collect()

        for row in mismatch_rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
