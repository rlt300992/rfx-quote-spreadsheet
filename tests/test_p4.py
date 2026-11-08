"""P4: query_view sandbox, scenarios, propose/commit award, the analyst loop (stubbed model), Excel export, UI."""
import io
import json
from types import SimpleNamespace

import openpyxl
import pandas as pd
import pytest

from agent import analyst as A
from agent import tools as T
from export.excel import build_workbook
from tests.conftest import get_quote, run_app


# ------------------------------------------------------------------ query_view sandbox
@pytest.mark.parametrize("sql", [
    "SELECT * FROM quotes", "SELECT * FROM audit_log", "UPDATE quotes SET raw_price=1", "SELECT 1; DELETE FROM quotes",
    "SELECT 1 -- x", "PRAGMA table_info(quotes)", "ATTACH DATABASE 'x' AS x", "DROP VIEW v_comparison_grid", "",
    "SELECT * FROM v_comparison_grid WHERE quote_id IN (SELECT quote_id FROM quotes)",
    "WITH q AS (SELECT * FROM line_items) SELECT * FROM q"])
def test_query_view_blocks_writes_base_tables_and_tricks(real_conn, sql):
    c, _ = real_conn
    with pytest.raises(ValueError):
        T.query_view(c, sql)


def test_query_view_reads_views_json_and_caps_rows(real_conn):
    c, _ = real_conn
    df = T.query_view(c, "SELECT vendor_code, COUNT(*) AS n FROM v_comparison_grid GROUP BY 1 ORDER BY 1")
    assert list(df.vendor_code) == ["V1", "V2", "V3", "V4", "V5"] and df.n.eq(30).all()
    j = T.query_view(c, "SELECT g.vendor_code, j.value AS flag FROM v_comparison_grid g, json_each(g.flags_json) j WHERE g.line_no=6 AND g.vendor_code='V2'")
    assert "UNIT_AMBIGUOUS" in set(j.flag)
    assert len(T.query_view(c, "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n<3000) SELECT n FROM r")) == 500


def test_query_view_cannot_modify_the_real_database(real_conn):
    c, _ = real_conn
    before = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    with pytest.raises(ValueError):
        T.query_view(c, "SELECT * FROM v_review_queue; INSERT INTO audit_log(ts,actor,action) VALUES ('x','x','confirm')")
    assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before


# ------------------------------------------------------------------ scenarios
def test_order_qty_override_rematches_tiers_without_touching_the_database(real_conn):
    c, _ = real_conn
    base = T.award_scenario(c)
    what_if = T.award_scenario(c, order_qty_override=20000)
    # V1 offers 4% off for 20,000 per release on lines 1, 3 and 9 and 19's 3% is unclear-basis: the total can only fall
    assert what_if.attrs["totals"]["total_value_inr"] < base.attrs["totals"]["total_value_inr"]
    changed = what_if.merge(base, on="line_no", suffixes=("_new", "_old"))
    changed = changed[(changed.price_inr_pc_new - changed.price_inr_pc_old).abs() > 1e-6]
    assert {1, 3, 9} <= set(changed.line_no) and (changed.price_inr_pc_new < changed.price_inr_pc_old).all()
    assert c.execute("SELECT typical_order_qty FROM line_items WHERE line_no=1").fetchone()[0] == 10000
    assert get_quote(c, "V1", 1)["tier_applied"] is None                      # real quote unchanged


def test_scenario_totals_report_caveats_and_savings(real_conn):
    c, _ = real_conn
    t = T.award_scenario(c).attrs["totals"]
    assert t["lines_awarded"] == 30 and set(t["by_vendor"]) <= {"V1", "V3"}
    assert t["not_firm_vendors"] == ["V3"] and t["lines_on_not_firm_prices"] == t["lines_by_vendor"]["V3"]
    assert t["saving_vs_ly_inr"] == pytest.approx(t["ly_value_inr"] - t["total_landed_inr"])
    ex = T.award_scenario(c, exclude_vendors=["V3"])
    g = pd.read_sql_query("SELECT vendor_code, is_awardable FROM v_comparison_grid", c)
    v1_lines = int(g[(g.vendor_code == "V1") & (g.is_awardable == 1)].shape[0])
    assert not (ex.vendor_code == "V3").any() and set(ex.vendor_code.dropna()) == {"V1"}
    assert ex.attrs["totals"]["lines_awarded"] == v1_lines and ex.attrs["totals"]["lines_without_winner"] == 30 - v1_lines
    assert ex.backup_vendor.isna().all()                                    # with V3 gone there is only one eligible vendor per line
    assert set(T.award_scenario(c, exclude_vendors=["V1"]).vendor_code.dropna()) == {"V3"}


