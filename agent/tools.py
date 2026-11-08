"""Analyst-agent tools and the award commit.

The agent can only READ the six views (query_view) and call pure scenario tools. It can PROPOSE an award
(propose_award stages it for the UI) but never commits: commit_award() is called by the Confirm award button.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from core import audit
from core.db import VIEWS
from core.flags import explain_reason

SPEC_ONLY = {"spec_substitute", "spec_deviation", "open_must_confirm:spec_match"}
BYPASS_CODES = ("qual_fail", "spec", "capacity", "validity", "price_not_firm", "buyer_judgment")
ROW_LIMIT = 500


# ------------------------------------------------------------------ query_view: views only, read only, 500 rows
def _view_snapshot(conn: sqlite3.Connection) -> sqlite3.Connection:
    """An in-memory database holding ONLY copies of the six views as tables. Whatever SQL runs against it can
    see nothing else (no base tables, no audit log), cannot write to the real database, and is forced read-only."""
    mem = sqlite3.connect(":memory:")
    for v in VIEWS:
        pd.read_sql_query(f"SELECT * FROM {v}", conn).to_sql(v, mem, index=False)
    mem.execute("PRAGMA query_only = ON")

    def authorizer(action: int, *_: Any) -> int:
        ok = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        return sqlite3.SQLITE_OK if action in ok else sqlite3.SQLITE_DENY

    mem.set_authorizer(authorizer)
    return mem


def query_view(conn: sqlite3.Connection, sql: str, limit: int = ROW_LIMIT, snapshot: sqlite3.Connection | None = None) -> pd.DataFrame:
    """Run one read-only SELECT against the views. Raises ValueError with a message the model can act on."""
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise ValueError("empty query")
    if ";" in s:
        raise ValueError("only one statement is allowed")
    if "--" in s or "/*" in s:
        raise ValueError("SQL comments are not allowed")
    if s.split(None, 1)[0].lower() not in ("select", "with"):
        raise ValueError("only SELECT queries are allowed")
    mem = snapshot or _view_snapshot(conn)
    try:
        return pd.read_sql_query(f"SELECT * FROM ({s}) LIMIT {int(min(limit, ROW_LIMIT))}", mem)
    except Exception as e:
        msg = str(e)
        if "no such table" in msg:
            raise ValueError(f"{msg}. Only these views exist: {', '.join(VIEWS)}") from None
        raise ValueError(f"query failed: {msg}") from None
    finally:
        if snapshot is None:
            mem.close()


# ------------------------------------------------------------------ award_scenario
def scenario_eligibility(g: pd.DataFrame, basis: str = "evaluated", exclude_vendors: list[str] | None = None, require_qualified: bool = True,
                         include_pending: bool = False, include_accepted_substitutes: bool = False) -> pd.Series:
    """True for every grid row that may win under these scenario settings (the rule award_scenario applies; exposed so the grid can grey out the rest)."""
    col = {"evaluated": "evaluated_inr_pc", "landed": "landed_inr_pc"}[basis]
    exclude = set(exclude_vendors or [])

    def eligible(r) -> bool:
        if r["quote_status"] != "quoted" or pd.isna(r[col]) or r["vendor_code"] in exclude:
            return False
        reasons = set(json.loads(r["ineligible_reasons_json"] or "[]"))
        if reasons and not (include_accepted_substitutes and reasons <= SPEC_ONLY):
            return False
        if require_qualified:
            return r["qual_status"] in ({"qualified"} | ({"pending"} if include_pending else set()))
        return True

    return g.apply(eligible, axis=1) if len(g) else pd.Series(dtype=bool)


def _scenario_from_grid(g: pd.DataFrame, basis: str, exclude_vendors: list[str] | None, require_qualified: bool,
                        include_pending: bool, include_accepted_substitutes: bool) -> pd.DataFrame:
    col = {"evaluated": "evaluated_inr_pc", "landed": "landed_inr_pc"}[basis]

    g = g.copy()
    g["_ok"] = scenario_eligibility(g, basis, exclude_vendors, require_qualified, include_pending, include_accepted_substitutes)
    rows = []
    for line_no, grp in g.groupby("line_no"):
        ok = grp[grp["_ok"]].sort_values([col, "vendor_code"])
        first = grp.iloc[0]
        rec = {"line_no": int(line_no), "catalog_sku": first["catalog_sku"], "description": first["description"],
               "annual_qty": first["annual_qty"], "typical_order_qty": first["typical_order_qty"], "vendor_code": None,
               "quote_id": None, "price_inr_pc": None, "annual_value_inr": None, "backup_vendor": None,
               "backup_price_inr_pc": None, "ly_landed_inr_pc": first["ly_landed_inr_pc"], "landed_inr_pc": None,
               "price_not_firm": False, "freight_imputed": 0, "review_state": None, "no_winner_reason": None}
        if ok.empty:
            rec["no_winner_reason"] = "no eligible quote"
        else:
            w = ok.iloc[0]
            rec.update(vendor_code=w["vendor_code"], quote_id=int(w["quote_id"]), price_inr_pc=w[col],
                       annual_value_inr=w[col] * w["annual_qty"], landed_inr_pc=w["landed_inr_pc"],
                       price_not_firm=bool(w["price_not_firm"]), freight_imputed=int(w["freight_imputed"] or 0),
                       review_state=w["review_state"])
            if len(ok) > 1:
                rec.update(backup_vendor=ok.iloc[1]["vendor_code"], backup_price_inr_pc=ok.iloc[1][col])
        rows.append(rec)
    df = pd.DataFrame(rows)
    if not df.empty:
        won = df[df["vendor_code"].notna()]
        ly = float((won["ly_landed_inr_pc"] * won["annual_qty"]).sum())
        sav = float(((won["ly_landed_inr_pc"] - won["landed_inr_pc"]) * won["annual_qty"]).sum())
        df.attrs["totals"] = {
            "basis": basis, "lines_awarded": int(len(won)), "lines_without_winner": int(len(df) - len(won)),
            "total_value_inr": float(won["annual_value_inr"].sum()),
            "by_vendor": won.groupby("vendor_code")["annual_value_inr"].sum().round(2).to_dict(),
            "lines_by_vendor": won.groupby("vendor_code").size().to_dict(),
            "ly_value_inr": ly, "total_landed_inr": float((won["landed_inr_pc"] * won["annual_qty"]).sum()),
            "saving_vs_ly_inr": sav, "saving_vs_ly_pct": sav / ly * 100 if ly else 0.0,
            "lines_on_not_firm_prices": int(won["price_not_firm"].sum()),
            "value_on_not_firm_prices_inr": float(won.loc[won["price_not_firm"], "annual_value_inr"].sum()),
            "not_firm_vendors": sorted(won.loc[won["price_not_firm"], "vendor_code"].unique().tolist()),
            "lines_with_imputed_freight": int((won["freight_imputed"] > 0).sum()),
            "lines_with_open_spot_checks": int((won["review_state"] == "spot_check").sum()),
        }
    return df


def award_scenario(conn: sqlite3.Connection, basis: str = "evaluated", exclude_vendors: list[str] | None = None,
                   require_qualified: bool = True, include_pending: bool = False,
                   include_accepted_substitutes: bool = False, order_qty_override: int | None = None) -> pd.DataFrame:
    """Per-line winner among eligible quotes (L1 on `basis`), plus the L2 backup. Reads v_comparison_grid only.

    require_qualified=True keeps only qualified vendors (include_pending=True also admits pending ones; a
    disqualified vendor is never admitted); require_qualified=False ignores qualification.
    include_accepted_substitutes=True is a what-if that also admits quotes whose ONLY blocker is an unaccepted
    spec deviation/substitute. order_qty_override re-prices EVERY line as if the typical order (release) quantity
    were that many pieces, in a throwaway in-memory copy: volume tiers and discounts are re-matched, nothing is saved.
    Totals are in df.attrs['totals'].
    """
    src = conn
    mem = None
    if order_qty_override:
        from core.recompute import recompute_all

        mem = sqlite3.connect(":memory:")
        mem.row_factory = sqlite3.Row
        conn.backup(mem)
        mem.execute("UPDATE line_items SET typical_order_qty=?", (int(order_qty_override),))
        recompute_all(mem)
        src = mem
    try:
        g = pd.read_sql_query("SELECT * FROM v_comparison_grid", src)
        df = _scenario_from_grid(g, basis, exclude_vendors, require_qualified, include_pending, include_accepted_substitutes)
        if order_qty_override:
            df.attrs["order_qty_override"] = int(order_qty_override)
        return df
    finally:
        if mem is not None:
            mem.close()


# ------------------------------------------------------------------ proposals (staged for the UI, never committed by the agent)
@dataclass
class ToolContext:
    """Per-question state shared by the agent's tools."""
    conn: sqlite3.Connection
    proposal: dict | None = None
    tables: list[tuple[str, pd.DataFrame]] = field(default_factory=list)
    figures: list[tuple[str, Any]] = field(default_factory=list)
    _snapshot: sqlite3.Connection | None = None

    def snapshot(self) -> sqlite3.Connection:
        if self._snapshot is None:
            self._snapshot = _view_snapshot(self.conn)
        return self._snapshot

    def close(self) -> None:
        if self._snapshot is not None:
            self._snapshot.close()
            self._snapshot = None


