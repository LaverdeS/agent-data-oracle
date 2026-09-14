import hashlib
import json
import random
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

CPSC_RECALL_API_URL = "https://www.saferproducts.gov/RestWebServices/Recall"
RETRIEVAL_TIMEOUT_SECONDS = 20.0
MAX_RETRIEVAL_ATTEMPTS = 3


class CpscRefreshMode(StrEnum):
    FIXTURE = "fixture"
    DAILY = "daily"
    FULL = "full"


class CpscRetrievalError(RuntimeError):
    """A CPSC response could not be safely retrieved."""

    def __init__(self, error_code: str, *, transient: bool) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.transient = transient


class SourceValidationError(ValueError):
    """The received source body cannot become a completed revision."""


class RevisionState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


def _rfc3339(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def build_cpsc_recall_url(
    mode: CpscRefreshMode, *, last_completed_at: datetime | None = None
) -> str:
    """Build the sole permitted live-source URL for a bounded refresh."""
    parameters: dict[str, str] = {"format": "json"}
    if mode is CpscRefreshMode.DAILY:
        if last_completed_at is None:
            raise ValueError("daily refresh requires the last completed source time")
        parameters = {
            "LastPublishDateStart": _rfc3339(last_completed_at - timedelta(days=7)),
            "format": "json",
        }
    return f"{CPSC_RECALL_API_URL}?{urlencode(parameters)}"


def _is_documented_cpsc_api_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.saferproducts.gov"
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/RestWebServices/Recall"
    )


def _http_fetch(url: str, timeout_seconds: float) -> bytes:
    if not _is_documented_cpsc_api_url(url):
        raise CpscRetrievalError("source_endpoint_invalid", transient=False)
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if not _is_documented_cpsc_api_url(response.url):
                raise CpscRetrievalError("source_redirect_invalid", transient=False)
            if response.status != 200:
                raise CpscRetrievalError(
                    f"http_{response.status}", transient=response.status >= 500
                )
            body = cast(bytes, response.read())
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None and declared_length != str(len(body)):
                raise CpscRetrievalError("transport_truncated", transient=True)
            return body
    except HTTPError as error:
        raise CpscRetrievalError(
            f"http_{error.code}", transient=error.code >= 500
        ) from error
    except (TimeoutError, URLError) as error:
        raise CpscRetrievalError("transport_failed", transient=True) from error


async def retrieve_cpsc_response(
    url: str,
    *,
    fetch: Callable[[str, float], bytes] = _http_fetch,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> bytes:
    """Retrieve one bounded response, with no more than three total attempts."""
    for attempt in range(MAX_RETRIEVAL_ATTEMPTS):
        try:
            return fetch(url, RETRIEVAL_TIMEOUT_SECONDS)
        except CpscRetrievalError as error:
            if not error.transient or attempt == MAX_RETRIEVAL_ATTEMPTS - 1:
                raise
        except TimeoutError as error:
            if attempt == MAX_RETRIEVAL_ATTEMPTS - 1:
                raise CpscRetrievalError("transport_failed", transient=True) from error
        sleep((2.0**attempt) + jitter())
    raise AssertionError("unreachable retrieval retry state")


@dataclass(frozen=True)
class RevisionIdentity:
    run_id: UUID
    revision_id: UUID


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
            "state": RevisionState.COMPLETED,
        }


@dataclass(frozen=True)
class RejectedImport:
    revision_id: UUID
    error_code: str

    def as_dict(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "revision_id": str(self.revision_id),
            "state": RevisionState.REJECTED,
        }


@dataclass(frozen=True)
class FailedImport:
    revision_id: UUID
    error_code: str = "promotion_failed"

    def as_dict(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "revision_id": str(self.revision_id),
            "state": RevisionState.FAILED,
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
    raw_response: bytes, *, expected_record_count: int | None
) -> tuple[CpscRecord, ...]:
    try:
        document = json.loads(raw_response)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceValidationError("invalid_json") from error
    if not isinstance(document, list):
        raise SourceValidationError("expected_record_array")
    if expected_record_count is not None and len(document) != expected_record_count:
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

        official_url = _required_text(record, "URL")
        parsed_url = urlsplit(official_url)
        if parsed_url.scheme != "https" or parsed_url.hostname not in {
            "www.cpsc.gov",
            "cpsc.gov",
        }:
            raise SourceValidationError("official_notice_provenance_invalid")

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
                official_url=official_url,
                raw_record=record,
                canonical_json=canonical_json,
                content_hash=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(records)


