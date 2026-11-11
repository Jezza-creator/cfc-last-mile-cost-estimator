from src.db import get_session


STORE_NAME = "1190 Truganina CFC VIC AU"
DAYS_BACK = 6
WAREHOUSE_CODE = "TRUG"
STR_ID = "1190"


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
inventory_latest AS (
    SELECT *
    FROM (
        SELECT
            i.*,
            ROW_NUMBER() OVER (
                PARTITION BY i.WAREHOUSE_CODE, i.SKU_ID, i.SELLER_CODE, i.BIN_LOCATION
                ORDER BY i.EXTRACT_TIMESTAMP DESC
            ) AS rn
        FROM KSFDA.CATCH_CFC.INVENTORY i
        WHERE i.WAREHOUSE_CODE = '{WAREHOUSE_CODE}'
    )
    WHERE rn = 1
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
)
"""


def _size_category_sql(alias: str = "s") -> str:
    return f"""
    CASE
        WHEN LEAST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH) <= 50
             AND ({alias}.HEIGHT + {alias}.WIDTH + {alias}.LENGTH
                  - LEAST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH)
                  - GREATEST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH)) <= 55
             AND GREATEST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH) <= 55
             AND {alias}.WEIGHT <= 15
            THEN 'Toteable'
        WHEN LEAST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH) <= 105
             AND ({alias}.HEIGHT + {alias}.WIDTH + {alias}.LENGTH
                  - LEAST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH)
                  - GREATEST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH)) <= 105
             AND GREATEST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH) <= 105
             AND {alias}.WEIGHT <= 45
            THEN 'Non-Toteable'
        WHEN LEAST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH) <= 200
             AND ({alias}.HEIGHT + {alias}.WIDTH + {alias}.LENGTH
                  - LEAST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH)
                  - GREATEST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH)) <= 200
             AND GREATEST({alias}.HEIGHT, {alias}.WIDTH, {alias}.LENGTH) <= 200
             AND {alias}.WEIGHT <= 80
            THEN 'Bulky'
        WHEN {alias}.HEIGHT IS NULL OR {alias}.WIDTH IS NULL OR {alias}.LENGTH IS NULL
            THEN 'No dims'
        ELSE 'Unknown'
    END
    """


def main() -> None:
    session = get_session()
    try:
        print("TRUGANINA STORAGE TYPE SUMMARY")
        summary_rows = session.sql(
            f"""
            {BASE_CTE},
            cfc_wms_inventory AS (
                SELECT
                    i.WAREHOUSE_CODE,
                    CASE
                        WHEN i.WAREHOUSE_CODE = 'TRUG' THEN '1190'
                        ELSE 'Unknown'
                    END AS STR_ID,
                    i.SKU_ID,
                    i.SELLER_CODE,
                    NVL(b.ZONE, 'New Bin - Zone Unknown') AS ZONE_CODE,
                    CASE
                        WHEN NVL(b.ZONE, 'New Bin - Zone Unknown') IN ('4','6','7','8','F','H','U','V','A','3','C','N','J','2','5','M','G')
                             AND i.WAREHOUSE_CODE = 'TRUG' THEN 'Storage'
                        ELSE 'In Process'
                    END AS zone_type,
                    NVL(z.ZONE_DESCRIPTION, 'New Bin - Zone Unknown') AS zone_description,
                    CASE
                        WHEN b.BIN_LOCATION LIKE 'L%' THEN 'AMR'
                        WHEN b.BIN_LOCATION BETWEEN '37%' AND '42%' THEN 'PFR'
                        WHEN b.BIN_LOCATION BETWEEN '01%' AND '05%' THEN 'Consumables/AMR Reserve Racking'
                        WHEN b.BIN_LOCATION BETWEEN '21%' AND '28%' THEN 'Hanger Racks/AMR Reserve Shelving'
                        WHEN NVL(z.ZONE_DESCRIPTION, 'New Bin - Zone Unknown') IN ('Rack Pickface', 'Rack Reserve') THEN 'Racking'
                        WHEN NVL(b.ZONE, 'New Bin - Zone Unknown') IN ('4') THEN 'KNAPP'
                        WHEN NVL(b.ZONE, 'New Bin - Zone Unknown') IN ('6','7','8','H','U','V','C') THEN 'Shelving with Ground Pallet'
                        WHEN NVL(b.ZONE, 'New Bin - Zone Unknown') IN ('F','3','2','5') THEN 'Ground Pallet'
                        WHEN NVL(b.ZONE, 'New Bin - Zone Unknown') IN ('A','J','M') THEN 'VNA Shelving / Pallet'
                        WHEN NVL(b.ZONE, 'New Bin - Zone Unknown') IN ('N') THEN 'Pallet Racking'
                        WHEN NVL(b.ZONE, 'New Bin - Zone Unknown') IN ('G','S') THEN 'Tote Tower'
                        ELSE 'In Process'
                    END AS storage_type
                FROM inventory_latest i
                LEFT JOIN bin_latest b
                  ON i.BIN_LOCATION = b.BIN_LOCATION
                LEFT JOIN zone_lookup z
                  ON CASE
                        WHEN b.ZONE IS NULL
                             AND i.BIN_LOCATION LIKE 'L%'
                             AND i.WAREHOUSE_CODE = 'TRUG'
                            THEN 'AMR'
                        ELSE b.ZONE
                     END = z.ZONE
            ),
            order_items AS (
                SELECT
                    p.invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    s.HEIGHT,
                    s.WIDTH,
                    s.LENGTH,
                    s.WEIGHT,
                    {_size_category_sql("s")} AS size_category_trug,
                    COALESCE(w.storage_type, 'No inventory mapping') AS storage_type
                FROM packed p
                LEFT JOIN sku_latest s
                  ON s.SKU_ID = p.SKU_ID
                LEFT JOIN cfc_wms_inventory w
                  ON w.SKU_ID = p.SKU_ID
                 AND w.STR_ID = '{STR_ID}'
            ),
            order_rollup AS (
                SELECT
                    invoice_number,
                    COUNT(*) AS packed_rows,
                    SUM(QUANTITY_PACKED) AS qty_packed,
                    COUNT_IF(size_category_trug = 'Toteable') AS toteable_rows,
                    COUNT_IF(size_category_trug = 'Non-Toteable') AS non_toteable_rows,
                    COUNT_IF(size_category_trug = 'Bulky') AS bulky_rows,
                    COUNT_IF(size_category_trug = 'No dims') AS no_dims_rows,
                    COUNT_IF(storage_type = 'AMR') AS amr_rows,
                    COUNT_IF(storage_type = 'PFR') AS pfr_rows,
                    COUNT_IF(storage_type IN ('Racking', 'Tote Tower', 'Ground Pallet', 'Shelving with Ground Pallet', 'VNA Shelving / Pallet', 'Pallet Racking', 'KNAPP')) AS pallet_style_rows
                FROM order_items
                GROUP BY 1
            )
            SELECT
                COUNT(*) AS order_count,
                ROUND(AVG(s.actual_parcel_count), 2) AS avg_actual_parcel_count,
                ROUND(AVG(COALESCE(r.packed_rows, 0)), 2) AS avg_packed_rows,
                ROUND(AVG(COALESCE(r.qty_packed, 0)), 2) AS avg_qty_packed,
                ROUND(AVG(COALESCE(r.toteable_rows, 0)), 2) AS avg_toteable_rows,
                ROUND(AVG(COALESCE(r.non_toteable_rows, 0)), 2) AS avg_non_toteable_rows,
                ROUND(AVG(COALESCE(r.bulky_rows, 0)), 2) AS avg_bulky_rows,
                ROUND(AVG(COALESCE(r.amr_rows, 0)), 2) AS avg_amr_rows,
                ROUND(AVG(COALESCE(r.pfr_rows, 0)), 2) AS avg_pfr_rows,
                ROUND(AVG(COALESCE(r.pallet_style_rows, 0)), 2) AS avg_pallet_style_rows
            FROM shippit s
            LEFT JOIN order_rollup r
              ON r.invoice_number = s.invoice_number
            """
        ).collect()
        for row in summary_rows:
            print(row)

        print("\nORDER MIX BY STORAGE TYPE")
        mix_rows = session.sql(
            f"""
            {BASE_CTE},
            cfc_wms_inventory AS (
                SELECT
                    i.SKU_ID,
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
                FROM inventory_latest i
                LEFT JOIN bin_latest b
                  ON i.BIN_LOCATION = b.BIN_LOCATION
            ),
            classified AS (
                SELECT
                    p.invoice_number,
                    p.SKU_ID,
                    {_size_category_sql("s")} AS size_category_trug,
                    COALESCE(w.storage_type, 'No inventory mapping') AS storage_type
                FROM packed p
                LEFT JOIN sku_latest s
                  ON s.SKU_ID = p.SKU_ID
                LEFT JOIN cfc_wms_inventory w
                  ON w.SKU_ID = p.SKU_ID
            )
            SELECT
                storage_type,
                size_category_trug,
                COUNT(*) AS packed_rows,
                COUNT(DISTINCT invoice_number) AS order_count
            FROM classified
            GROUP BY 1, 2
            ORDER BY packed_rows DESC, storage_type, size_category_trug
            """
        ).collect()
        for row in mix_rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