# ------------------------------------------------------------------ propose / commit
def test_propose_stages_but_never_writes(real_conn):
    c, _ = real_conn
    ctx = T.ToolContext(c)
    p = T.propose_award(ctx, chat_prompt="propose the cheapest", exclude_vendors=["V3"])
    assert ctx.proposal is p and p["params"] == {"exclude_vendors": ["V3"]}
    assert c.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award'").fetchone()[0] == 0


def _proposal(c, **params):
    ctx = T.ToolContext(c)
    return T.propose_award(ctx, chat_prompt="test", **params)


def test_commit_default_scenario_writes_one_audit_row_per_line_with_shared_batch(real_conn):
    c, _ = real_conn
    p = _proposal(c)
    res = T.commit_award(c, p, reason=None)
    assert res["lines_awarded"] == 30 and res["bypass_lines"] == []
    rows = c.execute("SELECT * FROM audit_log WHERE action='award'").fetchall()
    assert len(rows) == 30 and len({r["batch_id"] for r in rows}) == 1 and all(r["chat_prompt"] == "test" for r in rows)
    sc = json.loads(rows[0]["scenario_json"])
    assert len(sc["candidates"]) == 5 and {x["vendor"] for x in sc["candidates"]} == {"V1", "V2", "V3", "V4", "V5"}
    assert any(x["exclusion_reason"] == "vendor_disqualified" for x in sc["candidates"])
    li = c.execute("SELECT * FROM line_items WHERE line_no=1").fetchone()
    assert li["awarded_quote_id"] and li["l1_quote_id"] and li["l1_eligible_quote_id"] and li["bypass_reason_code"] is None and li["awarded_at"]
    assert li["l1_eligible_quote_id"] == li["awarded_quote_id"]
    # L1 (cheapest quote of any kind) is V5 on line 1, but it is not eligible: that is exactly what l1_quote_id records
    assert c.execute("SELECT vendor_code FROM quotes q JOIN vendors v USING (vendor_id) WHERE quote_id=?", (li["l1_quote_id"],)).fetchone()[0] == "V5"
    assert c.execute("SELECT status FROM rfqs").fetchone()[0] == "awarded"


def test_commit_requires_a_reason_and_code_where_award_is_not_cheapest_eligible(real_conn):
    c, _ = real_conn
    p = _proposal(c, exclude_vendors=["V3"])                               # forces non-L1-eligible winners on lines V3 would win
    review = T.award_review(c, p)
    assert review.bypass.any()
    with pytest.raises(ValueError, match="reason is required"):
        T.commit_award(c, p, reason=" ", bypass_code="buyer_judgment")
    with pytest.raises(ValueError, match="reason code"):
        T.commit_award(c, p, reason="V3 prices are not firm", bypass_code="nonsense")
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award'").fetchone()[0] == 0   # all-or-nothing
    res = T.commit_award(c, p, reason="V3 prices are kraft-linked and not firm", bypass_code="price_not_firm")
    assert set(res["bypass_lines"]) == set(review[review.bypass].line_no)
    row = c.execute("SELECT * FROM line_items WHERE line_no=?", (res["bypass_lines"][0],)).fetchone()
    assert row["bypass_reason_code"] == "price_not_firm" and row["awarded_quote_id"] != row["l1_eligible_quote_id"]
    a = c.execute("SELECT reason, evidence_json FROM audit_log WHERE action='award' AND entity_id=?", (res["bypass_lines"][0],)).fetchone()
    assert "kraft-linked" in a["reason"] and json.loads(a["evidence_json"])["bypass"] is True


def test_commit_refuses_a_stale_proposal(real_conn):
    from core import actions

    c, _ = real_conn
    p = _proposal(c)
    q = get_quote(c, "V1", 2)
    actions.correct_field(c, q["quote_id"], "raw_price", None or 99999.0, "make it ineligible via sanity")   # price now fails sanity
    stale = [r["line_no"] for r in p["rows"] if r["quote_id"] == q["quote_id"]]
    assert stale == [2]
    with pytest.raises(ValueError, match="no longer be awarded"):
        T.commit_award(c, p)


