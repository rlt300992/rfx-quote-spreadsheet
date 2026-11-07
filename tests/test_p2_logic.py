"""P2 follow-ups: percentage volume discounts (D1), document-gated knock-out verdicts, date verification."""
import json

import pytest

from core import confidence as conf_mod
from core.config import load_config
from core.recompute import recompute_quote
from core.rules import judge
from extract import extract as ex
from tests.conftest import get_quote


def _set_discounts(conn, vendor, line_no, discounts):
    q = get_quote(conn, vendor, line_no)
    conn.execute("UPDATE quotes SET discounts_json=?, price_tiers_json=NULL WHERE quote_id=?", (json.dumps(discounts), q["quote_id"]))
    recompute_quote(conn, q["quote_id"])
    return get_quote(conn, vendor, line_no)


def _flags(q):
    return json.loads(q["field_meta_json"])["flags"]


def vol(pct, qty, basis):
    return {"pct": pct, "condition_type": "volume_qty", "condition_value": qty, "qty_basis": basis,
            "condition_text": f"{pct}% off for qty >= {qty}", "source": "remarks", "applied": False}


# ------------------------------------------------------------------ D1 for percentage volume discounts
def test_volume_discount_not_met_per_order_is_flagged_and_not_applied(conn):
    q = _set_discounts(conn, "V1", 1, [vol(4, 20000, "per_order")])        # typical order is 10,000
    assert "TIER_NOT_MET_PER_ORDER" in _flags(q) and "CONDITIONAL_DISCOUNT_AVAILABLE" not in _flags(q)
    assert q["base_inr_pc"] == pytest.approx(8.87) and q["tier_applied"] is None


def test_volume_discount_with_unclear_basis_uses_higher_price_and_flags(conn):
    q = _set_discounts(conn, "V1", 19, [vol(3, 50000, "unclear")])         # typical order 10,000; annual would be 120,000
    assert "TIER_BASIS_UNCLEAR" in _flags(q) and "TIER_NOT_MET_PER_ORDER" not in _flags(q)
    assert q["base_inr_pc"] == pytest.approx(33.60)                         # not the 3% price
    assert any("higher price used" in t for t in json.loads(q["normalization_trace_json"]))


def test_volume_discount_is_applied_when_the_condition_is_verifiably_met(conn):
    q = _set_discounts(conn, "V1", 1, [vol(4, 20000, "annual")])           # annual qty is 120,000
    assert q["base_inr_pc"] == pytest.approx(8.87 * 0.96) and q["tier_qty_used"] == 120000
    assert "TIER_NOT_MET_PER_ORDER" not in _flags(q) and q["discount_applied_pct"] in (None, 0)
    conn.execute("UPDATE line_items SET typical_order_qty=25000 WHERE line_no=1")
    recompute_quote(conn, get_quote(conn, "V1", 1)["quote_id"])
    q = _set_discounts(conn, "V1", 1, [vol(4, 20000, "per_order")])        # now 25,000 per order meets it
    assert q["base_inr_pc"] == pytest.approx(8.87 * 0.96)


def test_non_volume_conditional_discounts_still_need_the_buyer_to_toggle(conn):
    d = [{"pct": 2, "condition_type": "payment_days_max", "condition_value": 15, "condition_text": "2% if paid in 15 days",
          "source": "footnote", "applied": False}, vol(4, 20000, "per_order")]
    q = _set_discounts(conn, "V1", 1, d)
    assert "CONDITIONAL_DISCOUNT_AVAILABLE" in _flags(q) and q["base_inr_pc"] == pytest.approx(8.87)


# ------------------------------------------------------------------ document-decided verdicts use the document's confidence
def _q(ro, code):
    return ro.execute("SELECT * FROM rfx_questions WHERE q_code=?", (code,)).fetchone()


def _doc(expiry, conf, matches=1):
    return {"doc_type": "ISO9001", "doc_number": "X1", "expiry_date": expiry, "holder_name": "H", "holder_matches_vendor": matches, "confidence": conf}


def test_expired_certificate_fails_even_when_the_answer_text_is_weak(ro):
    cfg = load_config()
    weak_answer = {"answer_value": "yes", "confidence": 0.70}              # terse email: low answer confidence
    res, why, _ = judge(_q(ro, "Q01_ISO9001"), weak_answer, _doc("2026-03-31", 0.99), "2026-11-16", cfg)
    assert res == "fail" and "expired" in why


def test_low_confidence_document_cannot_decide_a_knockout(ro):
    cfg = load_config()
    res, why, _ = judge(_q(ro, "Q01_ISO9001"), {"answer_value": "yes", "confidence": 0.99}, _doc("2026-03-31", 0.80), "2026-11-16", cfg)
    assert res == "unknown" and "knock-out minimum" in why


# ------------------------------------------------------------------ verification signals
def test_document_confidence_from_verified_fields_and_dates_in_any_written_form():
    cfg = load_config()
    assert conf_mod.score_document("pdf", cfg, [True, True, True])[0] == pytest.approx(0.99)
    assert conf_mod.score_document("pdf", cfg, [True, True, None])[0] == pytest.approx(0.95)
    assert conf_mod.score_document("pdf", cfg, [False, True, True])[0] < 0.95       # holder not found in the text
    for text in ("valid until 31 Mar 2026", "Expiry: 31/03/2026", "31-03-2026", "March 31, 2026", "2026-03-31", "31 MARCH 2026"):
        assert ex.date_in_text("2026-03-31", text) is True, text
    assert ex.date_in_text("2026-03-31", "valid until 31 Mar 2027") is False
    assert ex.date_in_text(None, "anything") is None and ex.date_in_text("2026-03-31", None) is None
    assert ex.date_in_text("2026-04-05", "issued 5 Apr 2026") is True               # day without zero padding


def test_prompts_are_versioned_individually():
    cfg = load_config()
    assert ex.prompt_version("quote_extract", cfg) == "v4" and ex.prompt_version("doc_extract", cfg) == "v1"
    assert "volume_qty" in ex.load_prompt("quote_extract", "v4") and "volume_qty" not in ex.load_prompt("quote_extract", "v1")
    for name in ("quote_extract", "second_read", "doc_extract", "clarify"):
        assert (ex.PROMPT_DIR / f"{name}_{ex.prompt_version(name, cfg)}.md").exists()
