import json

import pytest

from core import actions, audit
from core.recompute import recompute_all, recompute_quote
from core.validate import ensure_completeness
from tests.conftest import get_quote

RAW_COLS = ["raw_price_text", "raw_price", "raw_currency", "raw_unit", "tax_basis", "gst_rate_pct",
            "price_tiers_json", "discounts_json", "charges_json", "freight_basis", "raw_freight_value",
            "payment_terms_days", "offered_qty", "spec_match"]


def flags(row):
    return json.loads(row["field_meta_json"])["flags"]


def field(row, name):
    return json.loads(row["field_meta_json"])["fields"][name]


def line(c, n):
    return c.execute("SELECT * FROM line_items WHERE line_no=?", (n,)).fetchone()


# ------------------------------------------------------------------ raw is immutable
def test_raw_columns_never_overwritten(conn):
    before = [tuple(r) for r in conn.execute(f"SELECT {','.join(RAW_COLS)} FROM quotes ORDER BY quote_id")]
    recompute_all(conn)
    after = [tuple(r) for r in conn.execute(f"SELECT {','.join(RAW_COLS)} FROM quotes ORDER BY quote_id")]
    assert before == after


# ------------------------------------------------------------------ V2 "Rate/box" trap
@pytest.mark.parametrize("ln,raw", [(6, 620.0), (8, 944.0), (12, 1229.0)])
def test_v2_per_100_in_rate_box_column_trips_sanity(ro, ln, raw):
    q = get_quote(ro, "V2", ln)
    assert q["raw_price"] == raw and q["raw_unit"] == "per_piece"  # as the "Rate/box" header said
    assert {"SANITY_RANGE_FAIL", "UNIT_AMBIGUOUS"} <= set(flags(q))
    assert field(q, "price")["status"] == "must_confirm" and field(q, "unit")["status"] == "must_confirm"
    assert q["award_eligible"] == 0
    assert "open_must_confirm:price" in json.loads(q["ineligible_reasons_json"])
    detail = [c for c in json.loads(q["validation_json"]) if c["check"] == "unit_hypothesis"][0]["detail"]
    assert f"{raw / 100:.2f}" in detail  # the app tells the buyer what per-100 would be


def test_v2_other_lines_are_not_flagged(ro):
    for ln in range(1, 31):
        if ln not in (6, 8, 12):
            assert "SANITY_RANGE_FAIL" not in flags(get_quote(ro, "V2", ln)), ln


def test_correcting_unit_to_per_100_resolves_trap_and_is_audited(conn):
    q = get_quote(conn, "V2", 6)
    with pytest.raises(ValueError):  # a correction needs a reason
        actions.correct_field(conn, q["quote_id"], "raw_unit", "per_100", reason="")
    actions.correct_field(conn, q["quote_id"], "raw_unit", "per_100", reason="Vendor PDF footnote: rates are per 100")
    q2 = get_quote(conn, "V2", 6)
    assert q2["raw_unit"] == "per_piece"  # raw untouched
    assert q2["base_inr_pc"] == pytest.approx(6.20)
    assert "SANITY_RANGE_FAIL" not in flags(q2) and q2["award_eligible"] == 1
    a = conn.execute("SELECT * FROM audit_log WHERE action='override' AND entity_id=?", (q["quote_id"],)).fetchone()
    assert a["old_value"] == "per_piece" and a["new_value"] == "per_100" and "footnote" in a["reason"]


# ------------------------------------------------------------------ D3 MSME
def test_msme_cap_v2_90_to_45(ro):
    q = get_quote(ro, "V2", 1)
    assert q["payment_terms_days"] == 90 and q["effective_vendor_days"] == 45
    assert "MSME_TERMS_CAPPED" in flags(q) and field(q, "payment_terms")["status"] == "spot_check"


def test_no_cap_for_medium_none_or_unstated_terms(ro):
    assert "MSME_TERMS_CAPPED" not in flags(get_quote(ro, "V1", 1))   # medium, 60
    assert "MSME_TERMS_CAPPED" not in flags(get_quote(ro, "V3", 1))   # none, 60
    assert "MSME_TERMS_CAPPED" not in flags(get_quote(ro, "V5", 1))   # small, 45 is within the cap
    assert get_quote(ro, "V4", 2)["effective_vendor_days"] == 60      # micro, no terms stated -> standard


