"""Data for the Bid Analysis tab: scenarios (via agent.tools.award_scenario), the comparison grid cells, risk chips, the price build-up for one
quote and the qualification summary. Pure functions over v_comparison_grid / v_vendor_qualification / v_documents; no award logic of its own."""
from __future__ import annotations

import json
import re
import sqlite3

import pandas as pd

from agent.tools import award_scenario, scenario_eligibility
from core.queries import comparison_grid
from core.responses_view import doc_facts
from core.stage import fmt_date

CLOSE_PCT = 2.0
ASK_TEXT = "Explain line {n}: who wins, the backup, and what could change it."


def fmt_inr(x: float | None, signed: bool = False) -> str:
    """₹4.90 cr / ₹1.9 L / ₹52,000 (Indian units for money totals)."""
    if x is None or pd.isna(x):
        return "n/a"
    a = abs(x)
    s = f"₹{a / 1e7:,.2f} cr" if a >= 1e7 else f"₹{a / 1e5:,.1f} L" if a >= 1e5 else f"₹{a:,.0f}"
    return ("-" if x < 0 else "+" if signed and x > 0 else "") + s


def short_name(name: str) -> str:
    generic = {"corrugators", "packaging", "industries", "pvt", "ltd", "box", "works", "cartons", "llp"}
    out = []
    for w in name.split():
        if w.lower() in generic or len(out) == 2:
            break
        out.append(w)
    return " ".join(out) or name.split()[0]


# ------------------------------------------------------------------ scenarios
def scenario_defs(g: pd.DataFrame) -> list[dict]:
    """The four scenarios. Vendors to exclude (C) and the substitutes to accept (D) are read from the data, never hardcoded."""
    nf = sorted(g.loc[(g["price_not_firm"] == 1) & (g["quote_status"] == "quoted"), "vendor_code"].unique())
    spec = g[(g["quote_status"] == "quoted") & g["spec_match"].isin(["substitute", "deviation"]) & (g["spec_accepted"] == 0)]
    spec_v = sorted(spec["vendor_code"].unique())
    spec_lines = sorted(int(x) for x in spec["line_no"].unique())
    return [
        {"key": "A", "title": "Cheapest per line · qualified only", "sub": "L1 per line among vendors who cleared the questionnaire.", "params": {}},
        {"key": "B", "title": "Include pending vendors", "sub": "Also lets pending vendors win, as if their open questions resolve in their favour.",
         "params": {"include_pending": True}},
        {"key": "C", "title": f"Exclude {', '.join(nf) or 'not-firm vendors'} (price not firm)", "sub": "Removes kraft-linked prices; only fixed prices can win.",
         "params": {"exclude_vendors": nf}},
        {"key": "D", "title": f"Accept {', '.join(spec_v) or 'vendor'} substitutes",
         "sub": f"What if QA accepts the substitute and deviation (line{'s' if len(spec_lines) != 1 else ''} {', '.join(map(str, spec_lines))})?",
         "params": {"include_accepted_substitutes": True}},
    ]


def run_scenarios(conn: sqlite3.Connection, g: pd.DataFrame | None = None) -> list[dict]:
    g = g if g is not None else comparison_grid(conn)
    out = []
    for d in scenario_defs(g):
        df = award_scenario(conn, basis="evaluated", **d["params"])
        out.append({**d, "df": df, "totals": df.attrs.get("totals", {})})
    return out


def headline(sc: dict, n_lines: int) -> str:
    t = sc["totals"]
    by = t.get("lines_by_vendor", {})
    parts = " · ".join(f"{v} {n} lines" for v, n in sorted(by.items(), key=lambda kv: -kv[1]))
    sav = t.get("saving_vs_ly_inr", 0.0)
    verb = f"saving {fmt_inr(sav)} ({t.get('saving_vs_ly_pct', 0):.1f}%)" if sav >= 0 else f"costing {fmt_inr(-sav)} more ({-t.get('saving_vs_ly_pct', 0):.1f}%)"
    return (f"Award **{t.get('lines_awarded', 0)} of {n_lines} lines** to **{len(by)} vendor{'s' if len(by) != 1 else ''}** ({parts}) for "
            f"**{fmt_inr(t.get('total_value_inr'))}** a year, {verb} vs last year on the same lines.")


