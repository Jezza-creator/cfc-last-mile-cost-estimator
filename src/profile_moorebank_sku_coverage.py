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
            joined AS (
                SELECT
                    p.invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    p.PACK_TIMESTAMP,
                    s.HEIGHT,
                    s.WIDTH,
                    s.LENGTH,
                    s.CUBAGE,
                    s.WEIGHT,
                    s.IS_OVERSIZE_SYDM01,
                    s.IS_OVERSIZE_TRUG
                FROM packed p
                LEFT JOIN sku_latest s
                  ON s.SKU_ID = p.SKU_ID
            )
            SELECT
                COUNT(*) AS packed_rows,
                COUNT(DISTINCT invoice_number) AS order_count,
                COUNT(DISTINCT SKU_ID) AS distinct_skus,
                COUNT_IF(HEIGHT IS NOT NULL AND WIDTH IS NOT NULL AND LENGTH IS NOT NULL AND WEIGHT IS NOT NULL) AS rows_with_full_physical_profile,
                COUNT_IF(CUBAGE IS NOT NULL) AS rows_with_cubage,
                COUNT_IF(WEIGHT IS NOT NULL) AS rows_with_weight,
                COUNT_IF(HEIGHT IS NOT NULL) AS rows_with_height,
                COUNT_IF(WIDTH IS NOT NULL) AS rows_with_width,
                COUNT_IF(LENGTH IS NOT NULL) AS rows_with_length,
                COUNT_IF(IS_OVERSIZE_SYDM01 = TRUE) AS oversize_sydm01_rows,
                COUNT_IF(IS_OVERSIZE_TRUG = TRUE) AS oversize_trug_rows,
                COUNT_IF(HEIGHT IS NULL OR WIDTH IS NULL OR LENGTH IS NULL OR WEIGHT IS NULL) AS rows_with_missing_physical_fields
            FROM joined
            """
        ).collect()

        print("SKU COVERAGE SUMMARY")
        for row in rows:
            print(row)

        print("\nTOP SKUS BY PACKED ROWS")
        top_rows = session.sql(
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
            )
            SELECT
                p.SKU_ID,
                COUNT(*) AS packed_rows,
                SUM(p.QUANTITY_PACKED) AS total_qty_packed,
                ROUND(MAX(s.HEIGHT), 2) AS height_cm,
                ROUND(MAX(s.WIDTH), 2) AS width_cm,
                ROUND(MAX(s.LENGTH), 2) AS length_cm,
                ROUND(MAX(s.CUBAGE), 4) AS cubage,
                ROUND(MAX(s.WEIGHT), 2) AS weight_kg,
                MAX(s.IS_OVERSIZE_SYDM01) AS oversize_sydm01,
                MAX(s.IS_OVERSIZE_TRUG) AS oversize_trug
            FROM packed p
            LEFT JOIN sku_latest s
              ON s.SKU_ID = p.SKU_ID
            GROUP BY 1
            ORDER BY packed_rows DESC, total_qty_packed DESC, p.SKU_ID
            LIMIT 25
            """
        ).collect()
        for row in top_rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
