import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent_data_oracle.cpsc_source import (
    CPSC_RECALL_API_URL,
    CpscRefreshMode,
    RejectedImport,
    cpsc_source_status,
    import_cpsc_refresh_response,
    refresh_cpsc_source,
)

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


def import_fixture(
    postgres_url: str,
    observed_at: str,
    *,
    fixture: Path = FIXTURE,
    expected_record_count: int = 1,
    source_url: str | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        "job",
        "cpsc-import-fixture",
        "--database-url",
        postgres_url,
        "--fixture",
        str(fixture),
        "--observed-at",
        observed_at,
        "--expected-record-count",
        str(expected_record_count),
    ]
    if source_url is not None:
        arguments.extend(("--source-url", source_url))
    return run_command(*arguments)


@pytest_asyncio.fixture
async def source_database(postgres_url: str) -> AsyncEngine:
    migration = run_command("migrate", "--database-url", postgres_url)
    assert migration.returncode == 0, migration.stderr

    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE cpsc_source_tombstones, cpsc_current_source_revision, "
                "cpsc_current_records, "
                "cpsc_revision_records, "
                "cpsc_source_observations, cpsc_recall_versions, cpsc_recalls, "
                "cpsc_source_revisions, cpsc_ingestion_runs CASCADE"
            )
        )
        await connection.execute(
            text(
                "UPDATE cpsc_source_refresh_state SET "
                "last_successful_observed_at = NULL, last_failure_kind = NULL, "
                "integrity_pause_reason = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE singleton = true"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO global_pause_state (singleton, is_paused) "
                "VALUES (true, false) ON CONFLICT (singleton) DO UPDATE "
                "SET is_paused = false, trigger_kind = NULL, reason_category = NULL, "
                "activated_at = NULL, activated_by = NULL, resolution_note = NULL, "
                "resolved_at = NULL, resolved_by = NULL"
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_reconciliation_records_disappearances_as_tombstones(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    first = import_fixture(postgres_url, "2026-08-01T12:00:00Z")
    assert first.returncode == 0, first.stderr

    empty_response = await import_cpsc_refresh_response(
        database_url=postgres_url,
        raw_response=b"[]",
        observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        expected_record_count=0,
        source_url=f"{CPSC_RECALL_API_URL}?format=json",
        refresh_mode=CpscRefreshMode.FULL,
        retrieval_attempts=1,
    )

    assert empty_response.record_count == 0
    async with source_database.connect() as connection:
        tombstones = await connection.execute(
            text("SELECT recall_id FROM cpsc_source_tombstones ORDER BY recall_id")
        )
    assert tombstones.all() == [(10887,)]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_daily_overlap_keeps_unreturned_current_records(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    records = json.loads(FIXTURE.read_text())
    second_record = dict(records[0])
    second_record["RecallID"] = 20887
    second_record["RecallNumber"] = "99999"
    first_full = await import_cpsc_refresh_response(
        database_url=postgres_url,
        raw_response=json.dumps([records[0], second_record]).encode(),
        observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        expected_record_count=2,
        source_url=f"{CPSC_RECALL_API_URL}?format=json",
        refresh_mode=CpscRefreshMode.FULL,
        retrieval_attempts=1,
    )
    assert first_full.record_count == 2

    daily = await import_cpsc_refresh_response(
        database_url=postgres_url,
        raw_response=json.dumps([records[0]]).encode(),
        observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        expected_record_count=1,
        source_url=f"{CPSC_RECALL_API_URL}?LastPublishDateStart=2026-07-26T00%3A00%3A00Z&format=json",
        refresh_mode=CpscRefreshMode.DAILY,
        retrieval_attempts=1,
    )
    assert daily.record_count == 2
    async with source_database.connect() as connection:
        current_count = await connection.scalar(
            text("SELECT count(*) FROM cpsc_current_records")
        )
    assert current_count == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_live_refresh_uses_the_cross_process_advisory_lock(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    del source_database
    requested_urls: list[str] = []

    def recorded_fetch(url: str, timeout_seconds: float) -> bytes:
        del timeout_seconds
        requested_urls.append(url)
        return FIXTURE.read_bytes()

    result = await refresh_cpsc_source(
        database_url=postgres_url,
        refresh_mode=CpscRefreshMode.FULL,
        observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        expected_record_count=1,
        fetch=recorded_fetch,
    )

    assert result.record_count == 1
    assert requested_urls == [f"{CPSC_RECALL_API_URL}?format=json"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stale_completed_source_is_visibly_disabled_after_48_hours(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    del source_database
    imported = import_fixture(postgres_url, "2026-08-01T12:00:00Z")
    assert imported.returncode == 0, imported.stderr

    status = await cpsc_source_status(
        postgres_url, now=datetime(2026, 8, 3, 12, 0, 1, tzinfo=UTC)
    )

    assert status["source_status"] == {
        "application_readiness": "disabled",
        "freshness": "stale",
        "integrity_pause_reason": None,
        "last_failure_kind": None,
    }


@pytest.mark.asyncio
@pytest.mark.integration
async def test_provenance_failure_activates_the_database_global_pause(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    record = json.loads(FIXTURE.read_text())
    record[0]["URL"] = "https://example.invalid/not-a-cpsc-notice"

    result = await import_cpsc_refresh_response(
        database_url=postgres_url,
        raw_response=json.dumps(record).encode(),
        observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        expected_record_count=1,
        source_url=f"{CPSC_RECALL_API_URL}?format=json",
        refresh_mode=CpscRefreshMode.FULL,
        retrieval_attempts=1,
    )

    assert isinstance(result, RejectedImport)
    assert result.error_code == "official_notice_provenance_invalid"
    async with source_database.connect() as connection:
        paused = await connection.scalar(
            text("SELECT is_paused FROM global_pause_state WHERE singleton = true")
        )
    assert paused is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fixture_import_creates_an_inspectable_completed_revision(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    imported = import_fixture(
        postgres_url,
        "2026-08-01T12:00:00Z",
        source_url=FIXTURE_SOURCE_URL,
    )
    status = run_command("job", "cpsc-status", "--database-url", postgres_url)

    assert imported.returncode == 0, imported.stderr
    assert output_document(imported)["record_count"] == 1
    assert status.returncode == 0, status.stderr
    status_document = output_document(status)
    assert status_document["current_revision"]["record_count"] == 1
    assert (
        status_document["current_revision"]["revision_id"]
        == (output_document(imported)["revision_id"])
    )
    assert (
        datetime.fromisoformat(
            status_document["current_revision"]["completed_at"]
        ).tzinfo
        is UTC
    )
    assert status_document["last_run"] == {
        "observed_at": "2026-08-01T12:00:00+00:00",
        "record_count": 1,
        "state": "completed",
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

    first = import_fixture(postgres_url, "2026-08-01T12:00:00Z")
    second = import_fixture(
        postgres_url,
        "2026-07-01T12:00:00Z",
        fixture=reformatted_fixture,
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

    status = run_command("job", "cpsc-status", "--database-url", postgres_url)
    assert output_document(status)["last_run"]["observed_at"] == (
        "2026-07-01T12:00:00+00:00"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rejected_fixture_does_not_replace_current_revision(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    valid = import_fixture(postgres_url, "2026-08-01T12:00:00Z")

    rejected = import_fixture(
        postgres_url,
        "2026-08-02T12:00:00Z",
        expected_record_count=2,
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
    valid = import_fixture(postgres_url, "2026-08-01T12:00:00Z")
    assert valid.returncode == 0, valid.stderr

    async with source_database.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE cpsc_revision_records ADD CONSTRAINT "
                "simulate_promotion_failure CHECK (false) NOT VALID"
            )
        )
    try:
        failed = import_fixture(postgres_url, "2026-08-02T12:00:00Z")
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
    imported = import_fixture(postgres_url, "2026-08-01T12:00:00Z")
    assert imported.returncode == 0, imported.stderr

    with pytest.raises(DBAPIError):
        async with source_database.begin() as connection:
            await connection.execute(
                text("UPDATE cpsc_recall_versions SET recall_number = 'changed'")
            )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_current_projection_cannot_mix_completed_revisions(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    first = import_fixture(postgres_url, "2026-08-01T12:00:00Z")
    second = import_fixture(postgres_url, "2026-08-02T12:00:00Z")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    with pytest.raises(DBAPIError):
        async with source_database.begin() as connection:
            await connection.execute(
                text("UPDATE cpsc_current_records SET revision_id = :revision_id"),
                {"revision_id": output_document(first)["revision_id"]},
            )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pending_and_late_source_rows_never_enter_current_projection(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    imported = import_fixture(postgres_url, "2026-08-01T12:00:00Z")
    assert imported.returncode == 0, imported.stderr

    pending_run_id = uuid4()
    pending_revision_id = uuid4()
    async with source_database.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO cpsc_ingestion_runs "
                "(run_id, source_url, observed_at, raw_response, "
                "raw_response_sha256, state, created_at) VALUES "
                "(:run_id, 'recorded-test', CURRENT_TIMESTAMP, "
                ":raw_response, :raw_hash, 'pending', CURRENT_TIMESTAMP)"
            ),
            {
                "raw_hash": "0" * 64,
                "raw_response": b"[]",
                "run_id": pending_run_id,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO cpsc_source_revisions "
                "(revision_id, run_id, state, completeness, created_at) VALUES "
                "(:revision_id, :run_id, 'pending', 'unknown', CURRENT_TIMESTAMP)"
            ),
            {"revision_id": pending_revision_id, "run_id": pending_run_id},
        )

    with pytest.raises(DBAPIError):
        async with source_database.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE cpsc_current_source_revision SET revision_id = :revision_id"
                ),
                {"revision_id": pending_revision_id},
            )

    late_recall_id = 999_999
    late_version_id = uuid4()
    with pytest.raises(DBAPIError):
        async with source_database.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO cpsc_recalls (recall_id, first_observed_at) "
                    "VALUES (:recall_id, CURRENT_TIMESTAMP)"
                ),
                {"recall_id": late_recall_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO cpsc_recall_versions "
                    "(version_id, recall_id, content_hash, raw_record, "
                    "recall_number, official_url, created_at) VALUES "
                    "(:version_id, :recall_id, :content_hash, '{}'::jsonb, "
                    "'late', 'https://www.cpsc.gov/', CURRENT_TIMESTAMP)"
                ),
                {
                    "content_hash": "1" * 64,
                    "recall_id": late_recall_id,
                    "version_id": late_version_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO cpsc_revision_records "
                    "(revision_id, recall_id, version_id, source_position) "
                    "VALUES (:revision_id, :recall_id, :version_id, 1)"
                ),
                {
                    "recall_id": late_recall_id,
                    "revision_id": output_document(imported)["revision_id"],
                    "version_id": late_version_id,
                },
            )

    status = run_command("job", "cpsc-status", "--database-url", postgres_url)
    assert (
        output_document(status)["current_revision"]["revision_id"]
        == (output_document(imported)["revision_id"])
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_uncommitted_projection_switch_is_not_visible(
    postgres_url: str, source_database: AsyncEngine
) -> None:
    imported = import_fixture(postgres_url, "2026-08-01T12:00:00Z")
    assert imported.returncode == 0, imported.stderr

    run_id = uuid4()
    revision_id = uuid4()
    async with source_database.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text(
                "INSERT INTO cpsc_ingestion_runs "
                "(run_id, source_url, observed_at, raw_response, "
                "raw_response_sha256, state, created_at) VALUES "
                "(:run_id, 'recorded-test', CURRENT_TIMESTAMP, "
                ":raw_response, :raw_hash, 'pending', CURRENT_TIMESTAMP)"
            ),
            {"raw_hash": "2" * 64, "raw_response": b"[]", "run_id": run_id},
        )
        await connection.execute(
            text(
                "INSERT INTO cpsc_source_revisions "
                "(revision_id, run_id, state, completeness, created_at) VALUES "
                "(:revision_id, :run_id, 'pending', 'unknown', CURRENT_TIMESTAMP)"
            ),
            {"revision_id": revision_id, "run_id": run_id},
        )
        await connection.execute(
            text(
                "UPDATE cpsc_source_revisions SET state = 'completed', "
                "completeness = 'complete', completed_at = CURRENT_TIMESTAMP "
                "WHERE revision_id = :revision_id"
            ),
            {"revision_id": revision_id},
        )
        await connection.execute(
            text(
                "UPDATE cpsc_ingestion_runs SET state = 'completed', "
                "finished_at = CURRENT_TIMESTAMP WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        )
        await connection.execute(
            text("UPDATE cpsc_current_source_revision SET revision_id = :revision_id"),
            {"revision_id": revision_id},
        )
        await connection.execute(text("DELETE FROM cpsc_current_records"))

        status = run_command("job", "cpsc-status", "--database-url", postgres_url)
        assert (
            output_document(status)["current_revision"]["revision_id"]
            == (output_document(imported)["revision_id"])
        )
        await transaction.rollback()

    status = run_command("job", "cpsc-status", "--database-url", postgres_url)
    assert (
        output_document(status)["current_revision"]["revision_id"]
        == (output_document(imported)["revision_id"])
    )
