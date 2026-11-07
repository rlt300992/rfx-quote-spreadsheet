"""SQLite schema (docs/SCHEMA.md §2) and connection helpers. Views live in views.sql."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWS_SQL = Path(__file__).with_name("views.sql")


def db_path() -> Path:
    return Path(os.environ.get("RFX_DB") or ROOT / "rfx.db")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


DDL = """
CREATE TABLE IF NOT EXISTS rfqs (
  rfq_id INTEGER PRIMARY KEY,
  title TEXT, category TEXT, delivery_location TEXT,
  std_payment_terms_days INTEGER, expected_gst_pct REAL, baseline_period TEXT,
  response_due_date TEXT,
  award_reference_date TEXT NOT NULL,
  rfq_body TEXT,
  status TEXT CHECK (status IN ('draft','issued','evaluating','awarded')),
  issued_at TEXT
);

CREATE TABLE IF NOT EXISTS vendors (
  vendor_id INTEGER PRIMARY KEY,
  vendor_code TEXT NOT NULL UNIQUE,
  name TEXT, gstin TEXT, city TEXT, state TEXT, contact_email TEXT,
  msme_category TEXT CHECK (msme_category IN ('micro','small','medium','none')),
  udyam_no TEXT,
  known_aliases TEXT
);

CREATE TABLE IF NOT EXISTS line_items (
  line_id INTEGER PRIMARY KEY,
  rfq_id INTEGER NOT NULL REFERENCES rfqs(rfq_id),
  line_no INTEGER NOT NULL,
  catalog_sku TEXT NOT NULL,
  description TEXT,
  length_mm INTEGER, width_mm INTEGER, height_mm INTEGER,
  ply INTEGER CHECK (ply IN (3,5,7)),
  flute_profile TEXT, layers_json TEXT,
  bursting_strength_kgcm2 REAL,
  print_colours INTEGER, print_notes TEXT,
  annual_qty INTEGER,
  typical_order_qty INTEGER NOT NULL,
  size_class TEXT, theoretical_weight_kg REAL, weight_calc_trace TEXT,
  buyer_confirmed INTEGER DEFAULT 0,
  awarded_quote_id INTEGER REFERENCES quotes(quote_id),
  award_basis TEXT,
  l1_quote_id INTEGER REFERENCES quotes(quote_id),
  l1_eligible_quote_id INTEGER REFERENCES quotes(quote_id),
  bypass_reason_code TEXT CHECK (bypass_reason_code IS NULL OR bypass_reason_code IN
     ('qual_fail','spec','capacity','validity','price_not_firm','buyer_judgment')),
  awarded_at TEXT,
  UNIQUE (rfq_id, line_no)
);

CREATE TABLE IF NOT EXISTS rfx_questions (
  question_id INTEGER PRIMARY KEY,
  rfq_id INTEGER NOT NULL REFERENCES rfqs(rfq_id),
  q_code TEXT NOT NULL, question_text TEXT,
  answer_type TEXT CHECK (answer_type IN ('bool','date','number','text','document')),
  rule_type TEXT CHECK (rule_type IN ('equals','min','max','in_set','doc_valid_on')),
  rule_params_json TEXT,
  is_knockout INTEGER NOT NULL DEFAULT 0,
  required_doc_type TEXT,
  UNIQUE (rfq_id, q_code)
);

CREATE TABLE IF NOT EXISTS last_year_prices (
  lyp_id INTEGER PRIMARY KEY,
  vendor_id INTEGER NOT NULL REFERENCES vendors(vendor_id),
  catalog_sku TEXT NOT NULL, period TEXT NOT NULL,
  price_inr_pc REAL, freight_inr_pc REAL,
  payment_terms_days INTEGER, gst_rate_pct REAL, qty_supplied INTEGER,
  spec_snapshot_json TEXT,
  UNIQUE (vendor_id, catalog_sku, period)
);

