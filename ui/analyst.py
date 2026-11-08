"""Analyst & Award tab: the chat (left) and the award panel (right: propose, review, confirm, notify).

The analyst can only PROPOSE. commit_award is called from the Confirm button's dialog and nowhere else. Read-only parts are HTML blocks styled by
ui/theme.py; buttons, chips and inputs are Streamlit widgets."""
from __future__ import annotations

import html
import io
import os
import re
import zipfile
from datetime import datetime

import pandas as pd
import streamlit as st

from agent import tools as T
from agent.analyst import ask
from core import audit
from core import bid_view as B
from core.config import load_config
from core.review_items import open_item_count
from export import memo as M
from export.excel import build_workbook

STARTERS = ["Cheapest per line, only among vendors who cleared quality?",
            "Why didn't V5 win line 1? It's the cheapest.",
            "Which unconfirmed values could change the winner?",
            "What if we ordered 20,000 per release instead?",
            "Show savings vs last year by vendor as a chart",
            "Exclude V3. What's the total and who's the backup per line?"]
STEPS = ["1 Propose", "2 Review", "3 Confirm", "4 Notify"]
CODE_LABEL = {"qual_fail": "Qualification", "spec": "Specification", "capacity": "Capacity", "validity": "Offer validity", "price_not_firm": "Price not firm",
              "buyer_judgment": "Buyer judgment"}


# ------------------------------------------------------------------ small helpers
def _limit() -> int:
    return int(load_config().get("chat", {}).get("max_messages", 30))


def _buyer() -> str:
    return os.environ.get("BUYER_NAME", "").strip() or "the buyer"


def _e(x) -> str:
    return html.escape(str(x))


def _md(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text or "", quote=False))


def _scenario_title(params: dict) -> str:
    p = params or {}
    if not p:
        return "Cheapest per line · qualified only"
    bits = []
    if p.get("include_pending"):
        bits.append("Include pending vendors")
    if p.get("exclude_vendors"):
        bits.append(f"Exclude {', '.join(p['exclude_vendors'])}")
    if p.get("include_accepted_substitutes"):
        bits.append("Accept substitutes")
    if p.get("order_qty_override"):
        bits.append(f"{int(p['order_qty_override']):,} per release")
    if p.get("basis") == "landed":
        bits.append("landed price")
    return " · ".join(bits) or "Cheapest per line · qualified only"


def _queue(q: str) -> None:
    st.session_state["aa_queue"] = q


def _set_dialog(*spec) -> None:
    st.session_state["_aa_dialog"] = spec


def _close_dialog() -> None:
    st.session_state.pop("_aa_dialog", None)


def _toggle_open(key: str) -> None:
    s = set(st.session_state.get("aa_open", set()))
    s ^= {key}
    st.session_state["aa_open"] = s


# ------------------------------------------------------------------ chat processing
def _badges(conn, scen: dict | None) -> list[tuple[str, str]]:
    """Risk badges taken from the data the answer used (the scenario totals), plus the unreviewed-item count."""
    if not scen:
        return []
    t = scen["totals"]
    out = []
    if t.get("lines_on_not_firm_prices"):
        out.append((f"{t['lines_on_not_firm_prices']} lines on not-firm prices ({B.fmt_inr(t['value_on_not_firm_prices_inr'])})", "amber"))
    n = open_item_count(conn)
    if n:
        out.append((f"{n} items not yet reviewed", "amber"))
    if t.get("lines_with_imputed_freight"):
        out.append((f"{t['lines_with_imputed_freight']} lines use estimated freight", "amber"))
    if t.get("lines_without_winner"):
        out.append((f"{t['lines_without_winner']} lines have no winner", "red"))
    return out


