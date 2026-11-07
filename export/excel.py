"""Excel export: Award, Scenario, Audit trail and Assumptions sheets (xlsxwriter, returned as bytes)."""
from __future__ import annotations

import io
import json
import sqlite3
from typing import Any

import pandas as pd
import xlsxwriter

from agent.tools import award_scenario
from core.config import load_config
from core.flags import FLAG_HELP, explain_reason
from extract.extract import prompt_version


def _award_frame(conn: sqlite3.Connection, proposal: dict | None) -> tuple[pd.DataFrame, str]:
    """The committed award if there is one, else the proposal, else the current best scenario (labelled as such)."""
    g = pd.read_sql_query("SELECT * FROM v_comparison_grid", conn)
    committed = conn.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0]
    li = pd.read_sql_query("SELECT line_no, awarded_quote_id, l1_quote_id, l1_eligible_quote_id, bypass_reason_code, award_basis, awarded_at FROM line_items", conn)
    if committed:
        rows = []
        for _, r in li[li["awarded_quote_id"].notna()].iterrows():
            q = g[g["quote_id"] == int(r["awarded_quote_id"])].iloc[0]
            l1 = g[g["quote_id"] == r["l1_quote_id"]]
            l1e = g[g["quote_id"] == r["l1_eligible_quote_id"]]
            rows.append({"Line": int(q["line_no"]), "SKU": q["catalog_sku"], "Description": q["description"], "Annual qty": int(q["annual_qty"]),
                         "Awarded vendor": q["vendor_code"], "Landed INR/pc": q["landed_inr_pc"], "Evaluated INR/pc": q["evaluated_inr_pc"],
                         "Annual value INR": q["evaluated_inr_pc"] * q["annual_qty"],
                         "L1 (cheapest quote)": l1.iloc[0]["vendor_code"] if len(l1) else None,
                         "L1 eligible": l1e.iloc[0]["vendor_code"] if len(l1e) else None,
                         "Bypass reason code": r["bypass_reason_code"], "Price not firm": "YES" if q["price_not_firm"] else "",
                         "Freight imputed": "YES" if q["freight_imputed"] else "", "Review state": q["review_state"], "Awarded at": r["awarded_at"]})
        return pd.DataFrame(rows).sort_values("Line"), "COMMITTED AWARD"
    if proposal:
        s = pd.DataFrame(proposal["rows"])
        label = "PROPOSED AWARD (not committed)"
    else:
        s = award_scenario(conn)
        label = "CURRENT BEST SCENARIO (not committed)"
    out = pd.DataFrame({"Line": s["line_no"], "SKU": s["catalog_sku"], "Description": s["description"], "Annual qty": s["annual_qty"],
                        "Awarded vendor": s["vendor_code"], "Landed INR/pc": s["landed_inr_pc"], "Evaluated INR/pc": s["price_inr_pc"],
                        "Annual value INR": s["annual_value_inr"], "Backup (L2)": s["backup_vendor"], "Backup price INR/pc": s["backup_price_inr_pc"],
                        "Price not firm": s["price_not_firm"].map({True: "YES", False: ""}), "Freight imputed": (s["freight_imputed"] > 0).map({True: "YES", False: ""}),
                        "Review state": s["review_state"]})
    return out, label


