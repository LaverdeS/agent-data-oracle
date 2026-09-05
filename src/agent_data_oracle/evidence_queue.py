import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import text

from agent_data_oracle.auth import utc_now
from agent_data_oracle.database import Database

NORMALIZATION_VERSION = "identifier-v1"
MATCHER_VERSION = "no-candidate-v1"
MAX_IDENTIFIER_ROWS = 50
MAX_IDENTIFIER_LITERAL_LENGTH = 80
_BRAND_LITERAL = re.compile(r"[\w][\w'&+\-]{0,31}( [\w][\w'&+\-]{0,31}){0,2}")
_MODEL_LITERAL = re.compile(
    r"(?:Model )?(?:[A-Z0-9]+(?:[-_./][A-Z0-9]+)+|[A-Z]+[0-9]+)"
)
NO_CANDIDATE_LIMITATIONS = (
    "No candidate recall-to-listing action records were found for these "
    "submitted identifiers at the recorded evaluation time.",
    "No-candidate results do not prove that the CPSC source is complete, "
    "or that a product is safe, recalled, compliant, legal, removable, "
    "cleared, or permitted for sale.",
    "CPSC/U.S. consumer-product recall data only; not legal advice or a safety "
    "assessment; CPSC does not endorse this service.",
)


class SubmissionError(ValueError):
    """A browser submission is outside the bounded evidence-queue contract."""


class IdempotencyConflictError(ValueError):
    """An idempotency key was previously used for different input."""


class SourceUnavailableError(RuntimeError):
    """No completed CPSC source revision is available for evaluation."""


class IdentifierType(StrEnum):
    UPC = "upc"
    MODEL = "model"
    BRAND = "brand"


@dataclass(frozen=True)
class SubmittedIdentifier:
    identifier_type: IdentifierType
    submitted_literal: str
    normalized_value: str


@dataclass(frozen=True)
class EvaluationSummary:
    evaluation_id: UUID
    evaluated_at: datetime


@dataclass(frozen=True)
class EvidenceQueueContract:
    evaluation_id: UUID
    source_revision_id: UUID
    evaluated_at: datetime
    normalization_version: str
    matcher_version: str
    inputs: tuple[SubmittedIdentifier, ...]


def serialize_evidence_contract(contract: EvidenceQueueContract) -> dict[str, object]:
    """Build the mandatory evidence contract shared by HTML and future REST."""
    return {
        "evaluated_at": contract.evaluated_at.isoformat(),
        "inputs": [
            {
                "literal": identifier.submitted_literal,
                "type": identifier.identifier_type,
            }
            for identifier in contract.inputs
        ],
        "limitations": NO_CANDIDATE_LIMITATIONS,
        "matcher_version": contract.matcher_version,
        "normalization_version": contract.normalization_version,
        "outcome": "no_candidates",
        "source_revision_id": str(contract.source_revision_id),
    }


