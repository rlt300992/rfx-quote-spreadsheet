"""Co-pilot for the NEW DRAFT RFQ. Same design as copilot.copilot: the model proposes edit operations from a fixed vocabulary,
Python validates and applies them, and the reply is followed by what was actually applied or refused. The model never writes the draft."""
from __future__ import annotations

import copy
import json
import sqlite3
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

from core import rfq_draft as R

OPS = Literal["set_overview", "add_skus", "add_last_year", "remove_line", "set_line", "load_questionnaire_template",
              "set_knockout", "set_doc_required", "set_term", "select_suppliers", "remove_supplier"]


class DraftOp(BaseModel):
    op: OPS
    target: Optional[str] = Field(default=None, description="set_overview: title|category|contract_type|period|delivery|currency. set_term: "
                                  "payment_days|delivery_basis|gst_pct|response_due|award_ref|price_validity. remove_line/set_line: the SKU. "
                                  "set_knockout/set_doc_required: the q_code. remove_supplier: the vendor code.")
    field: Optional[str] = Field(default=None, description="set_line only: annual_qty | typical_order_qty")
    value: Optional[str] = Field(default=None, description="The new value as text. Dates YYYY-MM-DD. Booleans true/false.")
    values: list[str] = Field(default_factory=list, description="add_skus: the SKU codes exactly as the buyer wrote them. select_suppliers: vendor codes, or ['all'].")


class DraftReply(BaseModel):
    reply: str = Field(description="One to three short sentences: what you are changing, or the one question you need answered. Never claim a change that is not in ops.")
    ops: list[DraftOp] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list, description="2-3 short next steps the buyer could click, each under 50 characters, phrased as an instruction to you.")


SYSTEM = """You are the RFQ drafting co-pilot for a category buyer at Sahyadri Consumer Products Ltd (corrugated boxes, plant at Chakan MIDC, Pune).
The buyer builds a NEW RFQ draft in six sections: overview, line items, questionnaire, terms & timeline, suppliers, review. You do not edit anything yourself:
you answer with a short reply and edit operations (ops) from a fixed list; the app validates and applies them and tells the buyer what was applied or refused.

RULES
- Lines come ONLY from the item catalog below. To add boxes, use add_skus with the codes the buyer gave (even if a code looks wrong: the app checks it and reports unknown codes), add_last_year for "same as last year", or, when the buyer DESCRIBES a box, name the best catalog matches in `reply` and ask whether to add them (do not add them yet).
- The questionnaire comes from the standard template (load_questionnaire_template); knock-out and document flags are set per q_code with set_knockout / set_doc_required.
- Suppliers come ONLY from the approved vendor list below. "All approved vendors" = select_suppliers with ['all'].
- Never invent a SKU, a price, a quantity, a vendor, a date or a term the buyer did not give. Overview text may use what the buyer said (e.g. "annual rate contract for corrugated boxes, Chakan plant, FY27, INR" -> title, category, contract_type, period, delivery, currency).
- Relative dates: compute from today's date and state the result in the reply.
- If the request cannot be done with the available ops, say so plainly in the reply; do not pretend.
- Be brief and plain."""


def _catalog_text(conn: sqlite3.Connection) -> str:
    return "\n".join(f"{c['sku']} | {c['description']} | {c['length_mm']}x{c['width_mm']}x{c['height_mm']} | {c['ply']}-ply | BS {c['bs']} | annual {c['annual_qty']}"
                     for c in R.catalog(conn).values())


def _vendors_text(conn: sqlite3.Connection) -> str:
    return "\n".join(f"{s['code']} | {s['name']} | {s['location']} | {s['msme']}" for s in R.approved_suppliers(conn))


def _draft_text(d: dict) -> str:
    return json.dumps({"overview": d["overview"], "terms": d["terms"], "lines": [ln["sku"] for ln in d["lines"]], "unknown_skus": [u["code"] for u in d["unknown"]],
                       "questions": [{"q": q["q_code"], "knock_out": q["is_knockout"], "doc": q["doc_required"]} for q in d["questions"]],
                       "suppliers": d["suppliers"]}, ensure_ascii=False)


