"""Kill the Quote Spreadsheet: Streamlit entry. Passcode gate, global banner, five tabs.

Page code lives in ui/; business logic lives in core/, extract/, agent/, copilot/ and export/.
"""
import hmac
import logging
from pathlib import Path

import streamlit as st

from core.db import connect, init_db
from core.queries import pending_confirmation_count
from core.seed import seed_if_empty
from extract.gemini_client import get_secret, load_config, ping
from ui import analyst, bid, responses, review, rfq

ROOT = Path(__file__).parent
log = logging.getLogger("rfx.app")
st.set_page_config(page_title="Kill the Quote Spreadsheet", layout="wide")

TABS = ["Create RFQ", "Responses", "Review & Reconcile", "Bid Analysis", "Analyst & Award"]


def gate() -> bool:
    if st.session_state.get("authed"):
        return True
    expected = get_secret("APP_PASSCODE")
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.write("")
        st.write("")
        with st.container(border=True):
            st.markdown("<h1 style='text-align:center;margin-bottom:0'>Kill the Quote Spreadsheet</h1>", unsafe_allow_html=True)
            st.markdown("<p style='text-align:center;font-size:1.05rem;margin-top:.4rem'>RFQ co-pilot: from messy vendor replies to a defensible award</p>",
                        unsafe_allow_html=True)
            st.markdown("<p style='text-align:center;color:grey;font-size:.9rem'>Prototype by Rohit Tambe · Aerchain product assignment</p>",
                        unsafe_allow_html=True)
            if not expected:
                st.error("APP_PASSCODE is not configured.")
            else:
                with st.form("passcode_form", border=False):
                    code = st.text_input("Passcode", type="password")
                    go = st.form_submit_button("Enter", type="primary", width="stretch")
                if go:
                    if code and hmac.compare_digest(code, expected):
                        st.session_state["authed"] = True
                        st.rerun()
                    else:
                        st.error("Wrong passcode.")
        st.markdown("<p style='text-align:center;color:grey;font-size:.75rem'>Demo data is fictional · Corrugated packaging, 5 vendors, 30 lines · © 2026 Rohit Tambe</p>",
                    unsafe_allow_html=True)
    return False


@st.cache_resource
def get_conn():
    """One SQLite connection per app process; creates tables/views and seeds on first start."""
    conn = connect()
    init_db(conn)
    seed_if_empty(conn)
    ensure_quotes(conn)
    return conn


def ensure_quotes(conn) -> None:
    """A fresh container has no rfx.db: build the quotes from the committed extraction cache (offline, 0 API calls)."""
    if conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] > 0:
        return
    from extract import cache
    if len(list(cache.CACHE_DIR.glob("*.json"))) == 0:
        log.warning("No quotes and no committed extraction cache; the app will start empty.")
        return
    from extract.extract import ingest_all
    with st.spinner("First start: loading the vendor replies from the committed extraction cache (no API calls) ..."):
        try:
            ingest_all(conn, draft=True, only_missing=True)
        except Exception:
            log.exception("Offline ingest from cache failed")


def sidebar(cfg: dict, rfq) -> None:
    with st.sidebar:
        st.markdown("**Sahyadri Consumer Products Ltd · Procurement**")
        with st.expander("Evaluation settings", expanded=False):
            st.write(f"**USD/INR:** ₹{cfg['fx']['USD_INR']} ({cfg['fx']['date']})")
            st.write(f"**Cost of funds:** {cfg['cost_of_funds_pct']}%")
            st.write(f"**Standard payment:** {rfq['std_payment_terms_days']} days")
        st.write("")
        if st.button("Test Gemini"):
            try:
                st.success(f"Gemini replied OK ({ping()})")
            except Exception as e:  # show the real error
                st.error(f"Gemini failed: {e}")


def status_label(status: str | None) -> str:
    """DB status -> chip text: draft is Draft, anything issued onwards is Published."""
    return "Draft" if (status or "draft") == "draft" else "Published"


def event_header(rfq) -> None:
    st.markdown(f"<h2 style='margin-bottom:0'>{rfq['title']}</h2>", unsafe_allow_html=True)
    chip = status_label(rfq["status"])
    st.caption(f"Response due {rfq['response_due_date']}  ·  :{'green' if chip == 'Published' else 'orange'}-badge[{chip}]")


def banner(n: int) -> None:
    if n > 0:
        st.warning(f"**{n} values awaiting confirmation.** These could change the award. Review them in Review & Reconcile.", icon="⚠️")
    else:
        st.success("All values confirmed, ready to award.")


def main() -> None:
    if not gate():
        return
    cfg = load_config()
    conn = get_conn()
    rfq_row = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    sidebar(cfg, rfq_row)
    event_header(rfq_row)
    try:
        n = pending_confirmation_count(conn)
    except Exception:
        log.exception("pending count failed")
        n = 0
    banner(n)
    renderers = {"Create RFQ": rfq.render, "Responses": responses.render, "Review & Reconcile": review.render,
                 "Bid Analysis": bid.render, "Analyst & Award": analyst.render}
    for tab, name in zip(st.tabs(TABS), TABS):
        with tab:
            try:
                renderers[name](conn)
            except Exception:  # one tab's failure must not break the others
                log.exception("Tab %s failed", name)
                st.warning(f"The {name} tab hit a problem and could not be shown. The other tabs are unaffected. Details are in the app log.")

main()
