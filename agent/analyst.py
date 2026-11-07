"""Analyst agent: a Gemini function-calling loop over the views and the scenario tools.

The model never sees base tables and never commits anything. It answers from tool results only; tables and
charts are built here in Python from query results it asks for, so the UI can render them.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import plotly.express as px

from agent import tools as T
from core.config import load_config
from core.db import VIEWS
from extract.gemini_client import generate, model_chain, is_daily_quota_error

MAX_DATA_CALLS = 6      # query_view / award_scenario / propose_award per question
MAX_RENDER_CALLS = 3    # render_table / render_chart per question (presentation, not analysis)
MAX_TURNS = 12
DATA_TOOLS = {"query_view", "award_scenario", "propose_award"}

GLOSSARY = """KEY COLUMNS (v_comparison_grid is one row per line x vendor, current quotes only):
- line_no, catalog_sku, description, annual_qty, typical_order_qty; vendor_code (V1..V5), vendor_name, msme_category
- quote_status: quoted / not_quoted / no_bid / unmatched. A not_quoted line has no price; it is never zero.
- base_inr_pc (INR/pc ex-freight ex-GST), freight_inr_pc, landed_inr_pc (= base + freight = the QUOTED price),
  evaluated_inr_pc (= landed + one-time charges + payment-terms loading; this is the RANKING price). GST is never included.
- rank_landed / rank_evaluated (among all priced quotes on the line), rank_awardable (1 = cheapest awardable quote on the line).
- is_awardable = quote is eligible AND vendor is qualified. exclusion_reason says why a quote cannot win
  (not_quoted, vendor_disqualified, vendor_pending_qualification, freight_unknown, open_must_confirm:<field>, spec_substitute, partial_qty ...).
- qual_status: qualified / pending / disqualified (a missing or unknown knock-out answer = pending). ko_issues explains it (v_vendor_qualification).
- review_state: auto_ok / spot_check / must_confirm / confirmed. must_confirm values are excluded from the award until the buyer confirms them.
- freight_imputed: 0 = vendor quoted or included freight, 1 = imputed from the highest freight another vendor quoted, 2 = from last year.
- price_not_firm = 1 means the price is linked to kraft paper cost (a flag, still comparable). flags_json lists all flags on the quote.
- ly_landed_inr_pc: last year's landed price from the main supplier; saving_vs_ly_inr_pc = ly_landed - landed.
- payment_terms_days = days of credit the vendor QUOTED; effective_vendor_days = days used in the evaluation (45-day MSME cap applied). Evaluated minus landed is mostly the payment-terms loading: vendors with LESS credit than the 60-day standard get a positive loading, MORE credit a negative one.
- annual_value_inr = evaluated_inr_pc * annual_qty. is_awarded / award_stale describe a committed award.
v_review_queue: one row per open (must_confirm / spot_check) field: field_name, confidence, status, flags_json, could_change_winner (1 if confirming it could change who wins the line).
v_documents: validity_status (valid / expired / expiring_90d / unknown) on the award reference date; holder_matches_vendor 0 = held by another company.
"""

SYSTEM = """You are the procurement analyst inside a quote-comparison tool for a category buyer at Sahyadri Consumer Products Ltd (corrugated boxes, 30 lines, 5 vendors V1-V5). The buyer will act on your answers with real money, so accuracy and honesty matter more than fluency.

