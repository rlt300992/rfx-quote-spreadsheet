"""The award memo and the supplier notices: built from the database (committed award, else the staged proposal) and the answers the buyer pinned.
No model call. HTML for the preview, PDF (reportlab) for the download, plain rows for the Excel 'Memo' sheet."""
from __future__ import annotations

import html
import io
import json
import sqlite3
from datetime import date, datetime

import pandas as pd

from agent import tools as T
from core.config import load_config
from core.review_items import open_item_count
from core.stage import fmt_date

DASH = "·"


def inr(x: float | None, signed: bool = False) -> str:
    if x is None or pd.isna(x):
        return "n/a"
    a = abs(x)
    s = f"₹{a / 1e7:,.2f} cr" if a >= 1e7 else f"₹{a / 1e5:,.1f} L" if a >= 1e5 else f"₹{a:,.0f}"
    return ("-" if x < 0 else "+" if signed and x > 0 else "") + s


def current_award(conn: sqlite3.Connection, proposal: dict | None) -> dict:
    """{'status': 'awarded'|'proposed'|'none', 'rows', 'totals', 'batch', 'at', 'overrides'} for the memo, the panel and the notices."""
    committed = conn.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0]
    if committed:
        g = pd.read_sql_query("SELECT * FROM v_comparison_grid", conn)
        rows = []
        for r in conn.execute("SELECT line_no, awarded_quote_id, l1_eligible_quote_id, bypass_reason_code, awarded_at FROM line_items ORDER BY line_no"):
            if r["awarded_quote_id"] is None:
                continue
            q = g[g["quote_id"] == r["awarded_quote_id"]].iloc[0]
            others = g[(g["line_no"] == q["line_no"]) & (g["is_awardable"] == 1) & (g["quote_id"] != r["awarded_quote_id"])].sort_values("evaluated_inr_pc")
            rows.append({"line_no": int(q["line_no"]), "catalog_sku": q["catalog_sku"], "description": q["description"], "annual_qty": q["annual_qty"],
                         "vendor_code": q["vendor_code"], "quote_id": int(q["quote_id"]), "price_inr_pc": q["evaluated_inr_pc"], "landed_inr_pc": q["landed_inr_pc"],
                         "annual_value_inr": q["evaluated_inr_pc"] * q["annual_qty"], "price_not_firm": bool(q["price_not_firm"]),
                         "freight_imputed": int(q["freight_imputed"] or 0), "review_state": q["review_state"], "ly_landed_inr_pc": q["ly_landed_inr_pc"],
                         "backup_vendor": others.iloc[0]["vendor_code"] if len(others) else None,
                         "backup_price_inr_pc": others.iloc[0]["evaluated_inr_pc"] if len(others) else None, "bypass_code": r["bypass_reason_code"],
                         "l1_eligible_quote_id": r["l1_eligible_quote_id"], "awarded_at": r["awarded_at"]})
        a = conn.execute("SELECT batch_id, MIN(ts) ts, COUNT(*) n, actor FROM audit_log WHERE action='award' GROUP BY batch_id ORDER BY MAX(audit_id) DESC LIMIT 1").fetchone()
        return {"status": "awarded", "rows": rows, "totals": T.totals_from_rows(rows), "batch": a["batch_id"] if a else None, "at": a["ts"] if a else None,
                "actor": a["actor"] if a else "buyer", "overrides": [], "audit_n": a["n"] if a else len(rows)}
    if proposal:
        return {"status": "proposed", "rows": proposal["rows"], "totals": proposal["totals"], "batch": None, "at": proposal.get("created_at"),
                "overrides": proposal.get("overrides", []), "params": proposal.get("params", {}), "prompt": proposal.get("chat_prompt", "")}
    return {"status": "none", "rows": [], "totals": {}, "batch": None, "at": None, "overrides": []}


