"""Create-RFQ co-pilot.

The buyer talks to Gemini to shape the RFQ. The model does NOT write the draft: it returns a short reply plus a list
of edit operations from a fixed vocabulary, and apply_ops() validates and applies them in Python. So it can only
change what the buyer's own tables can change, and nothing is ever invented or silently saved.

Saving (save_draft) and sending (send_rfq, a stub) are separate, explicit, audited steps.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field

from core import audit
from core.config import load_config
from core.recompute import recompute_all
from core.rules import evaluate_all

TERM_KEYS = {
    "payment_days": "std_payment_terms_days", "delivery_location": "delivery_location", "gst_pct": "expected_gst_pct",
    "response_due_date": "response_due_date", "award_reference_date": "award_reference_date",
}
LINE_FIELDS = {"description": str, "annual_qty": int, "typical_order_qty": int, "print_colours": int}
LINE_COLS = ["include", "line_no", "catalog_sku", "description", "length_mm", "width_mm", "height_mm", "ply", "annual_qty", "typical_order_qty", "print_colours"]
Q_COLS = ["include", "q_code", "question_text", "is_knockout", "answer_type"]


# ------------------------------------------------------------------ draft state
@dataclass
class Draft:
    terms: dict[str, Any]
    lines: pd.DataFrame
    questions: pd.DataFrame
    history: list = field(default_factory=list)

    def copy(self) -> "Draft":
        return Draft(dict(self.terms), self.lines.copy(), self.questions.copy(), list(self.history))


def load_draft(conn: sqlite3.Connection) -> Draft:
    """The draft as currently saved in the database (for the demo, the confirmed seed RFQ)."""
    r = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    terms = {"payment_days": r["std_payment_terms_days"], "delivery_location": r["delivery_location"], "gst_pct": r["expected_gst_pct"],
             "response_due_date": r["response_due_date"], "award_reference_date": r["award_reference_date"]}
    lines = pd.read_sql_query("SELECT line_no, catalog_sku, description, length_mm, width_mm, height_mm, ply, annual_qty, typical_order_qty, print_colours "
                              "FROM line_items ORDER BY line_no", conn)
    lines.insert(0, "include", True)
    qs = pd.read_sql_query("SELECT q_code, question_text, is_knockout, answer_type FROM rfx_questions ORDER BY question_id", conn)
    qs["is_knockout"] = qs["is_knockout"].astype(bool)
    qs.insert(0, "include", True)
    return Draft(terms, lines[LINE_COLS], qs[Q_COLS])


def diff(draft: Draft, saved: Draft) -> list[str]:
    """Human-readable differences between a draft and what is saved (empty = nothing to confirm)."""
    out = []
    for k, v in draft.terms.items():
        if str(v) != str(saved.terms.get(k)):
            out.append(f"{k}: {saved.terms.get(k)} -> {v}")
    a, b = draft.lines.set_index("line_no"), saved.lines.set_index("line_no")
    for ln in a.index:
        for c in ("include", "description", "annual_qty", "typical_order_qty", "print_colours"):
            if str(a.loc[ln, c]) != str(b.loc[ln, c]):
                out.append(f"line {ln} {c}: {b.loc[ln, c]} -> {a.loc[ln, c]}")
    qa, qb = draft.questions.set_index("q_code"), saved.questions.set_index("q_code")
    for q in qa.index:
        for c in ("include", "question_text", "is_knockout"):
            if str(qa.loc[q, c]) != str(qb.loc[q, c]):
                out.append(f"{q} {c}: {qb.loc[q, c]} -> {qa.loc[q, c]}")
    return out


def validate_draft(d: Draft) -> list[str]:
    """Sanity warnings shown to the buyer (never blocking)."""
    w = []
    t = d.terms
    if not 0 <= int(t["payment_days"]) <= 120:
        w.append(f"Payment terms of {t['payment_days']} days look unusual.")
    if float(t["gst_pct"]) != 18:
        w.append(f"GST is {t['gst_pct']}%; corrugated boxes are currently taxed at 18%.")
    try:
        if date.fromisoformat(str(t["award_reference_date"])) < date.fromisoformat(str(t["response_due_date"])):
            w.append("The award reference date is before the response due date.")
    except ValueError:
        w.append("A date is not in YYYY-MM-DD form.")
    inc = d.lines[d.lines["include"]]
    if (inc["typical_order_qty"] > inc["annual_qty"]).any():
        w.append("Some lines have a typical order quantity larger than the annual quantity.")
    if not inc.shape[0]:
        w.append("No lines are included.")
    if not d.questions[d.questions["include"] & d.questions["is_knockout"]].shape[0]:
        w.append("No knock-out question is included, so every vendor would qualify automatically.")
    return w


# ------------------------------------------------------------------ edit operations (what the model may ask for)
class Op(BaseModel):
    op: Literal["set_term", "set_line", "drop_line", "restore_line", "set_question_text", "set_question_knockout", "drop_question", "restore_question"]
    target: str = Field(description="For set_term: payment_days | delivery_location | gst_pct | response_due_date | award_reference_date. "
                                    "For line ops: the line number. For question ops: the q_code, e.g. Q05_FIRST_SUPPLY.")
    field: Optional[str] = Field(default=None, description="For set_line: description | annual_qty | typical_order_qty | print_colours")
    value: Optional[str] = Field(default=None, description="The new value, as text. Dates as YYYY-MM-DD. For set_question_knockout: true or false.")


class CopilotReply(BaseModel):
    reply: str = Field(description="A short message to the buyer: what you changed, or the one question you need answered. Never claim a change you did not put in ops.")
    ops: list[Op] = Field(default_factory=list)


def apply_ops(draft: Draft, ops: list[Op]) -> tuple[Draft, list[str]]:
    """Validate and apply operations; returns (new draft, one note per operation saying applied or refused and why)."""
    d = draft.copy()
    notes: list[str] = []

    def line_idx(target: str) -> int | None:
        m = re.search(r"\d+", target or "")
        if not m:
            return None
        hit = d.lines.index[d.lines["line_no"] == int(m.group())]
        return int(hit[0]) if len(hit) else None

    def q_idx(target: str) -> int | None:
        hit = d.questions.index[d.questions["q_code"].str.lower() == (target or "").strip().lower()]
        if not len(hit):  # allow a prefix such as "Q05"
            hit = d.questions.index[d.questions["q_code"].str.lower().str.startswith((target or "").strip().lower() + "_")]
        return int(hit[0]) if len(hit) else None

    for op in ops:
        try:
            if op.op == "set_term":
                if op.target not in TERM_KEYS or op.value is None:
                    raise ValueError(f"unknown term '{op.target}'")
                v: Any = op.value.strip()
                if op.target == "payment_days":
                    v = int(float(v))
                    if not 0 <= v <= 365:
                        raise ValueError("payment days must be between 0 and 365")
                elif op.target == "gst_pct":
                    v = float(v)
                    if not 0 <= v <= 28:
                        raise ValueError("GST must be between 0 and 28")
                elif op.target.endswith("_date"):
                    date.fromisoformat(v)
                d.terms[op.target] = v
                notes.append(f"Applied: {op.target} = {v}")
            elif op.op in ("drop_line", "restore_line"):
                i = line_idx(op.target)
                if i is None:
                    raise ValueError(f"no line '{op.target}'")
                d.lines.loc[i, "include"] = op.op == "restore_line"
                notes.append(f"Applied: line {int(d.lines.loc[i, 'line_no'])} ({d.lines.loc[i, 'description']}) " + ("restored" if op.op == "restore_line" else "dropped"))
            elif op.op == "set_line":
                i = line_idx(op.target)
                if i is None or op.field not in LINE_FIELDS or op.value is None:
                    raise ValueError(f"cannot set '{op.field}' on '{op.target}'")
                val = LINE_FIELDS[op.field](float(op.value)) if LINE_FIELDS[op.field] is int else op.value.strip()
                if LINE_FIELDS[op.field] is int and val < 0:
                    raise ValueError("quantities cannot be negative")
                d.lines.loc[i, op.field] = val
                notes.append(f"Applied: line {int(d.lines.loc[i, 'line_no'])} {op.field} = {val}")
            elif op.op in ("drop_question", "restore_question", "set_question_text", "set_question_knockout"):
                i = q_idx(op.target)
                if i is None:
                    raise ValueError(f"no question '{op.target}'")
                if op.op in ("drop_question", "restore_question"):
                    d.questions.loc[i, "include"] = op.op == "restore_question"
                elif op.op == "set_question_text":
                    if not (op.value or "").strip():
                        raise ValueError("empty question text")
                    d.questions.loc[i, "question_text"] = op.value.strip()
                else:
                    d.questions.loc[i, "is_knockout"] = str(op.value).strip().lower() in ("true", "yes", "1")
                notes.append(f"Applied: {d.questions.loc[i, 'q_code']} {op.op.replace('_', ' ')}" + (f" = {op.value}" if op.value else ""))
        except (ValueError, TypeError) as e:
            notes.append(f"Not applied ({op.op} {op.target}): {e}")
    return d, notes


SYSTEM = """You are the RFQ drafting assistant for a category buyer at Sahyadri Consumer Products Ltd (corrugated boxes, plant at Chakan MIDC, Pune).
The buyer edits an RFQ made of: terms (payment days, delivery location, GST %, response due date, award reference date), 30 box lines and 8 vendor questions.
You do not edit anything yourself. You answer with a short reply and a list of edit operations (ops); the app validates and applies them.

