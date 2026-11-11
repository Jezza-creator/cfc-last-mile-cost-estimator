from src.db import get_session


def main() -> None:
    session = get_session()
    try:
        rows = session.sql(
            """
            WITH shippit AS (
                SELECT DISTINCT
                    ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number
                FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
                WHERE STORE_ATTRIBUTE__STORE_NAME = '1194 Moorebank CFC NSW AU'
                  AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN DATEADD(day, -6, CURRENT_DATE()) AND CURRENT_DATE()
            ),
            packed AS (
                SELECT
                    REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED
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
            classified AS (
                SELECT
                    p.invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    s.HEIGHT,
                    s.WIDTH,
                    s.LENGTH,
                    s.WEIGHT,
                    s.IS_OVERSIZE_SYDM01,
                    s.IS_OVERSIZE_TRUG,
                    LEAST(s.HEIGHT, s.WIDTH, s.LENGTH) AS shortest,
                    GREATEST(s.HEIGHT, s.WIDTH, s.LENGTH) AS longest,
                    (s.HEIGHT + s.WIDTH + s.LENGTH
                        - LEAST(s.HEIGHT, s.WIDTH, s.LENGTH)
                        - GREATEST(s.HEIGHT, s.WIDTH, s.LENGTH)) AS middle,
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
                    END AS size_category_sydm01
                FROM packed p
                LEFT JOIN sku_latest s
                  ON s.SKU_ID = p.SKU_ID
            )
            SELECT
                size_category_sydm01,
                COUNT(*) AS packed_rows,
                COUNT(DISTINCT invoice_number) AS order_count,
                COUNT(DISTINCT SKU_ID) AS distinct_skus,
                SUM(QUANTITY_PACKED) AS qty_packed
            FROM classified
            GROUP BY 1
            ORDER BY packed_rows DESC, size_category_sydm01
            """
        ).collect()

        print("MOOREBANK SIZE CATEGORY SUMMARY")
        for row in rows:
            print(row)

        print("\nORDER MIX SUMMARY")
        mix_rows = session.sql(
            """
            WITH shippit AS (
                SELECT DISTINCT
                    ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number
                FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
                WHERE STORE_ATTRIBUTE__STORE_NAME = '1194 Moorebank CFC NSW AU'
                  AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN DATEADD(day, -6, CURRENT_DATE()) AND CURRENT_DATE()
            ),
            packed AS (
                SELECT
                    REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED
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
            classified AS (
                SELECT
                    p.invoice_number,
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
                    END AS size_category_sydm01
                FROM packed p
                LEFT JOIN sku_latest s
                  ON s.SKU_ID = p.SKU_ID
            )
            SELECT
                invoice_number,
                COUNT(*) AS packed_rows,
                COUNT_IF(size_category_sydm01 = 'Toteable') AS toteable_rows,
                COUNT_IF(size_category_sydm01 = 'Non-Toteable') AS non_toteable_rows,
                COUNT_IF(size_category_sydm01 = 'Bulky') AS bulky_rows,
                COUNT_IF(size_category_sydm01 = 'No dims') AS no_dims_rows,
                COUNT_IF(size_category_sydm01 = 'Unknown') AS unknown_rows,
                CASE
                    WHEN COUNT_IF(size_category_sydm01 = 'Bulky') > 0 THEN 'Bulky present'
                    WHEN COUNT_IF(size_category_sydm01 = 'Non-Toteable') > 0
                         AND COUNT_IF(size_category_sydm01 = 'Toteable') > 0 THEN 'Mixed'
                    WHEN COUNT_IF(size_category_sydm01 = 'Non-Toteable') > 0 THEN 'Non-Toteable only'
                    WHEN COUNT_IF(size_category_sydm01 = 'Toteable') > 0 THEN 'Toteable only'
                    ELSE 'Other'
                END AS order_mix
            FROM classified
            GROUP BY 1
            ORDER BY packed_rows DESC, invoice_number
            LIMIT 50
            """
        ).collect()

        for row in mix_rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
