"""Bid Analysis: the recommended award first, then four scenarios, risks, the comparison grid, charts and qualification.

Everything shown comes from award_scenario and the comparison views (core.bid_view); nothing here awards or commits."""
from __future__ import annotations

import html
import io

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from agent.tools import ToolContext, propose_award
from core import bid_view as B
from core import review_items as RI
from core.queries import comparison_grid
from core.stage import fmt_date
from ui.style import inject

VENDOR_COLOURS = {"V1": "#4c8bf5", "V2": "#b57bee", "V3": "#f2b01e", "V4": "#2bc6b4", "V5": "#8b949e"}
PLIES = {"3-ply": 3, "5-ply": 5, "7-ply": 7}
FILTERS = ["All plies", "3-ply", "5-ply", "7-ply", "Close calls (<2%)", "At-risk lines", "☆ Starred vendors"]
QUAL_BADGE = {"qualified": "green", "pending": "orange", "disqualified": "red"}


# ------------------------------------------------------------------ callbacks
def _select(key: str) -> None:
    st.session_state["bid_scn"] = key


def _use(conn, sc: dict) -> None:
    """Stage the scenario as the proposed award (nothing is committed) and go to Analyst & Award."""
    ctx = ToolContext(conn)
    try:
        propose_award(ctx, chat_prompt=f"Staged from Bid Analysis: {sc['title']}", basis="evaluated", **sc["params"])
        st.session_state["proposal"] = ctx.proposal
    finally:
        ctx.close()
    st.session_state["main_tab"] = "Analyst & Award"


def _goto(target: str, vendor: str | None, sel: str | None = None) -> None:
    if target == "review":
        st.session_state["rv_vendor"] = vendor
        st.session_state["rv_sel"] = sel
        st.session_state["main_tab"] = "Review & Reconcile"
    else:
        st.session_state["main_tab"] = "Responses"
    st.session_state.pop("_bid_dialog", None)


def _ask(line_no: int) -> None:
    st.session_state["analyst_prefill"] = B.ASK_TEXT.format(n=line_no)
    st.session_state["main_tab"] = "Analyst & Award"
    st.session_state.pop("_bid_dialog", None)


def _close() -> None:
    st.session_state.pop("_bid_dialog", None)


def _star(code: str) -> None:
    s = set(st.session_state.get("bid_stars", set()))
    s ^= {code}
    st.session_state["bid_stars"] = s


# ------------------------------------------------------------------ recommended award + scenarios
def _recommended(sc: dict, n_lines: int) -> None:
    t = sc["totals"]
    with st.container(key="rr_card_recommended"):
        st.caption(f"RECOMMENDED AWARD · {sc['title']}")
        st.markdown(B.headline(sc, n_lines))
        k = st.columns(5)
        k[0].metric("Annual value (evaluated)", B.fmt_inr(t.get("total_value_inr")), help="The ranking price: landed price plus one-time charges and payment cost.")
        sav = t.get("saving_vs_ly_inr", 0.0)
        k[1].metric("Landed vs last year", B.fmt_inr(abs(sav)), delta=f"{'cheaper' if sav >= 0 else 'dearer'} than last year", delta_arrow="down" if sav >= 0 else "up", delta_color="normal" if sav >= 0 else "inverse",
                    help="Like with like: this year's landed price against last year's landed price, on the same lines.")
        k[1].caption(f"{B.fmt_inr(t.get('total_landed_inr'))} vs {B.fmt_inr(t.get('ly_value_inr'))} · landed vs landed")
        won = t.get("lines_awarded", 0)
        k[2].metric("Lines with a winner", f"{won} / {n_lines}")
        if won < n_lines:
            k[2].markdown(f":red[{n_lines - won} line{'s' if n_lines - won != 1 else ''} have no winner]")
        else:
            k[2].caption("every line covered")
        k[3].metric("On not-firm prices", B.fmt_inr(t.get("value_on_not_firm_prices_inr", 0)))
        k[3].caption(f"{t.get('lines_on_not_firm_prices', 0)} lines" + (f" · kraft-linked ({', '.join(t['not_firm_vendors'])})" if t.get("not_firm_vendors") else ""))
        k[4].metric("Vendors used", len(t.get("lines_by_vendor", {})))
        k[4].caption(", ".join(sorted(t.get("lines_by_vendor", {}))))


