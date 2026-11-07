# PROMPTS.md: paste one phase at a time into Claude Code

How to use this file:
- Start Claude Code in the project folder. It reads `CLAUDE.md` automatically.
- Paste one phase, let it finish, then run the **"You check"** steps yourself before moving on.
- If a check fails, tell Claude Code exactly what you saw ("Line 6 for V2 shows ₹620/pc; it should be flagged as per 100"). Don't start the next phase until the current one passes.

Time budget (about 14h): P0 0.5h · P1 2h · P2 3–4h · P3 2h · P4 2.5h · P5 1h · P6 3h

---

## P0: Setup and deploy an empty app first (about 30 min)
```
Read CLAUDE.md and docs/SCHEMA.md fully. Then do phase P0 only:
1. Create the folder layout from CLAUDE.md §3, plus requirements.txt (streamlit, pandas, openpyxl, python-docx, pypdf, plotly, pyyaml, python-dotenv, google-genai, xlsxwriter, pytest), .gitignore (.env, rfx.db, __pycache__, .venv, .streamlit/secrets.toml) and .env.example (GEMINI_API_KEY=, APP_PASSCODE=).
2. Copy data/seed/config.yaml to ./config.yaml and add a `gemini:` section (model: gemini-3.8-flash, temperature: 0, prompt_version: v1).
3. app.py: a passcode gate (APP_PASSCODE from st.secrets or .env), then 5 empty tabs named exactly: Create RFQ · Responses · Review & Reconcile · Bid Analysis · Analyst & Award. A sidebar showing the RFQ title, the buyer and the config dates (FX rate and date, cost of funds).
4. A tiny Gemini smoke test (extract/gemini_client.py, function ping()) that returns the model name, plus a sidebar button "Test Gemini".
5. git init, first commit. Tell me the exact commands to create a PRIVATE GitHub repo and push (use `gh` if it's installed), and the exact steps to deploy on share.streamlit.io: main file app.py, secrets GEMINI_API_KEY and APP_PASSCODE, then switch the app to Public in its share settings.
Stop after P0 and summarise.
```
**You check:** the app opens locally, the passcode works, "Test Gemini" replies, and the **live Streamlit link** works. Save that link.

---

## P1: Database, seed, normalization and views, with no AI yet (about 2h)
```
Phase P1 only. Build the data core from docs/SCHEMA.md:
1. core/db.py: create all 10 tables exactly per SCHEMA.md §2 (SQLite, with foreign keys, partial unique indexes on is_current), plus core/views.sql with the 6 views in §4. v_vendor_qualification must be TRI-STATE (qualified / pending / disqualified; a missing or unknown knock-out answer = pending).
2. core/seed.py: load data/seed/*.csv. It must be idempotent and run on app start if the DB is empty.
3. core/convert.py: convert(raw, line_spec, category='corrugated') → (inr_per_unit, method, trace), covering per_piece / per_box / per_100 / per_1000 / per_kg (weight formula from config) / USD.
4. core/recompute.py: recompute_quote(quote_id) is the ONLY writer of normalization and validation columns. It handles FX, tiers (D1), discounts only when applied (D2), MSME cap (D3), freight imputation order (D4), one-time charges, payment loading (D5), landed and evaluated prices, normalization_trace_json, award_eligible and ineligible_reasons_json. Also recompute_all().
5. core/validate.py + core/confidence.py: the layers 0–4 checks (layer 4 = compare to second_read_json if present), signal-based confidence, per-field routing with the config thresholds, and field_meta_json, min_confidence and review_state.
6. core/audit.py: log(action, entity, field, old, new, reason, evidence, batch_id).
7. A DEV-ONLY loader, tests/fixtures_from_answer_key.py, that inserts quotes from data/answer_key as if extracted. Use it ONLY in tests to verify the maths. Never import it from app code.
8. pytest tests: per_100, per_kg and USD conversions; V2 lines 6/8/12 trip the sanity check; MSME cap on V2 (90 → 45); freight imputed for V4/V5; V4 has 3 not_quoted rows; qualification → V1 and V3 qualified, V2 and V4 pending, V5 disqualified; award_scenario returns a winner per line that never picks V5 or a not_quoted row.
9. Show a read-only Bid Analysis grid from v_comparison_grid, populated in a test run only.
Commit and push. Report the test results.
```
**You check:** `pytest` passes. Spot-check one V5 per-kg line by hand: rate × weight from `line_items.csv`.

