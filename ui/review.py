"""Review & Reconcile: a queue of review ITEMS per vendor (vendor-term decisions first, then lines), with the original source beside it.

Page code only. Decisions go through core.review_items -> core.actions (audit + recompute); nothing here edits a value directly."""
from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from core import review_items as RI
from core.queries import review_rows
from core.responses_view import ROLE_LABEL
from core.stage import fmt_date
from ui.source_view import render_source
from ui.style import inject

GENERIC = {"corrugators", "packaging", "industries", "pvt", "ltd", "box", "works", "cartons", "llp"}
DOT = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
UNIT_LABEL = {"per_piece": "Per piece", "per_box": "Per box", "per_100": "Per 100 pieces", "per_1000": "Per 1000 pieces", "per_kg": "Per kg"}
DEFAULT_REASON = {"confirm": "Confirmed as read against the source", "keep": "Conditional discount kept not applied", "apply": "Conditional discount applied by the buyer",
                  "tier_order": "Tier read as per order", "tier_year": "Tier read as per year", "ask": "Asked the vendor"}
DONE_TEXT = {"confirm": "Confirmed", "keep": "Kept not applied", "apply": "Applied", "tier_order": "Per order", "tier_year": "Per year", "ask": "Asked vendor",
             "accept": "Accepted", "reject": "Rejected", "correct": "Corrected", "enter_freight": "Freight entered"}


def short_name(name: str) -> str:
    out = []
    for w in name.split():
        if w.lower() in GENERIC or len(out) == 2:
            break
        out.append(w)
    return " ".join(out) or name.split()[0]


def _money(x) -> str:
    return "n/a" if x is None else f"₹{x:,.2f}"


# ------------------------------------------------------------------ actions
def _do(conn, it: dict, action: str, reason: str | None = None, value=None, apply_to: list[dict] | None = None) -> None:
    try:
        did = RI.act(conn, it, action, reason or DEFAULT_REASON.get(action, ""), value, apply_to)
    except (ValueError, KeyError) as e:
        st.error(str(e))
        return
    scope = f"{len(it['quotes'])} lines" if it["scope"] == "vendor" else f"line {it['line_nos'][0]}"
    st.session_state["rv_last"] = {"id": did, "text": f"{DONE_TEXT.get(action, action)}: {it['title']} ({it['vendor']}, {scope})."
                                                    + (" Added to the clarification email in Responses." if action == "ask" else "")}
    st.session_state.pop("rv_form", None)
    st.session_state["rv_sel"] = None            # auto-advance: the next open item is selected on the redraw
    st.rerun()


def _undo(conn, did: int) -> None:
    RI.undo(conn, did)
    st.session_state.pop("rv_last", None)
    st.session_state["rv_sel"] = None


def _select(sel: str) -> None:
    st.session_state["rv_sel"] = sel


def _open_form(iid: str, action: str) -> None:
    st.session_state["rv_form"] = (iid, action)


def _close_form() -> None:
    st.session_state.pop("rv_form", None)


# ------------------------------------------------------------------ item card
def _evidence(conn, it: dict, r: dict, d: dict) -> None:
    val = r["validation"] or []
    bad = [c for c in val if c["result"] in ("fail", "warn")]
    passed = sum(c["result"] == "pass" for c in val)
    if bad:
        st.dataframe([{"Check": c["check"].replace("_", " ").capitalize(), "Result": "✕ Fail" if c["result"] == "fail" else "! Warn", "Detail": c["detail"]} for c in bad],
                     hide_index=True, width="stretch")
    with st.expander(f"{passed} of {len(val)} checks passed · show all"):
        st.dataframe([{"Check": c["check"].replace("_", " ").capitalize(), "Result": c["result"], "Detail": c["detail"]} for c in val], hide_index=True, width="stretch")
    if r["trace"]:
        st.markdown("**How computed:** " + " → ".join(t.rstrip(".") for t in r["trace"][-4:]))
    if d["source"]:
        st.markdown(f"**Source:** {d['source']['ref']} · “{d['source']['snippet']}”")