def exceptions(conn: sqlite3.Connection, award: dict, line_reasons: dict | None = None) -> list[dict]:
    """Lines not on the cheapest eligible bid: awarded vs cheapest eligible, the extra INR a year, the reason code and text."""
    if award["status"] == "none":
        return []
    rev = T.award_review(conn, {"rows": award["rows"]}) if award["status"] == "proposed" else None
    g = pd.read_sql_query("SELECT * FROM v_comparison_grid", conn)
    out = []
    rows = {int(r["line_no"]): r for r in award["rows"]}
    if rev is None:                                  # committed: the commit stored the cheapest eligible quote id and the reason code
        for ln, r in rows.items():
            if r.get("l1_eligible_quote_id") and int(r["l1_eligible_quote_id"]) != int(r["quote_id"]):
                rev_row = {"line_no": ln, "awarded_vendor": r["vendor_code"], "l1_eligible_quote_id": int(r["l1_eligible_quote_id"])}
                aud = conn.execute("SELECT reason FROM audit_log WHERE action='award' AND entity_id=? ORDER BY audit_id DESC LIMIT 1", (ln,)).fetchone()
                l1 = g[g["quote_id"] == rev_row["l1_eligible_quote_id"]].iloc[0]
                out.append({"line_no": ln, "desc": r["description"], "awarded_vendor": r["vendor_code"], "awarded_price": r["price_inr_pc"], "l1_vendor": l1["vendor_code"],
                            "l1_price": l1["evaluated_inr_pc"], "extra_per_year": (r["price_inr_pc"] - l1["evaluated_inr_pc"]) * r["annual_qty"],
                            "code": r.get("bypass_code"), "reason": aud["reason"] if aud else ""})
        return out
    for _, v in rev[rev["bypass"]].iterrows():
        ln = int(v["line_no"])
        r = rows[ln]
        l1 = g[g["quote_id"] == v["l1_eligible_quote_id"]].iloc[0] if v["l1_eligible_quote_id"] else None
        code, why = (line_reasons or {}).get(ln, (None, None))
        out.append({"line_no": ln, "desc": r["description"], "awarded_vendor": r["vendor_code"], "awarded_price": r["price_inr_pc"],
                    "l1_vendor": None if l1 is None else l1["vendor_code"], "l1_price": None if l1 is None else l1["evaluated_inr_pc"],
                    "extra_per_year": 0 if l1 is None else (r["price_inr_pc"] - l1["evaluated_inr_pc"]) * r["annual_qty"], "code": code, "reason": why or ""})
    return out


def risks(conn: sqlite3.Connection, totals: dict) -> list[str]:
    out = []
    if totals.get("value_on_not_firm_prices_inr"):
        out.append(f"{inr(totals['value_on_not_firm_prices_inr'])} on {', '.join(totals['not_firm_vendors'])} prices linked to the kraft paper index (not firm)")
    n = open_item_count(conn)
    if n:
        out.append(f"{n} extracted item{'s' if n != 1 else ''} not yet reviewed (they could change winners)")
    if totals.get("lines_with_imputed_freight"):
        out.append(f"{totals['lines_with_imputed_freight']} awarded lines use estimated freight")
    return out


def memo_data(conn: sqlite3.Connection, proposal: dict | None, pinned: list[dict], buyer: str = "the buyer", line_reasons: dict | None = None) -> dict:
    cfg = load_config()
    aw = current_award(conn, proposal)
    rfq = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    names = {r["vendor_code"]: r["name"] for r in conn.execute("SELECT vendor_code, name FROM vendors")}
    t = aw["totals"]
    by = t.get("lines_by_vendor", {})
    parts = " and ".join(f"{names[v]} ({v}, {n} lines)" for v, n in sorted(by.items(), key=lambda kv: -kv[1]))
    sav = t.get("saving_vs_ly_inr", 0.0)
    rec = (f"Award {t.get('lines_awarded', 0)} lines to {parts} for {inr(t.get('total_value_inr'))} a year, "
           + (f"{inr(sav)} ({t.get('saving_vs_ly_pct', 0):.1f}%) below last year on the same lines." if sav >= 0 else f"{inr(-sav)} ({-t.get('saving_vs_ly_pct', 0):.1f}%) above last year on the same lines.")) if by else "No award has been proposed yet."
    q = pd.read_sql_query("SELECT * FROM v_vendor_qualification ORDER BY vendor_code", conn)
    qual = q[q["qual_status"] == "qualified"]["vendor_code"].tolist()
    pend = q[q["qual_status"] == "pending"]["vendor_code"].tolist()
    dq = q[q["qual_status"] == "disqualified"]
    dq_txt = "; ".join(f"{r['vendor_code']} disqualified ({str(r['ko_issues']).split(': ', 1)[-1].split('; ')[0]})" for _, r in dq.iterrows())
    basis = (f"Cheapest evaluated price per line (landed incl. freight, plus payment cost at {cfg.get('cost_of_funds_pct', 10.9)}%, GST excluded) among suppliers that "
             f"met all knock-out questions ({', '.join(qual)}). {', '.join(pend)} pending (open questionnaire answers); {dq_txt}.")
    due = rfq["response_due_date"]
    n_resp = conn.execute("SELECT COUNT(DISTINCT vendor_id) FROM quotes WHERE is_current=1").fetchone()[0]
    n_late = conn.execute("SELECT COUNT(DISTINCT vendor_id) FROM vendor_responses WHERE received_after_due=1").fetchone()[0]
    n_v = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
    return {"title": rfq["title"], "status": aw["status"],
            "sub": f"RFQ issued {fmt_date(rfq['issued_at'])} {DASH} {n_v} suppliers invited {DASH} {n_resp} replied ({n_late} late) {DASH} prepared {fmt_date(date.today().isoformat())}",
            "recommendation": rec, "basis": basis, "exceptions": exceptions(conn, aw, line_reasons), "risks": risks(conn, t),
            "pinned": [{"title": p["title"], "text": p["text"]} for p in pinned],
            "next": "Notify suppliers · resolve the pending suppliers' questionnaires · issue the rate contract and release POs.",
            "audit": (f"Audit trail: batch {aw['batch'][:8]}, {aw.get('audit_n', len(aw['rows']))} entries · full detail in the Excel award pack."
                      if aw["batch"] else "Not yet confirmed: the audit reference is created when the award is confirmed."),
            "buyer": buyer, "due": due}


