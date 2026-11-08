"""Create RFQ rebuild: draft isolation, SKU lookup, review checks, the workbook, publish stub, load-demo confirm, and the tab flows."""
import hashlib
import io
import json
from datetime import date

import openpyxl
import pytest

from copilot import draft_copilot as DC
from core import rfq_draft as R
from export.rfq_xlsx import build_rfq_xlsx
from tests.conftest import run_app

TODAY = date(2026, 11, 8)


def _demo_fingerprint(c):
    out = {}
    for t, order in (("rfqs", "rfq_id"), ("line_items", "line_id"), ("rfx_questions", "question_id"), ("quotes", "quote_id"), ("vendors", "vendor_id")):
        rows = c.execute(f"SELECT * FROM {t} ORDER BY {order}").fetchall()
        out[t] = hashlib.sha256(json.dumps([tuple(r) for r in rows], default=str).encode()).hexdigest()
    return out


def _full(c) -> dict:
    d = R.from_demo(c)
    d["terms"]["response_due"] = "2026-11-16"
    return d


# ------------------------------------------------------------------ SKU lookup
def test_sku_lookup_known_unknown_and_closest(real_conn):
    c, _ = real_conn
    assert R.lookup_sku(c, " cb-3p-sh-01 ")["status"] == "found"
    r = R.lookup_sku(c, "CB-3P-JM-01")
    assert r["status"] == "unknown" and r["closest"] and r["closest"].startswith("CB-3P-")
    assert R.lookup_sku(c, "ZZZ")["closest"] is None


def test_unknown_sku_is_not_added_and_is_reported(real_conn):
    c, _ = real_conn
    d = R.empty_draft()
    notes = R.add_skus(c, d, ["CB-3P-SH-01", "CB-3P-SH-02", "CB-3P-JM-01", "CB-3P-SH-01"])
    assert [ln["sku"] for ln in d["lines"]] == ["CB-3P-SH-01", "CB-3P-SH-02"]
    assert d["unknown"][0]["code"] == "CB-3P-JM-01"
    assert any("not in the catalog" in n for n in notes) and any("already" in n for n in notes)
    assert R.section_status(d, "lines") == ("Needs input", "orange")


def test_same_as_last_year_adds_catalog_lines(real_conn):
    c, _ = real_conn
    d = R.empty_draft()
    R.add_last_year(c, d)
    assert len(d["lines"]) == len(R.last_year_skus(c)) == 30


# ------------------------------------------------------------------ review checks: blocking vs warning
def test_review_blocking_and_warning(real_conn):
    c, _ = real_conn
    d = _full(c)
    assert R.blocking_and_warnings(d, TODAY) == ([], [])
    d["terms"]["response_due"] = "2026-11-05"
    block, warn = R.blocking_and_warnings(d, TODAY)
    assert [s for s, _ in block] == ["terms"] and "past" in block[0][1] and not warn
    assert R.section_status(d, "terms", TODAY) == ("1 issue", "red")
    d2 = _full(c)
    for q in d2["questions"]:
        q["is_knockout"] = False
    block, warn = R.blocking_and_warnings(d2, TODAY)
    assert not block and [s for s, _ in warn] == ["questions"]                 # a warning does not block
    d3 = _full(c)
    d3["lines"], d3["suppliers"], d3["overview"]["title"] = [], [], ""
    assert {s for s, _ in R.blocking_and_warnings(d3, TODAY)[0]} == {"lines", "suppliers", "overview"}


def test_review_is_locked_until_sections_1_to_5_are_filled(real_conn):
    c, _ = real_conn
    d = R.empty_draft()
    assert not R.sections_filled(d, TODAY) and R.section_status(d, "review", TODAY) == ("Not started", "gray")
    assert R.sections_filled(_full(c), TODAY)
    d = _full(c)
    d["suppliers"] = []
    assert not R.sections_filled(d, TODAY)


# ------------------------------------------------------------------ the workbook
def test_xlsx_has_three_sheets_and_yellow_input_columns(real_conn):
    c, _ = real_conn
    wb = openpyxl.load_workbook(io.BytesIO(build_rfq_xlsx(R.from_demo(c))))
    assert wb.sheetnames == ["Terms", "Line items", "Questionnaire"]
    li, qs = wb["Line items"], wb["Questionnaire"]
    assert li.max_row == 31 and li["K1"].value.startswith("Your price") and li["L1"].value == "Remarks"
    assert li["K2"].fill.fgColor.rgb.endswith("FFF2B3") and li["L2"].fill.fgColor.rgb.endswith("FFF2B3")
    assert qs.max_row == 9 and qs["G1"].value == "Your answer" and qs["G2"].fill.fgColor.rgb.endswith("FFF2B3")
    assert qs["E2"].value == "Yes" and qs["F2"].value == "Yes"                   # Q01: knock-out, document required