def build_workbook(conn: sqlite3.Connection, proposal: dict | None = None) -> bytes:
    cfg = load_config()
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    head = wb.add_format({"bold": True, "bg_color": "#1f3a5f", "font_color": "white", "border": 1, "text_wrap": True, "valign": "top"})
    money = wb.add_format({"num_format": "#,##0.00"})
    money0 = wb.add_format({"num_format": "#,##0"})
    bold = wb.add_format({"bold": True})
    warn = wb.add_format({"bold": True, "font_color": "#b71c1c"})
    wrap = wb.add_format({"text_wrap": True, "valign": "top"})

    def write_df(ws, df: pd.DataFrame, row0: int = 0, money_cols: tuple[str, ...] = (), money0_cols: tuple[str, ...] = (), widths: dict | None = None) -> None:
        for j, c in enumerate(df.columns):
            ws.write(row0, j, c, head)
            ws.set_column(j, j, (widths or {}).get(c, max(12, min(40, len(str(c)) + 4))))
        for i, (_, r) in enumerate(df.iterrows(), start=row0 + 1):
            for j, c in enumerate(df.columns):
                v = r[c]
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    continue
                fmt = money if c in money_cols else money0 if c in money0_cols else None
                if hasattr(v, "item"):
                    v = v.item()
                ws.write(i, j, v, fmt) if fmt else ws.write(i, j, v)
        ws.freeze_panes(row0 + 1, 0)

    # ---- Award
    award, label = _award_frame(conn, proposal)
    ws = wb.add_worksheet("Award")
    ws.write(0, 0, label, warn if "not committed" in label else bold)
    ws.write(1, 0, "Prices are INR per piece, GST excluded. Evaluated = landed + one-time charges + payment-terms loading (ranking price).")
    write_df(ws, award, 3, ("Landed INR/pc", "Evaluated INR/pc", "Backup price INR/pc"), ("Annual value INR", "Annual qty"), {"Description": 38})
    n = len(award)
    ws.write(4 + n, 0, "Total", bold)
    col = list(award.columns).index("Annual value INR")
    ws.write_formula(4 + n, col, f"=SUM({xlsxwriter.utility.xl_rowcol_to_cell(4, col)}:{xlsxwriter.utility.xl_rowcol_to_cell(3 + n, col)})", wb.add_format({"bold": True, "num_format": "#,##0"}),
                     float(award["Annual value INR"].sum()))
    notes = []
    if "Price not firm" in award and (award["Price not firm"] == "YES").any():
        notes.append(f"{int((award['Price not firm'] == 'YES').sum())} awarded lines rest on NOT-FIRM (kraft-linked) prices.")
    if "Freight imputed" in award and (award["Freight imputed"] == "YES").any():
        notes.append(f"{int((award['Freight imputed'] == 'YES').sum())} awarded lines use imputed freight.")
    n_open = conn.execute("SELECT COUNT(*) FROM v_review_queue WHERE status='must_confirm' AND could_change_winner=1").fetchone()[0]
    notes.append(f"{n_open} must-confirm values were still open at export time and could change the award.")
    for k, t in enumerate(notes):
        ws.write(6 + n + k, 0, t, warn)

    # ---- Scenario: every candidate with its exclusion reason
    g = pd.read_sql_query("SELECT * FROM v_comparison_grid ORDER BY line_no, vendor_code", conn)
    sc = pd.DataFrame({"Line": g["line_no"], "Description": g["description"], "Vendor": g["vendor_code"], "Vendor name": g["vendor_name"],
                       "Qualification": g["qual_status"], "Quote status": g["quote_status"], "Base INR/pc": g["base_inr_pc"],
                       "Freight INR/pc": g["freight_inr_pc"], "Landed INR/pc": g["landed_inr_pc"], "Evaluated INR/pc": g["evaluated_inr_pc"],
                       "Rank (awardable)": g["rank_awardable"], "Can win": g["is_awardable"].map({1: "YES", 0: "no"}),
                       "Why it can't win": g["exclusion_reason"].map(lambda x: explain_reason(x) if x else ""),
                       "Price not firm": g["price_not_firm"].map({1: "YES", 0: ""}),
                       "Flags": g["flags_json"].map(lambda x: ", ".join(json.loads(x)) if isinstance(x, str) else ""),
                       "Last year landed INR/pc": g["ly_landed_inr_pc"]})
    ws2 = wb.add_worksheet("Scenario")
    write_df(ws2, sc, 0, ("Base INR/pc", "Freight INR/pc", "Landed INR/pc", "Evaluated INR/pc", "Last year landed INR/pc"), (),
             {"Description": 34, "Why it can't win": 60, "Flags": 40, "Vendor name": 28})
    ws2.autofilter(0, 0, len(sc), len(sc.columns) - 1)

    # ---- Audit trail
    au = pd.read_sql_query("SELECT audit_id, ts, actor, batch_id, action, entity_type, entity_id, field_name, old_value, new_value, reason, "
                           "evidence_json, scenario_json, chat_prompt FROM audit_log ORDER BY audit_id", conn)
    ws3 = wb.add_worksheet("Audit trail")
    write_df(ws3, au.rename(columns={"audit_id": "ID", "ts": "Time (UTC)", "actor": "Actor", "batch_id": "Batch", "action": "Action", "entity_type": "Entity",
                                     "entity_id": "Entity id", "field_name": "Field", "old_value": "Old", "new_value": "New", "reason": "Reason",
                                     "evidence_json": "Evidence", "scenario_json": "Scenario", "chat_prompt": "Chat prompt"}), 0, (), (),
             {"Time (UTC)": 22, "Reason": 40, "Evidence": 40, "Scenario": 30, "Chat prompt": 40})
    ws3.autofilter(0, 0, max(len(au), 1), 13)

    # ---- Assumptions
    rfq = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    ws4 = wb.add_worksheet("Assumptions")
    ws4.set_column(0, 0, 44)
    ws4.set_column(1, 1, 90, wrap)
    rows: list[tuple[str, Any]] = [
        ("Comparison unit", "INR per piece, freight included (FOR Chakan), GST excluded"),
        ("USD/INR (fixed, dated)", f"{cfg['fx']['USD_INR']} on {cfg['fx']['date']} ({cfg['fx']['source']})"),
        ("Cost of funds (payment loading)", f"{cfg['cost_of_funds_pct']}% p.a."),
        ("Standard payment terms", f"{rfq['std_payment_terms_days']} days; loading capped at +{cfg['payment_loading_max_extra_days']} days of extra credit"),
        ("MSME credit cap", f"{cfg['msme_max_credit_days']} days for micro and small vendors"),
        ("Expected GST rate", f"{rfq['expected_gst_pct']:g}% (stored, never added to prices)"),
        ("Award reference date (certificate validity)", rfq["award_reference_date"]),
        ("Response due date", rfq["response_due_date"]),
        ("Sanity band", f"+/-{cfg['sanity_band_pct']}% vs last year and vs other vendors' median"),
        ("Confidence routing: core fields", f"auto >= {cfg['thresholds']['core']['auto']:.0%}, spot-check >= {cfg['thresholds']['core']['spot']:.0%}, else must-confirm"),
        ("Confidence routing: terms fields", f"auto >= {cfg['thresholds']['terms']['auto']:.0%}, spot-check >= {cfg['thresholds']['terms']['spot']:.0%}"),
        ("Knock-out answer minimum confidence", f"{cfg['thresholds']['knockout_answer_min']:.0%}"),
        ("Weight formula", f"flute factors {cfg['weight_formula']['flute_factors']}, wastage {cfg['weight_formula']['wastage_factor']}"),
        ("Not-firm prices", "Flagged, not excluded (block_price_not_firm = %s). The kraft-paper escalation formula is not modelled." % cfg.get("eligibility", {}).get("block_price_not_firm")),
        ("Extraction model", f"{cfg['gemini']['model']}; prompt versions: " + ", ".join(f"{n} {prompt_version(n, cfg)}" for n in ("quote_extract", "second_read", "doc_extract", "clarify"))),
        ("Out of scope", "Percentage dual-sourcing within a line; kraft-paper escalation formula; automatic reading of vendor clarification replies; "
                          "multi-category specs; supply terms (palletisation, tolerances, liquidated damages); real email sending."),
    ]
    ws4.write(0, 0, "Assumption", head)
    ws4.write(0, 1, "Value", head)
    for i, (k, v) in enumerate(rows, start=1):
        ws4.write(i, 0, k, bold)
        ws4.write(i, 1, str(v), wrap)
    r0 = len(rows) + 3
    ws4.write(r0, 0, "Flag", head)
    ws4.write(r0, 1, "Meaning", head)
    for i, (k, v) in enumerate(FLAG_HELP.items(), start=r0 + 1):
        ws4.write(i, 0, k)
        ws4.write(i, 1, v, wrap)
    wb.close()
    return buf.getvalue()
