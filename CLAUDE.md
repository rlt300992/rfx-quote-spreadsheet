# CLAUDE.md — "Kill the Quote Spreadsheet" (Aerchain PM take-home)

Read this file fully before every phase. The full schema is in `docs/SCHEMA.md`, and it is the source of truth for tables, columns, views and tools.

## 1. What we are building
A Streamlit prototype for a category buyer at **Sahyadri Consumer Products Ltd** (fictional FMCG company, plant at Chakan MIDC, Pune). The buyer:
1. Drafts an RFQ for **30 corrugated box lines** plus an 8-question quality questionnaire with an AI co-pilot.
2. "Sends" it to 5 pre-approved vendors. Sending is stubbed.
3. Receives 5 messy replies: off-template Excel, PDF on letterhead, Word prose, an angled phone photo, and a terse email.
4. Gets every reply **extracted by Gemini, normalized to ₹/piece, validated and confidence-scored**, then reviews flagged values.
5. Compares the vendors side by side, then **asks questions in plain language** up to a defensible, line-wise award that the buyer confirms with a click.

**Judging criteria (from the brief):** ugly edges, **trust** ("would a buyer with ₹4 crore act on this screen?"), and judgment.
**The one hard rule:** stub the plumbing, but **the AI loops must be real.** Never fake extraction, never fake reasoning, never hardcode answers to demo questions.

## 2. Stack and conventions
- Python 3.11+, **Streamlit** UI, **SQLite** (one file, `rfx.db`), **pandas**, **openpyxl**, **python-docx**, **pypdf** (only if needed), **plotly** for charts.
- **Gemini via the official `google-genai` SDK** (`from google import genai`). The model ID lives in `config.yaml` (`gemini.model`, default `gemini-3.8-flash`). Never hardcode it elsewhere. If the key lacks access to it, switch config to another current Flash model.
- Secrets: `GEMINI_API_KEY` and `APP_PASSCODE` come from `.env` locally and `st.secrets` on Streamlit Cloud. Never commit them. `.env` is in `.gitignore`.
- Small, typed, pure functions. No business logic inside Streamlit page code; pages call `core/` functions.
- Every phase ends with tests passing, then `git commit` and `git push`.

## 3. Folder layout (create in P0)
```
app.py                    # Streamlit entry: passcode gate + 5 tabs
config.yaml               # copy of data/seed/config.yaml + gemini settings
core/  db.py  seed.py  convert.py  recompute.py  validate.py  confidence.py  views.sql  audit.py
extract/ preprocess.py  gemini_client.py  cache.py  extract.py  match.py  prompts/*.md
agent/  tools.py  analyst.py
copilot/ copilot.py
export/ excel.py
eval/   eval_extraction.py   # compares extraction to data/answer_key
tests/  test_convert.py  test_recompute.py  test_views.py
data/   seed/  inbox/  answer_key/  generators/
docs/   SCHEMA.md
```

## 4. Data (already generated; do not edit by hand)
- `data/seed/`: `rfqs.csv`, `vendors.csv` (V1–V5), `line_items.csv` (30 lines with theoretical weights), `rfx_questions.csv` (8, of which 3 are knock-outs), `last_year_prices.csv` (58 rows), `config.yaml`.
- `data/inbox/`: the 5 replies, 8 attachments and `manifest.json` (sender, received_at, files).
- `data/answer_key/`: the true values per vendor × line (150 rows) and the expected questionnaire results. **Used only by `eval/` and tests. App code must never read the answer key.**

