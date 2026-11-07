"""Analyst-agent tools. The agent reads only the views; it can PROPOSE a scenario but never commit.

P1 implements award_scenario(); propose_award / commit_award / query_view arrive in P4.
"""
from __future__ import annotations

import json
import sqlite3

import pandas as pd

SPEC_ONLY = {"spec_substitute", "spec_deviation", "open_must_confirm:spec_match"}


def award_scenario(conn: sqlite3.Connection, basis: str = "evaluated", exclude_vendors: list[str] | None = None,
                   require_qualified: bool = True, include_pending: bool = False,
                   include_accepted_substitutes: bool = False) -> pd.DataFrame:
    """Per-line winner among eligible quotes (L1 on `basis`), plus the L2 backup. Reads v_comparison_grid only.

    require_qualified=True keeps only qualified vendors (include_pending=True also admits pending ones; a
    disqualified vendor is never admitted). require_qualified=False ignores qualification entirely.
    include_accepted_substitutes=True is a what-if: also admit quotes whose ONLY blocker is an
    unaccepted spec deviation/substitute ("what if QA accepts the substitutes").
    Totals are in df.attrs['totals'].
    """
    col = {"evaluated": "evaluated_inr_pc", "landed": "landed_inr_pc"}[basis]
    g = pd.read_sql_query("SELECT * FROM v_comparison_grid", conn)
    exclude = set(exclude_vendors or [])

    def eligible(r) -> bool:
        if r["quote_status"] != "quoted" or pd.isna(r[col]) or r["vendor_code"] in exclude:
            return False
        reasons = set(json.loads(r["ineligible_reasons_json"] or "[]"))
        if reasons and not (include_accepted_substitutes and reasons <= SPEC_ONLY):
            return False
        if require_qualified:
            ok = {"qualified"} | ({"pending"} if include_pending else set())
            return r["qual_status"] in ok
        return True

    g["_ok"] = g.apply(eligible, axis=1) if len(g) else pd.Series(dtype=bool)
    rows = []
    for line_no, grp in g.groupby("line_no"):
        ok = grp[grp["_ok"]].sort_values([col, "vendor_code"])
        first = grp.iloc[0]
        rec = {"line_no": int(line_no), "catalog_sku": first["catalog_sku"], "annual_qty": first["annual_qty"],
               "vendor_code": None, "quote_id": None, "price_inr_pc": None, "annual_value_inr": None,
               "backup_vendor": None, "backup_price_inr_pc": None, "ly_landed_inr_pc": first["ly_landed_inr_pc"],
               "landed_inr_pc": None, "price_not_firm": False, "no_winner_reason": None}
        if ok.empty:
            rec["no_winner_reason"] = "no eligible quote"
        else:
            w = ok.iloc[0]
            rec.update(vendor_code=w["vendor_code"], quote_id=int(w["quote_id"]), price_inr_pc=w[col],
                       annual_value_inr=w[col] * w["annual_qty"], landed_inr_pc=w["landed_inr_pc"],
                       price_not_firm=bool(w["price_not_firm"]))
            if len(ok) > 1:
                rec.update(backup_vendor=ok.iloc[1]["vendor_code"], backup_price_inr_pc=ok.iloc[1][col])
        rows.append(rec)
    df = pd.DataFrame(rows)
    if not df.empty:
        won = df[df["vendor_code"].notna()]
        df.attrs["totals"] = {
            "lines_awarded": int(len(won)), "lines_without_winner": int(len(df) - len(won)),
            "total_value_inr": float(won["annual_value_inr"].sum()),
            "by_vendor": won.groupby("vendor_code")["annual_value_inr"].sum().round(2).to_dict(),
            "ly_value_inr": float((won["ly_landed_inr_pc"] * won["annual_qty"]).sum()),
            "total_landed_inr": float((won["landed_inr_pc"] * won["annual_qty"]).sum()),
            "saving_vs_ly_inr": float(((won["ly_landed_inr_pc"] - won["landed_inr_pc"]) * won["annual_qty"]).sum()),
            "saving_vs_ly_pct": float(((won["ly_landed_inr_pc"] - won["landed_inr_pc"]) * won["annual_qty"]).sum()
                                      / (won["ly_landed_inr_pc"] * won["annual_qty"]).sum() * 100) if len(won) else 0.0,
            # PRICE_NOT_FIRM (raw-material-linked) is a flag, not an exclusion: surface it in every summary
            "lines_on_not_firm_prices": int(won["price_not_firm"].sum()),
            "value_on_not_firm_prices_inr": float(won.loc[won["price_not_firm"], "annual_value_inr"].sum()),
            "not_firm_vendors": sorted(won.loc[won["price_not_firm"], "vendor_code"].unique().tolist()),
        }
    return df
