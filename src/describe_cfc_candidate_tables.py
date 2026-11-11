from src.db import get_session


TABLES = [
    "KSFDA.CATCH_CFC.SKU_ATTRIBUTE",
    "KSFDA.CATCH_CFC.PACKED",
    "KSFDA.CATCH_CFC.PICKED",
    "KSFDA.CATCH_CFC.INVENTORY",
    "KSFDA.CATCH_CFC.BIN_INFORMATION",
    "KSFDA.CATCH_CFC.ZONE_ATTRIBUTE",
]


def main() -> None:
    session = get_session()
    try:
        for table_name in TABLES:
            print(f"\n{table_name}")
            rows = session.sql(f"describe table {table_name}").collect()
            for row in rows:
                print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
