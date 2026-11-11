from src.db import get_session


BUCKET_SECONDS = 120


def main() -> None:
    session = get_session()
    try:
        rows = session.sql(
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
                    p.ORDER_ID,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    p.PACK_TIMESTAMP
                FROM KSFDA.CATCH_CFC.PACKED p
                JOIN shippit s
                  ON s.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
            ),
            bucketed AS (
                SELECT
                    invoice_number,
                    DATEADD(
                        second,
                        FLOOR(DATEDIFF(second, '1970-01-01'::timestamp_ntz, PACK_TIMESTAMP) / {BUCKET_SECONDS}) * {BUCKET_SECONDS},
                        '1970-01-01'::timestamp_ntz
                    ) AS pack_bucket_ts,
                    COUNT(*) AS packed_rows,
                    COUNT(DISTINCT SKU_ID) AS distinct_skus,
                    SUM(QUANTITY_PACKED) AS qty_packed,
                    MIN(PACK_TIMESTAMP) AS first_pack_ts,
                    MAX(PACK_TIMESTAMP) AS last_pack_ts
                FROM packed
                GROUP BY 1, 2
            ),
            inferred AS (
                SELECT
                    invoice_number,
                    COUNT(*) AS inferred_parcel_count,
                    SUM(packed_rows) AS packed_rows,
                    SUM(distinct_skus) AS total_distinct_skus,
                    SUM(qty_packed) AS total_qty_packed,
                    MIN(first_pack_ts) AS first_pack_ts,
                    MAX(last_pack_ts) AS last_pack_ts
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
                i.first_pack_ts,
                i.last_pack_ts,
                s.chargeable_kg,
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
            LIMIT 100
            """
        ).collect()

        print("TOP RECONSTRUCTION MISMATCHES")
        for row in rows:
            print(row)

        summary = session.sql(
            f"""
            WITH shippit AS (
                SELECT
                    ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number,
                    ORDER_PARCEL__PARCEL_COUNT AS actual_parcel_count
                FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
                WHERE STORE_ATTRIBUTE__STORE_NAME = '1194 Moorebank CFC NSW AU'
                  AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN DATEADD(day, -6, CURRENT_DATE()) AND CURRENT_DATE()
                  AND ORDER_PARCEL__PARCEL_COUNT IS NOT NULL
            ),
            packed AS (
                SELECT
                    REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
                    p.PACK_TIMESTAMP
                FROM KSFDA.CATCH_CFC.PACKED p
                JOIN shippit s
                  ON s.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
            ),
            bucketed AS (
                SELECT
                    invoice_number,
                    DATEADD(
                        second,
                        FLOOR(DATEDIFF(second, '1970-01-01'::timestamp_ntz, PACK_TIMESTAMP) / {BUCKET_SECONDS}) * {BUCKET_SECONDS},
                        '1970-01-01'::timestamp_ntz
                    ) AS pack_bucket_ts
                FROM packed
                GROUP BY 1, 2
            ),
            inferred AS (
                SELECT
                    invoice_number,
                    COUNT(*) AS inferred_parcel_count
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
                ROUND(AVG(IFF(COALESCE(i.inferred_parcel_count, 0) > s.actual_parcel_count, 1, 0)) * 100, 2) AS overcount_pct
            FROM shippit s
            LEFT JOIN inferred i
              ON i.invoice_number = s.invoice_number
            """
        ).collect()

        print("\nSUMMARY")
        for row in summary:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
