import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

CPSC_RECALL_API_URL = "https://www.saferproducts.gov/RestWebServices/Recall"


class SourceValidationError(ValueError):
    """The received source body cannot become a completed revision."""


@dataclass(frozen=True)
class CpscRecord:
    recall_id: int
    recall_number: str
    recall_date_literal: str | None
    last_publish_date_literal: str | None
    official_url: str
    raw_record: dict[str, Any]
    canonical_json: str
    content_hash: str


@dataclass(frozen=True)
class ImportResult:
    revision_id: UUID
    record_count: int
    reused_version_count: int
    content_hashes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "content_hashes": list(self.content_hashes),
            "record_count": self.record_count,
            "reused_version_count": self.reused_version_count,
            "revision_id": str(self.revision_id),
            "state": "completed",
        }


@dataclass(frozen=True)
class RejectedImport:
    revision_id: UUID
    error_code: str

    def as_dict(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "revision_id": str(self.revision_id),
            "state": "rejected",
        }


@dataclass(frozen=True)
class FailedImport:
    revision_id: UUID

    def as_dict(self) -> dict[str, object]:
        return {
            "error_code": "promotion_failed",
            "revision_id": str(self.revision_id),
            "state": "failed",
        }


def parse_observed_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SourceValidationError("observed_at_invalid") from error
    if parsed.tzinfo is None:
        raise SourceValidationError("observed_at_timezone_required")
    return parsed.astimezone(UTC)