# ------------------------------------------------------------------ co-pilot ops are validated by Python
def test_copilot_ops_are_validated(real_conn):
    c, _ = real_conn
    d = R.empty_draft()
    ops = [DC.DraftOp(op="set_overview", target="title", value="Corrugated boxes FY27"),
           DC.DraftOp(op="add_skus", values=["CB-3P-SH-01", "CB-3P-XX-99"]),
           DC.DraftOp(op="load_questionnaire_template"),
           DC.DraftOp(op="set_knockout", target="Q05", value="true"),
           DC.DraftOp(op="set_term", target="payment_days", value="9999"),
           DC.DraftOp(op="set_term", target="response_due", value="not-a-date"),
           DC.DraftOp(op="select_suppliers", values=["V1", "V9"])]
    new, ok, no = DC.apply_ops(c, d, ops)
    assert new["overview"]["title"] == "Corrugated boxes FY27" and "overview.title" in new["ai_fields"]
    assert [ln["sku"] for ln in new["lines"]] == ["CB-3P-SH-01"] and new["unknown"][0]["code"] == "CB-3P-XX-99"
    assert len(new["questions"]) == 8 and [q for q in new["questions"] if q["q_code"].startswith("Q05")][0]["is_knockout"]
    assert new["terms"]["payment_days"] is None and new["terms"]["response_due"] == ""       # refused, nothing half-applied
    assert new["suppliers"] == ["V1"]
    assert sum("Could not set term" in n for n in no) == 2 and any("V9" in n for n in no) and any("XX-99" in n for n in no)
    assert d == R.empty_draft()                                                              # the input draft is untouched


# ------------------------------------------------------------------ isolation + publish stub
def test_draft_never_touches_the_demo_rfq_and_publish_is_a_stub(real_conn):
    c, _ = real_conn
    before = _demo_fingerprint(c)
    d = _full(c)
    R.save(c, d)
    R.publish(c, d)
    assert _demo_fingerprint(c) == before
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='rfq_send'").fetchone()[0] == 0     # the demo's own send log is untouched
    a = c.execute("SELECT * FROM audit_log WHERE action='rfq_draft_publish'").fetchall()
    assert len(a) == 1 and len(json.loads(a[0]["evidence_json"])["vendors"]) == 5
    d2, _ = R.load(c)
    assert d2["status"] == "published" and len(d2["sent_to"]) == 5


def test_publish_refuses_a_draft_with_blocking_issues(real_conn):
    c, _ = real_conn
    d = _full(c)
    d["terms"]["response_due"] = "2020-01-01"
    with pytest.raises(ValueError, match="past"):
        R.publish(c, d)
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='rfq_draft_publish'").fetchone()[0] == 0


# ------------------------------------------------------------------ the tab
def _btn(at, key):
    return [b for b in at.button if b.key == key][0]


def test_tab_opens_on_the_read_only_demo_rfq(real_conn):
    c, path = real_conn
    at = run_app(path)
    assert not at.exception
    assert any("Published 7 Nov 2026, 11:00 · sent to 5 suppliers · read-only" in x.value for x in at.caption)
    assert any(i.value.startswith("Demo RFQ. The 5 vendor replies") for i in at.info)
    labels = [e.label for e in at.expander if "Published" in e.label]
    assert [l.split("**")[1] for l in labels][:6] == ["Overview", "Line items", "Questionnaire", "Terms & timeline", "Suppliers", "Sent email"]
    assert any("30 lines · 12 × 3-ply, 14 × 5-ply, 4 × 7-ply" in l for l in labels)
    assert any("8 questions · 3 knock-outs · 3 need a document" in l for l in labels)
    assert {"rfq_edit_btn", "rfq_start_new"} <= {b.key for b in at.button}
    assert at.session_state["rfq_mode"] == "Demo RFQ"


def test_event_header_and_banner_always_show_the_demo_rfq(real_conn):
    c, path = real_conn
    at = run_app(path)
    _btn(at, "rfq_start_new").click().run()
    assert at.session_state["rfq_mode"] == "New draft"
    assert any("Evaluating" in x.value for x in at.caption)


def test_start_new_draft_is_empty_with_six_sections_and_pills(real_conn):
    c, path = real_conn
    at = run_app(path)
    _btn(at, "rfq_start_new").click().run()
    assert not at.exception
    labels = [e.label for e in at.expander if "**" in e.label]
    assert [l.split("**")[1] for l in labels][:6] == ["1 · Overview", "2 · Line items", "3 · Questionnaire", "4 · Terms & timeline", "5 · Suppliers", "6 · Review & publish"]
    assert all("Not started" in l for l in labels[:6])
    assert any("What are we sourcing?" in m.value for m in at.markdown)
    assert any(x.value == "New draft · not saved yet" for x in at.caption)
    assert c.execute("SELECT COUNT(*) FROM rfq_drafts").fetchone()[0] == 0                # an empty draft is not stored


