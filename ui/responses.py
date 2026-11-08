"""Responses tab: one card per vendor with the main reply, the questionnaire (with linked evidence) and the clarification email."""
from __future__ import annotations

import streamlit as st

from core import jobs
from core.queries import vendor_cards
from core.responses_view import (FORMAT_LABEL, QUAL_CHIP, SIZE_LABEL, card_extras, files_covered, last_extracted,
                                 questionnaire_view, role_label, send_clarification)
from core.stage import fmt_date
from extract.extract import load_manifest
from ui.preview import preview_dialog
from ui.style import inject

COLS = [2.6, 0.5, 4.3, 2.6]          # question / mark / answer / evidence = 26% / 5% / 43% / 26%
ICON = {"excel": "📊", "pdf": "📄", "word": "📝", "photo": "🖼️", "email_text": "✉️"}
RESULT = {"pass": "✅", "fail": "❌"}


def _tier(msme: str | None) -> str:
    return f" ({SIZE_LABEL.get(msme or 'none', msme)})" if msme else ""


def _when(ts: str | None) -> str:
    if not ts:
        return "n/a"
    return f"{fmt_date(ts)}, {ts[11:16]}" if len(ts) >= 16 else fmt_date(ts)


# ------------------------------------------------------------------ dialogs (state lives in session_state, cleared on dismiss)
def _open_dialog(*spec) -> None:
    st.session_state["_responses_dialog"] = spec


def _close_dialog() -> None:
    st.session_state.pop("_responses_dialog", None)


def _reextract_all_body(conn, entries: list[dict]) -> None:
    st.write("This re-extracts every file and replaces all current extracted values. Corrections and confirmations made in Review & Reconcile will be lost.")
    st.caption("It runs in the background: you can close this dialog and keep working. Each card shows its status.")
    c = st.columns([2.2, 1, 1.4])
    if c[1].button("Cancel", type="primary", key="rx_all_cancel", width="stretch"):
        _close_dialog()
        st.rerun()
    if c[2].button("Re-extract all", key="rx_all_go", width="stretch"):
        jobs.start(conn, entries)          # status = extracting is saved before the thread starts
        _close_dialog()
        st.rerun()


def _reextract_vendor_body(conn, entry: dict | None, code: str) -> None:
    st.write(f"This re-extracts {code}'s files and replaces {code}'s current extracted values. Corrections and confirmations made for {code} "
             "in Review & Reconcile will be lost. Other vendors are not affected.")
    if entry is None:
        st.error(f"No inbox entry found for {code}.")
    else:
        st.write(files_covered(conn, entry))
    st.caption("It runs in the background: you can close this dialog and keep working.")
    c = st.columns([2.2, 1, 1.4])
    if c[1].button("Cancel", type="primary", key=f"rx_{code}_cancel", width="stretch"):
        _close_dialog()
        st.rerun()
    if c[2].button(f"Re-extract {code}", key=f"rx_{code}_go", disabled=entry is None, width="stretch"):
        jobs.start(conn, [entry])
        _close_dialog()
        st.rerun()


def _show_open_dialog(conn, entries: dict, n_vendors: int) -> None:
    spec = st.session_state.get("_responses_dialog")
    if not spec:
        return
    if spec[0] == "all":
        st.dialog(f"Re-extract all {n_vendors} responses?", on_dismiss=_close_dialog)(_reextract_all_body)(conn, list(entries.values()))
    elif spec[0] == "vendor":
        code, name = spec[1], spec[2]
        st.dialog(f"Re-extract {code} · {name}?", on_dismiss=_close_dialog)(_reextract_vendor_body)(conn, entries.get(code), code)


def _goto_review(code: str) -> None:
    """on_click callback: runs before the page redraws, so the Review filter and the target tab are already set when the
    tabs render. Switching tabs from code only works because the tab bar is created with on_change="rerun" (see app.py)."""
    st.session_state["rv_vendor"] = code
    st.session_state["main_tab"] = "Review & Reconcile"


