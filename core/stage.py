"""RFQ stage (header chip) and the single most-urgent banner. Pure functions over a stats dict, so they are easy to test."""
from __future__ import annotations

import sqlite3
from datetime import date

from core.queries import pending_confirmation_count


def fmt_date(s: str | None, month_year: bool = False) -> str:
    """'2026-11-16' -> '16 Nov 2026' (or 'Nov 2026'); anything unparseable is returned as is."""
    if not s:
        return "n/a"
    try:
        d = date.fromisoformat(str(s)[:10])
    except ValueError:
        return str(s)
    return f"{d:%b %Y}" if month_year else f"{d.day} {d:%b %Y}"


def stats(conn: sqlite3.Connection, proposal_open: bool = False) -> dict:
    rfq = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    vendors = conn.execute("SELECT vendor_id, vendor_code FROM vendors ORDER BY vendor_id").fetchall()
    received, failed = [], []
    for v in vendors:
        rs = conn.execute("SELECT extraction_status, lines_quoted + lines_not_quoted + lines_unmatched AS n FROM v_response_summary WHERE vendor_id=?",
                          (v["vendor_id"],)).fetchall()
        if any(r["extraction_status"] in ("extracted", "partial") and r["n"] > 0 for r in rs):
            received.append(v["vendor_code"])
        elif any(r["extraction_status"] == "failed" for r in rs):
            failed.append(v["vendor_code"])
    awarded_at = conn.execute("SELECT MAX(awarded_at) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0]
    return {"rfq_status": rfq["status"] if rfq else "draft", "due": rfq["response_due_date"] if rfq else None,
            "total": len(vendors), "received": received, "failed": failed,
            "pending": pending_confirmation_count(conn), "awarded_at": awarded_at, "proposal_open": bool(proposal_open)}


def chip(s: dict) -> tuple[str, str]:
    """(label, colour): Awarded once an award is committed; Evaluating once any response is in; else Draft / Published."""
    if s["awarded_at"]:
        return "Awarded", "green"
    if s["received"]:
        return "Evaluating", "orange"
    return ("Draft", "gray") if (s["rfq_status"] or "draft") == "draft" else ("Published", "blue")


def banner(s: dict) -> tuple[str, str]:
    """(level, text); level is error / info / warning / success. The most urgent state wins."""
    due = fmt_date(s["due"])
    if s["failed"]:
        n = len(s["failed"])
        codes = ", ".join(s["failed"])
        return "error", (f"**{n} response{'s' if n > 1 else ''} couldn't be read ({codes}).** "
                         f"Re-extract {'them' if n > 1 else 'it'} in Responses.")
    if len(s["received"]) < s["total"]:
        return "info", f"**{len(s['received'])} of {s['total']} responses received.** Response due {due}."
    if s["pending"] > 0:
        return "warning", f"**{s['pending']} values awaiting confirmation.** These could change the award. Review them in Review & Reconcile."
    if s["awarded_at"]:
        return "success", f"**Award confirmed on {fmt_date(s['awarded_at'])}.** Download it in Analyst & Award."
    if s["proposal_open"]:
        return "info", "**Award proposed, awaiting your confirmation** in Analyst & Award."
    return "success", "**All values confirmed.** Ready to award in Analyst & Award."