def test_chat_message_calls_the_model_once_and_autosaves(real_conn, monkeypatch):
    c, path = real_conn
    calls = []

    def fake(conn, draft, message, history=None):
        calls.append(message)
        new, ok, no = DC.apply_ops(conn, draft, [DC.DraftOp(op="set_overview", target="title", value="Boxes FY27"),
                                                 DC.DraftOp(op="add_skus", values=["CB-3P-SH-01", "CB-3P-JM-01"])])
        return "Filled the title and added the SKUs.", new, ok, no, ["Add last year's lines", "Use the standard questionnaire"]

    monkeypatch.setattr(DC, "ask", fake)
    at = run_app(path)
    _btn(at, "rfq_start_new").click().run()
    assert calls == []                                                                     # no model call until the buyer sends a message
    at.chat_input(key="draft_chat_input").set_value("boxes for FY27: CB-3P-SH-01 CB-3P-JM-01").run()
    assert not at.exception and len(calls) == 1
    assert any("✗" in x.value and "CB-3P-JM-01" in x.value for x in at.caption)           # what it couldn't do is shown
    assert any("auto-saved" in x.value for x in at.caption)
    d, _ = R.load(c)
    assert d["overview"]["title"] == "Boxes FY27" and len(d["lines"]) == 1 and d["unknown"][0]["code"] == "CB-3P-JM-01"


def test_direct_pills_do_not_call_the_model(real_conn, monkeypatch):
    c, path = real_conn
    monkeypatch.setattr(DC, "ask", lambda *a, **k: pytest.fail("the model must not be called"))
    at = run_app(path)
    _btn(at, "rfq_start_new").click().run()
    for pill in ("Copy last year's corrugated RFQ",):
        at.session_state["rfq_pills_0"] = pill
        at.run()
    d, _ = R.load(c)
    assert len(d["lines"]) == 30


def test_load_demo_asks_before_replacing_a_draft(real_conn):
    c, path = real_conn
    d = R.empty_draft()
    d["overview"]["title"] = "My own draft"
    R.save(c, d)
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rfq_mode", "New draft"))
    _btn(at, "rfq_load_demo").click().run()
    assert any("Replace your current draft?" in str(x) or "This fills the draft with the demo RFQ" in x.value for x in at.markdown)
    _btn(at, "loaddemo_cancel").click().run()
    assert R.load(c)[0]["overview"]["title"] == "My own draft"
    _btn(at, "rfq_load_demo").click().run()
    _btn(at, "loaddemo_go").click().run()
    assert not at.exception
    d, _ = R.load(c)
    assert len(d["lines"]) == 30 and len(d["questions"]) == 8 and len(d["suppliers"]) == 5


def test_load_demo_on_an_empty_draft_does_not_ask(real_conn):
    c, path = real_conn
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rfq_mode", "New draft"))
    _btn(at, "rfq_load_demo").click().run()
    assert "loaddemo_go" not in {b.key for b in at.button}
    assert len(R.load(c)[0]["lines"]) == 30


def test_publish_flow_through_the_ui_leaves_the_demo_alone(real_conn):
    c, path = real_conn
    R.save(c, _full(c))
    before = _demo_fingerprint(c)
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rfq_mode", "New draft"))
    assert not at.exception and not _btn(at, "rfq_preview_email").disabled
    _btn(at, "rfq_publish").click().run()
    assert any(b.key == "rfq_publish_go" for b in at.button) and any(b.key == "pv_rfq_xlsx" for b in at.button)
    _btn(at, "rfq_publish_go").click().run()
    assert not at.exception
    assert any("Sent to 5 suppliers (stub)" in s.value for s in at.success)
    assert R.load(c)[0]["status"] == "published"
    assert _demo_fingerprint(c) == before


def test_blocked_draft_disables_publish(real_conn):
    c, path = real_conn
    d = _full(c)
    d["terms"]["response_due"] = "2026-11-05"
    R.save(c, d)
    at = run_app(path, setup=lambda a: a.session_state.__setitem__("rfq_mode", "New draft"))
    assert _btn(at, "rfq_publish").disabled and _btn(at, "rfq_preview_email").disabled
    assert any("Fix 1 issue to publish" in x.value for x in at.caption)


def test_edit_rfq_opens_the_existing_editor_with_the_safeguard_note(real_conn):
    c, path = real_conn
    at = run_app(path)
    _btn(at, "rfq_edit_btn").click().run()
    assert not at.exception
    assert any("cannot be removed" in w.value and "line" in w.value for w in at.warning)
    assert any(b.key == "rfq_confirm" for b in at.button)
