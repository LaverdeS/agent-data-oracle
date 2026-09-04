import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
@pytest.mark.integration
async def test_migrations_apply_to_postgresql(postgres_url: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_data_oracle",
            "migrate",
            "--database-url",
            postgres_url,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
    finally:
        await engine.dispose()

    assert result.returncode == 0, result.stderr
    assert revision == "0003"
