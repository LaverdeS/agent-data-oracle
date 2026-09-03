import os

import pytest

from agent_data_oracle.config import LOCAL_DATABASE_URL


@pytest.fixture
def postgres_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", LOCAL_DATABASE_URL)