def _required_text(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise SourceValidationError(f"missing_{field}")
    return value


def _optional_source_literal(record: dict[str, Any], field: str) -> str | None:
    value = record.get(field)
    if value is not None and not isinstance(value, str):
        raise SourceValidationError(f"invalid_{field}")
    return value


def parse_source_records(
    raw_response: bytes, *, expected_record_count: int
) -> tuple[CpscRecord, ...]:
    try:
        document = json.loads(raw_response)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceValidationError("invalid_json") from error
    if not isinstance(document, list):
        raise SourceValidationError("expected_record_array")
    if len(document) != expected_record_count:
        raise SourceValidationError("record_count_mismatch")

    records: list[CpscRecord] = []
    recall_ids: set[int] = set()
    for value in document:
        if not isinstance(value, dict):
            raise SourceValidationError("record_must_be_object")
        record = cast(dict[str, Any], value)
        recall_id = record.get("RecallID")
        if not isinstance(recall_id, int) or isinstance(recall_id, bool):
            raise SourceValidationError("missing_RecallID")
        if recall_id in recall_ids:
            raise SourceValidationError("duplicate_RecallID")
        recall_ids.add(recall_id)

        canonical_json = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        records.append(
            CpscRecord(
                recall_id=recall_id,
                recall_number=_required_text(record, "RecallNumber"),
                recall_date_literal=_optional_source_literal(record, "RecallDate"),
                last_publish_date_literal=_optional_source_literal(
                    record, "LastPublishDate"
                ),
                official_url=_required_text(record, "URL"),
                raw_record=record,
                canonical_json=canonical_json,
                content_hash=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(records)


async def _start_revision(
    connection: AsyncConnection,
    *,
    run_id: UUID,
    revision_id: UUID,
    source_url: str,
    observed_at: datetime,
    raw_response: bytes,
) -> None:
    await connection.execute(
        text(
            "INSERT INTO cpsc_ingestion_runs "
            "(run_id, source_url, observed_at, raw_response, raw_response_sha256, "
            "state, created_at) VALUES "
            "(:run_id, :source_url, :observed_at, :raw_response, :raw_hash, "
            "'pending', :observed_at)"
        ),
        {
            "observed_at": observed_at,
            "raw_hash": hashlib.sha256(raw_response).hexdigest(),
            "raw_response": raw_response,
            "run_id": run_id,
            "source_url": source_url,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO cpsc_source_revisions "
            "(revision_id, run_id, state, completeness, created_at) VALUES "
            "(:revision_id, :run_id, 'pending', 'unknown', :observed_at)"
        ),
        {
            "observed_at": observed_at,
            "revision_id": revision_id,
            "run_id": run_id,
        },
    )


async def _reject_revision(
    connection: AsyncConnection,
    *,
    run_id: UUID,
    revision_id: UUID,
    observed_at: datetime,
    error_code: str,
) -> None:
    await connection.execute(
        text(
            "UPDATE cpsc_ingestion_runs SET state = 'rejected', "
            "error_code = :error_code, finished_at = :observed_at "
            "WHERE run_id = :run_id"
        ),
        {
            "error_code": error_code,
            "observed_at": observed_at,
            "run_id": run_id,
        },
    )
    await connection.execute(
        text(
            "UPDATE cpsc_source_revisions SET state = 'rejected', "
            "completeness = 'partial' WHERE revision_id = :revision_id"
        ),
        {"revision_id": revision_id},
    )


async def _fail_revision(
    connection: AsyncConnection,
    *,
    run_id: UUID,
    revision_id: UUID,
    observed_at: datetime,
) -> None:
    await connection.execute(
        text(
            "UPDATE cpsc_ingestion_runs SET state = 'failed', "
            "error_code = 'promotion_failed', finished_at = :observed_at "
            "WHERE run_id = :run_id"
        ),
        {"observed_at": observed_at, "run_id": run_id},
    )
    await connection.execute(
        text(
            "UPDATE cpsc_source_revisions SET state = 'failed', "
            "completeness = 'partial' WHERE revision_id = :revision_id"
        ),
        {"revision_id": revision_id},
    )


async def _record_version(
    connection: AsyncConnection,
    *,
    record: CpscRecord,
    observed_at: datetime,
) -> tuple[UUID, bool]:
    await connection.execute(
        text(
            "INSERT INTO cpsc_recalls (recall_id, first_observed_at) "
            "VALUES (:recall_id, :observed_at) ON CONFLICT (recall_id) DO NOTHING"
        ),
        {"observed_at": observed_at, "recall_id": record.recall_id},
    )
    version_id = uuid4()
    inserted = await connection.scalar(
        text(
            "INSERT INTO cpsc_recall_versions "
            "(version_id, recall_id, content_hash, raw_record, recall_number, "
            "recall_date_literal, last_publish_date_literal, official_url, created_at) "
            "VALUES (:version_id, :recall_id, :content_hash, "
            "CAST(:raw_record AS jsonb), :recall_number, :recall_date, "
            ":last_publish_date, :official_url, :observed_at) "
            "ON CONFLICT (recall_id, content_hash) DO NOTHING "
            "RETURNING version_id"
        ),
        {
            "content_hash": record.content_hash,
            "last_publish_date": record.last_publish_date_literal,
            "observed_at": observed_at,
            "official_url": record.official_url,
            "raw_record": record.canonical_json,
            "recall_date": record.recall_date_literal,
            "recall_id": record.recall_id,
            "recall_number": record.recall_number,
            "version_id": version_id,
        },
    )
    if inserted is not None:
        return cast(UUID, inserted), False
    reused = await connection.scalar(
        text(
            "SELECT version_id FROM cpsc_recall_versions "
            "WHERE recall_id = :recall_id AND content_hash = :content_hash"
        ),
        {"content_hash": record.content_hash, "recall_id": record.recall_id},
    )
    return cast(UUID, reused), True


async def _promote_revision(
    connection: AsyncConnection,
    *,
    run_id: UUID,
    revision_id: UUID,
    records: tuple[CpscRecord, ...],
    observed_at: datetime,
) -> int:
    reused_count = 0
    for position, record in enumerate(records):
        version_id, reused = await _record_version(
            connection, record=record, observed_at=observed_at
        )
        reused_count += int(reused)
        await connection.execute(
            text(
                "INSERT INTO cpsc_revision_records "
                "(revision_id, recall_id, version_id, source_position) VALUES "
                "(:revision_id, :recall_id, :version_id, :source_position)"
            ),
            {
                "recall_id": record.recall_id,
                "revision_id": revision_id,
                "source_position": position,
                "version_id": version_id,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO cpsc_source_observations "
                "(observation_id, run_id, revision_id, recall_id, version_id, "
                "observed_at) VALUES (:observation_id, :run_id, :revision_id, "
                ":recall_id, :version_id, :observed_at)"
            ),
            {
                "observation_id": uuid4(),
                "observed_at": observed_at,
                "recall_id": record.recall_id,
                "revision_id": revision_id,
                "run_id": run_id,
                "version_id": version_id,
            },
        )

    record_count = len(records)
    await connection.execute(
        text(
            "UPDATE cpsc_source_revisions SET state = 'completed', "
            "completeness = 'complete', record_count = :record_count, "
            "completed_at = :observed_at WHERE revision_id = :revision_id"
        ),
        {
            "observed_at": observed_at,
            "record_count": record_count,
            "revision_id": revision_id,
        },
    )
    await connection.execute(text("DELETE FROM cpsc_current_records"))
    await connection.execute(
        text(
            "INSERT INTO cpsc_current_records "
            "(recall_id, revision_id, version_id, recall_number, "
            "recall_date_literal, last_publish_date_literal, official_url, "
            "normalized_record, projected_at) "
            "SELECT versions.recall_id, records.revision_id, versions.version_id, "
            "versions.recall_number, versions.recall_date_literal, "
            "versions.last_publish_date_literal, versions.official_url, "
            "versions.raw_record, :observed_at "
            "FROM cpsc_revision_records AS records "
            "JOIN cpsc_recall_versions AS versions "
            "ON versions.version_id = records.version_id "
            "WHERE records.revision_id = :revision_id"
        ),
        {"observed_at": observed_at, "revision_id": revision_id},
    )
    await connection.execute(
        text(
            "INSERT INTO cpsc_current_source_revision "
            "(singleton, revision_id, projected_at) "
            "VALUES (true, :revision_id, :observed_at) "
            "ON CONFLICT (singleton) DO UPDATE SET "
            "revision_id = EXCLUDED.revision_id, "
            "projected_at = EXCLUDED.projected_at"
        ),
        {"observed_at": observed_at, "revision_id": revision_id},
    )
    await connection.execute(
        text(
            "UPDATE cpsc_ingestion_runs SET state = 'completed', "
            "record_count = :record_count, finished_at = :observed_at "
            "WHERE run_id = :run_id"
        ),
        {
            "observed_at": observed_at,
            "record_count": record_count,
            "run_id": run_id,
        },
    )
    return reused_count


async def import_cpsc_fixture(
    *,
    database_url: str,
    fixture_path: Path,
    observed_at: datetime,
    expected_record_count: int,
    source_url: str = CPSC_RECALL_API_URL,
) -> ImportResult | RejectedImport | FailedImport:
    raw_response = fixture_path.read_bytes()
    run_id = uuid4()
    revision_id = uuid4()
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await _start_revision(
                connection,
                run_id=run_id,
                revision_id=revision_id,
                source_url=source_url,
                observed_at=observed_at,
                raw_response=raw_response,
            )
        try:
            records = parse_source_records(
                raw_response, expected_record_count=expected_record_count
            )
        except SourceValidationError as error:
            async with engine.begin() as connection:
                await _reject_revision(
                    connection,
                    run_id=run_id,
                    revision_id=revision_id,
                    observed_at=observed_at,
                    error_code=str(error),
                )
            return RejectedImport(revision_id=revision_id, error_code=str(error))

        try:
            async with engine.begin() as connection:
                reused_count = await _promote_revision(
                    connection,
                    run_id=run_id,
                    revision_id=revision_id,
                    records=records,
                    observed_at=observed_at,
                )
        except SQLAlchemyError:
            async with engine.begin() as connection:
                await _fail_revision(
                    connection,
                    run_id=run_id,
                    revision_id=revision_id,
                    observed_at=observed_at,
                )
            return FailedImport(revision_id=revision_id)
        return ImportResult(
            revision_id=revision_id,
            record_count=len(records),
            reused_version_count=reused_count,
            content_hashes=tuple(record.content_hash for record in records),
        )
    finally:
        await engine.dispose()


async def cpsc_source_status(database_url: str) -> dict[str, object]:
    engine: AsyncEngine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            current = (
                (
                    await connection.execute(
                        text(
                            "SELECT revisions.revision_id, revisions.completed_at, "
                            "revisions.record_count FROM cpsc_current_source_revision "
                            "AS current JOIN cpsc_source_revisions AS revisions "
                            "ON revisions.revision_id = current.revision_id "
                            "WHERE current.singleton = true"
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            last_run = (
                (
                    await connection.execute(
                        text(
                            "SELECT state, observed_at, record_count "
                            "FROM cpsc_ingestion_runs "
                            "ORDER BY created_at DESC, run_id DESC LIMIT 1"
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
    finally:
        await engine.dispose()

    def instant(value: object) -> str:
        return cast(datetime, value).astimezone(UTC).isoformat()

    return {
        "current_revision": (
            None
            if current is None
            else {
                "completed_at": instant(current["completed_at"]),
                "record_count": current["record_count"],
                "revision_id": str(current["revision_id"]),
            }
        ),
        "last_run": (
            None
            if last_run is None
            else {
                "observed_at": instant(last_run["observed_at"]),
                "record_count": last_run["record_count"],
                "state": last_run["state"],
            }
        ),
    }
