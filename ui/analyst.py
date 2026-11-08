"""Analyst & Award tab: chat over the views, the Award panel (Confirm award), Excel export, clarification emails."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from agent import tools as T
from agent.analyst import ask
from core import audit
from core.config import load_config
from export.excel import build_workbook


def _limit() -> int:
    return int(load_config().get("chat", {}).get("max_messages", 30))


def _fmt(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and pd.isna(x)) else f"₹{x:,.0f}"


def _award_panel(conn) -> None:
    prop = st.session_state.get("proposal")
    committed = conn.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0]
    n_open = conn.execute("SELECT COUNT(*) FROM v_review_queue WHERE status='must_confirm' AND could_change_winner=1").fetchone()[0]
    if committed and not prop:
        row = conn.execute("SELECT MIN(awarded_at) t FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()
        st.success(f"Award committed: {committed} lines (at {row['t']}). See the Audit trail sheet in the export for the per-line record.")
        with st.expander("Clear the committed award"):
            why = st.text_input("Reason (required)", key="unaward_reason")
            if st.button("Clear award", key="unaward_btn"):
                try:
                    st.toast(f"Cleared {T.unaward(conn, why)} lines")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    if not prop:
        if not committed:
            st.info("No proposed award yet. Ask the analyst to propose one, e.g. “Propose the cheapest award among qualified vendors.”")
        return
    t = prop["totals"]
    st.subheader("Proposed award (not committed)")
    m = st.columns(4)
    m[0].metric("Total value (evaluated)", _fmt(t.get("total_value_inr")))
    m[1].metric("Saving vs last year (landed)", _fmt(t.get("saving_vs_ly_inr")), delta=f"{t.get('saving_vs_ly_pct', 0):.1f}%")
    m[2].metric("Lines awarded", f"{t.get('lines_awarded', 0)} / {t.get('lines_awarded', 0) + t.get('lines_without_winner', 0)}")
    m[3].metric("By vendor", ", ".join(f"{k}: {v}" for k, v in (t.get("lines_by_vendor") or {}).items()) or "none")
    st.caption("Scenario: " + (", ".join(f"{k}={v}" for k, v in prop["params"].items()) or "defaults (evaluated price, qualified vendors only)"))
    if t.get("lines_on_not_firm_prices"):
        st.warning(f"⚠ {t['lines_on_not_firm_prices']} awarded lines rest on NOT-FIRM (kraft-linked) prices from {', '.join(t['not_firm_vendors'])} "
                   f"(₹{t['value_on_not_firm_prices_inr']:,.0f}). The escalation clause is not modelled.")
    if t.get("lines_with_imputed_freight"):
        st.warning(f"{t['lines_with_imputed_freight']} awarded lines use imputed freight (not quoted by the vendor).")
    if n_open:
        st.warning(f"{n_open} values awaiting confirmation could change the award. Review them before committing.")
    if t.get("lines_without_winner"):
        st.warning(f"{t['lines_without_winner']} lines have no eligible winner in this scenario and will NOT be awarded.")
    df = pd.DataFrame(prop["rows"])
    show = df[["line_no", "description", "vendor_code", "price_inr_pc", "backup_vendor", "backup_price_inr_pc", "annual_value_inr", "price_not_firm",
               "freight_imputed", "review_state", "no_winner_reason"]].rename(columns={
        "line_no": "Line", "description": "Description", "vendor_code": "Winner", "price_inr_pc": "Evaluated ₹/pc", "backup_vendor": "Backup (L2)",
        "backup_price_inr_pc": "Backup ₹/pc", "annual_value_inr": "Annual value ₹", "price_not_firm": "Not firm", "freight_imputed": "Freight imputed",
        "review_state": "Review state", "no_winner_reason": "No winner because"})
    st.dataframe(show.style.format({"Evaluated ₹/pc": "{:.2f}", "Backup ₹/pc": "{:.2f}", "Annual value ₹": "{:,.0f}"}, na_rep=""),
                 hide_index=True, width="stretch", height=300)

    review = T.award_review(conn, prop)
    byp = review[review["bypass"]]
    reason, code = None, None
    if len(byp):
        st.warning(f"On {len(byp)} lines the proposed award is NOT the cheapest eligible quote. A reason is required to commit.")
        st.dataframe(byp[["line_no", "awarded_vendor", "l1_eligible_vendor", "l1_vendor"]].rename(columns={
            "line_no": "Line", "awarded_vendor": "Proposed", "l1_eligible_vendor": "Cheapest eligible", "l1_vendor": "Cheapest quote (any)"}),
            hide_index=True, width="stretch")
        c = st.columns([1, 3])
        code = c[0].selectbox("Reason code", T.BYPASS_CODES, index=T.BYPASS_CODES.index("buyer_judgment"), key="bypass_code")
        reason = c[1].text_input("Reason (required)", key="bypass_reason")
    b = st.columns([1, 1, 4])
    if b[0].button("Confirm award", type="primary", key="confirm_award"):
        try:
            res = T.commit_award(conn, prop, reason=reason, bypass_code=code)
            st.session_state["proposal"] = None
            st.session_state["commit_result"] = res
            st.rerun()
        except ValueError as e:
            st.error(str(e))
    if b[1].button("Discard proposal", key="discard_proposal"):
        st.session_state["proposal"] = None
        st.rerun()


def _clarifications(conn) -> None:
    rows = conn.execute("""SELECT vr.response_id, v.vendor_code, v.name, vr.clarification_draft, vr.clarification_status
                           FROM vendor_responses vr JOIN vendors v USING (vendor_id)
                           WHERE vr.clarification_draft IS NOT NULL
                             AND NOT EXISTS (SELECT 1 FROM vendor_responses n WHERE n.supersedes_response_id = vr.response_id)
                           ORDER BY v.vendor_id""").fetchall()
    with st.expander(f"Clarification emails ({len(rows)} drafted)"):
        if not rows:
            st.caption("No clarification drafts yet. They are created when the replies are extracted.")
        for r in rows:
            st.markdown(f"**{r['vendor_code']} · {r['name']}**  ·  status: `{r['clarification_status']}`")
            st.text(r["clarification_draft"])
            if r["clarification_status"] != "sent_stub" and st.button(
                    "Mark sent (stub)", key=f"sent_{r['response_id']}", help="Sending is stubbed: this only records that the email was sent."):
                conn.execute("UPDATE vendor_responses SET clarification_status='sent_stub' WHERE response_id=?", (r["response_id"],))
                audit.log(conn, "clarification_sent_stub", ("response", r["response_id"]), new="sent_stub")
                conn.commit()
                st.rerun()
            st.divider()


def render(conn) -> None:
    st.session_state.setdefault("chat", [])
    st.session_state.setdefault("proposal", None)
    res = st.session_state.pop("commit_result", None)
    if res:
        st.success(f"Award committed: {res['lines_awarded']} lines written, one audit row per line (batch {res['batch_id'][:8]}). "
                   + (f"Bypass reason recorded on lines {res['bypass_lines']}." if res["bypass_lines"] else ""))
    _award_panel(conn)
    c = st.columns([1, 4])
    if conn.execute("SELECT COUNT(*) FROM quotes WHERE is_current=1").fetchone()[0] == 0:
        c[0].button("Download Excel", disabled=True, key="dl_xlsx_off")
        c[1].caption("No award proposed yet: there are no quotes loaded, so there is nothing to export.")
    else:
        # data is a callable: the workbook is built only when the download is clicked
        c[0].download_button("Download Excel", data=lambda: build_workbook(conn, st.session_state.get("proposal")),
                             file_name="award_workbook.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_xlsx",
                             help="Award, Scenario (all candidates and exclusion reasons), Audit trail and Assumptions sheets.")
        c[1].caption("The workbook shows the committed award if there is one, otherwise the proposal or the current best scenario, and says which.")
    _clarifications(conn)

    st.divider()
    st.subheader("Ask the analyst")
    st.caption("Answers come from the comparison data and scenario tools only. The analyst cannot commit an award; you do that with Confirm award.")
    for i, m in enumerate(st.session_state["chat"]):
        with st.chat_message(m["role"]):
            st.markdown(m["text"])
            for title, df in m.get("tables", []):
                st.markdown(f"**{title}**")
                st.dataframe(df, hide_index=True, width="stretch")
            for j, (title, fig) in enumerate(m.get("figures", [])):
                st.plotly_chart(fig, width="stretch", key=f"fig_{i}_{j}")
            if m.get("trace"):
                with st.expander(f"How I got this ({len(m['trace'])} tool calls)"):
                    for t in m["trace"]:
                        st.code(f"{t['tool']}({t['args']})" + ("" if t["ok"] else f"\nERROR: {t['result']}"), language="text")

    used = sum(1 for m in st.session_state["chat"] if m["role"] == "user")
    left = _limit() - used
    q = st.chat_input("Ask about prices, qualification, what-ifs, or ask for a proposed award" if left > 0 else "Message limit reached for this session",
                      disabled=left <= 0, key="chat_input")
    st.caption(f"{max(left, 0)} of {_limit()} messages left in this session.")
    if q:
        history = [(m["role"], m["text"]) for m in st.session_state["chat"]]
        st.session_state["chat"].append({"role": "user", "text": q})
        with st.spinner("Analysing ..."):
            try:
                r = ask(conn, q, history)
                msg = {"role": "assistant", "text": r.text or "(no answer)", "tables": r.tables, "figures": r.figures, "trace": r.trace}
                if r.proposal:
                    st.session_state["proposal"] = r.proposal
            except Exception as e:  # show the real error to the buyer
                msg = {"role": "assistant", "text": f"Sorry, the analyst failed: {e}"}
        st.session_state["chat"].append(msg)
        st.rerun()