def _scenario_cards(conn, scs: list[dict], sel: str, n_lines: int) -> None:
    st.markdown("**Scenarios** :gray[· pick one to see it in the grid. Custom what-ifs: ask the analyst.]")
    cols = st.columns(len(scs))
    for col, sc in zip(cols, scs):
        t = sc["totals"]
        on = sc["key"] == sel
        with col.container(key=f"rr_card_bidsc_{'sel_' if on else ''}{sc['key']}"):
            st.markdown(f"{'● ' if on else ''}**{sc['title']}**")
            st.caption(sc["sub"])
            sav = t.get("saving_vs_ly_inr", 0.0)
            covered = t.get("lines_awarded", 0)
            for lab, val in (("Annual value", f"**{B.fmt_inr(t.get('total_value_inr'))}**"),
                             ("Landed vs last year", f":{'green' if sav >= 0 else 'red'}[{'↓' if sav >= 0 else '↑'} {B.fmt_inr(abs(sav))}]"),
                             ("Lines covered", (f":red[**{covered}/{n_lines}**]" if covered < n_lines else f"**{covered}/{n_lines}**")),
                             ("Not-firm value", f":orange[**{B.fmt_inr(t.get('value_on_not_firm_prices_inr', 0))}**]" if t.get("value_on_not_firm_prices_inr") else "**₹0**"),
                             ("Vendors", f"**{', '.join(sorted(t.get('lines_by_vendor', {})))}**")):
                r = st.columns([1.3, 1.1])
                r[0].caption(lab)
                r[1].markdown(val)
            if on:
                st.button("Use this scenario → Analyst & Award", key=f"bid_use_{sc['key']}", type="primary", width="stretch", on_click=_use, args=(conn, sc))
            else:
                st.button("Show in grid", key=f"bid_show_{sc['key']}", width="stretch", on_click=_select, args=(sc["key"],))


# ------------------------------------------------------------------ the grid
BRIDGE_JS = """<script>(function(){
if(window.__bidBridge){return;} window.__bidBridge=true;
function send(v){const el=document.querySelector('input[aria-label="bid_bridge"]'); if(!el){return;}
 const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set; set.call(el, v+'|'+Date.now());
 el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true}));}
document.addEventListener('click',function(e){
 const c=e.target.closest('td[data-cell]'); if(c){send('cell|'+c.dataset.cell);return;}
 const a=e.target.closest('td[data-ask]'); if(a){send('ask|'+a.dataset.ask);return;}
 const st=e.target.closest('[data-star]'); if(st){send('star|'+st.dataset.star);return;}});
})();</script>"""

QUAL_CHIP = {"qualified": "green", "pending": "amber", "disqualified": "red"}


