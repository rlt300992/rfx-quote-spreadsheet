"""New-draft RFQ: stored separately from the demo RFQ (table rfq_drafts, one JSON document), SKU lookup against the item
catalog, section status, publish checks and the co-pilot edit operations. Nothing here writes to rfqs, line_items,
rfx_questions or quotes: the demo RFQ cannot be changed from a draft."""
from __future__ import annotations

import copy
import difflib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from core import audit

OVERVIEW_FIELDS = {"title": "Title", "category": "Category", "contract_type": "Contract type", "period": "Contract period",
                   "delivery": "Delivery (FOR)", "currency": "Currency"}
TERM_FIELDS = {"payment_days": "Payment terms (days)", "delivery_basis": "Delivery basis", "gst_pct": "GST",
               "response_due": "Response due", "award_ref": "Award reference date", "price_validity": "Price validity"}
SECTIONS = ["overview", "lines", "questions", "terms", "suppliers", "review"]
SECTION_TITLE = {"overview": "Overview", "lines": "Line items", "questions": "Questionnaire", "terms": "Terms & timeline",
                 "suppliers": "Suppliers", "review": "Review & publish"}
XLSX_NAME = "RFQ_Corrugated_FY27.xlsx"


def empty_draft() -> dict:
    return {"overview": {k: "" for k in OVERVIEW_FIELDS}, "lines": [], "unknown": [], "questions": [],
            "terms": {"payment_days": None, "delivery_basis": "", "gst_pct": None, "response_due": "", "award_ref": "", "price_validity": ""},
            "suppliers": [], "ai_fields": [], "active": "overview", "status": "draft", "published_at": None, "sent_to": []}


def is_empty(d: dict) -> bool:
    return (not any(d["overview"].values()) and not d["lines"] and not d["unknown"] and not d["questions"] and not d["suppliers"]
            and not any(v not in (None, "") for v in d["terms"].values()))


# ------------------------------------------------------------------ storage (its own table)
def load(conn: sqlite3.Connection) -> tuple[dict, str | None]:
    r = conn.execute("SELECT state_json, updated_at FROM rfq_drafts ORDER BY draft_id DESC LIMIT 1").fetchone()
    if r is None:
        return empty_draft(), None
    return {**empty_draft(), **json.loads(r["state_json"])}, r["updated_at"]


def save(conn: sqlite3.Connection, d: dict) -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    blob = json.dumps(d, ensure_ascii=False)
    r = conn.execute("SELECT draft_id, state_json FROM rfq_drafts ORDER BY draft_id DESC LIMIT 1").fetchone()
    if r is not None and r["state_json"] == blob:
        return conn.execute("SELECT updated_at FROM rfq_drafts WHERE draft_id=?", (r["draft_id"],)).fetchone()[0]
    if r is None or json.loads(r["state_json"]).get("status") == "published":
        conn.execute("INSERT INTO rfq_drafts (state_json, updated_at) VALUES (?, ?)", (blob, now))
    else:
        conn.execute("UPDATE rfq_drafts SET state_json=?, updated_at=? WHERE draft_id=?", (blob, now, r["draft_id"]))
    conn.commit()
    return now


def discard(conn: sqlite3.Connection) -> None:
    """Delete the unpublished draft (a published one is a record and stays)."""
    conn.execute("DELETE FROM rfq_drafts WHERE json_extract(state_json, '$.status') IS NOT 'published'")
    conn.commit()


# ------------------------------------------------------------------ the item catalog (seed line_items, read only)
def catalog(conn: sqlite3.Connection) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT * FROM line_items ORDER BY line_no"):
        out[r["catalog_sku"]] = {"sku": r["catalog_sku"], "description": r["description"], "length_mm": r["length_mm"], "width_mm": r["width_mm"],
                                 "height_mm": r["height_mm"], "ply": r["ply"], "bs": r["bursting_strength_kgcm2"],
                                 "annual_qty": r["annual_qty"], "typical_order_qty": r["typical_order_qty"]}
    return out


