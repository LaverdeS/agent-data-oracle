import os

import pytest


@pytest.fixture
def postgres_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:54329/agent_data_oracle_test",
    )