def _grid_html(model: dict, strip: list[dict], rows_idx: list[int], vendors: list[str], stars: set, metric: str) -> str:
    """The grid as one HTML table: vendor header with chip, lines quoted and terms; a tag on a second line under the price (10.5px)."""
    e = html.escape
    sv = {v["code"]: v for v in strip}
    th = ""
    for v in vendors:
        x = sv[v]
        extra = " · ".join(t for t in (x["days"], "not firm" if x["not_firm"] else "") if t)
        th += (f"<th class='r'><div class='vh'><span class='star' data-star='{v}'>{'★' if v in stars else '☆'}</span>{e(v)} · {e(x['name'])}</div>"
               f"<div class='vs'><span class='rr-chip {QUAL_CHIP.get(x['qual'], 'grey')} sm'>{e(x['qual'].capitalize())}</span> {x['lines']}/{x['n_lines']} lines</div>"
               f"<div class='vt'>{e(extra) or '&nbsp;'}</div></th>")
    head = f"<thead><tr><th>#</th><th>Line</th><th class='r'>Annual qty</th><th class='r'>Last yr</th>{th}<th>Winner · vs last yr</th><th></th></tr></thead>"
    body = ""
    for i in rows_idx:
        r = model["df"].iloc[i]
        ln = int(r["#"])
        cells = ""
        for v in vendors:
            c = model["cells"][i][v]
            tag = f"<div class='tag'>{e(c['tag'])}</div>" if c["tag"] else ""
            dot = "<span class='dot'></span>" if c["dot"] else ""
            cells += f"<td class='c {c['cls']}' data-cell='{ln}|{v}'><span class='p'>{e(c['p'])}</span>{dot}{tag}</td>"
        win = str(r["Winner · vs last yr"])
        close = ""
        if " · close: " in win:
            win, who = win.split(" · close: ")
            close = f"<span class='rr-chip amber sm'>close: {e(who)}</span>"
        ly = "" if pd.isna(r["Last yr"]) else f"{r['Last yr']:.2f}"
        body += (f"<tr><td class='n'>{ln}</td><td>{e(str(r['Line']))}</td><td class='r'>{int(r['Annual qty']):,}</td><td class='r'>{ly}</td>{cells}"
                 f"<td class='w'>{e(win)}{close}</td><td class='ask' data-ask='{ln}'>💬</td></tr>")
    return f"<div class='bg-wrap'><table class='bg'>{head}<tbody>{body}</tbody></table></div>"


