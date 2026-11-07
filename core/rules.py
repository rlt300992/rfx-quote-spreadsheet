"""Questionnaire rule engine. Gemini only EXTRACTS answers; this module JUDGES them.

rule_result is pass / fail / unknown, from rfx_questions.rule_type + rule_params_json:
  equals | min | max | in_set | doc_valid_on
A knock-out answer that is missing or unknown leaves the vendor pending (see v_vendor_qualification).
A required document that is missing, expired on the award date or held by another company
fails / needs confirmation.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from core.config import load_config

YES = {"yes", "y", "true", "1", "accepted", "agree", "agreed", "ok"}
NO = {"no", "n", "false", "0", "declined", "not accepted"}


def _norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def holder_matches(holder: str | None, vendor: sqlite3.Row) -> bool:
    """Certificate holder must equal the vendor's name or a known alias (group companies do not count)."""
    h = _norm_name(holder)
    if not h:
        return False
    names = [vendor["name"], *(json.loads(vendor["known_aliases"]) if vendor["known_aliases"] else [])]
    return any(h == _norm_name(n) for n in names)


def refresh_document_matches(conn: sqlite3.Connection) -> None:
    """Set documents.holder_matches_vendor from holder name vs vendor name/aliases."""
    vendors = {v["vendor_id"]: v for v in conn.execute("SELECT * FROM vendors")}
    for d in conn.execute("SELECT doc_id, vendor_id, holder_name FROM documents").fetchall():
        v = vendors.get(d["vendor_id"])
        ok = None if v is None or not d["holder_name"] else int(holder_matches(d["holder_name"], v))
        conn.execute("UPDATE documents SET holder_matches_vendor=? WHERE doc_id=?", (ok, d["doc_id"]))
    conn.commit()


def _yesno(val: str | None) -> str | None:
    v = (val or "").strip().lower()
    return "yes" if v in YES else "no" if v in NO else None


def _number(val: str | None) -> float | None:
    m = re.search(r"-?\d+(?:[.,]\d+)?", (val or "").replace(",", ""))
    return float(m.group()) if m else None


def _doc_verdict(doc: sqlite3.Row | None, valid_on: str, holder_must_match: bool) -> tuple[str, str]:
    if doc is None:
        return "unknown", "no document supplied"
    label = f"{doc['doc_type']} {doc['doc_number'] or ''}".strip()
    if doc["expiry_date"] is None:
        return "unknown", f"{label} has no readable expiry date"
    if doc["expiry_date"] < valid_on:
        return "fail", f"{label} expired {doc['expiry_date']}, before the award date {valid_on}"
    if holder_must_match and doc["holder_matches_vendor"] == 0:
        return "unknown", f"{label} is held by '{doc['holder_name']}', not the quoting company; needs buyer confirmation"
    if holder_must_match and doc["holder_matches_vendor"] is None:
        return "unknown", f"{label} holder not yet checked against the vendor"
    return "pass", f"{label} valid until {doc['expiry_date']} (award date {valid_on}), holder matches vendor"


