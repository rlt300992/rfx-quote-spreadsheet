"""Validation layers 0-4 (CLAUDE.md step 4 and 6).

Layer 0 completeness  : ensure_completeness() inserts `not_quoted` rows BEFORE any ranking.
Layer 1 arithmetic    : vendor line total vs qty x price.
Layer 2 master data   : line matched, GST rate / tax basis, spec, validity, capacity, price firmness.
Layer 3 sanity        : +/-band vs last year and vs the median of the other vendors.
Layer 4 second read   : re-extraction of the 4 core fields (second_read_json) must agree.
"""
from __future__ import annotations

import sqlite3
import statistics
from typing import Any

from core.convert import PIECES_PER_UNIT


def _chk(layer: int, check: str, result: str, detail: str) -> dict:
    return {"layer": layer, "check": check, "result": result, "detail": detail}


# ---------------------------------------------------------------- layer 0
def ensure_completeness(conn: sqlite3.Connection, response_id: int) -> list[int]:
    """Insert `not_quoted` rows for every RFQ line the (full) response did not cover.

    Never treats a missing line as 0: the row exists, but has no price and is not awardable.
    Returns the line_nos inserted. A partial revision only touches the lines it contains.
    """
    resp = conn.execute("SELECT * FROM vendor_responses WHERE response_id=?", (response_id,)).fetchone()
    if resp is None:
        raise KeyError(f"response {response_id} not found")
    if (resp["revision_scope"] or "full") != "full":
        return []
    lines = conn.execute(
        """SELECT line_id, line_no FROM line_items WHERE rfq_id=?
           AND line_id NOT IN (SELECT line_id FROM quotes WHERE response_id=? AND line_id IS NOT NULL)""",
        (resp["rfq_id"], response_id),
    ).fetchall()
    inserted = []
    for ln in lines:
        has_current = conn.execute(
            "SELECT 1 FROM quotes WHERE vendor_id=? AND line_id=? AND is_current=1",
            (resp["vendor_id"], ln["line_id"]),
        ).fetchone()
        conn.execute(
            """INSERT INTO quotes (response_id, vendor_id, line_id, is_current, quote_status)
               VALUES (?,?,?,?, 'not_quoted')""",
            (response_id, resp["vendor_id"], ln["line_id"], 0 if has_current else 1),
        )
        inserted.append(ln["line_no"])
    conn.commit()
    return sorted(inserted)


# ---------------------------------------------------------------- layers 1-4
def _band_ok(value: float, ref: float, band_pct: float) -> bool:
    return abs(value / ref - 1) * 100 <= band_pct