def trade_off_note(sc: dict, base: dict, n_lines: int) -> str | None:
    """Amber note when the selected scenario covers fewer lines or saves less than A."""
    if sc["key"] == base["key"]:
        return None
    t, b = sc["totals"], base["totals"]
    bits = []
    if t.get("lines_awarded", 0) < b.get("lines_awarded", 0):
        bits.append(f"leaves {n_lines - t.get('lines_awarded', 0)} of {n_lines} lines without a winner")
    if t.get("saving_vs_ly_inr", 0) < b.get("saving_vs_ly_inr", 0) - 1:
        bits.append(f"saves {fmt_inr(b['saving_vs_ly_inr'] - t['saving_vs_ly_inr'])} less than the base scenario")
    if t.get("total_value_inr", 0) > b.get("total_value_inr", 0) and t.get("lines_awarded") == b.get("lines_awarded"):
        bits.append(f"costs {fmt_inr(t['total_value_inr'] - b['total_value_inr'])} more a year")
    return f"**{sc['title']}** " + " and ".join(bits) + ". Check this trade-off before you use it." if bits else None


# ------------------------------------------------------------------ the grid
def vendor_strip(g: pd.DataFrame, n_lines: int) -> list[dict]:
    out = []
    for code, grp in g.groupby("vendor_code"):
        f = grp.iloc[0]
        quoted = grp[grp["quote_status"] == "quoted"]
        nf = bool((quoted["price_not_firm"] == 1).any())
        eff, raw = quoted["effective_vendor_days"].dropna(), quoted["payment_terms_days"].dropna()
        days = f"{int(raw.iloc[0])}→{int(eff.iloc[0])} days" if len(raw) and len(eff) and int(raw.iloc[0]) != int(eff.iloc[0]) else (f"{int(eff.iloc[0])} days" if len(eff) else "")
        out.append({"code": code, "name": short_name(f["vendor_name"]), "full_name": f["vendor_name"], "qual": f["qual_status"], "lines": len(quoted),
                    "n_lines": n_lines, "days": days, "not_firm": nf, "msme": f["msme_category"]})
    return sorted(out, key=lambda x: x["code"])


def _tag(r) -> str:
    if r["quote_status"] != "quoted":
        return "Not quoted"
    if r["spec_match"] == "substitute" and not r["spec_accepted"]:
        return "Substitute · not accepted"
    if r["spec_match"] == "deviation" and not r["spec_accepted"]:
        return "Spec deviation"
    try:
        fields = json.loads(r["field_meta_json"] or "{}").get("fields", {})
    except ValueError:
        fields = {}
    if any(fields.get(f, {}).get("status") == "must_confirm" for f in ("price", "unit")):   # the price itself is unconfirmed (not every open term)
        return "Unconfirmed"
    return ""


