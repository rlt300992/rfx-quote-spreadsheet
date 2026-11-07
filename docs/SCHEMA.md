# Schema v2: "Kill the Quote Spreadsheet" (Corrugated Boxes RFQ)

**Status:** v2. Lead merge of the designer draft v1, the procurement-SME review and the engineer/query-tester review. Pending buyer (user) approval.
**Stack:** SQLite + pandas + Streamlit + Gemini Flash. Built in under 24 hours.

## 0. How this version was made
- **Draft v1** (data-architect agent) → **SME review** (12 findings) + **engineer review** (10 test queries: 5 PASS, 5 PARTIAL, 0 FAIL; 12 findings) → **lead merge**, which is this file.
- **Web-verified, by both reviewers:**
  - The MSME 45-day payment cap applies to **micro and small** enterprises only. It is 15 days without a written agreement.
  - Corrugated boxes moved from **12% to 18% GST** in 2025.
- **Everything else** comes from the brief, the decisions we locked, and the agents' domain memory.

## 1. Global assumptions
- **A1. One RFQ.** Child tables carry `rfq_id` where a join needs it.
- **A2. One vendor per line.** A split award means different vendors on different lines, not a percentage split within one line. A percentage split is out of scope; mention it in the note.
- **A3. Single user.** Money is `REAL` (₹/pc to 4 decimals), dates are ISO `TEXT`, and JSON is `TEXT` (SQLite JSON1 was verified on 3.53).
- **A4. Config vs snapshot.** `config.yaml` holds the FX rate and all weight-formula constants: flute factors, sheet margins (60/120 mm), the large-box cutoff (L+W > 890 mm), the 30 mm sheet-width allowance and the 1.03 wastage factor. It also holds confidence base scores, thresholds, the interest rate and the ±25% sanity band. Each quote row **snapshots** the values it used.
- **A5. Raw is immutable; everything else is recomputed.** Raw (X) columns are never overwritten. All normalized (N) and validation (V) columns are written by **one function, `recompute_quote(quote_id)`**, and nothing else.
- **A6. The chat agent never queries base tables.** It queries only the read-only **views** in §4 and calls the **Python tools** in §5.

Filled-by legend: **S** seed · **C** co-pilot (buyer-confirmed) · **X** Gemini extraction · **N** normalization · **V** validation/rules · **B** buyer · **SYS** app logic

---

## 2. Tables (10)

### 2.1 `rfqs`
Added in v1 because RFQ-level terms are buyer data, not config.

| column | type | notes | by |
|---|---|---|---|
| rfq_id | INTEGER PK | | SYS |
| title, category | TEXT | e.g. "Corrugated boxes FY27 rate contract" | C |
| delivery_location | TEXT | The FOR destination that defines "freight included" | C |
| std_payment_terms_days | INTEGER | Baseline for the evaluated price, e.g. 60 | C |
| expected_gst_pct | REAL | 18. A quote that differs is flagged **(SME F1)** | C |
| baseline_period | TEXT | e.g. `FY2025-26`, used for last-year comparisons **(Eng F8)** | C |
| response_due_date | TEXT | | C |
| award_reference_date | TEXT NOT NULL | Defaults to the due date. Certificate validity is judged on this date **(Eng F6)** | C/B |
| rfq_body | TEXT | Final RFQ text (the stubbed email) | C |
| status | TEXT | draft / issued / evaluating / awarded | SYS |
| issued_at | TEXT | | SYS |

### 2.2 `vendors` (seed)
| column | type | notes | by |
|---|---|---|---|
| vendor_id | INTEGER PK | | S |
| vendor_code | TEXT UNIQUE | V1..V5 | S |
| name, gstin, city, state, contact_email | TEXT | | S |
| msme_category | TEXT CHECK IN ('micro','small','medium','none') | Replaces `is_msme` **(SME, Eng F11; web-verified)** | S |
| udyam_no | TEXT | | S |
| known_aliases | TEXT JSON | Letterhead and alias names, used to match a file to its vendor | S |