def _grid(conn, g: pd.DataFrame, sc: dict, n_lines: int, qual: pd.DataFrame) -> None:
    strip = B.vendor_strip(g, n_lines)
    c = st.columns([1.9, 3.1, 4.6, 0.9], vertical_alignment="center")
    basis_label = c[0].segmented_control("Price", ["Evaluated", "Landed"], default="Evaluated", key="bid_basis", label_visibility="collapsed") or "Evaluated"
    metric = c[1].segmented_control("Show", ["₹ / pc", "% above cheapest", "Annual value"], default="₹ / pc", key="bid_metric", label_visibility="collapsed") or "₹ / pc"
    flt = c[2].pills("Filter", FILTERS, default="All plies", key="bid_filter", label_visibility="collapsed") or "All plies"
    basis = "evaluated" if basis_label == "Evaluated" else "landed"
    mm = {"₹ / pc": "₹/pc"}.get(metric, metric)
    model = B.grid_model(g, sc["params"], basis, mm)
    stars = st.session_state.get("bid_stars", set())
    keep = []
    for i, m in enumerate(model["meta"]):
        if flt in PLIES and m["ply"] != PLIES[flt]:
            continue
        if flt.startswith("Close") and not m["close"]:
            continue
        if flt == "At-risk lines" and not m["at_risk"]:
            continue
        keep.append(i)
    vendors = model["vendors"]
    if flt.startswith("☆") and stars:
        vendors = [v for v in vendors if v in stars]
    shown = model["df"].iloc[keep].reset_index(drop=True)[["#", "Line", "Annual qty", "Last yr"] + vendors + ["Winner · vs last yr"]]
    c[3].download_button("⬇ Excel", data=lambda: _grid_xlsx(shown), file_name="bid_grid.xlsx", key="bid_xlsx",
                         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    # the click bridge: the table's cells write "cell|line|vendor" into this (hidden) input, which reruns the app
    with st.container(key="bid_bridge"):
        bridge = st.text_input("bid_bridge", key="bid_bridge_in", label_visibility="collapsed")
    if not keep:
        st.info("No lines match this filter.")
    else:
        st.html(_grid_html(model, strip, keep, vendors, stars, metric) + BRIDGE_JS, unsafe_allow_javascript=True)
    st.caption("**Green** = cheapest eligible price in this scenario · red = the highest bid on the line · grey = can't win in this scenario · "
               "● price not firm · Not quoted is never a price. Click any price for how it was built up. ₹ per piece, GST excluded.")
    _on_bridge(bridge, [model["meta"][i] for i in keep], vendors)


def _on_bridge(value: str, meta: list[dict], vendors: list[str]) -> None:
    """Handle a click from the table: 'cell|line|vendor|ts' opens the build-up, 'ask|line|ts' pre-fills the analyst, 'star|V|ts' toggles a star."""
    if not value or st.session_state.get("bid_last_bridge") == value:
        return
    st.session_state["bid_last_bridge"] = value
    parts = value.split("|")
    if parts[0] == "cell" and len(parts) >= 3:
        st.session_state["_bid_dialog"] = ("cell", int(parts[1]), parts[2])
        st.rerun()
    elif parts[0] == "ask" and len(parts) >= 2:
        _ask(int(parts[1]))
        st.rerun()
    elif parts[0] == "star" and len(parts) >= 2:
        _star(parts[1])
        st.rerun()


def _grid_xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
        df.drop(columns=["Ask"]).to_excel(w, sheet_name="Bid grid", index=False)
    return buf.getvalue()


# ------------------------------------------------------------------ side panel
def _cell_dialog(conn, g: pd.DataFrame, sc: dict, qual: pd.DataFrame, line_no: int, vendor: str) -> None:
    grp = g[g["line_no"] == line_no]
    r = grp[grp["vendor_code"] == vendor].iloc[0]
    model = B.grid_model(g, sc["params"], "evaluated")
    m = next(x for x in model["meta"] if x["line_no"] == line_no)
    won = m["winner"] == vendor
    st.caption(f"{r['description']} · {int(r['ply'])}-ply · {int(r['annual_qty']):,} a year")
    if r["quote_status"] != "quoted" or pd.isna(r["evaluated_inr_pc"]):
        st.warning(f"{vendor} did not quote this line. It is shown as Not quoted, never as ₹0, and {vendor} can't win it.")
    else:
        if won:
            st.success(f"**Winner:** {vendor} is the cheapest eligible price on this line in this scenario.")
        else:
            st.error(f"**Can't win:** {B.cant_win_reason(r, qual, sc['title'])}")
        bu = B.build_up(r)
        names = [p[0] for p in bu["parts"]] + ["= Evaluated"]
        vals = [p[1] for p in bu["parts"]] + [bu["evaluated"]]
        fig = go.Figure(go.Bar(x=vals, y=names, orientation="h", text=[f"{v:.3f}" for v in vals], textposition="outside",
                               marker_color=["#4c8bf5", "#f2b01e", *(["#f2b01e"] * (len(vals) - 3)), "#2e9e5b"][:len(vals)], cliponaxis=False))
        fig.update_layout(height=60 + 46 * len(vals), margin=dict(l=0, r=40, t=10, b=0), xaxis=dict(visible=False), yaxis=dict(autorange="reversed"), showlegend=False)
        st.markdown("**Price build-up** :gray[(₹ per piece)]")
        st.plotly_chart(fig, width="stretch", key=f"bid_bu_{line_no}_{vendor}")
        for (lab, _v, note) in bu["parts"]:
            if note:
                st.caption(f"**{lab.replace('+ ', '')}:** {note}")
        with st.container(border=True):
            if m["winner"] and not won:
                diff = (r["evaluated_inr_pc"] / m["winner_price"] - 1) * 100
                st.markdown(f"Cheapest eligible on this line: **{m['winner']} ₹{m['winner_price']:.2f}** ({vendor} would be {abs(diff):.1f}% {'cheaper' if diff < 0 else 'dearer'})")
            elif m["winner"]:
                st.markdown(f"Cheapest eligible on this line: **{m['winner']} ₹{m['winner_price']:.2f}**")
            else:
                st.markdown("No eligible winner on this line in this scenario.")
            row = sc["df"][sc["df"]["line_no"] == line_no].iloc[0]
            if pd.notna(row["backup_vendor"]) and row["backup_vendor"]:
                nf = bool(grp[grp["vendor_code"] == row["backup_vendor"]].iloc[0]["price_not_firm"])
                st.markdown(f"Backup: **{row['backup_vendor']} ₹{row['backup_price_inr_pc']:.2f}**" + (" · price not firm" if nf else ""))
            ly = r["ly_landed_inr_pc"]
            lyv = conn.execute("SELECT ly_vendor_code FROM v_ly_baseline WHERE line_no=?", (line_no,)).fetchone()
            if pd.notna(ly):
                st.markdown(f"Last year: **₹{ly:.2f}**" + (f" ({lyv[0]})" if lyv and lyv[0] else ""))
    b = st.columns([1.3, 1.3, 1], vertical_alignment="center")
    sel = None
    if r["quote_status"] == "quoted":
        sel = next((i["id"] for i in RI.build_items(conn) if r["quote_id"] in i["quotes"]), f"line:{int(r['quote_id'])}")
    b[0].button("See source in Review ↗", key="bid_dlg_src", on_click=_goto, args=("review", vendor, sel), disabled=r["quote_status"] != "quoted", width="stretch")
    b[1].button("💬 Ask about this line", key="bid_dlg_ask", on_click=_ask, args=(line_no,), width="stretch")


# ------------------------------------------------------------------ charts
def _charts(g: pd.DataFrame, sc: dict) -> None:
    st.markdown("### Charts")
    a, b = st.columns([1, 1.6])
    by = sc["totals"].get("by_vendor", {})
    names = {v["code"]: f"{v['code']} · {v['name']}" for v in B.vendor_strip(g, g["line_no"].nunique())}
    with a.container(key="rr_card_chart_a"):
        st.markdown("**Award value by vendor** :gray[· this scenario]")
        items = sorted(by.items(), key=lambda kv: kv[1])
        fig = go.Figure(go.Bar(x=[v / 1e7 for _, v in items], y=[names.get(k, k) for k, _ in items], orientation="h",
                               text=[B.fmt_inr(v) for _, v in items], textposition="outside", marker_color="#3fa66b", cliponaxis=False))
        fig.update_layout(height=60 + 60 * max(len(items), 1), margin=dict(l=0, r=70, t=5, b=0), xaxis=dict(visible=False, range=[0, max([v / 1e7 for _, v in items] or [1]) * 1.3]))
        st.plotly_chart(fig, width="stretch", key="bid_chart_value")
        st.caption("Disqualified vendors are never shown as winners or ranked on savings.")
    with b.container(key="rr_card_chart_b"):
        st.markdown("**Price spread per line** :gray[· each dot is a vendor's price vs the winner (green line); below the line = cheaper but can't win]")
        model = B.grid_model(g, sc["params"], "evaluated")
        win = {m["line_no"]: m["winner_price"] for m in model["meta"]}
        fig = go.Figure()
        fig.add_hline(y=0, line_color="#3fb27f", line_width=2)
        fig.add_annotation(xref="paper", x=0.0, y=0, text="winner", showarrow=False, xanchor="left", yshift=11, font=dict(color="#3fb27f", size=11))
        for v in model["vendors"]:
            sub = g[(g["vendor_code"] == v) & (g["quote_status"] == "quoted") & g["evaluated_inr_pc"].notna()]
            pts = [(int(r["line_no"]), (r["evaluated_inr_pc"] / win[int(r["line_no"])] - 1) * 100, int(r["line_no"]) in win and False) for _, r in sub.iterrows()
                   if win.get(int(r["line_no"]))]
            dq = (sub["qual_status"] == "disqualified").any() if len(sub) else False
            xs = [p[0] for p in pts]
            ys = [max(-15, min(25, p[1])) for p in pts]
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", name=f"{v} (disqualified)" if dq else v,
                                     marker=dict(size=7, color="#8b949e" if dq else VENDOR_COLOURS.get(v, "#999")),
                                     hovertemplate=f"{v} line %{{x}}: %{{y:+.1f}}%<extra></extra>"))
        wx = [m["line_no"] for m in model["meta"] if m["winner"]]
        wc = [VENDOR_COLOURS.get(m["winner"], "#999") for m in model["meta"] if m["winner"]]
        fig.add_trace(go.Scatter(x=wx, y=[0] * len(wx), mode="markers", name="winner", marker=dict(size=12, color=wc, line=dict(color="white", width=2)), hoverinfo="skip"))
        fig.update_layout(height=330, margin=dict(l=10, r=10, t=5, b=0), yaxis=dict(range=[-16, 26], zeroline=False, tickvals=[-15, -10, -5, 5, 10, 15, 20, 25], ticktext=["−15%", "−10%", "−5%", "+5%", "+10%", "+15%", "+20%", "+25%"]), xaxis=dict(title="line number"),
                          legend=dict(orientation="h", y=-0.25))
        st.plotly_chart(fig, width="stretch", key="bid_chart_spread")
        st.caption("x = line number · capped at −15% / +25%")


