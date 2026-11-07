import json
import re
from pathlib import Path

import pandas as pd
import pytest

from agent.tools import award_scenario
from core import actions
from core.queries import evaluated_pivot, pending_confirmation_count
from core.recompute import recompute_all
from core.rules import judge
from tests.conftest import get_quote

ROOT = Path(__file__).resolve().parent.parent
KEY = ROOT / "data" / "answer_key"


# ------------------------------------------------------------------ qualification (tri-state)
def test_expected_qualification_tristate(ro):
    got = {r["vendor_code"]: r["qual_status"] for r in ro.execute("SELECT * FROM v_vendor_qualification")}
    assert got == {"V1": "qualified", "V2": "pending", "V3": "qualified", "V4": "pending", "V5": "disqualified"}


def test_rule_engine_matches_every_questionnaire_cell_of_the_answer_key(ro):
    key = pd.read_csv(KEY / "questionnaire_answer_key.csv").set_index("vendor_code")
    for code in key.index:
        for qcode in [c for c in key.columns if c.startswith("Q")]:
            r = ro.execute("""SELECT a.rule_result FROM questionnaire_answers a JOIN vendors v USING (vendor_id)
                              JOIN rfx_questions q USING (question_id) WHERE v.vendor_code=? AND q.q_code=? AND a.is_current=1""",
                           (code, qcode)).fetchone()
            actual = r[0] if r else "unknown"  # no answer = unknown
            assert actual == key.loc[code, qcode].split()[0], (code, qcode, actual, key.loc[code, qcode])


def test_missing_knockout_answer_is_pending_never_qualified(conn):
    conn.execute("DELETE FROM questionnaire_answers WHERE vendor_id=1 AND question_id=3")
    status = conn.execute("SELECT qual_status, ko_issues FROM v_vendor_qualification WHERE vendor_code='V1'").fetchone()
    assert status["qual_status"] == "pending" and "Q03_REJECTION" in status["ko_issues"]


def test_v2_certificate_held_by_group_company_needs_confirmation(ro):
    d = ro.execute("SELECT * FROM v_documents WHERE vendor_code='V2' AND doc_type='ISO9001'").fetchone()
    assert d["holder_name"] == "Shree Ganesh Group Holdings" and d["holder_matches_vendor"] == 0
    r = ro.execute("SELECT rule_result, rule_explanation FROM questionnaire_answers a JOIN vendors v USING (vendor_id) "
                   "WHERE v.vendor_code='V2' AND question_id=1").fetchone()
    assert r["rule_result"] == "unknown" and "Group Holdings" in r["rule_explanation"]


def test_v5_iso_expired_on_award_date_is_a_knockout_fail(ro):
    d = ro.execute("SELECT validity_status FROM v_documents WHERE vendor_code='V5' AND doc_type='ISO9001'").fetchone()
    assert d[0] == "expired"
    assert {r["vendor_code"]: r["validity_status"] for r in ro.execute("SELECT * FROM v_documents WHERE doc_type='ISO9001'")}["V1"] == "valid"


def test_yes_without_document_is_self_declared_and_fails(ro):
    q = ro.execute("SELECT * FROM rfx_questions WHERE q_code='Q01_ISO9001'").fetchone()
    ans = {"answer_value": "yes", "confidence": 0.99}
    res, why, _ = judge(q, ans, None, "2026-11-16", {"thresholds": {"knockout_answer_min": 0.95}})
    assert res == "fail" and "no evidence" in why


def test_low_confidence_knockout_answer_cannot_pass_a_vendor(ro):
    q = ro.execute("SELECT * FROM rfx_questions WHERE q_code='Q03_REJECTION'").fetchone()
    res, why, _ = judge(q, {"answer_value": "yes", "confidence": 0.80}, None, "2026-11-16",
                        {"thresholds": {"knockout_answer_min": 0.95}})
    assert res == "unknown" and "knock-out" in why


# ------------------------------------------------------------------ comparison grid
def test_grid_has_one_row_per_vendor_and_line(ro):
    g = pd.read_sql("SELECT * FROM v_comparison_grid", ro)
    assert len(g) == 150 and not g.duplicated(["line_no", "vendor_code"]).any()
    assert g[g.quote_status == "not_quoted"].vendor_code.eq("V4").all() and (g.quote_status == "not_quoted").sum() == 3


def test_exclusion_reasons(ro):
    g = pd.read_sql("SELECT * FROM v_comparison_grid", ro).set_index(["vendor_code", "line_no"])
    assert g.loc[("V5", 1), "exclusion_reason"] == "vendor_disqualified"
    assert g.loc[("V2", 1), "exclusion_reason"] == "vendor_pending_qualification"
    assert g.loc[("V4", 26), "exclusion_reason"] == "not_quoted"
    assert g.loc[("V3", 10), "exclusion_reason"] == "partial_qty"
    assert g.loc[("V1", 16), "exclusion_reason"] == "open_must_confirm:spec_match"
    assert pd.isna(g.loc[("V1", 2), "exclusion_reason"]) and g.loc[("V1", 2), "is_awardable"] == 1


