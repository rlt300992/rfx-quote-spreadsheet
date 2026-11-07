"""Read-only helpers over the views, for the UI. No business logic here."""
from __future__ import annotations

import sqlite3

import pandas as pd


def pending_confirmation_count(conn: sqlite3.Connection) -> int:
    """'X values awaiting confirmation could change the award' (shown on every screen)."""
    return conn.execute(
        "SELECT COUNT(*) FROM v_review_queue WHERE status='must_confirm' AND could_change_winner=1").fetchone()[0]


def comparison_grid(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM v_comparison_grid ORDER BY line_no, vendor_code", conn)


def evaluated_pivot(conn: sqlite3.Connection) -> pd.DataFrame:
    """Line x vendor grid of evaluated INR/pc; blank where a vendor can't be ranked."""
    g = comparison_grid(conn)
    if g.empty:
        return g
    return g.pivot(index="line_no", columns="vendor_code", values="evaluated_inr_pc")


def not_firm_cells(conn: sqlite3.Connection) -> set[tuple[int, str]]:
    """(line_no, vendor_code) whose price is raw-material-linked (PRICE_NOT_FIRM)."""
    rows = conn.execute("SELECT line_no, vendor_code FROM v_comparison_grid WHERE price_not_firm = 1").fetchall()
    return {(r["line_no"], r["vendor_code"]) for r in rows}


def winner_cells(conn: sqlite3.Connection) -> set[tuple[int, str]]:
    """(line_no, vendor_code) of the cheapest AWARDABLE quote per line (eligible quote + qualified vendor)."""
    rows = conn.execute("SELECT line_no, vendor_code FROM v_comparison_grid WHERE rank_awardable = 1").fetchall()
    return {(r["line_no"], r["vendor_code"]) for r in rows}


def vendor_cards(conn: sqlite3.Connection, manifest: list[dict] | None = None) -> list[dict]:
    """One dict per vendor for the Responses tab: format, received time, files, extraction status, lines quoted."""
    due = conn.execute("SELECT response_due_date FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    due = due[0] if due else None
    total = conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]
    mf = {m["vendor_code"]: m for m in (manifest or [])}
    cards = []
    for v in conn.execute("SELECT * FROM vendors ORDER BY vendor_id").fetchall():
        rs = conn.execute("SELECT * FROM v_response_summary WHERE vendor_id=? ORDER BY response_id", (v["vendor_id"],)).fetchall()
        main = next((r for r in rs if r["lines_quoted"] + r["lines_not_quoted"] + r["lines_unmatched"] > 0), None)
        m = mf.get(v["vendor_code"], {})
        received = (main["received_at"] if main else None) or m.get("received_at")
        draft = conn.execute("""SELECT clarification_draft, clarification_status FROM vendor_responses
                                WHERE vendor_id=? AND clarification_draft IS NOT NULL ORDER BY response_id DESC LIMIT 1""",
                             (v["vendor_id"],)).fetchone()
        cards.append({
            "vendor_code": v["vendor_code"], "name": v["name"], "msme": v["msme_category"],
            "format": main["source_type"] if main else None, "received_at": received,
            "late": bool(received and due and received[:10] > due),
            "files": [r["file_name"] for r in rs] or m.get("files", []),
            "extracted": main is not None,
            "status": main["extraction_status"] if main else "not extracted",
            "path": main["extraction_model"] if main else None,
            "lines_quoted": main["lines_quoted"] if main else 0, "lines_not_quoted": main["lines_not_quoted"] if main else 0,
            "lines_unmatched": main["lines_unmatched"] if main else 0, "lines_total": total,
            "clarification_status": draft["clarification_status"] if draft else "none",
            "clarification_draft": draft["clarification_draft"] if draft else None,
        })
    return cards


# ------------------------------------------------------------------ Review & Reconcile
def _loads(v, default):
    import json

    return json.loads(v) if isinstance(v, str) and v else default


def review_rows(conn: sqlite3.Connection) -> list[dict]:
    """Every current quote with its line spec, raw vendor values, normalized values, flags and evidence, parsed
    for display. Ordered so the items most likely to matter come first."""
    rows = conn.execute("""
        SELECT q.quote_id, q.quote_status, v.vendor_code, v.name AS vendor_name, l.line_id, l.line_no, l.description,
               l.length_mm, l.width_mm, l.height_mm, l.ply, l.layers_json, l.annual_qty, l.typical_order_qty,
               l.theoretical_weight_kg, q.raw_price_text, q.raw_price, q.raw_unit, q.raw_currency, q.vendor_line_ref,
               q.spec_match, q.spec_accepted, q.spec_deviation_note, q.discounts_json, q.freight_basis,
               q.raw_freight_value, q.freight_imputed, q.base_inr_pc, q.freight_inr_pc, q.landed_inr_pc,
               q.evaluated_inr_pc, q.min_confidence, q.review_state, q.award_eligible, q.ineligible_reasons_json,
               q.normalization_trace_json, q.validation_json, q.field_meta_json, q.payment_terms_days,
               q.effective_vendor_days, vr.file_name, vr.file_path, vr.source_type,
               g.exclusion_reason, g.qual_status, g.is_awardable, g.price_not_firm
        FROM quotes q
        JOIN vendors v ON v.vendor_id = q.vendor_id
        LEFT JOIN line_items l ON l.line_id = q.line_id
        JOIN vendor_responses vr ON vr.response_id = q.response_id
        LEFT JOIN v_comparison_grid g ON g.quote_id = q.quote_id
        WHERE q.is_current = 1
        ORDER BY l.line_no, v.vendor_code""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        meta = _loads(d["field_meta_json"], {})
        d["flags"] = meta.get("flags", [])
        d["fields"] = meta.get("fields", {})
        d["overrides"] = meta.get("overrides", {})
        d["open_fields"] = [f for f, v in d["fields"].items() if v.get("status") in ("must_confirm", "spot_check")]
        d["must_fields"] = [f for f, v in d["fields"].items() if v.get("status") == "must_confirm"]
        d["trace"] = _loads(d["normalization_trace_json"], [])
        d["validation"] = _loads(d["validation_json"], [])
        d["discounts"] = _loads(d["discounts_json"], [])
        d["layers"] = [lay["gsm"] for lay in _loads(d["layers_json"], [])]
        d["reasons"] = _loads(d["ineligible_reasons_json"], [])
        out.append(d)
    return out


def review_filter(rows: list[dict], tab: str, vendor: str | None = None) -> list[dict]:
    """Sub-tab membership: all / needs_review / not_quoted / unmatched."""
    def keep(r: dict) -> bool:
        if vendor and r["vendor_code"] != vendor:
            return False
        if tab == "not_quoted":
            return r["quote_status"] in ("not_quoted", "no_bid")
        if tab == "unmatched":
            return r["quote_status"] == "unmatched"
        if tab == "needs_review":
            return r["quote_status"] == "quoted" and bool(r["open_fields"])
        return True
    return [r for r in rows if keep(r)]


def review_counts(rows: list[dict], vendor: str | None = None) -> dict[str, int]:
    return {t: len(review_filter(rows, t, vendor)) for t in ("all", "needs_review", "not_quoted", "unmatched")}


# ------------------------------------------------------------------ Bid Analysis
def vendor_totals(conn: sqlite3.Connection, basis: str = "evaluated") -> pd.DataFrame:
    """Per vendor: lines priced, annual value of the lines it priced, last-year value of the same lines and the saving."""
    col = {"evaluated": "evaluated_inr_pc", "landed": "landed_inr_pc"}[basis]
    g = comparison_grid(conn)
    if g.empty:
        return pd.DataFrame()
    p = g[g[col].notna()].copy()

    def unit_doubt(r) -> bool:  # an ambiguous unit the buyer has not yet resolved: don't let it swamp the vendor's total
        meta = _loads(r["field_meta_json"], {})
        return "UNIT_AMBIGUOUS" in meta.get("flags", []) and meta.get("fields", {}).get("unit", {}).get("status") == "must_confirm"

    p["unit_doubt"] = p.apply(unit_doubt, axis=1)
    doubt = p[p["unit_doubt"]].groupby("vendor_code").size().rename("lines_excluded_unit_doubt")
    p = p[~p["unit_doubt"]].copy()
    p["value"] = p[col] * p["annual_qty"]
    p["ly_value"] = p["ly_landed_inr_pc"] * p["annual_qty"]
    p["landed_value"] = p["landed_inr_pc"] * p["annual_qty"]
    agg = p.groupby("vendor_code").agg(lines_priced=("line_no", "count"), value_inr=("value", "sum"),
                                       ly_value_inr=("ly_value", "sum"), landed_value_inr=("landed_value", "sum"))
    agg["saving_vs_ly_inr"] = agg["ly_value_inr"] - agg["landed_value_inr"]
    agg["saving_vs_ly_pct"] = agg["saving_vs_ly_inr"] / agg["ly_value_inr"] * 100
    awardable = g[g["is_awardable"] == 1].groupby("vendor_code").size().rename("lines_awardable")
    agg = agg.join(awardable).join(doubt).fillna({"lines_awardable": 0, "lines_excluded_unit_doubt": 0})
    return agg.reset_index()


def qualification_panel(conn: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(tri-state qualification with knock-out issues, documents with validity on the award reference date)."""
    q = pd.read_sql_query("SELECT vendor_code, vendor_name, qual_status, ko_pass, ko_fail, ko_unknown, ko_issues "
                          "FROM v_vendor_qualification ORDER BY vendor_code", conn)
    d = pd.read_sql_query("SELECT vendor_code, doc_type, doc_number, holder_name, holder_matches_vendor, expiry_date, "
                          "validity_status, award_reference_date FROM v_documents ORDER BY vendor_code, doc_type", conn)
    return q, d
