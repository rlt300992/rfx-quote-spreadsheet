"""P6: a Gemini failure (quota, 503, timeout, no key) is a friendly message and the saved data keeps working; never a stack trace."""
import io

import pytest

from core import errors
from tests.conftest import run_app


class FakeGeminiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


QUOTA = FakeGeminiError(429, "429 RESOURCE_EXHAUSTED: quota exceeded for metric generate_content_requests_per_day")
BUSY = FakeGeminiError(503, "503 UNAVAILABLE: the model is overloaded")


def _btn(at, key):
    return [b for b in at.button if b.key == key][0]


def _no_trace(at):
    blob = " ".join(str(getattr(x, "value", "")) for x in list(at.warning) + list(at.markdown) + list(at.error) + list(at.get("html")))
    assert "Traceback" not in blob and "FakeGeminiError" not in blob and "RESOURCE_EXHAUSTED" not in blob
    assert not at.exception


def test_error_kinds_have_plain_messages():
    assert errors.kind(QUOTA) == "quota" and errors.kind(BUSY) == "busy"
    assert errors.kind(TimeoutError("read timed out")) == "timeout"
    assert errors.kind(RuntimeError("GEMINI_API_KEY is not set (.env or st.secrets)")) == "key"
    assert errors.kind(ValueError("weird")) == "other"
    for e in (QUOTA, BUSY, TimeoutError("x"), RuntimeError("GEMINI_API_KEY is not set"), ValueError("weird")):
        m = errors.friendly(e)
        assert m.endswith(".") and "429" not in m and "503" not in m and "Error" not in m


def test_analyst_chat_failure_warns_and_answers_from_the_data(real_conn, monkeypatch):
    c, path = real_conn
    monkeypatch.setattr("ui.analyst.ask", lambda *a, **k: (_ for _ in ()).throw(QUOTA))
    at = run_app(path)
    _btn(at, "aa_st_0").click().run()
    _no_trace(at)
    assert any("quota is used up" in w.value for w in at.warning)
    blob = " ".join(str(h.value) for h in at.get("html"))
    assert "Award" in blob and "current best scenario" in blob                      # the fallback is computed from the saved data, labelled as such
    assert c.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0] == 0


def test_rfq_draft_copilot_failure_is_a_warning(real_conn, monkeypatch):
    c, path = real_conn
    from copilot import draft_copilot as DC
    monkeypatch.setattr(DC, "ask", lambda *a, **k: (_ for _ in ()).throw(BUSY))
    at = run_app(path)
    _btn(at, "rfq_start_new").click().run()
    at.chat_input(key="draft_chat_input").set_value("boxes for FY27").run()
    _no_trace(at)
    assert any("Gemini is busy" in w.value for w in at.warning)


def test_demo_rfq_chat_failure_is_a_warning(real_conn, monkeypatch):
    c, path = real_conn
    from copilot import draft_copilot as DC
    monkeypatch.setattr(DC, "answer_about_demo", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("read timed out")))
    at = run_app(path)
    at.chat_input(key="demo_chat_input").set_value("what did we ask?").run()
    _no_trace(at)
    assert any("took too long" in w.value for w in at.warning)


def test_edit_rfq_copilot_failure_is_a_warning(real_conn, monkeypatch):
    c, path = real_conn
    from copilot import copilot as C
    monkeypatch.setattr(C, "ask_copilot", lambda *a, **k: (_ for _ in ()).throw(QUOTA))
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rfq_edit", True))
    at.chat_input(key="rfq_chat_input").set_value("45 days").run()
    _no_trace(at)
    assert any("quota is used up" in w.value for w in at.warning)


def test_reextract_failure_keeps_the_saved_extraction(real_conn, monkeypatch):
    c, path = real_conn
    import extract.gemini_client as g
    from core import jobs
    from extract.extract import load_manifest

    monkeypatch.setattr(g, "generate", lambda *a, **k: (_ for _ in ()).throw(QUOTA))
    monkeypatch.setattr(g, "generate_structured", lambda *a, **k: (_ for _ in ()).throw(QUOTA))
    before = c.execute("SELECT COUNT(*) FROM quotes WHERE is_current=1").fetchone()[0]
    entry = next(e for e in load_manifest() if e["vendor_code"] == "V1")
    jobs.start(c, [entry], db_file=path, wait=True)                       # the real ingest_vendor(force=True), with the model failing
    job = c.execute("SELECT * FROM extraction_jobs WHERE vendor_code='V1'").fetchone()
    assert job["status"] == "failed" and "quota is used up" in job["error"] and "Traceback" not in job["error"]
    assert c.execute("SELECT COUNT(*) FROM quotes WHERE is_current=1").fetchone()[0] == before    # nothing was superseded: the cache stays in use
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rfq_mode", "Demo RFQ"))
    _no_trace(at)
    assert any("Re-extract failed" in m.value for m in at.markdown)
    assert any(b.key == "pv_main_V1" for b in at.button)


def test_previews_and_exports_work_when_gemini_is_down(real_conn, monkeypatch):
    c, path = real_conn
    import extract.gemini_client as g
    monkeypatch.setattr(g, "generate", lambda *a, **k: (_ for _ in ()).throw(QUOTA))
    monkeypatch.setattr(g, "generate_structured", lambda *a, **k: (_ for _ in ()).throw(QUOTA))
    at = run_app(path)
    for vendor in ("V1", "V2", "V4"):
        _btn(at, f"pv_main_{vendor}").click().run()
        _no_trace(at)
    from agent import tools as T
    from export import memo as M
    from export.excel import build_workbook
    ctx = T.ToolContext(c)
    p = T.propose_award(ctx, chat_prompt="x")
    ctx.close()
    assert build_workbook(c, p, [])[:2] == b"PK"
    assert M.memo_pdf(M.memo_data(c, p, []))[:4] == b"%PDF"


def test_cold_start_calls_no_model_and_is_fast(tmp_path, monkeypatch):
    """No database yet: the app builds it from the seed and the committed cache with zero model calls."""
    import time
    import extract.gemini_client as g
    monkeypatch.setattr(g, "generate", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called on cold start")), raising=False)
    monkeypatch.setattr(g, "generate_structured", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called on cold start")), raising=False)
    t = time.time()
    at = run_app(tmp_path / "cold.db")
    took = time.time() - t
    assert not at.exception and not any("hit a problem" in w.value for w in at.warning)
    from core.db import connect
    c = connect(tmp_path / "cold.db")
    assert c.execute("SELECT COUNT(*) FROM quotes WHERE is_current=1").fetchone()[0] == 150
    assert took < 30, took                      # the hard limit; the measured time is reported in the README (the app itself is ~3-5 s)
