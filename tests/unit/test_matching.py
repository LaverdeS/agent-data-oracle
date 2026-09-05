from agent_data_oracle.evidence_queue import (
    CandidateClass,
    IdentifierType,
    SubmittedIdentifier,
    match_cpsc_record,
    normalize_identifier,
    submitted_identifiers_from_form,
)


def test_upc_match_is_exact_only_when_normalized_literals_are_equal() -> None:
    submitted = SubmittedIdentifier(
        identifier_type=IdentifierType.UPC,
        submitted_literal="000-123 456789",
        normalized_value=normalize_identifier(IdentifierType.UPC, "000-123 456789"),
    )

    matches = match_cpsc_record(
        submitted,
        {
            "RecallID": 1,
            "ProductUPCs": [{"UPC": "000123456789"}],
            "URL": "https://www.cpsc.gov/Recalls/example",
        },
    )

    assert matches[0].candidate_class is CandidateClass.EXACT_IDENTIFIER
    assert matches[0].matched_field == "ProductUPCs[0].UPC"
    assert matches[0].matched_literal == "000123456789"


def test_model_and_brand_matching_are_possible_never_exact() -> None:
    model = SubmittedIdentifier(
        identifier_type=IdentifierType.MODEL,
        submitted_literal="HANS-2",
        normalized_value=normalize_identifier(IdentifierType.MODEL, "HANS-2"),
    )
    brand = SubmittedIdentifier(
        identifier_type=IdentifierType.BRAND,
        submitted_literal="HARPPA",
        normalized_value=normalize_identifier(IdentifierType.BRAND, "HARPPA"),
    )
    record = {
        "RecallID": 1,
        "Description": "MODEL No.: HANS-2 is printed on the label.",
        "Title": "HARPPA Recalls a product",
        "URL": "https://www.cpsc.gov/Recalls/example",
    }

    model_match = match_cpsc_record(model, record)[0]
    brand_match = match_cpsc_record(brand, record)[0]

    assert model_match.candidate_class is CandidateClass.POSSIBLE_IDENTIFIER
    assert brand_match.candidate_class is CandidateClass.POSSIBLE_IDENTIFIER
    assert brand_match.identity_limit == (
        "Brand equality alone is insufficient identity."
    )


def test_model_normalization_keeps_identity_bearing_punctuation_and_casefolds() -> None:
    identifiers = submitted_identifiers_from_form(
        {
            "authorization": ["authorized"],
            "csrf_token": ["token"],
            "idempotency_key": ["key"],
            "identifier_type": ["model"],
            "identifier_value": ["hans-02/r2"],
        },
        body_is_within_limit=True,
    )

    assert identifiers[0].normalized_value == "hans-02/r2"


def test_generic_text_overlap_does_not_create_a_candidate() -> None:
    submitted = SubmittedIdentifier(
        identifier_type=IdentifierType.MODEL,
        submitted_literal="HANS0002",
        normalized_value="hans0002",
    )

    matches = match_cpsc_record(
        submitted,
        {
            "Description": "The HANS0002 tower is sold in several colors.",
            "URL": "https://www.cpsc.gov/Recalls/example",
        },
    )

    assert matches == ()
