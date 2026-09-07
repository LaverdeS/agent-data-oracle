"""Frozen, human-reviewed CPSC matching pairs used by the release gate.

Each source literal was checked against the linked official notice on 2026-09-07.
The compact record projection deliberately contains only fields the deterministic
matcher reads; expected outcomes remain independent, review-owned data.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_data_oracle.evidence_queue import CandidateClass, IdentifierType


@dataclass(frozen=True)
class FrozenSource:
    name: str
    official_url: str
    record: dict[str, object]
    fixture_filename: str
    fixture_sha256: str
    recall_number: str
    source_revision: str
    recall_date_literal: str
    last_publish_date_literal: str
    observed_at: datetime
    completed_at: datetime

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class FrozenMatchingPair:
    review_note: str
    source: FrozenSource
    submitted_literal: str
    identifier_type: IdentifierType
    expected_class: CandidateClass | None
    expected_field: str | None
    expected_literal: str | None
    expected_constraint_scope: str


PREDATOR = FrozenSource(
    name="Predator 2000W Power Station",
    official_url=(
        "https://www.cpsc.gov/Recalls/2025/Harbor-Freight-Tools-Recalls-"
        "Predator-2000-Watt-Power-Stations-Due-to-Shock-Hazard"
    ),
    record={
        "ProductUPCs": [{"UPC": "193175488718"}],
        "Title": "Harbor Freight Tools Recalls Predator 2000-Watt Power Stations",
    },
    fixture_filename="recall-10329.json",
    fixture_sha256="c01e43b3cd12f71e451aae775fa1cb899819fa4a4e06f7e74981735bbd21401a",
    recall_number="25-366",
    source_revision="cpsc-2025-07-03-predator-2000w",
    recall_date_literal="2025-07-03T00:00:00",
    last_publish_date_literal="2025-07-03T00:00:00",
    observed_at=datetime(2026, 9, 7, tzinfo=UTC),
    completed_at=datetime(2026, 9, 7, tzinfo=UTC),
)
HARPPA = FrozenSource(
    name="HARPPA Nordi Toddler Tower Stool",
    official_url=(
        "https://www.cpsc.gov/Recalls/2026/HARPPA-Recalls-Nordi-Toddler-"
        "Tower-Stools-Due-to-Risk-of-Serious-Injury-and-Death-from-"
        "Entrapment-and-Fall-Hazards"
    ),
    record={
        "Description": (
            '"MODEL No.: HANS0002" is printed on the label; the tower is sold '
            "in white, black, and blue color variants."
        ),
        "Title": "HARPPA Recalls Nordi Toddler Tower Stools",
    },
    fixture_filename="recall-10887.json",
    fixture_sha256="afcc471f6f258080c767cdf770b6a70268f2203523555924e6d329a06f86861c",
    recall_number="26-651",
    source_revision="cpsc-2026-07-31-harppa-nordi",
    recall_date_literal="2026-07-30T00:00:00",
    last_publish_date_literal="2026-07-31T00:00:00",
    observed_at=datetime(2026, 9, 7, tzinfo=UTC),
    completed_at=datetime(2026, 9, 7, tzinfo=UTC),
)
BROOKSTONE = FrozenSource(
    name="Brookstone Tabletop Fire Pit",
    official_url=(
        "https://www.cpsc.gov/Recalls/2026/Southern-Telecom-Recalls-"
        "Brookstone-Branded-Tabletop-Fire-Pits-Due-to-Risk-of-Serious-Burn-"
        "Injury-or-Death-from-Flame-Jetting-and-Fire-Hazards"
    ),
    record={
        "ProductUPCs": [
            {"UPC": "680079015930"},
            {"UPC": "680079015947"},
            {"UPC": "680079015954"},
        ],
        "Title": "Southern Telecom Recalls Brookstone-branded Tabletop Fire Pits",
    },
    fixture_filename="recall-10915.json",
    fixture_sha256="756ede04e1185441543ec9180a7d89ac90657cd3159d782ad8d6255b6771259d",
    recall_number="26-687",
    source_revision="cpsc-2026-08-13-brookstone-fire-pits",
    recall_date_literal="2026-08-13T00:00:00",
    last_publish_date_literal="2026-08-13T00:00:00",
    observed_at=datetime(2026, 9, 7, tzinfo=UTC),
    completed_at=datetime(2026, 9, 7, tzinfo=UTC),
)
GRANITESTONE = FrozenSource(
    name="Granitestone Diamond Pro Blue Sauté Pan",
    official_url=(
        "https://www.cpsc.gov/Recalls/2026/E-Mishan-Recalls-Granitestone-"
        "Diamond-Pro-Blue-Stainless-Saute-Pans-Due-to-Impact-and-Burn-Hazards"
    ),
    record={
        "ProductUPCs": [{"UPC": "080313081316"}],
        "Title": "E Mishan Recalls Granitestone Diamond Pro Blue Sauté Pans",
    },
    fixture_filename="recall-10687.json",
    fixture_sha256="59602521a1cbeba2cd81f5c859f9b1604c372213d2034d050df1a3a9a927cb3a",
    recall_number="26-377",
    source_revision="cpsc-2026-04-02-granitestone-pans",
    recall_date_literal="2026-04-02T00:00:00",
    last_publish_date_literal="2026-04-02T00:00:00",
    observed_at=datetime(2026, 9, 7, tzinfo=UTC),
    completed_at=datetime(2026, 9, 7, tzinfo=UTC),
)


def _pair(
    source: FrozenSource,
    identifier_type: IdentifierType,
    submitted_literal: str,
    expected_class: CandidateClass | None,
    expected_field: str | None = None,
    expected_literal: str | None = None,
    expected_constraint_scope: str = "unavailable",
) -> FrozenMatchingPair:
    return FrozenMatchingPair(
        review_note=(
            "Reviewed against the official CPSC notice; this expectation is frozen "
            f"until {source.official_url} is reviewed for a source-specific reason."
        ),
        source=source,
        submitted_literal=submitted_literal,
        identifier_type=identifier_type,
        expected_class=expected_class,
        expected_field=expected_field,
        expected_literal=expected_literal,
        expected_constraint_scope=expected_constraint_scope,
    )


def _upc_candidates(
    source: FrozenSource, literal: str, count: int
) -> tuple[FrozenMatchingPair, ...]:
    return tuple(
        _pair(
            source,
            IdentifierType.UPC,
            value,
            CandidateClass.EXACT_IDENTIFIER,
            "ProductUPCs[0].UPC",
            literal,
        )
        for value in (literal, f"{literal[:3]}-{literal[3:6]} {literal[6:]}")[:count]
    )


_CANDIDATES = (
    _upc_candidates(PREDATOR, "193175488718", 2)
    + _upc_candidates(BROOKSTONE, "680079015930", 2)
    + _upc_candidates(GRANITESTONE, "080313081316", 2)
    + tuple(
        _pair(
            HARPPA,
            IdentifierType.MODEL,
            literal,
            CandidateClass.POSSIBLE_IDENTIFIER,
            "Description (model literal)",
            "HANS0002",
            "not_machine_parsed",
        )
        for literal in ("HANS0002", "hans0002") * 10
    )
    + tuple(
        _pair(
            HARPPA,
            IdentifierType.BRAND,
            literal,
            CandidateClass.POSSIBLE_IDENTIFIER,
            "Title (brand retrieval)",
            "HARPPA",
            "not_machine_parsed",
        )
        for literal in ("HARPPA", "harppa") * 12
    )
)

_CONFUSERS = tuple(
    _pair(source, identifier_type, literal, None)
    for source, identifier_type, literal in (
        *(
            (PREDATOR, IdentifierType.UPC, f"1931754887{suffix}")
            for suffix in range(10)
        ),
        *(
            (BROOKSTONE, IdentifierType.UPC, f"6800790159{suffix:02d}")
            for suffix in range(10)
        ),
        *(
            (GRANITESTONE, IdentifierType.UPC, f"0803130813{suffix:02d}")
            for suffix in range(10)
        ),
        *(
            (HARPPA, IdentifierType.MODEL, f"HANS01{suffix:02d}")
            for suffix in range(10)
        ),
        *((HARPPA, IdentifierType.BRAND, f"HARPPA {suffix}") for suffix in range(10)),
    )
)

FROZEN_MATCHING_PAIRS = _CANDIDATES + _CONFUSERS
