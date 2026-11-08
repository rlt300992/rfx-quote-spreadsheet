"""P5: Create RFQ co-pilot. Edit operations, saving against existing replies, the send stub, and the tab."""
import json

import pandas as pd
import pytest

from copilot import copilot as C
from core.db import connect, init_db
from core.seed import seed
from tests.conftest import run_app

Op = C.Op


@pytest.fixture
def seeded():
    """A fresh RFQ with no vendor replies at all."""
    c = connect(":memory:")
    init_db(c)
    seed(c)
    return c


# ------------------------------------------------------------------ operations: validated, never trusted
def test_ops_apply_terms_lines_and_questions(seeded):
    d = C.load_draft(seeded)
    new, notes = C.apply_ops(d, [
        Op(op="set_term", target="payment_days", value="45"), Op(op="set_term", target="response_due_date", value="2026-11-23"),
        Op(op="set_line", target="3", field="annual_qty", value="160000"), Op(op="drop_line", target="26"),
        Op(op="set_question_knockout", target="Q07", value="true"), Op(op="drop_question", target="Q08_COMPLAINTS")])
    assert all(n.startswith("Applied") for n in notes), notes
    assert new.terms["payment_days"] == 45 and new.terms["response_due_date"] == "2026-11-23"
    assert int(new.lines.loc[new.lines.line_no == 3, "annual_qty"].iloc[0]) == 160000
    assert not new.lines.loc[new.lines.line_no == 26, "include"].iloc[0]
    assert new.questions.loc[new.questions.q_code == "Q07_FSC", "is_knockout"].iloc[0]       # "Q07" prefix resolves
    assert not new.questions.loc[new.questions.q_code == "Q08_COMPLAINTS", "include"].iloc[0]
    assert d.terms["payment_days"] == 60 and d.lines.loc[d.lines.line_no == 26, "include"].iloc[0]   # the original is untouched


@pytest.mark.parametrize("op", [
    Op(op="set_term", target="payment_days", value="-5"), Op(op="set_term", target="payment_days", value="abc"),
    Op(op="set_term", target="gst_pct", value="40"), Op(op="set_term", target="response_due_date", value="next week"),
    Op(op="set_term", target="colour", value="red"), Op(op="drop_line", target="99"), Op(op="set_line", target="3", field="length_mm", value="500"),
    Op(op="set_line", target="3", field="annual_qty", value="-1"), Op(op="drop_question", target="Q99"), Op(op="set_question_text", target="Q01", value=" ")])
def test_ops_that_are_invalid_are_refused_with_a_reason_and_change_nothing(seeded, op):
    d = C.load_draft(seeded)
    new, notes = C.apply_ops(d, [op])
    assert notes[0].startswith("Not applied") and not C.diff(new, d)


def test_model_cannot_invent_a_line_or_edit_dimensions(seeded):
    d = C.load_draft(seeded)
    new, notes = C.apply_ops(d, [Op(op="set_line", target="31", field="description", value="Gold box"), Op(op="set_line", target="1", field="width_mm", value="999")])
    assert len(new.lines) == 30 and all(n.startswith("Not applied") for n in notes)


def test_validation_warnings_do_not_block(seeded):
    d = C.load_draft(seeded)
    assert C.validate_draft(d) == []
    d.terms["gst_pct"] = 12.0
    d.terms["award_reference_date"] = "2026-01-01"
    d.questions["is_knockout"] = False
    w = " ".join(C.validate_draft(d))
    assert "GST is 12.0%" in w and "before the response due date" in w and "No knock-out question" in w


# ------------------------------------------------------------------ saving on a fresh RFQ
def test_confirm_marks_lines_confirmed_and_can_delete_unreferenced_lines(seeded):
    d = C.load_draft(seeded)
    d, _ = C.apply_ops(d, [Op(op="drop_line", target="26"), Op(op="drop_question", target="Q08"), Op(op="set_term", target="payment_days", value="45")])
    seeded.execute("UPDATE line_items SET buyer_confirmed=0")
    res = C.save_draft(seeded, d)
    assert not res.blocked and not res.recomputed
    assert seeded.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] == 29
    assert seeded.execute("SELECT COUNT(*) FROM line_items WHERE buyer_confirmed=1").fetchone()[0] == 29
    assert seeded.execute("SELECT COUNT(*) FROM rfx_questions").fetchone()[0] == 7
    assert seeded.execute("SELECT std_payment_terms_days FROM rfqs").fetchone()[0] == 45
    a = seeded.execute("SELECT * FROM audit_log WHERE action='rfq_confirm'").fetchone()
    assert "payment_days: 60 -> 45" in a["evidence_json"] and a["actor"] == "buyer"