def test_unaward_needs_reason_and_is_audited(real_conn):
    c, _ = real_conn
    T.commit_award(c, _proposal(c))
    with pytest.raises(ValueError):
        T.unaward(c, "")
    assert T.unaward(c, "re-opening after a vendor revised prices") == 30
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='unaward'").fetchone()[0] == 30
    assert c.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0] == 0


# ------------------------------------------------------------------ the analyst loop with a stubbed model
def _call(name, **args):
    return SimpleNamespace(function_call=SimpleNamespace(name=name, args=args), text=None, thought=False)


def _text(t):
    return SimpleNamespace(function_call=None, text=t, thought=False)


def _resp(*parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=list(parts)))], text="".join(p.text or "" for p in parts))


class FakeModel:
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def __call__(self, contents, model=None, **config):
        self.seen.append(len(contents))
        return self.turns.pop(0) if self.turns else _resp(_text("done"))


def test_analyst_loop_runs_tools_and_returns_the_models_text(real_conn, monkeypatch):
    c, _ = real_conn
    fake = FakeModel(_resp(_call("query_view", sql="SELECT COUNT(*) AS n FROM v_comparison_grid")),
                     _resp(_call("render_chart", sql="SELECT vendor_code, AVG(evaluated_inr_pc) AS avg FROM v_comparison_grid GROUP BY 1", kind="bar",
                                 x="vendor_code", y="avg", title="Avg price"), _call("render_table", sql="SELECT vendor_code, qual_status FROM v_vendor_qualification", title="Quals")),
                     _resp(_text("There are 150 quotes.")))
    monkeypatch.setattr(A, "generate", fake)
    r = A.ask(c, "how many quotes?")
    assert r.text == "There are 150 quotes." and r.data_calls == 1 and [t["tool"] for t in r.trace] == ["query_view", "render_chart", "render_table"]
    assert len(r.figures) == 1 and len(r.tables) == 1 and r.tables[0][1].shape == (5, 2)
    assert fake.seen == sorted(fake.seen)                                  # history grows turn by turn


def test_analyst_tool_budget_and_errors_are_returned_to_the_model(real_conn, monkeypatch):
    c, _ = real_conn
    calls = [_resp(_call("query_view", sql=f"SELECT {i} AS x FROM v_ly_baseline LIMIT 1")) for i in range(8)]
    fake = FakeModel(_resp(_call("query_view", sql="SELECT * FROM quotes")), *calls, _resp(_text("final")))
    monkeypatch.setattr(A, "generate", fake)
    r = A.ask(c, "go")
    assert r.text == "final"
    assert r.trace[0]["ok"] is False and "Only these views exist" in json.dumps(r.trace[0]["result"])
    ok = [t for t in r.trace if t["ok"]]
    assert len(ok) == A.MAX_DATA_CALLS - 1                                  # 6 allowed in total, one was spent on the failing call
    assert any("budget" in json.dumps(t["result"]) for t in r.trace)        # later calls are refused


def test_analyst_propose_award_stages_a_proposal_and_never_commits(real_conn, monkeypatch):
    c, _ = real_conn
    monkeypatch.setattr(A, "generate", FakeModel(_resp(_call("propose_award", exclude_vendors=["V3"])), _resp(_text("Proposed; please review the Award panel."))))
    r = A.ask(c, "propose an award without V3")
    assert r.proposal and r.proposal["params"] == {"exclude_vendors": ["V3"]} and r.proposal["chat_prompt"] == "propose an award without V3"
    assert r.trace[0]["ok"] and r.trace[0]["tool"] == "propose_award"
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0] == 0


def test_analyst_has_no_commit_tool_and_prompt_forbids_inventing():
    names = {s["name"] for s in T.TOOL_SPECS}
    assert "commit_award" not in names and names == {"query_view", "award_scenario", "propose_award", "render_table", "render_chart"}
    assert "Answer ONLY from tool results" in A.SYSTEM and "cannot commit" in A.SYSTEM
    assert "v_comparison_grid" in A.SYSTEM and "Never explain WHY" in A.SYSTEM


def test_analyst_forced_final_answer_when_the_loop_never_stops(real_conn, monkeypatch):
    c, _ = real_conn
    turns = [_resp(_call("query_view", sql="SELECT 1 AS x FROM v_ly_baseline LIMIT 1")) for _ in range(A.MAX_TURNS)] + [_resp(_text("forced answer"))]
    monkeypatch.setattr(A, "generate", FakeModel(*turns))
    assert A.ask(c, "loop").text == "forced answer"