def propose_award(ctx: ToolContext, chat_prompt: str = "", **params: Any) -> dict:
    """Stage a scenario as the proposed award. Nothing is written to the database; the buyer must click Confirm."""
    df = award_scenario(ctx.conn, **params)
    ctx.proposal = {"params": params, "rows": df.to_dict("records"), "totals": df.attrs.get("totals", {}),
                    "chat_prompt": chat_prompt, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return ctx.proposal


def award_review(conn: sqlite3.Connection, proposal: dict) -> pd.DataFrame:
    """Each proposed line next to the L1 (cheapest current quote, eligible or not) and the L1-eligible quote.
    `bypass` is True where the proposed award is not the cheapest eligible one, which needs a recorded reason."""
    g = pd.read_sql_query("SELECT * FROM v_comparison_grid", conn)
    out = []
    for r in proposal["rows"]:
        grp = g[g["line_no"] == r["line_no"]]
        priced = grp[(grp["quote_status"] == "quoted") & grp["evaluated_inr_pc"].notna()].sort_values(["evaluated_inr_pc", "vendor_code"])
        elig = grp[grp["is_awardable"] == 1].sort_values(["evaluated_inr_pc", "vendor_code"])
        l1 = priced.iloc[0] if len(priced) else None
        l1e = elig.iloc[0] if len(elig) else None
        has_winner = r["quote_id"] is not None and not pd.isna(r["quote_id"])   # pandas turns a missing id into NaN
        out.append({"line_no": r["line_no"], "awarded_vendor": r["vendor_code"], "awarded_quote_id": int(r["quote_id"]) if has_winner else None,
                    "price_inr_pc": r["price_inr_pc"], "l1_vendor": None if l1 is None else l1["vendor_code"],
                    "l1_quote_id": None if l1 is None else int(l1["quote_id"]),
                    "l1_eligible_vendor": None if l1e is None else l1e["vendor_code"],
                    "l1_eligible_quote_id": None if l1e is None else int(l1e["quote_id"]),
                    "bypass": has_winner and (l1e is None or int(l1e["quote_id"]) != int(r["quote_id"]))})
    return pd.DataFrame(out)


def _candidates(g: pd.DataFrame, line_no: int) -> list[dict]:
    return [{"vendor": r["vendor_code"], "quote_id": int(r["quote_id"]), "status": r["quote_status"],
             "landed": None if pd.isna(r["landed_inr_pc"]) else float(r["landed_inr_pc"]),
             "evaluated": None if pd.isna(r["evaluated_inr_pc"]) else float(r["evaluated_inr_pc"]),
             "awardable": bool(r["is_awardable"]), "exclusion_reason": r["exclusion_reason"],
             "explanation": explain_reason(r["exclusion_reason"])} for _, r in g[g["line_no"] == line_no].sort_values("vendor_code").iterrows()]


def commit_award(conn: sqlite3.Connection, proposal: dict, chat_prompt: str | None = None, reason: str | None = None,
                 bypass_code: str | None = None, actor: str = "buyer") -> dict:
    """Write the proposed award. Called ONLY from the Confirm award button.

    Re-checks every quote (still current, on the right line, still awardable). Wherever the award is not the
    cheapest eligible quote a reason and a bypass code are required. Writes one audit row per line under one
    batch_id and sets awarded_quote_id, l1_quote_id, l1_eligible_quote_id, bypass_reason_code on the line.
    """
    review = award_review(conn, proposal)
    won = review[review["awarded_quote_id"].notna()]
    if won.empty:
        raise ValueError("the proposal has no winning lines to award")
    g = pd.read_sql_query("SELECT * FROM v_comparison_grid", conn)
    bad = []
    for _, r in won.iterrows():
        row = g[(g["quote_id"] == int(r["awarded_quote_id"])) & (g["line_no"] == r["line_no"])]
        if row.empty or int(row.iloc[0]["is_awardable"]) != 1:
            bad.append(int(r["line_no"]))
    if bad:
        raise ValueError(f"these lines changed since the proposal and can no longer be awarded as proposed: {bad}. Re-run the scenario.")
    bypass = won[won["bypass"]]
    if len(bypass):
        if not (reason or "").strip():
            raise ValueError(f"a reason is required for lines where the award is not the cheapest eligible quote: {sorted(bypass['line_no'].astype(int))}")
        if bypass_code not in BYPASS_CODES:
            raise ValueError(f"choose a bypass reason code from {BYPASS_CODES}")
    batch = audit.new_batch_id()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    basis = json.dumps(proposal.get("params") or {}, sort_keys=True)
    try:
        for _, r in won.iterrows():
            ln = int(r["line_no"])
            code = bypass_code if r["bypass"] else None
            conn.execute("""UPDATE line_items SET awarded_quote_id=?, award_basis=?, l1_quote_id=?, l1_eligible_quote_id=?,
                            bypass_reason_code=?, awarded_at=? WHERE line_no=?""",
                         (int(r["awarded_quote_id"]), f"scenario {basis}", r["l1_quote_id"], r["l1_eligible_quote_id"], code, now, ln))
            audit.log(conn, "award", ("line_item", ln), field="awarded_quote_id", new=int(r["awarded_quote_id"]),
                      reason=reason if r["bypass"] else None, actor=actor, batch_id=batch, chat_prompt=chat_prompt or proposal.get("chat_prompt"),
                      evidence={"awarded_vendor": r["awarded_vendor"], "l1_vendor": r["l1_vendor"], "l1_eligible_vendor": r["l1_eligible_vendor"],
                                "bypass": bool(r["bypass"]), "bypass_reason_code": code},
                      scenario={"params": proposal.get("params"), "candidates": _candidates(g, ln)})
        total = conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchone()[0]
        conn.execute("UPDATE rfqs SET status=?", ("awarded" if done == total else "evaluating",))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"batch_id": batch, "lines_awarded": int(len(won)), "bypass_lines": [int(x) for x in bypass["line_no"]],
            "lines_without_winner": int(len(review) - len(won))}


