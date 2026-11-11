from .db import CONNECTION_NAME
from snowflake.snowpark import Session

def main() -> None:
    session = Session.builder.config("connection_name", CONNECTION_NAME).create()
    try:
        result = session.sql("select current_user(), current_role(), current_warehouse(), current_database()").collect()
        print(result)
    finally:
        session.close()

if __name__ == "__main__":
    main()
