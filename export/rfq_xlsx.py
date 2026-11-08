"""The RFQ workbook sent to suppliers: Terms, Line items (with yellow price/remarks columns) and Questionnaire (yellow answer column)."""
from __future__ import annotations

import io

import xlsxwriter

from core.rfq_draft import ANSWER_LABEL, OVERVIEW_FIELDS, TERM_FIELDS, pass_rule_text

YELLOW = "#FFF2B3"


def build_rfq_xlsx(d: dict) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    head = wb.add_format({"bold": True, "bg_color": "#E8EEF4", "border": 1, "text_wrap": True, "valign": "top"})
    cell = wb.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    num = wb.add_format({"border": 1, "num_format": "#,##0", "valign": "top"})
    inp = wb.add_format({"border": 1, "bg_color": YELLOW, "valign": "top", "text_wrap": True})
    title = wb.add_format({"bold": True, "font_size": 14})
    note = wb.add_format({"italic": True, "font_color": "#555555"})

    ws = wb.add_worksheet("Terms")
    ws.write(0, 0, "Request for quotation", title)
    r = 2
    for k, label in OVERVIEW_FIELDS.items():
        ws.write(r, 0, label, head)
        ws.write(r, 1, str(d["overview"].get(k, "")), cell)
        r += 1
    r += 1
    for k, label in TERM_FIELDS.items():
        v = d["terms"].get(k)
        if k == "gst_pct" and v is not None:
            v = f"{v:g}% extra (quote ex-GST)"
        ws.write(r, 0, label, head)
        ws.write(r, 1, "" if v is None else str(v), cell)
        r += 1
    ws.write(r + 1, 0, "Quote in ₹ per piece, ex-GST. Yellow cells are for you to fill in.", note)
    ws.set_column(0, 0, 26)
    ws.set_column(1, 1, 60)

    ws = wb.add_worksheet("Line items")
    cols = ["#", "SKU", "Description", "L mm", "W mm", "H mm", "Ply", "BS kg/cm²", "Annual qty", "Typical order", "Your price ₹/pc (ex-GST)", "Remarks"]
    for c, h in enumerate(cols):
        ws.write(0, c, h, inp if c >= 10 else head)
    for i, ln in enumerate(d["lines"], 1):
        vals = [i, ln["sku"], ln["description"], ln["length_mm"], ln["width_mm"], ln["height_mm"], ln["ply"], ln["bs"], ln["annual_qty"], ln["typical_order_qty"]]
        for c, v in enumerate(vals):
            ws.write(i, c, v, num if c in (8, 9) else cell)
        ws.write_blank(i, 10, None, inp)
        ws.write_blank(i, 11, None, inp)
    ws.set_column(0, 0, 5)
    ws.set_column(1, 1, 14)
    ws.set_column(2, 2, 40)
    ws.set_column(3, 9, 11)
    ws.set_column(10, 11, 24)
    ws.freeze_panes(1, 3)

    ws = wb.add_worksheet("Questionnaire")
    cols = ["Code", "Question", "Answer type", "Pass rule", "Knock-out", "Document required", "Your answer"]
    for c, h in enumerate(cols):
        ws.write(0, c, h, inp if c == 6 else head)
    for i, q in enumerate(d["questions"], 1):
        vals = [q["q_code"], q["text"], ANSWER_LABEL.get(q["answer_type"], q["answer_type"]), pass_rule_text(q),
                "Yes" if q["is_knockout"] else "No", "Yes" if q["doc_required"] else "No"]
        for c, v in enumerate(vals):
            ws.write(i, c, v, cell)
        ws.write_blank(i, 6, None, inp)
    ws.set_column(0, 0, 18)
    ws.set_column(1, 1, 60)
    ws.set_column(2, 5, 16)
    ws.set_column(6, 6, 40)
    wb.close()
    return buf.getvalue()
