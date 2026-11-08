"""P3: Review & Reconcile and Bid Analysis, exercised through the real app on the real extraction."""
import json

import pytest

from core import flags as flagmod
from core.queries import review_counts, review_filter, review_rows, vendor_totals
from tests.conftest import get_quote, run_app


def _md(at) -> str:
    return "\n".join([m.value for m in at.markdown] + [c.value for c in at.caption])


def _grid(at):
    """The Bid Analysis line x vendor grid (30 lines x 5 vendors), wherever it sits among the app's tables."""
    return [d.value for d in at.dataframe if d.value.shape == (30, 5)][0]


def _select(at, key, value):
    [w for w in at.selectbox if w.key == key][0].select(value)


def _button(at, key):
    return [b for b in at.button if b.key == key][0]


# ------------------------------------------------------------------ data layer
def test_every_flag_the_pipeline_can_raise_is_explained():
    from core.confidence import FLAG_RULES

    known = set(FLAG_RULES) | {"FX_CONVERTED", "CONDITIONAL_DISCOUNT_AVAILABLE", "VALIDITY_EXPIRED", "TIER_NOT_MET_PER_ORDER", "TIER_BASIS_UNCLEAR"}
    assert known <= set(flagmod.FLAG_HELP), known - set(flagmod.FLAG_HELP)
    for reason in ("not_quoted", "unmatched", "vendor_disqualified", "vendor_pending_qualification", "freight_unknown", "spec_substitute", "partial_qty"):
        assert flagmod.explain_reason(reason)
    assert "must-confirm" in flagmod.explain_reason("open_must_confirm:price")
    assert flagmod.confidence_chip(0.97) == ":green[97%]" and flagmod.confidence_chip(0.85) == ":orange[85%]" and flagmod.confidence_chip(0.5) == ":red[50%]"


def test_sub_tab_counts_on_real_extraction(real_conn):
    c, _ = real_conn
    rows = review_rows(c)
    counts = review_counts(rows)
    assert counts["all"] == 150 and counts["not_quoted"] == 3 and counts["unmatched"] == 0
    assert 0 < counts["needs_review"] < 150
    assert review_counts(rows, "V4")["not_quoted"] == 3 and review_counts(rows, "V1")["not_quoted"] == 0


def test_every_planted_trap_is_present_and_has_an_explanation(real_conn):
    """CLAUDE.md section 7: each trap must be visible in the review data with a plain-language explanation."""
    c, _ = real_conn
    rows = {(r["vendor_code"], r["line_no"]): r for r in review_rows(c)}
    expect = {("V1", 16): "SPEC_DEVIATION", ("V1", 25): "SPEC_SUBSTITUTE", ("V1", 19): "TIER_BASIS_UNCLEAR", ("V1", 1): "TIER_NOT_MET_PER_ORDER",
              ("V2", 6): "UNIT_AMBIGUOUS", ("V2", 8): "UNIT_AMBIGUOUS", ("V2", 12): "UNIT_AMBIGUOUS", ("V2", 1): "MSME_TERMS_CAPPED",
              ("V3", 1): "FX_CONVERTED", ("V3", 10): "PARTIAL_QTY", ("V4", 14): "LOW_CONFIDENCE_OCR", ("V4", 2): "FREIGHT_IMPUTED",
              ("V5", 1): "DERIVED_PER_KG", ("V5", 28): "LY_SPEC_CHANGED", ("V5", 27): "IMPLIED_BY_BLANKET"}
    for key, flag in expect.items():
        assert flag in rows[key]["flags"], (key, flag, rows[key]["flags"])
        assert flagmod.FLAG_HELP[flag]
    assert "PRICE_NOT_FIRM" in rows[("V3", 5)]["flags"] and "GST_RATE_MISMATCH" in rows[("V3", 5)]["flags"]
    assert rows[("V4", 26)]["quote_status"] == "not_quoted" and rows[("V4", 26)]["exclusion_reason"] == "not_quoted"
    assert rows[("V5", 3)]["exclusion_reason"] == "vendor_disqualified" and rows[("V2", 3)]["exclusion_reason"] == "vendor_pending_qualification"


