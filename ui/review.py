"""Review & Reconcile tab: every vendor value next to our line, with evidence and audited actions.

Page code only: all state changes go through core.actions (audit + recompute).
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from core import actions, audit
from core.flags import FIELD_LABELS, FLAG_HELP, confidence_chip, explain_reason
from core.queries import review_counts, review_filter, review_rows
from core.recompute import OVERRIDABLE

ROOT = Path(__file__).resolve().parent.parent
PAGE = 15
SEVERE = ("SANITY_RANGE_FAIL", "UNIT_AMBIGUOUS", "SECOND_READ_MISMATCH", "ARITHMETIC_FAIL", "LOW_CONFIDENCE_OCR", "FREIGHT_UNKNOWN",
          "SPEC_SUBSTITUTE", "SPEC_DEVIATION", "LY_SPEC_CHANGED", "LY_REF_MISSING", "DERIVED_PER_KG", "IMPLIED_BY_BLANKET",
          "TAX_BASIS_UNSPECIFIED", "TAX_BASIS_INCL_GST")

CORRECTABLE = {
    "raw_price": ("Price (as written)", "number"), "raw_unit": ("Unit", ["per_piece", "per_box", "per_100", "per_1000", "per_kg", "as_last_year"]),
    "raw_currency": ("Currency", ["INR", "USD"]), "freight_basis": ("Freight basis", ["included", "extra_quoted", "extra_unquoted"]),
    "raw_freight_value": ("Freight INR per piece", "number"), "tax_basis": ("Tax basis", ["ex_gst", "incl_gst", "unspecified"]),
    "gst_rate_pct": ("GST rate %", "number"), "payment_terms_days": ("Payment days", "int"), "lead_time_days": ("Lead time days", "int"),
}


def _money(x) -> str:
    return "n/a" if x is None else f"₹{x:,.2f}"


def _badge(flag: str) -> str:
    colour = "red" if flag in SEVERE else "orange"
    return f":{colour}-badge[{flag}]"


def _toggle(conn, quote_id: int, idx: int, key: str) -> None:
    actions.toggle_discount(conn, quote_id, idx, bool(st.session_state[key]))


def _row(conn, r: dict, ns: str) -> None:
    qid = r["quote_id"]
    with st.container(border=True):
        left, right = st.columns([2, 4])
        with left:
            if r["line_no"] is None:
                st.markdown("**No matching line**")
            else:
                st.markdown(f"**Line {r['line_no']}** · {r['description']}")
                st.caption(f"{r['length_mm']}×{r['width_mm']}×{r['height_mm']} mm · {r['ply']}-ply · layer GSM {'/'.join(map(str, r['layers']))}")
                st.caption(f"Annual {r['annual_qty']:,} pcs · typical order {r['typical_order_qty']:,} pcs")
        with right:
            st.markdown(f"**{r['vendor_code']} · {r['vendor_name']}**" + (f"  ·  :gray[{r['qual_status']}]" if r["qual_status"] else ""))
            if r["quote_status"] in ("not_quoted", "no_bid"):
                st.markdown(":gray[**Not quoted.**] " + explain_reason(r["quote_status"]))
                return
            if r["quote_status"] == "unmatched":
                st.markdown(f"Vendor line: `{r['vendor_line_ref']}`  ·  wrote `{r['raw_price_text']}` ({r['raw_unit']})")
                st.caption(explain_reason("unmatched"))
                return
            ov = r["overrides"]
            raw = f"**{r['raw_price']:g}** {r['raw_unit']} ({r['raw_currency']})" if r["raw_price"] is not None else f"*{r['raw_unit']}*"
            st.markdown(f"Vendor wrote: “{r['raw_price_text']}” → read as {raw}")
            if ov:
                st.caption("Corrected by you: " + "; ".join(f"{k} → {v['value']} ({v['reason']})" for k, v in ov.items()))
            imputed = {1: " (imputed from another vendor)", 2: " (imputed from last year)"}.get(r["freight_imputed"], "")
            st.markdown(f"Normalized: base **{_money(r['base_inr_pc'])}**/pc · freight {_money(r['freight_inr_pc'])}{imputed} · "
                        f"landed **{_money(r['landed_inr_pc'])}** · evaluated **{_money(r['evaluated_inr_pc'])}**")
            chips = " ".join(f"{FIELD_LABELS.get(f, f)} {confidence_chip(r['fields'][f]['confidence'], r['fields'][f]['status'])}"
                             for f in r["open_fields"])
            st.markdown(f"Confidence {confidence_chip(r['min_confidence'], r['review_state'])}" + (f"  ·  open: {chips}" if chips else "  ·  nothing open")
                        + (f"  ·  **Not eligible:** {explain_reason(r['exclusion_reason'])}" if r["exclusion_reason"] else ""))
            if r["flags"]:
                st.markdown(" ".join(_badge(f) for f in r["flags"]))
                for f in [x for x in r["flags"] if x in SEVERE][:2]:
                    st.caption(f"**{f}:** {FLAG_HELP.get(f, '')}")
            with st.expander("Evidence and why flagged"):
                for f in r["flags"]:
                    st.markdown(f"- **{f}**: {FLAG_HELP.get(f, '')}")
                for fld, meta in r["fields"].items():
                    src = meta.get("source")
                    if src:
                        st.markdown(f"**{FIELD_LABELS.get(fld, fld)} source:** `{src.get('ref')}`: “{src.get('snippet')}”")
                if r["spec_deviation_note"]:
                    st.markdown(f"**Spec:** {r['spec_match']}: {r['spec_deviation_note']}")
                if r["validation"]:
                    st.dataframe([{"layer": c["layer"], "check": c["check"], "result": c["result"], "detail": c["detail"]} for c in r["validation"]],
                                 hide_index=True, width="stretch")
                if r["source_type"] == "photo" and r["file_path"] and Path(ROOT / r["file_path"]).exists():
                    st.image(str(ROOT / r["file_path"]), caption=f"Source photo ({r['file_name']})")
            with st.expander("How computed"):
                for t in r["trace"]:
                    st.markdown(f"- {t}")

            cols = st.columns([1, 1, 1, 3])
            if cols[0].button("Confirm", key=f"{ns}_cf_{qid}", disabled=not r["open_fields"], help="Accept the open values as read. Allowed even with open flags; recorded in the audit log."):
                actions.confirm_fields(conn, qid)
                st.toast(f"Confirmed {r['vendor_code']} line {r['line_no']}")
                st.rerun()
            with cols[1].popover("Correct"):
                col = st.selectbox("Value to correct", list(CORRECTABLE), format_func=lambda c: CORRECTABLE[c][0], key=f"{ns}_cc_{qid}")
                kind = CORRECTABLE[col][1]
                if isinstance(kind, list):
                    val = st.selectbox("New value", kind, key=f"{ns}_cv_{qid}")
                else:
                    val = st.text_input("New value", key=f"{ns}_cv_{qid}")
                reason = st.text_input("Reason (required)", key=f"{ns}_cr_{qid}")
                if st.button("Save correction", key=f"{ns}_cs_{qid}"):
                    try:
                        if kind == "number":
                            val = float(val)
                        elif kind == "int":
                            val = int(val)
                        actions.correct_field(conn, qid, col, val, reason)
                        st.toast("Correction saved")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e) if "reason" in str(e) else f"Invalid value: {e}")
            if r["spec_match"] in ("deviation", "substitute") and not r["spec_accepted"]:
                with cols[2].popover("Accept spec"):
                    st.caption(r["spec_deviation_note"] or "")
                    why = st.text_input("Reason / QA reference (required)", key=f"{ns}_sr_{qid}")
                    if st.button("Accept", key=f"{ns}_sa_{qid}"):
                        try:
                            actions.accept_spec(conn, qid, why)
                            st.toast("Spec accepted")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
            elif r["spec_accepted"]:
                cols[2].markdown(":blue[spec accepted]")
            for i, d in enumerate(r["discounts"]):
                if d.get("condition_type") == "volume_qty":
                    continue  # volume discounts are matched against order quantity automatically (D1), not toggled
                k = f"{ns}_dt_{qid}_{i}"
                cols[3].checkbox(f"Apply {d['pct']:g}%: {d['condition_text']}", value=bool(d.get("applied")), key=k,
                                 on_change=_toggle, args=(conn, qid, i, k),
                                 help="Conditional discounts are never applied automatically. Toggling is audited and recomputes the price.")


def _unmatched_controls(conn, r: dict, ns: str) -> None:
    free = [row["line_no"] for row in conn.execute(
        "SELECT line_no FROM line_items WHERE line_id NOT IN (SELECT line_id FROM quotes WHERE vendor_id=? AND is_current=1 AND line_id IS NOT NULL) ORDER BY line_no",
        (conn.execute("SELECT vendor_id FROM vendors WHERE vendor_code=?", (r["vendor_code"],)).fetchone()[0],))]
    c = st.columns([2, 3, 1])
    ln = c[0].selectbox("Match to our line", free, key=f"{ns}_mu_{r['quote_id']}")
    why = c[1].text_input("Reason (required)", key=f"{ns}_mr_{r['quote_id']}")
    if c[2].button("Match", key=f"{ns}_mb_{r['quote_id']}"):
        try:
            actions.match_unmatched(conn, r["quote_id"], ln, why)
            st.rerun()
        except ValueError as e:
            st.error(str(e))


def _list(conn, rows: list[dict], ns: str) -> None:
    if not rows:
        st.info("Nothing here.")
        return
    if ns == "needs_review":
        flags = sum(len(r["open_fields"]) for r in rows)
        with st.expander(f"Confirm all {len(rows)} rows shown ({flags} open values)"):
            st.warning(f"This accepts {flags} open values on {len(rows)} rows, including values still carrying flags. It does not block, and every "
                       "confirmation is written to the audit trail under one batch id.")
            if st.button(f"Confirm all {len(rows)} shown", key=f"{ns}_bulk"):
                batch = audit.new_batch_id()
                for r in rows:
                    actions.confirm_fields(conn, r["quote_id"], batch_id=batch)
                st.toast(f"Confirmed {len(rows)} rows")
                st.rerun()
    pages = max(1, -(-len(rows) // PAGE))
    page = st.number_input(f"Page (of {pages})", 1, pages, 1, key=f"{ns}_page") if pages > 1 else 1
    for r in rows[(page - 1) * PAGE: page * PAGE]:
        _row(conn, r, ns)
        if r["quote_status"] == "unmatched":
            _unmatched_controls(conn, r, ns)


def render(conn) -> None:
    rows = review_rows(conn)
    if not rows:
        st.info("No vendor quotes yet. Extract the replies on the Responses tab first.")
        return
    f = st.columns([2, 2, 3])
    vendor = f[0].selectbox("Vendor", ["All"] + sorted({r["vendor_code"] for r in rows}), key="rv_vendor")
    vendor = None if vendor == "All" else vendor
    only_winner = f[1].checkbox("Only items that could change the winner", key="rv_cw",
                                help="Open values on quotes that are cheaper than, or equal to, the best awardable price on their line.")
    f[2].caption("Needs review = quoted rows with at least one must-confirm or spot-check value. Not quoted lines are never treated as zero.")
    counts = review_counts(rows, vendor)
    tabs = st.tabs([f"All ({counts['all']})", f"Needs review ({counts['needs_review']})",
                    f"Not quoted ({counts['not_quoted']})", f"Unmatched ({counts['unmatched']})"])
    cw = set()
    if only_winner:
        cw = {(q["line_no"], q["vendor_code"]) for q in conn.execute(
            "SELECT DISTINCT line_no, vendor_code FROM v_review_queue WHERE could_change_winner=1")}
    for tab, key in zip(tabs, ("all", "needs_review", "not_quoted", "unmatched")):
        with tab:
            sel = review_filter(rows, key, vendor)
            if only_winner:
                sel = [r for r in sel if (r["line_no"], r["vendor_code"]) in cw]
            _list(conn, sel, key)
