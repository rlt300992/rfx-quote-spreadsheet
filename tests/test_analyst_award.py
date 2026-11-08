"""Analyst & Award: answer parsing, the override tool, chips, pin, per-line reasons before Confirm, commit, notify, undo, memo."""
import io
import json

import openpyxl
import pytest

from agent import analyst as A
from agent import tools as T
from export import memo as M
from export.excel import build_workbook
from tests.conftest import run_app


def _btn(at, key):
    return [b for b in at.button if b.key == key][0]


def _proposal(c, **params):
    ctx = T.ToolContext(c)
    try:
        return T.propose_award(ctx, chat_prompt="Propose the cheapest award", **params)
    finally:
        ctx.close()


def _fake(monkeypatch, scenario=True):
    text = ("**V1 wins 15 lines and V3 wins 15, for ₹4.91 cr a year.**\nOnly V1 and V3 cleared quality.\n"
            "FOLLOWUPS: Which lines are close? | What if V2 qualifies? | Propose this")
    calls = []

    def fake_ask(conn, q, history=None, proposal=None):
        calls.append(q)
        r = A.AnalystResult(text=text, trace=[{"tool": "award_scenario", "args": {}, "ok": True, "result": {}}])
        r.scenario = {"tool": "award_scenario", "params": {}} if scenario else None
        A.split_answer(r)
        return r

    monkeypatch.setattr("ui.analyst.ask", fake_ask)
    return calls


def _other(p, line=28):
    return "V3" if next(r for r in p["rows"] if r["line_no"] == line)["vendor_code"] == "V1" else "V1"


# ------------------------------------------------------------------ parsing and the override tool
def test_split_answer_headline_context_followups():
    r = A.AnalystResult(text="**V1 wins 14 lines.**\nContext line.\nMore.\nFOLLOWUPS: a | b | c | d")
    A.split_answer(r)
    assert r.headline == "V1 wins 14 lines." and r.context.startswith("Context line.") and r.followups == ["a", "b", "c"] and "FOLLOWUPS" not in r.text


def test_override_tool_edits_only_the_staged_proposal(real_conn):
    c, _ = real_conn
    p = _proposal(c)
    before = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    old = next(r for r in p["rows"] if r["line_no"] == 28)["vendor_code"]
    new = _other(p)
    out = T.override_line(c, p, 28, new)
    assert out["from"] == old and out["to"] == new
    assert next(r for r in p["rows"] if r["line_no"] == 28)["vendor_code"] == new and p["overrides"] == [{"line_no": 28, "from": old, "to": new}]
    assert p["totals"]["lines_awarded"] == 30 and c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before
    assert c.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0] == 0
    with pytest.raises(ValueError, match="cannot win"):
        T.override_line(c, p, 1, "V5")
    with pytest.raises(ValueError, match="did not quote"):
        T.override_line(c, p, 26, "V4")
    with pytest.raises(ValueError, match="no proposed award"):
        T.override_line(c, None, 1, "V1")


def test_agent_tool_loop_runs_the_override(real_conn):
    c, _ = real_conn
    ctx = T.ToolContext(c)
    ctx.proposal = _proposal(c)
    res = A.run_tool(ctx, "override_proposal_line", {"line_no": 28, "vendor_code": _other(ctx.proposal)}, "give line 28 to the other vendor")
    assert "error" not in res and "override_proposal_line" in A.DATA_TOOLS
    ctx2 = T.ToolContext(c)
    assert "error" in A.run_tool(ctx2, "override_proposal_line", {"line_no": 28, "vendor_code": "V3"})
    ctx.close()
    ctx2.close()


# ------------------------------------------------------------------ the chat
def test_starter_chip_sends_the_question_and_the_answer_is_rendered(real_conn, monkeypatch):
    c, path = real_conn
    calls = _fake(monkeypatch)
    at = run_app(path)
    assert not at.exception and len([b for b in at.button if b.key and b.key.startswith("aa_st_")]) == 6
    assert any("30 of 30 questions left" in x.value for x in at.caption)
    _btn(at, "aa_st_0").click().run()
    assert not at.exception and calls == ["Cheapest per line, only among vendors who cleared quality?"]
    blob = " ".join(str(h.value) for h in at.get("html"))
    assert "V1 wins 15 lines" in blob and "Only V1 and V3 cleared quality." in blob
    keys = {b.key for b in at.button}
    assert {"aa_pin_1", "aa_prop_1"} <= keys and "aa_fu_1_0" in keys
    assert any("29 of 30 questions left" in x.value for x in at.caption)
    assert not [b for b in at.button if b.key and b.key.startswith("aa_st_")]
    _btn(at, "aa_fu_1_0").click().run()
    assert calls[-1] == "Which lines are close?"


def test_how_i_got_this_lists_the_tool_calls(real_conn, monkeypatch):
    c, path = real_conn
    _fake(monkeypatch)
    at = run_app(path)
    _btn(at, "aa_st_0").click().run()
    assert any(e.label.startswith("How I got this (1 tool calls)") for e in at.expander)
    from ui.analyst import _how
    txt = _how({"trace": [{"tool": "award_scenario", "args": {"include_pending": True}, "ok": True, "result": {}},
                          {"tool": "query_view", "args": {"sql": "SELECT * FROM v_vendor_qualification"}, "ok": True, "result": {}}]})
    assert "award_scenario" in txt and "include_pending = True" in txt and "v_vendor_qualification" in txt and "landed vs last year" in txt


