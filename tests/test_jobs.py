"""Background re-extraction: status transitions saved to the DB, double-start guard, the card chip; clarification drafts list
every unanswered questionnaire item."""
import threading

from core import jobs
from core.db import connect
from extract.clarify import collect_issues
from tests.conftest import run_app

ENTRY = {"vendor_code": "V1", "files": ["a.xlsx"], "received_at": "2026-11-10T10:00:00"}


def _status(path, code="V1"):
    c = connect(path)
    r = c.execute("SELECT * FROM extraction_jobs WHERE vendor_code=?", (code,)).fetchone()
    c.close()
    return r


def test_status_is_extracting_before_the_job_runs_then_extracted(real_conn):
    c, path = real_conn
    gate, started = threading.Event(), threading.Event()

    def runner(conn, entry):
        started.set()
        gate.wait(10)
        return {"calls": {"api": 2, "cache": 0}}

    assert jobs.start(c, [ENTRY], runner=runner, db_file=path) == ["V1"]
    started.wait(10)
    assert _status(path)["status"] == "extracting" and jobs.is_running(c, "V1")      # saved to the DB while running
    assert jobs.start(c, [ENTRY], runner=runner, db_file=path) == []                  # double start is refused
    gate.set()
    for _ in range(100):
        if _status(path)["status"] != "extracting":
            break
        threading.Event().wait(0.05)
    r = _status(path)
    assert r["status"] == "extracted" and r["source"] == "fresh" and r["finished_at"]


def test_cache_only_run_is_labelled_from_cache_and_failure_is_recorded(real_conn):
    c, path = real_conn
    jobs.start(c, [ENTRY], runner=lambda conn, e: {"calls": {"api": 0, "cache": 3}}, db_file=path, wait=True)
    assert _status(path)["source"] == "cache"

    def boom(conn, e):
        raise RuntimeError("quota exhausted")

    jobs.start(c, [ENTRY], runner=boom, db_file=path, wait=True)
    r = _status(path)
    assert r["status"] == "failed" and "quota exhausted" in r["error"]
    assert not jobs.is_running(c, "V1")


def test_interrupted_job_is_marked_failed_at_restart(real_conn):
    c, path = real_conn
    c.execute("INSERT INTO extraction_jobs (vendor_code, status, started_at) VALUES ('V2','extracting','2026-11-08T10:00:00+00:00')")
    c.commit()
    jobs.recover_interrupted(c)
    assert _status(path, "V2")["status"] == "failed"


def test_card_shows_extracting_chip_and_disables_rerun(real_conn, monkeypatch):
    monkeypatch.setattr(jobs, "recover_interrupted", lambda conn: None)   # a fresh app process would otherwise fail the fake job
    from datetime import datetime, timezone
    c, path = real_conn
    c.execute("INSERT INTO extraction_jobs (vendor_code, status, started_at) VALUES ('V3','extracting',?)",
              (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    c.commit()
    at = run_app(path)
    assert not at.exception
    assert any("Extracting…" in m.value for m in at.markdown)
    assert [b for b in at.button if b.key == "rerun_V3"][0].disabled
    assert not [b for b in at.button if b.key == "rerun_V1"][0].disabled
    assert [b for b in at.button if b.label == "↻ Extract all"][0].disabled


def test_confirm_in_dialog_saves_extracting_without_running_inline(real_conn, monkeypatch):
    c, path = real_conn
    seen = []
    monkeypatch.setattr(jobs, "start", lambda conn, entries, **k: seen.append([e["vendor_code"] for e in entries]) or [])
    at = run_app(path)
    [b for b in at.button if b.key == "rerun_V2"][0].click().run()
    assert any("Re-extracts" in m.value and "quote" in m.value for m in at.markdown)       # the dialog says which files
    [b for b in at.button if b.key == "rx_V2_go"][0].click().run()
    assert seen == [["V2"]] and not at.exception


def test_status_tooltip_says_when_and_from_where(real_conn):
    from core.responses_view import last_extracted
    c, path = real_conn
    c.execute("INSERT INTO extraction_jobs (vendor_code, status, started_at, finished_at, source) VALUES ('V1','extracted','2026-11-08T07:00:00+00:00','2026-11-08T07:01:00+00:00','fresh')")
    c.commit()
    assert last_extracted(c, "V1", None).endswith("(fresh)")
    f = c.execute("SELECT file_path FROM vendor_responses WHERE vendor_id=2 AND content_role IN ('quote','mixed') LIMIT 1").fetchone()[0]
    assert last_extracted(c, "V2", f).endswith("(from cache)")


def test_clarification_issues_list_every_unanswered_questionnaire_item(real_conn):
    c, path = real_conn
    for code, in c.execute("SELECT vendor_code FROM vendors"):
        unanswered = [r[0] for r in c.execute(
            """SELECT q.q_code FROM rfx_questions q JOIN vendors v ON v.vendor_code=?
               LEFT JOIN questionnaire_answers a ON a.question_id=q.question_id AND a.vendor_id=v.vendor_id AND a.is_current=1
               WHERE a.answer_id IS NULL ORDER BY q.question_id""", (code,))]
        text = " ".join(collect_issues(c, code))
        if len(unanswered) == 8:
            assert "not received your response to our quality questionnaire" in text
        else:
            assert all(q in text for q in unanswered), (code, unanswered)
    v5 = " ".join(collect_issues(c, "V5"))
    for q in ("Q02", "Q05", "Q06", "Q07", "Q08"):
        assert q in v5
