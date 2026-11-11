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
        print("TRUGANINA CARRIER MIX")
        carrier_rows = session.sql(
            f"""
            {BASE_CTE}
            SELECT
                actual_carrier,
                COUNT(*) AS order_count,
                ROUND(AVG(actual_parcel_count), 2) AS avg_parcel_count,
                ROUND(AVG(chargeable_kg), 2) AS avg_chargeable_kg
            FROM shippit
            GROUP BY 1
            ORDER BY order_count DESC, actual_carrier
            """
        ).collect()
        for row in carrier_rows:
            print(row)

        print("\nTRUGANINA PARCEL COUNT MIX")
        parcel_mix_rows = session.sql(
            f"""
            {BASE_CTE}
            SELECT
                actual_parcel_count,
                COUNT(*) AS order_count,
                ROUND(AVG(chargeable_kg), 2) AS avg_chargeable_kg
            FROM shippit
            GROUP BY 1
            ORDER BY order_count DESC, actual_parcel_count
            """
        ).collect()
        for row in parcel_mix_rows:
            print(row)

        print("\nTRUGANINA STORAGE TYPE BY CARRIER")
        storage_rows = session.sql(
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
                    ,
                    CASE
                        WHEN b.ZONE IN ('4','6','7','8','F','H','U','V','B','L','A','P','M','G','S') THEN 'T2'
                        WHEN b.ZONE IN ('3','C','N','J','Q','2','5') THEN 'T3'
                        ELSE 'Unknown'
                    END AS bldg
                    ,
                    CASE
                        WHEN b.ZONE IN ('4','6','7','8','F','H','U','V','3','2','5') THEN 'Pickface'
                        WHEN b.ZONE IN ('A','C','N','J','M','G','S') THEN 'Reserve'
                        ELSE 'In Process'
                    END AS pf_res
                    ,
                    CASE
                        WHEN b.ZONE IN ('4') THEN 'KNAPP'
                        WHEN b.ZONE IN ('6','7','8','H','U','V','G') THEN 'UNDER MEZZ + CLS'
                        WHEN b.ZONE IN ('F','S') THEN 'UPPER MEZZ'
                        WHEN b.ZONE IN ('B','L','W','Q','PK','P','T','E','0','1','9','D','K','Y') THEN 'OTHER LOCATIONS ON SITE'
                        WHEN b.ZONE IN ('3','2','5') THEN 'T3 SAI PICKFACE'
                        WHEN b.ZONE IN ('C','N','J') THEN 'T3 RES '
                        WHEN b.ZONE IN ('A','M') THEN 'T2 RES '
                        WHEN b.ZONE IS NULL THEN 'No Zone '
                        ELSE 'Unknown'
                    END AS cfc_location
                FROM inventory_latest i
                LEFT JOIN bin_latest b
                  ON i.BIN_LOCATION = b.BIN_LOCATION
            ),
            classified AS (
                SELECT
                    s.actual_carrier,
                    p.invoice_number,
                    {_size_category_sql('sku')} AS size_category_trug,
                    COALESCE(w.storage_type, 'No inventory mapping') AS storage_type,
                    COALESCE(w.bldg, 'Unknown') AS bldg,
                    COALESCE(w.pf_res, 'In Process') AS pf_res,
                    COALESCE(w.cfc_location, 'Unknown') AS cfc_location
                FROM shippit s
                JOIN packed p
                  ON p.invoice_number = s.invoice_number
                LEFT JOIN sku_latest sku
                  ON sku.SKU_ID = p.SKU_ID
                LEFT JOIN cfc_wms_inventory w
                  ON w.SKU_ID = p.SKU_ID
            )
            SELECT
                actual_carrier,
                bldg,
                pf_res,
                cfc_location,
                storage_type,
                size_category_trug,
                COUNT(*) AS packed_rows,
                COUNT(DISTINCT invoice_number) AS order_count
            FROM classified
            GROUP BY 1, 2, 3, 4, 5, 6
            ORDER BY packed_rows DESC, actual_carrier, bldg, pf_res, cfc_location, storage_type, size_category_trug
            """
        ).collect()
        for row in storage_rows:
            print(row)

        print("\nTRUGANINA NO INVENTORY MAPPING DEEP DIVE")
        no_map_rows = session.sql(
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
                    ,
                    CASE
                        WHEN b.ZONE IN ('4','6','7','8','F','H','U','V','B','L','A','P','M','G','S') THEN 'T2'
                        WHEN b.ZONE IN ('3','C','N','J','Q','2','5') THEN 'T3'
                        ELSE 'Unknown'
                    END AS bldg
                    ,
                    CASE
                        WHEN b.ZONE IN ('4','6','7','8','F','H','U','V','3','2','5') THEN 'Pickface'
                        WHEN b.ZONE IN ('A','C','N','J','M','G','S') THEN 'Reserve'
                        ELSE 'In Process'
                    END AS pf_res
                    ,
                    CASE
                        WHEN b.ZONE IN ('4') THEN 'KNAPP'
                        WHEN b.ZONE IN ('6','7','8','H','U','V','G') THEN 'UNDER MEZZ + CLS'
                        WHEN b.ZONE IN ('F','S') THEN 'UPPER MEZZ'
                        WHEN b.ZONE IN ('B','L','W','Q','PK','P','T','E','0','1','9','D','K','Y') THEN 'OTHER LOCATIONS ON SITE'
                        WHEN b.ZONE IN ('3','2','5') THEN 'T3 SAI PICKFACE'
                        WHEN b.ZONE IN ('C','N','J') THEN 'T3 RES '
                        WHEN b.ZONE IN ('A','M') THEN 'T2 RES '
                        WHEN b.ZONE IS NULL THEN 'No Zone '
                        ELSE 'Unknown'
                    END AS cfc_location
                FROM inventory_latest i
                LEFT JOIN bin_latest b
                  ON i.BIN_LOCATION = b.BIN_LOCATION
            ),
            classified AS (
                SELECT
                    s.actual_carrier,
                    p.invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    sku.HEIGHT,
                    sku.WIDTH,
                    sku.LENGTH,
                    sku.WEIGHT,
                    {_size_category_sql('sku')} AS size_category_trug,
                    COALESCE(w.storage_type, 'No inventory mapping') AS storage_type,
                    COALESCE(w.bldg, 'Unknown') AS bldg,
                    COALESCE(w.pf_res, 'In Process') AS pf_res,
                    COALESCE(w.cfc_location, 'Unknown') AS cfc_location
                FROM shippit s
                JOIN packed p
                  ON p.invoice_number = s.invoice_number
                LEFT JOIN sku_latest sku
                  ON sku.SKU_ID = p.SKU_ID
                LEFT JOIN cfc_wms_inventory w
                  ON w.SKU_ID = p.SKU_ID
            )
            SELECT
                actual_carrier,
                bldg,
                pf_res,
                cfc_location,
                size_category_trug,
                SKU_ID,
                COUNT(*) AS packed_rows,
                SUM(QUANTITY_PACKED) AS qty_packed,
                ROUND(AVG(HEIGHT), 2) AS avg_height,
                ROUND(AVG(WIDTH), 2) AS avg_width,
                ROUND(AVG(LENGTH), 2) AS avg_length,
                ROUND(AVG(WEIGHT), 2) AS avg_weight
            FROM classified
            WHERE storage_type = 'No inventory mapping'
            GROUP BY 1, 2, 3, 4, 5, 6
            ORDER BY packed_rows DESC, actual_carrier, bldg, pf_res, cfc_location, SKU_ID
            LIMIT 100
            """
        ).collect()
        for row in no_map_rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