def test_savings_vs_last_year_use_the_main_ly_supplier(ro):
    r = ro.execute("SELECT ly_landed_inr_pc, landed_inr_pc, saving_vs_ly_inr_pc FROM v_comparison_grid WHERE vendor_code='V1' AND line_no=1").fetchone()
    ly = ro.execute("SELECT price_inr_pc, freight_inr_pc FROM last_year_prices WHERE catalog_sku='CB-3P-SH-01' ORDER BY qty_supplied DESC").fetchone()
    assert r["ly_landed_inr_pc"] == pytest.approx(ly[0] + (ly[1] or 0))
    assert r["saving_vs_ly_inr_pc"] == pytest.approx(r["ly_landed_inr_pc"] - r["landed_inr_pc"])


def test_v5_is_cheapest_on_most_lines_but_cannot_win(ro):
    g = pd.read_sql("SELECT * FROM v_comparison_grid WHERE evaluated_inr_pc IS NOT NULL", ro)
    cheapest = g.loc[g.groupby("line_no").evaluated_inr_pc.idxmin()]
    counts = cheapest.vendor_code.value_counts()
    assert counts.idxmax() == "V5" and counts["V5"] >= 15  # the planted temptation: most lines of any vendor
    assert not g[(g.vendor_code == "V5")].is_awardable.any()


# ------------------------------------------------------------------ award_scenario
def test_award_scenario_one_winner_per_line_never_v5_or_not_quoted(ro):
    s = award_scenario(ro)
    assert len(s) == 30 and s.vendor_code.notna().all()
    assert not s.vendor_code.isin(["V5", "V4", "V2"]).any()  # V5 disqualified, V4/V2 pending
    grid = pd.read_sql("SELECT * FROM v_comparison_grid", ro).set_index("quote_id")
    assert (grid.loc[s.quote_id, "quote_status"] == "quoted").all()
    assert (grid.loc[s.quote_id, "is_awardable"] == 1).all()


def test_award_scenario_winner_is_the_independently_computed_minimum(ro):
    s = award_scenario(ro).set_index("line_no")
    grid = pd.read_sql("SELECT * FROM v_comparison_grid", ro)
    for ln, grp in grid.groupby("line_no"):
        ok = grp[(grp.award_eligible == 1) & (grp.qual_status == "qualified")]
        assert s.loc[ln, "price_inr_pc"] == pytest.approx(ok.evaluated_inr_pc.min())
    t = s.attrs["totals"]
    assert t["total_value_inr"] == pytest.approx((s.price_inr_pc * s.annual_qty).sum())
    assert t["lines_awarded"] == 30 and set(t["by_vendor"]) <= {"V1", "V3"}


def test_award_scenario_what_ifs(ro):
    base = award_scenario(ro)
    no_v3 = award_scenario(ro, exclude_vendors=["V3"])
    assert not (no_v3.vendor_code == "V3").any()
    alt = no_v3.set_index("line_no")[["price_inr_pc"]].rename(columns={"price_inr_pc": "price_inr_pc_no_v3"})
    both = base.set_index("line_no").join(alt).dropna(subset=["price_inr_pc", "price_inr_pc_no_v3"])
    assert (both.price_inr_pc_no_v3 >= both.price_inr_pc - 1e-9).all()  # removing a vendor can never make a line cheaper
    assert no_v3.attrs["totals"]["lines_without_winner"] >= base.attrs["totals"]["lines_without_winner"]
    pend = award_scenario(ro, include_pending=True)
    assert set(pend.vendor_code.dropna()) <= {"V1", "V2", "V3", "V4"} and not (pend.vendor_code == "V5").any()
    assert pend.attrs["totals"]["total_value_inr"] <= base.attrs["totals"]["total_value_inr"] + 1e-6
    # V5 stays out even when qualification is ignored: its prices are unconfirmed (per-kg / blanket)
    assert not (award_scenario(ro, require_qualified=False).vendor_code == "V5").any()


def test_backup_vendor_is_the_next_eligible_quote(ro):
    s = award_scenario(ro).set_index("line_no")
    assert s.loc[1, "backup_vendor"] != s.loc[1, "vendor_code"] and s.loc[1, "backup_price_inr_pc"] >= s.loc[1, "price_inr_pc"]


