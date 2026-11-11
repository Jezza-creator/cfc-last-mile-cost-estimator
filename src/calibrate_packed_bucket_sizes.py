from src.db import get_session


BUCKET_SECONDS = [0, 30, 60, 120, 300]


def main() -> None:
    session = get_session()
    try:
        values_sql = ",\n                ".join(f"({seconds})" for seconds in BUCKET_SECONDS)
        rows = session.sql(
            f"""
            WITH moorebank_orders AS (
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
                JOIN moorebank_orders o
                  ON o.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
            ),
            bucket_sizes AS (
                SELECT column1::int AS bucket_seconds
                FROM VALUES
                    {values_sql}
            ),
            bucketed AS (
                SELECT
                    b.bucket_seconds,
                    p.invoice_number,
                    COUNT(DISTINCT
                        CASE
                            WHEN b.bucket_seconds = 0 THEN TO_VARCHAR(p.PACK_TIMESTAMP)
                            ELSE TO_VARCHAR(
                                DATEADD(
                                    second,
                                    FLOOR(DATEDIFF(second, '1970-01-01'::timestamp_ntz, p.PACK_TIMESTAMP) / b.bucket_seconds) * b.bucket_seconds,
                                    '1970-01-01'::timestamp_ntz
                                )
                            )
                        END
                    ) AS inferred_parcel_count
                FROM packed p
                CROSS JOIN bucket_sizes b
                GROUP BY 1, 2
            ),
            scored AS (
                SELECT
                    b.bucket_seconds,
                    m.invoice_number,
                    m.actual_parcel_count,
                    COALESCE(bd.inferred_parcel_count, 0) AS inferred_parcel_count,
                    ABS(COALESCE(bd.inferred_parcel_count, 0) - m.actual_parcel_count) AS abs_error,
                    CASE
                        WHEN COALESCE(bd.inferred_parcel_count, 0) = m.actual_parcel_count THEN 1
                        ELSE 0
                    END AS exact_match_flag
                FROM bucket_sizes b
                CROSS JOIN moorebank_orders m
                LEFT JOIN bucketed bd
                  ON bd.bucket_seconds = b.bucket_seconds
                 AND bd.invoice_number = m.invoice_number
            )
            SELECT
                bucket_seconds,
                COUNT(*) AS order_count,
                ROUND(AVG(actual_parcel_count), 2) AS avg_actual_parcel_count,
                ROUND(AVG(inferred_parcel_count), 2) AS avg_inferred_parcel_count,
                ROUND(AVG(abs_error), 2) AS mean_abs_error,
                ROUND(AVG(exact_match_flag) * 100, 2) AS exact_match_pct,
                ROUND(AVG(IFF(inferred_parcel_count < actual_parcel_count, 1, 0)) * 100, 2) AS undercount_pct,
                ROUND(AVG(IFF(inferred_parcel_count > actual_parcel_count, 1, 0)) * 100, 2) AS overcount_pct
            FROM scored
            GROUP BY 1
            ORDER BY bucket_seconds
            """
        ).collect()

        print("BUCKET CALIBRATION")
        for row in rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