RULES
1. Answer ONLY from tool results. Never invent or estimate a number, a vendor, a line or a reason. If you did not fetch it, say you have not.
2. Use query_view for facts and award_scenario for any "what if". You have at most 6 analysis tool calls per question, so combine facts into one query (GROUP BY, JOINs, json_each) and prefer the scenario tools' totals to hand arithmetic. Use render_chart when a chart is asked for or clearly helps, and render_table for lists the buyer should see.
3. Cite what you used: vendor codes, line numbers and the figures, in INR. Say whether a price is evaluated (ranking) or landed (quoted), and NEVER give one number as both: if you quote a price, quote the column it came from (landed_inr_pc or evaluated_inr_pc). GST is excluded from everything.
4. Never explain WHY a price or total differs (payment terms, freight, tier, spec) unless you queried the column that shows it (payment_terms_days, effective_vendor_days, freight_inr_pc, tier_applied via flags_json). If you did not query it, describe the difference without a cause. Never attach a payment-term or freight figure to a vendor from memory.
5. Be explicit about uncertainty: say when a figure rests on values that are still unconfirmed (review_state must_confirm / spot_check, or ineligible_reasons), on imputed freight (freight_imputed > 0), on prices that are not firm (price_not_firm = 1, kraft-linked), on per-kg derived prices or "same as last year" prices (see flags_json).
6. A vendor that is pending or disqualified cannot win, even if cheapest. Explain why using ko_issues / exclusion_reason. Do not suggest overriding qualification as if it were routine.
7. You cannot commit an award. propose_award only stages a proposal that the buyer must confirm in the app; use it only if asked to propose/prepare an award, and then tell the buyer to review the Award panel and press Confirm award.
8. Backup (L2) vendors come from the scenario tool; do not propose percentage splits within a line.
9. Be concise: lead with the answer, then the supporting numbers, then caveats. Use plain text and short lists; Indian formatting (lakh/crore) is fine alongside full rupee figures.

