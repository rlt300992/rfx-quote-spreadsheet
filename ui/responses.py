"""Responses tab: one card per vendor with the main reply, the questionnaire (with linked evidence) and the clarification email."""
from __future__ import annotations

import streamlit as st

from core.queries import vendor_cards
from core.responses_view import (FORMAT_LABEL, QUAL_CHIP, ROLE_LABEL, SIZE_LABEL, card_extras, questionnaire_view,
                                 send_clarification)
from core.stage import fmt_date
from extract.extract import ingest_all, ingest_vendor, load_manifest
from ui.preview import preview_dialog

ICON = {"excel": "📊", "pdf": "📄", "word": "📝", "photo": "🖼️", "email_text": "✉️"}
RESULT = {"pass": "✅", "fail": "❌"}


def _tier(msme: str | None) -> str:
    return f" ({SIZE_LABEL.get(msme or 'none', msme)})" if msme else ""


def _open_dialog(*spec) -> None:
    st.session_state["_responses_dialog"] = spec


def _close_dialog() -> None:
    st.session_state.pop("_responses_dialog", None)


@st.dialog("Re-extract all responses", on_dismiss=_close_dialog)
def _reextract_all_dialog(conn, n: int) -> None:
    st.markdown(f"**Re-extract all {n} responses?**")
    st.write("This re-extracts every file and replaces all current extracted values. Corrections and confirmations made in Review & Reconcile will be lost.")
    c = st.columns(2)
    if c[0].button("Cancel", type="primary", key="rx_all_cancel", width="stretch"):
        _close_dialog()
        st.rerun()
    if c[1].button("Re-extract all", key="rx_all_go", width="stretch"):
        with st.spinner("Re-extracting all vendor replies ..."):
            try:
                ingest_all(conn, force=True)
            except Exception as e:  # show the real error, never swallow it
                st.error(f"Extraction failed: {e}")
                return
        _close_dialog()
        st.rerun()


@st.dialog("Re-extract this response", on_dismiss=_close_dialog)
def _reextract_vendor_dialog(conn, entry: dict | None, code: str, name: str) -> None:
    st.markdown(f"**Re-extract {code} · {name}?**")
    st.write(f"This re-extracts {code}'s files and replaces {code}'s current extracted values. Corrections and confirmations made for {code} "
             "in Review & Reconcile will be lost. Other vendors are not affected.")
    if entry is None:
        st.error(f"No inbox entry found for {code}.")
    c = st.columns(2)
    if c[0].button("Cancel", type="primary", key=f"rx_{code}_cancel", width="stretch"):
        _close_dialog()
        st.rerun()
    if c[1].button(f"Re-extract {code}", key=f"rx_{code}_go", disabled=entry is None, width="stretch"):
        with st.spinner(f"Re-extracting {code} ..."):
            try:
                ingest_vendor(conn, entry, force=True)
            except Exception as e:
                st.error(f"Re-extraction failed: {e}")
                return
        _close_dialog()
        st.rerun()


def _goto_review(code: str) -> None:
    st.session_state["rv_vendor"] = code
    st.session_state["main_tab"] = "Review & Reconcile"
    st.toast(f"Filter set to {code}. Open Review & Reconcile ↑")


def _evidence_cell(code: str, q: dict) -> None:
    ev = q["evidence"]
    if ev:
        st.markdown(f":red[{ev['facts']}]" if ev["expired"] else ev["facts"])
        if st.button("Preview ↗", key=f"pv_ev_{code}_{q['q_code']}"):
            preview_dialog(ev["file_path"], ev["file_name"])
    if q["ref"]:
        st.caption(f"Source: {q['ref']}")
    if not ev and not q["ref"]:
        st.caption("n/a")


def _questionnaire(card: dict, qv: dict, draft_has_request: bool) -> None:
    code = card["vendor_code"]
    parts = [f"{qv['met']} met", f"{qv['not_met']} not met"] + ([f"{qv['pending']} pending"] if qv["pending"] else [])
    summary = " · ".join(parts) + (f" · 📎 {qv['evidence_n']} evidence doc{'s' if qv['evidence_n'] != 1 else ''}" if qv["evidence_n"] else "")
    label, colour = QUAL_CHIP.get(qv["qual_status"], ("Pending", "orange"))
    with st.expander(f"Questionnaire ({len(qv['rows'])}) · {summary} · :{colour}-badge[{label}]", expanded=False):
        if not qv["has_answers"]:
            sent = "only a rate card" if card["main_role"] == "quote" else "no questionnaire answers"
            extra = " A request for the answers is in the clarification draft below." if draft_has_request else ""
            st.info(f"The vendor sent {sent}. All {len(qv['rows'])} answers are missing, including the {qv['ko_total']} knock-outs, "
                    f"so the vendor stays {label}.{extra}")
        else:
            h = st.columns([3, 0.5, 3, 3.5])
            for c, t in zip(h, ("Question", "", "Vendor's answer", "Evidence · source")):
                c.markdown(f"**{t}**")
            for q in qv["rows"]:
                c = st.columns([3, 0.5, 3, 3.5])
                c[0].markdown(f"**{q['q_code']}** {q['text']}" + (" :red-badge[Knock-out]" if q["is_knockout"] else ""))
                c[1].markdown(RESULT.get(q["result"], "⏳"))
                c[2].write(q["answer"] if q["answered"] else "No answer received")
                with c[3]:
                    _evidence_cell(code, q)
        if qv["other"]:
            st.markdown("**Other attachments** (answer no question)")
            for i, o in enumerate(qv["other"]):
                c = st.columns([6, 1.5])
                c[0].write(f"📎 {o['file_name']}")
                if c[1].button("Preview ↗", key=f"pv_oth_{code}_{i}"):
                    preview_dialog(o["file_path"], o["file_name"])


