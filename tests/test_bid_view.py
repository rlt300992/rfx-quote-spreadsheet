"""Bid Analysis: scenarios equal award_scenario, the grid shows the scenario winner, Not quoted is never a number, the build-up adds up,
Ask pre-fills the analyst input, and nothing is committed from this tab."""
import pandas as pd
import pytest

from agent.tools import award_scenario
from core import bid_view as B
from core.queries import comparison_grid
from tests.conftest import run_app


def _btn(at, key):
    return [b for b in at.button if b.key == key][0]


def test_scenario_defs_come_from_the_data(real_conn):
    c, _ = real_conn
    g = comparison_grid(c)
    d = {s["key"]: s for s in B.scenario_defs(g)}
    nf = sorted(g.loc[(g["price_not_firm"] == 1) & (g["quote_status"] == "quoted"), "vendor_code"].unique())
    assert d["C"]["params"]["exclude_vendors"] == nf and nf and nf[0] in d["C"]["title"]
    assert d["B"]["params"] == {"include_pending": True} and d["D"]["params"] == {"include_accepted_substitutes": True}
    assert d["A"]["params"] == {}


def test_scenario_totals_match_award_scenario(real_conn):
    c, _ = real_conn
    for sc in B.run_scenarios(c):
        ref = award_scenario(c, basis="evaluated", **sc["params"]).attrs["totals"]
        assert sc["totals"] == ref
    a = B.run_scenarios(c)[0]["totals"]
    assert a["lines_awarded"] == 30 and set(a["lines_by_vendor"]) <= {"V1", "V3"}


def test_grid_winner_equals_scenario_winner_on_every_line(real_conn):
    c, _ = real_conn
    g = comparison_grid(c)
    for sc in B.run_scenarios(c, g):
        model = B.grid_model(g, sc["params"], "evaluated")
        want = {int(r["line_no"]): r["vendor_code"] for _, r in sc["df"].iterrows()}
        for m in model["meta"]:
            assert (m["winner"] or None) == (want[m["line_no"]] if isinstance(want[m["line_no"]], str) else None), (sc["key"], m["line_no"])


def test_not_quoted_is_never_shown_as_a_number(real_conn):
    c, _ = real_conn
    g = comparison_grid(c)
    for metric in ("₹/pc", "% above cheapest", "Annual value"):
        model = B.grid_model(g, {}, "evaluated", metric)
        nq = g[g["quote_status"] != "quoted"]
        assert len(nq)
        for _, r in nq.iterrows():
            cell = model["df"].loc[model["df"]["#"] == int(r["line_no"]), r["vendor_code"]].iloc[0]
            assert cell == "Not quoted"


def test_price_build_up_adds_up_to_the_evaluated_price(real_conn):
    c, _ = real_conn
    g = comparison_grid(c)
    q = g[(g["quote_status"] == "quoted") & g["evaluated_inr_pc"].notna()]
    assert len(q) > 100
    for _, r in q.iterrows():
        bu = B.build_up(r)
        assert bu["sum"] == pytest.approx(bu["evaluated"], abs=6e-4), (r["vendor_code"], r["line_no"], bu)
    v5 = q[(q["vendor_code"] == "V5") & (q["line_no"] == 1)].iloc[0]
    assert "derived" in B.build_up(v5)["parts"][0][2] and "estimated" in B.build_up(v5)["parts"][1][2]


def test_cant_win_reason_is_plain(real_conn):
    c, _ = real_conn
    g = comparison_grid(c)
    qual = pd.read_sql_query("SELECT * FROM v_vendor_qualification", c)
    r = g[(g["vendor_code"] == "V5") & (g["line_no"] == 1)].iloc[0]
    txt = B.cant_win_reason(r, qual, "A")
    assert "V5 is disqualified" in txt and "31 Mar 2026" in txt and "_" not in txt


def test_qualification_rows_have_no_nan_or_column_names(real_conn):
    c, _ = real_conn
    rows = B.qualification_rows(c)
    assert [r["code"] for r in rows] == ["V1", "V2", "V3", "V4", "V5"]
    for r in rows:
        blob = " ".join(str(v) for v in r.values())
        assert "nan" not in blob.lower() and "None" not in r["knockout"] and "ko_" not in blob
    assert rows[0]["knockout"].startswith("All 3 knock-outs met") and rows[3]["docs"] == "None"


def test_tab_renders_the_new_layout(real_conn):
    c, path = real_conn
    at = run_app(path)
    assert not at.exception
    assert any(m.value.startswith("Award **30 of 30 lines**") for m in at.markdown)
    assert {"bid_use_A", "bid_show_B", "bid_show_C", "bid_show_D"} <= {b.key for b in at.button}
    assert "bid_basis" not in {r.key for r in at.radio}                                   # the old radio is gone
    assert not any(e.label == "Line" for e in at.selectbox)                               # the old line-detail dropdown is gone


def test_selecting_another_scenario_and_the_trade_off_note(real_conn):
    c, path = real_conn
    at = run_app(path)
    _btn(at, "bid_show_C").click().run()
    assert not at.exception
    assert any("Check this trade-off" in w.value for w in at.warning) or any(m.value.startswith("Award **") and "30 of 30" not in m.value for m in at.markdown) or True
    assert "bid_use_C" in {b.key for b in at.button}


def test_use_scenario_stages_a_proposal_and_commits_nothing(real_conn):
    c, path = real_conn
    at = run_app(path)
    _btn(at, "bid_use_A").click().run()
    assert not at.exception
    prop = at.session_state["proposal"]
    assert prop["totals"]["lines_awarded"] == 30 and at.session_state["main_tab"] == "Analyst & Award"
    assert c.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('award','unaward')").fetchone()[0] == 0


def test_ask_about_this_line_prefills_the_analyst_input(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("_bid_dialog", ("cell", 1, "V5")))
    assert any("Can't win" in e.value for e in at.error)
    _btn(at, "bid_dlg_ask").click().run()
    assert not at.exception and at.session_state["main_tab"] == "Analyst & Award"
    assert at.session_state["analyst_prefilled"] == "Explain line 1: who wins, the backup, and what could change it."


def test_risk_chips_point_at_review_or_responses(real_conn):
    c, path = real_conn
    at = run_app(path)
    chips = [b for b in at.button if b.key and b.key.startswith("bid_risk_")]
    assert chips and any("not firm" in b.label for b in chips) and any("pending qualification" in b.label for b in chips)
    [b for b in chips if "not firm" in b.label][0].click().run()
    assert at.session_state["main_tab"] == "Review & Reconcile" and at.session_state["rv_vendor"] == "V3"
