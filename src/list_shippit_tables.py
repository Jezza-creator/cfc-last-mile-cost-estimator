from src.db import get_session


def main() -> None:
    session = get_session()
    try:
        rows = session.sql(
            "show tables like 'SHIPPIT%' in schema KSFPA.FREIGHT"
        ).collect()
        for row in rows:
            print(row)
    finally:
        session.close()


if __name__ == "__main__":
    main()
