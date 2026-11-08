"""Create RFQ tab: an event setup page (overview, lines, questionnaire, suppliers & terms) with the co-pilot beside it,
then Review & publish and an email preview before the stubbed Send."""
from __future__ import annotations

import re

import streamlit as st

from copilot import copilot as C

WELCOME = ("I've loaded the RFQ as it is saved: the terms, the 30 box lines from the catalogue and the 8 vendor questions. "
           "Tell me what to change, for example “make payment 45 days”, “drop the promo box” or “make Q05 a knock-out”. "
           "Nothing is saved until you press **Publish RFQ**.")

SUGGESTIONS = ["Make payment 45 days", "Make Q05 a knock-out", "Drop the promo box", "Change the due date to 20 Nov"]


def status_label(status: str | None) -> str:
    """DB status -> the chip shown in the event header and on this tab."""
    return "Draft" if (status or "draft") == "draft" else "Published"


def contract_period(title: str, baseline: str | None) -> str:
    m = re.search(r"FY\d{4}-\d{2}", title or "")
    return m.group(0) if m else (baseline or "n/a")


def _state(conn) -> C.Draft:
    if "rfq_draft" not in st.session_state:
        st.session_state["rfq_draft"] = C.load_draft(conn)
        st.session_state["rfq_ver"] = 0
        st.session_state["rfq_chat"] = [{"role": "assistant", "text": WELCOME}]
    return st.session_state["rfq_draft"]


def _bump() -> None:
    st.session_state["rfq_ver"] += 1


def checks(d: C.Draft, n_vendors: int) -> list[tuple[bool, str]]:
    """Pass/warn marks for the Review & publish card."""
    inc = d.lines[d.lines["include"]]
    qs = d.questions[d.questions["include"]]
    ko = int(qs["is_knockout"].sum())
    due = str(d.terms.get("response_due_date") or "")
    return [(len(inc) > 0, f"{len(inc)} lines"), (len(qs) > 0, f"{len(qs)} questions"),
            (ko > 0, f"{ko} knock-out"), (n_vendors > 0, f"{n_vendors} suppliers"),
            (bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)), f"due {due or 'not set'}")]


def _email_preview(conn, body: str) -> None:
    vendors = conn.execute("SELECT name, vendor_code FROM vendors ORDER BY vendor_id").fetchall()
    lines = (body or "").splitlines()
    has_subject = bool(lines) and lines[0].startswith("Subject:")
    subject = lines[0].removeprefix("Subject:").strip() if has_subject else "RFQ"
    rest = "\n".join(lines[1:] if has_subject else lines).strip()
    with st.container(border=True):
        st.markdown("**To:** " + ", ".join(f"{v['name']} ({v['vendor_code']})" for v in vendors))
        st.markdown(f"**Subject:** {subject}")
        st.divider()
        st.text(rest)