def test_accepting_a_substitute_makes_it_awardable_and_what_if_shows_it_early(conn):
    before = award_scenario(conn, include_accepted_substitutes=True)
    q = get_quote(conn, "V1", 25)
    reasons = set(json.loads(q["ineligible_reasons_json"]))
    assert reasons  # blocked today
    actions.accept_spec(conn, q["quote_id"], "QA ok")
    q = get_quote(conn, "V1", 25)
    # after acceptance the only remaining blocker is the (separate) price sanity flag, which the buyer confirms
    actions.confirm_fields(conn, q["quote_id"])  # confirm every open field on the row (warns, never blocks)
    assert get_quote(conn, "V1", 25)["award_eligible"] == 1
    assert len(before) == 30


def test_confirming_a_must_confirm_value_changes_what_can_win(conn):
    # V1 line 25 (3-ply substitute, cheaper): not awardable until spec accepted and price confirmed
    s0 = award_scenario(conn).set_index("line_no")
    q = get_quote(conn, "V1", 25)
    actions.accept_spec(conn, q["quote_id"], "QA ok")
    actions.confirm_fields(conn, q["quote_id"])
    s1 = award_scenario(conn).set_index("line_no")
    assert s1.loc[25, "price_inr_pc"] <= s0.loc[25, "price_inr_pc"]


# ------------------------------------------------------------------ review queue & banner
def test_review_queue_flags_which_open_items_could_change_the_winner(ro):
    q = pd.read_sql("SELECT * FROM v_review_queue", ro)
    assert set(q.status) <= {"must_confirm", "spot_check"} and len(q) > 0
    assert not q[(q.qual_status == "disqualified") & (q.could_change_winner == 1)].shape[0]  # V5 can never change the award
    row = q[(q.vendor_code == "V1") & (q.line_no == 25) & (q.field_name == "spec_match")].iloc[0]
    assert row.status == "must_confirm"


def test_pending_confirmation_count_matches_the_view(ro):
    n = pending_confirmation_count(ro)
    direct = ro.execute("SELECT COUNT(*) FROM v_review_queue WHERE status='must_confirm' AND could_change_winner=1").fetchone()[0]
    assert n == direct and n > 0


def test_bid_analysis_grid_is_populated_from_the_view(ro):
    """Step 9: read-only line x vendor grid of evaluated INR/pc, populated in a test run only."""
    p = evaluated_pivot(ro)
    assert p.shape == (30, 5) and list(p.columns) == ["V1", "V2", "V3", "V4", "V5"]
    assert p.loc[26, "V4"] != p.loc[26, "V4"]  # NaN: V4 did not quote line 26, not 0
    assert (p.fillna(1) > 0).all().all()
    print("\n" + p.round(2).head(10).to_string())


# ------------------------------------------------------------------ guard rails from CLAUDE.md
def test_app_code_never_reads_the_answer_key_or_the_dev_fixture():
    offenders = []
    for folder in ("core", "extract", "agent", "copilot", "export", "eval"):
        for f in (ROOT / folder).rglob("*.py"):
            if folder == "eval":  # eval/ is the one place allowed to compare against the key
                continue
            text = f.read_text(encoding="utf-8")
            if re.search(r"answer_key|fixtures_from_answer_key", text):
                offenders.append(str(f))
    offenders += [str(ROOT / "app.py")] if re.search(r"answer_key|fixtures_from_answer_key", (ROOT / "app.py").read_text(encoding="utf-8")) else []
    assert offenders == []


def test_recompute_is_idempotent(conn):
    snap = lambda: [tuple(r) for r in conn.execute("SELECT quote_id, evaluated_inr_pc, review_state, award_eligible, ineligible_reasons_json FROM quotes ORDER BY quote_id")]  # noqa: E731
    before = snap()
    recompute_all(conn)
    assert snap() == before


# ------------------------------------------------------------------ PRICE_NOT_FIRM stays a flag but is always surfaced
def test_price_not_firm_is_flagged_in_grid_and_award_summary_not_excluded(ro):
    g = pd.read_sql("SELECT vendor_code, price_not_firm, is_awardable FROM v_comparison_grid", ro)
    v3 = g[g.vendor_code == "V3"]
    assert v3.price_not_firm.eq(1).all() and g[g.vendor_code != "V3"].price_not_firm.eq(0).all()
    s = award_scenario(ro)
    t = s.attrs["totals"]
    won_v3 = s[s.vendor_code == "V3"]
    assert t["not_firm_vendors"] == ["V3"] and t["lines_on_not_firm_prices"] == len(won_v3) > 0
    assert t["value_on_not_firm_prices_inr"] == pytest.approx(won_v3.annual_value_inr.sum())
    assert s[s.vendor_code == "V3"].price_not_firm.all() and not s[s.vendor_code != "V3"].price_not_firm.any()