## 5. Locked decisions (do not change without asking the user)
**Normalization**
- **Canonical comparison unit:** ₹ per piece, **freight included (FOR Chakan)**, **GST excluded**. The GST rate is stored separately; expected is 18%.
- **Units:** per_100 / per_1000 are divided out. per_kg = rate × `line_items.theoretical_weight_kg`, flag `DERIVED_PER_KG`, and the formula is shown to the buyer. The weight formula and all its constants are in config.
- **USD:** converted at the fixed, dated rate in config (₹95.17, 26 Oct 2026). The rate is snapshotted on the quote row.
- **"Same as last year":** look up `last_year_prices` (vendor + SKU). If last year's spec differs, flag `LY_SPEC_CHANGED` (must_confirm). If there is no last-year row, flag `LY_REF_MISSING`.
- **Freight:** quoted → use it. Unquoted → impute the **highest** freight quoted by another vendor for that line (`freight_imputed=1`), else last year's freight (`=2`), else **NULL, the line is excluded as `freight_unknown`, and it goes into the clarification email. Never invent a number (D4).**
- **D1 price tiers:** match against `typical_order_qty`, not annual quantity, unless the vendor explicitly says annual. Unclear basis → per order (the higher price) plus `TIER_BASIS_UNCLEAR`.
- **D2 conditional discounts:** extract them with their conditions but **never auto-apply**. The buyer toggles each one; each toggle is audited and triggers a recompute.
- **D3 MSME:** for micro and small vendors, effective credit days = MIN(quoted, 45), flag `MSME_TERMS_CAPPED`. A declared category in the reply that differs from the vendor master → `MSME_CATEGORY_MISMATCH` (spot_check); keep using the master value.
- **D5 payment loading:** `evaluated = landed + one-time charges/annual_qty + landed × cost_of_funds × (std_days − MIN(eff_days, std_days+30)) / 365`. cost_of_funds = 10.9% from config; std_days = 60 from `rfqs`. **Ranking uses the evaluated price**, and the grid shows both the quoted (landed) and the evaluated price.