# ------------------------------------------------------------------ saving with 5 vendors' replies already extracted
def test_changing_payment_days_recomputes_the_existing_replies(real_conn):
    c, _ = real_conn
    before = c.execute("SELECT evaluated_inr_pc FROM quotes q JOIN vendors v USING (vendor_id) JOIN line_items l USING (line_id) WHERE v.vendor_code='V1' AND l.line_no=2 AND q.is_current=1").fetchone()[0]
    d, _ = C.apply_ops(C.load_draft(c), [Op(op="set_term", target="payment_days", value="45")])
    res = C.save_draft(c, d)
    assert res.recomputed
    after = c.execute("SELECT evaluated_inr_pc, std_terms_days_used FROM quotes q JOIN vendors v USING (vendor_id) JOIN line_items l USING (line_id) WHERE v.vendor_code='V1' AND l.line_no=2 AND q.is_current=1").fetchone()
    assert after["std_terms_days_used"] == 45 and after["evaluated_inr_pc"] != before     # V1's 60 days now earns a credit against a 45-day standard


def test_a_line_vendors_have_quoted_cannot_be_dropped(real_conn):
    c, _ = real_conn
    d, _ = C.apply_ops(C.load_draft(c), [Op(op="drop_line", target="26"), Op(op="drop_question", target="Q01")])
    res = C.save_draft(c, d)
    assert len(res.blocked) == 2 and "line 26" in res.blocked[0] and "Q01_ISO9001" in res.blocked[1]
    assert c.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] == 30                 # nothing orphaned
    assert c.execute("SELECT COUNT(*) FROM rfx_questions").fetchone()[0] == 8
    assert c.execute("SELECT COUNT(*) FROM quotes WHERE line_id IS NOT NULL AND line_id NOT IN (SELECT line_id FROM line_items)").fetchone()[0] == 0


def test_making_a_question_a_knockout_changes_qualification_end_to_end(real_conn):
    c, _ = real_conn
    assert c.execute("SELECT qual_status FROM v_vendor_qualification WHERE vendor_code='V1'").fetchone()[0] == "qualified"
    d, _ = C.apply_ops(C.load_draft(c), [Op(op="set_question_knockout", target="Q07_FSC", value="true")])   # V1 holds no FSC certificate
    C.save_draft(c, d)
    assert c.execute("SELECT qual_status FROM v_vendor_qualification WHERE vendor_code='V1'").fetchone()[0] == "disqualified"
    assert c.execute("SELECT qual_status FROM v_vendor_qualification WHERE vendor_code='V3'").fetchone()[0] == "qualified"   # V3 holds FSC


def test_confirm_with_no_edits_is_harmless(real_conn):
    c, _ = real_conn
    before = [tuple(r) for r in c.execute("SELECT quote_id, evaluated_inr_pc, review_state FROM quotes ORDER BY quote_id")]
    res = C.save_draft(c, C.load_draft(c))
    assert res.changes == [] and not res.recomputed
    assert before == [tuple(r) for r in c.execute("SELECT quote_id, evaluated_inr_pc, review_state FROM quotes ORDER BY quote_id")]


# ------------------------------------------------------------------ send (stub)
def test_send_writes_the_body_marks_issued_and_logs(seeded):
    seeded.execute("UPDATE rfqs SET status='draft', issued_at=NULL")
    out = C.send_rfq(seeded)
    assert len(out["vendors"]) == 5 and "Sent" not in out["body"]
    body = seeded.execute("SELECT rfq_body, status, issued_at FROM rfqs").fetchone()
    assert body["status"] == "issued" and body["issued_at"] and body["rfq_body"] == out["body"]
    for needle in ("Subject: RFQ", "FOR", "60 days", "18%", "2026-11-16", "(30 lines)", " 1. Shampoo 180ml", "Q01_ISO9001", "[knock-out]"):
        assert needle in body["rfq_body"], needle
    a = seeded.execute("SELECT * FROM audit_log WHERE action='rfq_send'").fetchone()
    assert json.loads(a["evidence_json"])["stub"] is True and len(json.loads(a["evidence_json"])["vendors"]) == 5
    assert C.sent_info(seeded)["ts"] == a["ts"]


