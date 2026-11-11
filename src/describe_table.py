import argparse

from src.db import get_session


def main() -> None:
    parser = argparse.ArgumentParser(description="Describe a Snowflake table.")
    parser.add_argument("table_name", help="Fully qualified table name")
    args = parser.parse_args()

    session = get_session()
    try:
        rows = session.sql(f"describe table {args.table_name}").collect()
        for row in rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