def _correct_form(conn, it: dict, r: dict, vendor_items: list[dict]) -> None:
    sug = RI.suggested_unit(r)
    units = list(UNIT_LABEL)
    cur = r["raw_unit"] if r["raw_unit"] in units else "per_piece"
    start = sug or cur
    with st.container(border=True, key=f"rvform_{it['id']}"):
        c = st.columns([1.3, 1.1, 1.8])
        unit = c[0].selectbox("Unit", units, index=units.index(start), format_func=lambda u: UNIT_LABEL[u], key=f"rvu_{it['id']}")
        if sug and unit == sug:
            c[0].markdown(":green-badge[suggested]")
        price = c[1].number_input("Price as written", value=float(r["raw_price"] or 0), format="%.2f", key=f"rvp_{it['id']}")
        try:
            res = RI.preview_correction(conn, r["quote_id"], unit, price)
            c[2].markdown(f"Result  \n**{_money(res['base_inr_pc'])}/pc** · landed {_money(res['landed_inr_pc'])} · evaluated {_money(res['evaluated_inr_pc'])}")
        except Exception as e:
            c[2].caption(f"Preview not available: {e}")
        reason = st.text_input("Reason (saved to the audit trail)", value=(f"Value is far above the other vendors; '{r['raw_unit']}' here means {UNIT_LABEL.get(sug, sug).lower()}." if sug and unit == sug
                                                                           else ""), key=f"rvr_{it['id']}_{unit}")
        same = [x for x in vendor_items if x["id"] != it["id"] and x["kind"] == it["kind"] and x["state"] == "open" and x["scope"] == "line"
                and RI.suggested_unit(x["first_row"]) == sug and sug]
        also = False
        if same:
            nums = ", ".join(str(x["line_nos"][0]) for x in same)
            also = st.checkbox(f"Also apply to lines {nums} (same column, same problem): yes, apply to all {len(same) + 1}", key=f"rvall_{it['id']}")
        b = st.columns([1.1, 1, 4])
        if b[0].button("Save correction", type="primary", key=f"rvsave_{it['id']}", disabled=not reason.strip()):
            _do(conn, it, "correct", reason, (unit, price), same if also else None)
        b[1].button("Cancel", key=f"rvcancel_{it['id']}", on_click=_close_form)


def _reason_form(conn, it: dict, action: str, label: str, default: str, numeric: str | None = None) -> None:
    with st.container(border=True, key=f"rvform_{it['id']}"):
        val = None
        if numeric:
            val = st.number_input(numeric, min_value=0.0, value=0.0, format="%.2f", key=f"rvn_{it['id']}")
        reason = st.text_input("Reason (saved to the audit trail)", value=default, key=f"rvr_{it['id']}_{action}")
        b = st.columns([1.3, 1, 4])
        if b[0].button(label, type="primary", key=f"rvsave_{it['id']}_{action}", disabled=not reason.strip() or (numeric is not None and not val)):
            _do(conn, it, action, reason, val)
        b[1].button("Cancel", key=f"rvcancel_{it['id']}_{action}", on_click=_close_form)


