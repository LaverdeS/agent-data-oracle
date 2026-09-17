from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from agent_data_oracle.auth import utc_now
from agent_data_oracle.database import Database

DATA_CATEGORIES = (
    "account_and_declaration",
    "acknowledgements",
    "delegated_key_metadata",
    "released_evaluations",
    "review_reports",
    "submissions",
)
FOUNDER_TEST_RETENTION = timedelta(days=30)
SECURITY_LOG_RETENTION = timedelta(days=14)
DELETION_RESPONSE_TARGET = timedelta(days=7)


@dataclass(frozen=True)
class DeletionRequest:
    due_at: datetime
    requested_at: datetime
    status: str


@dataclass(frozen=True)
class CleanupResult:
    completed_deletions: int
    expired_operators: int


class OperatorDataRights:
    """Own portable exports and bounded removal of operator-linked data."""

    def __init__(
        self, database: Database, *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._database = database
        self._clock = clock

    async def export_operator(self, *, operator_id: UUID) -> dict[str, object] | None:
        async with self._database.connection() as connection:
            account = (
                (
                    await connection.execute(
                        text(
                            "SELECT email_normalized, operator_type, sells_into_us, "
                            "declaration_recorded_at, created_at "
                            "FROM operators WHERE operator_id = :operator_id"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if account is None:
                return None
            submissions = (
                (
                    await connection.execute(
                        text(
                            "SELECT evaluation_id, source_revision_id, evaluated_at, "
                            "normalization_version, matcher_version, outcome "
                            "FROM evidence_evaluations WHERE operator_id = :operator_id "  # noqa: E501
                            "ORDER BY evaluated_at, evaluation_id"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .all()
            )
            inputs = (
                (
                    await connection.execute(
                        text(
                            "SELECT inputs.evaluation_id, inputs.row_position, "
                            "inputs.identifier_type, inputs.submitted_literal, "
                            "inputs.normalized_value, inputs.normalization_version "
                            "FROM evidence_evaluation_inputs AS inputs "
                            "JOIN evidence_evaluations AS evaluations ON "
                            "evaluations.evaluation_id = inputs.evaluation_id "
                            "WHERE evaluations.operator_id = :operator_id "
                            "ORDER BY inputs.evaluation_id, inputs.row_position"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .all()
            )
            released = (
                (
                    await connection.execute(
                        text(
                            "SELECT evaluations.evaluation_id, evaluations.source_revision_id, "  # noqa: E501
                            "evaluations.evaluated_at, evaluations.outcome, "
                            "COALESCE(evaluations.released_at, releases.released_at) "
                            "AS released_at FROM evidence_evaluations AS evaluations "
                            "LEFT JOIN evaluation_releases AS releases ON "
                            "releases.evaluation_id = evaluations.evaluation_id "
                            "WHERE evaluations.operator_id = :operator_id AND "
                            "COALESCE(evaluations.released_at, releases.released_at) IS NOT NULL "  # noqa: E501
                            "ORDER BY released_at, evaluations.evaluation_id"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .all()
            )
            released_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT rows.evaluation_id, rows.evidence_row_id, "
                            "rows.input_position, rows.candidate_class, "
                            "rows.match_bases, rows.affected_product_evidence, "
                            "rows.constraints, rows.recall_number, rows.official_url, "
                            "rows.recall_date_literal, rows.last_publish_date_literal, "
                            "rows.source_observed_at, "
                            "rows.source_revision_completed_at "
                            "FROM evidence_rows AS rows "
                            "JOIN evidence_evaluations AS evaluations ON "
                            "evaluations.evaluation_id = rows.evaluation_id "
                            "LEFT JOIN evaluation_releases AS releases ON "
                            "releases.evaluation_id = evaluations.evaluation_id "
                            "WHERE evaluations.operator_id = :operator_id AND "
                            "COALESCE(evaluations.released_at, releases.released_at) "
                            "IS NOT NULL "
                            "ORDER BY rows.evaluation_id, CASE rows.candidate_class "
                            "WHEN 'exact_identifier_candidate' THEN 0 ELSE 1 END, "
                            "rows.last_publish_date_literal DESC NULLS LAST, "
                            "rows.recall_number, rows.evidence_row_id"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .all()
            )
            acknowledgements = (
                (
                    await connection.execute(
                        text(
                            "SELECT acknowledgement_id, evaluation_id, outcome, "
                            "supersedes_acknowledgement_id, acknowledged_at "
                            "FROM human_review_acknowledgements WHERE operator_id = :operator_id "  # noqa: E501
                            "ORDER BY acknowledged_at, acknowledgement_id"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .all()
            )
            reports = (
                (
                    await connection.execute(
                        text(
                            "SELECT report_id, evaluation_id, agent_key_id, outcome, reported_at "  # noqa: E501
                            "FROM agent_review_reports WHERE operator_id = :operator_id "  # noqa: E501
                            "ORDER BY reported_at, report_id"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .all()
            )
            keys = (
                (
                    await connection.execute(
                        text(
                            "SELECT agent_key_id, secret_prefix, scopes, created_at, "
                            "last_used_at, revoked_at FROM agent_api_keys "
                            "WHERE operator_id = :operator_id ORDER BY created_at, agent_key_id"  # noqa: E501
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .all()
            )
        inputs_by_evaluation: dict[UUID, list[dict[str, Any]]] = {}
        for item in inputs:
            inputs_by_evaluation.setdefault(item["evaluation_id"], []).append(
                dict(item)
            )
        rows_by_evaluation: dict[UUID, list[dict[str, Any]]] = {}
        for item in released_rows:
            row = dict(item)
            evaluation_id = row.pop("evaluation_id")
            rows_by_evaluation.setdefault(evaluation_id, []).append(row)
        return {
            "account": {
                "email": account["email_normalized"],
                "operator_type": account["operator_type"],
                "sells_into_us": account["sells_into_us"],
                "declaration_recorded_at": account["declaration_recorded_at"],
                "created_at": account["created_at"],
            },
            "submissions": [
                {
                    **dict(item),
                    "inputs": inputs_by_evaluation.get(item["evaluation_id"], []),
                }
                for item in submissions
            ],
            "released_evaluations": [
                {
                    **dict(item),
                    "evidence_rows": rows_by_evaluation.get(
                        item["evaluation_id"], []
                    ),
                }
                for item in released
            ],
            "acknowledgements": [dict(item) for item in acknowledgements],
            "review_reports": [dict(item) for item in reports],
            "delegated_keys": [dict(item) for item in keys],
        }

    async def request_deletion(self, *, operator_id: UUID) -> DeletionRequest | None:
        requested_at = self._clock()
        due_at = requested_at + DELETION_RESPONSE_TARGET
        async with self._database.transaction() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM operators WHERE operator_id = :operator_id"),
                {"operator_id": operator_id},
            )
            if exists != 1:
                return None
            row = (
                (
                    await connection.execute(
                        text(
                            "INSERT INTO operator_deletion_work "
                            "(operator_id, requested_at, due_at, status, categories) VALUES "  # noqa: E501
                            "(:operator_id, :requested_at, :due_at, 'pending', :categories) "  # noqa: E501
                            "ON CONFLICT (operator_id) DO UPDATE SET operator_id = "
                            "operator_deletion_work.operator_id "
                            "RETURNING requested_at, due_at, status"
                        ),
                        {
                            "operator_id": operator_id,
                            "requested_at": requested_at,
                            "due_at": due_at,
                            "categories": list(DATA_CATEGORIES),
                        },
                    )
                )
                .mappings()
                .one()
            )
            await connection.execute(
                text(
                    "UPDATE browser_sessions SET revoked_at = :now "
                    "WHERE operator_id = :operator_id AND revoked_at IS NULL"
                ),
                {"now": requested_at, "operator_id": operator_id},
            )
            await connection.execute(
                text(
                    "UPDATE agent_api_keys SET revoked_at = :now "
                    "WHERE operator_id = :operator_id AND revoked_at IS NULL"
                ),
                {"now": requested_at, "operator_id": operator_id},
            )
        return DeletionRequest(
            requested_at=row["requested_at"], due_at=row["due_at"], status=row["status"]
        )

    async def place_incident_hold(
        self, *, operator_id: UUID, reason_category: str, recorded_at: datetime
    ) -> bool:
        if not reason_category.strip():
            raise ValueError("An incident hold reason category is required.")
        async with self._database.transaction() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM operators WHERE operator_id = :operator_id"),
                {"operator_id": operator_id},
            )
            if exists != 1:
                return False
            await connection.execute(
                text(
                    "INSERT INTO operator_retention_holds "
                    "(hold_id, operator_id, reason_category, recorded_at) VALUES "
                    "(:hold_id, :operator_id, :reason_category, :recorded_at)"
                ),
                {
                    "hold_id": uuid4(),
                    "operator_id": operator_id,
                    "reason_category": reason_category.strip(),
                    "recorded_at": recorded_at,
                },
            )
        return True

    async def cleanup(self, *, now: datetime) -> CleanupResult:
        async with self._database.transaction() as connection:
            await connection.execute(
                text("DELETE FROM auth_attempts WHERE attempted_at <= :cutoff"),
                {"cutoff": now - SECURITY_LOG_RETENTION},
            )
            await connection.execute(
                text(
                    "DELETE FROM agent_api_request_attempts WHERE attempted_at <= :cutoff"  # noqa: E501
                ),
                {"cutoff": now - SECURITY_LOG_RETENTION},
            )
            await connection.execute(
                text(
                    "DELETE FROM sign_in_delivery_admissions WHERE admitted_at <= :cutoff"  # noqa: E501
                ),
                {"cutoff": now - SECURITY_LOG_RETENTION},
            )
        async with self._database.connection() as connection:
            pending = (
                (
                    await connection.execute(
                        text(
                            "SELECT work.operator_id, work.due_at FROM operator_deletion_work "  # noqa: E501
                            "AS work WHERE work.status = 'pending' ORDER BY work.requested_at"  # noqa: E501
                        )
                    )
                )
                .mappings()
                .all()
            )
            expired = (
                (
                    await connection.execute(
                        text(
                            "SELECT operators.operator_id FROM operators WHERE created_at <= :cutoff "  # noqa: E501
                            "AND NOT EXISTS (SELECT 1 FROM operator_retention_holds AS holds "  # noqa: E501
                            "WHERE holds.operator_id = operators.operator_id AND "
                            "holds.released_at IS NULL) ORDER BY created_at"
                        ),
                        {"cutoff": now - FOUNDER_TEST_RETENTION},
                    )
                )
                .scalars()
                .all()
            )
        completed = 0
        for row in pending:
            if await self._has_active_hold(row["operator_id"]):
                if row["due_at"] <= now:
                    await self._pause_for_overdue_deletion(row["operator_id"], now)
                continue
            if await self._erase_operator(row["operator_id"], now):
                completed += 1
        expired_count = 0
        for operator_id in expired:
            if await self._erase_operator(operator_id, now):
                expired_count += 1
        await self._expire_refresh_history(now)
        return CleanupResult(completed, expired_count)

    async def _expire_refresh_history(self, now: datetime) -> None:
        async with self._database.transaction() as connection:
            await connection.execute(
                text("SET LOCAL agent_data_oracle.operator_erasure = 'on'")
            )
            await connection.execute(
                text(
                    "DELETE FROM evidence_evaluation_refreshes AS refreshes "
                    "USING evidence_evaluations AS successors "
                    "WHERE refreshes.successor_evaluation_id = successors.evaluation_id "  # noqa: E501
                    "AND successors.evaluated_at <= :cutoff"
                ),
                {"cutoff": now - FOUNDER_TEST_RETENTION},
            )

    async def _has_active_hold(self, operator_id: UUID) -> bool:
        async with self._database.connection() as connection:
            held = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM operator_retention_holds "
                    "WHERE operator_id = :operator_id AND released_at IS NULL)"
                ),
                {"operator_id": operator_id},
            )
        return held is True

    async def _pause_for_overdue_deletion(
        self, operator_id: UUID, now: datetime
    ) -> None:
        async with self._database.transaction() as connection:
            paused = await connection.scalar(
                text("SELECT is_paused FROM global_pause_state WHERE singleton = true")
            )
            if paused is True:
                return
            await connection.execute(
                text(
                    "UPDATE global_pause_state SET is_paused = true, "
                    "trigger_kind = 'deletion_response_deadline', "
                    "reason_category = 'deletion_work_overdue', activated_at = :now, "
                    "activated_by = NULL WHERE singleton = true"
                ),
                {"now": now, "operator_id": operator_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO global_pause_events "
                    "(event_id, action, trigger_kind, reason_category, actor_id, occurred_at) "  # noqa: E501
                    "VALUES (:event_id, 'activated', 'deletion_response_deadline', "
                    "'deletion_work_overdue', NULL, :now)"
                ),
                {"event_id": uuid4(), "now": now},
            )

    async def _erase_operator(self, operator_id: UUID, completed_at: datetime) -> bool:
        async with self._database.transaction() as connection:
            work = (
                (
                    await connection.execute(
                        text(
                            "SELECT categories FROM operator_deletion_work "
                            "WHERE operator_id = :operator_id"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            operator = (
                (
                    await connection.execute(
                        text(
                            "SELECT email_normalized FROM operators "
                            "WHERE operator_id = :operator_id FOR UPDATE"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if operator is None:
                return False
            await connection.execute(
                text("SET LOCAL agent_data_oracle.operator_erasure = 'on'")
            )
            evaluation_ids = (
                (
                    await connection.execute(
                        text(
                            "SELECT evaluation_id FROM evidence_evaluations "
                            "WHERE operator_id = :operator_id"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .scalars()
                .all()
            )
            key_ids = (
                (
                    await connection.execute(
                        text(
                            "SELECT agent_key_id FROM agent_api_keys "
                            "WHERE operator_id = :operator_id"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .scalars()
                .all()
            )
            parameters = {
                "operator_id": operator_id,
                "evaluation_ids": evaluation_ids,
                "key_ids": key_ids,
            }
            await connection.execute(
                text(
                    "DELETE FROM source_evidence_retrievals "
                    "WHERE operator_id = :operator_id"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM agent_review_reports WHERE operator_id = :operator_id"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM human_review_acknowledgements "
                    "WHERE operator_id = :operator_id"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM evidence_evaluation_refreshes "
                    "WHERE successor_evaluation_id = ANY(:evaluation_ids) "
                    "OR predecessor_evaluation_id = ANY(:evaluation_ids)"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM evaluation_audits WHERE founder_id = :operator_id "
                    "OR evaluation_id = ANY(:evaluation_ids)"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM evaluation_releases "
                    "WHERE evaluation_id = ANY(:evaluation_ids)"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM evidence_rows WHERE evaluation_id = ANY(:evaluation_ids)"  # noqa: E501
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM evidence_evaluation_inputs "
                    "WHERE evaluation_id = ANY(:evaluation_ids)"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM evidence_evaluations "
                    "WHERE evaluation_id = ANY(:evaluation_ids)"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM agent_api_request_attempts "
                    "WHERE agent_key_id = ANY(:key_ids)"
                ),
                parameters,
            )
            await connection.execute(
                text("DELETE FROM agent_api_keys WHERE operator_id = :operator_id"),
                parameters,
            )
            await connection.execute(
                text("DELETE FROM browser_sessions WHERE operator_id = :operator_id"),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM auth_recovery_codes WHERE operator_id = :operator_id"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM founder_totp_factors WHERE operator_id = :operator_id"
                ),
                parameters,
            )
            await connection.execute(
                text("DELETE FROM login_tokens WHERE email_normalized = :email"),
                {"email": operator["email_normalized"]},
            )
            await connection.execute(
                text(
                    "DELETE FROM operator_retention_holds WHERE operator_id = :operator_id"  # noqa: E501
                ),
                parameters,
            )
            await connection.execute(
                text("DELETE FROM global_pause_events WHERE actor_id = :operator_id"),
                parameters,
            )
            await connection.execute(
                text(
                    "UPDATE global_pause_state SET activated_by = NULL "
                    "WHERE activated_by = :operator_id"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "UPDATE global_pause_state SET resolved_by = NULL "
                    "WHERE resolved_by = :operator_id"
                ),
                parameters,
            )
            await connection.execute(
                text(
                    "DELETE FROM operator_deletion_work WHERE operator_id = :operator_id"  # noqa: E501
                ),
                parameters,
            )
            await connection.execute(
                text("DELETE FROM operators WHERE operator_id = :operator_id"),
                parameters,
            )
            await connection.execute(
                text(
                    "INSERT INTO deletion_completion_receipts "
                    "(receipt_id, completed_at, outcome, categories) VALUES "
                    "(:receipt_id, :completed_at, 'completed', :categories)"
                ),
                {
                    "receipt_id": uuid4(),
                    "completed_at": completed_at,
                    "categories": list(
                        DATA_CATEGORIES if work is None else work["categories"]
                    ),
                },
            )
        return True
