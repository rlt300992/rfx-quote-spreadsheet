"""Review & Reconcile items: grouping, counts, every action type (audit + recompute), Ask vendor, bulk confirm, undo, and the tab."""
import json

import pytest

from core import review_items as RI
from core.queries import review_rows
from tests.conftest import run_app


def _items(c):
    return RI.build_items(c)


def _item(c, vendor, kind, line=None):
    for i in _items(c):
        if i["vendor"] == vendor and i["kind"] == kind and (line is None or i["line_nos"] == [line]):
            return i
    raise AssertionError(f"no item {vendor} {kind} {line}")


def _q(c, qid):
    return c.execute("SELECT * FROM quotes WHERE quote_id=?", (qid,)).fetchone()


def _n_audit(c, action):
    return c.execute("SELECT COUNT(*) FROM audit_log WHERE action=?", (action,)).fetchone()[0]


# ------------------------------------------------------------------ grouping and counts
def test_vendor_terms_are_one_item_not_thirty_values(real_conn):
    c, _ = real_conn
    fr = _item(c, "V4", "freight")
    assert fr["scope"] == "vendor" and len(fr["quotes"]) == 27                       # imputed freight on 27 lines = ONE item
    pay = _item(c, "V2", "payment")
    assert pay["scope"] == "vendor" and len(pay["quotes"]) == 30                     # the 45-day cap on 30 lines = ONE item
    assert _item(c, "V2", "cash_discount")["scope"] == "vendor"
    lines = [i for i in _items(c) if i["vendor"] == "V2" and i["scope"] == "line"]
    assert sorted(i["line_nos"][0] for i in lines) == [6, 8, 12]                     # the three per-100 prices stay line items


def test_item_count_is_far_smaller_than_the_value_count_and_matches_the_pills(real_conn):
    c, _ = real_conn
    from core.queries import pending_confirmation_count
    items = _items(c)
    n_open = RI.open_item_count(c)
    assert n_open == sum(i["state"] == "open" for i in RI.build_cards(items))
    assert 0 < n_open < pending_confirmation_count(c) / 4
    per_vendor = sum(RI.vendor_progress(items, v)[1] - RI.vendor_progress(items, v)[0] for v in ("V1", "V2", "V3", "V4", "V5"))
    assert per_vendor == n_open                                          # pills and banner both count cards (one per line)


def test_banner_counts_items(real_conn):
    c, path = real_conn
    at = run_app(path)
    n = RI.open_item_count(c)
    assert any(f"**{n} items awaiting review.**" in w.value for w in at.warning)


def test_every_open_value_belongs_to_an_item(real_conn):
    c, _ = real_conn
    owned = {(q, f) for i in _items(c) for q, f in i["open_fields"]}
    for r in review_rows(c):
        if r["quote_status"] == "quoted":
            for f in r["open_fields"]:
                assert (r["quote_id"], f) in owned, (r["vendor_code"], r["line_no"], f)


# ------------------------------------------------------------------ actions: audit + recompute
def test_confirm_vendor_item_confirms_all_its_lines_with_audit(real_conn):
    c, _ = real_conn
    it = _item(c, "V2", "payment")
    before = _n_audit(c, "confirm")
    RI.act(c, it, "confirm", "Cap understood")
    assert _n_audit(c, "confirm") - before == 30
    assert _item(c, "V2", "payment")["state"] == "done"


def test_correct_price_unit_recomputes_and_apply_to_all(real_conn):
    c, _ = real_conn
    it = _item(c, "V2", "price", 6)
    qid = list(it["quotes"])[0]
    old = _q(c, qid)["evaluated_inr_pc"]
    others = [i for i in _items(c) if i["vendor"] == "V2" and i["kind"] == "price" and i["line_nos"] != [6]]
    before = _n_audit(c, "override")
    RI.act(c, it, "correct", "Rate/box means per 100", ("per_100", 620.0), apply_to=others)
    assert _n_audit(c, "override") - before == 6                                       # unit + price on three quotes
    assert _q(c, qid)["base_inr_pc"] == pytest.approx(6.20) and _q(c, qid)["evaluated_inr_pc"] < old / 50
    for i in others:
        assert _q(c, list(i["quotes"])[0])["base_inr_pc"] < 20
    assert _item(c, "V2", "price", 6)["state"] == "done"


def test_preview_does_not_save(real_conn):
    c, _ = real_conn
    qid = list(_item(c, "V2", "price", 6)["quotes"])[0]
    before = dict(_q(c, qid))
    res = RI.preview_correction(c, qid, "per_100", 620.0)
    assert res["base_inr_pc"] == pytest.approx(6.20) and res["landed_inr_pc"] > 6.2
    assert dict(_q(c, qid)) == before and _n_audit(c, "override") == 0