def memo_html(m: dict) -> str:
    e = html.escape
    ex = "".join(f"<tr><td>{r['line_no']}</td><td>{e(r['awarded_vendor'])} ₹{r['awarded_price']:.2f}</td><td>{e(str(r['l1_vendor']))} ₹{(r['l1_price'] or 0):.2f}</td>"
                 f"<td>₹{r['extra_per_year']:,.0f}</td><td>{e(str(r['code'] or ''))}: {e(r['reason'])}</td></tr>" for r in m["exceptions"])
    ex_html = (f"<table class='am'><tr><th>Line</th><th>Awarded</th><th>Cheapest eligible</th><th>Extra / yr</th><th>Reason</th></tr>{ex}</table>"
               if ex else "<p>None: every line is on the cheapest eligible bid.</p>")
    pins = "".join(f"<li><b>{e(p['title'])}</b>: {e(p['text'])}</li>" for p in m["pinned"]) or "<li>None pinned.</li>"
    return (f"<div class='am-doc'><div class='am-top'>SAHYADRI CONSUMER PRODUCTS LTD · PROCUREMENT · AWARD RECOMMENDATION · "
            f"{'FINAL' if m['status'] == 'awarded' else 'DRAFT'}</div><h2>{e(m['title'])}</h2><div class='am-sub'>{e(m['sub'])}</div>"
            f"<h4>RECOMMENDATION</h4><p>{e(m['recommendation'])}</p><h4>BASIS</h4><p>{e(m['basis'])}</p><h4>EXCEPTIONS</h4>{ex_html}"
            f"<h4>RISKS ACCEPTED</h4><p>{e('. '.join(m['risks']) + '.' if m['risks'] else 'None recorded.')}</p>"
            f"<h4>ANALYST FINDINGS PINNED</h4><ul>{pins}</ul><h4>NEXT STEPS</h4><p>{e(m['next'])}</p><div class='am-audit'>{e(m['audit'])}</div></div>")


MEMO_CSS = """<style>
.am-doc{background:#fff;color:#111;padding:22px 26px;border-radius:6px;font-family:Georgia,serif;font-size:14px;line-height:1.55}
.am-doc h2{font-family:Arial,sans-serif;margin:6px 0 2px;font-size:22px}.am-doc h4{font-family:Arial,sans-serif;color:#1f3a5f;margin:16px 0 4px;font-size:13px;letter-spacing:.5px}
.am-top{font:12px Arial,sans-serif;color:#555}.am-sub{font:12.5px Arial,sans-serif;color:#555}.am-audit{margin-top:14px;font:11.5px Arial,sans-serif;color:#777}
.am-doc table{border-collapse:collapse;width:100%;font:12.5px Arial,sans-serif}.am-doc th,.am-doc td{border:1px solid #bbb;padding:5px 8px;text-align:left}
.am-doc p{margin:0}.am-doc ul{margin:0;padding-left:18px}
</style>"""


def memo_rows(m: dict) -> list[tuple[str, str]]:
    rows = [("Award memo", m["title"]), ("", m["sub"]), ("Recommendation", m["recommendation"]), ("Basis", m["basis"])]
    rows += [("Exception", f"Line {r['line_no']}: {r['awarded_vendor']} ₹{r['awarded_price']:.2f} instead of {r['l1_vendor']} ₹{(r['l1_price'] or 0):.2f} (+₹{r['extra_per_year']:,.0f} a year). {r['code']}: {r['reason']}") for r in m["exceptions"]] or [("Exceptions", "None")]
    rows += [("Risks accepted", x) for x in m["risks"]] or [("Risks accepted", "None recorded")]
    rows += [("Pinned finding", f"{p['title']}: {p['text']}") for p in m["pinned"]]
    rows += [("Next steps", m["next"]), ("Audit reference", m["audit"])]
    return rows