async def _start_revision(
    connection: AsyncConnection,
    *,
    identity: RevisionIdentity,
    source_url: str,
    observed_at: datetime,
    raw_response: bytes,
    refresh_mode: CpscRefreshMode,
    retrieval_attempts: int = 0,
) -> None:
    await connection.execute(
        text(
            "INSERT INTO cpsc_ingestion_runs "
            "(run_id, source_url, observed_at, raw_response, raw_response_sha256, "
            "state, refresh_mode, retrieval_attempts, created_at) VALUES "
            "(:run_id, :source_url, :observed_at, :raw_response, :raw_hash, "
            "'pending', :refresh_mode, :retrieval_attempts, CURRENT_TIMESTAMP)"
        ),
        {
            "observed_at": observed_at,
            "raw_hash": hashlib.sha256(raw_response).hexdigest(),
            "raw_response": raw_response,
            "refresh_mode": refresh_mode.value,
            "retrieval_attempts": retrieval_attempts,
            "run_id": identity.run_id,
            "source_url": source_url,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO cpsc_source_revisions "
            "(revision_id, run_id, state, completeness, created_at) VALUES "
            "(:revision_id, :run_id, 'pending', 'unknown', CURRENT_TIMESTAMP)"
        ),
        {
            "revision_id": identity.revision_id,
            "run_id": identity.run_id,
        },
    )


async def _finish_unsuccessful_revision(
    connection: AsyncConnection,
    *,
    identity: RevisionIdentity,
    state: RevisionState,
    error_code: str,
) -> None:
    if state not in (RevisionState.FAILED, RevisionState.REJECTED):
        raise ValueError("unsuccessful revision requires a terminal failure state")
    await connection.execute(
        text(
            "UPDATE cpsc_ingestion_runs SET state = :state, "
            "error_code = :error_code, finished_at = CURRENT_TIMESTAMP "
            "WHERE run_id = :run_id"
        ),
        {
            "error_code": error_code,
            "run_id": identity.run_id,
            "state": state.value,
        },
    )
    await connection.execute(
        text(
            "UPDATE cpsc_source_revisions SET state = :state, "
            "completeness = 'partial' WHERE revision_id = :revision_id"
        ),
        {"revision_id": identity.revision_id, "state": state.value},
    )


