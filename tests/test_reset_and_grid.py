"""Reset demo (rebuild from seed + cache, no API calls), the sidebar System section, and the Bid Analysis HTML grid."""
import shutil

import pandas as pd

from core import bid_view as B
from core.db import connect
from core.queries import comparison_grid
from core.reset import reset_demo
from tests.conftest import run_app


def _btn(at, key):
    return [b for b in at.button if b.key == key][0]


def test_reset_rebuilds_from_seed_and_cache_without_api_calls(real_conn, monkeypatch):
    c, _ = real_conn
    import extract.gemini_client as g
    monkeypatch.setattr(g, "generate", lambda *a, **k: (_ for _ in ()).throw(AssertionError("reset must not call the model")), raising=False)
    monkeypatch.setattr(g, "generate_structured", lambda *a, **k: (_ for _ in ()).throw(AssertionError("reset must not call the model")), raising=False)
    from agent import tools as T
    T.commit_award(c, _prop(c))
    c.execute("INSERT INTO rfq_drafts (state_json, updated_at) VALUES ('{}', 'x')")
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] > 0
    res = reset_demo(c)
    assert res["api_calls"] == 0 and res["quotes"] > 100
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('award', 'override', 'confirm')").fetchone()[0] == 0   # the old log is gone (only the rebuild's own rows remain)
    assert c.execute("SELECT COUNT(*) FROM rfq_drafts").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT status FROM rfqs").fetchone()[0] == "issued"
    assert c.execute("SELECT COUNT(*) FROM v_comparison_grid").fetchone()[0] == 150
    c.execute("INSERT INTO audit_log (ts, actor, action) VALUES ('t', 'a', 'confirm')")   # triggers were restored: the log is append-only again
    import sqlite3
    try:
        c.execute("DELETE FROM audit_log")
        raise AssertionError("audit_log must be append-only")
    except sqlite3.DatabaseError:
        pass


def _prop(c):
    from agent import tools as T
    ctx = T.ToolContext(c)
    try:
        return T.propose_award(ctx, chat_prompt="x")
    finally:
        ctx.close()


def test_sidebar_system_section_and_reset_confirmation(real_conn):
    c, path = real_conn
    at = run_app(path)
    assert {"sb_test_gemini", "sb_reset"} <= {b.key for b in at.sidebar.button}
    assert any("System" in str(h.value) for h in at.sidebar.get("html"))
    _btn_s = [b for b in at.sidebar.button if b.key == "sb_reset"][0]
    _btn_s.click().run()
    assert any(b.key == "sb_reset_go" for b in at.button) and any("rebuilds the demo database" in m.value for m in at.markdown)
    [b for b in at.button if b.key == "sb_reset_cancel"][0].click().run()
    assert not any(b.key == "sb_reset_go" for b in at.button)


def test_grid_cells_tags_only_for_true_per_line_exceptions(real_conn):
    c, _ = real_conn
    g = comparison_grid(c)
    model = B.grid_model(g, {}, "evaluated", "₹/pc")
    cells = {m["line_no"]: model["cells"][i] for i, m in enumerate(model["meta"])}
    assert cells[6]["V2"]["tag"] == ""                              # V2 is pending: a grey cell does not repeat "Unconfirmed"
    assert cells[16]["V1"]["tag"] == "Spec deviation" and cells[25]["V1"]["tag"].startswith("Substitute")
    assert cells[26]["V4"] == {"p": "Not quoted", "cls": "nq", "tag": "", "dot": False}
    assert all(not v["tag"] for ln in cells for v in cells[ln].values() if v["cls"] == "out" and v["tag"] == "Unconfirmed")
    assert sum(v["dot"] for ln in cells for v in cells[ln].values()) == 15            # the not-firm dot is on V3's winning lines only
    assert all(v["cls"] in ("win", "out", "hi", "", "nq") for ln in cells for v in cells[ln].values())


def test_grid_html_has_vendor_header_chips_and_close_chip_and_no_vendor_cards(real_conn):
    c, path = real_conn
    at = run_app(path)
    blob = " ".join(str(h.value) for h in at.get("html"))
    assert "class='bg'" in blob and "Qualified" in blob and "30/30 lines" in blob and "close: V" in blob and "data-cell='1|V5'" in blob
    assert "rr-chip amber sm" in blob and "#f5c542" in open("ui/theme.py", encoding="utf-8").read()
    assert not any(b.key and b.key.startswith("bid_star_") for b in at.button)       # the separate vendor cards (with star buttons) are gone


def test_grid_click_bridge_opens_the_build_up_and_ask(real_conn):
    c, path = real_conn
    at = run_app(path)
    [t for t in at.text_input if t.key == "bid_bridge_in"][0].set_value("cell|1|V5|123").run()
    assert not at.exception and any("Can't win" in e.value for e in at.error)
    [b for b in at.button if b.key == "bid_dlg_ask"][0].click().run()
    assert at.session_state["main_tab"] == "Analyst & Award" and "line 1" in at.session_state["analyst_prefilled"]
    at2 = run_app(path)
    [t for t in at2.text_input if t.key == "bid_bridge_in"][0].set_value("star|V3|9").run()
    assert at2.session_state["bid_stars"] == {"V3"}


def test_no_tab_swallows_an_error_in_any_main_state(real_conn):
    """The app wraps each tab in try/except; a swallowed error shows 'hit a problem'. None of the main states may show it."""
    from core import rfq_draft as R
    c, path = real_conn
    d = R.from_demo(c)
    d["terms"]["response_due"] = "2026-11-16"
    R.save(c, d)
    states = [None, lambda a: a.session_state.__setitem__("rfq_mode", "New draft"), lambda a: a.session_state.__setitem__("rv_vendor", "V4")]
    for setup in states:
        at = run_app(path, setup=setup)
        assert not at.exception
        assert not any("hit a problem" in w.value for w in at.warning), [w.value for w in at.warning]
