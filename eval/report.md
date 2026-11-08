# Extraction evaluation

Run 2026-11-08. Compared with `data/answer_key` (eval only; the app never reads it).

Pipeline that produced each vendor's current values (from `vendor_responses.extraction_model`):

- V1: `xlsx→celltext→gemini-3.8-flash@prompt_v4`
- V2: `pdf→native→gemini-3.8-flash@prompt_v4`
- V3: `docx→markdown→gemini-3.8-flash@prompt_v4`
- V4: `jpg→native→gemini-3.8-flash@prompt_v4`
- V5: `eml→text→gemini-3.8-flash@prompt_v4`

Configured default model `gemini-3.8-flash`; fallbacks used when a model's daily quota is exhausted: `gemini-3.6-flash`.

## Summary

- **Prices correct (base INR/pc within 1%):** 144/147 (98%)
- **Prices correct or the planted unit trap flagged and blocked:** 147/147 (100%)
- **Planted unit traps flagged (V2 'Rate/box' lines):** 3/3 (100%)
- **Expected flags found:** 223/223 (100%)
- **Not-quoted lines detected:** 3/3 (100%)
- **Unmatched vendor lines:** 0
- **Vendor qualification correct:** 5/5
- **Items to review:** 22 (from 495 open values; 159 must-confirm values could change a winner)

## Per vendor

| Vendor | Status | Price | Price or trap | Unit | Currency | Spec | Freight basis | Freight value | Expected flags found |
|---|---|---|---|---|---|---|---|---|---|
| V1 | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | n/a | 6/6 (100%) |
| V2 | 30/30 (100%) | 27/30 (90%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 36/36 (100%) |
| V3 | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 91/91 (100%) |
| V4 | 30/30 (100%) | 27/27 (100%) | 27/27 (100%) | 27/27 (100%) | 27/27 (100%) | 27/27 (100%) | 27/27 (100%) | n/a | 28/28 (100%) |
| V5 | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | 30/30 (100%) | n/a | 62/62 (100%) |

## Planted traps

| Vendor | Line | Trap | Flagged and blocked | Detail |
|---|---|---|---|---|
| V2 | 6 | unit trap: header says one unit, value is per 100 | YES | extracted 620 per_box -> INR 620.0; flags ['SANITY_RANGE_FAIL', 'UNIT_AMBIGUOUS'] |
| V2 | 8 | unit trap: header says one unit, value is per 100 | YES | extracted 944 per_box -> INR 944.0; flags ['SANITY_RANGE_FAIL', 'UNIT_AMBIGUOUS'] |
| V2 | 12 | unit trap: header says one unit, value is per 100 | YES | extracted 1229 per_box -> INR 1229.0; flags ['SANITY_RANGE_FAIL', 'UNIT_AMBIGUOUS'] |

## Questionnaire (rule engine result vs answer key) and qualification

| Vendor | Q01_ISO9001 | Q02_BS_TEST | Q03_REJECTION | Q04_CAPACITY | Q05_FIRST_SUPPLY | Q06_PAPER_SOURCE | Q07_FSC | Q08_COMPLAINTS | Qualification (got / expected) |
|---|---|---|---|---|---|---|---|---|---|
| V1 | pass | pass | pass | pass | pass | pass | fail | pass | OK qualified / qualified |
| V2 | unknown | pass | unknown | pass | pass | fail | fail | pass | OK pending / pending |
| V3 | pass | pass | pass | pass | fail | pass | pass | pass | OK qualified / qualified |
| V4 | unknown | unknown | unknown | unknown | unknown | unknown | unknown | unknown | OK pending / pending |
| V5 | fail | unknown | **unknown** (want pass) | pass | unknown | unknown | unknown | unknown | OK disqualified / disqualified |

Questionnaire cells correct: 39/40 (98%).

## Flags raised that the key does not list (review: noise or real finds)

- V1: SANITY_RANGE_FAIL x1
- V5: TAX_BASIS_UNSPECIFIED x30, SANITY_RANGE_FAIL x1

## Mismatches

None.
