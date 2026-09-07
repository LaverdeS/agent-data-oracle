# Frozen matching precision corpus

`agent_data_oracle.frozen_matching.FROZEN_MATCHING_PAIRS` is the 100-pair
release gate for deterministic matching. The expected class and match-basis
facts are reviewer-owned data: do not regenerate or update them from matcher
output. Each change must update the pair's `review_note` with the direct CPSC
notice and the reason a reviewer changed the expectation.

The compact projections were checked on 2026-09-07 against these official
notices:

- Predator 2000W Power Station: https://www.cpsc.gov/Recalls/2025/Harbor-Freight-Tools-Recalls-Predator-2000-Watt-Power-Stations-Due-to-Shock-Hazard
- HARPPA Nordi Toddler Tower Stool: https://www.cpsc.gov/Recalls/2026/HARPPA-Recalls-Nordi-Toddler-Tower-Stools-Due-to-Risk-of-Serious-Injury-and-Death-from-Entrapment-and-Fall-Hazards
- Brookstone Tabletop Fire Pits: https://www.cpsc.gov/Recalls/2026/Southern-Telecom-Recalls-Brookstone-Branded-Tabletop-Fire-Pits-Due-to-Risk-of-Serious-Burn-Injury-or-Death-from-Flame-Jetting-and-Fire-Hazards
- Granitestone Diamond Pro Blue Sauté Pans: https://www.cpsc.gov/Recalls/2026/E-Mishan-Recalls-Granitestone-Diamond-Pro-Blue-Stainless-Saute-Pans-Due-to-Impact-and-Burn-Hazards

The gate is entirely offline. It verifies 50 expected candidates (including
exact UPC and possible model/brand paths) and 50 source-referenced confusers,
then requires zero false exact candidates and all mandatory evidence-contract
fields on expected candidates.