**Pipeline (8 steps, in order)**
1. **Preprocess, convert-first:** xlsx → cell-addressed text (`Sheet!E12: 887`); docx → Markdown with paragraph numbers; eml → text. PDF and JPG go to Gemini natively. Log the path in `vendor_responses.extraction_model`, e.g. `xlsx→celltext→gemini-3.8-flash@prompt_v3`.
2. **Extract** with Gemini **structured JSON output**. Capture all tiers, discounts with their conditions, charges, validity, version, freight text, GST basis and rate, terms, capacity, the vendor's spec, and **a source reference for every value** (cell, page and snippet, or a photo row).
3. **Match** each vendor line to a `line_id` by description, size and ply. Vendor row numbers are **not** our line numbers. Set `spec_match` to exact / deviation / substitute.
4. **Layer 0, completeness:** insert `not_quoted` rows for missing lines **before** any ranking. Never treat a missing line as ₹0.
5. **Normalize** via `convert()` and `recompute_quote()` only. **Raw columns are never overwritten.**
6. **Validate, layers 1–4:** arithmetic; master-data match; sanity (±25% vs last year and vs other vendors' median, which catches "Rate/box" values that are really per 100); **second read** (re-extract the 4 core fields with a different prompt; disagreement → flag).
7. **Confidence from signals, not the LLM's self-report.** Base score by source type (config), raised by passed checks, capped below must_confirm by any failed check or must-confirm flag.
8. **Route per field:** core fields (price, unit, currency, freight, tax_basis) auto ≥95%, spot-check 80–95%, must_confirm <80%. Terms fields (lead_time, moq, payment_terms) 85 / 70. must_confirm values are **excluded from the award** until confirmed.

**Plus:** a **light clarification loop**. One drafted email per vendor lists its flagged items. Sending is stubbed; the buyer resolves replies manually, and each resolution is audited.

**Questionnaire:** Gemini only **extracts** answers; a **rule engine** judges pass / fail / unknown using `rfx_questions.rule_type` and `rule_params_json`. A missing or unknown knock-out answer → vendor **pending, not qualified**. A required document that is missing, expired on the award date, or held by a different company → fail or must_confirm.

**Agent rules**
- The analyst agent queries **only the views** (read-only SELECT via `query_view`) and calls Python tools: `award_scenario()`, `propose_award()`.
- **The agent never commits.** The buyer clicks **Confirm award** in the UI, which calls `commit_award()`. That requires a reason for any line where the award ≠ cheapest eligible, and writes one audit row per line with a shared batch_id.
- Answers must cite the data they used (vendor, line and figures) and say when values are still unconfirmed.

**Caching and hosting**
- Extraction results are cached by **(file SHA-256, prompt version, model)**. The app loads from the cache; a **"Re-run extraction"** button forces a fresh call. Commit the cache JSON to the repo so the hosted app works without API calls.
- Hosting: private GitHub repo, with a **public** Streamlit Community Cloud app. A passcode screen (`APP_PASSCODE`) and a chat rate limit (e.g. 30 messages per session) protect the key.

## 6. UI: 5 tabs (industry-standard flow: create → responses → review → analyze → award)
1. **Create RFQ:** co-pilot chat (Gemini) pre-fills the 30 lines, the 8 questions and the terms from the seed catalog. The buyer edits and confirms. "Send to 5 vendors" logs a stub. Keep it light.
2. **Responses:** one card per vendor showing format, received time (V4 is **late**), files, extraction status, lines quoted out of 30, and a Re-run button.
3. **Review & Reconcile:** sub-tabs **All / Needs review / Not quoted / Unmatched, each with a count**. Each row shows our line on the left, the vendor's raw value and the normalized value on the right, the confidence chip, the flags, and **the source evidence** (cell text, snippet, or a crop or the full photo). Actions: Confirm, Correct (with a reason), Accept spec, Toggle discount. Each action writes an audit row and recomputes. Confirming with open flags **warns but doesn't block**.
4. **Bid Analysis:** line × vendor grid of evaluated ₹/pc. Cheapest *eligible* vendor highlighted, flag icons, and a hover or expander for **"how this was computed"** (`normalization_trace_json`) and **"why it can't win"** (`exclusion_reason`). A vendor qualification panel with documents and validity. Totals per vendor and savings vs last year.
5. **Analyst & Award:** chat over the views and tools, answering with text, tables and **plotly charts**. Proposed scenario → award panel → **Confirm award** → **Excel export** of the award sheet, the scenario, and the audit trail. A **Clarification emails** expander lists the drafted email per vendor.

Visible on every screen: "**X values awaiting confirmation could change the award**".

## 7. Planted traps the app must handle (the demo depends on these)
| Vendor | Traps |
|---|---|
| V1 Excel | Priced per 100; their own codes and ordering; merged cells; line 16 deviation (150 GSM outer); line 25 substitute (3-ply instead of 5-ply); a 4% tier needing 20,000 per order (not met per order); line 19 "3% above 50,000" with unclear basis |
| V2 PDF | **"Rate/box" header, but lines 6, 8 and 12 are per 100** (₹620, ₹944, ₹1,229); 2% footnote discount (15-day payment); 90-day credit, but the vendor is small, so it's capped at 45; ₹2,500 per colour plates; ISO held by "Shree Ganesh **Group Holdings**"; Q03 conditional; freight ₹0.60 |
| V3 Word | **USD**; 7-ply prices **in prose, not the table**; GST 12%; price linked to kraft paper (not firm); line 10 capacity 8,000/month; freight per ply in prose; Q05 fails (18 days) |
| V4 photo | Angled; **27 of 30 lines** (missing 26, 28, 29); **its row numbers ≠ ours** (match by size and ply); line 14 smudged; ex-works with no freight; **late** (17 Nov vs 16 Nov due); no questionnaire → pending |
| V5 email | "₹42/kg for the 5-ply, 38 for the 3-ply, rest same as last year, freight extra"; the 7-ply lines come from last year, with **lines 28 and 30 having a changed spec**; ISO **expired** 31 Mar 2026 → disqualified. V5 is the cheapest on most lines but can't win |

**Expected qualification:** V1 and V3 qualified, V2 and V4 pending, V5 disqualified.

## 8. Out of scope (state this in the note; don't build it)
Percentage dual-sourcing within a line (phase 2; the chat may *suggest* the L2 vendor as backup); a kraft-paper escalation formula; reading vendor clarification replies automatically (the standard loop, phase 2); multi-category specs (phase 2: `spec_json` plus a `convert()` per category); supply terms (palletisation, tolerances, liquidated damages); real email sending.

## 9. Definition of done for any change
Tests pass, the app runs locally, nothing in the app reads the answer key, there are no hardcoded demo answers, and you commit and push.