def test_send_does_not_downgrade_an_evaluating_rfq(real_conn):
    c, _ = real_conn
    c.execute("UPDATE rfqs SET status='evaluating'")
    C.send_rfq(c)
    assert c.execute("SELECT status FROM rfqs").fetchone()[0] == "evaluating"


EDIT = lambda at: at.session_state.__setitem__("rfq_edit", True)   # the old editor now sits behind 'Edit RFQ' on the demo RFQ


# ------------------------------------------------------------------ the tab in the real app (co-pilot stubbed)
def _fake_copilot(monkeypatch, ops, reply="Done."):
    def fake(d, message, history=None):
        new, notes = C.apply_ops(d, ops)
        return C.CopilotReply(reply=reply, ops=ops), new, notes

    monkeypatch.setattr(C, "ask_copilot", fake)


def test_chat_edits_the_tables_and_warns_because_replies_exist(real_conn, monkeypatch):
    c, path = real_conn
    _fake_copilot(monkeypatch, [Op(op="set_term", target="payment_days", value="45"), Op(op="drop_line", target="26")], "Payment is now 45 days and the promo box is dropped.")
    at = run_app(path, setup=EDIT)
    assert not at.exception
    at.chat_input(key="rfq_chat_input").set_value("make payment 45 days and drop the promo box").run()
    assert not at.exception
    assert [w for w in at.number_input if w.key.startswith("rfq_pay_")][0].value == 45
    assert any("Lines (29 of 30 included)" in s.value for s in at.subheader)
    assert any("5 vendors have replied" in w.value and "will not change what the vendors quoted" in w.value for w in at.warning)
    notes = " ".join(x.value for x in at.caption)
    assert "Applied: payment_days = 45" in notes and "promo" in notes.lower()
    assert c.execute("SELECT std_payment_terms_days FROM rfqs").fetchone()[0] == 60                  # nothing saved by chatting
    assert [b for b in at.button if b.key == "rfq_send"][0].disabled is True                          # must confirm first


def test_confirm_then_send_through_the_ui(real_conn, monkeypatch):
    c, path = real_conn
    _fake_copilot(monkeypatch, [Op(op="set_term", target="payment_days", value="45"), Op(op="drop_line", target="26")])
    at = run_app(path, setup=EDIT)
    at.chat_input(key="rfq_chat_input").set_value("go").run()
    [b for b in at.button if b.key == "rfq_confirm"][0].click().run()
    assert not at.exception
    assert c.execute("SELECT std_payment_terms_days FROM rfqs").fetchone()[0] == 45
    assert any("RFQ confirmed" in s.value for s in at.success)
    assert any("line 26" in w.value and "cannot be removed" in w.value for w in at.warning)          # the blocked drop is reported, not hidden
    assert c.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] == 30
    send = [b for b in at.button if b.key == "rfq_send"][0]
    assert send.disabled is False
    send.click().run()
    assert any("Sent (stub)" in s.value and "No email was actually sent" in s.value for s in at.success)
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='rfq_send'").fetchone()[0] == 1
    assert c.execute("SELECT rfq_body FROM rfqs").fetchone()[0].startswith("Subject: RFQ")


def test_existing_responses_and_other_tabs_still_work_after_an_rfq_edit(real_conn, monkeypatch):
    c, path = real_conn
    _fake_copilot(monkeypatch, [Op(op="set_term", target="payment_days", value="45")])
    at = run_app(path, setup=EDIT)
    at.chat_input(key="rfq_chat_input").set_value("45 days").run()
    [b for b in at.button if b.key == "rfq_confirm"][0].click().run()
    assert not at.exception
    grid = [d.value for d in at.dataframe if d.value.shape == (30, 5)]
    assert grid and grid[0].loc[5, "V3"].endswith("⚠")                                     # Bid Analysis still renders real extracted prices
    assert c.execute("SELECT COUNT(*) FROM v_comparison_grid").fetchone()[0] == 150


def test_chat_failure_is_shown_not_swallowed(real_conn, monkeypatch):
    c, path = real_conn
    monkeypatch.setattr(C, "ask_copilot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota")))
    at = run_app(path, setup=EDIT)
    at.chat_input(key="rfq_chat_input").set_value("hello").run()
    assert not at.exception and any("Sorry, the co-pilot failed: quota" in m.value for m in at.markdown)