# ------------------------------------------------------------------ status chip (only this fragment polls, and only while a job runs)
def _chip_body(conn, code: str, failed: bool, status: str, tip: str | None, was_running: bool) -> None:
    running = jobs.is_running(conn, code)
    if was_running and not running:
        st.rerun()                      # the job finished: redraw the whole card with the new status
    job = jobs.status_of(conn, code)
    if running:
        st.markdown("Status: :orange-badge[Extracting…]", help="Running in the background. You can close dialogs or switch tabs; it keeps going.")
    elif job is not None and job["status"] == "failed":
        st.markdown("Status: :red-badge[failed]", help=job["error"] or "Re-extraction failed.")
    elif failed:
        st.markdown("Status: :red-badge[failed]")
    else:
        st.markdown(f"Status: **{status}**", help=tip)


def _status_chip(conn, code: str, failed: bool, status: str, tip: str | None) -> None:
    running = jobs.is_running(conn, code)
    st.fragment(run_every=3 if running else None)(_chip_body)(conn, code, failed, status, tip, running)


# ------------------------------------------------------------------ card parts
def _evidence_cell(card: dict, q: dict) -> None:
    code = card["vendor_code"]
    ev = q["evidence"]
    if ev:
        if st.button(f"{ev['file_name']} ↗", key=f"pv_ev_{code}_{q['q_code']}", type="tertiary"):
            preview_dialog(ev["file_path"], ev["file_name"], f"{code} · {card['name']} · {ev['facts']}")
        st.caption(f":red[{ev['facts']}]" if ev["expired"] else ev["facts"])
    if q["ref"]:
        st.caption(f"Source: {q['ref'].replace('Vendor Questionnaire', 'Questionnaire')}")
    if not ev and not q["ref"]:
        st.caption(" ")


def _questionnaire(card: dict, qv: dict, draft_has_request: bool) -> None:
    code = card["vendor_code"]
    label, colour = QUAL_CHIP.get(qv["qual_status"], ("Pending", "orange"))
    if not qv["has_answers"]:
        summary = "No questionnaire response"
    else:
        parts = [f"{qv['met']} met", f"{qv['not_met']} not met"]
        if qv["to_confirm"]:
            parts.append(f"{qv['to_confirm']} to confirm")
        if qv["not_answered"]:
            parts.append(f"{qv['not_answered']} not answered")
        summary = " · ".join(parts)
        if qv["evidence_n"]:
            summary += f" · 📎 {qv['evidence_n']} evidence doc{'s' if qv['evidence_n'] != 1 else ''}"
    with st.expander(f"**Questionnaire ({len(qv['rows'])})**  {summary}  :{colour}-badge[{label}]", expanded=False):
        if not qv["has_answers"]:
            sent = "only a rate card" if card["main_role"] == "quote" else "no questionnaire answers"
            extra = " A request for the answers is in the clarification draft below." if draft_has_request else ""
            st.write(f"The vendor sent {sent}. All {len(qv['rows'])} answers are missing, including the {qv['ko_total']} knock-outs, "
                     f"so the vendor stays **{label}**.{extra}")
        else:
            h = st.columns(COLS)
            for c, t in zip(h, ("Question", "", "Vendor's answer", "Evidence · source")):
                c.caption(t)
            with st.container(key=f"qtable_{code}"):
                for q in qv["rows"]:
                    c = st.columns(COLS, vertical_alignment="center")
                    short = q["q_code"].replace("_", " · ", 1)           # 'Q01_ISO9001' -> 'Q01 · ISO9001'
                    c[0].markdown(f"**{short}**" + (" :red-badge[Knock-out]" if q["is_knockout"] else ""), help=q["text"])
                    c[1].markdown(RESULT.get(q["result"], "⏳"))
                    c[2].write(q["answer"] if q["answered"] else "Not answered")
                    with c[3]:
                        _evidence_cell(card, q)
                    st.divider()
        if qv["other"]:
            st.markdown("**Other attachments** (answer no question)")
            for i, o in enumerate(qv["other"]):
                c = st.columns([6, 1.5])
                c[0].write(f"📎 {o['file_name']}")
                if c[1].button("Preview ↗", key=f"pv_oth_{code}_{i}", type="tertiary"):
                    preview_dialog(o["file_path"], o["file_name"], f"{code} · {card['name']} · Attachment")
        else:
            st.caption("Other attachments: none. Every file this vendor sent answers a question above.")


