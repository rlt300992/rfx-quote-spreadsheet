"""Kill the Quote Spreadsheet: Streamlit entry (P0 skeleton)."""
import csv
import hmac
from pathlib import Path

import pandas as pd
import streamlit as st

from core.db import connect, init_db
from agent.tools import award_scenario
from core.queries import (comparison_grid, evaluated_pivot, not_firm_cells, pending_confirmation_count,
                          vendor_cards, winner_cells)
from core.seed import seed_if_empty
from extract.extract import ingest_all, ingest_vendor, load_manifest
from extract.gemini_client import get_secret, load_config, ping

ROOT = Path(__file__).parent
st.set_page_config(page_title="Kill the Quote Spreadsheet", layout="wide")


def gate() -> bool:
    if st.session_state.get("authed"):
        return True
    expected = get_secret("APP_PASSCODE")
    st.title("Kill the Quote Spreadsheet")
    if not expected:
        st.error("APP_PASSCODE is not configured.")
        return False
    code = st.text_input("Passcode", type="password")
    if code:
        if hmac.compare_digest(code, expected):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong passcode.")
    return False


def rfq_row() -> dict:
    with open(ROOT / "data/seed/rfqs.csv", encoding="utf-8") as f:
        return next(csv.DictReader(f))


def sidebar(cfg: dict) -> None:
    rfq = rfq_row()
    with st.sidebar:
        st.header(rfq["title"])
        st.caption("Buyer: Sahyadri Consumer Products Ltd")
        st.write(f"**USD/INR:** ₹{cfg['fx']['USD_INR']} ({cfg['fx']['date']})")
        st.write(f"**Cost of funds:** {cfg['cost_of_funds_pct']}%")
        st.write(f"**Standard payment:** {rfq['std_payment_terms_days']} days")
        st.write(f"**Response due:** {rfq['response_due_date']}")
        if st.button("Test Gemini"):
            try:
                st.success(f"Gemini replied OK ({ping()})")
            except Exception as e:  # show the real error
                st.error(f"Gemini failed: {e}")


@st.cache_resource
def get_conn():
    """One SQLite connection per app process; creates tables/views and seeds on first start."""
    conn = connect()
    init_db(conn)
    seed_if_empty(conn)
    return conn


def responses_tab(conn) -> None:
    """One card per vendor: format, received time (late flagged), files, extraction status, lines quoted."""
    manifest = load_manifest()
    top = st.columns([1, 3])
    if top[0].button("Extract all", type="primary", help="Extracts vendors that have no current extraction (cache first, Gemini only for uncached files). Vendors already extracted are left alone; use Re-run on a vendor to refresh it."):
        with st.status("Extracting all vendor replies ...", expanded=True) as status:
            try:
                for r in ingest_all(conn, progress=st.write, only_missing=True):
                    c = r["calls"]
                    st.write(f"{r['vendor']}: {r['quotes']} lines, {c['api']} Gemini calls, {c['cache']} from cache")
                status.update(label="Extraction complete", state="complete")
            except Exception as e:  # show the real error, never swallow it
                status.update(label="Extraction failed", state="error")
                st.error(f"Extraction failed: {e}")
        st.rerun()
    top[1].caption("Values come from real Gemini extraction (cached by file hash, prompt version and model). "
                   "Re-run forces a fresh call and replaces that vendor's current values; confirmations you made on the old values are not carried over.")
    entries = {m["vendor_code"]: m for m in manifest}
    for card in vendor_cards(conn, manifest):
        with st.container(border=True):
            head = st.columns([3, 2, 2, 2])
            tier = f" ({card['msme']} enterprise)" if card["msme"] and card["msme"] != "none" else ""
            head[0].markdown(f"**{card['vendor_code']} · {card['name']}**{tier}")
            head[1].markdown(f"Format: **{card['format'] or 'n/a'}**")
            late = " :red[**LATE**]" if card["late"] else ""
            head[2].markdown(f"Received: {(card['received_at'] or 'n/a')[:16].replace('T', ' ')}{late}")
            head[3].markdown(f"Status: **{card['status']}**")
            if card["extracted"]:
                st.progress(card["lines_quoted"] / card["lines_total"],
                            text=f"{card['lines_quoted']} of {card['lines_total']} lines quoted"
                                 + (f" · {card['lines_not_quoted']} not quoted" if card["lines_not_quoted"] else "")
                                 + (f" · {card['lines_unmatched']} unmatched" if card["lines_unmatched"] else ""))
                st.caption(f"Pipeline: {card['path']}")
            st.caption("Files: " + ", ".join(card["files"]))
            if card["clarification_draft"]:
                with st.expander(f"Draft clarification email ({card['clarification_status']}; sending is stubbed)"):
                    st.text(card["clarification_draft"])
            if st.button("Re-run extraction", key=f"rerun_{card['vendor_code']}", help="Fresh Gemini call, ignores the cache"):
                with st.spinner(f"Re-extracting {card['vendor_code']} ..."):
                    try:
                        r = ingest_vendor(conn, entries[card["vendor_code"]], force=True)
                        st.success(f"{r['quotes']} lines extracted ({r['calls']['api']} Gemini calls)")
                    except Exception as e:
                        st.error(f"Re-run failed: {e}")
                st.rerun()