def test_msme_category_mismatch_keeps_master_value(conn):
    conn.execute("UPDATE vendor_responses SET declared_msme_category='micro' WHERE vendor_id=(SELECT vendor_id FROM vendors WHERE vendor_code='V2')")
    recompute_all(conn)
    q = get_quote(conn, "V2", 1)
    assert "MSME_CATEGORY_MISMATCH" in flags(q)
    assert q["effective_vendor_days"] == 45  # master 'small' still applies


# ------------------------------------------------------------------ D5 payment loading
def test_payment_loading_formula_v2_line5(ro):
    q = get_quote(ro, "V2", 5)
    colours = line(ro, 5)["print_colours"]
    landed = 15.30 + 0.60
    assert q["landed_inr_pc"] == pytest.approx(landed)
    loading = landed * 0.109 * (60 - min(45, 90)) / 365
    onetime = 2500 * colours / line(ro, 5)["annual_qty"]
    assert q["payment_loading_inr_pc"] == pytest.approx(loading, abs=1e-4)
    assert q["onetime_inr_pc"] == pytest.approx(onetime, abs=1e-4)
    assert q["evaluated_inr_pc"] == pytest.approx(landed + onetime + loading, abs=2e-4)


def test_longer_credit_is_credited_symmetrically_and_capped_at_std_plus_30(conn):
    # V3 (not MSME): 120 days quoted -> credit capped at 60+30 = 90 days -> negative loading
    conn.execute("UPDATE quotes SET payment_terms_days=120 WHERE vendor_id=(SELECT vendor_id FROM vendors WHERE vendor_code='V3')")
    recompute_all(conn)
    q = get_quote(conn, "V3", 1)
    assert q["payment_loading_inr_pc"] == pytest.approx(q["landed_inr_pc"] * 0.109 * (60 - 90) / 365, abs=1e-4)
    assert q["evaluated_inr_pc"] < q["landed_inr_pc"]


def test_ranking_uses_evaluated_and_grid_shows_both(ro):
    r = ro.execute("""SELECT landed_inr_pc, evaluated_inr_pc, rank_evaluated FROM v_comparison_grid
                      WHERE vendor_code='V2' AND line_no=5""").fetchone()
    assert r["landed_inr_pc"] != r["evaluated_inr_pc"] and r["rank_evaluated"] is not None


# ------------------------------------------------------------------ D4 freight
def test_freight_imputed_for_v4_and_v5_uses_highest_other_vendor(ro):
    for code, ln in (("V4", 2), ("V5", 1), ("V4", 15), ("V5", 27)):
        q = get_quote(ro, code, ln)
        others = [get_quote(ro, v, ln)["raw_freight_value"] for v in ("V2", "V3")]
        assert q["freight_imputed"] == 1 and q["freight_inr_pc"] == pytest.approx(max(others))
        src = ro.execute("SELECT vendor_id FROM quotes WHERE quote_id=?", (q["freight_imputed_from_quote_id"],)).fetchone()
        assert src["vendor_id"] != q["vendor_id"]
        assert "FREIGHT_IMPUTED" in flags(q) and field(q, "freight")["status"] != "auto_ok"


def test_freight_included_and_quoted_are_not_imputed(ro):
    assert get_quote(ro, "V1", 1)["freight_inr_pc"] == 0 and get_quote(ro, "V1", 1)["freight_imputed"] == 0
    assert get_quote(ro, "V2", 1)["freight_inr_pc"] == pytest.approx(0.6)


def test_freight_falls_back_to_last_year_then_to_unknown(conn):
    # nobody else quotes freight on line 2 -> V5 uses its own last-year freight (2), V4 has none -> unknown
    conn.execute("UPDATE quotes SET freight_basis='included', raw_freight_value=0 WHERE line_id=(SELECT line_id FROM line_items WHERE line_no=2) AND vendor_id IN (SELECT vendor_id FROM vendors WHERE vendor_code IN ('V2','V3'))")
    recompute_all(conn)
    v5, v4 = get_quote(conn, "V5", 2), get_quote(conn, "V4", 2)
    ly = conn.execute("SELECT freight_inr_pc FROM last_year_prices WHERE vendor_id=5 AND catalog_sku=?", (line(conn, 2)["catalog_sku"],)).fetchone()[0]
    assert v5["freight_imputed"] == 2 and v5["freight_inr_pc"] == pytest.approx(ly)
    assert v4["freight_inr_pc"] is None and v4["landed_inr_pc"] is None and v4["evaluated_inr_pc"] is None
    assert "FREIGHT_UNKNOWN" in flags(v4) and "freight_unknown" in json.loads(v4["ineligible_reasons_json"])
    assert v4["award_eligible"] == 0  # never invented