def apply_ops(conn: sqlite3.Connection, draft: dict, ops: list[DraftOp]) -> tuple[dict, list[str], list[str]]:
    """Returns (new draft, applied notes, refused notes). Python decides what is allowed."""
    d = copy.deepcopy(draft)
    ok: list[str] = []
    no: list[str] = []
    sup = {s["code"] for s in R.approved_suppliers(conn)}

    def mark(section: str, key: str | None = None) -> None:
        d["active"] = section
        if key:
            d["ai_fields"] = sorted(set(d["ai_fields"]) | {key})

    for op in ops:
        try:
            if op.op == "set_overview":
                if op.target not in R.OVERVIEW_FIELDS or not (op.value or "").strip():
                    raise ValueError(f"unknown overview field '{op.target}'")
                d["overview"][op.target] = op.value.strip()
                mark("overview", f"overview.{op.target}")
                ok.append(f"Overview: {R.OVERVIEW_FIELDS[op.target]} = {op.value.strip()}")
            elif op.op == "add_skus":
                notes = R.add_skus(conn, d, op.values or R.split_codes(op.value or ""), source="ai")
                mark("lines")
                for n in notes:
                    (ok if n.startswith("Added") else no).append(n)
            elif op.op == "add_last_year":
                notes = R.add_last_year(conn, d)
                mark("lines")
                ok.extend(notes)
            elif op.op in ("remove_line", "set_line"):
                hit = [ln for ln in d["lines"] if ln["sku"].upper() == (op.target or "").strip().upper()]
                if not hit:
                    raise ValueError(f"{op.target} is not in the lines")
                if op.op == "remove_line":
                    d["lines"].remove(hit[0])
                    ok.append(f"Removed {hit[0]['sku']}")
                else:
                    if op.field not in ("annual_qty", "typical_order_qty"):
                        raise ValueError(f"cannot set '{op.field}'")
                    v = int(float(op.value))
                    if v < 0:
                        raise ValueError("quantities cannot be negative")
                    hit[0][op.field] = v
                    ok.append(f"{hit[0]['sku']}: {op.field.replace('_', ' ')} = {v:,}")
                mark("lines")
            elif op.op == "load_questionnaire_template":
                ok.extend(R.load_template(conn, d))
                mark("questions")
            elif op.op in ("set_knockout", "set_doc_required"):
                hit = [q for q in d["questions"] if q["q_code"].lower() == (op.target or "").strip().lower()
                       or q["q_code"].lower().startswith((op.target or "").strip().lower() + "_")]
                if not hit:
                    raise ValueError(f"no question '{op.target}' in the questionnaire (load the template first?)")
                val = str(op.value).strip().lower() in ("true", "yes", "1")
                hit[0]["is_knockout" if op.op == "set_knockout" else "doc_required"] = val
                ok.append(f"{hit[0]['q_code']}: {'knock-out' if op.op == 'set_knockout' else 'document required'} = {'on' if val else 'off'}")
                mark("questions")
            elif op.op == "set_term":
                if op.target not in R.TERM_FIELDS or op.value is None:
                    raise ValueError(f"unknown term '{op.target}'")
                v: object = op.value.strip()
                if op.target == "payment_days":
                    v = int(float(v))
                    if not 0 <= v <= 365:
                        raise ValueError("payment days must be 0-365")
                elif op.target == "gst_pct":
                    v = float(str(v).rstrip("%"))
                    if not 0 <= v <= 28:
                        raise ValueError("GST must be 0-28")
                elif op.target in ("response_due", "award_ref"):
                    date.fromisoformat(v)
                d["terms"][op.target] = v
                mark("terms", f"terms.{op.target}")
                ok.append(f"Terms: {R.TERM_FIELDS[op.target]} = {v}")
            elif op.op == "select_suppliers":
                codes = sorted(sup) if [x.lower() for x in op.values] == ["all"] else [c.upper() for c in op.values]
                bad = [c for c in codes if c not in sup]
                for c in bad:
                    no.append(f"{c} is not on the approved vendor list, so I did not add it.")
                d["suppliers"] = sorted(set(d["suppliers"]) | (set(codes) - set(bad)))
                mark("suppliers")
                ok.append(f"Suppliers: {', '.join(d['suppliers'])}")
            elif op.op == "remove_supplier":
                c = (op.target or "").upper()
                if c not in d["suppliers"]:
                    raise ValueError(f"{c} is not selected")
                d["suppliers"].remove(c)
                mark("suppliers")
                ok.append(f"Removed supplier {c}")
        except (ValueError, TypeError) as e:
            no.append(f"Could not {op.op.replace('_', ' ')}{' ' + op.target if op.target else ''}: {e}")
    return d, ok, no


