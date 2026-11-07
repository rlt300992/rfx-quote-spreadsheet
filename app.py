"""Kill the Quote Spreadsheet: Streamlit entry. Passcode gate, global banner, five tabs.

Page code lives in ui/; business logic lives in core/, extract/, agent/, copilot/ and export/.
"""
import hmac
from pathlib import Path

import streamlit as st

from core.db import connect, init_db
from core.queries import pending_confirmation_count
from core.seed import seed_if_empty
from extract.gemini_client import get_secret, load_config, ping
from ui import analyst, bid, responses, review

ROOT = Path(__file__).parent
st.set_page_config(page_title="Kill the Quote Spreadsheet", layout="wide")

TABS = ["Create RFQ", "Responses", "Review & Reconcile", "Bid Analysis", "Analyst & Award"]


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


@st.cache_resource
def get_conn():
    """One SQLite connection per app process; creates tables/views and seeds on first start."""
    conn = connect()
    init_db(conn)
    seed_if_empty(conn)
    return conn


def sidebar(conn, cfg: dict) -> None:
    rfq = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
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


def main() -> None:
    if not gate():
        return
    cfg = load_config()
    conn = get_conn()
    sidebar(conn, cfg)
    n = pending_confirmation_count(conn)
    st.markdown(f"**{n} values awaiting confirmation could change the award**")
    for tab, name in zip(st.tabs(TABS), TABS):
        with tab:
            if name == "Responses":
                responses.render(conn)
            elif name == "Review & Reconcile":
                review.render(conn)
            elif name == "Bid Analysis":
                bid.render(conn)
            elif name == "Analyst & Award":
                analyst.render(conn)
            else:
                st.info(f"{name}: coming in a later phase.")


main()