### 2.3 `line_items`
| column | type | notes | by |
|---|---|---|---|
| line_id | INTEGER PK | | SYS |
| rfq_id | FK rfqs | | SYS |
| line_no | INTEGER | 1..30, UNIQUE(rfq_id, line_no) | C |
| catalog_sku | TEXT NOT NULL | Join key to last-year prices **(Eng F8)** | C |
| description | TEXT | | C |
| length_mm, width_mm, height_mm | INTEGER | **Internal** dimensions (ID) | C |
| ply | INTEGER CHECK IN (3,5,7) | | C |
| flute_profile | TEXT | A / B / C / E / BC | C |
| layers_json | TEXT JSON | `[{"pos":1,"role":"liner","gsm":150,"bf":20},…]` | C |
| bursting_strength_kgcm2 | REAL | | C |
| print_colours, print_notes | INTEGER, TEXT | | C |
| annual_qty | INTEGER | Pieces. Drives annual value | C |
| typical_order_qty | INTEGER NOT NULL | Pieces per order (call-off). **Volume tiers are applied against this value (D1)** | C |
| size_class | TEXT | normal (60 mm margin) / large (120 mm) | N |
| theoretical_weight_kg | REAL | **Calculated once in Python with a tested formula, never by Gemini** | N |
| weight_calc_trace | TEXT | The formula with its numbers, shown to the buyer | N |
| buyer_confirmed | INTEGER 0/1 | | B |
| awarded_quote_id | FK quotes | The award for this line | B |
| award_basis | TEXT | e.g. "L1 evaluated among qualified vendors" | B |
| l1_quote_id | FK quotes | Cheapest current quote at award time, eligible or not **(SME F3)** | SYS |
| l1_eligible_quote_id | FK quotes | Cheapest *eligible* quote at award time **(SME F3)** | SYS |
| bypass_reason_code | TEXT | qual_fail / spec / capacity / validity / price_not_firm / buyer_judgment. **Required if the award ≠ l1_eligible** | B |
| awarded_at | TEXT | | B |

### 2.4 `rfx_questions`
| column | type | notes | by |
|---|---|---|---|
| question_id | INTEGER PK | | SYS |
| rfq_id | FK rfqs | | SYS |
| q_code | TEXT | e.g. `Q01_ISO9001` | C |
| question_text | TEXT | | C |
| answer_type | TEXT | bool / date / number / text / document | C |
| rule_type | TEXT | equals / min / max / in_set / doc_valid_on | C |
| rule_params_json | TEXT JSON | e.g. `{"doc_type":"ISO9001","valid_on":"award_reference_date"}` | C |
| is_knockout | INTEGER 0/1 | A failure disqualifies the vendor | C |
| required_doc_type | TEXT | If set, a "Yes" with no document = **fail** ("self-declared, no evidence") **(SME F10)** | C |

### 2.5 `last_year_prices` (seed)
| column | type | notes | by |
|---|---|---|---|
| lyp_id | INTEGER PK | | S |
| vendor_id | FK vendors | | S |
| catalog_sku | TEXT | UNIQUE(vendor_id, catalog_sku, period) | S |
| period | TEXT | `FY2025-26` | S |
| price_inr_pc | REAL | Ex-GST, ex-freight | S |
| freight_inr_pc | REAL | NULL = freight was included | S |
| payment_terms_days, gst_rate_pct, qty_supplied | INT, REAL, INT | gst_rate_pct will be 12, which shows the 12 → 18 change | S |
| spec_snapshot_json | TEXT JSON | Last year's ply/GSM, used to detect `LY_SPEC_CHANGED` **(SME F8)** | S |

