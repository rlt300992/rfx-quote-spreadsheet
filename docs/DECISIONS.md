# DECISIONS.md

<!-- MORNING SUMMARY goes here when P3-P5 are done -->

Decision log for the unattended P3 → P4 → P5 run (started 2026-11-08). Each entry: what was decided and why.
Rules I was given: decide sensibly and log it; tests + commit + push after each phase; if blocked, log it and move on;
do not touch secrets or Streamlit settings; keep Gemini calls minimal (cache first; extraction stays on gemini-3.8-flash).

## P3: Review & Reconcile, Bid Analysis, Responses polish

| # | Decision | Why |
|---|---|---|
| P3-1 | Moved tab code out of `app.py` into a `ui/` package (`ui/review.py`, `ui/bid.py`, `ui/responses.py`); `app.py` is only the gate, banner and tab wiring. | CLAUDE.md: no business logic in page code. `app.py` was approaching 200 lines and every phase adds a tab. `ui/` is not in the original folder layout, so this is an addition. |
| P3-2 | Every flag, exclusion reason and field label has a plain-language explanation in `core/flags.py`, and a test fails if the pipeline can raise a flag that has none. | The brief says every trap must be visible *and explained*. One shared dictionary keeps Review, Bid Analysis and later the analyst consistent. |
| P3-3 | Review list is paginated at 15 rows per page per sub-tab, with a vendor filter and an "only items that could change the winner" filter. | 150 rows with ~8 widgets each makes Streamlit slow and unreadable. Counts in the sub-tab labels always reflect the whole set, not the page. |
| P3-4 | "Needs review" = quoted rows with at least one must-confirm or spot-check field. Not quoted and Unmatched are separate sub-tabs; All shows everything. | Matches the brief's four sub-tabs. A not-quoted line is shown as "Not quoted" and never as zero. |
| P3-5 | "Confirm all shown" lives in an expander with a visible warning that open flags are being accepted; it does not block, and all rows share one `batch_id`. | Brief: "warns but doesn't block". One batch id lets the audit trail show it was a bulk action. |
| P3-6 | Percentage volume discounts are NOT shown as toggles; only conditional (payment/other) discounts are. | Volume discounts are matched against order quantity automatically (D1). Toggling them would let the buyer override a verifiable fact. Conditional ones stay buyer-controlled (D2). |
| P3-7 | Added `actions.match_unmatched()` (reason required, audited, supersedes a layer-0 `not_quoted` placeholder). The real data has 0 unmatched lines, so it is exercised by a test with a synthetic row. | The Unmatched sub-tab needs an action or it is a dead end. |
| P3-8 | Per-vendor totals leave out lines whose unit is still ambiguous (`UNIT_AMBIGUOUS` with the unit field still must-confirm) and say how many were left out. | V2's three "Rate/box" lines (₹620, ₹944, ₹1,229) otherwise made V2's total ₹19.7 cr and its saving -300%. A total that silently absorbs a value the system itself doubts is misleading. They re-enter the total once the buyer resolves the unit. |
| P3-9 | Totals only cover lines a vendor priced and the screen says so. | A vendor that skipped lines would otherwise look cheaper. |
| P3-10 | Streamlit dataframes cannot show per-cell hover text, so "why it can't win" is a line-detail section (per vendor expander with reasons and the full "How computed" trace) plus legend icons in the grid (⚠ not firm, ✱ must-confirm open, ≈ freight imputed, ◆ unit doubt). | The brief asked for hover or expander; expander is what the framework supports. |
| P3-11 | Confidence chip: green >= 95%, amber 80-95%, red < 80%; shown overall and per open field. | Uses the core-field thresholds from config so the colours match the routing. |
| P3-12 | `v_comparison_grid` gained no new columns this phase; the UI reads flags from `field_meta_json` through existing columns. | Avoid schema churn while the UI settles. |
| P3-13 | Test strategy: UI tests run the real app headless (Streamlit AppTest) against a database rebuilt **offline from the committed extraction cache** (asserts 0 API calls). | Exercises real extracted data without spending quota, and proves the hosted app can start from the cache alone. |

**P3 blockers:** none.

