"""MANUAL live check (not part of pytest): asks the six acceptance questions of the real analyst (real Gemini calls)
and compares the numbers in each answer with ground truth computed here, straight from the database.

    python -m tests.live_analyst_check            # writes docs/ANALYST_LIVE_CHECK.md

Nothing in app code knows these questions; the model answers them through the generic tools.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pandas as pd

from agent import tools as T
from agent.analyst import ask
from core.db import connect
from core.queries import vendor_totals

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = [
    "What if we split it, cheapest per line, but only among vendors who cleared the quality questionnaire?",
    "Which values are still unconfirmed, and could any of them change the winner?",
    "Why didn't V5 win line 1? It's the cheapest.",
    "Show savings vs last year by vendor as a chart.",
    "What if we ordered 20,000 per release instead? Does anything change?",
    "Exclude V3. What's the total and who's the backup per line?",
]


def forms(v: float) -> list[str]:
    """Ways a rupee figure might be written: plain, Western commas, Indian commas, lakh and crore."""
    out = {f"{v:,.0f}", f"{v:.0f}", f"{v / 1e7:.2f}", f"{v / 1e7:.1f}", f"{v / 1e5:.1f}", f"{v / 1e5:.2f}", f"{v / 1e5:,.0f}"}
    n = f"{int(round(v))}"
    if len(n) > 3:
        head, tail = n[:-3], n[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        out.add(",".join(parts + [tail]))
    return sorted(out)


def has_number(text: str, v: float) -> bool:
    return any(f in text for f in forms(v))


def truth(conn) -> dict:
    base = T.award_scenario(conn)
    ex = T.award_scenario(conn, exclude_vendors=["V3"])
    qty = T.award_scenario(conn, order_qty_override=20000)
    q = pd.read_sql_query("SELECT * FROM v_review_queue WHERE status='must_confirm'", conn)
    g = pd.read_sql_query("SELECT * FROM v_comparison_grid WHERE line_no=1 AND vendor_code='V5'", conn).iloc[0]
    return {"base": base, "ex": ex, "qty": qty, "queue": q, "v5l1": g, "totals": vendor_totals(conn)}


def main() -> None:
    conn = connect()
    gt = truth(conn)
    base_t, ex_t, qty_t = (gt[k].attrs["totals"] for k in ("base", "ex", "qty"))
    checks = [
        [("scenario total", base_t["total_value_inr"]), ("V1 value", base_t["by_vendor"]["V1"]), ("V3 value", base_t["by_vendor"]["V3"])],
        [("must-confirm values that could change the winner", float(((gt["queue"].could_change_winner == 1)).sum()))],
        [("V5 line-1 landed price", float(gt["v5l1"]["landed_inr_pc"]))],
        [],
        [("total at 20,000/release", qty_t["total_value_inr"]), ("base total", base_t["total_value_inr"])],
        [("total excluding V3", ex_t["total_value_inr"])],
    ]
    words = [["V1", "V3"], ["V4"], ["disqualified", "ISO"], ["V4", "V5"], ["V1", "4%"], ["backup", "V1"]]
    lines = ["# Analyst live check", "", f"Run against `rfx.db` with the real analyst on the configured Gemini model. Ground truth is computed in "
             "`tests/live_analyst_check.py` directly from the database, independent of the model.", ""]
    for i, q in enumerate(QUESTIONS):
        t0 = time.time()
        r = ask(conn, q)
        dt = time.time() - t0
        ok_nums = [(n, has_number(r.text, v)) for n, v in checks[i]]
        ok_words = [(w, w.lower() in r.text.lower()) for w in words[i]]
        lines += [f"## {i + 1}. {q}", "", f"*{dt:.0f}s, model `{r.model}`, {r.data_calls} analysis tool calls, {len(r.tables)} tables, {len(r.figures)} charts.*", "",
                  "**Tool calls:** " + "; ".join(f"`{t['tool']}`" + ("" if t["ok"] else " (error)") for t in r.trace), "", "**Answer**", "", r.text, "",
                  "**Checks against the database**", ""]
        lines += [f"- {'PASS' if ok else 'FAIL'}: answer contains {n} = ₹{v:,.0f}" for (n, ok), (_, v) in zip(ok_nums, checks[i])]
        lines += [f"- {'PASS' if ok else 'CHECK'}: mentions “{w}”" for w, ok in ok_words]
        if i == 3:
            lines.append(f"- {'PASS' if r.figures else 'FAIL'}: produced a chart ({len(r.figures)})")
        lines += ["", "---", ""]
        print(f"Q{i + 1} done in {dt:.0f}s", flush=True)
    out = ROOT / "docs" / "ANALYST_LIVE_CHECK.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    sys.exit(main())