### 2.6 `vendor_responses`
| column | type | notes | by |
|---|---|---|---|
| response_id | INTEGER PK | | SYS |
| rfq_id, vendor_id | FK | vendor_id is matched by sender or alias. The buyer can correct it | SYS/B |
| file_name, file_path | TEXT | | SYS |
| source_type | TEXT | excel / pdf / word / photo / email_text. Sets the base confidence | SYS |
| content_role | TEXT | quote / questionnaire / certificate / mixed | X |
| received_at | TEXT | | SYS |
| received_after_due | INTEGER 0/1 | Flagged, not blocked **(SME F2)** | SYS |
| version | INTEGER | 1, 2, … per vendor | SYS |
| revision_scope | TEXT | full / partial. **A partial revision only supersedes the lines it touches (SME F2)** | X/B |
| supersedes_response_id | FK self | | SYS |
| extraction_status | TEXT | pending / extracted / partial / failed | SYS |
| extraction_model | TEXT | Model and prompt version | SYS |
| raw_extraction_json | TEXT JSON | First Gemini pass, kept as returned | X |
| second_read_json | TEXT JSON | Second pass with a different prompt, **for the 4 core fields only (Eng F12)** | X |
| quote_ref_no, quote_date, validity_date | TEXT | | X |
| price_basis | TEXT | firm / rm_linked / unspecified **(SME F4)** | X |
| price_firm_until | TEXT | | X |
| rm_clause_text | TEXT | Verbatim kraft-paper price-variation clause | X |
| header_terms_json | TEXT JSON | Verbatim freight, payment, GST and footnote text | X |
| declared_msme_category | TEXT | MSME category as stated in the reply (e.g. a Udyam line on the letterhead). Missing → use `vendors.msme_category`. Matches → no action. Differs → flag `MSME_CATEGORY_MISMATCH` (spot_check), and keep using the master value until the buyer confirms | X |
| clarification_draft | TEXT | One email per vendor (light loop) | SYS |
| clarification_status | TEXT | none / drafted / sent_stub / resolved | SYS/B |

*Dropped from v1:* `is_current` (now set per quote row) and `lines_quoted`/`lines_not_quoted` (now computed in a view) **(Eng F4, F9)**.

### 2.7 `quotes` (core table: one row per vendor × line × version)

**Identity and status**
| column | type | notes | by |
|---|---|---|---|
| quote_id | INTEGER PK | | SYS |
| response_id, vendor_id | FK | | SYS |
| line_id | FK line_items | NULL only if `unmatched` | X/V |
| is_current | INTEGER | **UNIQUE INDEX (vendor_id, line_id) WHERE is_current=1 (Eng F4)** | SYS |
| supersedes_quote_id | FK quotes | Line-level revisions **(SME F2)** | SYS |
| quote_status | TEXT | quoted / not_quoted / no_bid / unmatched. Layer 0 inserts `not_quoted` rows (full revisions only) | V |
| vendor_line_ref | TEXT | The vendor's own item text or code | X |

**Raw values (X, never overwritten)**
| column | type | notes |
|---|---|---|
| raw_price_text | TEXT | Verbatim: "₹42/kg", "same as last year" |
| raw_price, raw_currency, raw_unit | REAL, TEXT, TEXT | raw_unit: per_piece / per_box / per_100 / per_1000 / per_kg / as_last_year |
| tax_basis | TEXT | **ex_gst / incl_gst / unspecified (SME F1)** |
| gst_rate_pct | REAL | Stored only. Never added to the comparison price |
| price_tiers_json | TEXT JSON | `[{"min_qty":10000,"price":11.0,"qty_basis":"per_order|annual|unclear"}]`. **D1:** `per_order` is the default. `unclear` is treated as per_order and flagged `TIER_BASIS_UNCLEAR` (spot_check). |
| discounts_json | TEXT JSON | `[{"pct":2,"condition_type":"payment_days_max","condition_value":7,"condition_text":"…","source":"footnote p2","applied":false}]` **(SME F9)**. **D2:** never auto-applied. The buyer toggles `applied` on and off; every toggle is audited and recomputed. |
| charges_json | TEXT JSON | Plates, dies, per-colour print: `[{"type":"plate","amount":2500,"basis":"one_time","colours":2,"source":"…"}]` **(SME F5)** |
| vendor_line_total | REAL | For the arithmetic check |
| freight_basis | TEXT | included / extra_quoted / extra_unquoted |
| raw_freight_text, raw_freight_value | TEXT, REAL | |
| payment_terms_days, lead_time_days, moq_qty | INTEGER | Copied from the response header onto each line |
| offered_qty | INTEGER | NULL = the full quantity. Capacity limit **(SME F7)** |
| capacity_text | TEXT | Verbatim |
| vendor_spec_json | TEXT JSON | The vendor's ply, GSM and size, including `dim_basis`: ID / OD / unknown **(SME F6)** |
| vendor_stated_weight_kg | REAL | If the vendor states a box weight **(SME F6)** |