CREATE TABLE IF NOT EXISTS vendor_responses (
  response_id INTEGER PRIMARY KEY,
  rfq_id INTEGER NOT NULL REFERENCES rfqs(rfq_id),
  vendor_id INTEGER NOT NULL REFERENCES vendors(vendor_id),
  file_name TEXT, file_path TEXT,
  source_type TEXT CHECK (source_type IN ('excel','pdf','word','photo','email_text')),
  content_role TEXT CHECK (content_role IN ('quote','questionnaire','certificate','mixed')),
  received_at TEXT, received_after_due INTEGER DEFAULT 0,
  version INTEGER DEFAULT 1,
  revision_scope TEXT DEFAULT 'full' CHECK (revision_scope IN ('full','partial')),
  supersedes_response_id INTEGER REFERENCES vendor_responses(response_id),
  extraction_status TEXT DEFAULT 'pending' CHECK (extraction_status IN ('pending','extracted','partial','failed')),
  extraction_model TEXT,
  raw_extraction_json TEXT, second_read_json TEXT,
  quote_ref_no TEXT, quote_date TEXT, validity_date TEXT,
  price_basis TEXT CHECK (price_basis IS NULL OR price_basis IN ('firm','rm_linked','unspecified')),
  price_firm_until TEXT, rm_clause_text TEXT, header_terms_json TEXT,
  declared_msme_category TEXT,
  clarification_draft TEXT,
  clarification_status TEXT DEFAULT 'none' CHECK (clarification_status IN ('none','drafted','sent_stub','resolved'))
);