def _process(conn, q: str) -> None:
    history = [(m["role"], m["text"]) for m in st.session_state["chat"]]
    st.session_state["chat"].append({"role": "user", "text": q})
    try:
        with st.spinner("Analysing ..."):
            r = ask(conn, q, history, proposal=st.session_state.get("proposal"))
        scen = None
        if r.scenario:
            try:
                scen = {**r.scenario, "totals": T.award_scenario(conn, **r.scenario["params"]).attrs.get("totals", {})}
            except Exception:
                scen = None
        msg = {"role": "assistant", "text": r.text or "(no answer)", "headline": r.headline, "context": r.context, "tables": r.tables, "figures": r.figures,
               "trace": r.trace, "followups": r.followups, "scenario": scen, "badges": _badges(conn, scen), "q": q}
        if r.proposal:
            st.session_state["proposal"] = r.proposal
    except Exception as e:  # a model problem is never a stack trace: say what happened and answer from the data we already have
        from core import errors
        msg = {"role": "assistant", "warn": errors.friendly(e), "q": q, "followups": ["Which unconfirmed values could change the winner?"], "text": "", "context": "", "headline": ""}
        try:
            best = B.run_scenarios(conn)[0]
            msg.update(headline=B.headline(best, conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]),
                       context="This is the current best scenario computed from the saved data, not an answer to your question.", text="(fallback)")
        except Exception:
            msg["headline"] = "The saved data is unchanged."
    st.session_state["chat"].append(msg)


def _pin(i: int) -> None:
    pins = st.session_state.setdefault("pinned", [])
    m = st.session_state["chat"][i]
    hit = next((p for p in pins if p["i"] == i), None)
    if hit:
        pins.remove(hit)
    else:
        pins.append({"i": i, "title": m.get("q", "Analyst answer")[:90], "text": (m.get("headline") or m["text"])[:400]})


def _propose_from(conn, i: int) -> None:
    m = st.session_state["chat"][i]
    ctx = T.ToolContext(conn)
    try:
        T.propose_award(ctx, chat_prompt=m.get("q", ""), **m["scenario"]["params"])
        st.session_state["proposal"] = ctx.proposal
        st.session_state["aa_reasons"] = {}
    finally:
        ctx.close()
    st.session_state["chat"].append({"role": "assistant", "text": "Proposed. Review it in the Award panel; only you can confirm.",
                                     "headline": "Proposed. Review it in the Award panel; only you can confirm.", "context": "", "followups": [], "q": ""})