**Spec decision (V/B)**
| column | type | notes |
|---|---|---|
| spec_match | TEXT | exact / deviation / substitute |
| spec_deviation_note | TEXT | Always in plain words, e.g. "5-ply at 120 GSM vs 150 spec" |
| spec_accepted | INTEGER 0/1 | The buyer (QA) accepted the substitute **(Eng F2)** |

**Normalization (N, written only by `recompute_quote`)**
| column | type | notes |
|---|---|---|
| conversion_method | TEXT | direct / per_n_pieces / per_kg_formula / last_year_ref |
| pieces_per_raw_unit | REAL | e.g. 100 for per_100 |
| weight_used_kg, weight_source | REAL, TEXT | `theoretical` (default) or `vendor_stated`, for per-kg quotes |
| ref_lyp_id | FK last_year_prices | Which last-year row resolved "same as last year" **(SME F8)** |
| fx_rate, fx_rate_date, fx_source | REAL, TEXT, TEXT | Snapshot |
| payment_rate_pct, std_terms_days_used, effective_vendor_days | REAL, INT, INT | Snapshot. effective_vendor_days = MIN(quoted, 45) for micro/small vendors, with flag `MSME_TERMS_CAPPED` **(D3; Eng F7, SME Q1)** |
| tier_applied, tier_qty_used | TEXT, INTEGER | The tier chosen and the quantity it was matched against (D1) |
| discount_applied_pct | REAL | Sum of the discounts the buyer has toggled `applied=true` (D2) |
| base_inr_pc | REAL | ₹/pc, ex-GST, ex-freight |
| freight_inr_pc | REAL | |
| freight_imputed | INTEGER | 0 none / 1 highest other vendor / 2 last year. **D4:** if no vendor quoted freight and there is no last-year number, freight stays NULL, the line is excluded (`freight_unknown`), and the item goes into the clarification email. Never invented. |
| freight_imputed_from_quote_id | FK quotes | "Imputed from whom" **(SME F11)** |
| landed_inr_pc | REAL | **The quoted price** = base + freight. NULL if not quoted or freight unknown |
| onetime_inr_pc | REAL | One-time charges ÷ annual_qty **(SME F5)** |
| payment_loading_inr_pc | REAL | landed × rate × (std − MIN(eff_days, std+30)) / 365. Symmetric, capped at +30 days |
| evaluated_inr_pc | REAL | landed + onetime + loading. **Used for ranking** |
| calc_snapshot_json | TEXT JSON | Thresholds, sanity band and flute-factor version used **(SME F11)** |
| normalization_trace_json | TEXT JSON | Ordered human-readable steps for the "how was this computed" popover |

**Validation, confidence and eligibility (V)**
| column | type | notes |
|---|---|---|
| validation_json | TEXT JSON | `[{"layer":1,"check":"qty_x_price","result":"fail","detail":"…"}]` |
| field_meta_json | TEXT JSON | Per-field confidence, signals, flags, source and status (§3) |
| min_confidence | REAL | Rollup |
| review_state | TEXT | Rollup: auto_ok / spot_check / must_confirm / confirmed / rejected |
| award_eligible | INTEGER | Quote-level eligibility. Vendor qualification is added in the view |
| ineligible_reasons_json | TEXT JSON | Codes: not_quoted, open_must_confirm:<field>, spec_substitute, validity_expired, partial_qty, price_not_firm, freight_unknown **(SME F3)** |
| updated_at | TEXT | |

