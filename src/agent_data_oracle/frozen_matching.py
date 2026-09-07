"""Reviewer-owned CPSC matching corpus and its deterministic release gate."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_data_oracle.evidence_queue import (
    CandidateClass,
    IdentifierType,
    SubmittedIdentifier,
    _source_constraints,
    match_cpsc_record,
    normalize_identifier,
)

FIXTURE_DIRECTORY = Path(__file__).resolve().parents[2] / "tests/fixtures/cpsc"
CORPUS_PATH = FIXTURE_DIRECTORY / "frozen_matching_corpus.json"


class FrozenCorpusError(ValueError):
    """A reviewer-owned corpus row no longer agrees with its retained source."""


@dataclass(frozen=True)
class FrozenSource:
    fixture_filename: str
    fixture_sha256: str
    official_url: str
    recall_number: str
    recall_date_literal: str
    last_publish_date_literal: str
    observed_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class FrozenMatchingPair:
    case_id: str
    review_note: str
    source: FrozenSource
    submitted_literal: str
    identifier_type: IdentifierType
    expected_class: CandidateClass | None
    expected_field: str | None
    expected_literal: str | None
    expected_constraint_scope: str


@dataclass(frozen=True)
class FrozenDiscrepancy:
    case_id: str
    kind: str
    expected: str | None
    actual: str | None


@dataclass(frozen=True)
class FrozenMatchingReport:
    discrepancies: tuple[FrozenDiscrepancy, ...]
    false_exact_candidates: tuple[str, ...]


def _required_string(mapping: dict[str, object], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value:
        raise FrozenCorpusError(f"missing {field}")
    return value


def _payload_string(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise FrozenCorpusError(f"retained payload is missing {field}")
    return value


def _expected_evidence(
    case: dict[str, object], record: dict[str, Any]
) -> tuple[dict[str, object], datetime, datetime]:
    expected = case.get("expected_evidence")
    if not isinstance(expected, dict):
        raise FrozenCorpusError("missing expected_evidence")
    expected_evidence = dict(expected)
    source_fields = {
        "official_url": record.get("URL"),
        "recall_number": record.get("RecallNumber"),
        "recall_date": record.get("RecallDate"),
        "last_publish_date": record.get("LastPublishDate"),
        "constraint_scope": _source_constraints(record)["scope"],
    }
    for field, actual in source_fields.items():
        if expected_evidence.get(field) != actual:
            raise FrozenCorpusError(f"{field} does not match retained payload")
    observed_at = datetime.fromisoformat(
        _required_string(expected_evidence, "source_observed_at").replace("Z", "+00:00")
    )
    completed_at = datetime.fromisoformat(
        _required_string(expected_evidence, "source_revision_completed_at").replace(
            "Z", "+00:00"
        )
    )
    if observed_at > completed_at:
        raise FrozenCorpusError("source observation is after source completion")
    return expected_evidence, observed_at, completed_at


def _record_for_fixture(filename: str, expected_hash: str) -> dict[str, Any]:
    payload = (FIXTURE_DIRECTORY / filename).read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise FrozenCorpusError(f"fixture hash mismatch for {filename}")
    decoded = json.loads(payload)
    if (
        not isinstance(decoded, list)
        or len(decoded) != 1
        or not isinstance(decoded[0], dict)
    ):
        raise FrozenCorpusError(f"fixture {filename} must contain one CPSC record")
    return decoded[0]


def _case_pair(case: object) -> FrozenMatchingPair:
    if not isinstance(case, dict):
        raise FrozenCorpusError("corpus case must be an object")
    fixture = case.get("fixture")
    identifier = case.get("identifier")
    match_basis = case.get("expected_match_basis")
    if not isinstance(fixture, dict) or not isinstance(identifier, dict):
        raise FrozenCorpusError("case is missing fixture or identifier")
    filename = _required_string(fixture, "filename")
    fixture_hash = _required_string(fixture, "sha256")
    record = _record_for_fixture(filename, fixture_hash)
    expected_evidence, observed_at, completed_at = _expected_evidence(case, record)
    official_notice = _required_string(case, "official_notice")
    if official_notice != _payload_string(record, "URL"):
        raise FrozenCorpusError("official_notice does not match retained payload")
    review_note = _required_string(case, "review_note")
    if official_notice not in review_note:
        raise FrozenCorpusError("review_note must include the official notice")
    expected_class_literal = case.get("expected_class")
    expected_class = (
        None
        if expected_class_literal is None
        else CandidateClass(_required_string(case, "expected_class"))
    )
    if expected_class is None:
        if match_basis is not None:
            raise FrozenCorpusError("confuser cannot have an expected match basis")
        expected_field = None
        expected_literal = None
    else:
        if not isinstance(match_basis, dict):
            raise FrozenCorpusError("candidate is missing expected_match_basis")
        expected_field = _required_string(match_basis, "field")
        expected_literal = _required_string(match_basis, "literal")
    return FrozenMatchingPair(
        case_id=_required_string(case, "case_id"),
        review_note=review_note,
        source=FrozenSource(
            fixture_filename=filename,
            fixture_sha256=fixture_hash,
            official_url=_payload_string(record, "URL"),
            recall_number=_payload_string(record, "RecallNumber"),
            recall_date_literal=_payload_string(record, "RecallDate"),
            last_publish_date_literal=_payload_string(record, "LastPublishDate"),
            observed_at=observed_at,
            completed_at=completed_at,
        ),
        submitted_literal=_required_string(identifier, "literal"),
        identifier_type=IdentifierType(_required_string(identifier, "type")),
        expected_class=expected_class,
        expected_field=expected_field,
        expected_literal=expected_literal,
        expected_constraint_scope=_required_string(
            expected_evidence, "constraint_scope"
        ),
    )


def load_frozen_matching_pairs(
    path: Path = CORPUS_PATH,
) -> tuple[FrozenMatchingPair, ...]:
    """Load independently recorded expectations and verify their source binding."""
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or not isinstance(document.get("cases"), list):
        raise FrozenCorpusError("corpus must contain a cases list")
    pairs = tuple(_case_pair(case) for case in document["cases"])
    if len(pairs) != 100:
        raise FrozenCorpusError("corpus must contain exactly 100 cases")
    case_ids = [pair.case_id for pair in pairs]
    identities = [
        (pair.source.fixture_filename, pair.identifier_type, pair.submitted_literal)
        for pair in pairs
    ]
    if len(set(case_ids)) != len(case_ids) or len(set(identities)) != len(identities):
        raise FrozenCorpusError("corpus cases must be individually distinct")
    if sum(pair.expected_class is not None for pair in pairs) < 50:
        raise FrozenCorpusError("corpus must contain at least 50 expected candidates")
    return pairs


def _submitted_identifier(pair: FrozenMatchingPair) -> SubmittedIdentifier:
    return SubmittedIdentifier(
        identifier_type=pair.identifier_type,
        submitted_literal=pair.submitted_literal,
        normalized_value=normalize_identifier(
            pair.identifier_type, pair.submitted_literal
        ),
    )


def matching_report(
    pairs: tuple[FrozenMatchingPair, ...],
) -> FrozenMatchingReport:
    """Compare all frozen expectations without modifying the reviewer-owned asset."""
    discrepancies: list[FrozenDiscrepancy] = []
    false_exact_candidates: list[str] = []
    for pair in pairs:
        record = _record_for_fixture(
            pair.source.fixture_filename, pair.source.fixture_sha256
        )
        matches = match_cpsc_record(_submitted_identifier(pair), record)
        actual_class = min(
            (match.candidate_class for match in matches),
            default=None,
            key=lambda value: value is not CandidateClass.EXACT_IDENTIFIER,
        )
        if actual_class is CandidateClass.EXACT_IDENTIFIER and (
            pair.expected_class is not CandidateClass.EXACT_IDENTIFIER
        ):
            false_exact_candidates.append(pair.case_id)
        if actual_class is not pair.expected_class:
            discrepancies.append(
                FrozenDiscrepancy(
                    pair.case_id,
                    "classification",
                    None if pair.expected_class is None else str(pair.expected_class),
                    None if actual_class is None else str(actual_class),
                )
            )
        if pair.expected_class is not None:
            actual_field = matches[0].matched_field if matches else None
            actual_literal = matches[0].matched_literal if matches else None
            if actual_field != pair.expected_field:
                discrepancies.append(
                    FrozenDiscrepancy(
                        pair.case_id, "field", pair.expected_field, actual_field
                    )
                )
            if actual_literal != pair.expected_literal:
                discrepancies.append(
                    FrozenDiscrepancy(
                        pair.case_id, "literal", pair.expected_literal, actual_literal
                    )
                )
    return FrozenMatchingReport(tuple(discrepancies), tuple(false_exact_candidates))


FROZEN_MATCHING_PAIRS = load_frozen_matching_pairs()