def unaward(conn: sqlite3.Connection, reason: str, actor: str = "buyer") -> int:
    """Clear every committed award (reason required, audited per line). Returns the number of lines cleared."""
    if not (reason or "").strip():
        raise ValueError("a reason is required to clear awards")
    rows = conn.execute("SELECT line_no, awarded_quote_id FROM line_items WHERE awarded_quote_id IS NOT NULL").fetchall()
    batch = audit.new_batch_id()
    for r in rows:
        audit.log(conn, "unaward", ("line_item", r["line_no"]), field="awarded_quote_id", old=r["awarded_quote_id"], new=None,
                  reason=reason, actor=actor, batch_id=batch)
    conn.execute("""UPDATE line_items SET awarded_quote_id=NULL, award_basis=NULL, l1_quote_id=NULL, l1_eligible_quote_id=NULL,
                    bypass_reason_code=NULL, awarded_at=NULL""")
    conn.execute("UPDATE rfqs SET status='evaluating'")
    conn.commit()
    return len(rows)


# ------------------------------------------------------------------ what the model sees
SCENARIO_PROPS = {
    "basis": {"type": "string", "enum": ["evaluated", "landed"], "description": "evaluated = ranking price (landed + one-time charges + payment-terms loading, default); landed = quoted price incl. freight."},
    "exclude_vendors": {"type": "array", "items": {"type": "string"}, "description": "Vendor codes to leave out, e.g. [\"V3\"]."},
    "require_qualified": {"type": "boolean", "description": "Default true: only vendors that cleared the knock-out questionnaire (qual_status = qualified) can win."},
    "include_pending": {"type": "boolean", "description": "Also let vendors with qual_status = pending win (a what-if; they have not cleared the questionnaire). Disqualified vendors are never admitted."},
    "include_accepted_substitutes": {"type": "boolean", "description": "What-if: also admit quotes whose only blocker is an unaccepted spec deviation/substitute."},
    "order_qty_override": {"type": "integer", "description": "Re-price every line as if each order (release) were this many pieces, so volume tiers/discounts are re-matched. Nothing is saved."},
}

