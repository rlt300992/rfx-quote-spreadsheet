"""Data for the Responses tab cards: the vendor's main reply, the questionnaire with linked evidence, other attachments."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.stage import fmt_date

ROOT = Path(__file__).resolve().parent.parent
FORMAT_LABEL = {"excel": "Excel", "pdf": "PDF", "word": "Word", "photo": "Photo", "email_text": "Email"}
ROLE_LABEL = {"quote": "Rate card", "mixed": "Quote and answers", "questionnaire": "Questionnaire", "certificate": "Certificate"}
DOC_LABEL = {"ISO9001": "ISO 9001", "test_report": "Test report", "FSC": "FSC chain of custody", "udyam_msme": "Udyam (MSME)",
             "gst_cert": "GST certificate", "other": "Document"}
SIZE_LABEL = {"micro": "micro enterprise", "small": "small enterprise", "medium": "medium enterprise", "none": "large, not MSME"}
QUAL_CHIP = {"qualified": ("Qualified", "green"), "pending": ("Pending", "orange"), "disqualified": ("Disqualified", "red")}


def resolve_path(file_path: str | None, file_name: str | None) -> Path | None:
    """The stored path is relative to the repo; fall back to the inbox by file name."""
    for cand in (Path(file_path) if file_path else None, (ROOT / file_path) if file_path else None,
                 (ROOT / "data" / "inbox" / file_name) if file_name else None):
        if cand is not None and cand.is_file():
            return cand
    return None


def file_kind(path: Path | str) -> str:
    ext = Path(path).suffix.lower()
    return {".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".pdf": "pdf", ".xlsx": "excel", ".xlsm": "excel",
            ".docx": "word", ".eml": "email"}.get(ext, "other")


def doc_facts(d: sqlite3.Row) -> str:
    """Key facts of a certificate / report, e.g. 'ISO 9001 · valid to Aug 2027' or 'ISO 9001 · expired 31 Mar 2026'."""
    parts = [DOC_LABEL.get(d["doc_type"], "Document")]
    if d["validity_status"] == "expired":
        parts.append(f"expired {fmt_date(d['expiry_date'])}")
    elif d["expiry_date"]:
        parts.append(f"valid to {fmt_date(d['expiry_date'], month_year=True)}")
    elif d["issue_date"]:
        parts.append(f"dated {fmt_date(d['issue_date'])}")
    if d["holder_matches_vendor"] == 0 and d["holder_name"]:
        parts.append(f"held by {d['holder_name']}, not this vendor")
    return " · ".join(parts)


def questionnaire_view(conn: sqlite3.Connection, vendor_id: int, main_file: str | None) -> dict:
    qs = conn.execute("SELECT * FROM rfx_questions ORDER BY question_id").fetchall()
    ans = {a["question_id"]: a for a in conn.execute(
        "SELECT * FROM questionnaire_answers WHERE vendor_id=? AND is_current=1", (vendor_id,))}
    docs = {d["doc_id"]: d for d in conn.execute(
        """SELECT vd.*, d.file_path FROM v_documents vd JOIN documents d USING (doc_id) WHERE vd.vendor_id=?""", (vendor_id,))}
    rows, used_files = [], set()
    for q in qs:
        a = ans.get(q["question_id"])
        ev = None
        if a is not None and a["evidence_doc_id"] in docs:
            d = docs[a["evidence_doc_id"]]
            ev = {"facts": doc_facts(d), "file_name": d["file_name"], "file_path": d["file_path"], "expired": d["validity_status"] == "expired"}
            used_files.add(d["file_name"])
        ref = None
        if a is not None and a["source_ref_json"]:
            try:
                ref = json.loads(a["source_ref_json"]).get("ref")
            except (ValueError, AttributeError):
                ref = None
        rows.append({"q_code": q["q_code"], "text": q["question_text"], "is_knockout": bool(q["is_knockout"]),
                     "result": (a["rule_result"] if a is not None and a["rule_result"] else None) if a is not None else None,
                     "answered": a is not None, "answer": a["answer_raw"] if a is not None else None, "ref": ref, "evidence": ev})
    met = sum(r["result"] == "pass" for r in rows)
    not_met = sum(r["result"] == "fail" for r in rows)
    other, seen = [], set()
    for r in conn.execute("""SELECT file_name, file_path FROM vendor_responses vr WHERE vendor_id=?
                             AND NOT EXISTS (SELECT 1 FROM vendor_responses n WHERE n.supersedes_response_id = vr.response_id)
                             ORDER BY response_id""", (vendor_id,)):
        if r["file_name"] in seen or r["file_name"] == main_file or r["file_name"] in used_files:
            continue
        seen.add(r["file_name"])
        other.append({"file_name": r["file_name"], "file_path": r["file_path"]})
    qual = conn.execute("SELECT qual_status, ko_total FROM v_vendor_qualification WHERE vendor_id=?", (vendor_id,)).fetchone()
    return {"rows": rows, "met": met, "not_met": not_met, "pending": len(rows) - met - not_met,
            "evidence_n": sum(r["evidence"] is not None for r in rows), "other": other,
            "has_answers": any(r["answered"] for r in rows), "ko_total": qual["ko_total"] if qual else 0,
            "qual_status": qual["qual_status"] if qual else "pending"}


def card_extras(conn: sqlite3.Connection, vendor_id: int) -> dict:
    """Main reply file, draft ownership and sent date, failure flag for one vendor (current responses only)."""
    rs = conn.execute("SELECT * FROM v_response_summary WHERE vendor_id=? ORDER BY response_id", (vendor_id,)).fetchall()
    main = next((r for r in rs if r["lines_quoted"] + r["lines_not_quoted"] + r["lines_unmatched"] > 0), None)
    f = None
    if main is not None:
        f = conn.execute("SELECT file_path FROM vendor_responses WHERE response_id=?", (main["response_id"],)).fetchone()
    draft = conn.execute("""SELECT response_id, clarification_draft, clarification_status FROM vendor_responses
                            WHERE vendor_id=? AND clarification_draft IS NOT NULL ORDER BY response_id DESC LIMIT 1""", (vendor_id,)).fetchone()
    sent_at = None
    if draft is not None and draft["clarification_status"] == "sent_stub":
        sent_at = conn.execute("SELECT MAX(ts) FROM audit_log WHERE action='clarification_sent_stub' AND entity_type='response' AND entity_id=?",
                               (draft["response_id"],)).fetchone()[0]
    return {"main_file": main["file_name"] if main else None, "main_path": f["file_path"] if f else None,
            "main_role": main["content_role"] if main else None, "main_response_id": main["response_id"] if main else None,
            "failed": any(r["extraction_status"] == "failed" for r in rs), "draft_response_id": draft["response_id"] if draft else None,
            "clarification_sent_at": sent_at}


def send_clarification(conn: sqlite3.Connection, response_id: int, final_text: str, actor: str = "buyer") -> None:
    """Stub send: store the final text, mark it sent and write it to the audit log (no email leaves the system)."""
    from core import audit

    conn.execute("UPDATE vendor_responses SET clarification_draft=?, clarification_status='sent_stub' WHERE response_id=?", (final_text, response_id))
    audit.log(conn, "clarification_sent_stub", ("response", response_id), field="clarification_draft", new=final_text, actor=actor,
              evidence={"stub": True})
    conn.commit()