def _xlsx_of(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
        df.to_excel(w, index=False, sheet_name="Data")
    return buf.getvalue()


# ------------------------------------------------------------------ chat rendering
def _how(m: dict) -> str:
    out = []
    for n, t in enumerate(m.get("trace") or [], 1):
        a = t["args"]
        if t["tool"] in ("query_view", "render_table", "render_chart"):
            views = sorted(set(re.findall(r"\b(v_[a-z_]+)\b", a.get("sql", ""))))
            what = f"<code>{t['tool']}</code> on {', '.join(f'<code>{v}</code>' for v in views) or 'the views'}"
        else:
            args = ", ".join(f"{k} = {v}" for k, v in a.items()) or "defaults (evaluated price, qualified only)"
            what = f"<code>{t['tool']}</code> ({_e(args)})"
        out.append(f"<div>{n}. {what}{'' if t['ok'] else ' <span style=color:var(--rr-red)>(refused: ' + _e(str(t['result'].get('error', ''))) + ')</span>'}</div>")
    out.append("<div style='margin-top:6px;color:var(--rr-mut)'>Prices: evaluated ₹/pc (landed + payment cost), GST excluded unless an answer says landed. "
               "Savings: landed vs last year's landed, same lines.</div>")
    return "".join(out)


def _message(conn, i: int, m: dict, last: bool) -> None:
    if m["role"] == "user":
        st.html(f"<div class='aa-user'>{_e(m['text'])}</div>")
        return
    with st.container(key=f"aa_ans_{i}"):
        if m.get("warn"):
            st.warning(m["warn"], icon="⚠️")
        head = m.get("headline") or m["text"]
        ctx_lines = (m.get("context") or "").splitlines() if m.get("headline") else []
        first = next((x for x in ctx_lines if x.strip()), "")
        st.html(f"<div class='aa-head'>{_md(head)}</div>" + (f"<div class='aa-ctx'>{_md(first)}</div>" if first else ""))
        rest = "\n".join(ctx_lines[ctx_lines.index(first) + 1:]).strip() if first else ""
        if rest:
            st.markdown(re.sub(r"^#+\s*(.+)$", r"**" + chr(92) + "1**", rest, flags=re.M))
        for j, (title, df) in enumerate(m.get("tables", [])):
            if title:
                st.markdown(f"**{title}**")
            full = f"aa_all_{i}_{j}" in st.session_state.get("aa_open", set())
            st.dataframe(df if full or len(df) <= 5 else df.head(5), hide_index=True, width="stretch")
            if len(df) > 5:
                st.button("show fewer" if full else f"… {len(df) - 5} more rows · show all", key=f"aa_more_{i}_{j}", type="tertiary", on_click=_toggle_open, args=(f"aa_all_{i}_{j}",))
        for j, (title, fig) in enumerate(m.get("figures", [])):
            st.plotly_chart(fig, width="stretch", key=f"fig_{i}_{j}")
        if m.get("badges"):
            st.html("<div class='aa-badges'>" + "".join(f"<span class='rr-chip {c}'>{_e(t)}</span>" for t, c in m["badges"]) + "</div>")
        if m.get("trace"):
            with st.expander(f"How I got this ({len(m['trace'])} tool calls)"):
                st.html(f"<div class='aa-how'>{_how(m)}</div>")
        pinned = any(p["i"] == i for p in st.session_state.get("pinned", []))
        with st.container(horizontal=True, gap="small", key=f"aa_btns_{i}"):
            st.button("📌 Pinned" if pinned else "📌 Pin to award memo", key=f"aa_pin_{i}", on_click=_pin, args=(i,))
            if m.get("scenario"):
                st.button("Propose this as the award", key=f"aa_prop_{i}", on_click=_propose_from, args=(conn, i))
            if m.get("tables"):
                st.download_button("⬇ Table as Excel", data=lambda t=m["tables"][0][1]: _xlsx_of(t), file_name="analyst_table.xlsx", key=f"aa_dl_{i}",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            elif m.get("figures"):
                fig = m["figures"][0][1]
                st.download_button("⬇ Chart data", data=lambda f=fig: _xlsx_of(pd.DataFrame({tr.name or f"series {k}": pd.Series(list(tr.y if tr.y is not None else [])) for k, tr in enumerate(f.data)})),
                                   file_name="analyst_chart.xlsx", key=f"aa_dlc_{i}", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if last and m.get("followups"):
        st.caption("Follow-ups")
        with st.container(horizontal=True, gap="small", key=f"aa_fu_{i}"):
            for k, f in enumerate(m["followups"]):
                st.button(f, key=f"aa_fu_{i}_{k}", on_click=_queue, args=(f,))


def _chat(conn):
    chat = st.session_state["chat"]
    with st.container(key="aa_chat"):
        st.html("<div class='aa-hd'><b>Ask the analyst</b><span class='rr-meta'>Answers come only from the comparison data. The analyst can propose; only you can confirm.</span></div>")
        name = _buyer()
        st.html("<div class='aa-card'>" + (f"Hi {_e(name)}. " if name != "the buyer" else "Hi. ") + "I can answer questions over all 5 replies, the 30 lines and the questionnaire, "
                "run what-ifs, and propose an award for you to confirm. Try one of these:</div>")
        used = sum(1 for m in chat if m["role"] == "user")
        if not used:
            with st.container(horizontal=True, gap="small", key="aa_starters"):
                for k, s in enumerate(STARTERS):
                    st.button(s, key=f"aa_st_{k}", on_click=_queue, args=(s,))
        last_a = max((i for i, m in enumerate(chat) if m["role"] == "assistant"), default=-1)
        for i, m in enumerate(chat):
            _message(conn, i, m, i == last_a)
        left = _limit() - used
        pre = st.session_state.pop("analyst_prefill", None)
        if pre:    # "Ask about this line" from Bid Analysis: put the question in the box (st.chat_input has no default value), the buyer presses Enter
            import json as _json
            st.session_state["analyst_prefilled"] = pre
            st.html("<script>(function(){const t=" + _json.dumps(pre) + ";let n=0;const id=setInterval(function(){"
                    "const a=[...document.querySelectorAll('textarea[data-testid=\"stChatInputTextArea\"]')].filter(e=>e.offsetParent!==null);"
                    "if(a.length){const el=a[0];const set=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;set.call(el,t);"
                    "el.dispatchEvent(new Event('input',{bubbles:true}));el.focus();clearInterval(id);} if(++n>40)clearInterval(id);},150);})();</script>",
                    unsafe_allow_javascript=True)
        q = st.chat_input("Ask about prices, qualification, what-ifs, or ask for a proposed award" if left > 0 else "Question limit reached for this session",
                          disabled=left <= 0, key="chat_input")
        st.caption(f"{max(left, 0)} of {_limit()} questions left in this session")
    return q


# ------------------------------------------------------------------ award panel
def _steps(current: int) -> str:
    cells = []
    for k, s in enumerate(STEPS, 1):
        cls = "done" if k < current else "cur" if k == current else ""
        cells.append(f"<span class='aa-step {cls}'>{'✓ ' if k < current else ''}{_e(s)}</span>")
    return "<div class='aa-steps'>" + "".join(cells) + "</div>"


def _kv(rows: list[tuple[str, str]]) -> str:
    return "<div class='aa-kv'>" + "".join(f"<span class='k'>{_e(k)}</span><span class='v'>{v}</span>" for k, v in rows) + "</div>"


def _bars(totals: dict) -> str:
    by = totals.get("by_vendor", {})
    mx = max(by.values()) if by else 1
    n = totals.get("lines_by_vendor", {})
    return "".join(f"<div class='aa-bar'><span>{_e(v)} · {n.get(v, 0)} lines</span><i><b style='width:{val / mx * 100:.0f}%'></b></i><span class='r'>{B.fmt_inr(val)}</span></div>"
                   for v, val in sorted(by.items(), key=lambda kv: -kv[1]))


def _risk_list(conn, t: dict) -> str:
    items = []
    if t.get("value_on_not_firm_prices_inr"):
        items.append(("⚠", f"{B.fmt_inr(t['value_on_not_firm_prices_inr'])} on {', '.join(t['not_firm_vendors'])} prices not firm (kraft-linked)"))
    n = open_item_count(conn)
    if n:
        items.append(("⚠", f"{n} items still to review (could change winners)"))
    if t.get("lines_with_imputed_freight"):
        items.append(("≈", f"{t['lines_with_imputed_freight']} awarded lines use estimated freight"))
    items.append(("ℹ", "Backup vendor recorded for every line"))
    return "<div class='aa-grp'>Risks to accept</div>" + "".join(f"<div class='aa-risk'><b>{i}</b> {_e(t_)}</div>" for i, t_ in items)


def _need_reason(conn, award: dict) -> tuple[list[dict], bool]:
    """Exception lines of a proposal with their per-line code + reason inputs. Returns (exceptions, every one complete)."""
    reasons = st.session_state.setdefault("aa_reasons", {})
    ex = M.exceptions(conn, award, dict(reasons))
    ok = True
    if ex:
        st.html("<div class='aa-grp'>Needs a reason</div>")
    for r in ex:
        ln = r["line_no"]
        with st.container(key=f"aa_need_{ln}"):
            st.html(f"<div class='aa-why'><b>Line {ln}</b> · {_e(r['desc'])} → <b>{_e(r['awarded_vendor'])} ₹{r['awarded_price']:.2f}</b> instead of the cheapest eligible "
                    f"<b>{_e(r['l1_vendor'])} ₹{(r['l1_price'] or 0):.2f}</b> (+₹{r['extra_per_year']:,.0f} a year)</div>")
            prev = reasons.get(ln, (None, ""))
            code = st.selectbox("Reason code", list(T.BYPASS_CODES), index=list(T.BYPASS_CODES).index(prev[0]) if prev[0] in T.BYPASS_CODES else None,
                                placeholder="Choose a code", format_func=lambda c: CODE_LABEL.get(c, c), key=f"aa_code_{ln}")
            why = st.text_input("Reason (saved to the audit trail)", value=prev[1] or "", key=f"aa_why_{ln}")
            reasons[ln] = (code, why)
            ok = ok and bool(code) and bool(why.strip())
    return ex, ok


def _award_panel(conn) -> None:
    prop = st.session_state.get("proposal")
    aw = M.current_award(conn, prop)
    pins = st.session_state.get("pinned", [])
    status = aw["status"]
    chip = {"none": "<span class='rr-chip grey'>No proposal yet</span>", "proposed": "<span class='rr-chip blue'>Proposed · not confirmed</span>",
            "awarded": "<span class='rr-chip green'>Awarded</span>"}[status]
    with st.container(key="aa_award"):
        st.html(f"<div class='aa-hd'><b>Award</b>{chip}</div>")
        if status == "none":
            st.html(_steps(1) + "<div class='aa-hint'>Ask the analyst to propose an award, for example <i>“Propose the cheapest award among qualified vendors”</i>, "
                    "or pick a scenario in Bid Analysis.</div>")
            if conn.execute("SELECT COUNT(*) FROM quotes WHERE is_current=1").fetchone()[0]:
                best = B.run_scenarios(conn)[0]["totals"]
                st.html("<div class='aa-grp'>Current best scenario (not proposed)</div>" + _kv([
                    ("Cheapest per line · qualified only", f"<b>{B.fmt_inr(best.get('total_value_inr'))}</b>"),
                    ("vs last year (landed)", f"<b style='color:var(--rr-green)'>{'↓' if best.get('saving_vs_ly_inr', 0) >= 0 else '↑'} {B.fmt_inr(abs(best.get('saving_vs_ly_inr', 0)))}</b>")]))
                st.download_button("⬇ Download Excel", data=lambda: build_workbook(conn, None, pins), file_name="award_workbook.xlsx", key="dl_xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                   help="Award, Scenario, Audit trail, Assumptions and Memo sheets.")
            else:
                st.button("Download Excel", disabled=True, key="dl_xlsx_off")
                st.caption("No award proposed yet: there are no quotes loaded, so there is nothing to export.")
            return
        t = aw["totals"]
        title = _scenario_title(aw.get("params", {})) + (f" · {len(aw['overrides'])} override{'s' if len(aw['overrides']) != 1 else ''}" if aw["overrides"] else "")
        if status == "proposed":
            st.html(_steps(2) + f"<div class='aa-meta'>Staged by the analyst from: “{_e((aw.get('prompt') or '')[:80])}” · {_e((aw.get('at') or '')[11:16])}</div>")
        else:
            when = ""
            if aw.get("at"):
                d = datetime.fromisoformat(aw["at"]).astimezone()
                when = f"{d.day} {d:%b %Y}, {d:%H:%M}"
            who = aw.get("actor") or "buyer"
            st.html(_steps(4) + f"<div class='aa-meta'>Confirmed by {_e(_buyer() if who == 'buyer' else who)} · {_e(when)} · batch {_e((aw['batch'] or '')[:8])}</div>")
        sav = t.get("saving_vs_ly_inr", 0.0)
        n_lines = conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]
        st.html(_kv([("Scenario", f"<b>{_e(title)}</b>"), ("Lines awarded", f"<b>{t.get('lines_awarded', 0)} / {n_lines}</b>"),
                     ("Annual value (evaluated)", f"<b>{B.fmt_inr(t.get('total_value_inr'))}</b>"),
                     ("vs last year (landed)", f"<b style='color:var(--rr-{'green' if sav >= 0 else 'red'})'>{'↓' if sav >= 0 else '↑'} {B.fmt_inr(abs(sav))} ({abs(t.get('saving_vs_ly_pct', 0)):.1f}%)</b>")])
                + "<div style='margin-top:10px'>" + _bars(t) + "</div>" + _risk_list(conn, t))
        blocked = []
        if status == "proposed":
            g = pd.read_sql_query("SELECT quote_id, is_awardable FROM v_comparison_grid", conn)
            ok_q = set(g.loc[g["is_awardable"] == 1, "quote_id"])
            blocked = sorted(int(r["line_no"]) for r in aw["rows"] if r.get("quote_id") is not None and not pd.isna(r["quote_id"]) and int(r["quote_id"]) not in ok_q)
            if blocked:
                st.warning(f"Lines {', '.join(map(str, blocked[:8]))}{'…' if len(blocked) > 8 else ''} are won by a vendor that is not yet qualified (or is not eligible), so this "
                           "scenario can't be confirmed. It is a what-if: resolve those vendors first, or propose another scenario.", icon="⚠️")
            ex, ok = _need_reason(conn, aw)
            ok = ok and not blocked
            with st.container(horizontal=True, gap="small", key="aa_actions"):
                st.button("📄 Preview award memo", key="aa_memo", on_click=_set_dialog, args=("memo",))
                st.button("Confirm award…", key="confirm_award", type="primary", disabled=not ok, on_click=_set_dialog, args=("confirm",))
                st.button("Discard proposal", key="discard_proposal", type="tertiary", on_click=lambda: st.session_state.update(proposal=None, aa_reasons={}))
            if not ok:
                st.caption("Give every line under “Needs a reason” a reason code and a reason to enable Confirm.")
            st.caption(f"Pinned answers: {len(pins)} · they go into the award memo.")
            st.download_button("⬇ Download Excel", data=lambda: build_workbook(conn, prop, pins), file_name="award_workbook.xlsx", key="dl_xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="tertiary")
        else:
            notified = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='award_notify_stub' AND batch_id=?", (aw["batch"],)).fetchone()[0]
            st.html("<div class='aa-grp'>Next steps</div>")
            with st.container(horizontal=True, gap="small", key="aa_actions"):
                st.button(f"✉ Notify suppliers (stub){' ✓' if notified else ''}", key="aa_notify", type="primary", on_click=_set_dialog, args=("notify",))
                st.download_button("⬇ Award pack (Excel + memo)", data=lambda: _pack(conn, pins), file_name="award_pack.zip", mime="application/zip", key="dl_xlsx")
            with st.container(horizontal=True, gap="small", key="aa_actions2"):
                st.button("Send for approval (stub)", key="aa_approve", on_click=_approval, args=(conn, aw["batch"]))
                st.button("↺ Undo award", key="aa_undo", on_click=_set_dialog, args=("undo",))
            st.caption("Next in a real system: create the rate contract / POs. Out of scope here.")


def _pack(conn, pins: list[dict]) -> bytes:
    m = M.memo_data(conn, None, pins, _buyer())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("award_workbook.xlsx", build_workbook(conn, None, pins))
        z.writestr("award_memo.pdf", M.memo_pdf(m))
    return buf.getvalue()


def _approval(conn, batch: str | None) -> None:
    audit.log(conn, "award_approval_stub", ("award", None), new="sent for approval (stub)", actor="buyer", batch_id=batch, evidence={"stub": True})
    conn.commit()
    st.toast("Sent for approval (stub). Logged in the audit trail.")


# ------------------------------------------------------------------ dialogs
def _confirm_body(conn) -> None:
    prop = st.session_state.get("proposal")
    aw = M.current_award(conn, prop)
    t = aw["totals"]
    reasons = st.session_state.get("aa_reasons", {})
    ex = M.exceptions(conn, aw, reasons)
    by = t.get("lines_by_vendor", {})
    split = ", ".join(f"{v} {n}" for v, n in sorted(by.items(), key=lambda kv: -kv[1]))
    sav = t.get("saving_vs_ly_inr", 0.0)
    exl = ", ".join(f"line {r['line_no']}" for r in ex[:6]) + ("…" if len(ex) > 6 else "") if ex else "none"
    st.html(_kv([("Lines", f"<b>{t.get('lines_awarded', 0)} · {_e(split)}</b>"), ("Annual value (evaluated)", f"<b>{B.fmt_inr(t.get('total_value_inr'))}</b>"),
                 ("vs last year (landed)", f"<b style='color:var(--rr-{'green' if sav >= 0 else 'red'})'>{'↓' if sav >= 0 else '↑'} {B.fmt_inr(abs(sav))}</b>"),
                 ("Not on the cheapest eligible bid", f"<b>{len(ex)}</b> ({_e(exl)}); reasons recorded")]))
    n_open = open_item_count(conn)
    if n_open or t.get("value_on_not_firm_prices_inr"):
        st.warning(f"{n_open} items are still unreviewed and {B.fmt_inr(t.get('value_on_not_firm_prices_inr', 0))} is on prices not firm. You can still confirm; both are recorded with the award.")
    st.caption("Writes one audit entry per line (who, when, scenario, reason) and sets the RFQ to Awarded. You can undo it later with a reason.")
    c = st.columns([3, 1, 1.4])
    c[1].button("Cancel", type="primary", key="aa_cancel", on_click=_close_dialog, width="stretch")
    if c[2].button("Confirm award", key="aa_do_confirm", width="stretch"):
        try:
            res = T.commit_award(conn, prop, chat_prompt=prop.get("chat_prompt"), line_reasons={k: v for k, v in reasons.items() if v[0]})
        except ValueError as e:
            st.error(str(e))
            return
        st.session_state["proposal"] = None
        st.session_state["aa_reasons"] = {}
        st.session_state["commit_result"] = res
        st.session_state["chat"].append({"role": "assistant", "text": "Award confirmed.", "headline": f"Award confirmed: {res['lines_awarded']} lines. Next: notify the suppliers and download the award pack.",
                                         "context": "", "followups": ["Draft the note to the non-winning suppliers", "Summarise the award for my VP"], "q": ""})
        _close_dialog()
        st.rerun()


def _memo_body(conn) -> None:
    m = M.memo_data(conn, st.session_state.get("proposal"), st.session_state.get("pinned", []), _buyer(),
                    {k: v for k, v in st.session_state.get("aa_reasons", {}).items() if v[0]})
    st.html(M.MEMO_CSS + M.memo_html(m))
    c = st.columns([3, 1.6, 1])
    c[1].download_button("⬇ Download memo (PDF)", data=lambda: M.memo_pdf(m), file_name="award_memo.pdf", mime="application/pdf", key="aa_memo_pdf")
    c[2].button("Close", type="primary", key="aa_memo_close", on_click=_close_dialog, width="stretch")


def _notify_body(conn) -> None:
    aw = M.current_award(conn, None)
    ns = M.notices(conn, aw)
    win = [n for n in ns if n["won"]]
    lose = [n for n in ns if not n["won"]]
    st.markdown("**Winners**")
    st.dataframe(pd.DataFrame([{"Supplier": f"{n['code']} · {n['name']}", "Lines": n["lines"], "Annual value": B.fmt_inr(n["value"])} for n in win]), hide_index=True, width="stretch")
    st.markdown("**Not awarded**")
    st.dataframe(pd.DataFrame([{"Supplier": f"{n['code']} · {n['name']}", "Reason": n["reason"]} for n in lose]), hide_index=True, width="stretch")
    pick = st.segmented_control("Preview the email to", [n["code"] for n in ns], default=ns[0]["code"], key="aa_notify_pick") or ns[0]["code"]
    n = next(x for x in ns if x["code"] == pick)
    st.html(f"<div class='aa-mail'><b>To</b> {_e(n['email'])}<br><b>Subject</b> {_e(n['subject'])}<hr>{_e(n['body']).replace(chr(10), '<br>')}</div>")
    c = st.columns([3, 1, 1.8])
    c[1].button("Cancel", type="primary", key="aa_notify_cancel", on_click=_close_dialog, width="stretch")
    if c[2].button(f"Send {len(ns)} emails (stub)", key="aa_notify_send", width="stretch"):
        for x in ns:
            audit.log(conn, "award_notify_stub", ("vendor", None), field=x["code"], new="won" if x["won"] else "not awarded", actor="buyer", batch_id=aw["batch"],
                      evidence={"to": x["email"], "subject": x["subject"], "lines": x["lines"], "stub": True})
        conn.commit()
        st.toast(f"Sent {len(ns)} emails (stub). Logged in the audit trail.")
        _close_dialog()
        st.rerun()


def _undo_body(conn) -> None:
    why = st.text_input("Reason for undoing the award (required)", key="unaward_reason")
    c = st.columns([3, 1, 1.4])
    c[1].button("Cancel", type="primary", key="aa_undo_cancel", on_click=_close_dialog, width="stretch")
    if c[2].button("Undo award", key="unaward_btn", width="stretch", disabled=not why.strip()):
        try:
            n = T.unaward(conn, why)
        except ValueError as e:
            st.error(str(e))
            return
        st.toast(f"Cleared {n} lines")
        _close_dialog()
        st.rerun()


def _show_dialog(conn) -> None:
    spec = st.session_state.get("_aa_dialog")
    if not spec:
        return
    k = spec[0]
    if k == "confirm":
        st.dialog("Confirm award?", on_dismiss=_close_dialog)(_confirm_body)(conn)
    elif k == "memo":
        st.dialog("Award memo · preview", width="large", on_dismiss=_close_dialog)(_memo_body)(conn)
    elif k == "notify":
        st.dialog("Notify suppliers (stub)", width="large", on_dismiss=_close_dialog)(_notify_body)(conn)
    elif k == "undo":
        st.dialog("Undo the award?", on_dismiss=_close_dialog)(_undo_body)(conn)


# ------------------------------------------------------------------ the tab
def render(conn) -> None:
    st.session_state.setdefault("chat", [])
    st.session_state.setdefault("proposal", None)
    st.session_state.setdefault("pinned", [])
    queued = st.session_state.pop("aa_queue", None)
    if queued and sum(1 for m in st.session_state["chat"] if m["role"] == "user") < _limit():
        _process(conn, queued)
    res = st.session_state.pop("commit_result", None)
    if res:
        st.success(f"Award committed: {res['lines_awarded']} lines written, one audit row per line (batch {res['batch_id'][:8]}). "
                   + (f"Bypass reasons recorded on lines {res['bypass_lines']}." if res["bypass_lines"] else ""))
    left, right = st.columns([1.5, 1], gap="medium")
    with left:
        q = _chat(conn)
    with right:
        _award_panel(conn)
    _show_dialog(conn)
    if q:
        _process(conn, q)
        st.rerun()
    if queued:
        st.rerun()
