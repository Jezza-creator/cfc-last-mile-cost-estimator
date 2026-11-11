from src.db import get_session


def main() -> None:
    session = get_session()
    try:
        print("PACKED ROW COUNT")
        print(session.sql("select count(*) as row_count from KSFDA.CATCH_CFC.PACKED").collect())

        print("\nPACKED ORDER_ID SAMPLE")
        rows = session.sql(
            """
            select
                ORDER_ID,
                count(*) as row_count,
                min(PACK_TIMESTAMP) as min_pack_ts,
                max(PACK_TIMESTAMP) as max_pack_ts
            from KSFDA.CATCH_CFC.PACKED
            group by 1
            order by row_count desc, ORDER_ID
            limit 25
            """
        ).collect()
        for row in rows:
            print(row)

        print("\nJOIN RATE TO SHIPPIT KEYS")
        join_rows = session.sql(
            """
            with shippit as (
                select distinct
                    ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER as invoice_number,
                    ORDER_ATTRIBUTE__ORDER_REFERENCE_NUMBER as reference_number
                from KSFPA.FREIGHT.SHIPPIT_ORDERS
                where STORE_ATTRIBUTE__STORE_NAME = '1194 Moorebank CFC NSW AU'
                  and ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::date
                        between dateadd(day, -6, current_date()) and current_date()
            ),
            packed as (
                select distinct
                    ORDER_ID,
                    regexp_replace(ORDER_ID, '^KMT1194', '') as stripped_kmt1194,
                    regexp_replace(ORDER_ID, '^KMT', '') as stripped_kmt
                from KSFDA.CATCH_CFC.PACKED
            )
            select
                count(*) as packed_order_ids,
                count_if(i.invoice_number is not null) as match_invoice_raw,
                count_if(r.reference_number is not null) as match_reference_raw,
                count_if(i2.invoice_number is not null) as match_invoice_strip_kmt1194,
                count_if(r2.reference_number is not null) as match_reference_strip_kmt1194,
                count_if(i3.invoice_number is not null) as match_invoice_strip_kmt,
                count_if(r3.reference_number is not null) as match_reference_strip_kmt
            from packed p
            left join shippit i
              on i.invoice_number = p.ORDER_ID
            left join shippit r
              on r.reference_number = p.ORDER_ID
            left join shippit i2
              on i2.invoice_number = p.stripped_kmt1194
            left join shippit r2
              on r2.reference_number = p.stripped_kmt1194
            left join shippit i3
              on i3.invoice_number = p.stripped_kmt
            left join shippit r3
              on r3.reference_number = p.stripped_kmt
            """
        ).collect()
        for row in join_rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