def _item_card(conn, it: dict, rows: list[dict], vendor_items: list[dict], selected: bool) -> None:
    r = it["first_row"]
    d = RI.describe(conn, it, rows)
    scope = (f"all {len(it['quotes'])} {'quoted ' if it['vendor'] and False else ''}lines" if it["scope"] == "vendor" and len(it["quotes"]) > 1
             else f"line {it['line_nos'][0]}" if it["line_nos"] else "")
    if it["scope"] == "line":
        head = f"**Line {it['line_nos'][0]}** · {r['description']} · {it['title'].lower()}"
    else:
        head = f"**{it['title']}** · {scope}" + (f" ({', '.join(map(str, it['line_nos']))})" if it["kind"] == "same_ly" else "")
    with st.container(border=True, key=f"rvitem_{'sel_' if selected else ''}{it['id'].replace(':', '_')}"):
        h = st.columns([6, 1.4], vertical_alignment="center")
        chips = ""
        if it["could_change"] and it["state"] == "open":
            chips += " :red-badge[Could change the award]"
        if it["qual_status"] == "disqualified":
            chips += " :gray-badge[Can't win: disqualified]"
        if it["state"] == "asked":
            chips += " :orange-badge[✉ Asked vendor · waiting]"
        h[0].markdown(f"{DOT[it['severity']]} {head}{chips}")
        h[1].button("Show source", key=f"rvs_{it['id']}", type="tertiary", on_click=_select, args=(it["id"],))
        for lab, txt in (("Vendor wrote", d["wrote"]), ("Read as", d["read"])):
            if txt:
                lc = st.columns([1, 4.2])
                lc[0].markdown(f":gray[{lab}]")
                lc[1].markdown(txt)
        if d["why"]:
            st.markdown(d["why"])
        if it["state"] == "asked":
            c = st.columns([5, 1.6, 1.8], vertical_alignment="center")
            c[0].caption(f"Added to {it['vendor']}'s clarification email in Responses. The value stays out of the award until they answer.")
            if it["kind"] in ("price", "ocr"):
                c[1].button("✎ Correct now", key=f"rvcn_{it['id']}", on_click=_open_form, args=(it["id"], "correct"))
        elif it["state"] == "open":
            acts = RI.actions_for(it)
            cols = st.columns([max(1.2, len(l) / 9) for _, l in acts] + [0.5], vertical_alignment="center")
            for i, (a, label) in enumerate(acts):
                if cols[i].button(label, key=f"rvb_{it['id']}_{a}", on_click=_select, args=(it["id"],)):
                    if a in ("correct", "accept", "reject", "enter_freight"):
                        _open_form(it["id"], a)
                        st.rerun()
                    elif a == "apply":
                        pct = next((x.get("pct") for x in r["discounts"] if x.get("condition_type") != "volume_qty"), "")
                        _do(conn, it, "apply", f"Apply the {pct:g}% discount")
                    elif a == "tier_year":
                        _do(conn, it, a, "Buyer reads the discount as per year")
                    else:
                        _do(conn, it, a)
        form = st.session_state.get("rv_form")
        if form and form[0] == it["id"]:
            if form[1] == "correct":
                _correct_form(conn, it, r, vendor_items)
            elif form[1] == "accept":
                _reason_form(conn, it, "accept", "Accept", "QA agrees: bursting strength is met")
            elif form[1] == "reject":
                _reason_form(conn, it, "reject", "Reject", f"{it['title']} not acceptable for this line")
            elif form[1] == "enter_freight":
                _reason_form(conn, it, "enter_freight", "Save freight", "Freight confirmed with the vendor", numeric="Freight ₹ per piece (all these lines)")
        with st.expander("Evidence & checks"):
            _evidence(conn, it, r, d)


# ------------------------------------------------------------------ dialogs
def _set_dialog(*spec) -> None:
    st.session_state["_rv_dialog"] = spec


def _close_dialog() -> None:
    st.session_state.pop("_rv_dialog", None)


def _bulk_body(conn, vendor: str) -> None:
    rows = review_rows(conn)
    items = RI.build_items(conn, rows)
    clean, excl, skip = RI.clean_lines(conn, vendor, rows, items)
    st.write("Every check passed on these lines. Scan them, then confirm. They are logged in the audit trail as one entry.")
    df = pd.DataFrame([{"Line": r["line_no"], "Description": r["description"], "Vendor wrote": r["raw_price_text"], "₹/pc": r["base_inr_pc"],
                        "Landed": r["landed_inr_pc"], "Checks": f"{RI.check_counts(r)[0]}/{RI.check_counts(r)[1]} ✓"} for r in clean])
    st.dataframe(df, hide_index=True, width="stretch", height=340,
                 column_config={"₹/pc": st.column_config.NumberColumn(format="₹%.2f"), "Landed": st.column_config.NumberColumn(format="₹%.2f")})
    names = ", ".join(str(r["line_no"]) for r in excl)
    st.caption(f"Showing {len(clean)} lines (scroll)." + (f" Lines {names} are not included: they still need your review." if excl else ""))
    b = st.columns([3, 1, 1.5])
    b[1].button("Cancel", type="primary", key="rvbulk_cancel", on_click=_close_dialog, width="stretch")
    if b[2].button(f"Confirm {len(clean)} lines", key="rvbulk_go", width="stretch", disabled=not clean):
        from core import actions
        actions.confirm_many(conn, [r["quote_id"] for r in clean], skip=skip)
        st.session_state["rv_last"] = {"id": None, "text": f"Confirmed {len(clean)} clean lines for {vendor} (one audit entry)."}
        _close_dialog()
        st.rerun()