def submitted_identifiers_from_form(
    form: dict[str, list[str]], *, body_is_within_limit: bool
) -> tuple[SubmittedIdentifier, ...]:
    allowed_fields = {
        "authorization",
        "csrf_token",
        "idempotency_key",
        "identifier_type",
        "identifier_value",
    }
    single_value_fields = {"authorization", "csrf_token", "idempotency_key"}
    if (
        not body_is_within_limit
        or set(form) - allowed_fields
        or any(len(form.get(field, [])) != 1 for field in single_value_fields)
    ):
        raise SubmissionError("Submit only explicit UPC, model, or brand rows.")
    if form["authorization"] != ["authorized"]:
        raise SubmissionError("Confirm that you are authorized to submit these values.")
    types = form.get("identifier_type", [])
    values = form.get("identifier_value", [])
    if len(types) != len(values) or len(types) > MAX_IDENTIFIER_ROWS:
        raise SubmissionError("Submit between one and 50 typed identifier rows.")

    identifiers: list[SubmittedIdentifier] = []
    for identifier_type, submitted_literal in zip(types, values, strict=True):
        if not submitted_literal:
            continue
        if (
            submitted_literal.strip()
            .casefold()
            .startswith(("http://", "https://", "www."))
        ):
            raise SubmissionError("Submit only explicit UPC, model, or brand rows.")
        try:
            typed_identifier = IdentifierType(identifier_type)
        except ValueError as error:
            raise SubmissionError(
                "Submit only explicit UPC, model, or brand rows."
            ) from error
        if len(submitted_literal) > MAX_IDENTIFIER_LITERAL_LENGTH:
            raise SubmissionError(
                "Each submitted identifier must be no more than 80 characters."
            )
        if typed_identifier is IdentifierType.UPC:
            compact_upc = submitted_literal.replace(" ", "").replace("-", "")
            is_valid_literal = compact_upc.isascii() and compact_upc.isdecimal()
        elif typed_identifier is IdentifierType.MODEL:
            is_valid_literal = _MODEL_LITERAL.fullmatch(submitted_literal) is not None
        else:
            is_valid_literal = _BRAND_LITERAL.fullmatch(submitted_literal) is not None
        if not is_valid_literal:
            raise SubmissionError("Submit only explicit UPC, model, or brand rows.")
        identifiers.append(
            SubmittedIdentifier(
                identifier_type=typed_identifier,
                submitted_literal=submitted_literal,
                normalized_value=submitted_literal.strip().casefold(),
            )
        )
    if not identifiers:
        raise SubmissionError("Submit between one and 50 typed identifier rows.")
    return tuple(identifiers)


