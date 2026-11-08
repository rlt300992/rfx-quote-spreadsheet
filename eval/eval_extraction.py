"""Compare what the app extracted and computed against data/answer_key (the ONLY place besides tests allowed to read it).

    python -m eval.eval_extraction            # reads the app database (RFX_DB or rfx.db), writes eval/report.md
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

from core.config import load_config
from core.db import connect

ROOT = Path(__file__).resolve().parent.parent
KEY = ROOT / "data" / "answer_key"
REPORT = Path(__file__).resolve().parent / "report.md"
UNIT_EQ = {"per_box": "per_piece"}  # "per box" and "per piece" are the same unit for a box buyer


def _unit(u: str | None) -> str | None:
    return UNIT_EQ.get(u, u)


def _flags(row: sqlite3.Row) -> set[str]:
    meta = json.loads(row["field_meta_json"]) if row["field_meta_json"] else {}
    return set(meta.get("flags") or [])


def evaluate(conn: sqlite3.Connection) -> dict:
    key = pd.read_csv(KEY / "quotes_answer_key.csv")
    rows = conn.execute("""SELECT q.*, v.vendor_code, l.line_no FROM quotes q JOIN vendors v USING (vendor_id)
                           JOIN line_items l USING (line_id) WHERE q.is_current=1""").fetchall()
    db = {(r["vendor_code"], r["line_no"]): r for r in rows}
    unmatched = conn.execute("SELECT v.vendor_code, COUNT(*) n FROM quotes q JOIN vendors v USING (vendor_id) "
                             "WHERE q.is_current=1 AND q.quote_status='unmatched' GROUP BY 1").fetchall()
    res: dict = {"vendors": {}, "mismatches": [], "traps": [], "unexpected_flags": defaultdict(Counter),
                 "unmatched": {r["vendor_code"]: r["n"] for r in unmatched}}
    for code, grp in key.groupby("vendor_code"):
        m = Counter()
        for _, k in grp.iterrows():
            ln = int(k.line_no)
            r = db.get((code, ln))
            exp_flags = set(str(k.expected_flags).split(";")) - {"nan", ""} if pd.notna(k.expected_flags) else set()
            status = r["quote_status"] if r else "missing"
            m["lines"] += 1
            m["status_ok"] += status == k.quote_status
            if status != k.quote_status:
                res["mismatches"].append((code, ln, "quote_status", k.quote_status, status))
            if k.quote_status != "quoted":
                m["notquoted_total"] += 1
                m["notquoted_ok"] += status == "not_quoted"
                continue
            if r is None or r["quote_status"] != "quoted":
                continue
            trap_unit = "UNIT_AMBIGUOUS" in exp_flags
            got_flags = _flags(r)
            # price: base INR/pc within 1% of the truth
            m["priced"] += 1
            base = r["base_inr_pc"]
            price_ok = base is not None and abs(base - k.true_base_inr_pc) <= 0.01 * abs(k.true_base_inr_pc)
            m["price_ok"] += price_ok
            if trap_unit:
                trapped = {"UNIT_AMBIGUOUS", "SANITY_RANGE_FAIL"} <= got_flags and "open_must_confirm:price" in json.loads(r["ineligible_reasons_json"] or "[]")
                m["trap_total"] += 1
                m["trap_flagged"] += trapped
                res["traps"].append((code, ln, "unit trap: header says one unit, value is per 100", trapped,
                                     f"extracted {r['raw_price']:g} {r['raw_unit']} -> INR {base}; flags {sorted(got_flags & {'UNIT_AMBIGUOUS', 'SANITY_RANGE_FAIL'})}"))
            if not price_ok and not trap_unit:
                res["mismatches"].append((code, ln, "base_inr_pc", k.true_base_inr_pc, base))
            m["effective_ok"] += price_ok or (trap_unit and trapped)
            m["unit_ok"] += trap_unit or _unit(r["raw_unit"]) == _unit(k.raw_unit)
            if not trap_unit and _unit(r["raw_unit"]) != _unit(k.raw_unit):
                res["mismatches"].append((code, ln, "unit", k.raw_unit, r["raw_unit"]))
            m["cur_ok"] += r["raw_currency"] == k.raw_currency
            if r["raw_currency"] != k.raw_currency:
                res["mismatches"].append((code, ln, "currency", k.raw_currency, r["raw_currency"]))
            m["spec_ok"] += r["spec_match"] == k.spec_match
            if r["spec_match"] != k.spec_match:
                res["mismatches"].append((code, ln, "spec_match", k.spec_match, r["spec_match"]))
            m["tax_ok"] += r["tax_basis"] == k.tax_basis
            m["fb_ok"] += r["freight_basis"] == k.freight_basis
            if r["freight_basis"] != k.freight_basis:
                res["mismatches"].append((code, ln, "freight_basis", k.freight_basis, r["freight_basis"]))
            if k.freight_basis == "extra_quoted":
                m["fv_total"] += 1
                m["fv_ok"] += r["raw_freight_value"] is not None and abs(r["raw_freight_value"] - k.true_freight_inr_pc) <= 0.01
            # flags: every expected flag must be present
            for f in exp_flags - {"NOT_QUOTED"}:
                m["flags_expected"] += 1
                hit = f in got_flags
                m["flags_found"] += hit
                if not hit:
                    res["mismatches"].append((code, ln, "missing flag", f, sorted(got_flags)))
            extra = got_flags - exp_flags - {"CONDITIONAL_DISCOUNT_AVAILABLE"}
            for f in extra:
                res["unexpected_flags"][code][f] += 1
        res["vendors"][code] = m

    res["pipelines"] = {r["vendor_code"]: r["extraction_model"] for r in conn.execute(
        """SELECT v.vendor_code, vr.extraction_model FROM vendor_responses vr JOIN vendors v USING (vendor_id)
           WHERE vr.content_role IN ('quote','mixed') AND EXISTS (SELECT 1 FROM quotes q WHERE q.response_id=vr.response_id AND q.is_current=1)
           ORDER BY v.vendor_id""")}
    qk = pd.read_csv(KEY / "questionnaire_answer_key.csv").set_index("vendor_code")
    qcols = [c for c in qk.columns if c.startswith("Q")]
    qres: dict[str, dict] = {}
    for code in qk.index:
        cells = {}
        for q in qcols:
            r = conn.execute("""SELECT a.rule_result, a.answer_value, a.confidence FROM questionnaire_answers a JOIN vendors v USING (vendor_id)
                                JOIN rfx_questions x USING (question_id) WHERE v.vendor_code=? AND x.q_code=? AND a.is_current=1""", (code, q)).fetchone()
            got = r["rule_result"] if r else "unknown"
            cells[q] = (got, qk.loc[code, q].split()[0], r["answer_value"] if r else None)
        qual = conn.execute("SELECT qual_status FROM v_vendor_qualification WHERE vendor_code=?", (code,)).fetchone()[0]
        qres[code] = {"cells": cells, "qual": qual, "expected_qual": qk.loc[code, "expected_qualification"].split()[0]}
    res["questionnaire"] = qres
    from core.queries import pending_confirmation_count
    from core.review_items import open_item_count

    res["items_open"] = open_item_count(conn)                       # review items (vendor terms + one card per line)
    res["values_open"] = conn.execute("SELECT COUNT(*) FROM v_review_queue").fetchone()[0]
    res["values_could_change"] = pending_confirmation_count(conn)
    return res


def _pct(a: int, b: int) -> str:
    return "n/a" if not b else f"{a}/{b} ({a / b:.0%})"


def render(res: dict, cfg: dict) -> str:
    out = ["# Extraction evaluation", "",
           f"Run {date.today().isoformat()}. Compared with `data/answer_key` (eval only; the app never reads it).", "",
           "Pipeline that produced each vendor's current values (from `vendor_responses.extraction_model`):", ""]
    out += [f"- {code}: `{p}`" for code, p in res["pipelines"].items()]
    out += ["", f"Configured default model `{cfg['gemini']['model']}`; fallbacks used when a model's daily quota is exhausted: "
            f"{', '.join('`%s`' % m for m in cfg['gemini'].get('fallback_models', []))}.", ""]
    tot = Counter()
    for m in res["vendors"].values():
        tot.update(m)
    out += ["## Summary", "",
            f"- **Prices correct (base INR/pc within 1%):** {_pct(tot['price_ok'], tot['priced'])}",
            f"- **Prices correct or the planted unit trap flagged and blocked:** {_pct(tot['effective_ok'], tot['priced'])}",
            f"- **Planted unit traps flagged (V2 'Rate/box' lines):** {_pct(tot['trap_flagged'], tot['trap_total'])}",
            f"- **Expected flags found:** {_pct(tot['flags_found'], tot['flags_expected'])}",
            f"- **Not-quoted lines detected:** {_pct(tot['notquoted_ok'], tot['notquoted_total'])}",
            f"- **Unmatched vendor lines:** {sum(res['unmatched'].values()) or 0}",
            f"- **Vendor qualification correct:** {sum(d['qual'] == d['expected_qual'] for d in res['questionnaire'].values())}/{len(res['questionnaire'])}",
            f"- **Items to review:** {res['items_open']} (from {res['values_open']} open values; {res['values_could_change']} must-confirm values could change a winner)", "",
            "## Per vendor", "",
            "| Vendor | Status | Price | Price or trap | Unit | Currency | Spec | Freight basis | Freight value | Expected flags found |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for code, m in res["vendors"].items():
        q = m["priced"]
        out.append(f"| {code} | {_pct(m['status_ok'], m['lines'])} | {_pct(m['price_ok'], q)} | {_pct(m['effective_ok'], q)} | "
                   f"{_pct(m['unit_ok'], q)} | {_pct(m['cur_ok'], q)} | {_pct(m['spec_ok'], q)} | {_pct(m['fb_ok'], q)} | "
                   f"{_pct(m['fv_ok'], m['fv_total'])} | {_pct(m['flags_found'], m['flags_expected'])} |")
    out += ["", "## Planted traps", "", "| Vendor | Line | Trap | Flagged and blocked | Detail |", "|---|---|---|---|---|"]
    for code, ln, what, ok, detail in res["traps"]:
        out.append(f"| {code} | {ln} | {what} | {'YES' if ok else '**NO**'} | {detail} |")
    out += ["", "## Questionnaire (rule engine result vs answer key) and qualification", "",
            "| Vendor | " + " | ".join(next(iter(res['questionnaire'].values()))["cells"]) + " | Qualification (got / expected) |",
            "|---|" + "---|" * (len(next(iter(res["questionnaire"].values()))["cells"]) + 1)]
    qcorrect = qtotal = 0
    for code, d in res["questionnaire"].items():
        cells = []
        for q, (got, exp, val) in d["cells"].items():
            qtotal += 1
            qcorrect += got == exp
            cells.append(f"{got}" if got == exp else f"**{got}** (want {exp})")
        ok = d["qual"] == d["expected_qual"]
        out.append(f"| {code} | " + " | ".join(cells) + f" | {'OK' if ok else '**WRONG**'} {d['qual']} / {d['expected_qual']} |")
    out += ["", f"Questionnaire cells correct: {_pct(qcorrect, qtotal)}.", ""]
    if any(res["unexpected_flags"].values()):
        out += ["## Flags raised that the key does not list (review: noise or real finds)", ""]
        for code, c in res["unexpected_flags"].items():
            if c:
                out.append(f"- {code}: " + ", ".join(f"{f} x{n}" for f, n in c.most_common()))
        out.append("")
    out += ["## Mismatches", ""]
    if not res["mismatches"]:
        out.append("None.")
    else:
        out += ["| Vendor | Line | Field | Expected | Got |", "|---|---|---|---|---|"]
        out += [f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in res["mismatches"][:80]]
        if len(res["mismatches"]) > 80:
            out.append(f"\n... and {len(res['mismatches']) - 80} more.")
    return "\n".join(out) + "\n"


def main() -> None:
    conn = connect()
    res = evaluate(conn)
    REPORT.write_text(render(res, load_config()), encoding="utf-8")
    tot = Counter()
    for m in res["vendors"].values():
        tot.update(m)
    q = res["questionnaire"]
    print("Eval summary (report written to eval/report.md):")
    print(f"  prices extracted exactly (within 1%): {_pct(tot['price_ok'], tot['priced'])}; with the unit traps flagged and blocked: {_pct(tot['effective_ok'], tot['priced'])}")
    print(f"  planted issues flagged: unit traps {_pct(tot['trap_flagged'], tot['trap_total'])}; expected flags {_pct(tot['flags_found'], tot['flags_expected'])}; not-quoted lines {_pct(tot['notquoted_ok'], tot['notquoted_total'])}")
    print(f"  vendor qualification correct: {sum(d['qual'] == d['expected_qual'] for d in q.values())}/{len(q)}")
    print(f"  items to review: {res['items_open']} (from {res['values_open']} open values)")
    print(f"  mismatches against the key: {len(res['mismatches'])}")


if __name__ == "__main__":
    main()
