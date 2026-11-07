"""Idempotent loader for data/seed/*.csv. Runs on app start if the DB is empty."""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from core.config import load_config
from core.convert import theoretical_weight

SEED_DIR = Path(__file__).resolve().parent.parent / "data" / "seed"


def _rows(name: str) -> list[dict]:
    with open(SEED_DIR / name, encoding="utf-8", newline="") as f:
        return [{k: (v if v != "" else None) for k, v in r.items()} for r in csv.DictReader(f)]


def _insert(conn: sqlite3.Connection, table: str, rows: list[dict], cols: list[str]) -> None:
    sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])


def seed(conn: sqlite3.Connection) -> None:
    """Load all seed tables. INSERT OR IGNORE on the CSV primary keys makes it idempotent."""
    cfg = load_config()
    rfqs = _rows("rfqs.csv")
    _insert(conn, "rfqs", rfqs, ["rfq_id", "title", "category", "delivery_location", "std_payment_terms_days",
                                 "expected_gst_pct", "baseline_period", "response_due_date",
                                 "award_reference_date", "rfq_body", "status", "issued_at"])
    _insert(conn, "vendors", _rows("vendors.csv"),
            ["vendor_id", "vendor_code", "name", "gstin", "city", "state", "contact_email",
             "msme_category", "udyam_no", "known_aliases"])

    lines = _rows("line_items.csv")
    for ln in lines:  # weight is computed by the tested formula, not trusted from the CSV
        ln["size_class"], ln["theoretical_weight_kg"], ln["weight_calc_trace"] = theoretical_weight(ln, cfg)
    _insert(conn, "line_items", lines,
            ["line_id", "rfq_id", "line_no", "catalog_sku", "description", "length_mm", "width_mm",
             "height_mm", "ply", "flute_profile", "layers_json", "bursting_strength_kgcm2", "print_colours",
             "print_notes", "annual_qty", "typical_order_qty", "size_class", "theoretical_weight_kg",
             "weight_calc_trace", "buyer_confirmed"])
    _insert(conn, "rfx_questions", _rows("rfx_questions.csv"),
            ["question_id", "rfq_id", "q_code", "question_text", "answer_type", "rule_type",
             "rule_params_json", "is_knockout", "required_doc_type"])
    _insert(conn, "last_year_prices", _rows("last_year_prices.csv"),
            ["lyp_id", "vendor_id", "catalog_sku", "period", "price_inr_pc", "freight_inr_pc",
             "payment_terms_days", "gst_rate_pct", "qty_supplied", "spec_snapshot_json"])
    conn.commit()


def seed_if_empty(conn: sqlite3.Connection) -> bool:
    """Seed only when the DB has no RFQ yet. Returns True if it seeded."""
    if conn.execute("SELECT COUNT(*) FROM rfqs").fetchone()[0] == 0:
        seed(conn)
        return True
    return False
