from src.db import get_session


def main() -> None:
    session = get_session()
    try:
        rows = session.sql(
            """
            WITH moorebank_orders AS (
                SELECT DISTINCT
                    ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number
                FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
                WHERE STORE_ATTRIBUTE__STORE_NAME = '1194 Moorebank CFC NSW AU'
                  AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN DATEADD(day, -6, CURRENT_DATE()) AND CURRENT_DATE()
            ),
            packed AS (
                SELECT
                    p.ORDER_ID,
                    REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    p.PACK_TIMESTAMP
                FROM KSFDA.CATCH_CFC.PACKED p
                JOIN moorebank_orders o
                  ON o.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
            ),
            packed_rollup AS (
                SELECT
                    invoice_number,
                    PACK_TIMESTAMP,
                    COUNT(*) AS packed_rows,
                    COUNT(DISTINCT SKU_ID) AS distinct_skus,
                    SUM(QUANTITY_PACKED) AS qty_packed
                FROM packed
                GROUP BY 1, 2
            )
            SELECT
                COUNT(*) AS packed_rows,
                COUNT(DISTINCT invoice_number) AS order_count,
                COUNT(DISTINCT invoice_number || '|' || TO_VARCHAR(PACK_TIMESTAMP)) AS order_timestamp_groups,
                ROUND(AVG(packed_rows), 2) AS avg_rows_per_order_timestamp_group,
                ROUND(AVG(distinct_skus), 2) AS avg_distinct_skus_per_group,
                ROUND(AVG(qty_packed), 2) AS avg_qty_packed_per_group,
                COUNT_IF(packed_rows = 1) AS singleton_groups,
                COUNT_IF(packed_rows > 1) AS multi_row_groups
            FROM packed_rollup
            """
        ).collect()

        print("PACKED CLUSTER SUMMARY")
        for row in rows:
            print(row)

        print("\nTOP PACKING CLUSTERS")
        top_rows = session.sql(
            """
            WITH moorebank_orders AS (
                SELECT DISTINCT
                    ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number
                FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
                WHERE STORE_ATTRIBUTE__STORE_NAME = '1194 Moorebank CFC NSW AU'
                  AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN DATEADD(day, -6, CURRENT_DATE()) AND CURRENT_DATE()
            ),
            packed AS (
                SELECT
                    p.ORDER_ID,
                    REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    p.PACK_TIMESTAMP
                FROM KSFDA.CATCH_CFC.PACKED p
                JOIN moorebank_orders o
                  ON o.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
            )
            SELECT
                invoice_number,
                PACK_TIMESTAMP,
                COUNT(*) AS packed_rows,
                COUNT(DISTINCT SKU_ID) AS distinct_skus,
                SUM(QUANTITY_PACKED) AS qty_packed
            FROM packed
            GROUP BY 1, 2
            QUALIFY ROW_NUMBER() OVER (
                ORDER BY COUNT(*) DESC, SUM(QUANTITY_PACKED) DESC, invoice_number
            ) <= 25
            """
        ).collect()
        for row in top_rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