def run_checks(ctx: dict[str, Any]) -> tuple[list[dict], list[str]]:
    """Return (validation entries, validation-derived flags). Pure: reads ctx only."""
    q, line, resp, rfq, cfg = ctx["q"], ctx["line"], ctx["resp"], ctx["rfq"], ctx["cfg"]
    checks: list[dict] = []
    flags: list[str] = []
    band = cfg["sanity_band_pct"]
    unit, price = q.get("raw_unit"), q.get("raw_price")

    # layer 1: arithmetic
    total = q.get("vendor_line_total")
    if total is not None and price is not None and unit in PIECES_PER_UNIT:
        qty = q.get("offered_qty") or line["annual_qty"]
        expected = price / PIECES_PER_UNIT[unit] * qty
        if expected and abs(total - expected) / expected <= 0.01:
            checks.append(_chk(1, "qty_x_price", "pass",
                               f"{qty} pcs x {price:g}/{unit} = {expected:,.2f} matches vendor total {total:,.2f}"))
        else:
            checks.append(_chk(1, "qty_x_price", "fail",
                               f"{qty} pcs x {price:g}/{unit} = {expected:,.2f} but vendor total is {total:,.2f}"))
            flags.append("ARITHMETIC_FAIL")
    else:
        checks.append(_chk(1, "qty_x_price", "skip", "vendor gave no line total to cross-check"))

    # layer 2: master data
    checks.append(_chk(2, "vendor_line_matched", "pass" if q.get("line_id") else "fail",
                       f"matched to line {line['line_no']} ({line['catalog_sku']})"))
    unit_ok = unit in PIECES_PER_UNIT or unit in ("per_kg", "as_last_year")
    checks.append(_chk(2, "unit_known", "pass" if unit_ok else "fail", f"unit '{unit}' is a recognised pricing unit"))
    cur_ok = (q.get("raw_currency") or "INR").upper() in ("INR", "USD")
    checks.append(_chk(2, "currency_known", "pass" if cur_ok else "fail",
                       f"currency '{q.get('raw_currency') or 'INR'}' has a dated rate in config"))
    fb = q.get("freight_basis")
    if fb == "included" or (fb == "extra_quoted" and q.get("raw_freight_value") is not None):
        checks.append(_chk(2, "freight_basis_stated", "pass", f"freight basis '{fb}' stated by the vendor"))
    else:
        checks.append(_chk(2, "freight_basis_stated", "warn", "vendor did not quote freight; it will be imputed or the line excluded"))
    sm0 = q.get("spec_match")
    checks.append(_chk(2, "spec_exact", "pass" if sm0 == "exact" else "warn",
                       "vendor spec matches ours" if sm0 == "exact" else f"spec match is '{sm0}'"))
    gst = q.get("gst_rate_pct")
    if gst is None:
        checks.append(_chk(2, "gst_rate_expected", "skip", "vendor stated no GST rate"))
    elif abs(gst - rfq["expected_gst_pct"]) < 1e-9:
        checks.append(_chk(2, "gst_rate_expected", "pass",
                           f"GST {gst:g}% equals expected {rfq['expected_gst_pct']:g}%"))
    else:
        checks.append(_chk(2, "gst_rate_expected", "warn",
                           f"GST {gst:g}% differs from expected {rfq['expected_gst_pct']:g}% (stored, never added to the price)"))
        flags.append("GST_RATE_MISMATCH")
    tb = q.get("tax_basis") or "unspecified"
    if tb == "ex_gst":
        checks.append(_chk(2, "tax_basis_stated", "pass", "price stated ex-GST"))
    elif tb == "incl_gst":
        checks.append(_chk(2, "tax_basis_stated", "fail", "price includes GST; backed out for comparison"))
        flags.append("TAX_BASIS_INCL_GST")
    else:
        checks.append(_chk(2, "tax_basis_stated", "fail", "tax basis not stated"))
        flags.append("TAX_BASIS_UNSPECIFIED")
    if tb in ("ex_gst", "incl_gst") and gst is not None:
        checks.append(_chk(2, "tax_basis_consistent", "pass", f"vendor states both the basis ({tb}) and the rate ({gst:g}%)"))
    else:
        checks.append(_chk(2, "tax_basis_consistent", "skip", "basis and rate not both stated"))
    sm = q.get("spec_match")
    if sm == "deviation":
        flags.append("SPEC_DEVIATION")
    elif sm == "substitute":
        flags.append("SPEC_SUBSTITUTE")
    if resp["price_basis"] == "rm_linked":
        flags.append("PRICE_NOT_FIRM")
    if resp["validity_date"] and resp["validity_date"] < rfq["award_reference_date"]:
        flags.append("VALIDITY_EXPIRED")
    offered = q.get("offered_qty")
    if offered is not None and line["annual_qty"] and offered < line["annual_qty"]:
        flags.append("PARTIAL_QTY")

    # layer 3: sanity, on the ex-freight INR/pc price
    base = ctx.get("base_list")
    if base is not None and base > 0:
        fail_refs: list[float] = []
        ly = ctx.get("ly_ref")
        if ly and not ctx.get("is_ly_ref"):
            if _band_ok(base, ly, band):
                checks.append(_chk(3, "sanity_vs_last_year", "pass",
                                   f"INR {base:.2f} within {band:g}% of last year INR {ly:.2f}"))
            else:
                checks.append(_chk(3, "sanity_vs_last_year", "fail",
                                   f"INR {base:.2f} is {abs(base / ly - 1) * 100:.0f}% away from last year INR {ly:.2f}"))
                fail_refs.append(ly)
        else:
            checks.append(_chk(3, "sanity_vs_last_year", "skip", "no independent last-year reference"))
        peers = ctx.get("peer_bases") or []
        if len(peers) >= 2:
            med = statistics.median(peers)
            if _band_ok(base, med, band):
                checks.append(_chk(3, "sanity_vs_peers", "pass",
                                   f"INR {base:.2f} within {band:g}% of other vendors' median INR {med:.2f}"))
            else:
                checks.append(_chk(3, "sanity_vs_peers", "fail",
                                   f"INR {base:.2f} is {abs(base / med - 1) * 100:.0f}% away from other vendors' median INR {med:.2f}"))
                fail_refs.append(med)
        else:
            checks.append(_chk(3, "sanity_vs_peers", "skip", "fewer than 2 other vendor prices for this line"))
        if fail_refs:
            flags.append("SANITY_RANGE_FAIL")
            if unit in ("per_piece", "per_box"):
                for div in (100, 1000):
                    if all(_band_ok(base / div, r, band) for r in fail_refs):
                        flags.append("UNIT_AMBIGUOUS")
                        checks.append(_chk(3, "unit_hypothesis", "fail",
                                           f"if this rate is per {div}, INR {base / div:.2f}/pc fits the reference; "
                                           f"the header says '{unit}'"))
                        break
    else:
        checks.append(_chk(3, "sanity_vs_last_year", "skip", "no comparable INR/pc price"))

    # layer 4: second read
    sr = ctx.get("second_read")
    if sr is None:
        checks.append(_chk(4, "second_read", "skip", "no second read available"))
    else:
        diffs = []
        raw = ctx.get("raw_q") or q  # the second read validates the EXTRACTION, not the buyer's later correction
        for fld, mine in (("raw_price", raw.get("raw_price")), ("raw_unit", raw.get("raw_unit")),
                          ("raw_currency", raw.get("raw_currency")), ("raw_freight_value", raw.get("raw_freight_value"))):
            if fld not in sr:
                continue
            same = sr[fld] == mine or (
                isinstance(mine, (int, float)) and isinstance(sr[fld], (int, float)) and abs(mine - sr[fld]) < 1e-9)
            if not same:
                diffs.append(f"{fld}: first read {mine!r} vs second read {sr[fld]!r}")
        if diffs:
            checks.append(_chk(4, "second_read", "fail", "; ".join(diffs)))
            flags.append("SECOND_READ_MISMATCH")
        else:
            checks.append(_chk(4, "second_read", "pass", "second read agrees on price, unit, currency and freight"))
    return checks, flags