def _submission_hash(identifiers: tuple[SubmittedIdentifier, ...]) -> str:
    canonical = [
        {
            "identifier_type": identifier.identifier_type,
            "submitted_literal": identifier.submitted_literal,
            "normalized_value": identifier.normalized_value,
        }
        for identifier in identifiers
    ]
    return hashlib.sha256(
        json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


class EvidenceQueues:
    """Create and retrieve the authoritative immutable evidence contract."""

    def __init__(
        self, database: Database, *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._database = database
        self._clock = clock

    async def submit_no_candidate_evaluation(
        self,
        *,
        operator_id: UUID,
        idempotency_key: str,
        identifiers: tuple[SubmittedIdentifier, ...],
    ) -> EvaluationSummary:
        if not 16 <= len(idempotency_key) <= 128:
            raise SubmissionError("A valid submission token is required.")
        canonical_submission_hash = _submission_hash(identifiers)
        evaluated_at = self._clock()
        async with self._database.transaction() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"evidence-idempotency:{operator_id}:{idempotency_key}"},
            )
            previous = (
                (
                    await connection.execute(
                        text(
                            "SELECT evaluation_id, canonical_submission_hash, "
                            "evaluated_at FROM evidence_evaluations "
                            "WHERE operator_id = :operator_id "
                            "AND idempotency_key = :idempotency_key"
                        ),
                        {
                            "idempotency_key": idempotency_key,
                            "operator_id": operator_id,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if previous is not None:
                if previous["canonical_submission_hash"] != canonical_submission_hash:
                    raise IdempotencyConflictError("submission token already used")
                return EvaluationSummary(
                    evaluation_id=previous["evaluation_id"],
                    evaluated_at=previous["evaluated_at"],
                )

            source_revision_id = await connection.scalar(
                text(
                    "SELECT current.revision_id "
                    "FROM cpsc_current_source_revision AS current "
                    "JOIN cpsc_source_revisions AS revisions "
                    "ON revisions.revision_id = current.revision_id "
                    "WHERE revisions.state = 'completed'"
                )
            )
            if not isinstance(source_revision_id, UUID):
                raise SourceUnavailableError("completed CPSC revision unavailable")
            evaluation_id = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO evidence_evaluations "
                    "(evaluation_id, operator_id, source_revision_id, evaluated_at, "
                    "normalization_version, matcher_version, outcome, released_at, "
                    "idempotency_key, canonical_submission_hash) VALUES "
                    "(:evaluation_id, :operator_id, :source_revision_id, "
                    ":evaluated_at, "
                    ":normalization_version, :matcher_version, 'no_candidates', "
                    ":released_at, :idempotency_key, :canonical_submission_hash)"
                ),
                {
                    "canonical_submission_hash": canonical_submission_hash,
                    "evaluated_at": evaluated_at,
                    "evaluation_id": evaluation_id,
                    "idempotency_key": idempotency_key,
                    "matcher_version": MATCHER_VERSION,
                    "normalization_version": NORMALIZATION_VERSION,
                    "operator_id": operator_id,
                    "released_at": evaluated_at,
                    "source_revision_id": source_revision_id,
                },
            )
            for row_position, identifier in enumerate(identifiers):
                await connection.execute(
                    text(
                        "INSERT INTO evidence_evaluation_inputs "
                        "(evaluation_id, row_position, identifier_type, "
                        "submitted_literal, "
                        "normalized_value, normalization_version) VALUES "
                        "(:evaluation_id, :row_position, :identifier_type, "
                        ":submitted_literal, :normalized_value, :normalization_version)"
                    ),
                    {
                        "evaluation_id": evaluation_id,
                        "identifier_type": identifier.identifier_type,
                        "normalization_version": NORMALIZATION_VERSION,
                        "normalized_value": identifier.normalized_value,
                        "row_position": row_position,
                        "submitted_literal": identifier.submitted_literal,
                    },
                )
        return EvaluationSummary(evaluation_id=evaluation_id, evaluated_at=evaluated_at)

    async def list_released(
        self, *, operator_id: UUID
    ) -> tuple[EvaluationSummary, ...]:
        async with self._database.connection() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT evaluation_id, evaluated_at "
                            "FROM evidence_evaluations "
                            "WHERE operator_id = :operator_id "
                            "AND outcome = 'no_candidates' "
                            "ORDER BY released_at DESC"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            EvaluationSummary(
                evaluation_id=row["evaluation_id"], evaluated_at=row["evaluated_at"]
            )
            for row in rows
        )

    async def released_contract(
        self, *, operator_id: UUID, evaluation_id: UUID
    ) -> EvidenceQueueContract | None:
        async with self._database.connection() as connection:
            evaluation = (
                (
                    await connection.execute(
                        text(
                            "SELECT evaluation_id, source_revision_id, evaluated_at, "
                            "normalization_version, matcher_version "
                            "FROM evidence_evaluations "
                            "WHERE evaluation_id = :evaluation_id "
                            "AND operator_id = :operator_id "
                            "AND outcome = 'no_candidates'"
                        ),
                        {"evaluation_id": evaluation_id, "operator_id": operator_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if evaluation is None:
                return None
            inputs = (
                (
                    await connection.execute(
                        text(
                            "SELECT identifier_type, submitted_literal, "
                            "normalized_value "
                            "FROM evidence_evaluation_inputs "
                            "WHERE evaluation_id = :evaluation_id "
                            "ORDER BY row_position"
                        ),
                        {"evaluation_id": evaluation_id},
                    )
                )
                .mappings()
                .all()
            )
        return EvidenceQueueContract(
            evaluation_id=evaluation["evaluation_id"],
            source_revision_id=evaluation["source_revision_id"],
            evaluated_at=evaluation["evaluated_at"],
            normalization_version=evaluation["normalization_version"],
            matcher_version=evaluation["matcher_version"],
            inputs=tuple(
                SubmittedIdentifier(
                    identifier_type=IdentifierType(row["identifier_type"]),
                    submitted_literal=row["submitted_literal"],
                    normalized_value=row["normalized_value"],
                )
                for row in inputs
            ),
        )