RULES
- Use ONLY the line numbers and question codes in the current draft below. Never invent a line, a question, a price or a quantity the buyer did not give.
- "Drop the promo box" means drop_line for the line whose description matches. If several lines could match, do not guess: ask which one in `reply` and return no ops.
- Quantities and dates must be exactly what the buyer said. If they give a relative date ("a week later"), compute it from the dates in the draft and state it in the reply.
- One op per change. Your reply must say exactly what the ops do and nothing more; if the request cannot be done with the available ops, say so plainly. Adding a new line, removing a vendor, and changing box dimensions, ply or SKU are NOT supported in this version (the 30 lines come from the catalogue); do not suggest the buyer can do them in the table. Quantities, descriptions, colours, terms and questions can be changed.
- Be brief and plain. Do not discuss vendors' prices.
"""


def draft_summary(d: Draft) -> str:
    inc = d.lines
    lines = "\n".join(f"{int(r.line_no)}. {r.description} | {r.length_mm}x{r.width_mm}x{r.height_mm} {int(r.ply)}-ply | annual {int(r.annual_qty)} | typical order {int(r.typical_order_qty)} | colours {int(r.print_colours)}"
                      + ("" if r.include else " | DROPPED") for r in inc.itertuples())
    qs = "\n".join(f"{r.q_code} | {r.question_text} | knock-out={bool(r.is_knockout)}" + ("" if r.include else " | DROPPED") for r in d.questions.itertuples())
    return f"TERMS: {json.dumps(d.terms)}\nLINES:\n{lines}\nQUESTIONS:\n{qs}"


def ask_copilot(d: Draft, message: str, history: list[tuple[str, str]] | None = None) -> tuple[CopilotReply, Draft, list[str]]:
    """One chat turn: returns the model's reply, the updated draft and a note per op."""
    from extract.gemini_client import generate_structured

    convo = "\n".join(f"{'Buyer' if r == 'user' else 'Assistant'}: {t}" for r, t in (history or [])[-6:])
    prompt = f"CURRENT DRAFT\n{draft_summary(d)}\n\nCONVERSATION SO FAR\n{convo or '(none)'}\n\nBUYER SAYS: {message}"
    parsed, _raw, _usage = generate_structured([prompt], CopilotReply, system=SYSTEM)
    new, notes = apply_ops(d, parsed.ops)
    return parsed, new, notes