def next_steps(d: dict) -> list[str]:
    """Deterministic fallback suggestions, derived from what is still missing."""
    out = []
    if not any(d["overview"].values()):
        return ["Describe what you're sourcing", "Paste SKU codes", "Copy last year's corrugated RFQ"]
    if not all(d["overview"].values()):
        out.append("Fill in the overview")
    if not d["lines"]:
        out += ["Add last year's lines", "Paste SKU codes"]
    if not d["questions"]:
        out.append("Use the standard questionnaire")
    if not d["suppliers"]:
        out.append("Invite all approved vendors")
    if any(v in (None, "") for v in d["terms"].values()):
        out.append("Set payment 60 days, GST extra")
    return (out or ["Review & publish"])[:3]


def ask(conn: sqlite3.Connection, draft: dict, message: str, history: list[tuple[str, str]] | None = None) -> tuple[str, dict, list[str], list[str], list[str]]:
    """One chat turn (the only place the draft co-pilot calls Gemini). Returns (reply, new draft, applied, refused, suggestions)."""
    from extract.gemini_client import generate_structured

    convo = "\n".join(f"{'Buyer' if r == 'user' else 'Assistant'}: {t}" for r, t in (history or [])[-6:])
    prompt = (f"TODAY: {date.today().isoformat()}\n\nITEM CATALOG\n{_catalog_text(conn)}\n\nAPPROVED VENDORS\n{_vendors_text(conn)}\n\n"
              f"CURRENT DRAFT\n{_draft_text(draft)}\n\nCONVERSATION SO FAR\n{convo or '(none)'}\n\nBUYER SAYS: {message}")
    parsed, _raw, _usage = generate_structured([prompt], DraftReply, system=SYSTEM)
    new, ok, no = apply_ops(conn, draft, parsed.ops)
    sugg = [s.strip() for s in parsed.suggestions if 3 <= len(s.strip()) <= 60][:3] or next_steps(new)
    return parsed.reply, new, ok, no, sugg


# ------------------------------------------------------------------ questions about the published demo RFQ (read only)
class DemoAnswer(BaseModel):
    answer: str = Field(description="A short, plain answer using only the RFQ text given. If the RFQ does not say, say so.")


def answer_about_demo(conn: sqlite3.Connection, question: str) -> str:
    """Chat about the published demo RFQ. Grounded in the RFQ text and vendor list; changes nothing."""
    from copilot.copilot import build_rfq_body
    from extract.gemini_client import generate_structured

    prompt = (f"THE PUBLISHED RFQ (read only)\n{build_rfq_body(conn)}\n\nINVITED VENDORS\n{_vendors_text(conn)}\n\nBUYER ASKS: {question}")
    parsed, _raw, _usage = generate_structured([prompt], DemoAnswer, system=(
        "You answer a category buyer's questions about one published RFQ. Use ONLY the RFQ text and vendor list given. Be brief. "
        "You cannot change the RFQ; if asked to, say it is published and read-only and suggest Edit RFQ or Start new draft."))
    return parsed.answer


def demo_facts(conn: sqlite3.Connection, which: str) -> str:
    """Deterministic answers for the two suggestion chips (no model call)."""
    d = R.from_demo(conn)
    if which == "invited":
        return "Invited (5 approved vendors):\n\n" + "\n".join(f"- **{s['code']}** {s['name']}, {s['location']} ({s['msme']})" for s in R.approved_suppliers(conn))
    return (f"We asked for ₹/piece, ex-GST, FOR delivery, on **{len(d['lines'])} lines** ({R._plies(d['lines'])}), plus an **{len(d['questions'])}-question** quality "
            f"questionnaire with {sum(q['is_knockout'] for q in d['questions'])} knock-outs ({', '.join(q['q_code'].split('_')[0] for q in d['questions'] if q['is_knockout'])}) "
            f"and documents for {', '.join(q['q_code'].split('_')[0] for q in d['questions'] if q['doc_required'])}.")
