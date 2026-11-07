"""Light clarification loop: one drafted email per vendor listing its flagged items.

The ISSUES are collected deterministically from the database (so nothing is invented); Gemini only
phrases them as a polite email. Sending is stubbed. The buyer resolves replies manually (each resolution
is audited by core.actions in a later phase).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from core import audit
from core.config import load_config
from extract import cache
from extract.extract import load_prompt, prompt_version
from extract.gemini_client import generate, is_daily_quota_error, model_chain


def collect_issues(conn: sqlite3.Connection, vendor_code: str) -> list[str]:
    """Plain-language clarification items for one vendor, grouped so the email stays short."""
    v = conn.execute("SELECT * FROM vendors WHERE vendor_code=?", (vendor_code,)).fetchone()
    rows = conn.execute("""SELECT q.*, l.line_no, l.description, l.length_mm, l.width_mm, l.height_mm, l.ply
                           FROM quotes q LEFT JOIN line_items l USING (line_id)
                           WHERE q.vendor_id=? AND q.is_current=1 ORDER BY l.line_no""", (v["vendor_id"],)).fetchall()
    by_flag: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        flags = (json.loads(r["field_meta_json"]).get("flags") if r["field_meta_json"] else []) or []
        for f in flags:
            by_flag.setdefault(f, []).append(r)

    def lines(rs: list[sqlite3.Row]) -> str:
        return ", ".join(f"line {r['line_no']}" for r in rs)

    items: list[str] = []
    notq = [r for r in rows if r["quote_status"] == "not_quoted"]
    if notq:
        items.append("No price was found for " + ", ".join(
            f"line {r['line_no']} ({r['description']}, {r['length_mm']}x{r['width_mm']}x{r['height_mm']} mm, {r['ply']}-ply)" for r in notq)
            + ". Please quote these items or confirm that you are not bidding for them.")
    unm = [r for r in rows if r["quote_status"] == "unmatched"]
    if unm:
        items.append("We could not match these items in your reply to our list: " + "; ".join(r["vendor_line_ref"] or "?" for r in unm)
                     + ". Please confirm the box size and ply for each.")
    if "FREIGHT_UNKNOWN" in by_flag or "FREIGHT_IMPUTED" in by_flag:
        rs = by_flag.get("FREIGHT_UNKNOWN", []) + by_flag.get("FREIGHT_IMPUTED", [])
        items.append(f"Freight is not stated for {len(rs)} line(s). Please confirm the freight per box up to our Chakan MIDC plant "
                     "for 3-ply, 5-ply and 7-ply boxes, or confirm that your rates are delivered (FOR Chakan).")
    if "UNIT_AMBIGUOUS" in by_flag:
        items.append("Some rates look unusually high for the unit stated: " + "; ".join(
            f"line {r['line_no']} ({r['raw_price']:g} shown as '{r['raw_unit']}')" for r in by_flag["UNIT_AMBIGUOUS"] if r["raw_price"] is not None)
            + ". Please confirm whether these rates are per box or per 100 boxes.")
    if "DERIVED_PER_KG" in by_flag:
        items.append(f"You quoted per kg for {len(by_flag['DERIVED_PER_KG'])} line(s). Please give per-box prices, or tell us the box weight you assumed.")
    if "IMPLIED_BY_BLANKET" in by_flag:
        changed = {r["line_no"] for r in by_flag.get("LY_SPEC_CHANGED", [])}
        same = [r for r in by_flag["IMPLIED_BY_BLANKET"] if r["line_no"] not in changed]
        if same:
            items.append(f"Please confirm the exact per-box price for {lines(same)}, which you quoted as 'same as last year'.")
        if changed:
            items.append(f"The specification for {', '.join('line %d' % n for n in sorted(changed))} has changed since last year, so last year's price may "
                         "not apply. Please quote the current price for the specification in our RFQ.")
    if "SPEC_SUBSTITUTE" in by_flag or "SPEC_DEVIATION" in by_flag:
        for r in by_flag.get("SPEC_SUBSTITUTE", []) + by_flag.get("SPEC_DEVIATION", []):
            items.append(f"Line {r['line_no']}: {r['spec_deviation_note']}. Please confirm this is an alternative you are offering and send supporting test data, "
                         "or quote the specification in our RFQ.")
    if "TIER_BASIS_UNCLEAR" in by_flag:
        items.append(f"For the volume discount on {lines(by_flag['TIER_BASIS_UNCLEAR'])}: is the quantity per order or per year?")
    if "PRICE_NOT_FIRM" in by_flag:
        items.append("Your prices are linked to raw-material (kraft paper) costs. Please state the validity period of the rates, the index used and the "
                     "price-change formula.")
    if "PARTIAL_QTY" in by_flag:
        items.append(f"Your capacity for {lines(by_flag['PARTIAL_QTY'])} is below our annual requirement. Please confirm the quantity you can commit.")
    if "LOW_CONFIDENCE_OCR" in by_flag:
        items.append(f"The price for {lines(by_flag['LOW_CONFIDENCE_OCR'])} is not legible in the copy we received. Please re-send or confirm it.")
    if "MSME_TERMS_CAPPED" in by_flag:
        items.append("You quoted credit terms longer than 45 days. Please confirm your Udyam category and that payment within 45 days from "
                     "acceptance is acceptable.")
    if "MSME_CATEGORY_MISMATCH" in by_flag:
        items.append("The Udyam/MSME category in your reply differs from our records. Please confirm your current category and registration number.")
    if "GST_RATE_MISMATCH" in by_flag:
        rate = by_flag["GST_RATE_MISMATCH"][0]["gst_rate_pct"]
        items.append(f"You have quoted GST at {rate:g}%. Please confirm the GST rate applicable to corrugated boxes that you will charge.")
    if "TAX_BASIS_UNSPECIFIED" in by_flag:
        items.append("Please confirm whether your rates are exclusive or inclusive of GST.")

    # questionnaire: anything that is not a clean pass on a knock-out question
    qa = conn.execute("""SELECT q.q_code, q.question_text, q.is_knockout, a.rule_result, a.rule_explanation
                         FROM rfx_questions q LEFT JOIN questionnaire_answers a
                           ON a.question_id=q.question_id AND a.vendor_id=? AND a.is_current=1
                         WHERE q.is_knockout=1 ORDER BY q.question_id""", (v["vendor_id"],)).fetchall()
    unanswered = [r for r in qa if r["rule_result"] is None]
    if unanswered and len(unanswered) == len(qa):
        items.append("We have not received your response to our quality questionnaire. Please complete it and attach the ISO 9001 certificate "
                     "and a recent bursting-strength test report.")
    else:
        for r in qa:
            if r["rule_result"] is None:
                items.append(f"Please answer: {r['question_text']}")
            elif r["rule_result"] == "unknown":
                items.append(f"{r['q_code']}: {r['rule_explanation']}. Please clarify.")
            elif r["rule_result"] == "fail":
                items.append(f"{r['q_code']}: {r['rule_explanation']}. If you have a current certificate or evidence, please send it.")
    return items


def _issues_hash(vendor_code: str, items: list[str], prompt: str) -> str:
    return hashlib.sha256(json.dumps([vendor_code, items, prompt]).encode("utf-8")).hexdigest()


def draft_for_vendor(conn: sqlite3.Connection, vendor_code: str, cfg: dict | None = None, force: bool = False) -> str | None:
    """Draft (and store) the clarification email for one vendor. Returns the email text, or None if nothing to ask."""
    cfg = cfg or load_config()
    v = conn.execute("SELECT * FROM vendors WHERE vendor_code=?", (vendor_code,)).fetchone()
    resp = conn.execute("""SELECT response_id FROM vendor_responses vr WHERE vendor_id=? AND content_role IN ('quote','mixed')
                           AND EXISTS (SELECT 1 FROM quotes q WHERE q.response_id=vr.response_id)
                           AND NOT EXISTS (SELECT 1 FROM vendor_responses n WHERE n.supersedes_response_id=vr.response_id)
                           ORDER BY response_id DESC LIMIT 1""", (v["vendor_id"],)).fetchone()
    if resp is None:
        return None
    items = collect_issues(conn, vendor_code)
    if not items:
        conn.execute("UPDATE vendor_responses SET clarification_draft=NULL, clarification_status='none' WHERE response_id=?", (resp["response_id"],))
        conn.commit()
        return None
    rfq = conn.execute("SELECT title FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()["title"]
    prompt = load_prompt("clarify", prompt_version("clarify", cfg))
    filled = (prompt.replace("{{VENDOR}}", f"{v['name']} (contact: {v['contact_email']})").replace("{{RFQ}}", rfq)
              .replace("{{ITEMS}}", "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))))
    key = _issues_hash(vendor_code, items, filled)
    version = prompt_version("clarify", cfg)
    chain = model_chain("clarify", cfg)
    text = None
    if not force:  # a draft cached under any model in the chain is reused
        for m in chain:
            hit = cache.load(key, "clarify", version, m)
            if hit is not None:
                text = hit["result"]
                break
    if text is None:
        last: Exception | None = None
        for m in chain:  # daily quota exhausted on one model -> next model
            try:
                text = generate(filled, model=m).text.strip()
            except Exception as e:
                if is_daily_quota_error(e):
                    last = e
                    continue
                raise
            cache.save(key, "clarify", version, m, text, {"items": len(items), "model": m})
            break
        if text is None:
            raise RuntimeError(f"All models in {chain} have exhausted their daily quota for clarification drafts") from last
    conn.execute("UPDATE vendor_responses SET clarification_draft=?, clarification_status='drafted' WHERE response_id=?",
                 (text, resp["response_id"]))
    audit.log(conn, "clarification_drafted", ("response", resp["response_id"]), new=f"{len(items)} items", actor="gemini")
    conn.commit()
    return text