# ------------------------------------------------------------------ saving and sending
@dataclass
class SaveResult:
    changes: list[str]
    blocked: list[str]
    recomputed: bool


def sent_info(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute("""SELECT a.ts, a.evidence_json FROM audit_log a WHERE a.action='rfq_send' ORDER BY a.audit_id DESC LIMIT 1""").fetchone()
    return None if r is None else {"ts": r["ts"], "vendors": json.loads(r["evidence_json"] or "{}").get("vendors", [])}


def replies_exist(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(DISTINCT vendor_id) FROM quotes WHERE is_current=1").fetchone()[0]


def save_draft(conn: sqlite3.Connection, d: Draft, actor: str = "buyer") -> SaveResult:
    """Confirm RFQ: write the draft to rfqs, line_items (buyer_confirmed=1) and rfx_questions.

    A line or question that vendors have already quoted/answered cannot be deleted without orphaning their data,
    so it is kept and reported as blocked. If replies exist, everything is recomputed (payment days, order quantities
    and GST feed the evaluation) and the questionnaire rules are re-run.
    """
    saved = load_draft(conn)
    changes = diff(d, saved)
    blocked: list[str] = []
    rfq = conn.execute("SELECT rfq_id FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    conn.execute("""UPDATE rfqs SET std_payment_terms_days=?, delivery_location=?, expected_gst_pct=?, response_due_date=?, award_reference_date=?
                    WHERE rfq_id=?""", (int(d.terms["payment_days"]), d.terms["delivery_location"], float(d.terms["gst_pct"]),
                                        d.terms["response_due_date"], d.terms["award_reference_date"], rfq["rfq_id"]))
    for r in d.lines.itertuples():
        if r.include:
            conn.execute("UPDATE line_items SET description=?, annual_qty=?, typical_order_qty=?, print_colours=?, buyer_confirmed=1 WHERE line_no=?",
                         (r.description, int(r.annual_qty), int(r.typical_order_qty), int(r.print_colours), int(r.line_no)))
        else:
            n = conn.execute("SELECT COUNT(*) FROM quotes q JOIN line_items l USING (line_id) WHERE l.line_no=?", (int(r.line_no),)).fetchone()[0]
            if n:
                blocked.append(f"line {int(r.line_no)} ({r.description}) cannot be removed: {n} vendor quote rows already reference it. It was kept.")
            else:
                conn.execute("DELETE FROM line_items WHERE line_no=?", (int(r.line_no),))
    for r in d.questions.itertuples():
        if r.include:
            conn.execute("UPDATE rfx_questions SET question_text=?, is_knockout=? WHERE q_code=?", (r.question_text, int(r.is_knockout), r.q_code))
        else:
            n = conn.execute("""SELECT COUNT(*) FROM questionnaire_answers a JOIN rfx_questions q USING (question_id) WHERE q.q_code=?""", (r.q_code,)).fetchone()[0]
            if n:
                blocked.append(f"{r.q_code} cannot be removed: {n} vendor answers already reference it. It was kept.")
            else:
                conn.execute("DELETE FROM rfx_questions WHERE q_code=?", (r.q_code,))
    audit.log(conn, "rfq_confirm", ("rfq", rfq["rfq_id"]), field="draft", old=None, new=f"{len(changes)} changes", actor=actor,
              evidence={"changes": changes, "blocked": blocked})
    conn.commit()
    recomputed = False
    if replies_exist(conn) and changes:
        evaluate_all(conn)
        recompute_all(conn)
        recomputed = True
    return SaveResult(changes, blocked, recomputed)


def build_rfq_body(conn: sqlite3.Connection) -> str:
    """The (stubbed) RFQ email, generated from what is saved in the database."""
    r = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    lines = conn.execute("SELECT * FROM line_items ORDER BY line_no").fetchall()
    qs = conn.execute("SELECT * FROM rfx_questions ORDER BY question_id").fetchall()
    out = [f"Subject: RFQ - {r['title']}", "", "Dear Sir/Madam,", "",
           f"Sahyadri Consumer Products Ltd invites your best offer for the items below ({len(lines)} lines).", "",
           "COMMERCIAL TERMS",
           f"- Delivery: {r['delivery_location']}. Please quote rates FOR (freight included) or state freight separately per box.",
           f"- Prices: INR per piece, exclusive of GST (expected GST rate {r['expected_gst_pct']:g}%).",
           f"- Standard payment terms: {r['std_payment_terms_days']} days from invoice. State any different terms.",
           f"- Please reply by {r['response_due_date']}. Offers will be evaluated as of {r['award_reference_date']}.", "", "ITEMS", ]
    out += [f"{l['line_no']:>2}. {l['description']} | {l['length_mm']}x{l['width_mm']}x{l['height_mm']} mm (inside) | {l['ply']}-ply | "
            f"annual {l['annual_qty']:,} pcs | typical order {l['typical_order_qty']:,} pcs" for l in lines]
    out += ["", "QUALITY QUESTIONNAIRE (please answer each and attach the documents asked for)"]
    out += [f"{q['q_code']}: {q['question_text']}" + ("  [knock-out]" if q["is_knockout"] else "") for q in qs]
    out += ["", "Regards,", "Category Buyer, Sahyadri Consumer Products Ltd, Chakan MIDC, Pune"]
    return "\n".join(out)


def send_rfq(conn: sqlite3.Connection, actor: str = "buyer") -> dict:
    """Stub: stores the RFQ body, marks it issued and logs who it 'went' to. No email is sent."""
    body = build_rfq_body(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rfq = conn.execute("SELECT rfq_id FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    vendors = [{"code": v["vendor_code"], "email": v["contact_email"]} for v in conn.execute("SELECT * FROM vendors ORDER BY vendor_id")]
    conn.execute("UPDATE rfqs SET rfq_body=?, status=CASE WHEN status IN ('awarded','evaluating') THEN status ELSE 'issued' END, issued_at=? WHERE rfq_id=?",
                 (body, now, rfq["rfq_id"]))
    audit.log(conn, "rfq_send", ("rfq", rfq["rfq_id"]), new="sent (stub)", actor=actor, evidence={"vendors": vendors, "stub": True})
    conn.commit()
    return {"sent_at": now, "vendors": vendors, "body": body}
