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


def winner_cells(conn: sqlite3.Connection) -> set[tuple[int, str]]:
    """(line_no, vendor_code) of the cheapest AWARDABLE quote per line (eligible quote + qualified vendor)."""
    rows = conn.execute("SELECT line_no, vendor_code FROM v_comparison_grid WHERE rank_awardable = 1").fetchall()
    return {(r["line_no"], r["vendor_code"]) for r in rows}