# ------------------------------------------------------------------ Excel
def test_excel_has_four_sheets_and_says_what_it_shows(real_conn):
    c, _ = real_conn
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(c)))
    assert wb.sheetnames == ["Award", "Scenario", "Audit trail", "Assumptions"]
    assert wb["Award"]["A1"].value == "CURRENT BEST SCENARIO (not committed)"
    sc = pd.DataFrame(wb["Scenario"].values)
    sc.columns = sc.iloc[0]; sc = sc[1:]
    assert len(sc) == 150 and "Why it can't win" in sc.columns
    v5 = sc[(sc["Vendor"] == "V5") & (sc["Line"] == 1)].iloc[0]
    assert "knock-out" in v5["Why it can't win"] and v5["Can win"] == "no"
    assumptions = "\n".join(str(r[0]) + " " + str(r[1]) for r in wb["Assumptions"].values)
    assert "95.17" in assumptions and "10.9%" in assumptions and "2026-11-16" in assumptions and "DERIVED_PER_KG" in assumptions


def test_excel_after_commit_shows_committed_award_and_audit_rows(real_conn):
    c, _ = real_conn
    T.commit_award(c, _proposal(c))
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(c)))
    assert wb["Award"]["A1"].value == "COMMITTED AWARD"
    aw = pd.DataFrame(wb["Award"].values).iloc[3:34]
    assert aw.iloc[0, 0] == "Line" and len(aw) == 31
    audit = pd.DataFrame(wb["Audit trail"].values)
    assert (audit[4] == "award").sum() == 30
    notes = [r[0] for r in wb["Award"].values if isinstance(r[0], str) and "NOT-FIRM" in r[0]]
    assert notes


# ------------------------------------------------------------------ the tab in the real app
def _btn(at, key):
    return [b for b in at.button if b.key == key][0]


def test_award_panel_commits_with_audit_rows_through_the_ui(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("proposal", _proposal(c)))
    assert not at.exception and any("not committed" in s.value.lower() for s in at.subheader)
    assert any("NOT-FIRM" in w.value for w in at.warning)
    _btn(at, "confirm_award").click().run()
    assert not at.exception
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award'").fetchone()[0] == 30
    assert any("Award committed" in s.value for s in at.success)


def test_award_panel_blocks_bypass_without_reason_then_accepts_it(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("proposal", _proposal(c, exclude_vendors=["V3"])))
    assert any("NOT the cheapest eligible" in w.value for w in at.warning)
    _btn(at, "confirm_award").click().run()
    assert any("reason is required" in e.value for e in at.error)
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award'").fetchone()[0] == 0
    [w for w in at.text_input if w.key == "bypass_reason"][0].set_value("V3 prices not firm").run()
    [w for w in at.selectbox if w.key == "bypass_code"][0].select("price_not_firm").run()
    _btn(at, "confirm_award").click().run()
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='award'").fetchone()[0] > 0


def test_chat_message_limit_is_enforced_per_session(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("chat", [{"role": "user", "text": "q"}] * 30))
    assert at.chat_input(key="chat_input").disabled is True                      # the analyst's input (the RFQ co-pilot has its own)
    assert at.chat_input(key="rfq_chat_input").disabled is False
    assert any("0 of 30 messages left" in x.value for x in at.caption)


def test_clarification_send_is_stubbed_audited_and_locks(real_conn):
    c, path = real_conn
    at = run_app(path)
    assert not any(e.label.startswith("Clarification emails") for e in at.expander)   # moved out of Analyst & Award
    ta = at.text_area(key="clar_V1")
    ta.set_value(ta.value + " Edited by buyer.").run()
    [b for b in at.button if b.key == "clar_send_V1"][0].click().run()
    row = c.execute("SELECT new_value FROM audit_log WHERE action='clarification_sent_stub'").fetchall()
    assert len(row) == 1 and row[0][0].endswith("Edited by buyer.")
    assert c.execute("SELECT COUNT(*) FROM vendor_responses WHERE clarification_status='sent_stub'").fetchone()[0] == 1
    assert at.text_area(key="clar_V1").disabled is True
    assert any(e.label.startswith("Clarification sent ·") for e in at.expander)
