"""Kill the Quote Spreadsheet: Streamlit entry (P0 skeleton)."""
import csv
import hmac
from pathlib import Path

import streamlit as st

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


def main() -> None:
    if not gate():
        return
    cfg = load_config()
    sidebar(cfg)
    names = ["Create RFQ", "Responses", "Review & Reconcile", "Bid Analysis", "Analyst & Award"]
    for tab, name in zip(st.tabs(names), names):
        with tab:
            st.info(f"{name}: coming in a later phase.")


main()