*Dropped from v1:* `annual_value_inr` (computed in the view) **(Eng F9)**.

### 2.8 `questionnaire_answers`
| column | type | notes | by |
|---|---|---|---|
| answer_id | INTEGER PK | | SYS |
| response_id, vendor_id, question_id | FK | | SYS |
| is_current | INTEGER | UNIQUE INDEX (vendor_id, question_id) WHERE is_current=1 | SYS |
| answer_raw, answer_value | TEXT | NULL = not answered | X |
| evidence_doc_id | FK documents | | X/V |
| source_ref_json | TEXT JSON | | X |
| confidence, confidence_signals_json | REAL, JSON | **Strict tier (≥95%) when the answer decides a knock-out (SME Q5)** | V |
| rule_result | TEXT | pass / fail / unknown. **Rule engine only** | V |
| rule_explanation | TEXT | e.g. "ISO 9001 expires 2026-03-31, before the award date → FAIL" | V |
| rule_snapshot_json | TEXT JSON | The rule and reference date used **(SME F10)** | V |
| evaluated_at | TEXT | | V |
| review_state | TEXT | | V/B |

### 2.9 `documents`
| column | type | notes | by |
|---|---|---|---|
| doc_id | INTEGER PK | | SYS |
| rfq_id | FK rfqs NOT NULL | So the join never drops a document **(Eng F6)** | SYS |
| vendor_id, response_id | FK | | SYS |
| file_name, file_path, page_range | TEXT | | SYS |
| doc_type | TEXT | ISO9001 / test_report / FSC / udyam_msme / gst_cert / other | X |
| issuer, doc_number, issue_date, expiry_date | TEXT | | X |
| holder_name, site_address, scope_text | TEXT | **A certificate held by someone else, or for the wrong scope, = must_confirm (SME F10)** | X |
| holder_matches_vendor | INTEGER | Checked against name, GSTIN and aliases | V |
| covers_line_ids_json | TEXT JSON | For test reports | X |
| extracted_json, source_ref_json | TEXT JSON | | X |
| confidence, review_state | REAL, TEXT | | V/B |

*Dropped from v1:* the stored `validity_status`. It goes stale, so it is now computed live in `v_documents` **(Eng F6)**.

### 2.10 `audit_log` (append-only)
| column | type | notes |
|---|---|---|
| audit_id | INTEGER PK | |
| ts, actor | TEXT | actor: `buyer:<name>` / `system` / `gemini` |
| batch_id | TEXT | One UUID per award commit or bulk confirm **(Eng F10)** |
| action | TEXT | extract_run / confirm / override / reject / spec_accept / clarification_drafted / clarification_resolved / response_superseded / award / unaward |
| entity_type, entity_id | TEXT, INT | `quote`, `line_item`, `response`, … |
| field_name, old_value, new_value | TEXT | |
| reason | TEXT | **Required** for override, spec_accept, and an award that bypasses L1-eligible |
| evidence_json | TEXT JSON | Source reference or vendor reply snippet. For a bulk confirm, the list of quote_ids |
| scenario_json | TEXT JSON | For an award: **one row per line**, holding basis, filters, and the candidates with their prices and exclusion reasons |
| chat_prompt | TEXT | The question that produced the award |

---

## 3. Field-level confidence (unchanged from v1, endorsed by the engineer)
- **Layout:** typed value columns, plus one `field_meta_json` per quote, plus rollup columns.
- **Fields tracked (10):**
  - price, unit, currency, freight, **tax_basis** (≥95 / 80)
  - lead_time, moq, payment_terms (≥85 / 70)
  - gst_rate
  - spec_match
- **Base confidence** comes from `source_type`.
  - Passing validations raises it.
  - Any failed check caps it below must-confirm.
  - Rule-driven caps:
    - `DERIVED_PER_KG`, `IMPLIED_BY_BLANKET` ("rest same as last year") → always must_confirm for price
    - `WEIGHT_MISMATCH` (>5%) → spot_check
    - `LY_SPEC_CHANGED`, `LY_REF_MISSING` → must_confirm
    - `GST_RATE_MISMATCH` → spot_check
    - `MSME_TERMS_CAPPED` → spot_check
    - `MSME_CATEGORY_MISMATCH` (reply ≠ vendor master) → spot_check; the master value is used until the buyer confirms
