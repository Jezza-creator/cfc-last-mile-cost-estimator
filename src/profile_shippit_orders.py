from src.db import get_session


def main() -> None:
    session = get_session()
    try:
        print("ROW COUNT")
        print(
            session.sql(
                "select count(*) as row_count from KSFPA.FREIGHT.SHIPPIT_ORDERS"
            ).collect()
        )

        print("\nDISTINCT SERVICES")
        service_rows = session.sql(
            """
            select
                CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME as service_name,
                count(*) as row_count
            from KSFPA.FREIGHT.SHIPPIT_ORDERS
            group by 1
            order by row_count desc, service_name
            """
        ).collect()
        for row in service_rows:
            print(row)

        print("\nKEY COLUMN COVERAGE")
        coverage_rows = session.sql(
            """
            select
                count_if(ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER is null) as null_order_invoice_number,
                count_if(CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME is null) as null_service_name,
                count_if(ORDER_LOCATION__DELIVERY_POSTCODE is null) as null_delivery_postcode,
                count_if(ORDER_LOCATION__DELIVERY_SUBURB is null) as null_delivery_suburb,
                count_if(ORDER_LOCATION__DELIVERY_STATE is null) as null_delivery_state,
                count_if(ORDER_WEIGHT__DEAD_WEIGHT is null) as null_dead_weight,
                count_if(ORDER_WEIGHT__VOLUMETRIC_WEIGHT is null) as null_volumetric_weight,
                count_if(ORDER_WEIGHT__CHARGEABLE_WEIGHT is null) as null_chargeable_weight,
                count_if(ORDER_PARCEL__PARCEL_COUNT is null) as null_parcel_count,
                count_if(ORDER_CARRIER__QUOTED_SHIPPING_COST_INCL_GST is null) as null_quoted_cost
            from KSFPA.FREIGHT.SHIPPIT_ORDERS
            """
        ).collect()
        print(coverage_rows)
    finally:
        session.close()


if __name__ == "__main__":
    main()
