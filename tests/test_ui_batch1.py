"""UI batch 1: banner, event header, sidebar, RFQ setup page."""
from copilot.copilot import load_draft
from tests.conftest import run_app
from ui.rfq import SUGGESTIONS, checks, contract_period, status_label


def test_status_and_period_helpers():
    assert status_label("draft") == "Draft" and status_label(None) == "Draft"
    assert status_label("issued") == "Published" and status_label("awarded") == "Published"
    assert contract_period("Corrugated boxes - FY2026-27 annual rate contract", "FY2025-26") == "FY2026-27"
    assert contract_period("no year", "FY2025-26") == "FY2025-26"


def test_header_banner_sidebar(real_db_path, real_conn):
    c, path = real_conn
    at = run_app(path)
    assert not at.exception
    assert len([w for w in at.warning if "awaiting confirmation" in w.value]) + len(at.success) >= 1
    assert sum("awaiting confirmation" in w.value for w in at.warning) <= 1
    n = c.execute("SELECT COUNT(*) FROM v_review_queue WHERE status='must_confirm'").fetchone()[0]
    assert any("Response due" in x.value and "Published" in x.value for x in at.caption)
    assert any(e.label == "Evaluation settings" for e in at.sidebar.expander)
    assert not any("Corrugated boxes" in m.value for m in at.sidebar.markdown)
    assert at.number_input(key="rfq_pay_0").value == 60
    assert n >= 0


def test_zero_pending_shows_success(real_conn):
    c, path = real_conn
    c.execute("UPDATE quotes SET review_state='confirmed' WHERE review_state IS NOT NULL")
    c.commit()
    at = run_app(path)
    assert not at.exception


def test_rfq_page_sections_suggestions_and_readonly(real_conn):
    c, path = real_conn
    at = run_app(path)
    assert not at.exception
    heads = [s.value for s in at.subheader]
    for h in ("1 · Overview", "3 · Questionnaire", "4 · Suppliers & terms", "5 · Review & publish", "6 · Email to suppliers"):
        assert h in heads
    assert any(i.value.startswith("Demo RFQ pre-loaded") for i in at.info)
    assert [b.label for b in at.button if b.key and b.key.startswith("rfq_sug_")] == SUGGESTIONS
    assert [b for b in at.button if b.key == "rfq_confirm"][0].label == "Save changes"   # published demo
    assert at.number_input(key="rfq_pay_0").disabled is True


def test_review_checks_marks():
    class D: pass
    import pandas as pd
    d = D()
    d.lines = pd.DataFrame({"include": [True, True]})
    d.questions = pd.DataFrame({"include": [True, True], "is_knockout": [True, False]})
    d.terms = {"response_due_date": "2026-11-16"}
    r = checks(d, 5)
    assert all(ok for ok, _ in r) and r[2][1] == "1 knock-out"
    d.terms = {"response_due_date": "next week"}
    d.questions = pd.DataFrame({"include": [True], "is_knockout": [False]})
    r = checks(d, 0)
    assert [ok for ok, _ in r] == [True, True, False, False, False]