- **Buyer actions** write an override into the JSON, append to `audit_log`, then call `recompute_quote`.

## 4. Views (the only thing the chat agent queries)
| view | purpose |
|---|---|
| `v_vendor_qualification` | **Tri-state**: qualified / pending / disqualified. A missing or `unknown` knock-out answer = **pending, not qualified** (tested SQL in the engineer review, F1). Exposes `ko_issues` |
| `v_comparison_grid` | One row per current (line, vendor). Includes prices, `annual_value_inr`, `rank_landed`/`rank_evaluated`, spec, review, `qual_status`, last-year landed price and saving, `is_awardable` (= award_eligible AND qualified), `is_awarded`, `award_stale`, and `exclusion_reason` (the first reason in priority order) |
| `v_review_queue` | `field_meta_json` flattened: quote, line, vendor, field, confidence, status, flags, source snippet, `pct_vs_best`. Shows which open items could change the winner |
| `v_documents` | Live `validity_status` (valid / expired / expiring_90d / unknown) as of `award_reference_date` |
| `v_ly_baseline` | Last-year landed price per line, from the main last-year supplier (highest qty_supplied). Used for "savings vs last year" across vendors |
| `v_response_summary` | Lines quoted / not quoted per vendor, plus clarification status |

## 5. Python tools for the chat agent (no free-form writes)
- `award_scenario(basis='evaluated', exclude_vendors=[], require_qualified=True, include_pending=False, include_accepted_substitutes=False)` → DataFrame of the per-line winner and the totals. This answers the VP's question and every "what if" question.
- `commit_award(scenario, chat_prompt, reason)` writes the awards. **The agent can only *propose* a scenario. Nothing is committed until the buyer clicks Confirm in the UI.** It:
  - Asserts each quote is current and on the right line.
  - Writes one audit row per line with a shared `batch_id`.
  - Requires a reason wherever the award ≠ l1_eligible.
- `query_view(sql)` runs a read-only SELECT against the §4 views only.
- `recompute_quote(quote_id)`, internal and not an agent tool, is the single writer of the N and V columns.
- `convert(raw, line_spec, category) → (inr_per_unit, method, trace)`, internal: the **pluggable unit converter** called by `recompute_quote`. Phase 1 implements `corrugated` (per_piece, per_100/1000, per_kg via the weight formula). Adding a category means adding one converter function, not changing the schema.

## 6. Decisions locked by the buyer (user)
| # | Decision | Basis |
|---|---|---|
| D1 | Price tiers are picked against **typical order quantity**. Annual quantity is used only if the vendor says so. If unclear, the higher price is used and flagged | Corrugated is priced by run size (SME review). Avoids understating cost |
| D2 | Conditional discounts are **shown, never auto-applied**. The buyer toggles each one on or off to compare, and every toggle is audited | Avoids a condition-engine rabbit hole. Keeps the buyer in control |
| D3 | Micro/small vendor terms above 45 days are **capped at 45 in the evaluation and flagged** | Web: MSMED Act s.15 caps credit at 45 days even with a written agreement. Accounting practice caps the due date |
| D4 | No freight quoted and no last-year figure → **freight unknown, line excluded, vendor asked**. A number is never invented | Trust |
| D5 | Payment-term loading rate = **company cost of funds, default 10.9% p.a.** (SBI base 9.90% + 1%, IOCL style), **configurable** as `cost_of_funds_pct` in config.yaml. The standard term is set per RFQ (`rfqs.std_payment_terms_days`, default 60) | Web: IOCL loads at SBI + 1%, BHEL at SBI + 6%, NTPC rejects deviations, and private firms use cost of capital. A corporate-style rate fits a private FMCG buyer |