def memo_pdf(m: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    st = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=st["BodyText"], fontSize=10, leading=14)
    h = ParagraphStyle("h", parent=st["Heading4"], textColor=colors.HexColor("#1f3a5f"), spaceBefore=10, spaceAfter=2)
    small = ParagraphStyle("s", parent=body, fontSize=8.5, textColor=colors.grey)
    esc = html.escape
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title="Award memo")
    el = [Paragraph("SAHYADRI CONSUMER PRODUCTS LTD · PROCUREMENT · AWARD RECOMMENDATION · " + ("FINAL" if m["status"] == "awarded" else "DRAFT"), small),
          Paragraph(esc(m["title"]), st["Heading2"]), Paragraph(esc(m["sub"]), small),
          Paragraph("RECOMMENDATION", h), Paragraph(esc(m["recommendation"]), body), Paragraph("BASIS", h), Paragraph(esc(m["basis"]), body), Paragraph("EXCEPTIONS", h)]
    if m["exceptions"]:
        data = [["Line", "Awarded", "Cheapest eligible", "Extra / yr", "Reason"]] + [
            [str(r["line_no"]), f"{r['awarded_vendor']} Rs {r['awarded_price']:.2f}", f"{r['l1_vendor']} Rs {(r['l1_price'] or 0):.2f}", f"Rs {r['extra_per_year']:,.0f}",
             Paragraph(esc(f"{r['code']}: {r['reason']}"), small)] for r in m["exceptions"]]
        t = Table(data, colWidths=[12 * mm, 32 * mm, 34 * mm, 24 * mm, 70 * mm])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee"))]))
        el.append(t)
    else:
        el.append(Paragraph("None: every line is on the cheapest eligible bid.", body))
    el += [Paragraph("RISKS ACCEPTED", h), Paragraph(esc(". ".join(m["risks"]) + "." if m["risks"] else "None recorded."), body), Paragraph("ANALYST FINDINGS PINNED", h)]
    el += [Paragraph("• " + esc(f"{p['title']}: {p['text']}"), body) for p in m["pinned"]] or [Paragraph("None pinned.", body)]
    el += [Paragraph("NEXT STEPS", h), Paragraph(esc(m["next"]), body), Spacer(1, 8), Paragraph(esc(m["audit"]), small)]
    doc.build(el)
    return buf.getvalue()


# ------------------------------------------------------------------ supplier notices (stub)
def notices(conn: sqlite3.Connection, award: dict) -> list[dict]:
    """One email per supplier: winners (lines and value) and non-winners (a plain reason). Text only; sending is a stub."""
    rfq = conn.execute("SELECT title FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()["title"]
    g = pd.read_sql_query("SELECT * FROM v_comparison_grid", conn)
    out = []
    won: dict[str, list[dict]] = {}
    for r in award["rows"]:
        won.setdefault(r["vendor_code"], []).append(r)
    for v in conn.execute("SELECT * FROM vendors ORDER BY vendor_id"):
        code = v["vendor_code"]
        mine = won.get(code, [])
        value = sum(r["annual_value_inr"] for r in mine)
        if mine:
            lines = ", ".join(str(r["line_no"]) for r in sorted(mine, key=lambda r: r["line_no"]))
            body = (f"Dear {v['name']},\n\nThank you for your quotation for {rfq}. We are pleased to confirm that we intend to award you {len(mine)} lines "
                    f"(lines {lines}), at the prices in your quotation, subject to the rate contract and our standard terms. Annual value on our evaluation: {inr(value)}.\n"
                    "We will send the rate contract shortly.\n\nRegards,\nCategory Buyer, Sahyadri Consumer Products Ltd")
            reason = None
        else:
            sub = g[g["vendor_code"] == code]
            qs = sub.iloc[0]["qual_status"] if len(sub) else ""
            if qs == "disqualified":
                reason = "did not meet a knock-out requirement"
            elif qs == "pending":
                reason = "has open questionnaire answers"
            else:
                reason = "was not the most competitive on the lines it quoted"
            body = (f"Dear {v['name']},\n\nThank you for your quotation for {rfq}. After evaluation we will not be awarding you business in this round ({reason}). "
                    "We value the relationship and will keep you in mind for future requirements.\n\nRegards,\nCategory Buyer, Sahyadri Consumer Products Ltd")
        out.append({"code": code, "name": v["name"], "email": v["contact_email"], "won": bool(mine), "lines": len(mine), "value": value, "reason": reason,
                    "subject": f"RFQ outcome: {rfq}", "body": body})
    return out
