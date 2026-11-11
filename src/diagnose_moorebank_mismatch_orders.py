from __future__ import annotations

from collections import defaultdict

from src.db import get_session


STORE_NAME = "1194 Moorebank CFC NSW AU"
DAYS_BACK = 6
SAMPLE_ORDER_LIMIT = 50
TOP_ORDER_LIMIT = 2


BASE_CTE = f"""
WITH shippit AS (
    SELECT
        ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number,
        ORDER_PARCEL__PARCEL_COUNT AS actual_parcel_count,
        CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME AS actual_carrier
    FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
    WHERE STORE_ATTRIBUTE__STORE_NAME = '{STORE_NAME}'
      AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
            BETWEEN DATEADD(day, -{DAYS_BACK}, CURRENT_DATE()) AND CURRENT_DATE()
      AND ORDER_PARCEL__PARCEL_COUNT IS NOT NULL
    ORDER BY ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER
    LIMIT {SAMPLE_ORDER_LIMIT}
),
packed AS (
    SELECT
        REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
        s.actual_parcel_count,
        s.actual_carrier,
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
      AND i.EXTRACT_TIMESTAMP::DATE BETWEEN DATEADD(day, -7, CURRENT_DATE()) AND CURRENT_DATE()
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
        p.ORDER_ID,
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


def main() -> None:
    session = get_session()
    try:
        mismatch_rows = session.sql(
            f"""
            {BASE_CTE}
            SELECT
                invoice_number,
                actual_parcel_count,
                actual_carrier,
                COUNT(*) AS packed_rows,
                SUM(IFF(size_category_sydm01 = 'Toteable', 1, 0)) AS toteable_rows,
                SUM(IFF(size_category_sydm01 = 'Non-Toteable', 1, 0)) AS non_toteable_rows,
                SUM(IFF(size_category_sydm01 = 'Bulky', 1, 0)) AS bulky_rows,
                SUM(IFF(storage_type = 'AMR', 1, 0)) AS amr_rows,
                SUM(IFF(storage_type = 'PFR', 1, 0)) AS pfr_rows,
                SUM(IFF(storage_type = 'No inventory mapping', 1, 0)) AS no_inventory_rows,
                ABS(
                    (
                        COUNT(DISTINCT CASE WHEN size_category_sydm01 = 'Toteable' THEN PACK_TIMESTAMP END)
                        + SUM(IFF(size_category_sydm01 IN ('Non-Toteable', 'Bulky'), 1, 0))
                    ) - actual_parcel_count
                ) AS parcel_count_error,
                (
                    COUNT(DISTINCT CASE WHEN size_category_sydm01 = 'Toteable' THEN PACK_TIMESTAMP END)
                    + SUM(IFF(size_category_sydm01 IN ('Non-Toteable', 'Bulky'), 1, 0))
                ) AS inferred_parcel_count
            FROM row_level
            GROUP BY 1, 2, 3
            ORDER BY parcel_count_error DESC, actual_parcel_count DESC, invoice_number
            LIMIT {TOP_ORDER_LIMIT}
            """
        ).collect()

        print("TOP MISMATCH ORDERS")
        for row in mismatch_rows:
            print(row)

        invoices = [row["INVOICE_NUMBER"] for row in mismatch_rows]
        if not invoices:
            return

        detail_rows = session.sql(
            f"""
            {BASE_CTE}
            SELECT
                invoice_number,
                actual_parcel_count,
                actual_carrier,
                ORDER_ID,
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
            WHERE invoice_number IN ({",".join([f"'{invoice}'" for invoice in invoices])})
            ORDER BY invoice_number, PACK_TIMESTAMP, SKU_ID
            """
        ).collect()

        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in detail_rows:
            grouped[row["INVOICE_NUMBER"]].append(dict(row.asDict()))

        for invoice_number in invoices:
            print(f"\nORDER {invoice_number}")
            for row in grouped.get(invoice_number, []):
                print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