def render(conn) -> None:
    d = _state(conn)
    ver = st.session_state["rfq_ver"]
    saved = C.load_draft(conn)
    sent = C.sent_info(conn)
    replies = C.replies_exist(conn)
    rfq = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    published = status_label(rfq["status"]) == "Published"
    vendors = conn.execute("SELECT * FROM vendors ORDER BY vendor_id").fetchall()
    pending = C.diff(d, saved)

    st.info("Demo RFQ pre-loaded. Vendor replies in this demo were written for it.")
    if (sent or replies) and pending:
        st.warning(f"Vendors have already quoted ({replies} vendors have replied{', RFQ sent' if sent else ''}). Your {len(pending)} unsaved edits will not change what "
                   "the vendors quoted. Publishing will recompute prices (payment days, order quantities and GST feed the evaluation) and re-run the questionnaire "
                   "rules; lines and questions that vendors have already answered cannot be removed.")
    elif published:
        when = f" (sent {sent['ts'][:16].replace('T', ' ')} UTC)" if sent else ""
        st.caption(f"Published{when}; {replies} vendors have replied. The tables are read-only: use the co-pilot to change anything, "
                   "and each change is checked against those replies before it is saved.")

    main, side = st.columns([3, 1.35], gap="large")

    # ---- co-pilot (right panel)
    with side:
        st.subheader("Co-pilot")
        box = st.container(height=420, border=True)
        with box:
            for m in st.session_state["rfq_chat"]:
                with st.chat_message(m["role"]):
                    st.markdown(m["text"])
                    for n in m.get("notes", []):
                        st.caption(n)
        st.caption("Try one:")
        for i, sug in enumerate(SUGGESTIONS):
            if st.button(sug, key=f"rfq_sug_{i}", width="stretch"):
                st.session_state["rfq_prompt"] = sug
                st.rerun()
        typed = st.chat_input("Ask the co-pilot to change the RFQ", key="rfq_chat_input")
        msg = typed or st.session_state.pop("rfq_prompt", None)
        if msg:
            hist = [(m["role"], m["text"]) for m in st.session_state["rfq_chat"] if m["role"] in ("user", "assistant")]
            st.session_state["rfq_chat"].append({"role": "user", "text": msg})
            try:
                reply, new, notes = C.ask_copilot(d, msg, hist)
                st.session_state["rfq_draft"] = new
                st.session_state["rfq_chat"].append({"role": "assistant", "text": reply.reply, "notes": notes})
                _bump()
            except Exception as e:  # show the real error
                st.session_state["rfq_chat"].append({"role": "assistant", "text": f"Sorry, the co-pilot failed: {e}"})
            st.rerun()

    with main:
        # ---- 1 overview
        st.subheader("1 · Overview")
        o = st.columns(2)
        o[0].text_input("Title", rfq["title"], disabled=True, key=f"rfq_title_{ver}")
        o[1].text_input("Category", rfq["category"], disabled=True, key=f"rfq_cat_{ver}")
        o2 = st.columns([2, 1, 1])
        d.terms["delivery_location"] = o2[0].text_input("Delivery (FOR)", d.terms["delivery_location"], key=f"rfq_loc_{ver}", disabled=published)
        o2[1].text_input("Contract period", contract_period(rfq["title"], rfq["baseline_period"]), disabled=True, key=f"rfq_per_{ver}")
        d.terms["response_due_date"] = o2[2].text_input("Response due (YYYY-MM-DD)", d.terms["response_due_date"], key=f"rfq_due_{ver}", disabled=published)

        # ---- 2 lines
        st.subheader(f"2 · Line items ({int(d.lines['include'].sum())} of {len(d.lines)} included)")
        edited = st.data_editor(
            d.lines, key=f"rfq_lines_{ver}", hide_index=True, width="stretch", height=380,
            disabled=True if published else ["line_no", "catalog_sku", "length_mm", "width_mm", "height_mm", "ply"],
            column_config={"include": st.column_config.CheckboxColumn("Include"), "line_no": "Line", "catalog_sku": "SKU", "description": "Description",
                           "length_mm": "L", "width_mm": "W", "height_mm": "H", "ply": "Ply",
                           "annual_qty": st.column_config.NumberColumn("Annual qty", min_value=0, step=100),
                           "typical_order_qty": st.column_config.NumberColumn("Typical order", min_value=0, step=100),
                           "print_colours": st.column_config.NumberColumn("Colours", min_value=0, max_value=8, step=1)})
        if not published:
            d.lines = edited

        # ---- 3 questionnaire
        st.subheader("3 · Questionnaire")
        for _, q in d.questions.iterrows():
            badge = " :red-badge[Knock-out]" if q["is_knockout"] else ""
            st.markdown(f"**{q['q_code']}**  {q['question_text']}{badge}" + ("" if q["include"] else "  _(dropped)_"))
        with st.expander("Edit questions" + (" (read-only after publish)" if published else "")):
            qed = st.data_editor(
                d.questions, key=f"rfq_q_{ver}", hide_index=True, width="stretch", disabled=True if published else ["q_code", "answer_type"],
                column_config={"include": st.column_config.CheckboxColumn("Include"), "q_code": "Code", "question_text": st.column_config.TextColumn("Question", width="large"),
                               "is_knockout": st.column_config.CheckboxColumn("Knock-out", help="A failed or missing knock-out answer stops a vendor qualifying."),
                               "answer_type": "Type"})
            if not published:
                d.questions = qed

        # ---- 4 suppliers & terms
        st.subheader("4 · Suppliers & terms")
        for col, v in zip(st.columns(len(vendors) or 1), vendors):
            col.markdown(f"**{v['vendor_code']}**  \n{v['name']}  \nMSME: {v['msme_category']}")
        t = st.columns([1, 1, 2])
        d.terms["payment_days"] = int(t[0].number_input("Payment days", 0, 365, int(d.terms["payment_days"]), key=f"rfq_pay_{ver}", disabled=published))
        d.terms["gst_pct"] = float(t[1].number_input("GST %", 0.0, 28.0, float(d.terms["gst_pct"]), key=f"rfq_gst_{ver}", disabled=published))
        d.terms["award_reference_date"] = t[2].text_input("Award reference date", d.terms["award_reference_date"], key=f"rfq_ref_{ver}",
                                                           help="Certificate validity is judged on this date.", disabled=published)
        for w in C.validate_draft(d):
            st.warning(w)

        # ---- 5 review & publish
        changes = C.diff(d, saved)
        st.subheader("5 · Review & publish")
        with st.container(border=True):
            cks = checks(d, len(vendors))
            for col, (ok, txt) in zip(st.columns(len(cks)), cks):
                col.markdown(f"{'✅' if ok else '⚠️'} {txt}")
            a = st.columns([1.2, 1, 3])
            if a[0].button("Publish RFQ" if not published else "Save changes", type="primary", key="rfq_confirm",
                           help="Saves the terms, lines and questions and marks the lines confirmed."):
                try:
                    res = C.save_draft(conn, d)
                    st.session_state["rfq_draft"] = C.load_draft(conn)
                    st.session_state["rfq_result"] = res
                    _bump()
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save: {e}")
            if a[1].button("Discard edits", key="rfq_discard", disabled=not changes):
                st.session_state["rfq_draft"] = C.load_draft(conn)
                _bump()
                st.rerun()
            a[2].caption(f"{len(changes)} unsaved changes" if changes else "No unsaved changes")
        res = st.session_state.pop("rfq_result", None)
        if res is not None:
            st.success(f"RFQ published: {len(res.changes)} changes saved." + (" Prices were recomputed against the existing replies." if res.recomputed else ""))
            for b in res.blocked:
                st.warning(b)

        # ---- 6 email preview + send
        st.subheader("6 · Email to suppliers")
        out = st.session_state.get("rfq_sent_body")
        _email_preview(conn, out["body"] if out else (rfq["rfq_body"] or C.build_rfq_body(conn)))
        if changes:
            st.caption("The preview shows the saved RFQ. Publish your edits first to include them.")
        if st.button("Send to 5 vendors", key="rfq_send", disabled=bool(changes),
                     help="Stubbed: writes the RFQ email body, marks the RFQ issued and logs it. Nothing is emailed. Publish your edits first."):
            st.session_state["rfq_sent_body"] = C.send_rfq(conn)
            st.rerun()
        if out:
            st.success(f"Sent (stub) at {out['sent_at'][:16].replace('T', ' ')} UTC to {', '.join(v['code'] for v in out['vendors'])}. No email was actually sent.")