# ------------------------------------------------------------------ D1 tiers
def test_tier_not_met_per_order_uses_list_price(ro):
    q = get_quote(ro, "V1", 1)
    assert "TIER_NOT_MET_PER_ORDER" in flags(q) and q["tier_applied"] is None
    assert q["base_inr_pc"] == pytest.approx(8.87)  # not the 4%-off price


def test_tier_basis_unclear_defaults_to_higher_price(ro):
    q = get_quote(ro, "V1", 19)
    assert "TIER_BASIS_UNCLEAR" in flags(q) and q["tier_applied"] is None
    assert q["base_inr_pc"] == pytest.approx(33.60)


def test_tier_applies_once_order_qty_is_met_and_annual_basis_is_honoured(conn):
    conn.execute("UPDATE line_items SET typical_order_qty=25000 WHERE line_no=1")
    recompute_quote(conn, get_quote(conn, "V1", 1)["quote_id"])
    q = get_quote(conn, "V1", 1)
    assert q["tier_qty_used"] == 25000 and q["base_inr_pc"] == pytest.approx(round(887 * 0.96, 2) / 100)
    # an explicit annual basis matches against annual qty (120,000 >= 20,000) even with a small order
    conn.execute("UPDATE line_items SET typical_order_qty=10000 WHERE line_no=1")
    tiers = json.loads(q["price_tiers_json"]); tiers[0]["qty_basis"] = "annual"
    conn.execute("UPDATE quotes SET price_tiers_json=? WHERE quote_id=?", (json.dumps(tiers), q["quote_id"]))
    recompute_quote(conn, q["quote_id"])
    q = get_quote(conn, "V1", 1)
    assert q["tier_qty_used"] == 120000 and q["base_inr_pc"] == pytest.approx(round(887 * 0.96, 2) / 100)


