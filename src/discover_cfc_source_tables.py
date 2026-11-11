from src.db import get_session


def main() -> None:
    session = get_session()
    try:
        queries = {
            "CATCH_CFC tables": """
                select table_schema, table_name
                from KSFDA.information_schema.tables
                where table_schema = 'CATCH_CFC'
                order by table_name
            """,
            "Possible SKU tables": """
                select table_schema, table_name
                from KSFDA.information_schema.tables
                where table_schema = 'CATCH_CFC'
                  and upper(table_name) like '%SKU%'
                order by table_name
            """,
            "Possible order tables": """
                select table_schema, table_name
                from KSFDA.information_schema.tables
                where table_schema = 'CATCH_CFC'
                  and (
                      upper(table_name) like '%ORDER%'
                      or upper(table_name) like '%SHIP%'
                      or upper(table_name) like '%PARCEL%'
                  )
                order by table_name
            """,
            "Possible inventory/bin tables": """
                select table_schema, table_name
                from KSFDA.information_schema.tables
                where table_schema = 'CATCH_CFC'
                  and (
                      upper(table_name) like '%INVENTORY%'
                      or upper(table_name) like '%BIN%'
                      or upper(table_name) like '%ZONE%'
                  )
                order by table_name
            """,
        }

        for label, sql in queries.items():
            print(f"\n{label}")
            rows = session.sql(sql).collect()
            for row in rows:
                print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