### Demo constants (all configurable)
| Item | Value | Source |
|---|---|---|
| Buyer | Sahyadri Consumer Products Ltd (fictional FMCG company) | Invented |
| Delivery (FOR) | Chakan MIDC plant, Pune | Web: dense corrugated-supplier base around Pune/Chakan |
| Standard payment term | 60 days (default, set per RFQ) | Web: illustrative FMCG norm |
| Cost of funds | 10.9% p.a. (`cost_of_funds_pct`) | SBI base 9.90% (Dec 2025) + 1% |
| USD/INR | ₹95.17, dated 26 Oct 2026 (market close, not the SBI TT rate) | Web: Pound Sterling Live |
| GST expected | 18% (last year 12%) | Web: 2025 rate change |

## 7. Agreed out of scope (for the note)
- **Pricing:** the kraft-paper escalation formula, percentage splits within one line, and integer-paise money.
- **Supply terms:** palletisation, delivery schedules, quantity and GSM tolerances, and liquidated damages.
- **Vendor checks:** NABL accreditation and test-report age, and corrugator capacity verification.
- **Clarification loop:** the standard version (automatic reading of vendor replies) is phase 2.
- **Multi-category support:** the spec columns are corrugated-specific by design, because typed columns give stricter validation, which builds trust. About 80% of the schema is category-agnostic. Phase 2 replaces the spec columns with `spec_json`, adds a per-category field list, and adds one `convert()` plug-in per category.
- **Dual-sourcing percentage splits:** phase 2. Phase 1 has the chat suggest the L2 vendor as a backup per line, which is derived from the ranking and not stored.

---

## 8. P1 implementation notes (additions to v2; no column or table was removed)
- **audit_log.action** also allows `discount_toggle` (D2: every toggle is audited).
- **ineligible_reasons_json** also uses `spec_deviation`, `no_price`, `rejected` and `unmatched`. A deviation or substitute has `spec_match` = must_confirm until the buyer accepts it (`spec_accepted`).
- **price_not_firm is a flag, not a hard exclusion**, unless `eligibility.block_price_not_firm: true` in config.yaml (default false). Reason: a raw-material-linked price (V3) is still comparable; the buyer sees `PRICE_NOT_FIRM` and the kraft-escalation formula is out of scope (§7).
- **field_meta_json** layout: `extraction` (flags and per-field source, written by the extractor), `overrides` and `confirmed` (written by buyer actions), `rejected`, and `fields` / `flags` (derived, rewritten by `recompute_quote`). Buyer corrections are overrides applied on top of the raw columns, so raw stays immutable.
- **Validation results** are `pass` / `fail` / `warn` / `skip`. Only `fail` caps confidence. A GST rate that differs from expected is a `warn` (it routes `gst_rate` to spot_check through its flag).
- **Confidence** = config base for the source type + `confidence.check_bonus` (0.05) per passed supporting check, capped at `confidence.cap` (0.99); a failed check or must-confirm flag caps the field below the spot threshold.
- **Freight** uses `raw_freight_value` as INR per piece. Imputation reads other vendors' effective raw freight (`extra_quoted` only), so it never depends on another quote's computed state.
- **Second read** is stored as `{"<quote_id>": {raw_price, raw_unit, raw_currency, raw_freight_value}}` in `vendor_responses.second_read_json` and is compared to the vendor's raw extraction, not to a later buyer correction.
- **award_scenario(include_accepted_substitutes=True)** is a what-if that also admits quotes whose only blocker is an unaccepted spec deviation or substitute.
- **P4/P5 additions:** `audit_log.action` is no longer a CHECK; the allowed values are `core.db.AUDIT_ACTIONS` (adds `clarification_sent_stub`, `rfq_confirm`, `rfq_send`) and `init_db` migrates older databases in place. `commit_award` fills `line_items.awarded_quote_id`, `l1_quote_id` (cheapest current quote of any kind), `l1_eligible_quote_id` (cheapest awardable quote), `bypass_reason_code`, `award_basis`, `awarded_at` and writes one `award` audit row per line (shared `batch_id`, candidates and exclusion reasons in `scenario_json`). No tables or columns were added.