def _show_dialog(conn) -> None:
    spec = st.session_state.get("_rv_dialog")
    if spec and spec[0] == "bulk":
        n = spec[2]
        st.dialog(f"Confirm {n} clean lines for {spec[1]}?", width="large", on_dismiss=_close_dialog)(_bulk_body)(conn, spec[1])


# ------------------------------------------------------------------ bottom sections
def _not_quoted(conn, vendor: str, rows: list[dict]) -> None:
    nq = [r for r in rows if r["vendor_code"] == vendor and r["quote_status"] in ("not_quoted", "no_bid")]
    with st.expander(f"Not quoted ({len(nq)})"):
        if not nq:
            st.caption("Nothing here.")
            return
        lines = ", ".join(str(r["line_no"]) for r in nq)
        st.markdown(f"⚪ **Lines {lines}**")
        st.caption(" · ".join(r["description"] for r in nq))
        st.markdown(f"Not on {vendor}'s reply. Shown as **Not quoted**, never ₹0, and {vendor} can't win these lines.")
        key = f"{vendor}:notquoted"
        asked = conn.execute("SELECT 1 FROM review_decisions WHERE item_id=? AND undone=0", (key,)).fetchone()
        if asked:
            st.markdown(":orange-badge[✉ Asked vendor · waiting]")
        elif st.button(f"✉ Ask {vendor} to quote these", key=f"rvnq_{vendor}"):
            RI.ask_vendor(conn, vendor, f"Please quote the following lines or confirm you are not bidding for them: {lines}.")
            RI._record(conn, {"id": key, "vendor": vendor, "kind": "notquoted"}, "ask", "Asked to quote the missing lines", [])
            st.rerun()


def _unmatched(conn, vendor: str, rows: list[dict]) -> None:
    from core import actions

    um = [r for r in rows if r["vendor_code"] == vendor and r["quote_status"] == "unmatched"]
    with st.expander(f"Unmatched lines ({len(um)})"):
        if not um:
            st.caption("Nothing here.")
        for r in um:
            st.markdown(f"Vendor line `{r['vendor_line_ref']}` · wrote `{r['raw_price_text']}` ({r['raw_unit']})")
            free = [x["line_no"] for x in conn.execute(
                "SELECT line_no FROM line_items WHERE line_id NOT IN (SELECT line_id FROM quotes WHERE vendor_id=(SELECT vendor_id FROM vendors WHERE vendor_code=?) "
                "AND is_current=1 AND line_id IS NOT NULL) ORDER BY line_no", (vendor,))]
            c = st.columns([2, 3, 1])
            ln = c[0].selectbox("Match to our line", free, key=f"rvmu_{r['quote_id']}")
            why = c[1].text_input("Reason (required)", key=f"rvmr_{r['quote_id']}")
            if c[2].button("Match", key=f"rvmb_{r['quote_id']}"):
                try:
                    actions.match_unmatched(conn, r["quote_id"], ln, why)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))


# ------------------------------------------------------------------ all lines
def _status_of(r: dict, items: list[dict]) -> str:
    if r["quote_status"] != "quoted":
        return "Not quoted"
    mine = [i for i in items if r["quote_id"] in i["quotes"]]
    if any(i["state"] == "open" for i in mine):
        return "Needs review"
    if any(i["state"] == "asked" for i in mine):
        return "Waiting"
    if r["overrides"]:
        return "Corrected"
    if r["fields"] and all(f.get("status") == "confirmed" for f in r["fields"].values()):
        return "Confirmed"
    return "All checks passed" if not r["open_fields"] else "Needs review"


def _all_lines(conn, vendor: str, rows: list[dict], items: list[dict]) -> None:
    mine = [r for r in rows if r["vendor_code"] == vendor and r["line_no"] is not None]
    df = pd.DataFrame([{"Line": r["line_no"], "Description": r["description"], "Vendor wrote": r["raw_price_text"] or "", "₹/pc": r["base_inr_pc"],
                        "Landed": r["landed_inr_pc"], "Status": _status_of(r, items)} for r in mine])
    ev = st.dataframe(df, hide_index=True, width="stretch", height=560, on_select="rerun", selection_mode="single-row", key="rv_all_table",
                      column_config={"₹/pc": st.column_config.NumberColumn(format="₹%.2f"), "Landed": st.column_config.NumberColumn(format="₹%.2f")})
    rowsel = ev.selection.rows if ev is not None else []
    if rowsel:
        r = mine[rowsel[0]]
        hit = next((i for i in items if r["quote_id"] in i["quotes"] and i["state"] != "done"), None)
        sel = hit["id"] if hit else f"line:{r['quote_id']}"
        if st.session_state.get("rv_all_last") != sel:
            st.session_state["rv_all_last"] = sel
            st.session_state["rv_sel"] = sel
            st.session_state["rv_view"] = "Needs review" if hit else st.session_state.get("rv_view")
            if hit:
                st.rerun()