def grid_model(g: pd.DataFrame, params: dict, basis: str = "evaluated", metric: str = "₹/pc") -> dict:
    """Per-line rows for the grid: text per vendor cell, css per cell, the winner, the close-call flag. Not quoted is never a number."""
    col = {"evaluated": "evaluated_inr_pc", "landed": "landed_inr_pc"}[basis]
    ok = scenario_eligibility(g, basis, params.get("exclude_vendors"), True, params.get("include_pending", False), params.get("include_accepted_substitutes", False))
    g = g.assign(_ok=ok.values)
    vendors = sorted(g["vendor_code"].unique())
    rows, texts, csss, meta = [], [], [], []
    for ln, grp in g.groupby("line_no"):
        first = grp.iloc[0]
        elig = grp[grp["_ok"]].sort_values([col, "vendor_code"])
        win = elig.iloc[0] if len(elig) else None
        back = elig.iloc[1] if len(elig) > 1 else None
        hi = elig[col].max() if len(elig) > 1 else None
        text, css = {}, {}
        for v in vendors:
            r = grp[grp["vendor_code"] == v].iloc[0]
            tag = _tag(r)
            if r["quote_status"] != "quoted" or pd.isna(r[col]):
                text[v], css[v] = "Not quoted", "color:#8b949e;font-style:italic"
                continue
            price = r[col]
            if metric == "% above cheapest":
                s = "n/a" if win is None else f"{(price / win[col] - 1) * 100:+.1f}%".replace("+0.0%", "0.0%")
            elif metric == "Annual value":
                s = f"{price * r['annual_qty']:,.0f}"
            else:
                s = f"{price:.2f}"
            if win is not None and r["vendor_code"] == win["vendor_code"]:
                s += " ●" if r["price_not_firm"] else ""
                css[v] = "background-color:rgba(46,160,67,.40);font-weight:700"
            elif not r["_ok"]:
                css[v] = "color:#8b949e"
            elif hi is not None and price == hi:
                css[v] = "color:#e5534b"
            else:
                css[v] = ""
            text[v] = s + (f" · {tag}" if tag else "")
        close = None
        if win is not None and back is not None and (back[col] / win[col] - 1) * 100 < CLOSE_PCT:
            close = back["vendor_code"]
        ly = first["ly_landed_inr_pc"]
        vs_ly = None
        if win is not None and pd.notna(ly) and ly:
            vs_ly = (win["landed_inr_pc"] / ly - 1) * 100
        if win is None:
            wtxt = "No winner"
        else:
            wtxt = f"{win['vendor_code']}" + (f" {'↓' if vs_ly < 0 else '↑'}{abs(vs_ly):.1f}%" if vs_ly is not None else "") + (f" · close: {close}" if close else "")
        at_risk = win is not None and (bool(win["price_not_firm"]) or int(win["freight_imputed"] or 0) > 0 or win["review_state"] in ("must_confirm", "spot_check"))
        rows.append({"#": int(ln), "Line": f"{first['description']} · {int(first['ply'])}-ply", "Annual qty": int(first["annual_qty"]),
                     "Last yr": None if pd.isna(ly) else float(ly), **{v: text[v] for v in vendors}, "Winner · vs last yr": wtxt, "Ask": "💬"})
        csss.append(css)
        meta.append({"line_no": int(ln), "ply": int(first["ply"]), "winner": None if win is None else win["vendor_code"], "winner_price": None if win is None else float(win[col]),
                     "close": close, "at_risk": at_risk})
    return {"df": pd.DataFrame(rows), "css": csss, "meta": meta, "vendors": vendors, "basis": basis}


# ------------------------------------------------------------------ risks
def risk_chips(conn: sqlite3.Connection, g: pd.DataFrame, sc: dict, open_items: list[dict]) -> list[dict]:
    """[{label, target ('review'|'responses'), vendor}] for the chips under the scenarios."""
    t = sc["totals"]
    out = []
    if t.get("lines_on_not_firm_prices"):
        out.append({"icon": "⚠", "label": f"{fmt_inr(t['value_on_not_firm_prices_inr'])} on prices not firm ({', '.join(t['not_firm_vendors'])})", "target": "review",
                    "vendor": t["not_firm_vendors"][0]})
    chg = [i for i in open_items if i["state"] == "open" and i["could_change"]]
    if chg:
        vs = sorted({i["vendor"] for i in chg})
        out.append({"icon": "⚠", "label": f"{len(chg)} unconfirmed item{'s' if len(chg) != 1 else ''} could change the award ({', '.join(vs)})", "target": "review", "vendor": vs[0]})
    q = g[g["quote_status"] == "quoted"]
    imp = q[q["freight_imputed"].fillna(0) > 0]
    if len(imp):
        vs = sorted(imp["vendor_code"].unique())
        out.append({"icon": "≈", "label": f"{len(imp)} prices with freight estimated ({', '.join(vs)})", "target": "review", "vendor": vs[0]})
    pend = sorted(g.loc[g["qual_status"] == "pending", "vendor_code"].unique())
    if pend:
        out.append({"icon": "◔", "label": f"{len(pend)} vendor{'s' if len(pend) != 1 else ''} pending qualification ({', '.join(pend)})", "target": "responses", "vendor": pend[0]})
    nq = g[g["quote_status"].isin(["not_quoted", "no_bid"])]
    for v, grp in nq.groupby("vendor_code"):
        out.append({"icon": "✕", "label": f"{len(grp)} line{'s' if len(grp) != 1 else ''} not quoted by {v}", "target": "review", "vendor": v})
    return out