def last_year_skus(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT catalog_sku FROM last_year_prices ORDER BY catalog_sku")]


def lookup_sku(conn: sqlite3.Connection, code: str) -> dict:
    """{'status': 'found', 'item': {...}} or {'status': 'unknown', 'closest': sku | None}. An unknown code is never added."""
    cat = catalog(conn)
    c = re.sub(r"\s+", "", code or "").upper()
    if c in cat:
        return {"status": "found", "item": cat[c]}
    hit = difflib.get_close_matches(c, list(cat), n=1, cutoff=0.6)
    return {"status": "unknown", "code": c, "closest": hit[0] if hit else None}


def split_codes(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,;]+", text or "") if t]


def add_skus(conn: sqlite3.Connection, d: dict, codes: list[str], source: str = "paste") -> list[str]:
    """Add catalog items by code. Returns notes (what was added, skipped duplicates, unknown codes)."""
    notes, added = [], 0
    have = {ln["sku"] for ln in d["lines"]}
    for code in codes:
        r = lookup_sku(conn, code)
        if r["status"] == "found":
            if r["item"]["sku"] in have:
                notes.append(f"{r['item']['sku']} was already in the lines.")
                continue
            d["lines"].append({**r["item"], "source": source})
            have.add(r["item"]["sku"])
            added += 1
        else:
            if not any(u["code"] == r["code"] for u in d["unknown"]):
                d["unknown"].append({"code": r["code"], "closest": r["closest"]})
            notes.append(f"{r['code']} is not in the catalog, so I did not add it." + (f" Closest match: {r['closest']}." if r["closest"] else ""))
    if added:
        notes.insert(0, f"Added {added} line{'s' if added != 1 else ''}.")
    return notes


def add_last_year(conn: sqlite3.Connection, d: dict) -> list[str]:
    skus = last_year_skus(conn)
    notes = add_skus(conn, d, skus, source="last_year")
    return notes or ["Last year's lines are already all in."]


def load_template(conn: sqlite3.Connection, d: dict) -> list[str]:
    """The standard corrugated questionnaire = the 8 questions of the demo RFQ (read only)."""
    d["questions"] = []
    for q in conn.execute("SELECT * FROM rfx_questions ORDER BY question_id"):
        d["questions"].append({"q_code": q["q_code"], "text": q["question_text"], "answer_type": q["answer_type"], "rule_type": q["rule_type"],
                               "rule_params": json.loads(q["rule_params_json"] or "{}"), "is_knockout": bool(q["is_knockout"]),
                               "doc_required": bool(q["required_doc_type"]), "doc_type": q["required_doc_type"]})
    return [f"Loaded the standard template: {len(d['questions'])} questions."]


ANSWER_LABEL = {"document": "Document", "bool": "Yes/No", "number": "Number", "text": "Text"}


def pass_rule_text(q: dict) -> str:
    p = q.get("rule_params") or {}
    rt = q.get("rule_type")
    if rt == "doc_valid_on":
        return "Valid on award date"
    if rt == "equals":
        return f"Must be {str(p.get('value', '')).capitalize()}"
    if rt == "min":
        return f"≥ {p.get('min'):g}" if isinstance(p.get("min"), (int, float)) else "Minimum"
    if rt == "max":
        return f"≤ {p.get('max'):g}" if isinstance(p.get("max"), (int, float)) else "Maximum"
    if rt == "in_set":
        return " or ".join(str(a).capitalize() for a in p.get("allowed", []))
    return "Any answer"


def approved_suppliers(conn: sqlite3.Connection) -> list[dict]:
    """The approved vendor list for the category (the vendor master; all five are approved for corrugated)."""
    msme = {"micro": "Micro", "small": "Small", "medium": "Medium", "none": "Not MSME"}
    return [{"code": v["vendor_code"], "name": v["name"], "location": v["city"], "msme": msme.get(v["msme_category"], v["msme_category"]),
             "approved": True, "contact": v["contact_email"]} for v in conn.execute("SELECT * FROM vendors ORDER BY vendor_id")]


def from_demo(conn: sqlite3.Connection) -> dict:
    """Fill a draft with the demo RFQ (30 lines, 8 questions, terms, 5 suppliers). Read only on the demo tables."""
    d = empty_draft()
    v = demo_view(conn)
    d["overview"] = dict(v["overview"])
    for ln in conn.execute("SELECT * FROM line_items ORDER BY line_no"):
        d["lines"].append({"sku": ln["catalog_sku"], "description": ln["description"], "length_mm": ln["length_mm"], "width_mm": ln["width_mm"],
                           "height_mm": ln["height_mm"], "ply": ln["ply"], "bs": ln["bursting_strength_kgcm2"], "annual_qty": ln["annual_qty"],
                           "typical_order_qty": ln["typical_order_qty"], "source": "demo"})
    load_template(conn, d)
    d["terms"] = dict(v["terms"])
    d["suppliers"] = [s["code"] for s in approved_suppliers(conn)]
    return d


# ------------------------------------------------------------------ read-only view of the demo RFQ
def _plies(lines: list[dict]) -> str:
    n: dict[int, int] = {}
    for ln in lines:
        n[int(ln["ply"])] = n.get(int(ln["ply"]), 0) + 1
    return ", ".join(f"{c} × {p}-ply" for p, c in sorted(n.items()))


def _period(title: str) -> str:
    m = re.search(r"FY\d{4}-\d{2}", title or "")
    return f"{m.group()} (12 months)" if m else ""


def demo_view(conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    loc = r["delivery_location"] or ""
    short = loc.split(",", 2)[-1].replace("(FOR destination)", "").strip() if "," in loc else loc
    overview = {"title": r["title"], "category": r["category"], "contract_type": "Annual rate contract" if "rate contract" in (r["title"] or "").lower() else "",
                "period": _period(r["title"]), "delivery": short, "currency": "INR (₹)"}
    terms = {"payment_days": r["std_payment_terms_days"], "delivery_basis": "FOR Chakan (freight included)" if "Chakan" in loc else "FOR destination",
             "gst_pct": r["expected_gst_pct"], "response_due": r["response_due_date"], "award_ref": r["award_reference_date"], "price_validity": "6 months from PO"}
    return {"overview": overview, "terms": terms, "issued_at": r["issued_at"]}


# ------------------------------------------------------------------ status and review
def _due(d: dict) -> date | None:
    try:
        return date.fromisoformat(str(d["terms"]["response_due"])[:10])
    except ValueError:
        return None


def blocking_and_warnings(d: dict, today: date | None = None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """([(section, message)] blocking, [(section, message)] warnings)."""
    today = today or date.today()
    block, warn = [], []
    miss = [OVERVIEW_FIELDS[k] for k, v in d["overview"].items() if not str(v or "").strip()]
    if miss:
        block.append(("overview", "Overview is missing: " + ", ".join(miss)))
    if not d["lines"]:
        block.append(("lines", "No line items"))
    if not d["suppliers"]:
        block.append(("suppliers", "No suppliers selected"))
    due = _due(d)
    if due is not None and due < today:
        block.append(("terms", f"Response due {_fmt(due)} is in the past"))
    elif d["terms"]["response_due"] in ("", None):
        block.append(("terms", "No response due date"))
    if d["questions"] and not any(q["is_knockout"] for q in d["questions"]):
        warn.append(("questions", "No knock-out questions. Any vendor can qualify."))
    return block, warn


def _fmt(x: date | str | None) -> str:
    if not x:
        return ""
    if isinstance(x, str):
        try:
            x = date.fromisoformat(x[:10])
        except ValueError:
            return x
    return f"{x.day} {x:%b %Y}"


def section_status(d: dict, section: str, today: date | None = None) -> tuple[str, str]:
    """(label, colour) for one section chip: Not started / Needs input / Complete / '1 issue'."""
    block, _ = blocking_and_warnings(d, today)
    if section == "overview":
        n = sum(bool(str(v or "").strip()) for v in d["overview"].values())
        return ("Not started", "gray") if n == 0 else (("Complete", "green") if n == len(d["overview"]) else ("Needs input", "orange"))
    if section == "lines":
        if d["unknown"]:
            return "Needs input", "orange"
        return ("Complete", "green") if d["lines"] else ("Not started", "gray")
    if section == "questions":
        return ("Complete", "green") if d["questions"] else ("Not started", "gray")
    if section == "terms":
        n = sum(v not in (None, "") for v in d["terms"].values())
        if n == 0:
            return "Not started", "gray"
        if n < len(d["terms"]):
            return "Needs input", "orange"
        t = [m for s, m in block if s == "terms"]
        return (f"{len(t)} issue", "red") if t else ("Complete", "green")
    if section == "suppliers":
        return ("Complete", "green") if d["suppliers"] else ("Not started", "gray")
    if section == "review":
        if not sections_filled(d, today):
            return "Not started", "gray"
        return (f"{len(block)} issue", "red") if block else ("Complete", "green")
    raise KeyError(section)


def sections_filled(d: dict, today: date | None = None) -> bool:
    """Review & publish unlocks once sections 1-5 are filled in (a section with an issue still counts as filled)."""
    return all(section_status(d, s, today)[0] not in ("Not started", "Needs input") for s in SECTIONS[:5])


def section_summary(d: dict, section: str) -> str:
    o, t = d["overview"], d["terms"]
    if section == "overview":
        n = sum(bool(str(v or "").strip()) for v in o.values())
        return f"{n} of {len(o)} fields" if n < len(o) else " · ".join([o["category"], o["contract_type"], o["period"].split(" (")[0], "FOR Chakan" if "Chakan" in o["delivery"] else o["delivery"], o["currency"].split(" ")[0]])
    if section == "lines":
        if not d["lines"] and not d["unknown"]:
            return ""
        s = f"{len(d['lines'])} lines" + (f" · {len(d['unknown'])} SKU not found" if d["unknown"] else "")
        return s + (f" · {_plies(d['lines'])}" if d["lines"] and not d["unknown"] else "")
    if section == "questions":
        if not d["questions"]:
            return ""
        return f"{len(d['questions'])} questions · {sum(q['is_knockout'] for q in d['questions'])} knock-outs" + (
            f" · {sum(q['doc_required'] for q in d['questions'])} need a document" if any(q["doc_required"] for q in d["questions"]) else "")
    if section == "terms":
        n = sum(v not in (None, "") for v in t.values())
        if n == 0:
            return ""
        return f"{n} of {len(t)} fields" if n < len(t) else f"Payment {t['payment_days']} days · GST {t['gst_pct']:g}% extra · Due {_fmt(t['response_due'])}"
    if section == "suppliers":
        return f"{len(d['suppliers'])} approved vendors" if d["suppliers"] else ""
    return ""


# ------------------------------------------------------------------ cover note and attachment
def cover_note(d: dict) -> str:
    o, t = d["overview"], d["terms"]
    n_q = len(d["questions"])
    return (f"Dear Sir/Madam,\n\n"
            f"Sahyadri Consumer Products Ltd invites your quotation for {len(d['lines'])} {o['category'].lower() or 'item'} lines under "
            f"{('an ' if o['contract_type'][:1].lower() in 'aeiou' else 'a ') + o['contract_type'].lower() if o['contract_type'] else 'a contract'} "
            f"for {o['period'].split(' (')[0]}, delivered {t['delivery_basis'].split(' (')[0] or 'FOR our plant'}.\n\n"
            f"The attached workbook has the terms, the line items with specifications, and {'an ' if n_q == 8 else 'a '}{n_q}-question quality questionnaire. "
            f"You may fill in the workbook or reply in your own format; please quote in ₹ per piece, ex-GST, and attach the documents the questionnaire asks for.\n\n"
            f"Please reply by {_fmt(t['response_due'])}.\n\n"
            f"Regards,\nCategory Buyer, Sahyadri Consumer Products Ltd, Chakan MIDC, Pune")


def subject(d: dict) -> str:
    return f"RFQ: {d['overview']['title']}"


# ------------------------------------------------------------------ publish (stub)
def publish(conn: sqlite3.Connection, d: dict, actor: str = "buyer") -> dict:
    """Stub send: the draft becomes published and read-only; the audit log gets one row. Nothing outside rfq_drafts changes."""
    block, _ = blocking_and_warnings(d)
    if block:
        raise ValueError("Cannot publish: " + "; ".join(m for _, m in block))
    sup = {s["code"]: s for s in approved_suppliers(conn)}
    sent = [{"code": c, "email": sup[c]["contact"]} for c in d["suppliers"] if c in sup]
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    d["status"], d["published_at"], d["sent_to"] = "published", now, sent
    save(conn, d)
    audit.log(conn, "rfq_draft_publish", ("rfq_draft", None), new="published (stub)", actor=actor,
              evidence={"title": d["overview"]["title"], "vendors": sent, "lines": len(d["lines"]), "questions": len(d["questions"]),
                        "attachment": XLSX_NAME, "stub": True})
    conn.commit()
    return d