CREATE TABLE IF NOT EXISTS quotes (
  quote_id INTEGER PRIMARY KEY,
  response_id INTEGER NOT NULL REFERENCES vendor_responses(response_id),
  vendor_id INTEGER NOT NULL REFERENCES vendors(vendor_id),
  line_id INTEGER REFERENCES line_items(line_id),
  is_current INTEGER NOT NULL DEFAULT 1,
  supersedes_quote_id INTEGER REFERENCES quotes(quote_id),
  quote_status TEXT NOT NULL DEFAULT 'quoted'
     CHECK (quote_status IN ('quoted','not_quoted','no_bid','unmatched')),
  vendor_line_ref TEXT,
  -- raw (X): never overwritten
  raw_price_text TEXT, raw_price REAL, raw_currency TEXT,
  raw_unit TEXT CHECK (raw_unit IS NULL OR raw_unit IN
     ('per_piece','per_box','per_100','per_1000','per_kg','as_last_year')),
  tax_basis TEXT CHECK (tax_basis IS NULL OR tax_basis IN ('ex_gst','incl_gst','unspecified')),
  gst_rate_pct REAL,
  price_tiers_json TEXT, discounts_json TEXT, charges_json TEXT,
  vendor_line_total REAL,
  freight_basis TEXT CHECK (freight_basis IS NULL OR freight_basis IN ('included','extra_quoted','extra_unquoted')),
  raw_freight_text TEXT, raw_freight_value REAL,
  payment_terms_days INTEGER, lead_time_days INTEGER, moq_qty INTEGER,
  offered_qty INTEGER, capacity_text TEXT,
  vendor_spec_json TEXT, vendor_stated_weight_kg REAL,
  -- spec decision
  spec_match TEXT CHECK (spec_match IS NULL OR spec_match IN ('exact','deviation','substitute')),
  spec_deviation_note TEXT,
  spec_accepted INTEGER NOT NULL DEFAULT 0,
  -- normalization (N): written only by recompute_quote
  conversion_method TEXT, pieces_per_raw_unit REAL,
  weight_used_kg REAL, weight_source TEXT,
  ref_lyp_id INTEGER REFERENCES last_year_prices(lyp_id),
  fx_rate REAL, fx_rate_date TEXT, fx_source TEXT,
  payment_rate_pct REAL, std_terms_days_used INTEGER, effective_vendor_days INTEGER,
  tier_applied TEXT, tier_qty_used INTEGER,
  discount_applied_pct REAL,
  base_inr_pc REAL, freight_inr_pc REAL,
  freight_imputed INTEGER NOT NULL DEFAULT 0,
  freight_imputed_from_quote_id INTEGER REFERENCES quotes(quote_id),
  landed_inr_pc REAL, onetime_inr_pc REAL, payment_loading_inr_pc REAL, evaluated_inr_pc REAL,
  calc_snapshot_json TEXT, normalization_trace_json TEXT,
  -- validation / confidence / eligibility (V)
  validation_json TEXT, field_meta_json TEXT,
  min_confidence REAL,
  review_state TEXT CHECK (review_state IS NULL OR review_state IN
     ('auto_ok','spot_check','must_confirm','confirmed','rejected')),
  award_eligible INTEGER NOT NULL DEFAULT 0,
  ineligible_reasons_json TEXT,
  updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_quotes_current ON quotes (vendor_id, line_id) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS ix_quotes_line ON quotes (line_id);
CREATE INDEX IF NOT EXISTS ix_quotes_response ON quotes (response_id);

CREATE TABLE IF NOT EXISTS documents (
  doc_id INTEGER PRIMARY KEY,
  rfq_id INTEGER NOT NULL REFERENCES rfqs(rfq_id),
  vendor_id INTEGER REFERENCES vendors(vendor_id),
  response_id INTEGER REFERENCES vendor_responses(response_id),
  file_name TEXT, file_path TEXT, page_range TEXT,
  doc_type TEXT CHECK (doc_type IS NULL OR doc_type IN
     ('ISO9001','test_report','FSC','udyam_msme','gst_cert','other')),
  issuer TEXT, doc_number TEXT, issue_date TEXT, expiry_date TEXT,
  holder_name TEXT, site_address TEXT, scope_text TEXT,
  holder_matches_vendor INTEGER,
  covers_line_ids_json TEXT, extracted_json TEXT, source_ref_json TEXT,
  confidence REAL, review_state TEXT
);

CREATE TABLE IF NOT EXISTS questionnaire_answers (
  answer_id INTEGER PRIMARY KEY,
  response_id INTEGER REFERENCES vendor_responses(response_id),
  vendor_id INTEGER NOT NULL REFERENCES vendors(vendor_id),
  question_id INTEGER NOT NULL REFERENCES rfx_questions(question_id),
  is_current INTEGER NOT NULL DEFAULT 1,
  answer_raw TEXT, answer_value TEXT,
  evidence_doc_id INTEGER REFERENCES documents(doc_id),
  source_ref_json TEXT,
  confidence REAL, confidence_signals_json TEXT,
  rule_result TEXT CHECK (rule_result IS NULL OR rule_result IN ('pass','fail','unknown')),
  rule_explanation TEXT, rule_snapshot_json TEXT, evaluated_at TEXT,
  review_state TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_answers_current ON questionnaire_answers (vendor_id, question_id) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS audit_log (
  audit_id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL, actor TEXT NOT NULL, batch_id TEXT,
  action TEXT NOT NULL,  -- see AUDIT_ACTIONS; not a CHECK so new actions never need a table rebuild
  entity_type TEXT, entity_id INTEGER,
  field_name TEXT, old_value TEXT, new_value TEXT,
  reason TEXT, evidence_json TEXT, scenario_json TEXT, chat_prompt TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

AUDIT_ACTIONS = ("extract_run", "confirm", "override", "reject", "spec_accept", "clarification_drafted", "clarification_sent_stub",
                 "clarification_resolved", "response_superseded", "award", "unaward", "discount_toggle", "rfq_confirm", "rfq_send")

TABLES = ("rfqs", "vendors", "line_items", "rfx_questions", "last_year_prices",
          "vendor_responses", "quotes", "documents", "questionnaire_answers", "audit_log")
VIEWS = ("v_vendor_qualification", "v_comparison_grid", "v_review_queue",
         "v_documents", "v_ly_baseline", "v_response_summary")


def create_views(conn: sqlite3.Connection) -> None:
    """(Re)create all views; safe to call on every start so view changes ship with code."""
    conn.executescript(VIEWS_SQL.read_text(encoding="utf-8"))


def _migrate_audit_log(conn: sqlite3.Connection) -> None:
    """Databases created before P4 have a CHECK on audit_log.action. Rebuild the table (rows kept, append-only
    triggers restored) so newer actions can be logged."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone()
    if row is None or "CHECK (action IN" not in row[0]:
        return
    conn.executescript("""
        DROP TRIGGER IF EXISTS audit_no_update;
        DROP TRIGGER IF EXISTS audit_no_delete;
        ALTER TABLE audit_log RENAME TO audit_log_old;
    """)
    conn.executescript(DDL)  # recreates audit_log (no CHECK) and its triggers; other tables already exist
    conn.execute("INSERT INTO audit_log SELECT * FROM audit_log_old")
    conn.execute("DROP TABLE audit_log_old")


def init_db(conn: sqlite3.Connection) -> None:
    _migrate_audit_log(conn)
    conn.executescript(DDL)
    create_views(conn)
    conn.commit()