---

## P2: Real Gemini extraction (about 3–4h, the core)
```
Phase P2 only. Build the real extraction pipeline per CLAUDE.md §5 steps 1–8:
1. extract/preprocess.py (convert-first): xlsx → "Sheet!Cell: value" lines including merged cells; docx → Markdown with numbered paragraphs and tables; eml → headers + body; pdf and jpg passed as files to Gemini.
2. extract/prompts/: quote_extract_v1.md (a strict JSON schema: header terms, lines[], tiers, discounts with conditions, charges, freight, tax basis and rate, payment days, validity, price basis / RM clause, declared MSME category, questionnaire answers, attached-document facts, and a source_ref for EVERY value), second_read_v1.md (only line ref + price + unit + currency, different wording), doc_extract_v1.md (certificates and test reports: holder, number, issue/expiry dates, scope, site).
3. extract/gemini_client.py: use google-genai with response_mime_type=application/json and a response schema; retries with backoff; timeout. Read the model from config.
4. extract/cache.py: cache by (sha256 of file, prompt_version, model) under cache/extractions/*.json. COMMIT the cache folder.
5. extract/match.py: map vendor lines to line_id by size (LxWxH, tolerate spacing and "x" variants), ply and description similarity. Never use the vendor's row number. Unmatched → quote_status='unmatched'.
6. extract/extract.py: ingest(manifest entry) → vendor_responses + quotes + questionnaire_answers + documents; then layer-0 completeness, recompute_all(), the rule engine for questionnaire answers, and the clarification draft per vendor (one Gemini call that lists that vendor's flagged items in a polite email).
7. eval/eval_extraction.py: compare DB quotes to data/answer_key/quotes_answer_key.csv. Print per-vendor accuracy for true_base_inr_pc (±1%), unit, currency, quote_status and expected flags, plus the questionnaire results. Save to eval/report.md.
8. Wire the Responses tab: vendor cards, "Extract all" and "Re-run extraction" buttons, status and counts.
Run the full extraction on all 5 vendors, run eval, and show me the report. Then fix the worst errors by improving PROMPTS (never by special-casing a vendor in code). Bump prompt_version when prompts change. Commit and push, including the cache.
```
**You check:** `eval/report.md`. Aim for **≥ 90% of prices correct** and **every planted trap flagged**. Any trap that isn't flagged gets fixed before P3.

---

## P3: Review & Reconcile, Bid Analysis, Responses polish (about 2h)
```
Phase P3 only. Build tabs 2–4 per CLAUDE.md §6:
- Review & Reconcile: sub-tabs All / Needs review / Not quoted / Unmatched with counts. Each row shows our line (spec) on the left; vendor raw text, raw value and unit, normalized ₹/pc, a confidence chip (green/amber/red) and flags on the right; and an "Evidence" expander (cell text or snippet; for the photo, show the image). Actions: Confirm, Correct (requires a reason), Accept spec, Toggle discount. Each writes audit_log and recomputes. A "confirm all with open flags" action warns but doesn't block.
- Bid Analysis: grid of evaluated ₹/pc, lines × vendors. Cheapest eligible highlighted; a struck-through or greyed cell for ineligible quotes with the exclusion_reason on hover/expander; a "How computed" expander showing normalization_trace_json; a toggle Quoted (landed) vs Evaluated; totals per vendor; savings vs last year; and a qualification panel (tri-state, knock-out issues, documents with validity on the award date).
- A global banner on every tab: "N values awaiting confirmation could change the award".
Keep it clean and readable: wide layout, consistent colours, no clutter. Commit and push.
```
**You check:** walk through every trap in CLAUDE.md §7. Each should be visible **and explained** on screen.

