"""Bid Analysis tab: line x vendor grid, how each price was computed, totals, savings and qualification."""
from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import streamlit as st

from agent.tools import award_scenario
from core.flags import FLAG_HELP, explain_reason
from core.queries import comparison_grid, qualification_panel, vendor_totals

WIN_CSS = "background-color:#c8e6c9;font-weight:bold"
OUT_CSS = "color:#9e9e9e;text-decoration:line-through"
OPEN_CSS = "background-color:#fff8e1"
QUAL_COLOUR = {"qualified": "green", "pending": "orange", "disqualified": "red"}


def _fmt(x) -> str:
    return "n/a" if pd.isna(x) else f"₹{x:,.0f}"


def _cells(g: pd.DataFrame, col: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(text grid, css grid) for line x vendor."""
    lines = sorted(g["line_no"].unique())
    vendors = sorted(g["vendor_code"].unique())
    text = pd.DataFrame("", index=lines, columns=vendors, dtype=object)
    css = pd.DataFrame("", index=lines, columns=vendors, dtype=object)
    for _, r in g.iterrows():
        ln, v = r["line_no"], r["vendor_code"]
        if r["quote_status"] != "quoted" or pd.isna(r[col]):
            text.loc[ln, v] = "not quoted" if r["quote_status"] != "quoted" else "no price"
            css.loc[ln, v] = "color:#bdbdbd;font-style:italic"
            continue
        flags = json.loads(r["flags_json"]) if isinstance(r["flags_json"], str) else []
        icons = ("⚠" if r["price_not_firm"] else "") + ("✱" if "open_must_confirm" in (r["ineligible_reasons_json"] or "") else "") \
            + ("≈" if r["freight_imputed"] else "") + ("◆" if "UNIT_AMBIGUOUS" in flags else "")
        text.loc[ln, v] = f"{r[col]:.2f} {icons}".strip()
        if r["rank_awardable"] == 1:
            css.loc[ln, v] = WIN_CSS
        elif not r["is_awardable"]:
            css.loc[ln, v] = OUT_CSS
    return text, css


def render(conn) -> None:
    g = comparison_grid(conn)
    if g.empty:
        st.info("No vendor quotes loaded yet. Extract the replies on the Responses tab first.")
        return
    basis_label = st.radio("Price shown", ["Evaluated (ranking price)", "Quoted (landed)"], horizontal=True, key="bid_basis",
                           help="Landed = rate converted to INR/pc plus freight, GST excluded. Evaluated adds one-time charges and the payment-terms loading, and is what ranks vendors.")
    basis = "evaluated" if basis_label.startswith("Evaluated") else "landed"
    col = "evaluated_inr_pc" if basis == "evaluated" else "landed_inr_pc"

    scen = award_scenario(conn, basis=basis)
    t = scen.attrs.get("totals", {})
    m = st.columns(4)
    m[0].metric("Best scenario value (awardable only)", _fmt(t.get("total_value_inr")), help="Cheapest awardable vendor on every line.")
    m[1].metric("Last year, same lines", _fmt(t.get("ly_value_inr")))
    m[2].metric("Saving vs last year (landed)", _fmt(t.get("saving_vs_ly_inr")),
                delta=f"{t.get('saving_vs_ly_pct', 0):.1f}%" if t.get("ly_value_inr") else None)
    m[3].metric("Lines with a winner", f"{t.get('lines_awarded', 0)} / {len(scen)}")
    if t.get("lines_on_not_firm_prices"):
        st.warning(f"⚠ {t['lines_on_not_firm_prices']} of {t['lines_awarded']} lines in the current best scenario rest on NOT-FIRM prices from "
                   f"{', '.join(t['not_firm_vendors'])} (₹{t['value_on_not_firm_prices_inr']:,.0f} of ₹{t['total_value_inr']:,.0f}). "
                   "Prices linked to kraft paper can move; the escalation clause is not modelled.")

    text, css = _cells(g, col)
    st.caption("**Green** = cheapest awardable vendor on the line · ~~grey strike-through~~ = cannot win (reason below) · "
               "⚠ price not firm · ✱ a must-confirm value is open · ≈ freight imputed · ◆ unit looks wrong · INR per piece, GST excluded.")
    st.dataframe(text.style.apply(lambda _: css, axis=None), width="stretch", height=560)

    st.subheader("Line detail")
    lines = sorted(g["line_no"].unique())
    ln = st.selectbox("Line", lines, format_func=lambda n: f"Line {n}: {g[g.line_no == n].iloc[0]['description']}", key="bid_line")
    gl = g[g.line_no == ln].sort_values("vendor_code")
    first = gl.iloc[0]
    st.caption(f"Annual {int(first['annual_qty']):,} pcs · typical order {int(first['typical_order_qty']):,} pcs · "
               f"last year landed {_fmt(first['ly_landed_inr_pc']).replace('₹', '₹')}" if pd.notna(first["ly_landed_inr_pc"]) else "")
    for _, r in gl.iterrows():
        status = "CAN WIN" if r["is_awardable"] else "cannot win"
        head = f"{r['vendor_code']} · {r['vendor_name']}: " + (f"landed ₹{r['landed_inr_pc']:.2f} → evaluated ₹{r['evaluated_inr_pc']:.2f}"
                                                          if pd.notna(r["evaluated_inr_pc"]) else "no price") + f"  ·  {status}"
        with st.expander(head, expanded=bool(r["rank_awardable"] == 1)):
            if not r["is_awardable"]:
                st.markdown(f"**Why it can't win:** {explain_reason(r['exclusion_reason'])}")
                reasons = json.loads(r["ineligible_reasons_json"] or "[]")
                for x in reasons:
                    st.markdown(f"- {explain_reason(x)}")
            else:
                st.markdown(":green[Eligible and from a qualified vendor.]")
            for f in (json.loads(r["flags_json"]) if isinstance(r["flags_json"], str) else []):
                st.markdown(f"- **{f}**: {FLAG_HELP.get(f, '')}")
            if r["normalization_trace_json"]:
                st.markdown("**How computed**")
                for step in json.loads(r["normalization_trace_json"]):
                    st.markdown(f"1. {step}")

    st.subheader("Totals per vendor")
    tot = vendor_totals(conn, basis)
    if not tot.empty:
        show = tot.rename(columns={"vendor_code": "Vendor", "lines_priced": "Lines priced", "value_inr": f"Annual value ({basis})",
                                   "ly_value_inr": "Last year, same lines", "saving_vs_ly_inr": "Saving vs LY (landed)",
                                   "saving_vs_ly_pct": "Saving %", "lines_awardable": "Lines it can win",
                                   "lines_excluded_unit_doubt": "Lines left out: unit unconfirmed"}).drop(columns=["landed_value_inr"])
        st.dataframe(show.style.format({f"Annual value ({basis})": "₹{:,.0f}", "Last year, same lines": "₹{:,.0f}",
                                        "Saving vs LY (landed)": "₹{:,.0f}", "Saving %": "{:.1f}%", "Lines it can win": "{:.0f}",
                                        "Lines left out: unit unconfirmed": "{:.0f}"}), hide_index=True, width="stretch")
        st.caption("Totals cover only the lines each vendor priced, so vendors that skipped lines look cheaper. Lines whose unit is still "
                   "ambiguous are left out of that vendor's total until you resolve them. Savings compare landed price with last year's main supplier.")
        chart = tot.assign(saving=tot["saving_vs_ly_inr"] / 1e5)
        fig = px.bar(chart, x="vendor_code", y="saving", labels={"saving": "Saving vs last year (₹ lakh)", "vendor_code": "Vendor"},
                     color="saving", color_continuous_scale=["#c62828", "#eeeeee", "#2e7d32"], color_continuous_midpoint=0)
        fig.update_layout(height=280, coloraxis_showscale=False, margin=dict(t=10, b=10))
        st.plotly_chart(fig, width="stretch")

    st.subheader("Vendor qualification")
    q, d = qualification_panel(conn)
    for _, r in q.iterrows():
        colour = QUAL_COLOUR.get(r["qual_status"], "gray")
        st.markdown(f":{colour}[**{r['qual_status'].upper()}**]  **{r['vendor_code']} · {r['vendor_name']}**  "
                    f"(knock-outs: {int(r['ko_pass'])} pass, {int(r['ko_fail'])} fail, {int(r['ko_unknown'])} unknown)")
        if r["ko_issues"]:
            st.caption(r["ko_issues"])
    st.markdown("**Documents, valid on the award reference date**")
    if d.empty:
        st.caption("No documents.")
    else:
        d["holder matches vendor"] = d["holder_matches_vendor"].map({1: "yes", 0: "NO: held by another company", None: "n/a"})
        st.dataframe(d.drop(columns=["holder_matches_vendor"]).rename(columns={"validity_status": "validity on award date"}),
                     hide_index=True, width="stretch")