def _clarification(conn, card: dict) -> None:
    code = card["vendor_code"]
    draft = card["clarification_draft"]
    if not draft:
        return
    sent = card["clarification_status"] == "sent_stub"
    label = (f"Clarification sent · {fmt_date(card['clarification_sent_at'])}" if sent
             else f"Draft clarification email ({card['clarification_status']}; sending is stubbed)")
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
    inject()
    top = st.columns([1.2, 5], vertical_alignment="center")
    top[0].button("↻ Extract all", key="extract_all", width="stretch", on_click=_open_dialog, args=("all",),
                  disabled=jobs.any_running(conn))
    top[1].caption("Values come from real AI extraction (cached by file hash, prompt version and model). Use ↻ on a card to re-extract one vendor.")
    _show_open_dialog(conn, entries, len(cards))

    for card in cards:
        code = card["vendor_code"]
        vid = conn.execute("SELECT vendor_id FROM vendors WHERE vendor_code=?", (code,)).fetchone()[0]
        card = {**card, **card_extras(conn, vid)}
        failed = card["failed"] and not card["extracted"]
        fmt = card["format"] or card["main_format"]
        with st.container(key=f"rr_card_{code}"):
            head = st.columns([2.6, 1.4, 2.5, 2.5, 0.6, 0.3], vertical_alignment="center")
            head[0].markdown(f"**{code} · {card['name']}**{_tier(card['msme'])}")
            head[1].markdown(f"Format: **{FORMAT_LABEL.get(fmt, fmt or 'n/a')}**")
            head[2].markdown(f"Received: {_when(card['received_at'])}" + (" :red-badge[LATE]" if card["late"] else ""))
            with head[3]:
                _status_chip(conn, code, failed, card["status"], last_extracted(conn, code, card["main_path"]))
            head[4].button("↻", key=f"rerun_{code}", help="Re-extract this vendor's files", on_click=_open_dialog,
                           args=("vendor", code, card["name"]), disabled=jobs.is_running(conn, code))

            if card["extracted"]:
                st.progress(card["lines_quoted"] / card["lines_total"],
                            text=f"{card['lines_quoted']} of {card['lines_total']} lines quoted"
                                 + (f" · {card['lines_not_quoted']} not quoted" if card["lines_not_quoted"] else "")
                                 + (f" · {card['lines_unmatched']} unmatched" if card["lines_unmatched"] else ""))
            elif failed:
                st.progress(0.0, text=":red[File couldn't be read. No lines extracted.]")
            pipeline = card["path"] or card["main_model"]
            if pipeline:
                st.caption(f"Pipeline: {pipeline}")

            st.caption("VENDOR RESPONSE")
            if card["main_file"]:
                with st.container(key=f"mainrow_{code}"):
                    r = st.columns([0.4, 4, 2.6, 1.3], vertical_alignment="center")
                    r[0].markdown(ICON.get(fmt, "📄"))
                    r[1].write(card["main_file"])
                    r[2].caption(role_label(card["main_role"], fmt))
                    if r[3].button("Preview ↗", key=f"pv_main_{code}", type="tertiary"):
                        preview_dialog(card["main_path"], card["main_file"],
                                       f"{code} · {card['name']} · {role_label(card['main_role'], fmt)} · received {_when(card['received_at'])}")
            else:
                st.caption("Files: " + ", ".join(card["files"]) + " (not extracted yet)")

            qv = questionnaire_view(conn, vid, card["main_file"])
            wants = bool(card["clarification_draft"]) and any(w in card["clarification_draft"].lower() for w in ("questionnaire", "questions"))
            _questionnaire(card, qv, wants)
            _clarification(conn, card)

            b = st.columns([5, 1.7])
            b[1].button("Review extracted values →", type="primary", key=f"goto_review_{code}", width="stretch",
                        on_click=_goto_review, args=(code,))