# ------------------------------------------------------------------ qualification
def _qualification(conn) -> None:
    rows = B.qualification_rows(conn)
    n = {s: sum(r["status"] == s for r in rows) for s in ("qualified", "pending", "disqualified")}
    chips = " ".join(f":{QUAL_BADGE[s]}-badge[{c} {s}]" for s, c in n.items() if c)
    with st.expander(f"**Qualification & documents**  {chips}"):
        st.caption(f"Checked on the award date, {fmt_date(rows[0]['award_date']) if rows else ''}")
        h = st.columns([1.4, 1.2, 4, 3.6, 1.5])
        for c, t in zip(h, ("Vendor", "Status", "Knock-out questions", "Documents", "")):
            c.caption(t)
        for r in rows:
            c = st.columns([1.4, 1.2, 4, 3.6, 1.5], vertical_alignment="center")
            c[0].markdown(f"**{r['code']}** · {r['name']}")
            c[1].markdown(f":{QUAL_BADGE.get(r['status'], 'gray')}-badge[{r['status'].capitalize()}]")
            c[2].write(r["knockout"])
            c[3].write(r["docs"])
            c[4].button("Open in Responses ↗", key=f"bid_resp_{r['code']}", type="tertiary", on_click=_goto, args=("responses", r["code"]))