def test_totals_leave_out_unresolved_unit_doubt_lines(real_conn):
    c, _ = real_conn
    t = vendor_totals(c).set_index("vendor_code")
    assert t.loc["V2", "lines_excluded_unit_doubt"] == 3 and t.loc["V2", "lines_priced"] == 27
    assert t.loc["V2", "value_inr"] < 1e8                                   # not swamped by the 620/944/1229 lines
    assert t.loc["V4", "lines_priced"] == 27


# ------------------------------------------------------------------ the Review tab in the real app







def test_match_unmatched_flow(real_conn):
    from core import actions

    c, _ = real_conn
    vid = c.execute("SELECT vendor_id FROM vendors WHERE vendor_code='V4'").fetchone()[0]
    rid = c.execute("SELECT response_id FROM vendor_responses WHERE vendor_id=? AND content_role IN ('quote','mixed') LIMIT 1", (vid,)).fetchone()[0]
    qid = c.execute("""INSERT INTO quotes (response_id, vendor_id, quote_status, vendor_line_ref, raw_price, raw_unit, raw_currency, tax_basis, freight_basis)
                       VALUES (?,?, 'unmatched', 'mystery 450x350x250', 40.8, 'per_piece', 'INR', 'ex_gst', 'extra_unquoted')""", (rid, vid)).lastrowid
    with pytest.raises(ValueError):
        actions.match_unmatched(c, qid, 26, "")
    actions.match_unmatched(c, qid, 26, "Same size and ply as our promo box")
    q = c.execute("SELECT * FROM quotes WHERE quote_id=?", (qid,)).fetchone()
    assert q["quote_status"] == "quoted" and q["evaluated_inr_pc"] is not None
    assert c.execute("SELECT COUNT(*) FROM quotes WHERE vendor_id=? AND line_id=? AND is_current=1", (vid, q["line_id"])).fetchone()[0] == 1
    with pytest.raises(ValueError):
        actions.match_unmatched(c, qid, 26, "again")


# ------------------------------------------------------------------ Bid Analysis in the real app
def test_bid_analysis_grid_totals_and_qualification(real_conn):
    c, path = real_conn
    at = run_app(path)
    assert not at.exception
    grid = _grid(at)
    assert grid.loc[26, "V4"] == "not quoted"
    assert "⚠" in grid.loc[1, "V3"] and "◆" in grid.loc[6, "V2"]
    assert any("NOT-FIRM" in w.value for w in at.warning)
    md = _md(at)
    assert "DISQUALIFIED" in md and "QUALIFIED" in md and "PENDING" in md
    assert "Q01_ISO9001 (fail)" in md and "expired 2026-03-31" in md
    metrics = {m.label: m.value for m in at.metric}
    assert "Saving vs last year (landed)" in metrics and metrics["Lines with a winner"].endswith("/ 30")


def test_bid_toggle_switches_between_landed_and_evaluated(real_conn):
    c, path = real_conn
    at = run_app(path)
    ev = _grid(at).loc[5, "V2"]
    [r for r in at.radio if r.key == "bid_basis"][0].set_value("Quoted (landed)").run()
    la = _grid(at).loc[5, "V2"]
    assert ev != la
    row = c.execute("SELECT landed_inr_pc, evaluated_inr_pc FROM v_comparison_grid WHERE vendor_code='V2' AND line_no=5").fetchone()
    assert la.startswith(f"{row['landed_inr_pc']:.2f}") and ev.startswith(f"{row['evaluated_inr_pc']:.2f}")


def test_line_detail_explains_why_a_quote_cannot_win(real_conn):
    c, path = real_conn
    at = run_app(path)
    [s for s in at.selectbox if s.key == "bid_line"][0].select(1)
    at.run()
    md = _md(at)
    assert "Why it can't win" in md and "knock-out" in md                # V5 disqualified on line 1
    assert "How computed" in md and "per_kg" in md                       # V5 per-kg trace shown
    assert not at.exception
