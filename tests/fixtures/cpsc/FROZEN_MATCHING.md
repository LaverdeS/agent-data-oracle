# Frozen matching precision corpus

[`frozen_matching_corpus.json`](frozen_matching_corpus.json) is the 100-case,
reviewer-owned release gate for deterministic matching. It is deliberately a
flat, explicit asset: each row has its own identifier, retained fixture hash,
typed literal, expected classification and basis, expected source/evidence
facts, direct CPSC notice, and source-specific review reason. Do not replace
these rows with a generator, permutations created at load time, or matcher
output. Every expectation change requires a reviewer to update that row's note
with the official-source reason.

The compact projections were checked on 2026-09-07 against these official
notices:

- Predator 2000W Power Station: https://www.cpsc.gov/Recalls/2025/Harbor-Freight-Tools-Recalls-Predator-2000-Watt-Power-Stations-Due-to-Shock-Hazard
- HARPPA Nordi Toddler Tower Stool: https://www.cpsc.gov/Recalls/2026/HARPPA-Recalls-Nordi-Toddler-Tower-Stools-Due-to-Risk-of-Serious-Injury-and-Death-from-Entrapment-and-Fall-Hazards
- Brookstone Tabletop Fire Pits: https://www.cpsc.gov/Recalls/2026/Southern-Telecom-Recalls-Brookstone-Branded-Tabletop-Fire-Pits-Due-to-Risk-of-Serious-Burn-Injury-or-Death-from-Flame-Jetting-and-Fire-Hazards
- MGA Miniverse Make It Mini Sets: https://www.cpsc.gov/Recalls/2024/MGA-Entertainment-Recalls-Miniverse-Make-It-Mini-Sets-with-Unused-Liquid-Resins-Due-to-Risk-of-Skin-Eye-and-Respiratory-Irritation-and-Sensitization-Violation-of-the-Federal-Hazardous-Substances-Act
- Auto World Deluxe Pit Kit Slot Cars: https://www.cpsc.gov/Recalls/2025/Round-2-Recalls-Auto-World-Unassembled-Deluxe-Pit-Kit-Slot-Cars-Due-to-Ingestion-Hazard-Violation-of-Federal-Regulations-for-Magnets
- Granitestone Diamond Pro Blue Sauté Pans: https://www.cpsc.gov/Recalls/2026/E-Mishan-Recalls-Granitestone-Diamond-Pro-Blue-Stainless-Saute-Pans-Due-to-Impact-and-Burn-Hazards

The gate is entirely offline. The loader verifies each fixture hash and every
source fact against the retained payload before it exposes pairs to the matcher.
It verifies 50 expected candidates (including exact UPC, model, brand,
delimiter, and constraint behavior) and 50 distinct source-referenced
confusers, aggregates all discrepancies, requires zero false exact candidates,
and checks mandatory evidence-contract fields on every expected candidate.

The full recorded API payload for Brookstone recall 26-687 is retained as
`recall-10915.json`; it is the authoritative offline record for the structured
UPC candidate cases. The existing `recall-10887.json` is the retained HARPPA
record for the model, brand, and constraint cases.