async def _activate_source_integrity_pause(
    connection: AsyncConnection, *, error_code: str
) -> None:
    await connection.execute(
        text(
            "INSERT INTO global_pause_state "
            "(singleton, is_paused, trigger_kind, reason_category, activated_at) "
            "VALUES (true, true, 'source_integrity', :error_code, CURRENT_TIMESTAMP) "
            "ON CONFLICT (singleton) DO UPDATE SET is_paused = true, "
            "trigger_kind = 'source_integrity', reason_category = :error_code, "
            "activated_at = CURRENT_TIMESTAMP, resolution_note = NULL, "
            "resolved_at = NULL, resolved_by = NULL "
            "WHERE global_pause_state.is_paused = false"
        ),
        {"error_code": error_code},
    )
    await connection.execute(
        text(
            "UPDATE cpsc_source_refresh_state SET "
            "integrity_pause_reason = :error_code, last_failure_kind = :error_code, "
            "updated_at = CURRENT_TIMESTAMP WHERE singleton = true"
        ),
        {"error_code": error_code},
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
            ":last_publish_date, :official_url, CURRENT_TIMESTAMP) "
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
    identity: RevisionIdentity,
    records: tuple[CpscRecord, ...],
    observed_at: datetime,
    refresh_mode: CpscRefreshMode,
) -> tuple[int, int]:
    if refresh_mode is CpscRefreshMode.DAILY:
        current_records = (
            await connection.execute(
                text(
                    "SELECT recall_id, normalized_record FROM cpsc_current_records "
                    "ORDER BY recall_id"
                )
            )
        ).mappings()
        returned_recall_ids = {record.recall_id for record in records}
        retained_raw_records = [
            row["normalized_record"]
            for row in current_records
            if row["recall_id"] not in returned_recall_ids
        ]
        if retained_raw_records:
            retained_records = parse_source_records(
                json.dumps(retained_raw_records).encode(), expected_record_count=None
            )
            records = (*records, *retained_records)

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
                "revision_id": identity.revision_id,
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
                "revision_id": identity.revision_id,
                "run_id": identity.run_id,
                "version_id": version_id,
            },
        )

    record_count = len(records)
    if refresh_mode is CpscRefreshMode.FULL:
        current_rows = (
            await connection.execute(
                text(
                    "SELECT recall_id, revision_id FROM cpsc_current_records "
                    "ORDER BY recall_id"
                )
            )
        ).mappings()
        received_recall_ids = {record.recall_id for record in records}
        for current in current_rows:
            if current["recall_id"] not in received_recall_ids:
                await connection.execute(
                    text(
                        "INSERT INTO cpsc_source_tombstones "
                        "(tombstone_id, revision_id, recall_id, "
                        "last_seen_revision_id, recorded_at) VALUES "
                        "(:tombstone_id, :revision_id, :recall_id, "
                        ":last_seen_revision_id, :recorded_at)"
                    ),
                    {
                        "last_seen_revision_id": current["revision_id"],
                        "recall_id": current["recall_id"],
                        "recorded_at": observed_at,
                        "revision_id": identity.revision_id,
                        "tombstone_id": uuid4(),
                    },
                )
    await connection.execute(
        text(
            "UPDATE cpsc_source_revisions SET state = 'completed', "
            "completeness = 'complete', record_count = :record_count, "
            "completed_at = CURRENT_TIMESTAMP WHERE revision_id = :revision_id"
        ),
        {
            "record_count": record_count,
            "revision_id": identity.revision_id,
        },
    )
    await connection.execute(
        text(
            "UPDATE cpsc_source_refresh_state SET "
            "last_successful_observed_at = :observed_at, last_failure_kind = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE singleton = true"
        ),
        {"observed_at": observed_at},
    )
    await connection.execute(
        text(
            "INSERT INTO cpsc_current_source_revision "
            "(singleton, revision_id, projected_at) "
            "VALUES (true, :revision_id, CURRENT_TIMESTAMP) "
            "ON CONFLICT (singleton) DO UPDATE SET "
            "revision_id = EXCLUDED.revision_id, "
            "projected_at = EXCLUDED.projected_at"
        ),
        {"revision_id": identity.revision_id},
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
            "versions.raw_record, CURRENT_TIMESTAMP "
            "FROM cpsc_revision_records AS records "
            "JOIN cpsc_recall_versions AS versions "
            "ON versions.version_id = records.version_id "
            "WHERE records.revision_id = :revision_id"
        ),
        {"revision_id": identity.revision_id},
    )
    await connection.execute(
        text(
            "UPDATE cpsc_ingestion_runs SET state = 'completed', "
            "record_count = :record_count, finished_at = CURRENT_TIMESTAMP "
            "WHERE run_id = :run_id"
        ),
        {
            "record_count": record_count,
            "run_id": identity.run_id,
        },
    )
    return reused_count, record_count


async def import_cpsc_fixture(
    *,
    database_url: str,
    fixture_path: Path,
    observed_at: datetime,
    expected_record_count: int,
    source_url: str = CPSC_RECALL_API_URL,
) -> ImportResult | RejectedImport | FailedImport:
    raw_response = fixture_path.read_bytes()
    identity = RevisionIdentity(run_id=uuid4(), revision_id=uuid4())
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await _start_revision(
                connection,
                identity=identity,
                source_url=source_url,
                observed_at=observed_at,
                raw_response=raw_response,
                refresh_mode=CpscRefreshMode.FIXTURE,
            )
        try:
            records = parse_source_records(
                raw_response, expected_record_count=expected_record_count
            )
        except SourceValidationError as error:
            async with engine.begin() as connection:
                await _finish_unsuccessful_revision(
                    connection,
                    identity=identity,
                    state=RevisionState.REJECTED,
                    error_code=str(error),
                )
            return RejectedImport(
                revision_id=identity.revision_id, error_code=str(error)
            )

        try:
            async with engine.begin() as connection:
                reused_count, record_count = await _promote_revision(
                    connection,
                    identity=identity,
                    records=records,
                    observed_at=observed_at,
                    refresh_mode=CpscRefreshMode.FIXTURE,
                )
        except SQLAlchemyError:
            async with engine.begin() as connection:
                await _finish_unsuccessful_revision(
                    connection,
                    identity=identity,
                    state=RevisionState.FAILED,
                    error_code="promotion_failed",
                )
            return FailedImport(revision_id=identity.revision_id)
        return ImportResult(
            revision_id=identity.revision_id,
            record_count=record_count,
            reused_version_count=reused_count,
            content_hashes=tuple(record.content_hash for record in records),
        )
    finally:
        await engine.dispose()


