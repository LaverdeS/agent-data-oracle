import os

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@127.0.0.1:54329/agent_data_oracle_test"
)


def database_url_from_environment() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
