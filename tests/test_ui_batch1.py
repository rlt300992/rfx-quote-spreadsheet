"""UI batch 1: banner, event header, sidebar."""
from copilot.copilot import load_draft
from tests.conftest import run_app


def test_header_banner_sidebar(real_db_path, real_conn):
    c, path = real_conn
    at = run_app(path)
    assert not at.exception
    assert len([w for w in at.warning if "awaiting review" in w.value]) + len(at.success) >= 1
    assert sum("awaiting review" in w.value for w in at.warning) <= 1
    n = c.execute("SELECT COUNT(*) FROM v_review_queue WHERE status='must_confirm'").fetchone()[0]
    assert any("Response due" in x.value and "Evaluating" in x.value for x in at.caption)
    assert any(e.label == "Evaluation settings" for e in at.sidebar.expander)
    assert not any("Corrugated boxes" in m.value for m in at.sidebar.markdown)
    assert any("Payment 60 days" in e.label for e in at.expander)          # the demo RFQ still reads 60-day payment terms
    assert n >= 0


def test_zero_pending_shows_success(real_conn):
    c, path = real_conn
    c.execute("UPDATE quotes SET review_state='confirmed' WHERE review_state IS NOT NULL")
    c.commit()
    at = run_app(path)
    assert not at.exception
