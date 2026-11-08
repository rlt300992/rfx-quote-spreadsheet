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

[class*="st-key-rr_card_"]{border:1px solid var(--rr-line);border-radius:10px;padding:11px 13px;background:var(--rr-card);gap:.45rem;margin-bottom:2px}
[class*="st-key-rr_card_sel_"]{border-color:var(--rr-ambersel);box-shadow:inset 3px 0 0 var(--rr-ambersel)}
[class*="st-key-rr_card_"] [data-testid="stHtml"]{margin:0}
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

/* ---- components shared by every tab (same radius, padding, font and fill everywhere) */
[data-testid="stExpander"] { border: 1px solid var(--rr-line); border-radius: 10px; background: transparent; }
[data-testid="stExpander"] summary { padding-top: 0.45rem; padding-bottom: 0.45rem; }
[data-testid="stVerticalBlockBorderWrapper"]:has(> [data-testid="stVerticalBlock"]) { border-radius: 10px; }
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button { border-radius: 8px; padding: 4px 12px; min-height: 0; font-size: 12.5px; line-height: 1.35; }
[data-testid="stButton"] button p, [data-testid="stDownloadButton"] button p { font-size: 12.5px; }
[data-testid="stAlert"] { border-radius: 8px; }
/* selected segmented controls and pills: the theme's grey fill, not a red outline */
[data-testid="stButtonGroup"] button { border-radius: 999px; font-size: 12.5px; padding: 4px 12px; background: transparent; border: 1px solid var(--rr-line); color: inherit; }
[data-testid="stButtonGroup"] button[data-selected="true"], [data-testid="stButtonGroup"] button[aria-checked="true"], [data-testid="stButtonGroup"] button[aria-pressed="true"]
  { background: var(--rr-sel) !important; border-color: var(--rr-selb) !important; font-weight: 600; color: inherit !important; }
[data-testid="stButtonGroup"] [data-baseweb="button-group"] button[data-variant="segmented_control"] { border-radius: 8px; }
/* the sidebar: a small grey System section pinned to the bottom */
[data-testid="stSidebarContent"] { position: relative; }
[class*="st-key-sb_system"] { position: absolute; bottom: 1rem; left: 1rem; right: 1rem; padding-top: .8rem; border-top: 1px solid var(--rr-line); gap: .5rem; }
.sb-lbl { font-size: 11px; color: var(--rr-mut); letter-spacing: .6px; text-transform: uppercase; }

/* ---- rules that used to live in ui/style.py (Responses, Create RFQ, Bid Analysis) */
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
/* Bid Analysis: the selected scenario card is outlined */
[class*="st-key-rr_card_bidsc_sel_"] { border-color: #3fb27f !important; box-shadow: 0 0 0 1px #3fb27f; }

/* ---- Bid Analysis grid (an HTML table so the vendor header can carry chips and tags can sit on a second line) */
.bg-wrap { overflow: auto; max-height: 640px; border: 1px solid var(--rr-line); border-radius: 10px; }
table.bg { border-collapse: separate; border-spacing: 0; width: 100%; font-size: 13px; }
table.bg th, table.bg td { padding: 5px 8px; border-bottom: 1px solid var(--rr-line); vertical-align: middle; }
table.bg thead th { position: sticky; top: 0; background: var(--rr-card); z-index: 2; text-align: left; font-weight: 600; vertical-align: top; }
table.bg td.r, table.bg th.r { text-align: right; font-variant-numeric: tabular-nums; }
table.bg td.n { color: var(--rr-mut); width: 30px; }
table.bg td.c { text-align: right; cursor: pointer; min-width: 112px; font-variant-numeric: tabular-nums; }
table.bg td.c:hover { outline: 1px solid var(--rr-selb); }
table.bg td.win { background: rgba(46,160,67,.32); font-weight: 700; }
table.bg td.hi .p { color: var(--rr-red); }
table.bg td.out .p, table.bg td.nq .p { color: var(--rr-dim); }
table.bg td.nq .p { font-style: italic; }
table.bg .tag { font-size: 10.5px; color: var(--rr-mut); font-weight: 400; line-height: 1.2; white-space: normal; }
table.bg .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #f5c542; margin-left: 4px; }
table.bg .vh { font-weight: 700; white-space: nowrap; }
table.bg .vs, table.bg .vt { font-size: 11px; font-weight: 400; color: var(--rr-mut); white-space: nowrap; }
table.bg .star { cursor: pointer; color: var(--rr-mut); margin-right: 3px; }
table.bg td.ask { cursor: pointer; text-align: center; width: 34px; }
table.bg td.w { white-space: nowrap; font-weight: 600; }
.rr-chip.amber.sm { font-size: 10.5px; padding: 0 6px; margin-left: 4px; }
[class*="st-key-bid_bridge"] { position: absolute; left: -9999px; height: 0; overflow: hidden; }
</style>
"""


def inject() -> None:
    """Call once per run, early in app.py (after set_page_config)."""
    st.html(CSS)
