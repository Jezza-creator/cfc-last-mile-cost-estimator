import os

from snowflake.snowpark import Session

CONNECTION_NAME = os.environ.get("LM_SNOWFLAKE_CONNECTION", "tsfpa_app_dev").strip() or "tsfpa_app_dev"

def get_session() -> Session:
    """Create a Snowpark session using the named Snowflake connection."""
    return Session.builder.config("connection_name", CONNECTION_NAME).create()