# ------------------------------------------------------------------ done states
def _vendor_done(conn, vendor: str, order: list[str], items: list[dict], names: dict) -> None:
    ds = conn.execute("SELECT action, COUNT(*) n FROM review_decisions WHERE vendor_code=? AND undone=0 GROUP BY action", (vendor,)).fetchall()
    summ = " · ".join(f"{r['n']} {DONE_TEXT.get(r['action'], r['action']).lower()}" for r in ds) or "nothing needed a decision"
    st.success(f"✅ **{vendor} is done.** {summ}.")
    nxt = next((v for v in order if v != vendor and any(i["vendor"] == v and i["state"] == "open" for i in items)), None)
    if nxt:
        n = sum(i["vendor"] == nxt and i["state"] == "open" for i in items)
        st.button(f"Next: {nxt} · {n} to review →", key="rv_next_vendor", type="primary", on_click=lambda: st.session_state.update(rv_vendor=nxt, rv_sel=None))


def _review_log_xlsx(conn) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
        pd.read_sql_query("SELECT * FROM review_decisions ORDER BY decision_id", conn).to_excel(w, sheet_name="Decisions", index=False)
        pd.read_sql_query("SELECT ts, actor, action, entity_type, entity_id, field_name, old_value, new_value, reason, batch_id FROM audit_log ORDER BY audit_id",
                          conn).to_excel(w, sheet_name="Audit trail", index=False)
    return buf.getvalue()


def _all_done(conn) -> None:
    st.success("✅ **Everything is reviewed.** Nothing is waiting on you in Review & Reconcile.")
    c = st.columns([1.4, 1.6, 4])
    c[0].button("Go to Bid Analysis →", type="primary", key="rv_goto_bid", on_click=lambda: st.session_state.update(main_tab="Bid Analysis"))
    c[1].download_button("Download review log (Excel)", data=lambda: _review_log_xlsx(conn), file_name="review_log.xlsx",
                         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="rv_log_dl")


