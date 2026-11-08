"""Global look for every tab: layout widths and the design tokens of the mockups, injected once. Blocks are styled through the keys given to
st.container(key=...) (CSS class .st-key-<key>); no Streamlit-internal class names are used. Light values by default, dark values follow the
browser/Streamlit dark theme, so the app matches the mockups in either."""
import streamlit as st

CSS = """
<style>
:root{
  --rr-line:#d6d6d6; --rr-mut:#6b6b6b; --rr-dim:#8a8a8a; --rr-card:#ffffff; --rr-card2:#f6f6f6; --rr-sel:#e8e8e8; --rr-selb:#8a8a8a;
  --rr-amber:#8a6100; --rr-amberbg:#fff2cc; --rr-green:#1c7c43; --rr-greenbg:#dff3e6; --rr-red:#b3261e; --rr-redbg:#fde7e7;
  --rr-blue:#1f5fa8; --rr-bluebg:#e3f0fc; --rr-ambersel:#d9a300; --rr-okline:#2f8f55; --rr-track:#e2e2e2; --rr-in:#f0f0f0;
}
@media (prefers-color-scheme: dark){
  :root{ --rr-line:#3a3a3a; --rr-mut:#a3a3a3; --rr-dim:#7c7c7c; --rr-card:#1f1f1f; --rr-card2:#181818; --rr-sel:#3a3a3a; --rr-selb:#666;
    --rr-amber:#f5c542; --rr-amberbg:#3a3014; --rr-green:#5fd38d; --rr-greenbg:#173a24; --rr-red:#ff8a8a; --rr-redbg:#3d1a1a;
    --rr-blue:#8cc4ff; --rr-bluebg:#132c45; --rr-ambersel:#f5c542; --rr-okline:#2f6b45; --rr-track:#333; --rr-in:#262730; }
}
/* ---- layout shared by all tabs */
section[data-testid="stSidebar"] { width: 210px !important; min-width: 210px !important; }
section[data-testid="stSidebar"] > div:first-child { width: 210px !important; }
[data-testid="stMainBlockContainer"], .block-container { padding-left: 1.5rem !important; padding-right: 1.5rem !important; padding-top: 3.5rem !important; max-width: none; }

/* ---- Review & Reconcile blocks */
.rr-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;font-weight:600;white-space:nowrap}
.rr-chip.grey{background:var(--rr-sel);color:var(--rr-mut)} .rr-chip.green{background:var(--rr-greenbg);color:var(--rr-green)}
.rr-chip.amber{background:var(--rr-amberbg);color:var(--rr-amber)} .rr-chip.red{background:var(--rr-redbg);color:var(--rr-red)}
.rr-chip.blue{background:var(--rr-bluebg);color:var(--rr-blue)}
.rr-dot{width:10px;height:10px;border-radius:50%;display:inline-block;flex-shrink:0}
.rr-dot.red{background:#ff5c5c}.rr-dot.yellow{background:#f5c542}.rr-dot.green{background:#5fd38d}
.rr-r1{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:14px}
.rr-ttl{font-weight:700}.rr-fld{color:var(--rr-mut)}
.rr-kv{display:grid;grid-template-columns:120px 1fr;gap:3px 10px;margin:8px 0 2px;font-size:13.5px}
.rr-kv .k{color:var(--rr-mut)}
.rr-why{margin-top:6px;font-size:13.5px;line-height:1.45}
.rr-sub{margin-top:8px;font-size:12px;font-weight:700;color:var(--rr-mut);text-transform:uppercase;letter-spacing:.4px}
.rr-grp{font-size:12px;color:var(--rr-mut);font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin:14px 0 6px;display:flex;justify-content:space-between}
.rr-prog{height:6px;background:var(--rr-track);border-radius:3px;margin:6px 0 12px}.rr-prog i{display:block;height:6px;border-radius:3px;background:var(--rr-green)}
.rr-title{font-weight:700;font-size:16px}.rr-meta{color:var(--rr-mut);font-size:13px}

[class*="st-key-rr_card_"]{border:1px solid var(--rr-line);border-radius:10px;padding:11px 13px;background:var(--rr-card);gap:.3rem;margin-bottom:2px}
[class*="st-key-rr_card_sel_"]{border-color:var(--rr-ambersel);box-shadow:inset 3px 0 0 var(--rr-ambersel)}
[class*="st-key-rr_card_"] [data-testid="stHtml"], [class*="st-key-rr_card_"] [data-testid="stMarkdownContainer"] p{margin:0}
[class*="st-key-rr_acts_"]{margin-top:6px;align-items:center}
[class*="st-key-rr_acts_"] button{padding:4px 10px;min-height:0;font-size:12.5px;border-radius:8px;line-height:1.3}
[class*="st-key-rr_acts_"] button p{font-size:12.5px}
[class*="st-key-rr_linkA_"]{margin-left:auto}
[class*="st-key-rr_linkA_"] button, [class*="st-key-rr_linkB_"] button{border:none;background:transparent;color:var(--rr-blue);padding:4px 6px}
[class*="st-key-rr_linkA_"] button p, [class*="st-key-rr_linkB_"] button p{color:var(--rr-blue)}
[class*="st-key-rr_box_"]{border:1px solid var(--rr-line);border-radius:8px;padding:10px 12px;margin-top:8px;background:var(--rr-card2);font-size:12.8px}
[class*="st-key-rr_form_"]{border:1px solid var(--rr-line);border-radius:8px;padding:10px 12px;margin-top:8px;background:var(--rr-card2)}

[class*="st-key-rr_pill_"] button{border:1px solid var(--rr-line);border-radius:999px;padding:5px 12px;font-size:13px;background:transparent;min-height:0}
[class*="st-key-rr_pill_sel_"] button{background:var(--rr-sel);border-color:var(--rr-selb);font-weight:700}
[class*="st-key-rr_clean"] button{border:1px solid var(--rr-okline);color:var(--rr-green);padding:4px 12px;font-size:12.5px;min-height:0;background:transparent}
[class*="st-key-rr_clean"] button p{color:var(--rr-green);font-size:12.5px}
[class*="st-key-rv_view"] button{font-size:12.5px;padding:5px 12px;background:transparent !important;border-color:var(--rr-line) !important;color:inherit !important}
[class*="st-key-rv_view"] button[data-selected="true"]{background:var(--rr-sel) !important;border-color:var(--rr-selb) !important;font-weight:600}
[class*="st-key-rr_pills"]{justify-content:flex-start !important;gap:8px !important}
[class*="st-key-rr_pills"] > *{flex:0 0 auto !important;width:auto !important}
[class*="st-key-rr_pill_"]{width:auto !important;flex:0 0 auto !important;flex-grow:0 !important}
[class*="st-key-rr_group"]{margin:0}
[class*="st-key-rr_pane_r"]{position:sticky;top:12px;align-self:flex-start}
[data-testid="stColumn"]:has([class*="st-key-rr_pane_r"]){position:sticky;top:12px;align-self:flex-start}
[class*="st-key-rr_src"]{border:1px solid var(--rr-line);border-radius:10px;overflow:hidden;padding:0;gap:0}
.rr-src-hd{padding:10px 12px;font-size:13px}
.rr-src-ft{padding:8px 12px;font-size:12.5px;color:var(--rr-mut);border-top:1px solid var(--rr-line)}
.rr-src-bd{background:#fff;color:#111;max-height:640px;overflow:auto}
.rr-xl{border-collapse:collapse;table-layout:fixed;width:100%;font-size:11px;color:#111}
.rr-xl td{border:1px solid #d0d0d0;padding:3px 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;height:22px;font-size:11px}
.rr-xl tr.h td{background:#f1f1f1;color:#555;text-align:center}
.rr-xl td.n{width:30px;background:#f1f1f1;color:#555;text-align:center}
.rr-xl tr.hl td{background:#fff3b0;outline:2px solid #e07800}
.rr-doc{padding:14px 16px;color:#111;font-family:Arial,sans-serif;font-size:13.5px;line-height:1.6}
.rr-doc mark{background:#fff3b0;outline:2px solid #e07800}

/* ---- Analyst & Award */
.aa-hd{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:2px 2px 8px;border-bottom:1px solid var(--rr-line);margin-bottom:10px;font-size:14px}
.aa-card{background:var(--rr-card);border:1px solid var(--rr-line);border-radius:10px;padding:12px 14px;font-size:14px;margin-bottom:6px}
.aa-user{margin:8px 0 8px auto;max-width:90%;width:fit-content;background:#1d4f91;color:#fff;border-radius:12px;padding:9px 14px;font-size:14px}
.aa-head{font-weight:700;font-size:15px;line-height:1.4}.aa-ctx{margin-top:2px;font-size:13.5px;color:var(--rr-mut)}
.aa-badges{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 2px}
.aa-how{font-size:13px;line-height:1.7}.aa-how code{background:var(--rr-sel);padding:0 5px;border-radius:4px;font-size:12px}
.aa-steps{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:2px 0 10px}
.aa-step{padding:7px 4px;text-align:center;border-radius:8px;font-size:12.8px;background:var(--rr-card2);color:var(--rr-dim);border:1px solid transparent}
.aa-step.done{background:var(--rr-greenbg);color:var(--rr-green)}.aa-step.cur{background:var(--rr-sel);font-weight:700;color:inherit}
.aa-hint{font-size:14px;color:var(--rr-mut);line-height:1.5;margin-bottom:8px}.aa-meta{font-size:12.5px;color:var(--rr-mut);margin-bottom:6px}
.aa-grp{font-size:12px;color:var(--rr-mut);font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin:12px 0 6px}
.aa-kv{display:grid;grid-template-columns:minmax(150px,1fr) auto;gap:5px 14px;font-size:13.5px}.aa-kv .k{color:var(--rr-mut)}.aa-kv .v{text-align:right}
.aa-bar{display:grid;grid-template-columns:110px 1fr 70px;gap:8px;align-items:center;font-size:13px;margin:4px 0}.aa-bar i{display:block;height:9px;background:var(--rr-track);border-radius:5px}
.aa-bar b{display:block;height:9px;background:var(--rr-okline);border-radius:5px}.aa-bar .r{text-align:right;font-weight:600}
.aa-risk{font-size:13.5px;margin:3px 0}.aa-why{font-size:13.5px;line-height:1.45;margin-bottom:6px}
.aa-mail{background:var(--rr-card2);border:1px solid var(--rr-line);border-radius:8px;padding:10px 12px;font-size:13px;line-height:1.5}
[class*="st-key-aa_chat"]{border:1px solid var(--rr-line);border-radius:12px;padding:14px 16px;gap:.5rem}
[class*="st-key-aa_award"]{border:1px solid var(--rr-line);border-radius:12px;padding:14px 16px;gap:.5rem}
[data-testid="stColumn"]:has([class*="st-key-aa_award"]){position:sticky;top:12px;align-self:flex-start}
[class*="st-key-aa_ans_"]{border:1px solid var(--rr-line);border-radius:10px;background:var(--rr-card);padding:12px 14px;gap:.4rem}
[class*="st-key-aa_need_"]{border:1px solid var(--rr-ambersel);border-radius:8px;padding:8px 10px;background:var(--rr-amberbg);gap:.3rem}
[class*="st-key-aa_btns_"] button, [class*="st-key-aa_fu_"] button, [class*="st-key-aa_starters"] button, [class*="st-key-aa_actions"] button{padding:4px 10px;min-height:0;font-size:12.5px;border-radius:999px;line-height:1.3}
[class*="st-key-aa_actions"] button{border-radius:8px}
[class*="st-key-aa_btns_"] button p, [class*="st-key-aa_fu_"] button p, [class*="st-key-aa_starters"] button p, [class*="st-key-aa_actions"] button p{font-size:12.5px}
</style>
"""


def inject() -> None:
    """Call once per run, early in app.py (after set_page_config)."""
    st.html(CSS)