def _clarification(conn, card: dict) -> None:
    code = card["vendor_code"]
    draft = card["clarification_draft"]
    if not draft:
        return
    sent = card["clarification_status"] == "sent_stub"
    label = f"Clarification sent · {fmt_date(card['clarification_sent_at'])}" if sent else "Draft clarification email"
    with st.expander(label):
        txt = st.text_area("Email text", draft, height=320, key=f"clar_{code}", disabled=sent, label_visibility="collapsed")
        if sent:
            st.caption("Sent (stub): no email was actually sent. The final text is recorded in the audit log.")
        elif st.button("Send (stub)", key=f"clar_send_{code}", help="Sending is stubbed: the final text is saved to the audit log."):
            send_clarification(conn, card["draft_response_id"], txt)
            st.rerun()


def render(conn) -> None:
    manifest = load_manifest()
    entries = {m["vendor_code"]: m for m in manifest}
    cards = vendor_cards(conn, manifest)
    top = st.columns([1.2, 5])
    top[0].button("↻ Extract all", key="extract_all", width="stretch", on_click=_open_dialog, args=("all",))
    spec = st.session_state.get("_responses_dialog")
    if spec and spec[0] == "all":
        _reextract_all_dialog(conn, len(cards))
    elif spec and spec[0] == "vendor":
        _reextract_vendor_dialog(conn, entries.get(spec[1]), spec[1], spec[2])
    top[1].caption("Values come from real AI extraction (cached by file hash, prompt version and model). Use ↻ on a card to re-extract one vendor.")

    for card in cards:
        code = card["vendor_code"]
        ex = card_extras(conn, conn.execute("SELECT vendor_id FROM vendors WHERE vendor_code=?", (code,)).fetchone()[0])
        card = {**card, **ex}
        vid = conn.execute("SELECT vendor_id FROM vendors WHERE vendor_code=?", (code,)).fetchone()[0]
        with st.container(border=True):
            head = st.columns([3.4, 1.3, 2.2, 1.5, 0.5], vertical_alignment="center")
            head[0].markdown(f"**{code} · {card['name']}**{_tier(card['msme'])}")
            head[1].markdown(f"Format: **{FORMAT_LABEL.get(card['format'], card['format'] or 'n/a')}**")
            late = " :red-badge[LATE]" if card["late"] else ""
            head[2].markdown(f"Received: {(card['received_at'] or 'n/a')[:16].replace('T', ' ')}{late}")
            status = "failed" if ex["failed"] and not card["extracted"] else card["status"]
            head[3].markdown(f"Status: **{status}**")
            head[4].button("↻", key=f"rerun_{code}", help="Re-extract this file", on_click=_open_dialog, args=("vendor", code, card["name"]))

            if card["extracted"]:
                st.progress(card["lines_quoted"] / card["lines_total"],
                            text=f"{card['lines_quoted']} of {card['lines_total']} lines quoted"
                                 + (f" · {card['lines_not_quoted']} not quoted" if card["lines_not_quoted"] else "")
                                 + (f" · {card['lines_unmatched']} unmatched" if card["lines_unmatched"] else ""))
                st.caption(f"Pipeline: {card['path']}")

            st.markdown("**VENDOR RESPONSE**")
            if ex["main_file"]:
                r = st.columns([0.4, 4, 2, 1.5], vertical_alignment="center")
                r[0].markdown(ICON.get(card["format"], "📄"))
                r[1].write(ex["main_file"])
                r[2].caption(ROLE_LABEL.get(ex["main_role"], ex["main_role"] or ""))
                if r[3].button("Preview ↗", key=f"pv_main_{code}"):
                    preview_dialog(ex["main_path"], ex["main_file"])
            else:
                st.caption("Files: " + ", ".join(card["files"]) + " (not extracted yet)")

            qv = questionnaire_view(conn, vid, ex["main_file"])
            wants = bool(card["clarification_draft"]) and any(w in card["clarification_draft"].lower() for w in ("questionnaire", "questions"))
            _questionnaire(card, qv, wants)
            _clarification(conn, card)

            b = st.columns([5, 1.6])
            b[1].button("Review extracted values →", type="primary", key=f"goto_review_{code}", width="stretch",
                        on_click=_goto_review, args=(code,))