""" + GLOSSARY


@dataclass
class AnalystResult:
    text: str
    tables: list[tuple[str, pd.DataFrame]] = field(default_factory=list)
    figures: list[tuple[str, Any]] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    proposal: dict | None = None
    data_calls: int = 0
    model: str = ""


def schema_text(conn: sqlite3.Connection) -> str:
    parts = []
    for v in VIEWS:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({v})")]
        parts.append(f"{v}({', '.join(cols)})")
    return "VIEWS AND COLUMNS (SQLite):\n" + "\n".join(parts)


def _jsonable(v: Any) -> Any:
    if isinstance(v, float):
        return None if pd.isna(v) else round(v, 4)
    if hasattr(v, "item"):
        return _jsonable(v.item())
    return v


def df_json(df: pd.DataFrame, max_rows: int = 60) -> dict:
    rows = [{k: _jsonable(v) for k, v in r.items()} for r in df.head(max_rows).to_dict("records")]
    return {"columns": list(df.columns), "rows": rows, "row_count": int(len(df)), "truncated": bool(len(df) > max_rows)}


def scenario_json(df: pd.DataFrame) -> dict:
    cols = ["line_no", "vendor_code", "price_inr_pc", "backup_vendor", "backup_price_inr_pc", "price_not_firm", "freight_imputed", "review_state", "no_winner_reason"]
    out = {"totals": {k: _jsonable(v) if not isinstance(v, (dict, list)) else v for k, v in df.attrs.get("totals", {}).items()},
           "lines": [{k: _jsonable(r[k]) for k in cols} for r in df.to_dict("records")]}
    if df.attrs.get("order_qty_override"):
        out["order_qty_override"] = df.attrs["order_qty_override"]
    return out


def build_figure(df: pd.DataFrame, kind: str, x: str, y: str, color: str | None, title: str):
    for c in (x, y) + ((color,) if color else ()):
        if c not in df.columns:
            raise ValueError(f"column '{c}' is not in the query result; columns are {list(df.columns)}")
    if kind == "pie":
        fig = px.pie(df, names=x, values=y, title=title)
    else:
        fn = {"bar": px.bar, "line": px.line, "scatter": px.scatter}[kind]
        fig = fn(df, x=x, y=y, color=color, title=title, **({"barmode": "group"} if kind == "bar" and color else {}))
    fig.update_layout(height=380, margin=dict(t=50, b=20))
    return fig


def run_tool(ctx: T.ToolContext, name: str, args: dict, user_text: str = "") -> dict:
    """Execute one tool call; returns a JSON-able dict (errors are returned to the model, not raised)."""
    try:
        if name == "query_view":
            return df_json(T.query_view(ctx.conn, args["sql"], snapshot=ctx.snapshot()))
        if name == "award_scenario":
            return scenario_json(T.award_scenario(ctx.conn, **_scenario_args(args)))
        if name == "propose_award":
            p = T.propose_award(ctx, chat_prompt=user_text, **_scenario_args(args))
            df = pd.DataFrame(p["rows"]); df.attrs["totals"] = p["totals"]
            out = scenario_json(df)
            out["status"] = "PROPOSED, not committed. The buyer must review the Award panel and press Confirm award."
            return out
        if name == "render_table":
            df = T.query_view(ctx.conn, args["sql"], snapshot=ctx.snapshot())
            ctx.tables.append((args.get("title", ""), df))
            return {"shown_to_buyer": True, "rows": int(len(df))}
        if name == "render_chart":
            df = T.query_view(ctx.conn, args["sql"], snapshot=ctx.snapshot())
            fig = build_figure(df, args["kind"], args["x"], args["y"], args.get("color"), args["title"])
            ctx.figures.append((args["title"], fig))
            return {"shown_to_buyer": True, "points": int(len(df))}
        return {"error": f"unknown tool {name}"}
    except (ValueError, KeyError, TypeError) as e:
        return {"error": str(e)}


def _scenario_args(args: dict) -> dict:
    allowed = set(T.SCENARIO_PROPS)
    out = {k: v for k, v in args.items() if k in allowed and v is not None}
    if "exclude_vendors" in out:
        out["exclude_vendors"] = list(out["exclude_vendors"])
    if "order_qty_override" in out:
        out["order_qty_override"] = int(out["order_qty_override"])
    return out


def _declarations():
    from google.genai import types

    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=s["name"], description=s["description"], parameters_json_schema=s["parameters"]) for s in T.TOOL_SPECS])]


def ask(conn: sqlite3.Connection, question: str, history: list[tuple[str, str]] | None = None) -> AnalystResult:
    """Answer one question. `history` is prior (role, text) turns, role 'user' or 'assistant'."""
    from google.genai import types

    cfg = load_config()
    chain = model_chain("analyst", cfg)
    ctx = T.ToolContext(conn)
    contents: list[Any] = []
    for role, text in (history or [])[-8:]:
        contents.append(types.Content(role="user" if role == "user" else "model", parts=[types.Part.from_text(text=text)]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=question)]))
    config = dict(system_instruction=SYSTEM + "\n" + schema_text(conn), tools=_declarations(),
                  automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    res = AnalystResult(text="")
    data_calls = render_calls = 0
    try:
        for turn in range(MAX_TURNS):
            resp = None
            for m in chain:
                try:
                    resp = generate(contents, model=m, **config)
                    res.model = m
                    break
                except Exception as e:
                    if not is_daily_quota_error(e):
                        raise
            if resp is None:
                raise RuntimeError("Gemini quota is exhausted for every configured model; try again later.")
            cand = resp.candidates[0].content if resp.candidates else None
            parts = list(cand.parts or []) if cand is not None else []
            calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
            if not calls:
                res.text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)).strip()
                break
            contents.append(cand)  # keep the model's own content (carries any thought signatures)
            replies = []
            for fc in calls:
                args = dict(fc.args or {})
                if fc.name in DATA_TOOLS:
                    data_calls += 1
                    over = data_calls > MAX_DATA_CALLS
                else:
                    render_calls += 1
                    over = render_calls > MAX_RENDER_CALLS
                result = ({"error": "tool-call budget for this question is used up. Answer now from the results you already have."}
                          if over else run_tool(ctx, fc.name, args, question))
                res.trace.append({"tool": fc.name, "args": args, "ok": "error" not in result,
                                  "result": result if "error" in result else {k: result[k] for k in list(result)[:3]}})
                replies.append(types.Part.from_function_response(name=fc.name, response=result))
            contents.append(types.Content(role="user", parts=replies))
        if not res.text:  # loop ended without a final answer: ask for it with tools switched off
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text="Give your final answer now, from the tool results above only.")]))
            final = generate(contents, model=res.model or chain[0], system_instruction=config["system_instruction"])
            res.text = (final.text or "").strip()
    finally:
        ctx.close()
    res.tables, res.figures, res.proposal, res.data_calls = ctx.tables, ctx.figures, ctx.proposal, data_calls
    return res
