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
