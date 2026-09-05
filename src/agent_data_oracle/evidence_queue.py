import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from agent_data_oracle.auth import utc_now
from agent_data_oracle.database import Database

NORMALIZATION_VERSION = "identifier-v2"
MATCHER_VERSION = "deterministic-candidate-v1"
MAX_IDENTIFIER_ROWS = 50
MAX_IDENTIFIER_LITERAL_LENGTH = 80
_BRAND_LITERAL = re.compile(r"[\w][\w'&+\-]{0,31}( [\w][\w'&+\-]{0,31}){0,2}")
_MODEL_LITERAL = re.compile(
    r"(?:[Mm]odel )?(?:[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)+|[A-Za-z]+[0-9]+)"
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
CANDIDATE_LIMITATIONS = (
    "This evidence queue contains CPSC/U.S. consumer-product recall data only. "
    "It is not legal advice or a safety assessment, and CPSC does not endorse "
    "this service.",
    "A candidate is a source-linked prompt for human review, not a statement that "
    "a listing is recalled, safe, compliant, illegal, removable, cleared, or "
    "permitted for sale.",
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


class CandidateClass(StrEnum):
    EXACT_IDENTIFIER = "exact_identifier_candidate"
    POSSIBLE_IDENTIFIER = "possible_identifier_candidate"


@dataclass(frozen=True)
class SubmittedIdentifier:
    identifier_type: IdentifierType
    submitted_literal: str
    normalized_value: str


@dataclass(frozen=True)
class RecordMatch:
    candidate_class: CandidateClass
    matched_field: str
    matched_literal: str
    identity_limit: str | None = None


def normalize_identifier(identifier_type: IdentifierType, literal: str) -> str:
    """Normalize only the representation rules permitted for the typed input."""
    if identifier_type is IdentifierType.UPC:
        return literal.replace(" ", "").replace("-", "")
    return " ".join(unicodedata.normalize("NFKC", literal).casefold().split())


def _string_values(value: object, *, field: str) -> tuple[tuple[str, str], ...]:
    if isinstance(value, str):
        return ((field, value),)
    if not isinstance(value, list):
        return ()
    values: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            values.append((f"{field}[{index}]", item))
        elif isinstance(item, dict):
            for key in ("UPC", "upc", "Value", "value"):
                literal = item.get(key)
                if isinstance(literal, str):
                    values.append((f"{field}[{index}].{key}", literal))
    return tuple(values)


def _model_values(record: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    products = record.get("Products")
    if isinstance(products, list):
        for index, product in enumerate(products):
            if isinstance(product, dict) and isinstance(product.get("Model"), str):
                values.append((f"Products[{index}].Model", product["Model"]))
    description = record.get("Description")
    if isinstance(description, str):
        for match in re.finditer(
            r"\bMODEL(?:\s+(?:NO\.?|NUMBER))?\s*[:#]?\s*"
            r"([A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*)",
            description,
            flags=re.IGNORECASE,
        ):
            values.append(("Description (model literal)", match.group(1)))
    return tuple(values)


def _brand_values(record: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    title = record.get("Title")
    if isinstance(title, str):
        # The source title's leading named phrase is a retrieval cue only.
        leading = title.split(" Recalls", maxsplit=1)[0]
        if leading:
            values.append(("Title (brand retrieval)", leading))
    for collection in ("Manufacturers", "Importers", "Distributors"):
        items = record.get(collection)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if isinstance(item, dict) and isinstance(item.get("Name"), str):
                values.append((f"{collection}[{index}].Name", item["Name"]))
    return tuple(values)


def match_cpsc_record(
    submitted: SubmittedIdentifier, record: dict[str, Any]
) -> tuple[RecordMatch, ...]:
    """Return inspectable deterministic bases for one input/recall pair."""
    if submitted.identifier_type is IdentifierType.UPC:
        values = _string_values(record.get("ProductUPCs"), field="ProductUPCs")
        matches = [
            RecordMatch(CandidateClass.EXACT_IDENTIFIER, field, literal)
            for field, literal in values
            if normalize_identifier(IdentifierType.UPC, literal)
            == submitted.normalized_value
            and len(normalize_identifier(IdentifierType.UPC, literal))
            == len(submitted.normalized_value)
        ]
        return tuple(matches)
    if submitted.identifier_type is IdentifierType.MODEL:
        return tuple(
            RecordMatch(CandidateClass.POSSIBLE_IDENTIFIER, field, literal)
            for field, literal in _model_values(record)
            if normalize_identifier(IdentifierType.MODEL, literal)
            == submitted.normalized_value
        )
    return tuple(
        RecordMatch(
            CandidateClass.POSSIBLE_IDENTIFIER,
            field,
            literal,
            "Brand equality alone is insufficient identity.",
        )
        for field, literal in _brand_values(record)
        if normalize_identifier(IdentifierType.BRAND, literal)
        == submitted.normalized_value
    )


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
    candidates: tuple["EvidenceRow", ...] = ()


@dataclass(frozen=True)
class EvidenceRow:
    submitted_identifier: SubmittedIdentifier
    candidate_class: CandidateClass
    match_bases: tuple[RecordMatch, ...]
    affected_product_evidence: dict[str, Any]
    constraints: dict[str, str]
    recall_number: str
    official_url: str
    recall_date_literal: str | None
    last_publish_date_literal: str | None
    source_observed_at: datetime
    source_revision_completed_at: datetime


def serialize_evidence_contract(contract: EvidenceQueueContract) -> dict[str, object]:
    """Build the mandatory evidence contract shared by HTML and future REST."""
    outcome = "candidates" if contract.candidates else "no_candidates"
    document: dict[str, object] = {
        "evaluated_at": contract.evaluated_at.isoformat(),
        "inputs": [
            {
                "literal": identifier.submitted_literal,
                "type": identifier.identifier_type,
            }
            for identifier in contract.inputs
        ],
        "limitations": (
            CANDIDATE_LIMITATIONS if contract.candidates else NO_CANDIDATE_LIMITATIONS
        ),
        "matcher_version": contract.matcher_version,
        "normalization_version": contract.normalization_version,
        "outcome": outcome,
        "source_revision_id": str(contract.source_revision_id),
    }
    if contract.candidates:
        document["candidates"] = [
            {
                "affected_product_evidence": row.affected_product_evidence,
                "candidate_class": row.candidate_class,
                "constraints": row.constraints,
                "last_publish_date": row.last_publish_date_literal,
                "match_bases": [
                    {
                        "field": basis.matched_field,
                        "identity_limit": basis.identity_limit,
                        "literal": basis.matched_literal,
                    }
                    for basis in row.match_bases
                ],
                "official_url": row.official_url,
                "recall_date": row.recall_date_literal,
                "recall_number": row.recall_number,
                "submitted_identifier": {
                    "literal": row.submitted_identifier.submitted_literal,
                    "type": row.submitted_identifier.identifier_type,
                },
                "source_observed_at": row.source_observed_at.isoformat(),
                "source_revision_completed_at": (
                    row.source_revision_completed_at.isoformat()
                ),
            }
            for row in contract.candidates
        ]
    return document


def _affected_product_evidence(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": record.get("Description"),
        "products": record.get("Products", []),
        "title": record.get("Title"),
    }


def _source_constraints(record: dict[str, Any]) -> dict[str, str]:
    description = record.get("Description")
    if not isinstance(description, str):
        return {"scope": "unavailable"}
    if re.search(r"\b(batch|date code|size|color|serial|variant)\b", description, re.I):
        return {"scope": "not_machine_parsed", "source_literal": description}
    return {"scope": "unavailable"}


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
            compact_upc = normalize_identifier(typed_identifier, submitted_literal)
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
                normalized_value=normalize_identifier(
                    typed_identifier, submitted_literal
                ),
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

    async def submit_evaluation(
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
            source_records = (
                (
                    await connection.execute(
                        text(
                            "SELECT current.recall_id, current.recall_number, "
                            "current.official_url, current.recall_date_literal, "
                            "current.last_publish_date_literal, "
                            "current.normalized_record, observations.observed_at, "
                            "revisions.completed_at "
                            "FROM cpsc_current_records AS current "
                            "JOIN cpsc_source_revisions AS revisions "
                            "ON revisions.revision_id = current.revision_id "
                            "JOIN cpsc_source_observations AS observations "
                            "ON observations.revision_id = current.revision_id "
                            "AND observations.recall_id = current.recall_id "
                            "WHERE current.revision_id = :source_revision_id "
                            "ORDER BY current.recall_id"
                        ),
                        {"source_revision_id": source_revision_id},
                    )
                )
                .mappings()
                .all()
            )
            candidate_rows: list[tuple[int, Any, tuple[RecordMatch, ...]]] = []
            for input_position, identifier in enumerate(identifiers):
                for source_record in source_records:
                    raw_record = source_record["normalized_record"]
                    if not isinstance(raw_record, dict):
                        raise SourceUnavailableError("CPSC record is unavailable")
                    match_bases = match_cpsc_record(identifier, raw_record)
                    if match_bases:
                        candidate_rows.append(
                            (input_position, source_record, match_bases)
                        )
            evaluation_id = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO evidence_evaluations "
                    "(evaluation_id, operator_id, source_revision_id, evaluated_at, "
                    "normalization_version, matcher_version, outcome, released_at, "
                    "idempotency_key, canonical_submission_hash) VALUES "
                    "(:evaluation_id, :operator_id, :source_revision_id, "
                    ":evaluated_at, "
                    ":normalization_version, :matcher_version, :outcome, "
                    ":released_at, :idempotency_key, :canonical_submission_hash)"
                ),
                {
                    "canonical_submission_hash": canonical_submission_hash,
                    "evaluated_at": evaluated_at,
                    "evaluation_id": evaluation_id,
                    "idempotency_key": idempotency_key,
                    "matcher_version": MATCHER_VERSION,
                    "normalization_version": NORMALIZATION_VERSION,
                    "outcome": "candidates" if candidate_rows else "no_candidates",
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
            for input_position, source_record, match_bases in candidate_rows:
                candidate_class = min(
                    (basis.candidate_class for basis in match_bases),
                    key=lambda value: (
                        0 if value is CandidateClass.EXACT_IDENTIFIER else 1
                    ),
                )
                raw_record = source_record["normalized_record"]
                if not isinstance(raw_record, dict):
                    raise SourceUnavailableError("CPSC record is unavailable")
                completed_at = source_record["completed_at"]
                observed_at = source_record["observed_at"]
                if not isinstance(completed_at, datetime) or not isinstance(
                    observed_at, datetime
                ):
                    raise SourceUnavailableError("CPSC source timing is unavailable")
                await connection.execute(
                    text(
                        "INSERT INTO evidence_rows "
                        "(evidence_row_id, evaluation_id, input_position, recall_id, "
                        "candidate_class, match_bases, affected_product_evidence, "
                        "constraints, recall_number, official_url, "
                        "recall_date_literal, "
                        "last_publish_date_literal, source_observed_at, "
                        "source_revision_completed_at) VALUES "
                        "(:evidence_row_id, :evaluation_id, :input_position, "
                        ":recall_id, :candidate_class, CAST(:match_bases AS jsonb), "
                        "CAST(:affected_product_evidence AS jsonb), "
                        "CAST(:constraints AS jsonb), :recall_number, :official_url, "
                        ":recall_date_literal, :last_publish_date_literal, "
                        ":source_observed_at, :source_revision_completed_at)"
                    ),
                    {
                        "affected_product_evidence": json.dumps(
                            _affected_product_evidence(raw_record), sort_keys=True
                        ),
                        "candidate_class": candidate_class,
                        "constraints": json.dumps(
                            _source_constraints(raw_record), sort_keys=True
                        ),
                        "evaluation_id": evaluation_id,
                        "evidence_row_id": uuid4(),
                        "input_position": input_position,
                        "last_publish_date_literal": source_record[
                            "last_publish_date_literal"
                        ],
                        "match_bases": json.dumps(
                            [
                                {
                                    "candidate_class": basis.candidate_class,
                                    "identity_limit": basis.identity_limit,
                                    "matched_field": basis.matched_field,
                                    "matched_literal": basis.matched_literal,
                                }
                                for basis in match_bases
                            ],
                            sort_keys=True,
                        ),
                        "official_url": source_record["official_url"],
                        "recall_date_literal": source_record["recall_date_literal"],
                        "recall_id": source_record["recall_id"],
                        "recall_number": source_record["recall_number"],
                        "source_observed_at": observed_at,
                        "source_revision_completed_at": completed_at,
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
                            "AND released_at IS NOT NULL "
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
                            "AND released_at IS NOT NULL"
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
            candidate_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT input_position, candidate_class, match_bases, "
                            "affected_product_evidence, constraints, recall_number, "
                            "official_url, recall_date_literal, "
                            "last_publish_date_literal, source_observed_at, "
                            "source_revision_completed_at "
                            "FROM evidence_rows WHERE evaluation_id = :evaluation_id "
                            "ORDER BY CASE candidate_class "
                            "WHEN 'exact_identifier_candidate' THEN 0 ELSE 1 END, "
                            "last_publish_date_literal DESC NULLS LAST, recall_number, "
                            "evidence_row_id"
                        ),
                        {"evaluation_id": evaluation_id},
                    )
                )
                .mappings()
                .all()
            )
        submitted_inputs = tuple(
            SubmittedIdentifier(
                identifier_type=IdentifierType(row["identifier_type"]),
                submitted_literal=row["submitted_literal"],
                normalized_value=row["normalized_value"],
            )
            for row in inputs
        )
        return EvidenceQueueContract(
            evaluation_id=evaluation["evaluation_id"],
            source_revision_id=evaluation["source_revision_id"],
            evaluated_at=evaluation["evaluated_at"],
            normalization_version=evaluation["normalization_version"],
            matcher_version=evaluation["matcher_version"],
            inputs=submitted_inputs,
            candidates=tuple(
                EvidenceRow(
                    submitted_identifier=submitted_inputs[row["input_position"]],
                    candidate_class=CandidateClass(row["candidate_class"]),
                    match_bases=tuple(
                        RecordMatch(
                            candidate_class=CandidateClass(
                                basis["candidate_class"]
                            ),
                            matched_field=basis["matched_field"],
                            matched_literal=basis["matched_literal"],
                            identity_limit=basis["identity_limit"],
                        )
                        for basis in row["match_bases"]
                    ),
                    affected_product_evidence=row["affected_product_evidence"],
                    constraints=row["constraints"],
                    recall_number=row["recall_number"],
                    official_url=row["official_url"],
                    recall_date_literal=row["recall_date_literal"],
                    last_publish_date_literal=row["last_publish_date_literal"],
                    source_observed_at=row["source_observed_at"],
                    source_revision_completed_at=row["source_revision_completed_at"],
                )
                for row in candidate_rows
            ),
        )