def bid_analysis_tab(conn) -> None:
    """Read-only line x vendor grid of evaluated INR/pc from v_comparison_grid (P3 adds the drill-downs)."""
    g = comparison_grid(conn)
    if g.empty:
        st.info("No vendor quotes loaded yet. Extraction arrives in P2. To preview the grid with test data run "
                "`python -m tests.populate_dev_db`, then start the app with the env var RFX_DB=rfx_dev.db.")
        return
    pivot = evaluated_pivot(conn)
    wins = winner_cells(conn)
    st.caption("Evaluated INR per piece (landed + one-time charges + payment-terms loading, GST excluded). "
               "Green = cheapest awardable vendor on the line. Blank = not quoted or no price. "
               "⚠ = price is linked to raw-material (kraft) cost and is NOT firm. Read-only.")
    nf = not_firm_cells(conn)
    scen = award_scenario(conn)
    tot = scen.attrs.get("totals", {})
    if tot.get("lines_on_not_firm_prices"):
        st.warning(f"⚠ {tot['lines_on_not_firm_prices']} of {tot['lines_awarded']} lines in the current best "
                   f"scenario rest on NOT-FIRM prices from {', '.join(tot['not_firm_vendors'])} "
                   f"(₹{tot['value_on_not_firm_prices_inr']:,.0f} of ₹{tot['total_value_inr']:,.0f}). "
                   "These prices can move with kraft paper cost; the escalation clause is not modelled.")
    def paint(df):
        css = pd.DataFrame("", index=df.index, columns=df.columns)
        for ln, v in wins:
            if ln in css.index and v in css.columns:
                css.loc[ln, v] = "background-color:#c8e6c9;font-weight:bold"
        return css

    shown = pivot.copy().astype(object)
    for ln in shown.index:
        for v in shown.columns:
            x = pivot.loc[ln, v]
            shown.loc[ln, v] = "" if pd.isna(x) else f"{x:.2f}" + (" ⚠" if (ln, v) in nf else "")
    st.dataframe(shown.style.apply(paint, axis=None), width="stretch", height=600)
    reasons = g[g["exclusion_reason"].notna()].groupby(["vendor_code", "exclusion_reason"]).size().reset_index(name="lines")
    with st.expander("Why quotes can't win (count of lines by reason)"):
        st.dataframe(reasons, hide_index=True)


def main() -> None:
    if not gate():
        return
    cfg = load_config()
    conn = get_conn()
    sidebar(cfg)
    n = pending_confirmation_count(conn)
    st.markdown(f"**{n} values awaiting confirmation could change the award**")
    names = ["Create RFQ", "Responses", "Review & Reconcile", "Bid Analysis", "Analyst & Award"]
    for tab, name in zip(st.tabs(names), names):
        with tab:
            if name == "Responses":
                responses_tab(conn)
            elif name == "Bid Analysis":
                bid_analysis_tab(conn)
            else:
                st.info(f"{name}: coming in a later phase.")


main()
