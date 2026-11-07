"""Kill the Quote Spreadsheet: Streamlit entry (P0 skeleton)."""
import csv
import hmac
from pathlib import Path

import pandas as pd
import streamlit as st

from core.db import connect, init_db
from core.queries import comparison_grid, evaluated_pivot, pending_confirmation_count, winner_cells
from core.seed import seed_if_empty
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
               "Green = cheapest awardable vendor on the line. Blank = not quoted or no price. Read-only.")
    def paint(df):
        css = pd.DataFrame("", index=df.index, columns=df.columns)
        for ln, v in wins:
            if ln in css.index and v in css.columns:
                css.loc[ln, v] = "background-color:#c8e6c9;font-weight:bold"
        return css

    styled = pivot.style.format("{:.2f}", na_rep="").apply(paint, axis=None)
    st.dataframe(styled, width="stretch", height=600)
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
            if name == "Bid Analysis":
                bid_analysis_tab(conn)
            else:
                st.info(f"{name}: coming in a later phase.")


main()