---

## P4: Analyst chat, award and export (about 2.5h)
```
Phase P4 only. Build the Analyst & Award tab:
1. agent/tools.py: query_view(sql) (SELECT only, views only, row limit 500); award_scenario(basis='evaluated', exclude_vendors=[], require_qualified=True, include_pending=False, include_accepted_substitutes=False, order_qty_override=None) → per-line winner + L2 backup + totals + savings vs last year; propose_award(scenario) → stores the proposal in session state for the UI. Expose them to Gemini via function calling, with clear tool descriptions and the view schemas.
2. agent/analyst.py: a tool-calling loop (max 6 tool calls per question). System prompt: answer only from tool results, cite vendors, lines and numbers, state when values are unconfirmed or imputed, never invent numbers, suggest a chart when it helps. Return text plus optional tables and plotly specs that the UI renders.
3. UI: chat history; render tables and plotly charts; an Award panel showing the proposed scenario with a Confirm award button → commit_award() (requires a reason for each line where the award ≠ L1-eligible; one audit row per line with a shared batch_id; sets awarded_quote_id, l1_quote_id, l1_eligible_quote_id, bypass_reason_code).
4. export/excel.py: Award sheet, Scenario sheet (candidates and exclusion reasons), Audit trail sheet, and an Assumptions sheet (FX, cost of funds, thresholds, dates). A download button.
5. A "Clarification emails" expander showing the per-vendor drafts with a "Mark sent (stub)" button.
6. A session rate limit of 30 chat messages.
Test these questions live (they must NOT be hardcoded anywhere):
- "What if we split it, cheapest per line, but only among vendors who cleared the quality questionnaire?"
- "Which values are still unconfirmed, and could any of them change the winner?"
- "Why didn't V5 win line 1? It's the cheapest."
- "Show savings vs last year by vendor as a chart."
- "What if we ordered 20,000 per release instead? Does anything change?"
- "Exclude V3. What's the total and who's the backup per line?"
Commit and push.
```
**You check:** the answers match the Bid Analysis grid, Confirm award writes the audit rows, and the Excel file opens with all 4 sheets.

---

## P5: Create RFQ co-pilot, light (about 1h)
```
Phase P5 only. Build the Create RFQ tab:
- A chat co-pilot (Gemini) that helps the buyer draft the RFQ: it proposes the 30 lines from the seed catalog (line_items), the 8 questions and the terms (payment days, delivery FOR Chakan, GST 18%, due date). The buyer can ask for changes in chat ("drop the promo box", "make payment 45 days") and the co-pilot updates an editable table (st.data_editor) for the lines and the terms.
- "Confirm RFQ" saves to rfqs, line_items and rfx_questions (buyer_confirmed=1). "Send to 5 vendors" writes the stubbed email body (rfq_body), logs to audit_log, and shows "Sent (stub)".
- The demo data is already the confirmed RFQ, so make sure the tab works without breaking the existing responses: editing after the send should show a warning.
Commit and push.
```
**You check:** you can talk the RFQ into shape and the rest of the app still works.

---

## P6: Hardening, deploy and demo prep (about 3h, including your video and deck)
```
Phase P6 only:
1. Run eval again and paste the final accuracy into eval/report.md.
2. Error handling: Gemini failures show a friendly message and fall back to the cache; no stack traces in the UI.
3. A "Reset demo" button in the sidebar that rebuilds the DB from seed + cache.
4. A performance check: app cold start under 10 s from the cache.
5. README.md: what it is, how to run, architecture diagram (Mermaid), the key decisions (from CLAUDE.md §5), what was left out (§8), and the eval accuracy.
6. Push and redeploy. Confirm the public link works in an incognito window with the passcode.
```
**You check:** the incognito test passes. Then record the Loom video (6–8 min) and make the deck. Bring both back to the chat and I'll help script the walkthrough and draft the deck.
