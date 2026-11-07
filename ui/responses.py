"""Responses tab: one card per vendor with format, received time (late flagged), files, extraction status and counts."""
from __future__ import annotations

import streamlit as st

from core.queries import vendor_cards
from extract.extract import ingest_all, ingest_vendor, load_manifest


def render(conn) -> None:
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