# ------------------------------------------------------------------ the tab
def render(conn) -> None:
    inject()
    rows = review_rows(conn)
    if not rows:
        st.info("No vendor quotes yet. Extract the replies on the Responses tab first.")
        return
    items = RI.build_items(conn, rows)
    order = sorted({r["vendor_code"] for r in rows})
    names = {r["vendor_code"]: r["vendor_name"] for r in rows}
    n_open = {v: sum(i["vendor"] == v and i["state"] == "open" for i in items) for v in order}
    if st.session_state.get("rv_vendor") not in order:
        st.session_state["rv_vendor"] = next((v for v in order if n_open[v]), order[0])
    st.pills("Vendor", order, key="rv_vendor", selection_mode="single",
             format_func=lambda v: f"{v} · {short_name(names[v])}  " + (f"({n_open[v]})" if n_open[v] else "✓"))
    vendor = st.session_state.get("rv_vendor") or order[0]
    vitems = [i for i in items if i["vendor"] == vendor]
    vrows = [r for r in rows if r["vendor_code"] == vendor]
    done, total = RI.vendor_progress(items, vendor)
    n_lines = conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]

    late = conn.execute("""SELECT received_at, (SELECT response_due_date FROM rfqs LIMIT 1) due FROM vendor_responses vr WHERE vendor_id=(SELECT vendor_id FROM vendors WHERE vendor_code=?)
                           AND received_after_due=1 ORDER BY response_id DESC LIMIT 1""", (vendor,)).fetchone()
    if late:
        st.info(f"{vendor} replied on {fmt_date(late['received_at'])}, after the due date ({fmt_date(late['due'])}). The reply is flagged, not blocked.", icon="ℹ️")
    if vrows and vrows[0]["qual_status"] == "disqualified":
        st.info(f"{vendor} is disqualified, so it can't win. Its items are listed last; review them only if you want a clean record.", icon="ℹ️")

    clean, excl, skip = RI.clean_lines(conn, vendor, rows, items)
    head = st.columns([3.2, 2.2, 1.8], vertical_alignment="center")
    head[0].markdown(f"**{vendor} · {names[vendor]}** :gray[· {done} of {total} reviewed]")
    view = head[1].segmented_control("View", ["Needs review", f"All {n_lines} lines"], default="Needs review", key="rv_view", label_visibility="collapsed") or "Needs review"
    head[2].button(f"✓ Confirm {len(clean)} clean lines", key="rv_bulk", disabled=not clean, width="stretch", on_click=_set_dialog, args=("bulk", vendor, len(clean)))
    st.progress(done / total if total else 1.0)

    left, right = st.columns([3, 2])
    open_items = [i for i in vitems if i["state"] == "open"]
    sel = st.session_state.get("rv_sel")
    pool = [i["id"] for i in vitems]
    if sel not in pool and not (isinstance(sel, str) and sel.startswith("line:")):
        sel = open_items[0]["id"] if open_items else None
        st.session_state["rv_sel"] = sel

    with left:
        last = st.session_state.get("rv_last")
        if last:
            c = st.columns([6, 1], vertical_alignment="center")
            c[0].success(last["text"], icon="✅")
            if last["id"]:
                c[1].button("Undo", key="rv_undo", on_click=_undo, args=(conn, last["id"]))
        if view == "Needs review":
            if not open_items and not any(i["state"] == "asked" for i in vitems):
                if not any(n_open.values()):
                    _all_done(conn)
                else:
                    _vendor_done(conn, vendor, order, items, names)
            vt = [i for i in vitems if i["scope"] == "vendor" and i["state"] != "done"]
            ln = [i for i in vitems if i["scope"] == "line" and i["state"] != "done"]
            if vt:
                st.caption(f"VENDOR TERMS · APPLY TO MANY LINES")
                for i in vt:
                    _item_card(conn, i, rows, vitems, i["id"] == sel)
            if ln:
                st.caption(f"LINES · {sum(i['state'] == 'open' for i in ln)} TO REVIEW" + ("      sorted: could change the award first" if len(ln) > 1 else ""))
                for i in ln:
                    _item_card(conn, i, rows, vitems, i["id"] == sel)
            reviewed = [i for i in vitems if i["state"] == "done"]
            if reviewed:
                with st.expander(f"Reviewed ({len(reviewed)})"):
                    for i in reviewed:
                        dd = i["decision"]
                        c = st.columns([5, 1.4], vertical_alignment="center")
                        c[0].markdown(f"🟢 **{i['title']}** · {'all ' + str(len(i['quotes'])) + ' lines' if i['scope'] == 'vendor' else 'line ' + str(i['line_nos'][0])}"
                                      f" :gray[· {DONE_TEXT.get(dd['action'], dd['action']) if dd else 'Confirmed'}]")
                        if dd:
                            c[1].button("Undo", key=f"rvun_{i['id']}", on_click=_undo, args=(conn, dd["decision_id"]))
            _not_quoted(conn, vendor, rows)
            _unmatched(conn, vendor, rows)
        else:
            _all_lines(conn, vendor, rows, items)

    with right:
        with st.container(key="rv_viewer", border=True):
            it = next((i for i in vitems if i["id"] == sel), None)
            if it is not None:
                d = RI.describe(conn, it, rows)
                render_source(it["first_row"], (d["source"] or {}).get("ref"), (d["source"] or {}).get("snippet"))
            elif isinstance(sel, str) and sel.startswith("line:"):
                r = next((x for x in vrows if f"line:{x['quote_id']}" == sel), None)
                s = (r or {}).get("fields", {}).get("price", {}).get("source") or {}
                render_source(r, s.get("ref"), s.get("snippet"))
            else:
                src = vrows[0] if vrows else None
                s = (src or {}).get("fields", {}).get("price", {}).get("source") or {}
                render_source(src, s.get("ref"), s.get("snippet"))
    _show_dialog(conn)