# ------------------------------------------------------------------ D2 discounts
def test_conditional_discount_never_auto_applied_and_toggle_is_audited(conn):
    q = get_quote(conn, "V2", 5)
    assert q["discount_applied_pct"] == 0 and q["base_inr_pc"] == pytest.approx(15.30)
    assert "CONDITIONAL_DISCOUNT_AVAILABLE" in flags(q)
    actions.toggle_discount(conn, q["quote_id"], 0, True)
    q = get_quote(conn, "V2", 5)
    assert q["discount_applied_pct"] == 2 and q["base_inr_pc"] == pytest.approx(15.30 * 0.98)
    actions.toggle_discount(conn, q["quote_id"], 0, False)
    assert get_quote(conn, "V2", 5)["base_inr_pc"] == pytest.approx(15.30)
    rows = conn.execute("SELECT old_value,new_value FROM audit_log WHERE action='discount_toggle' ORDER BY audit_id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("false", "true"), ("true", "false")]


# ------------------------------------------------------------------ FX, per-kg, last-year
def test_v3_usd_snapshot_and_conversion(ro):
    q = get_quote(ro, "V3", 1)
    assert q["fx_rate"] == 95.17 and q["fx_rate_date"] == "2026-10-26"
    assert q["base_inr_pc"] == pytest.approx(0.0866 * 95.17, abs=1e-4)
    assert {"FX_CONVERTED", "GST_RATE_MISMATCH", "PRICE_NOT_FIRM"} <= set(flags(q))
    assert q["gst_rate_pct"] == 12  # stored, never added to the price


def test_v5_per_kg_is_derived_with_visible_formula_and_must_confirm(ro):
    q = get_quote(ro, "V5", 1)
    assert q["base_inr_pc"] == pytest.approx(38 * 0.192, abs=1e-4)
    assert "DERIVED_PER_KG" in flags(q) and field(q, "price")["status"] == "must_confirm"
    assert any("Weight formula" in t for t in json.loads(q["normalization_trace_json"]))
    assert get_quote(ro, "V5", 13)["base_inr_pc"] == pytest.approx(42 * 0.856, abs=1e-3)  # 5-ply at 42/kg


def test_v5_same_as_last_year_lookup_and_spec_change(ro):
    for ln in (27, 28, 29, 30):
        q = get_quote(ro, "V5", ln)
        lyp = ro.execute("SELECT * FROM last_year_prices WHERE vendor_id=5 AND catalog_sku=?", (line(ro, ln)["catalog_sku"],)).fetchone()
        assert q["base_inr_pc"] == pytest.approx(lyp["price_inr_pc"]) and q["ref_lyp_id"] == lyp["lyp_id"]
        assert "IMPLIED_BY_BLANKET" in flags(q) and field(q, "price")["status"] == "must_confirm"
        assert ("LY_SPEC_CHANGED" in flags(q)) == (ln in (28, 30))


def test_same_as_last_year_without_reference_is_flagged_not_invented(conn):
    conn.execute("UPDATE quotes SET ref_lyp_id=NULL")  # release FK references before removing the reference row
    conn.execute("DELETE FROM last_year_prices WHERE vendor_id=5 AND catalog_sku=?", (line(conn, 27)["catalog_sku"],))
    recompute_quote(conn, get_quote(conn, "V5", 27)["quote_id"])
    q = get_quote(conn, "V5", 27)
    assert "LY_REF_MISSING" in flags(q) and q["base_inr_pc"] is None and q["landed_inr_pc"] is None


# ------------------------------------------------------------------ layer 0 completeness
def test_v4_has_exactly_three_not_quoted_rows_created_by_layer_0(ro):
    rows = ro.execute("""SELECT l.line_no, q.* FROM quotes q JOIN line_items l USING (line_id)
                         JOIN vendors v USING (vendor_id) WHERE v.vendor_code='V4' AND q.quote_status='not_quoted'
                         ORDER BY l.line_no""").fetchall()
    assert [r["line_no"] for r in rows] == [26, 28, 29]
    for r in rows:  # missing is never zero
        assert r["raw_price"] is None and r["landed_inr_pc"] is None and r["evaluated_inr_pc"] is None
        assert r["award_eligible"] == 0 and json.loads(r["ineligible_reasons_json"]) == ["not_quoted"]
    assert ro.execute("SELECT lines_quoted, lines_not_quoted FROM v_response_summary WHERE vendor_code='V4'").fetchone()[:] == (27, 3)


def test_layer_0_is_idempotent_and_partial_revisions_are_not_padded(conn):
    rid = conn.execute("SELECT response_id FROM vendor_responses WHERE vendor_id=4").fetchone()[0]
    assert ensure_completeness(conn, rid) == []
    conn.execute("UPDATE vendor_responses SET revision_scope='partial' WHERE response_id=?", (rid,))
    conn.execute("DELETE FROM quotes WHERE response_id=? AND quote_status='not_quoted'", (rid,))
    assert ensure_completeness(conn, rid) == []


def test_unmatched_quote_is_held_for_review_not_ranked(conn):
    rid = conn.execute("SELECT response_id FROM vendor_responses WHERE vendor_id=1").fetchone()[0]
    qid = conn.execute("INSERT INTO quotes (response_id, vendor_id, line_id, quote_status, raw_price, raw_unit, raw_currency) VALUES (?,1,NULL,'unmatched',50,'per_piece','INR')", (rid,)).lastrowid
    recompute_quote(conn, qid)
    q = conn.execute("SELECT * FROM quotes WHERE quote_id=?", (qid,)).fetchone()
    assert q["award_eligible"] == 0 and q["review_state"] == "must_confirm" and q["evaluated_inr_pc"] is None


# ------------------------------------------------------------------ validation layers & confidence
def test_clean_excel_lines_are_auto_ok_and_eligible(ro):
    q = get_quote(ro, "V1", 2)
    fields = json.loads(q["field_meta_json"])["fields"]
    assert q["review_state"] == "auto_ok" and q["award_eligible"] == 1
    assert all(f["status"] == "auto_ok" for f in fields.values())
    assert all(fields[k]["confidence"] >= 0.95 for k in ("price", "unit", "currency", "freight", "tax_basis"))  # core auto >= 95%
    assert fields["payment_terms"]["confidence"] >= 0.85  # terms auto >= 85%


def test_v1_spec_deviation_and_substitute_need_acceptance(conn):
    for ln, flag in ((16, "SPEC_DEVIATION"), (25, "SPEC_SUBSTITUTE")):
        q = get_quote(conn, "V1", ln)
        assert flag in flags(q) and field(q, "spec_match")["status"] == "must_confirm" and q["award_eligible"] == 0
    q = get_quote(conn, "V1", 25)
    with pytest.raises(ValueError):
        actions.accept_spec(conn, q["quote_id"], reason=" ")
    actions.accept_spec(conn, q["quote_id"], reason="QA approved 3-ply 200/150/200 for this SKU")
    # the price flag from the sanity check on the cheaper substitute remains for the buyer to confirm separately
    q = get_quote(conn, "V1", 25)
    assert field(q, "spec_match")["status"] == "confirmed" and "spec_substitute" not in json.loads(q["ineligible_reasons_json"])


def test_v4_photo_smudged_line_is_must_confirm_price(ro):
    q = get_quote(ro, "V4", 14)
    assert "LOW_CONFIDENCE_OCR" in flags(q) and field(q, "price")["status"] == "must_confirm"
    assert field(get_quote(ro, "V4", 2), "price")["status"] == "spot_check"  # photo never auto-passes price


def test_confidence_comes_from_signals_not_the_model(ro):
    f = field(get_quote(ro, "V1", 2), "price")
    assert f["signals"][0].startswith("base 0.92") and any("sanity_vs_last_year passed" in s for s in f["signals"])


def test_partial_quantity_blocks_the_line_for_v3(ro):
    q = get_quote(ro, "V3", 10)
    assert "PARTIAL_QTY" in flags(q) and "partial_qty" in json.loads(q["ineligible_reasons_json"])


def test_second_read_disagreement_is_flagged(conn):
    q = get_quote(conn, "V1", 2)
    rid = q["response_id"]
    sr = json.loads(conn.execute("SELECT second_read_json FROM vendor_responses WHERE response_id=?", (rid,)).fetchone()[0])
    sr[str(q["quote_id"])]["raw_price"] = 1461.0  # transposed digits; the first read says 1416
    conn.execute("UPDATE vendor_responses SET second_read_json=? WHERE response_id=?", (json.dumps(sr), rid))
    recompute_quote(conn, q["quote_id"])
    q = get_quote(conn, "V1", 2)
    assert "SECOND_READ_MISMATCH" in flags(q) and field(q, "price")["status"] == "must_confirm"


def test_arithmetic_check_catches_a_wrong_line_total(conn):
    q = get_quote(conn, "V2", 5)
    good = 15.30 * line(conn, 5)["annual_qty"]
    conn.execute("UPDATE quotes SET vendor_line_total=? WHERE quote_id=?", (good, q["quote_id"]))
    recompute_quote(conn, q["quote_id"])
    assert "ARITHMETIC_FAIL" not in flags(get_quote(conn, "V2", 5))
    conn.execute("UPDATE quotes SET vendor_line_total=? WHERE quote_id=?", (good * 10, q["quote_id"]))
    recompute_quote(conn, q["quote_id"])
    assert "ARITHMETIC_FAIL" in flags(get_quote(conn, "V2", 5))


def test_confirm_with_open_flags_warns_but_does_not_block_and_is_audited(conn):
    q = get_quote(conn, "V5", 1)
    batch = audit.new_batch_id()
    done = actions.confirm_fields(conn, q["quote_id"], ["price"], batch_id=batch)
    assert done == ["price"]
    q = get_quote(conn, "V5", 1)
    assert field(q, "price")["status"] == "confirmed" and "DERIVED_PER_KG" in flags(q)
    a = conn.execute("SELECT * FROM audit_log WHERE action='confirm' AND batch_id=?", (batch,)).fetchone()
    assert "DERIVED_PER_KG" in a["evidence_json"]


# ------------------------------------------------------------------ audit log
def test_audit_requires_reason_and_is_append_only(conn):
    with pytest.raises(ValueError):
        audit.log(conn, "override", ("quote", 1), field="raw_price", old=1, new=2, reason=None)
    aid = audit.log(conn, "confirm", "quote:1", field="price", new="confirmed", batch_id="b1")
    with pytest.raises(Exception):
        conn.execute("UPDATE audit_log SET reason='x' WHERE audit_id=?", (aid,))
    with pytest.raises(Exception):
        conn.execute("DELETE FROM audit_log WHERE audit_id=?", (aid,))
