from src.db import get_session


BUCKET_SECONDS = 120


def main() -> None:
    session = get_session()
    try:
        summary_rows = session.sql(
            f"""
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
                WHERE STORE_ATTRIBUTE__STORE_NAME = '1194 Moorebank CFC NSW AU'
                  AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN DATEADD(day, -6, CURRENT_DATE()) AND CURRENT_DATE()
                  AND ORDER_PARCEL__PARCEL_COUNT IS NOT NULL
            ),
            packed AS (
                SELECT
                    REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    p.PACK_TIMESTAMP
                FROM KSFDA.CATCH_CFC.PACKED p
                JOIN shippit s
                  ON s.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
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
            packed_physical AS (
                SELECT
                    p.invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    p.PACK_TIMESTAMP,
                    DATEADD(
                        second,
                        FLOOR(DATEDIFF(second, '1970-01-01'::timestamp_ntz, p.PACK_TIMESTAMP) / {BUCKET_SECONDS}) * {BUCKET_SECONDS},
                        '1970-01-01'::timestamp_ntz
                    ) AS pack_bucket_ts,
                    s.WEIGHT AS sku_weight_kg,
                    s.CUBAGE AS sku_cubage,
                    s.IS_OVERSIZE_SYDM01,
                    s.IS_OVERSIZE_TRUG
                FROM packed p
                LEFT JOIN sku_latest s
                  ON s.SKU_ID = p.SKU_ID
            ),
            bucketed AS (
                SELECT
                    invoice_number,
                    pack_bucket_ts,
                    COUNT(*) AS packed_rows,
                    COUNT(DISTINCT SKU_ID) AS distinct_skus,
                    SUM(QUANTITY_PACKED) AS qty_packed,
                    SUM(COALESCE(sku_weight_kg, 0) * QUANTITY_PACKED) AS total_sku_weight_kg,
                    SUM(COALESCE(sku_cubage, 0) * QUANTITY_PACKED) AS total_sku_cubage,
                    COUNT_IF(IS_OVERSIZE_SYDM01 = TRUE) AS oversize_sydm01_rows,
                    COUNT_IF(IS_OVERSIZE_TRUG = TRUE) AS oversize_trug_rows,
                    COUNT_IF(sku_weight_kg IS NULL OR sku_cubage IS NULL) AS missing_physical_rows
                FROM packed_physical
                GROUP BY 1, 2
            ),
            inferred AS (
                SELECT
                    invoice_number,
                    COUNT(*) AS inferred_parcel_count,
                    SUM(packed_rows) AS packed_rows,
                    SUM(distinct_skus) AS total_distinct_skus,
                    SUM(qty_packed) AS total_qty_packed,
                    SUM(total_sku_weight_kg) AS total_sku_weight_kg,
                    SUM(total_sku_cubage) AS total_sku_cubage,
                    SUM(oversize_sydm01_rows) AS oversize_sydm01_rows,
                    SUM(oversize_trug_rows) AS oversize_trug_rows,
                    SUM(missing_physical_rows) AS missing_physical_rows,
                    SUM(IFF(total_sku_weight_kg > 22, 1, 0)) AS over_22kg_bucket_count,
                    SUM(IFF(total_sku_cubage > 0, 1, 0)) AS nonzero_cube_bucket_count
                FROM bucketed
                GROUP BY 1
            )
            SELECT
                COUNT(*) AS order_count,
                ROUND(AVG(s.actual_parcel_count), 2) AS avg_actual_parcel_count,
                ROUND(AVG(COALESCE(i.inferred_parcel_count, 0)), 2) AS avg_inferred_parcel_count,
                ROUND(AVG(ABS(COALESCE(i.inferred_parcel_count, 0) - s.actual_parcel_count)), 2) AS mean_abs_error,
                ROUND(AVG(IFF(COALESCE(i.inferred_parcel_count, 0) = s.actual_parcel_count, 1, 0)) * 100, 2) AS exact_match_pct,
                ROUND(AVG(IFF(COALESCE(i.inferred_parcel_count, 0) < s.actual_parcel_count, 1, 0)) * 100, 2) AS undercount_pct,
                ROUND(AVG(IFF(COALESCE(i.inferred_parcel_count, 0) > s.actual_parcel_count, 1, 0)) * 100, 2) AS overcount_pct,
                ROUND(AVG(COALESCE(i.missing_physical_rows, 0)), 2) AS avg_missing_physical_rows,
                ROUND(AVG(COALESCE(i.oversize_sydm01_rows, 0)), 2) AS avg_oversize_sydm01_rows
            FROM shippit s
            LEFT JOIN inferred i
              ON i.invoice_number = s.invoice_number
            """
        ).collect()

        print("SUMMARY")
        for row in summary_rows:
            print(row)

        print("\nTOP MISMATCHES")
        mismatch_rows = session.sql(
            f"""
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
                WHERE STORE_ATTRIBUTE__STORE_NAME = '1194 Moorebank CFC NSW AU'
                  AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN DATEADD(day, -6, CURRENT_DATE()) AND CURRENT_DATE()
                  AND ORDER_PARCEL__PARCEL_COUNT IS NOT NULL
            ),
            packed AS (
                SELECT
                    REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    p.PACK_TIMESTAMP
                FROM KSFDA.CATCH_CFC.PACKED p
                JOIN shippit s
                  ON s.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
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
            packed_physical AS (
                SELECT
                    p.invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    p.PACK_TIMESTAMP,
                    DATEADD(
                        second,
                        FLOOR(DATEDIFF(second, '1970-01-01'::timestamp_ntz, p.PACK_TIMESTAMP) / {BUCKET_SECONDS}) * {BUCKET_SECONDS},
                        '1970-01-01'::timestamp_ntz
                    ) AS pack_bucket_ts,
                    s.WEIGHT AS sku_weight_kg,
                    s.CUBAGE AS sku_cubage,
                    s.IS_OVERSIZE_SYDM01,
                    s.IS_OVERSIZE_TRUG
                FROM packed p
                LEFT JOIN sku_latest s
                  ON s.SKU_ID = p.SKU_ID
            ),
            bucketed AS (
                SELECT
                    invoice_number,
                    pack_bucket_ts,
                    COUNT(*) AS packed_rows,
                    COUNT(DISTINCT SKU_ID) AS distinct_skus,
                    SUM(QUANTITY_PACKED) AS qty_packed,
                    SUM(COALESCE(sku_weight_kg, 0) * QUANTITY_PACKED) AS total_sku_weight_kg,
                    SUM(COALESCE(sku_cubage, 0) * QUANTITY_PACKED) AS total_sku_cubage,
                    COUNT_IF(IS_OVERSIZE_SYDM01 = TRUE) AS oversize_sydm01_rows,
                    COUNT_IF(IS_OVERSIZE_TRUG = TRUE) AS oversize_trug_rows,
                    COUNT_IF(sku_weight_kg IS NULL OR sku_cubage IS NULL) AS missing_physical_rows
                FROM packed_physical
                GROUP BY 1, 2
            ),
            inferred AS (
                SELECT
                    invoice_number,
                    COUNT(*) AS inferred_parcel_count,
                    SUM(packed_rows) AS packed_rows,
                    SUM(distinct_skus) AS total_distinct_skus,
                    SUM(qty_packed) AS total_qty_packed,
                    SUM(total_sku_weight_kg) AS total_sku_weight_kg,
                    SUM(total_sku_cubage) AS total_sku_cubage,
                    SUM(oversize_sydm01_rows) AS oversize_sydm01_rows,
                    SUM(oversize_trug_rows) AS oversize_trug_rows,
                    SUM(missing_physical_rows) AS missing_physical_rows
                FROM bucketed
                GROUP BY 1
            )
            SELECT
                s.invoice_number,
                s.actual_carrier,
                s.actual_parcel_count,
                COALESCE(i.inferred_parcel_count, 0) AS inferred_parcel_count,
                COALESCE(i.packed_rows, 0) AS packed_rows,
                COALESCE(i.total_distinct_skus, 0) AS total_distinct_skus,
                COALESCE(i.total_qty_packed, 0) AS total_qty_packed,
                COALESCE(i.total_sku_weight_kg, 0) AS total_sku_weight_kg,
                COALESCE(i.total_sku_cubage, 0) AS total_sku_cubage,
                COALESCE(i.oversize_sydm01_rows, 0) AS oversize_sydm01_rows,
                COALESCE(i.missing_physical_rows, 0) AS missing_physical_rows,
                ABS(COALESCE(i.inferred_parcel_count, 0) - s.actual_parcel_count) AS parcel_count_error,
                CASE
                    WHEN COALESCE(i.inferred_parcel_count, 0) = s.actual_parcel_count THEN 'EXACT'
                    WHEN COALESCE(i.inferred_parcel_count, 0) < s.actual_parcel_count THEN 'UNDER'
                    ELSE 'OVER'
                END AS error_direction
            FROM shippit s
            LEFT JOIN inferred i
              ON i.invoice_number = s.invoice_number
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