# ------------------------------------------------------------------ the tab
def render(conn) -> None:
    inject()
    g = comparison_grid(conn)
    if g.empty:
        st.info("No vendor quotes loaded yet. Extract the replies on the Responses tab first.")
        return
    n_lines = int(g["line_no"].nunique())
    scs = B.run_scenarios(conn, g)
    sel = st.session_state.get("bid_scn", "A")
    sc = next((s for s in scs if s["key"] == sel), scs[0])
    qual = pd.read_sql_query("SELECT * FROM v_vendor_qualification", conn)

    _recommended(sc, n_lines)
    note = B.trade_off_note(sc, scs[0], n_lines)
    if note:
        st.warning(note, icon="⚠️")
    _scenario_cards(conn, scs, sc["key"], n_lines)
    items = RI.build_items(conn)
    chips = B.risk_chips(conn, g, sc, items)
    if chips:
        with st.container(horizontal=True):
            for i, c in enumerate(chips):
                st.button(f"{c['icon']} {c['label']} →", key=f"bid_risk_{i}", on_click=_goto, args=(c["target"], c["vendor"]))
    _grid(conn, g, sc, n_lines, qual)
    _charts(g, sc)
    _qualification(conn)
    spec = st.session_state.get("_bid_dialog")
    if spec and spec[0] == "cell":
        st.dialog(f"Line {spec[1]} · {spec[2]} {B.short_name(g.loc[g['vendor_code'] == spec[2], 'vendor_name'].iloc[0])}", width="large", on_dismiss=_close)(_cell_dialog)(conn, g, sc, qual, spec[1], spec[2])
