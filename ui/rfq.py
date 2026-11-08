"""Create RFQ tab. Two modes: the published Demo RFQ (read-only, with the old editor behind "Edit RFQ") and a separate New draft
built with the co-pilot. The draft lives in its own table and can never change the demo RFQ or its replies."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from copilot import draft_copilot as DC
from core import rfq_draft as R
from core.stage import fmt_date
from export.rfq_xlsx import build_rfq_xlsx
from ui import rfq_edit
from ui.preview import _body as preview_body
from ui.style import inject

MODES = ["Demo RFQ", "New draft"]
NOTE_DEMO = ("Demo RFQ. The 5 vendor replies in this demo were written for it. A new draft is kept separate and never changes this RFQ or its replies.")
PILL_DEMO = ["Start a new draft", "What did we ask suppliers?", "Who was invited?"]


# ------------------------------------------------------------------ small helpers
def _chip(label: str, colour: str) -> str:
    return f":{colour}-badge[{label}]"


def _label(n: int | None, title: str, chip: tuple[str, str], summary: str) -> str:
    return f"**{f'{n} · ' if n else ''}{title}**  {_chip(*chip)}  :gray[{summary}]" if summary else f"**{f'{n} · ' if n else ''}{title}**  {_chip(*chip)}"


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.day} {d:%b %Y}, {d:%H:%M}"


def _greeting() -> str:
    name = os.environ.get("BUYER_NAME", "").strip()
    return (f"Hi {name}. " if name else "Hi. ") + ("What are we sourcing? You can describe it in a sentence, paste SKU codes from the item master, "
                                                   "or copy last year's RFQ for the same category.")


def _set_dialog(*spec) -> None:
    st.session_state["_rfq_dialog"] = spec


def _close_dialog() -> None:
    st.session_state.pop("_rfq_dialog", None)
    st.session_state.pop("_rfq_email_view", None)


def _lines_df(d: dict) -> pd.DataFrame:
    return pd.DataFrame([{"#": i, "SKU": ln["sku"], "Description": ln["description"],
                          "Size (mm)": f"{ln['length_mm']}×{ln['width_mm']}×{ln['height_mm']}", "Ply": ln["ply"], "BS kg/cm²": ln["bs"],
                          "Annual qty": ln["annual_qty"], "Typical order": ln["typical_order_qty"]} for i, ln in enumerate(d["lines"], 1)])


def _questions_df(d: dict) -> pd.DataFrame:
    return pd.DataFrame([{"#": q["q_code"].split("_")[0], "Question": q["text"], "Answer type": R.ANSWER_LABEL.get(q["answer_type"], q["answer_type"]),
                          "Pass rule": R.pass_rule_text(q), "Knock-out": "Yes" if q["is_knockout"] else "No",
                          "Document required": "Yes" if q["doc_required"] else "No"} for q in d["questions"]])


def _suppliers_df(conn, d: dict) -> pd.DataFrame:
    return pd.DataFrame([{"Code": s["code"], "Supplier": s["name"], "Location": s["location"], "MSME": s["msme"], "Approved": "✅",
                          "Contact": s["contact"]} for s in R.approved_suppliers(conn) if s["code"] in d["suppliers"]])


# ------------------------------------------------------------------ dialogs
def _set_email_view(view: str) -> None:
    st.session_state["_rfq_email_view"] = view


def _email_body(conn, d: dict) -> None:
    """One dialog with two views (email / attachment preview): Streamlit keeps an open dialog, so the content is swapped inside it."""
    if st.session_state.get("_rfq_email_view") == "xlsx":
        _xlsx_body(d, True)
        return
    names = {s["code"]: s["name"] for s in R.approved_suppliers(conn)}
    to = ", ".join(f"{names[c]} ({c})" for c in d["suppliers"] if c in names)
    with st.container(border=True):
        st.markdown(f"**To**  {to} · :gray[one email each]  \n**Subject**  **{R.subject(d)}**")
        st.divider()
        st.text(R.cover_note(d))
        with st.container(border=True):
            c = st.columns([5, 1.2], vertical_alignment="center")
            c[0].markdown(f"📎 **{R.XLSX_NAME}**  :gray[3 sheets: Terms · Line items · Questionnaire]")
            c[1].button("Preview ↗", key="pv_rfq_xlsx", type="tertiary", on_click=_set_email_view, args=("xlsx",))
    b = st.columns([3, 1.1, 2.2])
    b[1].button("Back to edit", key="rfq_email_back", width="stretch", on_click=_close_dialog)
    block, _ = R.blocking_and_warnings(d)
    if b[2].button(f"Publish & send to {len(d['suppliers'])} suppliers", type="primary", key="rfq_publish_go", width="stretch", disabled=bool(block)):
        R.publish(conn, d)
        st.session_state["_rfq_just_published"] = True
        _close_dialog()
        st.rerun()


def _xlsx_body(d: dict, from_email: bool) -> None:
    data = build_rfq_xlsx(d)
    p = Path(tempfile.gettempdir()) / "rfx_rfq_preview"
    p.mkdir(exist_ok=True)
    f = p / R.XLSX_NAME
    f.write_bytes(data)
    preview_body(str(f), R.XLSX_NAME, f"{d['overview']['title']} · attachment to the supplier email")
    if from_email:
        st.button("← Back to email", key="rfq_xlsx_back", on_click=_set_email_view, args=("email",))


def _paste_body(conn, d: dict) -> None:
    txt = st.text_area("SKU codes", placeholder="CB-3P-SH-01, CB-3P-SH-02, ...", height=140, key="paste_text")
    st.caption("Each code is looked up in the item catalog. An unknown code is not added; it is shown with the closest match.")
    c = st.columns([3, 1, 1.4])
    c[1].button("Cancel", key="paste_cancel", on_click=_close_dialog, width="stretch")
    if c[2].button("Add to lines", type="primary", key="paste_go", width="stretch", disabled=not txt.strip()):
        notes = R.add_skus(conn, d, R.split_codes(txt))
        d["active"] = "lines"
        _say(d, "Added from your pasted SKUs.", notes)
        _close_dialog()
        st.session_state["rfq_ver"] = st.session_state.get("rfq_ver", 0) + 1
        st.rerun()


def _describe_body() -> None:
    txt = st.text_input("Describe the box", placeholder="3-ply shipper for 180 ml shampoo bottles, about 300×200×150", key="describe_text")
    st.caption("The co-pilot suggests matching catalog SKUs. It adds nothing until you say so.")
    c = st.columns([3, 1, 1.4])
    c[1].button("Cancel", key="describe_cancel", on_click=_close_dialog, width="stretch")
    if c[2].button("Ask co-pilot", type="primary", key="describe_go", width="stretch", disabled=not txt.strip()):
        st.session_state["_rfq_pending_msg"] = f"Suggest catalog SKUs for this box: {txt.strip()}"
        _close_dialog()
        st.rerun()


def _load_demo_body(conn) -> None:
    st.write("This fills the draft with the demo RFQ: 30 lines, 8 questions, terms and 5 suppliers. Your current draft will be lost.")
    c = st.columns([2.2, 1, 1.4])
    c[1].button("Cancel", type="primary", key="loaddemo_cancel", on_click=_close_dialog, width="stretch")
    if c[2].button("Replace draft", key="loaddemo_go", width="stretch"):
        _load_demo(conn)
        _close_dialog()
        st.rerun()


def _show_dialog(conn, d: dict) -> None:
    spec = st.session_state.get("_rfq_dialog")
    if not spec:
        return
    k = spec[0]
    if k == "email":
        st.dialog("Email to suppliers", width="large", on_dismiss=_close_dialog)(_email_body)(conn, d)
    elif k == "xlsx":
        st.dialog(R.XLSX_NAME, width="large", on_dismiss=_close_dialog)(_xlsx_body)(d, len(spec) > 1)
    elif k == "paste":
        st.dialog("Paste SKUs", on_dismiss=_close_dialog)(_paste_body)(conn, d)
    elif k == "describe":
        st.dialog("Describe a box", on_dismiss=_close_dialog)(_describe_body)()
    elif k == "load_demo":
        st.dialog("Replace your current draft?", on_dismiss=_close_dialog)(_load_demo_body)(conn)


# ------------------------------------------------------------------ draft state
def _state(conn) -> dict:
    if "rfq_d" not in st.session_state:
        d, ts = R.load(conn)
        st.session_state["rfq_d"] = d
        st.session_state["rfq_saved_at"] = ts
        st.session_state["rfq_ver"] = 0
        st.session_state["rfq_chat"] = [{"role": "assistant", "text": _greeting()}]
        st.session_state["rfq_sugg"] = DC.next_steps(d)
    return st.session_state["rfq_d"]


def _autosave(conn, d: dict) -> None:
    if R.is_empty(d):
        return
    st.session_state["rfq_saved_at"] = R.save(conn, d)


def _bump() -> None:
    st.session_state["rfq_ver"] = st.session_state.get("rfq_ver", 0) + 1


def _say(d: dict, text: str, notes: list[str] | None = None, sugg: list[str] | None = None) -> None:
    st.session_state["rfq_chat"].append({"role": "assistant", "text": text, "notes": notes or []})
    st.session_state["rfq_sugg"] = sugg or DC.next_steps(d)


def _load_demo(conn) -> None:
    d = R.from_demo(conn)
    d["ai_fields"] = []
    st.session_state["rfq_d"] = d
    st.session_state["rfq_chat"] = [{"role": "assistant", "text": _greeting()},
                                    {"role": "assistant", "text": "I filled the draft with the demo RFQ: 30 lines, 8 questions, terms and 5 suppliers. Change anything you like."}]
    st.session_state["rfq_sugg"] = DC.next_steps(d)
    _bump()


def _start_new(conn) -> None:
    d = R.empty_draft()
    R.discard(conn)
    st.session_state["rfq_d"] = d
    st.session_state["rfq_saved_at"] = None
    st.session_state["rfq_chat"] = [{"role": "assistant", "text": _greeting()}]
    st.session_state["rfq_sugg"] = DC.next_steps(d)
    st.session_state["rfq_mode"] = "New draft"
    _bump()


def _discard(conn) -> None:
    R.discard(conn)
    st.session_state.pop("rfq_d", None)          # reloaded from the DB (empty, or the last published draft) on the next run


def _ask_load_demo(conn) -> None:
    d = st.session_state.get("rfq_d")
    if d is not None and not R.is_empty(d) and d["status"] != "published":
        _set_dialog("load_demo")
    else:
        _load_demo(conn)


# ------------------------------------------------------------------ co-pilot panel
def _direct(conn, d: dict, pill: str) -> bool:
    """Pills that map to one plain action run without the model. Returns True when handled."""
    low = pill.lower()
    op = None
    if "last year" in low and ("add" in low or "copy" in low):
        op = DC.DraftOp(op="add_last_year")
    elif "standard questionnaire" in low or "standard template" in low:
        op = DC.DraftOp(op="load_questionnaire_template")
    elif "all approved" in low and ("invite" in low or "select" in low):
        op = DC.DraftOp(op="select_suppliers", values=["all"])
    if pill == "Review & publish":
        d["active"] = "review"
        _say(d, "Here is the checklist. Fix anything in red before publishing.")
        return True
    if pill == "Paste SKU codes":
        _set_dialog("paste")
        return True
    if pill == "Describe what you're sourcing":
        _say(d, "Tell me in one sentence, for example “Annual rate contract for corrugated boxes, Chakan plant, FY27, in INR”.")
        return True
    if op is not None:
        new, ok, no = DC.apply_ops(conn, d, [op])
        d.update(new)
        _say(d, "Done." if ok else "I couldn't do that.", ok + no)
        _bump()
        return True
    return False


def _send(conn, d: dict, msg: str) -> None:
    hist = [(m["role"], m["text"]) for m in st.session_state["rfq_chat"]]
    st.session_state["rfq_chat"].append({"role": "user", "text": msg})
    try:
        with st.spinner("Co-pilot is working ..."):
            reply, new, ok, no, sugg = DC.ask(conn, d, msg, hist)
        d.update(new)
        notes = [f"✓ {n}" for n in ok] + [f"✗ {n}" for n in no]
        _say(d, reply, notes, sugg)
        _bump()
    except Exception as e:  # show the real error, never swallow it
        st.session_state["rfq_chat"].append({"role": "assistant", "text": f"Sorry, the co-pilot failed: {e}"})


def _copilot_panel(conn, d: dict, key: str) -> None:
    with st.container(border=True):
        h = st.columns([1, 1.6], vertical_alignment="center")
        h[0].markdown("**Co-pilot**")
        h[1].caption("drafts from the item catalog")
        box = st.container(height=300, border=False)
        with box:
            for m in st.session_state["rfq_chat"]:
                with st.chat_message(m["role"]):
                    st.markdown(m["text"])
                    for n in m.get("notes", []):
                        st.caption(n)
        n = st.session_state.get("rfq_pill_n", 0)
        pick = st.pills("Next steps", st.session_state.get("rfq_sugg", []), selection_mode="single", key=f"rfq_pills_{n}",
                        label_visibility="collapsed")
        msg = st.chat_input("Ask the co-pilot...", key=key)
    pending = st.session_state.pop("_rfq_pending_msg", None)
    if pick:
        st.session_state["rfq_pill_n"] = n + 1
        if not _direct(conn, d, pick):
            _send(conn, d, pick)
        st.rerun()
    if msg or pending:
        _send(conn, d, msg or pending)
        st.rerun()


# ------------------------------------------------------------------ read-only view (demo RFQ, or a published draft)
def _readonly(conn, d: dict, published_at: str | None, key: str, sent_n: int | None = None) -> None:
    pub = ("Published", "blue")
    sums = {s: R.section_summary(d, s) for s in R.SECTIONS}
    n = sent_n if sent_n is not None else len(d["suppliers"])
    for i, s in enumerate(R.SECTIONS[:5], 1):
        with st.expander(_label(None, R.SECTION_TITLE[s], pub, sums[s]), expanded=False):
            if s == "overview":
                cols = st.columns(3)
                for j, (k, lab) in enumerate(R.OVERVIEW_FIELDS.items()):
                    cols[j % 3].text_input(lab, d["overview"][k], disabled=True, key=f"{key}_ov_{k}")
            elif s == "lines":
                st.dataframe(_lines_df(d), hide_index=True, width="stretch", height=min(420, 40 + 35 * len(d["lines"])))
            elif s == "questions":
                st.dataframe(_questions_df(d), hide_index=True, width="stretch")
            elif s == "terms":
                cols = st.columns(3)
                for j, (k, lab) in enumerate(R.TERM_FIELDS.items()):
                    v = d["terms"].get(k)
                    v = fmt_date(v) if k in ("response_due", "award_ref") else (f"{v:g}% extra" if k == "gst_pct" and v is not None else v)
                    cols[j % 3].text_input(lab, "" if v is None else str(v), disabled=True, key=f"{key}_t_{k}")
            elif s == "suppliers":
                st.dataframe(_suppliers_df(conn, d), hide_index=True, width="stretch")
                st.caption("Micro and small vendors' credit is capped at 45 days in evaluation (MSMED Act).")
    when = f"{_when(published_at)} · " if published_at else ""
    with st.expander(_label(None, "Sent email", pub, f"{when}To {n} suppliers · 📎 {R.XLSX_NAME}"), expanded=bool(st.session_state.pop("_rfq_open_sent", False))):
        st.markdown(f"**Subject**  {R.subject(d)}")
        st.text(R.cover_note(d))
        c = st.columns([5, 1.4], vertical_alignment="center")
        c[0].markdown(f"📎 **{R.XLSX_NAME}**  :gray[3 sheets: Terms · Line items · Questionnaire]")
        if c[1].button("Preview ↗", key=f"pv_rfq_sent_{key}", type="tertiary"):
            _set_dialog("xlsx")
            st.rerun()


# ------------------------------------------------------------------ demo mode
def _demo_chat(conn) -> None:
    st.session_state.setdefault("demo_chat", [{"role": "assistant", "text": "This RFQ was published on 7 Nov and all 5 suppliers have replied. Ask me about it, or start a new draft to see how I build an RFQ from scratch."}])
    with st.container(border=True):
        h = st.columns([1, 1.6], vertical_alignment="center")
        h[0].markdown("**Co-pilot**")
        box = st.container(height=300, border=False)
        with box:
            for m in st.session_state["demo_chat"]:
                with st.chat_message(m["role"]):
                    st.markdown(m["text"])
        pick = st.pills("Suggestions", PILL_DEMO, selection_mode="single", key=f"demo_pills_{st.session_state.get('demo_pill_n', 0)}", label_visibility="collapsed")
        msg = st.chat_input("Ask the co-pilot...", key="demo_chat_input")
    q = pick or msg
    if pick:
        st.session_state["demo_pill_n"] = st.session_state.get("demo_pill_n", 0) + 1
    if pick == "Start a new draft":
        _start_new(conn)
        st.rerun()
    if q:
        st.session_state["demo_chat"].append({"role": "user", "text": q})
        try:
            if q == PILL_DEMO[1]:
                st.session_state["demo_chat"].append({"role": "assistant", "text": DC.demo_facts(conn, "asked")})
            elif q == PILL_DEMO[2]:
                st.session_state["demo_chat"].append({"role": "assistant", "text": DC.demo_facts(conn, "invited")})
            else:
                with st.spinner("Co-pilot is reading the RFQ ..."):
                    st.session_state["demo_chat"].append({"role": "assistant", "text": DC.answer_about_demo(conn, q)})
        except Exception as e:
            st.session_state["demo_chat"].append({"role": "assistant", "text": f"Sorry, the co-pilot failed: {e}"})
        st.rerun()


def _blocked_note(conn) -> None:
    rows = conn.execute("""SELECT l.line_no, COUNT(DISTINCT q.vendor_id) n FROM line_items l JOIN quotes q USING (line_id)
                           WHERE q.is_current=1 AND q.quote_status='quoted' GROUP BY l.line_no ORDER BY n DESC, l.line_no LIMIT 1""").fetchone()
    if rows:
        st.warning(f"Edit safeguards: lines and questions that vendors have already quoted or answered cannot be removed (for example line {rows[0]} "
                   f"was quoted by {rows[1]} vendors). Quantity, term and rule changes are re-checked against the replies. The co-pilot explains why when you ask.")


def _demo(conn) -> None:
    d = R.from_demo(conn)
    v = R.demo_view(conn)
    sent_n = len(d["suppliers"])
    editing = bool(st.session_state.get("rfq_edit"))
    top = st.columns([3.4, 4, 1.1, 1.5], vertical_alignment="center")
    _mode_control(top[0])
    top[1].caption(f"Published {_when(v['issued_at'])} · sent to {sent_n} suppliers · {'editing' if editing else 'read-only'}")
    if editing:
        top[2].button("← Done", key="rfq_edit_done", width="stretch", on_click=lambda: st.session_state.update(rfq_edit=False))
    else:
        top[2].button("✎ Edit RFQ", key="rfq_edit_btn", width="stretch", on_click=lambda: st.session_state.update(rfq_edit=True))
    top[3].button("+ Start new draft", type="primary", key="rfq_start_new", width="stretch", on_click=_start_new, args=(conn,))
    if editing:
        _blocked_note(conn)
        rfq_edit.render(conn)
        return
    st.info(NOTE_DEMO, icon="ℹ️")
    left, right = st.columns([2.6, 1])
    with left:
        _readonly(conn, d, v["issued_at"], "demo")
    with right:
        _demo_chat(conn)
    _show_dialog(conn, d)


# ------------------------------------------------------------------ draft mode
def _mode_control(col) -> None:
    col.segmented_control("Mode", MODES, default="Demo RFQ", key="rfq_mode", label_visibility="collapsed")


def _field_box(d: dict, ai_key: str):
    return st.container(key=f"ai_{ai_key.replace('.', '_')}") if ai_key in d["ai_fields"] else st.container()


def _sec_overview(conn, d: dict, ver: int) -> None:
    cols = st.columns(3)
    for j, (k, lab) in enumerate(R.OVERVIEW_FIELDS.items()):
        with cols[j % 3]:
            with _field_box(d, f"overview.{k}"):
                v = st.text_input(lab, d["overview"][k], key=f"ov_{k}_{ver}")
        if v != d["overview"][k]:
            d["overview"][k] = v
            d["ai_fields"] = [a for a in d["ai_fields"] if a != f"overview.{k}"]


def _sec_lines(conn, d: dict, ver: int) -> None:
    if d["lines"]:
        df = _lines_df(d)
        df.insert(0, "Remove", False)
        ed = st.data_editor(df, key=f"lines_{ver}", hide_index=True, width="stretch", height=min(430, 40 + 35 * len(df)),
                            disabled=["#", "SKU", "Description", "Size (mm)", "Ply", "BS kg/cm²"],
                            column_config={"Remove": st.column_config.CheckboxColumn("Remove", width="small"),
                                           "Annual qty": st.column_config.NumberColumn(min_value=0, step=100, format="localized"),
                                           "Typical order": st.column_config.NumberColumn(min_value=0, step=100, format="localized")})
        gone = [d["lines"][i]["sku"] for i in ed.index[ed["Remove"]]]
        for i, ln in enumerate(d["lines"]):
            ln["annual_qty"] = int(ed.loc[i, "Annual qty"] or 0)
            ln["typical_order_qty"] = int(ed.loc[i, "Typical order"] or 0)
        if gone:
            d["lines"] = [ln for ln in d["lines"] if ln["sku"] not in gone]
            _bump()
            st.rerun()
    for u in list(d["unknown"]):
        c = st.columns([8, 1.6], vertical_alignment="center")
        c[0].error(f"**{u['code']}** · Not in the catalog. Not added." + (f" Did you mean {u['closest']}?" if u["closest"] else ""), icon="❌")
        if c[1].button("Dismiss", key=f"unk_{u['code']}_{ver}"):
            d["unknown"].remove(u)
            _bump()
            st.rerun()
    a = st.columns([1.5, 2, 1.8, 2.2])
    a[0].button("+ Paste SKUs", key="add_paste", on_click=_set_dialog, args=("paste",), width="stretch")
    if a[1].button("+ Same as last year", key="add_ly", width="stretch"):
        notes = R.add_last_year(conn, d)
        d["active"] = "lines"
        _say(d, "Added last year's lines.", notes)
        _bump()
        st.rerun()
    a[2].button("+ Describe a box", key="add_desc", on_click=_set_dialog, args=("describe",), width="stretch")


def _sec_questions(conn, d: dict, ver: int) -> None:
    if not d["questions"]:
        st.caption("Load the standard corrugated quality questionnaire (8 questions), then adjust the knock-outs and documents.")
        if st.button("Load standard template", key="load_tpl", type="primary"):
            R.load_template(conn, d)
            d["active"] = "questions"
            _say(d, "Loaded the standard corrugated questionnaire.")
            _bump()
            st.rerun()
        return
    w = [0.7, 4, 1.5, 1.9, 1.1, 1.5]
    for c, t in zip(st.columns(w), ("#", "Question", "Answer type", "Pass rule", "Knock-out", "Document required")):
        c.caption(t)
    for q in d["questions"]:
        c = st.columns(w, vertical_alignment="center")
        c[0].markdown(q["q_code"].split("_")[0])
        c[1].markdown(q["text"])
        c[2].markdown(R.ANSWER_LABEL.get(q["answer_type"], q["answer_type"]))
        c[3].markdown(R.pass_rule_text(q))
        q["is_knockout"] = c[4].toggle("Knock-out", q["is_knockout"], key=f"q_k_{q['q_code']}_{ver}", label_visibility="collapsed")
        q["doc_required"] = c[5].checkbox("Document required", q["doc_required"], key=f"q_d_{q['q_code']}_{ver}", label_visibility="collapsed")
    f = st.columns([1.3, 6], vertical_alignment="center")
    if f[0].button("+ Add question", key="add_q"):
        n = len(d["questions"]) + 1
        d["questions"].append({"q_code": f"Q{n:02d}_CUSTOM", "text": "New question", "answer_type": "text", "rule_type": "any", "rule_params": {},
                               "is_knockout": False, "doc_required": False, "doc_type": None})
        _bump()
        st.rerun()
    f[1].caption("A failed knock-out disqualifies the vendor. A “Yes” without the required document counts as not met.")


def _sec_terms(conn, d: dict, ver: int) -> None:
    t = d["terms"]
    c = st.columns(3)

    def box(k):
        return _field_box(d, f"terms.{k}")

    with c[0]:
        with box("payment_days"):
            v = st.number_input("Payment terms (days)", 0, 365, t["payment_days"], key=f"t_pay_{ver}", placeholder="e.g. 60")
        t["payment_days"] = None if v is None else int(v)
        st.caption("From invoice. Vendors may propose different terms.")
        with box("response_due"):
            due = st.date_input("Response due", _d(t["response_due"]), key=f"t_due_{ver}", format="DD/MM/YYYY")
        t["response_due"] = due.isoformat() if due else ""
        if due:
            n = (due - date.today()).days
            st.caption(f"{n} days from today" if n >= 0 else f"{-n} days ago, in the past")
    with c[1]:
        with box("delivery_basis"):
            t["delivery_basis"] = st.text_input("Delivery basis", t["delivery_basis"], key=f"t_del_{ver}", placeholder="FOR Chakan (freight included)")
        with box("award_ref"):
            ar = st.date_input("Award reference date", _d(t["award_ref"]), key=f"t_ref_{ver}", format="DD/MM/YYYY")
        t["award_ref"] = ar.isoformat() if ar else ""
        st.caption("Certificates must be valid on this date")
    with c[2]:
        with box("gst_pct"):
            g = st.number_input("GST (% extra)", 0.0, 28.0, t["gst_pct"], key=f"t_gst_{ver}", placeholder="e.g. 18")
        t["gst_pct"] = None if g is None else float(g)
        st.caption("Prices quoted ex-GST")
        with box("price_validity"):
            t["price_validity"] = st.text_input("Price validity", t["price_validity"], key=f"t_val_{ver}", placeholder="6 months from PO")


def _d(s: str):
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def _sec_suppliers(conn, d: dict, ver: int) -> None:
    w = [0.6, 0.7, 3, 2, 1.1, 1.6, 3]
    for c, t in zip(st.columns(w), ("", "Code", "Supplier", "Location", "MSME", "Approved", "Contact")):
        c.caption(t)
    for s in R.approved_suppliers(conn):
        c = st.columns(w, vertical_alignment="center")
        on = c[0].checkbox(s["code"], s["code"] in d["suppliers"], key=f"sup_{s['code']}_{ver}", label_visibility="collapsed")
        if on and s["code"] not in d["suppliers"]:
            d["suppliers"].append(s["code"])
        if not on and s["code"] in d["suppliers"]:
            d["suppliers"].remove(s["code"])
        c[1].markdown(s["code"])
        c[2].markdown(s["name"])
        c[3].markdown(s["location"])
        c[4].markdown(s["msme"])
        c[5].markdown("✅")
        c[6].markdown(s["contact"])
    st.caption("From the approved vendor list for this category. Micro and small vendors' credit is capped at 45 days in evaluation (MSMED Act).")


def _sec_review(conn, d: dict, ver: int) -> None:
    if not R.sections_filled(d):
        st.caption("Unlocks when 1–5 are complete.")
        return
    block, warn = R.blocking_and_warnings(d)
    t = d["terms"]
    items = [("overview", "Overview", f"{sum(bool(str(v).strip()) for v in d['overview'].values())} of {len(d['overview'])} fields"),
             ("lines", "Line items", f"{len(d['lines'])} lines"),
             ("questions", "Questionnaire", R.section_summary(d, "questions")),
             ("terms", "Terms", f"{t['payment_days']} days · {t['delivery_basis'].split(' (')[0]} · GST {t['gst_pct']:g}% extra"),
             ("timeline", "Timeline", f"Due {fmt_date(t['response_due'])}"),
             ("suppliers", "Suppliers", f"{len(d['suppliers'])} approved vendors")]
    cols = st.columns(3)
    for i, (sec, title, text) in enumerate(items):
        src = "terms" if sec == "timeline" else sec
        b = [m for s_, m in block if s_ == src and sec != "terms"]
        w = [m for s_, m in warn if s_ == src]
        with cols[i % 3]:
            if b:
                st.error(f"**{title}**  \n{b[0]}", icon="❌")
            elif w:
                st.warning(f"**{title}**  \n{w[0]}", icon="⚠️")
            else:
                with st.container(border=True):
                    st.markdown(f"✅ **{title}**  \n{text}")
    n = len(block)
    bar = st.columns([3, 1.6, 2.2], vertical_alignment="center")
    if n:
        bar[0].caption(f"Fix {n} issue{'s' if n != 1 else ''} to publish")
    bar[1].button("Preview email to suppliers", key="rfq_preview_email", on_click=_set_dialog, args=("email",), disabled=bool(n), width="stretch")
    bar[2].button(f"Publish & send to {len(d['suppliers'])} suppliers", type="primary", key="rfq_publish", on_click=_set_dialog, args=("email",),
                  disabled=bool(n), width="stretch", help="Opens the email preview first; sending is a stub.")


SECTION_FN = {"overview": _sec_overview, "lines": _sec_lines, "questions": _sec_questions, "terms": _sec_terms, "suppliers": _sec_suppliers, "review": _sec_review}


def _draft(conn) -> None:
    d = _state(conn)
    ver = st.session_state.get("rfq_ver", 0)
    published = d["status"] == "published"
    top = st.columns([3.4, 4, 1.4, 1.6], vertical_alignment="center")
    _mode_control(top[0])
    if published:
        top[1].caption(f"Published {_when(d['published_at'])} · read-only")
        top[3].button("+ Start new draft", key="rfq_start_new2", width="stretch", on_click=_start_new, args=(conn,))
        st.success(f"✅ **Sent to {len(d['sent_to'])} suppliers (stub).** Each got the cover email with {R.XLSX_NAME}. Logged in the audit trail. "
                   "In this demo, the Responses tab keeps showing the replies to the demo RFQ.")
        left, right = st.columns([2.6, 1])
        with left:
            _readonly(conn, d, d["published_at"], "pub", sent_n=len(d["sent_to"]))
        with right:
            with st.container(border=True):
                st.markdown("**Co-pilot**")
                st.markdown(f"Published and sent to {len(d['sent_to'])} suppliers. I'll flag replies as they arrive in Responses.")
                p = st.pills("After publish", ["View sent email", "Start another draft"], selection_mode="single", key=f"pub_pills_{st.session_state.get('pub_pill_n', 0)}",
                             label_visibility="collapsed")
            if p:
                st.session_state["pub_pill_n"] = st.session_state.get("pub_pill_n", 0) + 1
                if p == "View sent email":
                    st.session_state["_rfq_open_sent"] = True
                else:
                    _start_new(conn)
                st.rerun()
        _show_dialog(conn, d)
        return

    status = top[1].empty()      # filled after the sections ran, so it shows the save of this very run
    top[2].button("Discard draft", key="rfq_discard_draft", width="stretch", on_click=_discard, args=(conn,))
    top[3].button("Load demo RFQ", key="rfq_load_demo", width="stretch", on_click=_ask_load_demo, args=(conn,))
    left, right = st.columns([2.6, 1])
    with left:
        for i, s in enumerate(R.SECTIONS, 1):
            label, colour = R.section_status(d, s)
            locked = s == "review" and not R.sections_filled(d)
            summary = "Unlocks when 1–5 are complete" if locked else R.section_summary(d, s)
            if s == "review" and not locked:
                block, warn = R.blocking_and_warnings(d)
                summary = f"{len(block)} blocking issue{'s' if len(block) != 1 else ''} · {len(warn)} warning{'s' if len(warn) != 1 else ''}"
            with st.expander(_label(i, R.SECTION_TITLE[s], (label, colour), summary), expanded=(d["active"] == s and not locked and not R.is_empty(d))):
                SECTION_FN[s](conn, d, ver)
        if R.is_empty(d):
            st.caption("Each section fills in as you talk to the co-pilot. You can also open any section and type directly.")
    with right:
        _copilot_panel(conn, d, "draft_chat_input")
    _autosave(conn, d)
    saved = st.session_state.get("rfq_saved_at")
    status.caption(f"Draft · auto-saved {saved[11:16]}" if saved else "New draft · not saved yet")
    _show_dialog(conn, d)


# ------------------------------------------------------------------ entry
def render(conn) -> None:
    inject()
    mode = st.session_state.get("rfq_mode") or "Demo RFQ"
    if mode == "New draft":
        _draft(conn)
    else:
        _demo(conn)
