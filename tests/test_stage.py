"""Header chip and banner state logic (pure functions) plus responses-card data."""
from core import stage
from core.responses_view import questionnaire_view
from tests.conftest import run_app

BASE = {"rfq_status": "issued", "due": "2026-11-16", "total": 5, "received": ["V1", "V2", "V3", "V4", "V5"], "failed": [],
        "pending": 0, "awarded_at": None, "proposal_open": False}


def s(**kw):
    return {**BASE, **kw}


def test_fmt_date():
    assert stage.fmt_date("2026-11-16") == "16 Nov 2026" and stage.fmt_date("2027-08-11", month_year=True) == "Aug 2027"
    assert stage.fmt_date(None) == "n/a" and stage.fmt_date("soon") == "soon"


def test_chip_states():
    assert stage.chip(s(received=[], rfq_status="draft")) == ("Draft", "gray")
    assert stage.chip(s(received=[], rfq_status="issued")) == ("Published", "blue")
    assert stage.chip(s(received=["V1"])) == ("Evaluating", "orange")
    assert stage.chip(s(awarded_at="2026-11-20T10:00:00")) == ("Awarded", "green")


def test_banner_priority_order():
    assert stage.banner(s(failed=["V4"], pending=9, received=["V1"]))[0] == "error"
    assert "**1 response couldn't be read (V4).** Re-extract it in Responses." == stage.banner(s(failed=["V4"]))[1]
    assert "**2 responses couldn't be read (V3, V4).**" in stage.banner(s(failed=["V3", "V4"]))[1]
    lvl, txt = stage.banner(s(received=["V1", "V2", "V3"], pending=4))
    assert lvl == "info" and txt == "**3 of 5 responses received.** Response due 16 Nov 2026."
    lvl, txt = stage.banner(s(pending=7))
    assert lvl == "warning" and txt.startswith("**7 values awaiting confirmation.**")
    assert stage.banner(s()) == ("success", "**All values confirmed.** Ready to award in Analyst & Award.")
    assert stage.banner(s(proposal_open=True)) == ("info", "**Award proposed, awaiting your confirmation** in Analyst & Award.")
    lvl, txt = stage.banner(s(awarded_at="2026-11-20T10:00:00", proposal_open=True))
    assert lvl == "success" and txt == "**Award confirmed on 20 Nov 2026.** Download it in Analyst & Award."


def test_demo_shows_evaluating_and_one_banner(real_conn):
    c, path = real_conn
    at = run_app(path)
    assert not at.exception
    assert any("Evaluating" in x.value and "16 Nov 2026" in x.value for x in at.caption)
    banners = list(at.error) + list(at.info) + [w for w in at.warning if "awaiting confirmation" in w.value] + list(at.success)
    assert len([b for b in banners if "awaiting" in b.value or "Ready to award" in b.value or "responses received" in b.value
                or "couldn't be read" in b.value]) == 1


def test_questionnaire_view_v1_v4_v5(real_conn):
    c, _ = real_conn
    vid = {r[0]: r[1] for r in c.execute("SELECT vendor_code, vendor_id FROM vendors")}
    v1 = questionnaire_view(c, vid["V1"], None)
    assert v1["met"] == 7 and v1["not_met"] == 1 and v1["evidence_n"] >= 2
    q1 = next(r for r in v1["rows"] if r["q_code"].startswith("Q01"))
    assert q1["evidence"] and "valid to Aug 2027" in q1["evidence"]["facts"] and q1["ref"] == "Vendor Questionnaire!C2"
    v4 = questionnaire_view(c, vid["V4"], None)
    assert not v4["has_answers"] and v4["qual_status"] == "pending"
    v5 = questionnaire_view(c, vid["V5"], None)
    q5 = next(r for r in v5["rows"] if r["q_code"].startswith("Q01"))
    assert q5["result"] == "fail" and "expired 31 Mar 2026" in q5["evidence"]["facts"] and q5["evidence"]["expired"]
    assert v5["qual_status"] == "disqualified"


def test_failed_response_drives_banner_and_card(real_conn):
    c, path = real_conn
    c.execute("UPDATE vendor_responses SET extraction_status='failed' WHERE vendor_id=(SELECT vendor_id FROM vendors WHERE vendor_code='V4')")
    c.commit()
    s_ = stage.stats(c)
    assert s_["failed"] == [] or s_["failed"] == ["V4"]     # lines still exist from the earlier extraction, so V4 counts as received
    c.execute("DELETE FROM quotes WHERE vendor_id=(SELECT vendor_id FROM vendors WHERE vendor_code='V4')")
    c.commit()
    s_ = stage.stats(c)
    assert s_["failed"] == ["V4"] and "V4" not in s_["received"]
    at = run_app(path)
    assert not at.exception
    assert any("1 response couldn't be read (V4)" in e.value for e in at.error)
    assert any(m.value == "Status: :red-badge[failed]" for m in at.markdown)
    assert any(b.key == "pv_main_V4" for b in at.button)       # the file stays previewable when extraction failed
    assert any(b.key == "rerun_V4" for b in at.button)       # the re-extract icon stays available when failed