TOOL_SPECS: list[dict] = [
    {"name": "query_view", "description": "Run ONE read-only SQLite SELECT against the views (v_comparison_grid, v_vendor_qualification, v_review_queue, v_documents, v_ly_baseline, v_response_summary). Max 500 rows. Use this for facts: prices, ranks, flags, exclusion reasons, qualification, documents. Never guess a number you could query.",
     "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": "A single SELECT statement over the views only."}}, "required": ["sql"]}},
    {"name": "award_scenario", "description": "Compute the per-line winner (cheapest eligible quote) with the backup (L2) per line, totals, savings vs last year, and counts of not-firm / imputed-freight lines. Use it for every 'what if' question. It does not save anything.",
     "parameters": {"type": "object", "properties": SCENARIO_PROPS}},
    {"name": "propose_award", "description": "Stage a scenario as the PROPOSED award for the buyer to review. Same parameters as award_scenario. Use only when the buyer asks to propose or prepare an award. It cannot commit anything: the buyer must click Confirm award in the app.",
     "parameters": {"type": "object", "properties": SCENARIO_PROPS}},
    {"name": "render_table", "description": "Show the result of a SELECT over the views to the buyer as a table in the chat. Use for lists the buyer should see (e.g. unconfirmed values). Prefer few, readable columns.",
     "parameters": {"type": "object", "properties": {"sql": {"type": "string"}, "title": {"type": "string"}}, "required": ["sql", "title"]}},
    {"name": "render_chart", "description": "Show a chart to the buyer. The data comes from a SELECT over the views. Suggest a chart when it helps (comparisons across vendors or lines).",
     "parameters": {"type": "object", "properties": {
         "sql": {"type": "string", "description": "SELECT returning the columns used below."},
         "kind": {"type": "string", "enum": ["bar", "line", "scatter", "pie"]},
         "x": {"type": "string"}, "y": {"type": "string"}, "color": {"type": "string", "description": "Optional column to colour/group by."},
         "title": {"type": "string"}}, "required": ["sql", "kind", "x", "y", "title"]}},
]