def test_spec_accept_and_reject(real_conn):
    c, _ = real_conn
    a = _item(c, "V1", "spec", 16)
    RI.act(c, a, "accept", "QA agrees")
    assert _q(c, list(a["quotes"])[0])["spec_accepted"] == 1 and _n_audit(c, "spec_accept") == 1
    r = _item(c, "V1", "spec", 25)
    RI.act(c, r, "reject", "3-ply is not acceptable for the mixed-SKU shipper")
    q = _q(c, list(r["quotes"])[0])
    assert q["award_eligible"] == 0 and _n_audit(c, "reject") == 1


def test_tier_basis_per_year_applies_the_discount(real_conn):
    c, _ = real_conn
    it = _item(c, "V1", "tier")
    qid = list(it["quotes"])[0]
    old = _q(c, qid)["evaluated_inr_pc"]
    RI.act(c, it, "tier_year", "Per year")
    assert _q(c, qid)["evaluated_inr_pc"] < old and _n_audit(c, "override") >= 1


def test_cash_discount_apply_toggles_every_line(real_conn):
    c, _ = real_conn
    it = _item(c, "V2", "cash_discount")
    qid = list(it["quotes"])[0]
    old = _q(c, qid)["evaluated_inr_pc"]
    RI.act(c, it, "apply", "We will pay in 15 days")
    assert _n_audit(c, "discount_toggle") == 30 and _q(c, qid)["evaluated_inr_pc"] < old
    assert _item(c, "V2", "cash_discount")["state"] == "done"


def test_enter_vendor_freight(real_conn):
    c, _ = real_conn
    it = _item(c, "V4", "freight")
    qid = list(it["quotes"])[0]
    RI.act(c, it, "enter_freight", "Vendor confirmed by phone", 1.5)
    q = _q(c, qid)
    assert q["freight_inr_pc"] == pytest.approx(1.5) and q["freight_imputed"] in (0, None)


# ------------------------------------------------------------------ ask vendor
def test_ask_vendor_adds_to_the_draft_counts_as_handled_and_keeps_the_value_out(real_conn):
    c, _ = real_conn
    it = _item(c, "V4", "ocr", 14)
    qid = list(it["quotes"])[0]
    n0 = RI.open_item_count(c)
    RI.act(c, it, "ask", "")
    draft = c.execute("SELECT clarification_draft FROM vendor_responses WHERE clarification_draft IS NOT NULL AND vendor_id=(SELECT vendor_id FROM vendors WHERE vendor_code='V4')").fetchone()[0]
    assert "Added from Review & Reconcile" in draft and "line" in draft.lower()
    again = _item(c, "V4", "ocr", 14)
    assert again["state"] == "asked" and RI.open_item_count(c) == n0 - 1
    assert _q(c, qid)["award_eligible"] == 0                                           # still excluded until resolved
    assert RI.vendor_progress(_items(c), "V4")[0] >= 1                                 # counts as handled for progress


# ------------------------------------------------------------------ bulk confirm
def test_bulk_confirm_takes_only_clean_lines_with_one_audit_row(real_conn):
    c, _ = real_conn
    clean, excl, skip = RI.clean_lines(c, "V2")
    assert len(clean) == 27 and sorted(r["line_no"] for r in excl) == [6, 8, 12]
    from core import actions
    n0 = _n_audit(c, "confirm")
    actions.confirm_many(c, [r["quote_id"] for r in clean], skip=skip)
    assert _n_audit(c, "confirm") - n0 == 1
    ev = json.loads(c.execute("SELECT evidence_json FROM audit_log WHERE action='confirm' ORDER BY audit_id DESC LIMIT 1").fetchone()[0])
    assert len(ev["lines"]) == 27
    for r in review_rows(c):
        if r["vendor_code"] == "V2" and r["line_no"] in (6, 8, 12):
            assert "price" in r["open_fields"]                                       # flagged lines were not touched
    assert _item(c, "V2", "payment")["state"] == "open"                                # the vendor-term decision is still the buyer's


# ------------------------------------------------------------------ undo
def test_undo_confirm_and_correction(real_conn):
    c, _ = real_conn
    it = _item(c, "V2", "payment")
    did = RI.act(c, it, "confirm", "x")
    assert _item(c, "V2", "payment")["state"] == "done"
    RI.undo(c, did)
    assert _item(c, "V2", "payment")["state"] == "open" and _n_audit(c, "review_undo") == 1
    p = _item(c, "V2", "price", 6)
    qid = list(p["quotes"])[0]
    base = _q(c, qid)["base_inr_pc"]
    did = RI.act(c, p, "correct", "per 100", ("per_100", 620.0))
    assert _q(c, qid)["base_inr_pc"] != base
    RI.undo(c, did)
    assert _q(c, qid)["base_inr_pc"] == pytest.approx(base) and _item(c, "V2", "price", 6)["state"] == "open"
    with pytest.raises(ValueError):
        RI.undo(c, did)


