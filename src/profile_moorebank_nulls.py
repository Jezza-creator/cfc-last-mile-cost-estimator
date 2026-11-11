from src.db import get_session


def main() -> None:
    session = get_session()
    try:
        summary = session.sql(
            """
            WITH base AS (
                SELECT
                    ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number,
                    ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE AS despatch_date,
                    CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME AS actual_carrier,
                    ORDER_PARCEL__PARCEL_COUNT AS parcel_count,
                    GREATEST(
                        ORDER_WEIGHT__DEAD_WEIGHT,
                        ORDER_WEIGHT__VOLUMETRIC_WEIGHT
                    ) AS chargeable_kg,
                    ORDER_WEIGHT__DEAD_WEIGHT AS dead_weight_kg,
                    ORDER_CARRIER__QUOTED_SHIPPING_COST_INCL_GST AS quoted_cost_inc_gst
                FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
                WHERE STORE_ATTRIBUTE__STORE_NAME = '1194 Moorebank CFC NSW AU'
                  AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN DATEADD(day, -6, CURRENT_DATE()) AND CURRENT_DATE()
            )
            SELECT
                COUNT(*) AS row_count,
                COUNT_IF(parcel_count = 1) AS single_parcel_count,
                COUNT_IF(parcel_count > 1) AS multi_parcel_count,
                COUNT_IF(parcel_count IS NULL) AS null_parcel_count,
                COUNT_IF(actual_carrier IS NULL) AS null_service_count,
                COUNT_IF(chargeable_kg > 22) AS over_22kg_count,
                COUNT_IF(chargeable_kg > 22 AND parcel_count > 1) AS over_22kg_multi_parcel_count,
                ROUND(AVG(parcel_count), 2) AS avg_parcel_count,
                ROUND(AVG(chargeable_kg), 2) AS avg_chargeable_kg,
                ROUND(AVG(IFF(parcel_count > 1, 1, 0)) * 100, 2) AS multi_parcel_pct,
                ROUND(AVG(IFF(chargeable_kg > 22, 1, 0)) * 100, 2) AS over_22kg_pct
            FROM base
            """
        ).collect()

        print("SUMMARY")
        for row in summary:
            print(row)

        print("\nNULL COST BY CARRIER AND PARCEL COUNT")
        rows = session.sql(
            """
            WITH base AS (
                SELECT
                    ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number,
                    CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME AS actual_carrier,
                    ORDER_PARCEL__PARCEL_COUNT AS parcel_count,
                    GREATEST(
                        ORDER_WEIGHT__DEAD_WEIGHT,
                        ORDER_WEIGHT__VOLUMETRIC_WEIGHT
                    ) AS chargeable_kg,
                    ORDER_CARRIER__QUOTED_SHIPPING_COST_INCL_GST AS quoted_cost_inc_gst
                FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
                WHERE STORE_ATTRIBUTE__STORE_NAME = '1194 Moorebank CFC NSW AU'
                  AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN DATEADD(day, -6, CURRENT_DATE()) AND CURRENT_DATE()
            ),
            scored AS (
                SELECT
                    invoice_number,
                    actual_carrier,
                    parcel_count,
                    chargeable_kg,
                    quoted_cost_inc_gst,
                    CASE
                        WHEN actual_carrier IN ('eParcel', 'eParcel Metro')
                             AND chargeable_kg > 22 THEN 'AP > 22 kg'
                        WHEN actual_carrier = 'GoEXPRESS'
                             AND chargeable_kg > 20 THEN 'GoEXPRESS > 20 kg'
                        WHEN actual_carrier = 'GoEXPRESS'
                             AND chargeable_kg <= 20 THEN 'GoEXPRESS zone mapping'
                        WHEN actual_carrier = 'Allied Express'
                             AND chargeable_kg IS NOT NULL THEN 'Allied zone mapping'
                        WHEN actual_carrier = 'Couriers Please'
                             AND chargeable_kg IS NOT NULL THEN 'Couriers Please zone mapping'
                        WHEN actual_carrier IS NULL THEN 'Missing carrier'
                        ELSE 'Other / review'
                    END AS root_cause_group
                FROM base
            )
            SELECT
                actual_carrier,
                parcel_count,
                root_cause_group,
                COUNT(*) AS row_count,
                ROUND(AVG(chargeable_kg), 2) AS avg_chargeable_kg,
                ROUND(MAX(chargeable_kg), 2) AS max_chargeable_kg
            FROM scored
            WHERE quoted_cost_inc_gst IS NOT NULL
            GROUP BY 1, 2, 3
            ORDER BY row_count DESC, actual_carrier, parcel_count, root_cause_group
            """
        ).collect()

        for row in rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
