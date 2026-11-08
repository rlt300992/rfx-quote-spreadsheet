# DECISIONS.md

## Morning summary (2026-11-08)

**P3, P4 and P5 are built, tested, committed and pushed.** 172 tests pass (was 102). Nothing blocked. I did not touch secrets, `.streamlit/` or `CLAUDE.md`, and extraction stayed on `gemini-3.8-flash` (no extraction calls were made; everything ran from the committed cache).

| Phase | Commit | What you now have |
|---|---|---|
| P3 | `5d8ffac` | Review & Reconcile (All / Needs review / Not quoted / Unmatched with counts, evidence, Confirm / Correct / Accept spec / Toggle discount, bulk confirm that warns but never blocks), Bid Analysis (landed vs evaluated toggle, winner highlight, strike-through for quotes that can't win, per-quote "how computed", totals, savings vs last year, qualification panel), shared plain-language explanation for every flag |
| P4 | `c0e2eb0` | Analyst & Award: sandboxed `query_view`, `award_scenario` (incl. the 20,000-per-release what-if), `propose_award`, Gemini tool loop with tables and plotly charts, Award panel with **Confirm award** (reason required when not cheapest eligible, one audit row per line, shared batch id), Excel export (Award, Scenario, Audit trail, Assumptions), clarification emails with Mark sent (stub), 30-message session limit |
| P5 | `9b6352d` | Create RFQ co-pilot: chat edits the terms, 30 lines and 8 questions through validated operations; Confirm RFQ; Send to 5 vendors (stub); warning when editing after send |

### Please look at these first (in this order)
1. **Review & Reconcile, vendor V2, Needs review:** the three "Rate/box" lines show UNIT_AMBIGUOUS with an explanation. Use **Correct -> Unit -> per_100** with a reason and watch the price fall to ₹6.20 and an audit row appear.
2. **Bid Analysis:** ⚠ on V3's cells, the warning about ₹2.68 cr resting on not-firm prices, V5's line-1 detail ("Why it can't win"), and the qualification panel.
3. **Analyst & Award:** ask your six questions. Then "propose the cheapest award among qualified vendors", press **Confirm award**, and download the Excel (it opens with 4 sheets; A1 of the Award sheet says whether it is a committed award or a draft).
4. **Create RFQ:** "make payment 45 days", "drop the promo box" (it will be applied in the draft, and **Confirm RFQ will refuse to delete line 26** because five vendors quoted it; that is deliberate, see P5-4).

### Things I want you to know (honest list)
- **The analyst is not deterministic.** In my first live run it explained V1's/V3's price gap using payment terms and **swapped them** (V1 is 60 days, V3 is 30). The figures were right, the explanation was invented. I added a system-prompt rule and the second run was correct, but a rule is not a guarantee. My automated checks only confirm the right numbers appear; I also read the answers. Treat chat answers as a helper and confirm against the Bid Analysis grid. Full answers are in `docs/ANALYST_LIVE_CHECK.md`.
- **Bugs the tests caught in my own code and I fixed:** NaN crashing `explain_reason`; NaN crashing `award_review` on lines with no winner; V2 totals swallowing the unresolved ₹620/₹944/₹1,229 lines (now left out and counted); a second-read comparison against corrected values (P2).
- **Not verified:** I ran every screen headless (Streamlit AppTest) against the real extracted data, never in a browser, so layout, spacing and the feel of the 15-row pages are unchecked. The public Streamlit Cloud deployment is untested. The analyst chat and the RFQ co-pilot need `GEMINI_API_KEY` in the hosted secrets (I did not touch them).
- **Gemini usage:** about 75 calls in P4 (two full runs of the six questions plus a smoke test) and about 9 in P5. Tier 1 billing was on throughout.
- **Existing local `rfx.db`** is not tracked and was migrated in place (audit table). Tests use copies rebuilt from the cache, so they never touch it.

### Decisions you may want to reverse (each is logged below with its reason)
- P3-8: per-vendor totals leave out lines whose unit is still unresolved (`UNIT_AMBIGUOUS`).
- P4-3: 6 analysis + 3 presentation tool calls per question.
- P4-8: one shared reason + code for all non-cheapest-eligible lines (not one per line).
- P5-4: a line vendors already quoted cannot be deleted from the RFQ. A proper "withdraw this line from evaluation" would be a schema change.
- `PRICE_NOT_FIRM` remains a flag, not an exclusion (your earlier call); it is now shown in the grid, the award panel and the Excel.

### Known gaps / suggested next steps (P6)
- V5's Q03 ("Rejection ok") stays *unknown* (90% confidence, below the 95% knock-out minimum). It does not change V5's outcome.
- No "withdraw line" feature; no per-line bypass reasons; Excel is not re-checked after you correct values (it is regenerated on each download).
- P6: hardening, deploy, demo script. First thing I would do is open the app in a real browser and fix whatever looks wrong.

---


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


## P4: Analyst chat, award and export

| # | Decision | Why |
|---|---|---|
| P4-1 | Removed the hard-coded `CHECK` on `audit_log.action` (the allowed list now lives in `core.db.AUDIT_ACTIONS` and `audit.log()` rejects unknown actions). `init_db` migrates an existing database in place: rename, recreate, copy rows, restore the append-only triggers (tested on a copy of the real `rfx.db`). | P4 and P5 need new actions (`clarification_sent_stub`, `rfq_confirm`, `rfq_send`). SQLite cannot alter a CHECK, so a table rebuild on every new action would be fragile. Validation moved to code. |
| P4-2 | `query_view` runs against an **in-memory snapshot that contains only copies of the six views** (as tables), with `query_only` on, an authorizer that allows only SELECT/READ/FUNCTION, one statement, no comments, 500-row cap. | Stronger than regex-checking table names: base tables, the audit log and the answer key do not exist inside the sandbox, so no query trick can reach them. 11 attack-style queries are tested. |
| P4-3 | Tool budget per question: **6 analysis calls** (`query_view`, `award_scenario`, `propose_award`) plus **3 presentation calls** (`render_table`, `render_chart`). Over-budget calls get an error telling the model to answer now. | The brief says max 6 tool calls. Charts/tables are presentation, not analysis, and counting them would starve the answers. If the loop never produces text, one final tool-less call forces an answer. |
| P4-4 | Tables and charts are **built in Python** from a SELECT the model asks for (`render_table`, `render_chart` with kind/x/y/color). The model never writes plotting code. | Deterministic, safe, and the figure always matches the data shown. |
| P4-5 | `order_qty_override` re-prices every line in a **throwaway in-memory copy** of the database (backup, set `typical_order_qty`, `recompute_all`), then discards it. | It re-matches volume tiers/discounts with the real pipeline instead of approximating. Nothing in the real DB changes (tested). |
| P4-6 | The model has **no commit tool** (a test asserts the tool list). `propose_award` only stages a proposal in `st.session_state`; `commit_award` runs from the Confirm award button. | CLAUDE.md: the agent never commits. |
| P4-7 | `commit_award` is all-or-nothing, re-checks every quote is still current/on its line/awardable (stale proposals are refused), and writes one audit row per line under one `batch_id` with the candidate list and exclusion reasons in `scenario_json`. | Brief + trust: a proposal made earlier must not silently commit a quote that has since become ineligible. |
| P4-8 | `l1_quote_id` = cheapest current quote of any kind (even a disqualified vendor); `l1_eligible_quote_id` = cheapest awardable quote. Bypass = awarded != l1-eligible. One shared reason text + one reason code (default `buyer_judgment`) applies to all bypass lines in the panel. | Matches SCHEMA 2.3. A per-line reason form for up to 30 lines is clutter; the table lists exactly which lines need it. |
| P4-9 | Added `unaward()` (reason required, one audit row per line). | A committed award with no way back is not usable; also needed for the tests. |
| P4-10 | Excel shows, in priority order: the committed award, else the proposal, else the current best scenario, and **labels which one** in cell A1 (red if not committed). Sheets: Award, Scenario (every candidate with a plain-language exclusion reason), Audit trail, Assumptions (FX, cost of funds, thresholds, dates, model and prompt versions, out-of-scope list, flag glossary). It also lists open must-confirm values and not-firm / imputed-freight lines. | The brief names the four sheets; an export that could be mistaken for a final award while it is only a draft would be a trust problem. |
| P4-11 | Session rate limit: 30 user messages, read from `config.yaml` (`chat.max_messages`), shown as "N of 30 left"; the input is disabled at 0. | Protects the API key on the public app; kept in config rather than hard-coded. |
| P4-12 | Chat memory sent to the model = last 8 turns as plain text only (no old tool traces). | Keeps latency and cost down; each question re-queries the data so answers can't go stale. |
| P4-13 | No "suggested question" chips in the UI. | You asked that the demo questions are not hard-coded anywhere; chips would look like they are. |
| P4-14 | The analyst uses the configured model (`gemini-3.8-flash`) through the same fallback chain as extraction. | Rule: extraction/model stays on gemini-3.8-flash. |
| P4-15 | "Mark sent (stub)" sets `clarification_status = sent_stub` and writes a `clarification_sent_stub` audit row; the expander lists only current (non-superseded) responses' drafts. | Found a duplicate draft from the earlier V4 re-run showing as "6 drafted" for 5 vendors. |

**Bugs found by the P4 tests and fixed:** `explain_reason` crashed on NaN (pandas gives a float for "no reason"); `award_review` crashed on lines with no winner (NaN quote id).

**Live check (real Gemini, `docs/ANALYST_LIVE_CHECK.md`):** all six acceptance questions answered; for each I computed the ground truth from the database and checked the figures appear in the answer (scenario total, per-vendor values, the 20,000-per-release total, the total excluding V3, the chart being produced). All pass. **Caveat on those checks:** they only test that the right numbers appear; I also read the answers. Reading found one real problem the checks could not: the first run explained V1/V3 price differences with payment terms and **swapped** them (said V1 30 days, V3 60; data says V1 60, V3 30). The numbers were right, the story was invented. Fix: a system-prompt rule not to state causes unless the column was queried, plus payment-term columns documented in the glossary. Re-run: correct. LLM answers vary run to run, so this is a check, not a guarantee; the analyst should still be treated as a helper whose figures the Bid Analysis tab can confirm.

**Gemini calls spent in P4:** about 75 (one smoke test, then two full runs of the six questions at 3-8 calls each).

**P4 blockers:** none.

## P5: Create RFQ co-pilot

| # | Decision | Why |
|---|---|---|
| P5-1 | The co-pilot **does not write the draft**. Gemini returns a short reply plus a list of edit operations from a fixed vocabulary (`set_term`, `set_line`, `drop_line`/`restore_line`, `set_question_text`, `set_question_knockout`, `drop_question`/`restore_question`); `apply_ops()` validates each one and applies it, and returns a note per operation ("Applied: ..." or "Not applied: why"). | The model can only change what the buyer's own tables can change. It cannot invent a line, a quantity or a question. Invalid values (negative quantity, GST 40%, "next week" as a date, unknown line) are refused with a reason and change nothing (10 parametrised tests). |
| P5-2 | Editable: the five terms (payment days, delivery, GST %, due date, award reference date), per line the description / annual qty / typical order qty / print colours / include, per question the text / knock-out / include. **Not editable and not addable: SKU, dimensions, ply, new lines.** The co-pilot says so plainly. | Each line's dimensions and layers drive the weight formula (per-kg prices) and the spec comparison, and the 30 lines come from the catalogue. Letting a chat add a line would create a line no vendor has priced. In the live test the co-pilot first told the buyer to "edit the table" for a new line, which is impossible; I corrected the prompt and re-checked. |
| P5-3 | On first load the draft is the RFQ **as saved in the database** and the greeting is static text, so opening the tab costs no Gemini call. The chat calls Gemini only when the buyer sends a message. | "Propose the 30 lines, 8 questions and terms from the seed" is satisfied by showing the seeded catalogue; spending a call to paraphrase it adds nothing. Keeps calls minimal. |
| P5-4 | **Confirm RFQ refuses to delete a line or question that vendors already quoted or answered.** It keeps it and shows a warning saying why. Lines/questions with no vendor data can be deleted. | Deleting line 26 would orphan five extracted quotes (and break the comparison grid). The brief's "drop the promo box" is exactly this case once replies exist, so the behaviour is reported, not hidden. A true "withdraw this line from evaluation" feature would be a bigger change (new state in the schema) and is listed in the morning summary as a follow-up. |
| P5-5 | If replies exist, Confirm re-runs the questionnaire rules and `recompute_all`. | Payment days feed the payment-terms loading, typical order qty feeds the volume tiers, the award reference date feeds certificate validity, and knock-out flags feed qualification. Tested end to end: setting payment to 45 days changes V1's evaluated price; making FSC a knock-out disqualifies V1 (no FSC certificate) and leaves V3 qualified. |
| P5-6 | **Send to 5 vendors is a stub**: it writes the RFQ email body to `rfqs.rfq_body`, sets `issued_at`, writes one `rfq_send` audit row listing the five vendors, and shows "Sent (stub) ... No email was actually sent." It is disabled while there are unsaved edits. It does not downgrade an `evaluating` or `awarded` RFQ back to `issued`. Re-sending is allowed and audited again. | Brief: sending is stubbed. Forcing a Confirm first means the body always matches what is saved. |
| P5-7 | The RFQ email body is a **deterministic template** built from the saved data (terms, 30 lines, questionnaire with knock-outs marked). No Gemini call. | Predictable and testable; the buyer reviews it in an expander. |
| P5-8 | `buyer_confirmed = 1` is set on lines only. `rfx_questions` has no such column in SCHEMA.md, so I did not add one. | The prompt says lines and questions; the agreed schema only has it on `line_items`. Adding a column for a flag nothing reads is churn. Confirmation of the questions is recorded in the `rfq_confirm` audit row. |
| P5-9 | Edits made after the RFQ was sent or answered show a **warning**: replies exist, edits will not change what vendors quoted, prices will be recomputed, answered lines/questions cannot be removed. Sanity warnings (GST != 18%, date order, no knock-out left, order qty above annual qty) are shown but never block. | Brief: "editing after the send should show a warning". |
| P5-10 | The editors use a version counter in their widget keys: after the chat changes the draft the editors are re-keyed so they show the new data instead of stale edit state. Chat memory sent to the model is the last 6 turns. Failures from Gemini are shown in the chat, not swallowed. | Streamlit's `data_editor` keeps its own edit state, which fights programmatic changes unless re-keyed. |

**Live check (real Gemini):** five requests. "Make payment 45 days" -> `set_term`. "Drop the promo box" -> `drop_line 26`. "Drop the detergent box" -> **no ops, asked which of four matching lines**. "Add a new line for pallet covers" -> refused (after the prompt fix: "not supported in this version"). "Make the response due date a week later and make Q05 a knock-out" -> computed 2026-11-23 and applied both. About 9 Gemini calls in total including the re-check.

**Also fixed in this phase:** one P4 test selected the first chat input on the page; with the RFQ chat now on an earlier tab it picked the wrong one. It selects by key now.

**P5 blockers:** none.

## Hotfix: cold start on Streamlit Cloud (Python 3.14)

| # | Decision | Why |
|---|---|---|
| H-1 | `get_conn()` now calls `ensure_quotes()`: if `quotes` is empty it runs `ingest_all(only_missing=True)` from the committed cache (0 API calls) under a spinner. `get_conn` is `st.cache_resource`, so once per container. If the cache is missing it logs and starts empty. | `rfx.db` is gitignored, so the cloud container only seeded and never ingested. |
| H-2 | `build_workbook`/`_award_frame` return an empty "NO AWARD PROPOSED YET" sheet when there are no scenario rows. The Excel is built only when the download is clicked (callable `data`); with no quotes the button is disabled with a note. | Empty frame raised KeyError in `_award_frame`. |
| H-3 | Each tab render and the banner count are wrapped in try/except; failure logs the traceback and shows a warning in that tab only. | One broken tab must not take down the others. |
| H-4 | Python: developed and tested on 3.13.12 (175 tests pass). **3.14 could not be run locally (not installed)**; requirements are unpinned so the cloud resolves 3.14 wheels. Only pure-Python/wheel deps are used. If a package fails on 3.14, set Python 3.13 in the Streamlit Cloud advanced settings. | Honest: 3.14 is unverified here. |

## UI batch 2: header, banner, Responses

| # | Decision | Why |
|---|---|---|
| B2-1 | Header chip is derived (`core/stage.py`): Awarded if any line has a committed award, Evaluating if any vendor's reply is extracted, else Draft/Published from `rfqs.status`. | Demo loads as Evaluating. |
| B2-2 | One banner, most urgent first: failed > responses missing > N awaiting confirmation > award confirmed > award proposed > all confirmed. | Awarded with open values still warns. The "enter values manually in Responses" wording was replaced because no such feature exists; the banner points to Re-extract and to Review & Reconcile. |
| B2-3 | "Re-extract all" and the per-card ↻ call the existing `ingest_all/ingest_vendor(force=True)`: fresh Gemini calls, old quotes superseded, so confirmations and corrections on them are lost (verified in `_supersede`). The old "Extract all" only filled gaps and never wiped; the new one does what the dialog says. | Wording matches behaviour. |
| B2-4 | Dialog open state lives in session_state (cleared on dismiss) so dialogs work in a full rerun. | Also testable headless. |
| B2-5 | "Review extracted values →" sets the Review vendor filter and selects the tab through the tabs `key`; a toast says "Filter set to V1. Open Review & Reconcile ↑" as the fallback. `bind="query-params"` not used because it makes non-selected tabs lazy. | Keeps every tab rendering eagerly. |
| B2-6 | Clarification emails moved to the Responses card (editable, Send (stub) writes the final text to the audit log and locks it); removed from Analyst & Award. | One place for it. |