async def import_cpsc_refresh_response(
    *,
    database_url: str,
    raw_response: bytes,
    observed_at: datetime,
    expected_record_count: int | None,
    source_url: str,
    refresh_mode: CpscRefreshMode,
    retrieval_attempts: int,
) -> ImportResult | RejectedImport | FailedImport:
    """Validate and atomically promote a live CPSC response.

    The caller owns transport and supplies the exact received bytes.  No partial
    parsing result is ever passed to promotion.
    """
    if refresh_mode not in {CpscRefreshMode.DAILY, CpscRefreshMode.FULL}:
        raise ValueError("a refresh response must be daily or full")
    identity = RevisionIdentity(run_id=uuid4(), revision_id=uuid4())
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await _start_revision(
                connection,
                identity=identity,
                source_url=source_url,
                observed_at=observed_at,
                raw_response=raw_response,
                refresh_mode=refresh_mode,
                retrieval_attempts=retrieval_attempts,
            )
        try:
            records = parse_source_records(
                raw_response, expected_record_count=expected_record_count
            )
        except SourceValidationError as error:
            async with engine.begin() as connection:
                await _finish_unsuccessful_revision(
                    connection,
                    identity=identity,
                    state=RevisionState.REJECTED,
                    error_code=str(error),
                )
                await connection.execute(
                    text(
                        "UPDATE cpsc_source_refresh_state SET "
                        "last_failure_kind = :error_code, "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE singleton = true"
                    ),
                    {"error_code": str(error)},
                )
                await _activate_source_integrity_pause(
                    connection, error_code=str(error)
                )
            return RejectedImport(
                revision_id=identity.revision_id, error_code=str(error)
            )
        try:
            async with engine.begin() as connection:
                reused_count, record_count = await _promote_revision(
                    connection,
                    identity=identity,
                    records=records,
                    observed_at=observed_at,
                    refresh_mode=refresh_mode,
                )
        except SQLAlchemyError:
            async with engine.begin() as connection:
                await _finish_unsuccessful_revision(
                    connection,
                    identity=identity,
                    state=RevisionState.FAILED,
                    error_code="promotion_failed",
                )
                await connection.execute(
                    text(
                        "UPDATE cpsc_source_refresh_state SET "
                        "last_failure_kind = 'promotion_failed', "
                        "updated_at = CURRENT_TIMESTAMP WHERE singleton = true"
                    )
                )
            return FailedImport(revision_id=identity.revision_id)
        return ImportResult(
            revision_id=identity.revision_id,
            record_count=record_count,
            reused_version_count=reused_count,
            content_hashes=tuple(record.content_hash for record in records),
        )
    finally:
        await engine.dispose()


async def _last_completed_source_observed_at(database_url: str) -> datetime | None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            observed_at = await connection.scalar(
                text(
                    "SELECT runs.observed_at "
                    "FROM cpsc_current_source_revision AS current "
                    "JOIN cpsc_source_revisions AS revisions "
                    "ON revisions.revision_id = current.revision_id "
                    "JOIN cpsc_ingestion_runs AS runs "
                    "ON runs.run_id = revisions.run_id "
                    "WHERE current.singleton = true"
                )
            )
    finally:
        await engine.dispose()
    return observed_at if isinstance(observed_at, datetime) else None


