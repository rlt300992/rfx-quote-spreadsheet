"""The only custom CSS in the app: Streamlit has no option for row height or link-style buttons. Everything is keyed on
widget keys (st-key-*) so it cannot leak onto other tabs."""
import streamlit as st

CSS = """
<style>
/* Preview links: tertiary buttons that read as blue text links (main file, evidence files, other attachments) */
[class*="st-key-pv_"] button { padding: 0; min-height: 0; }
[class*="st-key-pv_"] button p { color: #1a73e8; font-size: 0.9rem; }
[class*="st-key-pv_"] button:hover p { text-decoration: underline; }
[class*="st-key-pv_oth_"] button, [class*="st-key-pv_main_"] button { justify-content: flex-end; }
/* the Vendor-response row looks and measures like the two expanders below it (same 40px box, same border) */
[class*="st-key-mainrow_"] { border: 1px solid rgba(128,128,128,.3); border-radius: 0.5rem; padding: 0 1rem; height: 40px;
                             min-height: 40px; justify-content: center; overflow: hidden; }
[class*="st-key-mainrow_"] p { margin: 0; }
/* questionnaire table: one thin native divider between rows, tight spacing */
[class*="st-key-qtable_"] { gap: 0.4rem; }
[class*="st-key-qtable_"] hr { margin: 0; border-color: rgba(128,128,128,.25); }
[class*="st-key-qtable_"] p { margin-bottom: 0; }
[class*="st-key-mainrow_"] [data-testid="stColumn"] { display: flex; justify-content: center; }
/* fields filled by the co-pilot get a subtle green left edge */
[class*="st-key-ai_"] { border-left: 3px solid #3fb27f; padding-left: 0.5rem; }
/* Review & Reconcile: the source viewer stays in view while the queue scrolls; the selected item has an amber edge */
[data-testid="stColumn"]:has([class*="st-key-rv_viewer"]) { position: sticky; top: 3.5rem; align-self: flex-start; }
[class*="st-key-rvitem_sel_"] { border-color: #f2b01e !important; box-shadow: 0 0 0 1px #f2b01e; }
[class*="st-key-rvform_"] { background: rgba(128,128,128,.08); }
</style>
"""


def inject() -> None:
    st.html(CSS)
