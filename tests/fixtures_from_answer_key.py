"""DEV-ONLY: insert quotes from data/answer_key as if Gemini had extracted them.

Used ONLY by tests and tests/populate_dev_db.py to verify the maths. App code must never import this
module and must never read data/answer_key (CLAUDE.md section 4).

The answer key holds the TRUE values; the vendor-side inputs it lacks (tiers, discounts, plate charges,
payment terms, questionnaire answers, certificates) are supplied below from CLAUDE.md section 7. The
V2 "Rate/box" trap is inserted the way the vendor's header would be read (per_piece) so the sanity
layer has to catch it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from core.db import init_db
from core.recompute import recompute_all
from core.rules import evaluate_all
from core.seed import seed
from core.validate import ensure_completeness

ROOT = Path(__file__).resolve().parent.parent
import os
KEY = Path(os.environ.get("ANSWER_KEY_DIR") or ROOT / "data" / "answer_key")
MANIFEST = ROOT / "data" / "inbox" / "manifest.json"

SOURCE_TYPE = {"V1": "excel", "V2": "pdf", "V3": "word", "V4": "photo", "V5": "email_text"}
PAYMENT_DAYS = {"V1": 60, "V2": 90, "V3": 60, "V4": None, "V5": 45}  # V2 90 per CLAUDE.md; V4 photo states none; V5 "as before" = last year 45; V1/V3 assumed
ANSWERS = {  # what extraction would return per questionnaire reply (value as text)
    "V1": {"Q01_ISO9001": "yes", "Q02_BS_TEST": "yes", "Q03_REJECTION": "yes", "Q04_CAPACITY": "320",
           "Q05_FIRST_SUPPLY": "10", "Q06_PAPER_SOURCE": "virgin", "Q07_FSC": "no", "Q08_COMPLAINTS": "1"},
    "V2": {"Q01_ISO9001": "yes", "Q02_BS_TEST": "yes", "Q03_REJECTION": "conditional: subject to joint inspection",
           "Q04_CAPACITY": "220", "Q05_FIRST_SUPPLY": "12", "Q06_PAPER_SOURCE": "recycled", "Q07_FSC": "no",
           "Q08_COMPLAINTS": "2"},
    "V3": {"Q01_ISO9001": "yes", "Q02_BS_TEST": "yes", "Q03_REJECTION": "yes", "Q04_CAPACITY": "400",
           "Q05_FIRST_SUPPLY": "18", "Q06_PAPER_SOURCE": "mixed", "Q07_FSC": "yes", "Q08_COMPLAINTS": "0"},
    "V4": {},
    "V5": {"Q01_ISO9001": "yes", "Q03_REJECTION": "yes", "Q04_CAPACITY": "180"},
}
DOCS = {  # (doc_type, holder_is_vendor_name, holder_override, expiry)
    "V1": [("ISO9001", None, "2028-06-30"), ("test_report", None, None)],
    "V2": [("ISO9001", "Shree Ganesh Group Holdings", "2028-01-31"), ("test_report", None, None)],
    "V3": [("ISO9001", None, "2028-03-31"), ("FSC", None, "2027-12-31"), ("test_report", None, None)],
    "V4": [],
    "V5": [("ISO9001", None, "2026-03-31")],
}


def _nn(v):
    return None if pd.isna(v) else v


def load_answer_key_fixture(conn: sqlite3.Connection) -> None:
    """Populate responses, quotes, documents and answers, run layer 0, the rule engine and recompute_all."""
    key = pd.read_csv(KEY / "quotes_answer_key.csv")
    vendors = {r["vendor_code"]: r for r in conn.execute("SELECT * FROM vendors")}
    lines = {r["line_no"]: r for r in conn.execute("SELECT * FROM line_items")}
    due = conn.execute("SELECT response_due_date FROM rfqs").fetchone()[0]
    manifest = {m["vendor_code"]: m for m in json.loads(MANIFEST.read_text(encoding="utf-8"))}

    resp_id: dict[str, int] = {}
    for code, v in vendors.items():
        m = manifest[code]
        cur = conn.execute(
            """INSERT INTO vendor_responses (rfq_id, vendor_id, file_name, source_type, content_role, received_at,
                  received_after_due, extraction_status, price_basis)
               VALUES (1,?,?,?,?,?,?, 'extracted', ?)""",
            (v["vendor_id"], m["files"][0], SOURCE_TYPE[code], "mixed", m["received_at"],
             int(m["received_at"][:10] > due), "rm_linked" if code == "V3" else "firm"))
        resp_id[code] = cur.lastrowid

    quote_ids: dict[tuple[str, int], int] = {}
    for _, k in key[key.quote_status == "quoted"].iterrows():  # not_quoted rows come from layer 0, not from here
        code, ln = k.vendor_code, int(k.line_no)
        flags = str(k.expected_flags) if pd.notna(k.expected_flags) else ""
        unit = "per_piece" if "UNIT_AMBIGUOUS" in flags else k.raw_unit  # vendor header said Rate/box
        price = _nn(k.raw_price)
        fb = k.freight_basis
        tiers = discounts = charges = offered = capacity = note = None
        if "TIER_NOT_MET_PER_ORDER" in flags:
            tiers = [{"min_qty": 20000, "price": round(price * 0.96, 2), "qty_basis": "per_order"}]
        if "TIER_BASIS_UNCLEAR" in flags:
            tiers = [{"min_qty": 50000, "price": round(price * 0.97, 2), "qty_basis": "unclear"}]
        if code == "V2":
            discounts = [{"pct": 2, "condition_type": "payment_days_max", "condition_value": 15,
                          "condition_text": "2% if paid within 15 days", "source": "footnote", "applied": False}]
            if lines[ln]["print_colours"]:
                charges = [{"type": "plate", "amount": 2500, "basis": "one_time",
                            "colours": lines[ln]["print_colours"], "source": "per colour plates"}]
        if "PARTIAL_QTY" in flags:
            offered = int(lines[ln]["annual_qty"] * 8000 / 12000)
            capacity = "Capacity 8,000/month vs need 12,000/month"
        if k.spec_match != "exact":
            note = _nn(k.notes)
        ex_flags = ["LOW_CONFIDENCE_OCR"] if "LOW_CONFIDENCE_OCR" in flags else []
        meta = {"extraction": {"flags": ex_flags, "source": {"price": {"ref": f"{code} fixture", "snippet": k.raw_price_text}}}}
        cur = conn.execute(
            """INSERT INTO quotes (response_id, vendor_id, line_id, quote_status, raw_price_text, raw_price,
                  raw_currency, raw_unit, tax_basis, gst_rate_pct, price_tiers_json, discounts_json, charges_json,
                  freight_basis, raw_freight_value, payment_terms_days, offered_qty, capacity_text,
                  spec_match, spec_deviation_note, field_meta_json)
               VALUES (?,?,?, 'quoted', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (resp_id[code], vendors[code]["vendor_id"], lines[ln]["line_id"], k.raw_price_text, price,
             k.raw_currency, unit, k.tax_basis, _nn(k.gst_rate_pct),
             json.dumps(tiers) if tiers else None, json.dumps(discounts) if discounts else None,
             json.dumps(charges) if charges else None, fb,
             _nn(k.true_freight_inr_pc) if fb == "extra_quoted" else (0.0 if fb == "included" else None),
             PAYMENT_DAYS[code], offered, capacity, k.spec_match, note, json.dumps(meta)))
        quote_ids[(code, ln)] = cur.lastrowid

    # second read agrees with the first for every vendor that was read twice (not the email, not the smudged cell)
    for code, rid in resp_id.items():
        if code == "V5":
            continue
        sr = {}
        for (c, ln), qid in quote_ids.items():
            if c != code or (code == "V4" and ln == 14):
                continue
            r = conn.execute("SELECT raw_price, raw_unit, raw_currency, raw_freight_value FROM quotes WHERE quote_id=?",
                             (qid,)).fetchone()
            sr[str(qid)] = dict(r)
        conn.execute("UPDATE vendor_responses SET second_read_json=? WHERE response_id=?", (json.dumps(sr), rid))

    for code, docs in DOCS.items():
        for dtype, holder, expiry in docs:
            conn.execute(
                """INSERT INTO documents (rfq_id, vendor_id, response_id, doc_type, holder_name, expiry_date, confidence)
                   VALUES (1,?,?,?,?,?,0.99)""",
                (vendors[code]["vendor_id"], resp_id[code], dtype, holder or vendors[code]["name"], expiry))
    qid_of = {r["q_code"]: r["question_id"] for r in conn.execute("SELECT * FROM rfx_questions")}
    for code, ans in ANSWERS.items():
        for qcode, val in ans.items():
            conn.execute(
                """INSERT INTO questionnaire_answers (response_id, vendor_id, question_id, answer_raw, answer_value, confidence)
                   VALUES (?,?,?,?,?,0.99)""", (resp_id[code], vendors[code]["vendor_id"], qid_of[qcode], val, val))
    conn.commit()

    for rid in resp_id.values():  # layer 0 BEFORE any ranking
        ensure_completeness(conn, rid)
    evaluate_all(conn)
    recompute_all(conn)


def build_dev_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    init_db(conn)
    seed(conn)
    load_answer_key_fixture(conn)
    return conn