@asynccontextmanager
async def _source_refresh_lock(database_url: str) -> AsyncIterator[None]:
    """Serialize the complete live-refresh workflow across processes."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_advisory_lock(hashtextextended('cpsc-refresh', 0))")
            )
            try:
                yield
            finally:
                await connection.execute(
                    text(
                        "SELECT pg_advisory_unlock(hashtextextended('cpsc-refresh', 0))"
                    )
                )
    finally:
        await engine.dispose()


async def _record_transport_failure(
    *,
    database_url: str,
    source_url: str,
    observed_at: datetime,
    refresh_mode: CpscRefreshMode,
    error_code: str,
) -> FailedImport:
    identity = RevisionIdentity(run_id=uuid4(), revision_id=uuid4())
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await _start_revision(
                connection,
                identity=identity,
                source_url=source_url,
                observed_at=observed_at,
                raw_response=b"",
                refresh_mode=refresh_mode,
                retrieval_attempts=MAX_RETRIEVAL_ATTEMPTS,
            )
            await _finish_unsuccessful_revision(
                connection,
                identity=identity,
                state=RevisionState.FAILED,
                error_code=error_code,
            )
            await connection.execute(
                text(
                    "UPDATE cpsc_source_refresh_state SET "
                    "last_failure_kind = :error_code, updated_at = CURRENT_TIMESTAMP "
                    "WHERE singleton = true"
                ),
                {"error_code": error_code},
            )
    finally:
        await engine.dispose()
    return FailedImport(revision_id=identity.revision_id, error_code=error_code)


async def refresh_cpsc_source(
    *,
    database_url: str,
    refresh_mode: CpscRefreshMode,
    observed_at: datetime,
    expected_record_count: int | None = None,
    fetch: Callable[[str, float], bytes] = _http_fetch,
) -> ImportResult | RejectedImport | FailedImport:
    """Run the bounded live-source path; fixtures use the separate importer."""
    if refresh_mode not in {CpscRefreshMode.DAILY, CpscRefreshMode.FULL}:
        raise ValueError("refresh mode must be daily or full")
    async with _source_refresh_lock(database_url):
        last_completed_at = await _last_completed_source_observed_at(database_url)
        try:
            source_url = build_cpsc_recall_url(
                refresh_mode,
                last_completed_at=last_completed_at,
            )
        except ValueError:
            return await _record_transport_failure(
                database_url=database_url,
                source_url=CPSC_RECALL_API_URL,
                observed_at=observed_at,
                refresh_mode=refresh_mode,
                error_code="completed_revision_required",
            )
        if (
            refresh_mode is CpscRefreshMode.DAILY
            and last_completed_at is not None
            and observed_at - last_completed_at > timedelta(hours=48)
        ):
            return await _record_transport_failure(
                database_url=database_url,
                source_url=source_url,
                observed_at=observed_at,
                refresh_mode=refresh_mode,
                error_code="stale_source_requires_weekly_reconciliation",
            )
        try:
            raw_response = await retrieve_cpsc_response(source_url, fetch=fetch)
        except CpscRetrievalError as error:
            return await _record_transport_failure(
                database_url=database_url,
                source_url=source_url,
                observed_at=observed_at,
                refresh_mode=refresh_mode,
                error_code=error.error_code,
            )
        return await import_cpsc_refresh_response(
            database_url=database_url,
            raw_response=raw_response,
            observed_at=observed_at,
            expected_record_count=expected_record_count,
            source_url=source_url,
            refresh_mode=refresh_mode,
            retrieval_attempts=1,
        )


async def cpsc_source_status(
    database_url: str, *, now: datetime | None = None
) -> dict[str, object]:
    engine: AsyncEngine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            current = (
                (
                    await connection.execute(
                        text(
                            "SELECT revisions.revision_id, revisions.completed_at, "
                            "revisions.record_count, runs.observed_at "
                            "FROM cpsc_current_source_revision "
                            "AS current JOIN cpsc_source_revisions AS revisions "
                            "ON revisions.revision_id = current.revision_id "
                            "JOIN cpsc_ingestion_runs AS runs "
                            "ON runs.run_id = revisions.run_id "
                            "WHERE current.singleton = true"
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            refresh_state = (
                (
                    await connection.execute(
                        text(
                            "SELECT last_failure_kind, integrity_pause_reason "
                            "FROM cpsc_source_refresh_state WHERE singleton = true"
                        )
                    )
                )
                .mappings()
                .one()
            )
            last_run = (
                (
                    await connection.execute(
                        text(
                            "SELECT state, observed_at, record_count "
                            "FROM cpsc_ingestion_runs "
                            "ORDER BY run_sequence DESC LIMIT 1"
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

    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    if current is None:
        availability = "unavailable"
        freshness = "unavailable"
    elif refresh_state["integrity_pause_reason"] is not None:
        availability = "integrity_paused"
        freshness = "unavailable"
    else:
        observed_at = cast(datetime, current["observed_at"])
        is_within_grace = current_time - observed_at <= timedelta(hours=48)
        freshness = "current" if is_within_grace else "stale"
        last_run_succeeded = last_run is not None and last_run["state"] == "completed"
        availability = (
            "ready"
            if is_within_grace and last_run_succeeded
            else "transient_failure"
            if is_within_grace
            else "disabled"
        )

    return {
        "current_revision": (
            None
            if current is None
            else {
                "completed_at": instant(current["completed_at"]),
                "record_count": current["record_count"],
                "revision_id": str(current["revision_id"]),
                "observed_at": instant(current["observed_at"]),
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
        "source_status": {
            "application_readiness": availability,
            "freshness": freshness,
            "last_failure_kind": refresh_state["last_failure_kind"],
            "integrity_pause_reason": refresh_state["integrity_pause_reason"],
        },
    }
