import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

FIXTURE = Path("tests/fixtures/cpsc/recall-10887.json")
FIXTURE_SOURCE_URL = (
    "https://www.saferproducts.gov/RestWebServices/Recall?RecallID=10887&format=json"
)


def run_command(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_data_oracle", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def output_document(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout.splitlines()[-1])


@pytest_asyncio.fixture
async def source_database(postgres_url: str) -> AsyncEngine:
    migration = run_command("migrate", "--database-url", postgres_url)
    assert migration.returncode == 0, migration.stderr

    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE cpsc_current_records, cpsc_revision_records, "
                "cpsc_source_observations, cpsc_recall_versions, cpsc_recalls, "
                "cpsc_source_revisions, cpsc_ingestion_runs CASCADE"
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fixture_import_creates_an_inspectable_completed_revision(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    imported = run_command(
        "job",
        "cpsc-import-fixture",
        "--database-url",
        postgres_url,
        "--fixture",
        str(FIXTURE),
        "--observed-at",
        "2026-08-01T12:00:00Z",
        "--expected-record-count",
        "1",
        "--source-url",
        FIXTURE_SOURCE_URL,
    )
    status = run_command("job", "cpsc-status", "--database-url", postgres_url)

    assert imported.returncode == 0, imported.stderr
    assert output_document(imported)["record_count"] == 1
    assert status.returncode == 0, status.stderr
    assert output_document(status) == {
        "current_revision": {
            "completed_at": "2026-08-01T12:00:00+00:00",
            "record_count": 1,
            "revision_id": output_document(imported)["revision_id"],
        },
        "last_run": {
            "observed_at": "2026-08-01T12:00:00+00:00",
            "record_count": 1,
            "state": "completed",
        },
    }
    assert "HARPPA" not in status.stdout

    async with source_database.connect() as connection:
        source_dates = (
            await connection.execute(
                text(
                    "SELECT recall_date_literal, last_publish_date_literal "
                    "FROM cpsc_recall_versions"
                )
            )
        ).one()
        run_evidence = (
            await connection.execute(
                text("SELECT raw_response, source_url FROM cpsc_ingestion_runs")
            )
        ).one()

    assert source_dates == (
        "2026-07-30T00:00:00",
        "2026-07-31T00:00:00",
    )
    assert bytes(run_evidence.raw_response) == FIXTURE.read_bytes()
    assert run_evidence.source_url == FIXTURE_SOURCE_URL


@pytest.mark.asyncio
@pytest.mark.integration
async def test_equivalent_json_reuses_version_and_appends_observation(
    tmp_path: Path, postgres_url: str, source_database: AsyncEngine
) -> None:
    reformatted_fixture = tmp_path / "reformatted.json"
    reformatted_fixture.write_text(
        json.dumps(json.loads(FIXTURE.read_text(encoding="utf-8")), indent=2),
        encoding="utf-8",
    )

    first = run_command(
        "job",
        "cpsc-import-fixture",
        "--database-url",
        postgres_url,
        "--fixture",
        str(FIXTURE),
        "--observed-at",
        "2026-08-01T12:00:00Z",
        "--expected-record-count",
        "1",
    )
    second = run_command(
        "job",
        "cpsc-import-fixture",
        "--database-url",
        postgres_url,
        "--fixture",
        str(reformatted_fixture),
        "--observed-at",
        "2026-08-02T12:00:00+00:00",
        "--expected-record-count",
        "1",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (
        output_document(first)["content_hashes"]
        == output_document(second)["content_hashes"]
    )
    assert output_document(second)["reused_version_count"] == 1

    async with source_database.connect() as connection:
        counts = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM cpsc_recall_versions), "
                    "(SELECT count(*) FROM cpsc_source_observations), "
                    "(SELECT count(*) FROM cpsc_source_revisions), "
                    "(SELECT count(*) FROM cpsc_ingestion_runs)"
                )
            )
        ).one()
        current_revision_id = await connection.scalar(
            text("SELECT DISTINCT revision_id FROM cpsc_current_records")
        )

    assert counts == (1, 2, 2, 2)
    assert str(current_revision_id) == output_document(second)["revision_id"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rejected_fixture_does_not_replace_current_revision(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    valid = run_command(
        "job",
        "cpsc-import-fixture",
        "--database-url",
        postgres_url,
        "--fixture",
        str(FIXTURE),
        "--observed-at",
        "2026-08-01T12:00:00Z",
        "--expected-record-count",
        "1",
    )

    rejected = run_command(
        "job",
        "cpsc-import-fixture",
        "--database-url",
        postgres_url,
        "--fixture",
        str(FIXTURE),
        "--observed-at",
        "2026-08-02T12:00:00Z",
        "--expected-record-count",
        "2",
    )
    status = run_command("job", "cpsc-status", "--database-url", postgres_url)

    assert valid.returncode == 0, valid.stderr
    assert rejected.returncode == 1
    assert output_document(rejected)["state"] == "rejected"
    assert (
        output_document(status)["current_revision"]["revision_id"]
        == (output_document(valid)["revision_id"])
    )
    assert output_document(status)["last_run"]["state"] == "rejected"

    rejected_revision_id = output_document(rejected)["revision_id"]
    with pytest.raises(DBAPIError):
        async with source_database.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE cpsc_current_source_revision SET revision_id = :revision_id"
                ),
                {"revision_id": rejected_revision_id},
            )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_failed_promotion_keeps_the_previous_current_revision(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    valid = run_command(
        "job",
        "cpsc-import-fixture",
        "--database-url",
        postgres_url,
        "--fixture",
        str(FIXTURE),
        "--observed-at",
        "2026-08-01T12:00:00Z",
        "--expected-record-count",
        "1",
    )
    assert valid.returncode == 0, valid.stderr

    async with source_database.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE cpsc_revision_records ADD CONSTRAINT "
                "simulate_promotion_failure CHECK (false) NOT VALID"
            )
        )
    try:
        failed = run_command(
            "job",
            "cpsc-import-fixture",
            "--database-url",
            postgres_url,
            "--fixture",
            str(FIXTURE),
            "--observed-at",
            "2026-08-02T12:00:00Z",
            "--expected-record-count",
            "1",
        )
    finally:
        async with source_database.begin() as connection:
            await connection.execute(
                text(
                    "ALTER TABLE cpsc_revision_records DROP CONSTRAINT "
                    "simulate_promotion_failure"
                )
            )

    status = run_command("job", "cpsc-status", "--database-url", postgres_url)
    assert failed.returncode == 1
    assert output_document(failed)["state"] == "failed"
    assert (
        output_document(status)["current_revision"]["revision_id"]
        == (output_document(valid)["revision_id"])
    )
    assert output_document(status)["last_run"]["state"] == "failed"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_record_versions_are_immutable(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    imported = run_command(
        "job",
        "cpsc-import-fixture",
        "--database-url",
        postgres_url,
        "--fixture",
        str(FIXTURE),
        "--observed-at",
        "2026-08-01T12:00:00Z",
        "--expected-record-count",
        "1",
    )
    assert imported.returncode == 0, imported.stderr

    with pytest.raises(DBAPIError):
        async with source_database.begin() as connection:
            await connection.execute(
                text("UPDATE cpsc_recall_versions SET recall_number = 'changed'")
            )
