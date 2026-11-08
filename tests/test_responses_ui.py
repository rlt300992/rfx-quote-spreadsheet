"""Responses tab interactions: previews of every file kind, both re-extract dialogs, Review shortcut, no new Gemini calls on load."""
import pytest

from core.responses_view import resolve_path
from tests.conftest import run_app


def _btn(at, key):
    return [b for b in at.button if b.key == key][0]


def test_cards_render_for_all_vendors_without_gemini(real_conn, monkeypatch):
    import extract.gemini_client as g
    calls = []
    monkeypatch.setattr(g, "generate", lambda *a, **k: calls.append(1), raising=False)
    c, path = real_conn
    at = run_app(path)
    assert not at.exception and not calls
    keys = {b.key for b in at.button}
    for v in ("V1", "V2", "V3", "V4", "V5"):
        assert {f"rerun_{v}", f"pv_main_{v}", f"goto_review_{v}"} <= keys
    assert "Re-run extraction" not in [b.label for b in at.button]
    assert any(b.label == "↻ Extract all" for b in at.button)
    labels = [e.label for e in at.expander]
    assert any(l.startswith("Questionnaire (8) · 7 met · 1 not met") and "Qualified" in l for l in labels)     # V1
    assert any(l.startswith("Questionnaire (8)") and "Disqualified" in l for l in labels)                      # V5
    assert sum(l.startswith("Draft clarification email") for l in labels) == 5
    assert any("large, not MSME" in m.value for m in at.markdown)                                              # V3


@pytest.mark.parametrize("vendor,kind", [("V1", "excel"), ("V2", "pdf"), ("V3", "word"), ("V4", "photo"), ("V5", "email")])
def test_main_preview_opens_for_each_kind(real_conn, vendor, kind):
    c, path = real_conn
    at = run_app(path)
    _btn(at, f"pv_main_{vendor}").click().run()
    assert not at.exception
    from core.responses_view import file_kind
    f = c.execute("SELECT file_path, file_name FROM vendor_responses WHERE file_name LIKE ? AND content_role IN ('quote','mixed') LIMIT 1", (vendor + "_%",)).fetchone()
    assert file_kind(resolve_path(f[0], f[1])) in {"excel": "excel", "pdf": "pdf", "word": "word", "photo": "photo", "email": "email"}[kind]
    if kind == "excel":
        assert len(at.dataframe) >= 1
    if kind == "email":
        assert any("From:" in m.value for m in at.markdown)
    if kind == "word":
        assert any(m.value.startswith("1. ") for m in at.markdown)


def test_evidence_and_other_attachment_preview(real_conn):
    c, path = real_conn
    at = run_app(path)
    _btn(at, "pv_ev_V5_Q01_ISO9001").click().run()
    assert not at.exception
    assert any("V5_KKP_ISO9001.pdf" in m.value for m in at.markdown)
    # an unlinked attachment shows under "Other attachments" and previews
    c.execute("UPDATE questionnaire_answers SET evidence_doc_id=NULL WHERE evidence_doc_id=(SELECT doc_id FROM documents WHERE file_name='V1_BS_test_report.pdf')")
    c.commit()
    at = run_app(path)
    others = [b for b in at.button if b.key and b.key.startswith("pv_oth_V1")]
    assert len(others) == 1
    others[0].click().run()
    assert not at.exception


def test_v4_has_no_questionnaire_message(real_conn):
    c, path = real_conn
    at = run_app(path)
    msgs = [i.value for i in at.info if "sent only a rate card" in i.value]
    assert len(msgs) == 1 and "All 8 answers are missing, including the 3 knock-outs" in msgs[0] and "Pending" in msgs[0]


def test_reextract_vendor_dialog_cancel_and_confirm(real_conn, monkeypatch):
    c, path = real_conn
    seen = []
    monkeypatch.setattr("ui.responses.ingest_vendor", lambda conn, entry, force=False: seen.append((entry["vendor_code"], force)))
    at = run_app(path)
    _btn(at, "rerun_V2").click().run()
    assert not at.exception
    assert any("replaces V2's current extracted values" in m.value and "Other vendors are not affected" in m.value for m in at.markdown)
    _btn(at, "rx_V2_cancel").click().run()
    assert seen == []
    at = run_app(path)
    _btn(at, "rerun_V2").click().run()
    _btn(at, "rx_V2_go").click().run()
    assert seen == [("V2", True)]


def test_reextract_all_dialog(real_conn, monkeypatch):
    c, path = real_conn
    seen = []
    monkeypatch.setattr("ui.responses.ingest_all", lambda conn, force=False, **k: seen.append(force))
    at = run_app(path)
    _btn(at, "extract_all").click().run()
    assert any("Corrections and confirmations made in Review & Reconcile will be lost" in m.value for m in at.markdown)
    _btn(at, "rx_all_cancel").click().run()
    assert seen == []
    at = run_app(path)
    _btn(at, "extract_all").click().run()
    _btn(at, "rx_all_go").click().run()
    assert seen == [True]


def test_review_button_sets_vendor_filter_and_tab(real_conn):
    c, path = real_conn
    at = run_app(path)
    _btn(at, "goto_review_V1").click().run()
    assert not at.exception
    assert at.session_state["rv_vendor"] == "V1" and at.session_state["main_tab"] == "Review & Reconcile"
    assert at.selectbox(key="rv_vendor").value == "V1"