# ------------------------------------------------------------------ side panel
def build_up(r) -> dict:
    """Parts that add up to the evaluated price: base + freight (+ one-time charges) + payment cost = evaluated."""
    trace = json.loads(r["normalization_trace_json"] or "[]")
    base_note = next((t for t in trace if "(theoretical weight)" in t), next((t for t in trace if t.startswith("Vendor rate")), ""))
    m = re.search(r"INR ([\d.]+)/kg x ([\d.]+) kg", base_note)
    if m:
        base_note = f"₹{float(m.group(1)):g}/kg × {m.group(2)} kg box weight · derived"
    elif base_note.startswith("Vendor rate"):
        base_note = base_note.replace("INR ", "₹").replace("Vendor rate: ", "Vendor rate ").replace("_", " ")
    fr = {1: "not quoted · estimated at the highest other vendor's freight", 2: "not quoted · last year's freight"}.get(int(r["freight_imputed"] or 0))
    if fr is None:
        fr = "included in the rate (FOR Chakan)" if (r["freight_inr_pc"] or 0) == 0 else "quoted by the vendor"
    std = None
    pay = next((t for t in trace if t.startswith("Payment loading")), "")
    m2 = re.search(r"\(([\d.]+) - min\(", pay)
    if m2:
        std = int(float(m2.group(1)))
    days = r["effective_vendor_days"]
    pay_note = (f"{int(days)} days vs our {std} · cost of funds" if std is not None and pd.notna(days) else "payment terms vs standard")
    parts = [("Base price", float(r["base_inr_pc"] or 0), base_note), ("+ Freight", float(r["freight_inr_pc"] or 0), fr)]
    one = float(r["onetime_inr_pc"] or 0)
    if abs(one) > 1e-9:
        parts.append(("+ One-time charges", one, "spread over the annual quantity"))
    parts.append(("+ Payment cost", float(r["payment_loading_inr_pc"] or 0), pay_note))
    return {"parts": parts, "evaluated": float(r["evaluated_inr_pc"]), "sum": sum(p[1] for p in parts)}


def plain_issue(s: str) -> str:
    """'Q01_ISO9001 (unknown): ISO ... 2026-03-31' -> 'Q01: ISO ... 31 Mar 2026'."""
    s = re.sub(r"^(Q\d+)_[A-Z0-9_]+ \((?:unknown|fail|pass)\):\s*", r"\1: ", s.strip())
    return re.sub(r"\d{4}-\d{2}-\d{2}", lambda m: fmt_date(m.group()), s)


def cant_win_reason(r, qual: pd.DataFrame, scenario_title: str) -> str:
    code = r["exclusion_reason"] if isinstance(r["exclusion_reason"], str) else ""
    if code == "vendor_disqualified":
        q = qual[qual["vendor_code"] == r["vendor_code"]]
        issue = plain_issue(str(q.iloc[0]["ko_issues"]).split("; ")[0]) if len(q) and isinstance(q.iloc[0]["ko_issues"], str) else "it failed a knock-out question"
        return f"{r['vendor_code']} is disqualified. {issue}."
    from core.flags import explain_reason
    if code:
        return explain_reason(code)
    return f"{r['vendor_code']} is not eligible to win in this scenario ({scenario_title})."


# ------------------------------------------------------------------ qualification
def qualification_rows(conn: sqlite3.Connection) -> list[dict]:
    q = pd.read_sql_query("SELECT * FROM v_vendor_qualification ORDER BY vendor_code", conn)
    d = pd.read_sql_query("SELECT * FROM v_documents ORDER BY vendor_code, doc_type", conn)
    award = d["award_reference_date"].iloc[0] if len(d) else None
    out = []
    for _, r in q.iterrows():
        if isinstance(r["ko_issues"], str) and r["ko_issues"]:
            ko = " · ".join(plain_issue(x) for x in r["ko_issues"].split("; "))
        else:
            ko = f"All {int(r['ko_total'])} knock-outs met"
        docs = [doc_facts({k: (None if pd.isna(v) else v) for k, v in x.items()}) for _, x in d[d["vendor_code"] == r["vendor_code"]].iterrows()]
        out.append({"code": r["vendor_code"], "name": short_name(r["vendor_name"]), "status": r["qual_status"], "knockout": ko, "docs": " · ".join(docs) if docs else "None",
                    "award_date": award})
    return out