# ------------------------------------------------------------------ the tab
def _btn(at, key):
    return [b for b in at.button if b.key == key][0]


def test_tab_shows_items_and_a_click_confirms_and_offers_undo(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rv_vendor", "V2"))
    assert not at.exception
    keys = {b.key for b in at.button}
    assert "rvb_V2:payment_confirm" in keys and "rvb_V2:cash_discount_keep" in keys and "rv_bulk" in keys
    assert "rv_page" not in keys and not any(x.label.startswith("Only items that could change") for x in at.checkbox)     # page box and checkbox are gone
    _btn(at, "rvb_V2:payment_confirm").click().run()
    assert not at.exception
    assert any("Confirmed: Payment terms" in s.value for s in at.success) and "rv_undo" in {b.key for b in at.button}
    _btn(at, "rv_undo").click().run()
    assert "rvb_V2:payment_confirm" in {b.key for b in at.button}


def test_tab_correct_form_previews_then_saves(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rv_vendor", "V2"))
    _btn(at, "rvb_V2:price:6_correct").click().run()
    assert any("₹6.20/pc" in m.value for m in at.markdown)
    assert _n_audit(c, "override") == 0                                                  # nothing saved yet
    _btn(at, "rvsave_V2:price:6").click().run()
    assert not at.exception and _n_audit(c, "override") >= 2


def test_bulk_dialog_lists_clean_lines_and_confirms(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rv_vendor", "V2"))
    _btn(at, "rv_bulk").click().run()
    assert any(x.label == "Confirm 27 lines" for x in at.button) and any(d.value.shape[0] == 27 for d in at.dataframe)
    assert any("Lines 6, 8, 12 are not included" in x.value for x in at.caption)
    _btn(at, "rvbulk_cancel").click().run()
    assert _n_audit(c, "confirm") == 0
    _btn(at, "rv_bulk").click().run()
    _btn(at, "rvbulk_go").click().run()
    assert _n_audit(c, "confirm") == 1


def test_late_and_disqualified_notes(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rv_vendor", "V4"))
    assert any("after the due date" in i.value for i in at.info)
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rv_vendor", "V5"))
    assert any("disqualified" in i.value for i in at.info)


# ------------------------------------------------------------------ UI batch 4.1: one card per line, verbatim "Vendor wrote", wording
def test_issues_on_one_line_are_one_card(real_conn):
    c, _ = real_conn
    items = RI.build_items(c)
    cards = RI.build_cards(items)
    v1_25 = [x for x in cards if x["vendor"] == "V1" and x["line_nos"] == [25]]
    assert len(v1_25) == 1 and {i["kind"] for i in v1_25[0]["issues"]} == {"price", "spec"}
    assert v1_25[0]["primary"]["kind"] == "price"                                   # the more important issue's actions are offered
    assert len(cards) < len(items) and RI.open_item_count(c) == sum(x["state"] == "open" for x in cards)
    RI.act(c, v1_25[0]["primary"], "confirm", "ok")
    again = [x for x in RI.build_cards(RI.build_items(c)) if x["vendor"] == "V1" and x["line_nos"] == [25]][0]
    assert again["state"] == "open" and again["primary"]["kind"] == "spec"          # the card stays until the spec issue is decided too


def test_vendor_wrote_is_verbatim_and_read_as_is_our_interpretation(real_conn):
    c, _ = real_conn
    spec16 = _item(c, "V1", "spec", 16)
    d = RI.describe(c, spec16)
    assert "Offered in 150 GSM outer liner - meets BS" in d["wrote"]
    assert d["read"] == "**Deviation**: 150 GSM outer liner vs 180 specified (layers 1 and 5)"
    d6 = RI.describe(c, _item(c, "V2", "price", 6))
    assert d6["wrote"].startswith("“620.00”")


def test_comparison_wording_never_says_1x():
    assert "1×" not in RI.vs_peers(6.0, 5.98) and "in line with" in RI.vs_peers(6.0, 5.98)
    assert "104× the other vendors" in RI.vs_peers(620, 5.98)
    assert "31% below the other vendors" in RI.vs_peers(22.84, 33.27, ", because it's a 3-ply substitute")
    assert "because it's a 3-ply substitute" in RI.vs_peers(22.84, 33.27, ", because it's a 3-ply substitute")


def test_tab_has_one_card_per_line_and_pills_count_cards(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rv_vendor", "V1"))
    assert not at.exception
    assert not any(b.key == "rvb_V1:spec:25_accept" for b in at.button)             # line 25 shows the price actions (one card)
    assert "rvb_V1:price:25_confirm" in {b.key for b in at.button}
    assert any(b.key == "rvpill_V1" and ":orange-badge[3]" in b.label for b in at.button)