def judge(question: sqlite3.Row, answer: sqlite3.Row | None, doc: sqlite3.Row | None,
          award_date: str, cfg: dict) -> tuple[str, str, dict]:
    """Return (rule_result, explanation, snapshot) for one vendor x question."""
    params = json.loads(question["rule_params_json"] or "{}")
    rtype = question["rule_type"]
    snap = {"rule_type": rtype, "params": params, "award_reference_date": award_date,
            "required_doc_type": question["required_doc_type"]}
    val = (answer["answer_value"] if answer else None)
    val = val.strip() if isinstance(val, str) and val.strip() else None

    if rtype == "doc_valid_on":
        valid_on = award_date if params.get("valid_on") in (None, "award_reference_date") else params["valid_on"]
        snap["valid_on"] = valid_on
        yn = _yesno(val)
        if doc is not None:
            res, why = _doc_verdict(doc, valid_on, bool(params.get("holder_must_match")))
        elif yn == "no":
            res, why = "fail", "vendor states it does not hold this certificate"
        elif yn == "yes":
            res, why = "fail", "self-declared 'yes' with no document attached (no evidence)"
        else:
            res, why = "unknown", "no answer and no document"
    elif val is None:
        res, why = "unknown", "no answer received"
    elif rtype == "equals":
        want = str(params.get("value", "")).lower()
        if want in YES | NO:
            yn = _yesno(val)
            if yn is None:
                res, why = "unknown", f"answer '{val}' is conditional or unclear, not a plain yes/no"
            else:
                res = "pass" if yn == want else "fail"
                why = f"answered '{yn}', rule requires '{want}'"
        else:
            res = "pass" if val.lower() == want else "fail"
            why = f"answered '{val}', rule requires '{want}'"
    elif rtype in ("min", "max"):
        num = _number(val)
        limit = params.get(rtype)
        if num is None or limit is None:
            res, why = "unknown", f"could not read a number from '{val}'"
        else:
            ok = num >= limit if rtype == "min" else num <= limit
            res = "pass" if ok else "fail"
            why = f"answered {num:g}, rule requires {rtype} {limit:g}"
    elif rtype == "in_set":
        allowed = [str(a).lower() for a in params.get("allowed", [])]
        res = "pass" if val.lower() in allowed else "fail"
        why = f"answered '{val}', allowed: {', '.join(allowed)}"
    else:
        res, why = "unknown", f"unsupported rule_type {rtype!r}"

    # a 'yes' that must be evidenced by a document (e.g. test report)
    req = question["required_doc_type"]
    if rtype != "doc_valid_on" and req and res == "pass":
        if doc is None:
            res, why = "fail", f"{why}; but no {req} supplied (self-declared, no evidence)"
        elif doc["holder_matches_vendor"] == 0:
            res, why = "unknown", f"{why}; {req} is held by '{doc['holder_name']}', not the quoting company"

    # knock-out answers must be read with high confidence before they can pass or fail a vendor
    conf = answer["confidence"] if answer else None
    if rtype == "doc_valid_on" and doc is not None:  # the verdict comes from the document, so gate on ITS confidence
        conf = doc["confidence"]
    min_conf = cfg["thresholds"]["knockout_answer_min"]
    if question["is_knockout"] and res in ("pass", "fail") and conf is not None and conf < min_conf:
        res, why = "unknown", f"{why}; extraction confidence {conf:.0%} is below the {min_conf:.0%} knock-out minimum"
    return res, why, snap


def evaluate_all(conn: sqlite3.Connection, cfg: dict | None = None) -> int:
    """Judge every current answer, and create no rows for unanswered questions (the view treats them as unknown).
    Returns the number of answers judged."""
    cfg = cfg or load_config()
    refresh_document_matches(conn)
    award_date = conn.execute("SELECT award_reference_date FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()[0]
    qs = {q["question_id"]: q for q in conn.execute("SELECT * FROM rfx_questions")}
    n = 0
    for a in conn.execute("SELECT * FROM questionnaire_answers WHERE is_current=1").fetchall():
        q = qs[a["question_id"]]
        docs = []
        if q["required_doc_type"]:
            docs = conn.execute(
                "SELECT * FROM documents WHERE vendor_id=? AND doc_type=? ORDER BY expiry_date DESC",
                (a["vendor_id"], q["required_doc_type"])).fetchall()
        doc = docs[0] if docs else None
        res, why, snap = judge(q, a, doc, award_date, cfg)
        conn.execute(
            """UPDATE questionnaire_answers SET rule_result=?, rule_explanation=?, rule_snapshot_json=?,
               evidence_doc_id=?, evaluated_at=?, review_state=? WHERE answer_id=?""",
            (res, why, json.dumps(snap), doc["doc_id"] if doc else None,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "auto_ok" if res != "unknown" else "must_confirm", a["answer_id"]))
        n += 1
    conn.commit()
    return n
