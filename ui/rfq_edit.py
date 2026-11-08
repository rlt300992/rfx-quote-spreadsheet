"""Create RFQ tab: chat co-pilot + editable terms, lines and questions; Confirm RFQ; Send to vendors (stub)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from copilot import copilot as C

WELCOME = ("I've loaded the RFQ as it is saved: the terms, the 30 box lines from the catalogue and the 8 vendor questions. "
           "Tell me what to change, for example “make payment 45 days”, “drop the promo box” or “make Q05 a knock-out”, "
           "or edit the tables directly. Nothing is saved until you press **Confirm RFQ**.")


def _state(conn) -> C.Draft:
    if "rfq_draft" not in st.session_state:
        st.session_state["rfq_draft"] = C.load_draft(conn)
        st.session_state["rfq_ver"] = 0
        st.session_state["rfq_chat"] = [{"role": "assistant", "text": WELCOME}]
    return st.session_state["rfq_draft"]


def _bump() -> None:
    st.session_state["rfq_ver"] += 1


def render(conn) -> None:
    d = _state(conn)
    ver = st.session_state["rfq_ver"]
    saved = C.load_draft(conn)
    sent = C.sent_info(conn)
    replies = C.replies_exist(conn)
    rfq = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()

    st.markdown(f"**{rfq['title']}**  ·  status: `{rfq['status']}`" + (f"  ·  :green[Sent (stub) {sent['ts'][:16].replace('T', ' ')} UTC to {len(sent['vendors'])} vendors]" if sent else ""))
    pending = C.diff(d, saved)
    if (sent or replies) and pending:
        st.warning(f"This RFQ has already been {'sent' if sent else 'issued'} and {replies} vendors have replied. Your {len(pending)} unsaved edits will not change what "
                   "the vendors quoted. Confirming will recompute prices (payment days, order quantities and GST feed the evaluation) and re-run the questionnaire "
                   "rules; lines and questions that vendors have already answered cannot be removed.")
    elif sent or replies:
        st.caption(f"Already sent; {replies} vendors have replied. Edits here are checked against those replies before they are saved.")

    # ---- chat
    if w := st.session_state.pop("ai_warn", None):
        st.warning(w, icon="⚠️")
    box = st.container(height=240, border=True)
    with box:
        for m in st.session_state["rfq_chat"]:
            with st.chat_message(m["role"]):
                st.markdown(m["text"])
                for n in m.get("notes", []):
                    st.caption(n)
    msg = st.chat_input("Ask the co-pilot to change the RFQ", key="rfq_chat_input")
    if msg:
        hist = [(m["role"], m["text"]) for m in st.session_state["rfq_chat"] if m["role"] in ("user", "assistant")]
        st.session_state["rfq_chat"].append({"role": "user", "text": msg})
        try:
            reply, new, notes = C.ask_copilot(d, msg, hist)
            st.session_state["rfq_draft"] = new
            st.session_state["rfq_chat"].append({"role": "assistant", "text": reply.reply, "notes": notes})
            _bump()
        except Exception as e:  # show the real error
            from core import errors
            st.session_state["rfq_chat"].append({"role": "assistant", "text": "⚠️ " + errors.friendly(e)})
            st.session_state["ai_warn"] = errors.friendly(e)
        st.rerun()

    # ---- terms
    st.subheader("Terms")
    t = st.columns([1, 1, 3, 1.4, 1.4])
    d.terms["payment_days"] = int(t[0].number_input("Payment days", 0, 365, int(d.terms["payment_days"]), key=f"rfq_pay_{ver}"))
    d.terms["gst_pct"] = float(t[1].number_input("GST %", 0.0, 28.0, float(d.terms["gst_pct"]), key=f"rfq_gst_{ver}"))
    d.terms["delivery_location"] = t[2].text_input("Delivery (FOR)", d.terms["delivery_location"], key=f"rfq_loc_{ver}")
    d.terms["response_due_date"] = t[3].text_input("Response due (YYYY-MM-DD)", d.terms["response_due_date"], key=f"rfq_due_{ver}")
    d.terms["award_reference_date"] = t[4].text_input("Award reference date", d.terms["award_reference_date"], key=f"rfq_ref_{ver}",
                                                       help="Certificate validity is judged on this date.")
    for w in C.validate_draft(d):
        st.warning(w)

    # ---- lines
    st.subheader(f"Lines ({int(d.lines['include'].sum())} of {len(d.lines)} included)")
    edited = st.data_editor(
        d.lines, key=f"rfq_lines_{ver}", hide_index=True, width="stretch", height=380,
        disabled=["line_no", "catalog_sku", "length_mm", "width_mm", "height_mm", "ply"],
        column_config={"include": st.column_config.CheckboxColumn("Include"), "line_no": "Line", "catalog_sku": "SKU", "description": "Description",
                       "length_mm": "L", "width_mm": "W", "height_mm": "H", "ply": "Ply",
                       "annual_qty": st.column_config.NumberColumn("Annual qty", min_value=0, step=100),
                       "typical_order_qty": st.column_config.NumberColumn("Typical order", min_value=0, step=100),
                       "print_colours": st.column_config.NumberColumn("Colours", min_value=0, max_value=8, step=1)})
    d.lines = edited

    st.subheader("Vendor questions")
    qed = st.data_editor(
        d.questions, key=f"rfq_q_{ver}", hide_index=True, width="stretch", disabled=["q_code", "answer_type"],
        column_config={"include": st.column_config.CheckboxColumn("Include"), "q_code": "Code", "question_text": st.column_config.TextColumn("Question", width="large"),
                       "is_knockout": st.column_config.CheckboxColumn("Knock-out", help="A failed or missing knock-out answer stops a vendor qualifying."),
                       "answer_type": "Type"})
    d.questions = qed

    # ---- actions
    changes = C.diff(d, saved)
    a = st.columns([1, 1.4, 1, 4])
    if a[0].button("Confirm RFQ", type="primary", key="rfq_confirm", help="Saves the terms, lines and questions and marks the lines confirmed."):
        try:
            res = C.save_draft(conn, d)
            st.session_state["rfq_draft"] = C.load_draft(conn)
            st.session_state["rfq_result"] = res
            _bump()
            st.rerun()
        except Exception as e:
            st.error(f"Could not save: {e}")
    if a[1].button("Send to 5 vendors", key="rfq_send", disabled=bool(changes),
                   help="Stubbed: writes the RFQ email body, marks the RFQ issued and logs it. Nothing is emailed. Confirm your edits first."):
        out = C.send_rfq(conn)
        st.session_state["rfq_sent_body"] = out
        st.rerun()
    if a[2].button("Discard edits", key="rfq_discard", disabled=not changes):
        st.session_state["rfq_draft"] = C.load_draft(conn)
        _bump()
        st.rerun()
    a[3].caption(f"{len(changes)} unsaved changes" if changes else "No unsaved changes")

    res = st.session_state.pop("rfq_result", None)
    if res is not None:
        st.success(f"RFQ confirmed: {len(res.changes)} changes saved." + (" Prices were recomputed against the existing replies." if res.recomputed else ""))
        for b in res.blocked:
            st.warning(b)
    out = st.session_state.get("rfq_sent_body")
    if out:
        st.success(f"Sent (stub) at {out['sent_at'][:16].replace('T', ' ')} UTC to {', '.join(v['code'] for v in out['vendors'])}. No email was actually sent.")
    if rfq["rfq_body"]:
        with st.expander("RFQ email body (stub)"):
            st.text(rfq["rfq_body"] if not out else out["body"])
