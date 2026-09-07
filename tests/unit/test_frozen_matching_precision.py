import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from agent_data_oracle.evidence_queue import (
    CandidateClass,
    EvidenceQueueContract,
    EvidenceRow,
    SubmittedIdentifier,
    _source_constraints,
    match_cpsc_record,
    normalize_identifier,
    serialize_evidence_contract,
)
from agent_data_oracle.frozen_matching import (
    FROZEN_MATCHING_PAIRS,
    FrozenCorpusError,
    load_frozen_matching_pairs,
    matching_report,
    retained_record,
)

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
    assert all(pair.case_id.startswith("cpsc-") for pair in FROZEN_MATCHING_PAIRS)
    assert len({pair.case_id for pair in FROZEN_MATCHING_PAIRS}) == 100


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


def test_corpus_loader_rejects_a_fixture_hash_or_source_fact_mismatch(
    tmp_path: Path,
) -> None:
    asset = json.loads(
        Path("tests/fixtures/cpsc/frozen_matching_corpus.json").read_text()
    )
    asset["cases"][0]["fixture"]["sha256"] = "0" * 64
    invalid_hash = tmp_path / "invalid-hash.json"
    invalid_hash.write_text(json.dumps(asset))

    with pytest.raises(FrozenCorpusError, match="hash"):
        load_frozen_matching_pairs(invalid_hash)

    asset = json.loads(
        Path("tests/fixtures/cpsc/frozen_matching_corpus.json").read_text()
    )
    asset["cases"][0]["expected_evidence"]["official_url"] = "https://invalid.test"
    invalid_source = tmp_path / "invalid-source.json"
    invalid_source.write_text(json.dumps(asset))

    with pytest.raises(FrozenCorpusError, match="official_url"):
        load_frozen_matching_pairs(invalid_source)


def test_frozen_matching_corpus_preserves_reviewed_classifications_and_bases() -> None:
    report = matching_report(FROZEN_MATCHING_PAIRS)

    assert report.false_exact_candidates == ()
    assert report.discrepancies == ()


def test_matching_gate_reports_all_discrepancies_without_rewriting_expectations(
) -> None:
    incorrect_class = replace(
        FROZEN_MATCHING_PAIRS[0], expected_class=CandidateClass.POSSIBLE_IDENTIFIER
    )
    incorrect_basis = replace(
        FROZEN_MATCHING_PAIRS[1], expected_field="different source field"
    )

    report = matching_report((incorrect_class, incorrect_basis))

    assert [discrepancy.kind for discrepancy in report.discrepancies] == [
        "classification",
        "field",
    ]
    assert incorrect_class.expected_class is CandidateClass.POSSIBLE_IDENTIFIER

    incorrect_evidence = replace(
        FROZEN_MATCHING_PAIRS[2],
        expected_evidence=replace(
            FROZEN_MATCHING_PAIRS[2].expected_evidence,
            official_url="https://incorrect.example",
            constraint_scope="not_machine_parsed",
        ),
    )

    report = matching_report((incorrect_evidence,))

    assert [discrepancy.kind for discrepancy in report.discrepancies] == [
        "evidence.official_url",
        "evidence.constraint_scope",
    ]


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
        record = retained_record(pair)
        matches = match_cpsc_record(submitted, record)
        assert matches
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
                    candidate_class=matches[0].candidate_class,
                    match_bases=matches,
                    affected_product_evidence=record,
                    constraints=constraints,
                    recall_number=pair.expected_evidence.recall_number,
                    official_url=pair.expected_evidence.official_url,
                    recall_date_literal=pair.expected_evidence.recall_date_literal,
                    last_publish_date_literal=(
                        pair.expected_evidence.last_publish_date_literal
                    ),
                    source_observed_at=pair.source.observed_at,
                    source_revision_completed_at=pair.source.completed_at,
                ),
            ),
        )
        rendered = serialize_evidence_contract(contract)
        assert required <= set(rendered["candidates"][0])  # type: ignore[index]
        assert rendered["limitations"]
