import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_data_oracle.evidence_queue import (
    CandidateClass,
    EvidenceQueueContract,
    EvidenceRow,
    RecordMatch,
    SubmittedIdentifier,
    _source_constraints,
    match_cpsc_record,
    normalize_identifier,
    serialize_evidence_contract,
)
from agent_data_oracle.frozen_matching import FROZEN_MATCHING_PAIRS

FIXTURE_DIRECTORY = Path("tests/fixtures/cpsc")


def _submitted_identifier(pair: object) -> SubmittedIdentifier:
    identifier_type = pair.identifier_type  # type: ignore[union-attr]
    literal = pair.submitted_literal  # type: ignore[union-attr]
    return SubmittedIdentifier(
        identifier_type=identifier_type,
        submitted_literal=literal,
        normalized_value=normalize_identifier(identifier_type, literal),
    )


def _retained_record(pair: object) -> dict[str, object]:
    source = pair.source  # type: ignore[union-attr]
    payload = (FIXTURE_DIRECTORY / source.fixture_filename).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == source.fixture_sha256
    record = json.loads(payload)[0]
    assert isinstance(record, dict)
    return record


def test_frozen_matching_corpus_has_one_hundred_reviewed_pairs() -> None:
    assert len(FROZEN_MATCHING_PAIRS) == 100
    assert (
        len(
            {
                (
                    pair.source.fixture_filename,
                    pair.identifier_type,
                    pair.submitted_literal,
                )
                for pair in FROZEN_MATCHING_PAIRS
            }
        )
        == 100
    )
    assert sum(pair.expected_class is not None for pair in FROZEN_MATCHING_PAIRS) == 50
    assert all(
        pair.source.observed_at <= pair.source.completed_at
        for pair in FROZEN_MATCHING_PAIRS
    )
    assert all(
        pair.source.official_url in pair.review_note for pair in FROZEN_MATCHING_PAIRS
    )


def test_frozen_sources_are_bound_to_complete_retained_cpsc_payloads() -> None:
    sources = {
        pair.source.fixture_filename: pair.source for pair in FROZEN_MATCHING_PAIRS
    }
    for source in sources.values():
        payload = (FIXTURE_DIRECTORY / source.fixture_filename).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == source.fixture_sha256
        record = json.loads(payload)[0]
        assert record["URL"] == source.official_url
        assert record["RecallNumber"] == source.recall_number.replace("-", "")


def test_frozen_matching_corpus_preserves_reviewed_classifications_and_bases() -> None:
    discrepancies = []
    false_exact_candidates = []
    for pair in FROZEN_MATCHING_PAIRS:
        submitted = _submitted_identifier(pair)
        matches = match_cpsc_record(submitted, _retained_record(pair))
        actual_class = min(
            (match.candidate_class for match in matches),
            default=None,
            key=lambda value: value is not CandidateClass.EXACT_IDENTIFIER,
        )
        if actual_class is CandidateClass.EXACT_IDENTIFIER and (
            pair.expected_class is not CandidateClass.EXACT_IDENTIFIER
        ):
            false_exact_candidates.append(pair)

        if actual_class is not pair.expected_class:
            discrepancies.append((pair, "classification", actual_class))
            continue
        if pair.expected_class is not None:
            if matches[0].matched_field != pair.expected_field:
                discrepancies.append((pair, "field", matches[0].matched_field))
            if matches[0].matched_literal != pair.expected_literal:
                discrepancies.append((pair, "literal", matches[0].matched_literal))

    assert false_exact_candidates == [] and discrepancies == []


def test_every_expected_candidate_has_the_mandatory_evidence_contract_fields() -> None:
    required = {
        "affected_product_evidence",
        "candidate_class",
        "constraints",
        "last_publish_date",
        "match_bases",
        "official_url",
        "recall_date",
        "recall_number",
        "source_observed_at",
        "source_revision_completed_at",
        "submitted_identifier",
    }
    instant = datetime(2026, 9, 7, tzinfo=UTC)
    for pair in FROZEN_MATCHING_PAIRS:
        if pair.expected_class is None:
            continue
        submitted = _submitted_identifier(pair)
        record = _retained_record(pair)
        constraints = _source_constraints(record)
        assert constraints["scope"] == pair.expected_constraint_scope
        contract = EvidenceQueueContract(
            evaluation_id=uuid4(),
            source_revision_id=uuid4(),
            evaluated_at=instant,
            normalization_version="frozen-test",
            matcher_version="frozen-test",
            inputs=(submitted,),
            candidates=(
                EvidenceRow(
                    submitted_identifier=submitted,
                    candidate_class=pair.expected_class,
                    match_bases=(
                        RecordMatch(
                            candidate_class=pair.expected_class,
                            matched_field=pair.expected_field or "",
                            matched_literal=pair.expected_literal or "",
                        ),
                    ),
                    affected_product_evidence=record,
                    constraints=constraints,
                    recall_number=record["RecallNumber"],
                    official_url=pair.source.official_url,
                    recall_date_literal=record["RecallDate"],
                    last_publish_date_literal=record["LastPublishDate"],
                    source_observed_at=pair.source.observed_at,
                    source_revision_completed_at=pair.source.completed_at,
                ),
            ),
        )
        rendered = serialize_evidence_contract(contract)
        assert required <= set(rendered["candidates"][0])  # type: ignore[index]
        assert rendered["limitations"]