def test_pin_goes_into_the_memo(real_conn, monkeypatch):
    c, path = real_conn
    _fake(monkeypatch)
    at = run_app(path)
    _btn(at, "aa_st_0").click().run()
    _btn(at, "aa_pin_1").click().run()
    pins = at.session_state["pinned"]
    assert len(pins) == 1 and "V1 wins 15 lines" in pins[0]["text"]
    m = M.memo_data(c, _proposal(c), pins)
    assert "V1 wins 15 lines" in M.memo_html(m) and any("V1 wins 15 lines" in v for _, v in M.memo_rows(m))
    _btn(at, "aa_pin_1").click().run()
    assert at.session_state["pinned"] == []


def test_propose_this_stages_but_never_commits(real_conn, monkeypatch):
    c, path = real_conn
    _fake(monkeypatch)
    at = run_app(path)
    _btn(at, "aa_st_0").click().run()
    _btn(at, "aa_prop_1").click().run()
    at.run()
    assert at.session_state["proposal"]["totals"]["lines_awarded"] == 30
    assert c.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award'").fetchone()[0] == 0


def test_question_limit_is_enforced(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("chat", [{"role": "user", "text": "q"}] * 30))
    assert at.chat_input(key="chat_input").disabled is True
    assert any("0 of 30 questions left" in x.value for x in at.caption)


# ------------------------------------------------------------------ the award panel
def test_panel_empty_state_and_proposed_state(real_conn):
    c, path = real_conn
    at = run_app(path)
    assert any("Current best scenario" in str(h.value) for h in at.get("html"))
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("proposal", _proposal(c)))
    assert not at.exception and not _btn(at, "confirm_award").disabled


def test_override_needs_code_and_reason_before_confirm_is_enabled(real_conn):
    c, path = real_conn
    p = _proposal(c)
    T.override_line(c, p, 28, _other(p))
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("proposal", p))
    assert _btn(at, "confirm_award").disabled
    assert "aa_code_28" in {s.key for s in at.selectbox}
    [s for s in at.selectbox if s.key == "aa_code_28"][0].select("capacity").run()
    assert _btn(at, "confirm_award").disabled
    [t for t in at.text_input if t.key == "aa_why_28"][0].set_value("Vendor is near its capacity on 7-ply").run()
    assert not _btn(at, "confirm_award").disabled


def test_confirm_writes_audit_rows_per_line_with_their_own_reason_and_sets_awarded(real_conn):
    c, path = real_conn
    p = _proposal(c)
    T.override_line(c, p, 28, _other(p))
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("proposal", p))
    [s for s in at.selectbox if s.key == "aa_code_28"][0].select("capacity").run()
    [t for t in at.text_input if t.key == "aa_why_28"][0].set_value("Vendor is near its capacity on 7-ply").run()
    _btn(at, "confirm_award").click().run()
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award'").fetchone()[0] == 0
    _btn(at, "aa_cancel").click().run()
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award'").fetchone()[0] == 0
    _btn(at, "confirm_award").click().run()
    _btn(at, "aa_do_confirm").click().run()
    assert not at.exception
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award'").fetchone()[0] == 30
    row = c.execute("SELECT reason, evidence_json FROM audit_log WHERE action='award' AND entity_id=28").fetchone()
    assert row["reason"] == "Vendor is near its capacity on 7-ply" and json.loads(row["evidence_json"])["bypass_reason_code"] == "capacity"
    assert c.execute("SELECT status FROM rfqs").fetchone()[0] == "awarded"
    assert "aa_notify" in {b.key for b in at.button}


def test_notify_logs_one_row_per_vendor_and_undo_works(real_conn):
    c, path = real_conn
    T.commit_award(c, _proposal(c))
    at = run_app(path)
    assert {"aa_notify", "aa_undo", "aa_approve"} <= {b.key for b in at.button}
    _btn(at, "aa_notify").click().run()
    assert not at.exception
    _btn(at, "aa_notify_send").click().run()
    rows = c.execute("SELECT field_name FROM audit_log WHERE action='award_notify_stub'").fetchall()
    assert sorted(r[0] for r in rows) == ["V1", "V2", "V3", "V4", "V5"]
    _btn(at, "aa_approve").click().run()
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award_approval_stub'").fetchone()[0] == 1
    _btn(at, "aa_undo").click().run()
    assert _btn(at, "unaward_btn").disabled
    [t for t in at.text_input if t.key == "unaward_reason"][0].set_value("Testing the undo").run()
    _btn(at, "unaward_btn").click().run()
    assert c.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT status FROM rfqs").fetchone()[0] == "evaluating"


def test_memo_has_exceptions_pinned_items_and_exports(real_conn):
    c, _ = real_conn
    p = _proposal(c, exclude_vendors=["V3"])
    reasons = {int(r["line_no"]): ("price_not_firm", "V3 prices are kraft-linked") for r in M.exceptions(c, M.current_award(c, p))}
    assert reasons
    m = M.memo_data(c, p, [{"title": "Savings", "text": "V3 lines save ₹3.5 L"}], "Rohit", reasons)
    assert m["exceptions"] and all(e["code"] == "price_not_firm" and e["extra_per_year"] > 0 for e in m["exceptions"])
    h = M.memo_html(m)
    assert "EXCEPTIONS" in h and "price_not_firm" in h and "V3 lines save" in h and "RISKS ACCEPTED" in h and "NEXT STEPS" in h
    assert M.memo_pdf(m)[:4] == b"%PDF"
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(c, p, [{"title": "Savings", "text": "V3 lines save ₹3.5 L"}])))
    assert "Memo" in wb.sheetnames and any("V3 lines save" in str(r[1]) for r in wb["Memo"].values)


def test_nothing_commits_without_the_button(real_conn, monkeypatch):
    c, path = real_conn
    _fake(monkeypatch)
    at = run_app(path)
    _btn(at, "aa_st_0").click().run()
    _btn(at, "aa_prop_1").click().run()
    at.run()
    assert c.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0] == 0
