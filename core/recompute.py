"""recompute_quote(): the ONLY writer of normalization (N), validation (V) and eligibility columns.

Raw (X) columns are never touched. Buyer corrections live in quotes.field_meta_json["overrides"] and
are applied on top of the raw values here, so the raw extraction stays auditable.

Pipeline for one quote:
  as-last-year lookup (D-LY) -> volume tier (D1) -> convert() (+FX) -> conditional discounts only if
  toggled on (D2) -> freight (D4) -> one-time charges -> MSME cap (D3) -> payment loading (D5)
  -> landed / evaluated -> validation layers 1-4 -> confidence & routing -> eligibility.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from core import confidence as conf_mod
from core import validate
from core.config import load_config
from core.convert import PIECES_PER_UNIT, convert

OVERRIDABLE = {"raw_price", "raw_unit", "raw_currency", "raw_freight_value", "freight_basis", "tax_basis",
               "gst_rate_pct", "payment_terms_days", "lead_time_days", "moq_qty", "offered_qty"}
MSME_CAPPED = {"micro", "small"}


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) and v else None


def _dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def effective_quote(row: sqlite3.Row | dict) -> dict:
    """Quote row as a dict with the buyer's overrides applied on top of the raw columns."""
    d = dict(row)
    meta = _j(d.get("field_meta_json")) or {}
    for col, o in (meta.get("overrides") or {}).items():
        if col in OVERRIDABLE:
            d[col] = o["value"]
    return d


def _ly_row(conn: sqlite3.Connection, vendor_id: int, sku: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM last_year_prices WHERE vendor_id=? AND catalog_sku=? ORDER BY period DESC LIMIT 1",
        (vendor_id, sku)).fetchone()


def _ly_reference_price(conn: sqlite3.Connection, vendor_id: int, sku: str) -> float | None:
    own = _ly_row(conn, vendor_id, sku)
    if own:
        return own["price_inr_pc"]
    main = conn.execute("SELECT price_inr_pc FROM last_year_prices WHERE catalog_sku=? "
                        "ORDER BY qty_supplied DESC LIMIT 1", (sku,)).fetchone()
    return main["price_inr_pc"] if main else None


def _layers_sig(layers: list[dict]) -> list[tuple]:
    return [(l.get("role"), l.get("flute"), l.get("gsm")) for l in layers]


def ly_spec_changed(line: sqlite3.Row, lyp: sqlite3.Row) -> bool:
    snap = _j(lyp["spec_snapshot_json"]) or {}
    cur = _j(line["layers_json"]) or []
    return (snap.get("ply") != line["ply"] or snap.get("flute") != line["flute_profile"]
            or _layers_sig(snap.get("layers") or []) != _layers_sig(cur))


def _apply_tier(q: dict, line: sqlite3.Row, price: float, trace: list[str]) -> tuple[float, dict]:
    """D1: tiers are matched against typical_order_qty unless the vendor says annual."""
    info: dict[str, Any] = {"tier_applied": None, "tier_qty_used": None, "flags": []}
    tiers = _j(q.get("price_tiers_json")) or []
    best = None
    for t in tiers:
        basis = t.get("qty_basis") or "unclear"
        if basis == "unclear":
            info["flags"].append("TIER_BASIS_UNCLEAR")
        qty = line["annual_qty"] if basis == "annual" else line["typical_order_qty"]
        if qty >= t["min_qty"]:
            if best is None or t["min_qty"] > best[0]["min_qty"]:
                best = (t, qty, basis)
        elif basis == "per_order":
            info["flags"].append("TIER_NOT_MET_PER_ORDER")
            trace.append(f"Tier >= {t['min_qty']:,} per order NOT met: typical order is {qty:,} pcs, so the list price applies")
        else:
            trace.append(f"Tier >= {t['min_qty']:,} (basis unclear, treated per order) not met: typical order is {qty:,} pcs; "
                         "higher list price used")
    if best:
        t, qty, basis = best
        info.update(tier_applied=f">={t['min_qty']} @ {t['price']:g} ({basis})", tier_qty_used=qty)
        trace.append(f"Tier >= {t['min_qty']:,} met on {basis} qty {qty:,}: rate {price:g} -> {t['price']:g}")
        price = float(t["price"])
    info["flags"] = list(dict.fromkeys(info["flags"]))
    return price, info


def _imputed_freight(conn: sqlite3.Connection, q: dict, vendor_sku: str) -> tuple[float | None, int, int | None, str]:
    """D4: highest freight quoted by another vendor on this line (1), else last year's (2), else None."""
    peers = conn.execute(
        "SELECT * FROM quotes WHERE line_id=? AND vendor_id<>? AND is_current=1 AND quote_status='quoted'",
        (q["line_id"], q["vendor_id"])).fetchall()
    best: tuple[float, int, int] | None = None  # (freight, quote_id, vendor_id)
    for p in peers:
        e = effective_quote(p)
        if e.get("freight_basis") == "extra_quoted" and e.get("raw_freight_value") is not None:
            cand = (float(e["raw_freight_value"]), p["quote_id"], p["vendor_id"])
            if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[2] < best[2]):
                best = cand
    if best:
        return best[0], 1, best[1], f"Freight not quoted: imputed INR {best[0]:.2f}/pc, the highest freight quoted by another vendor on this line"
    ly = _ly_row(conn, q["vendor_id"], vendor_sku)
    if ly and ly["freight_inr_pc"] is not None:
        return float(ly["freight_inr_pc"]), 2, None, f"Freight not quoted and no other vendor quoted it: last year's INR {ly['freight_inr_pc']:.2f}/pc used"
    return None, 0, None, "Freight not quoted, no other vendor quoted it and there is no last-year figure: line excluded (freight_unknown); vendor to be asked"


def _blank_n() -> dict[str, Any]:
    cols = ["conversion_method", "pieces_per_raw_unit", "weight_used_kg", "weight_source", "ref_lyp_id", "fx_rate",
            "fx_rate_date", "fx_source", "payment_rate_pct", "std_terms_days_used", "effective_vendor_days",
            "tier_applied", "tier_qty_used", "discount_applied_pct", "base_inr_pc", "freight_inr_pc",
            "freight_imputed_from_quote_id", "landed_inr_pc", "onetime_inr_pc", "payment_loading_inr_pc",
            "evaluated_inr_pc", "calc_snapshot_json", "normalization_trace_json", "validation_json",
            "min_confidence"]
    out: dict[str, Any] = {c: None for c in cols}
    out.update(freight_imputed=0, award_eligible=0)
    return out


def _write(conn: sqlite3.Connection, quote_id: int, vals: dict[str, Any]) -> None:
    vals = dict(vals, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    sets = ", ".join(f"{k}=?" for k in vals)
    conn.execute(f"UPDATE quotes SET {sets} WHERE quote_id=?", (*vals.values(), quote_id))


def _recompute(conn: sqlite3.Connection, quote_id: int, cfg: dict, cascade: bool) -> None:
    row = conn.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if row is None:
        raise KeyError(f"quote {quote_id} not found")
    q = effective_quote(row)
    meta = _j(row["field_meta_json"]) or {}
    out = _blank_n()

    def finish(extra: dict[str, Any]) -> None:
        keep = {k: meta[k] for k in ("extraction", "overrides", "confirmed", "rejected") if k in meta}
        keep.update(extra.pop("_meta", {}))
        out.update(extra)
        out["field_meta_json"] = _dump(keep)
        _write(conn, quote_id, out)
        if cascade and row["line_id"] is not None:
            for p in conn.execute("SELECT quote_id FROM quotes WHERE line_id=? AND quote_id<>? AND is_current=1",
                                  (row["line_id"], quote_id)).fetchall():
                _recompute(conn, p["quote_id"], cfg, cascade=False)

    # ---- not a priced quote: never ranked, never zero
    if q["quote_status"] != "quoted" or q["line_id"] is None:
        reason = "unmatched" if q["quote_status"] == "unmatched" or q["line_id"] is None else q["quote_status"]
        finish({"ineligible_reasons_json": _dump([reason]),
                "review_state": "must_confirm" if reason == "unmatched" else "auto_ok",
                "_meta": {"fields": {}, "flags": []}})
        return

    line = conn.execute("SELECT * FROM line_items WHERE line_id=?", (q["line_id"],)).fetchone()
    vendor = conn.execute("SELECT * FROM vendors WHERE vendor_id=?", (q["vendor_id"],)).fetchone()
    resp = conn.execute("SELECT * FROM vendor_responses WHERE response_id=?", (q["response_id"],)).fetchone()
    rfq = conn.execute("SELECT * FROM rfqs WHERE rfq_id=?", (line["rfq_id"],)).fetchone()
    flags: list[str] = list(dict.fromkeys((meta.get("extraction") or {}).get("flags", [])))
    trace: list[str] = []
    unit, currency = q["raw_unit"], (q["raw_currency"] or "INR").upper()
    base_list: float | None = None
    is_ly_ref = False

    # ---- price -> INR per piece
    if unit == "as_last_year":
        is_ly_ref = True
        lyp = _ly_row(conn, q["vendor_id"], line["catalog_sku"])
        flags.append("IMPLIED_BY_BLANKET")
        if lyp is None:
            flags.append("LY_REF_MISSING")
            trace.append('Vendor said "same as last year" but there is no last-year price for this vendor and SKU')
        else:
            base_list = float(lyp["price_inr_pc"])
            out.update(conversion_method="last_year_ref", pieces_per_raw_unit=1, ref_lyp_id=lyp["lyp_id"])
            trace.append(f'Vendor said "same as last year": {lyp["period"]} price INR {base_list:.2f}/pc (ex-freight)')
            if ly_spec_changed(line, lyp):
                flags.append("LY_SPEC_CHANGED")
                trace.append("Last year's spec differs from this RFQ's spec (ply/flute/GSM): the old price may not apply")
    elif q["raw_price"] is None:
        trace.append("No price extracted for this line")
    else:
        price, tinfo = _apply_tier(q, line, float(q["raw_price"]), trace)
        flags += tinfo["flags"]
        out.update(tier_applied=tinfo["tier_applied"], tier_qty_used=tinfo["tier_qty_used"])
        raw = {"price": price, "currency": currency, "unit": unit}
        if currency == "USD":
            raw["fx_rate"] = cfg["fx"]["USD_INR"]
            out.update(fx_rate=cfg["fx"]["USD_INR"], fx_rate_date=cfg["fx"]["date"], fx_source=cfg["fx"]["source"])
            flags.append("FX_CONVERTED")
        base_list, method, ctrace = convert(raw, dict(line))
        trace += ctrace
        out.update(conversion_method=method)
        if unit == "per_kg":
            flags.append("DERIVED_PER_KG")
            out.update(weight_used_kg=line["theoretical_weight_kg"], weight_source="theoretical",
                       pieces_per_raw_unit=None)
            stated = q.get("vendor_stated_weight_kg")
            if stated and line["theoretical_weight_kg"] and \
                    abs(stated - line["theoretical_weight_kg"]) / line["theoretical_weight_kg"] * 100 > cfg["weight_mismatch_pct"]:
                flags.append("WEIGHT_MISMATCH")
                trace.append(f"Vendor-stated box weight {stated:g} kg differs from theoretical {line['theoretical_weight_kg']:g} kg by more than {cfg['weight_mismatch_pct']:g}%")
        else:
            out.update(pieces_per_raw_unit=PIECES_PER_UNIT.get(unit))
        if q.get("tax_basis") == "incl_gst":
            g = q.get("gst_rate_pct")
            if g:
                base_list /= 1 + g / 100
                trace.append(f"Price included GST: divided by {1 + g / 100:.2f} to compare ex-GST")

    # ---- D2: conditional discounts only when the buyer has toggled them on
    base = base_list
    discounts = _j(q.get("discounts_json")) or []
    applied_pct = sum(d.get("pct", 0) for d in discounts if d.get("applied"))
    if base is not None:
        if applied_pct:
            base = base_list * (1 - applied_pct / 100)
            trace.append(f"Buyer-applied conditional discount(s) {applied_pct:g}%: INR {base_list:.4f} -> INR {base:.4f}")
        if any(not d.get("applied") for d in discounts):
            flags.append("CONDITIONAL_DISCOUNT_AVAILABLE")
            trace.append("Conditional discount(s) offered but NOT applied (buyer has not toggled them on)")
    out.update(discount_applied_pct=applied_pct if discounts else None)

    # ---- D4: freight
    freight: float | None = None
    fb = q.get("freight_basis")
    if base is not None:
        if fb == "included":
            freight = 0.0
            trace.append("Freight included in the rate (FOR Chakan): INR 0.00 added")
        elif fb == "extra_quoted" and q.get("raw_freight_value") is not None:
            freight = float(q["raw_freight_value"])
            trace.append(f"Freight quoted separately: INR {freight:.2f}/pc added")
        else:
            freight, how, src_qid, msg = _imputed_freight(conn, q, line["catalog_sku"])
            trace.append(msg)
            out.update(freight_imputed=how, freight_imputed_from_quote_id=src_qid)
            flags.append("FREIGHT_IMPUTED" if freight is not None else "FREIGHT_UNKNOWN")

    landed = base + freight if base is not None and freight is not None else None

    # ---- one-time charges
    onetime = None
    charges = _j(q.get("charges_json")) or []
    if charges and line["annual_qty"]:
        total = sum(c["amount"] * (c.get("colours") or 1) for c in charges if c.get("basis", "one_time") == "one_time")
        onetime = total / line["annual_qty"]
        trace.append(f"One-time charges INR {total:,.0f} / annual qty {line['annual_qty']:,} = INR {onetime:.4f}/pc")
    elif landed is not None:
        onetime = 0.0

    # ---- D3 MSME cap, D5 payment loading
    std = rfq["std_payment_terms_days"]
    terms = q.get("payment_terms_days")
    eff = std if terms is None else terms
    if terms is not None and vendor["msme_category"] in MSME_CAPPED and terms > cfg["msme_max_credit_days"]:
        eff = cfg["msme_max_credit_days"]
        flags.append("MSME_TERMS_CAPPED")
        trace.append(f"{vendor['msme_category']} enterprise: quoted {terms} days capped at {eff} (MSMED Act s.15)")
    declared = resp["declared_msme_category"]
    if declared and declared != vendor["msme_category"]:
        flags.append("MSME_CATEGORY_MISMATCH")
        trace.append(f"Reply declares MSME category '{declared}' but vendor master says '{vendor['msme_category']}': master used")
    cof = cfg["cost_of_funds_pct"] / 100
    loading = None
    if landed is not None:
        loading = landed * cof * (std - min(eff, std + cfg["payment_loading_max_extra_days"])) / 365
        trace.append(f"Payment loading: {landed:.4f} x {cof:.3%} x ({std} - min({eff}, {std + cfg['payment_loading_max_extra_days']})) / 365 = INR {loading:.4f}")
    evaluated = landed + (onetime or 0) + loading if landed is not None else None
    if evaluated is not None:
        trace.append(f"Landed INR {landed:.4f} (quoted) -> evaluated INR {evaluated:.4f} (ranking price)")

    # ---- validation layers 1-4
    peers = [r["base_inr_pc"] for r in conn.execute(
        "SELECT base_inr_pc FROM quotes WHERE line_id=? AND quote_id<>? AND vendor_id<>? AND is_current=1 "
        "AND quote_status='quoted' AND base_inr_pc IS NOT NULL", (q["line_id"], quote_id, q["vendor_id"]))]
    sr_all = _j(resp["second_read_json"]) or {}
    checks, vflags = validate.run_checks({
        "q": q, "raw_q": dict(row), "line": line, "resp": resp, "rfq": rfq, "cfg": cfg, "base_list": base_list,
        "ly_ref": _ly_reference_price(conn, q["vendor_id"], line["catalog_sku"]), "is_ly_ref": is_ly_ref,
        "peer_bases": peers, "second_read": sr_all.get(str(quote_id)),
    })
    flags = list(dict.fromkeys(flags + vflags))

    # ---- confidence & routing per field
    src = resp["source_type"] or "email_text"
    present = ["price", "unit", "currency", "freight", "tax_basis"]
    for fld, col in (("lead_time", "lead_time_days"), ("moq", "moq_qty"), ("payment_terms", "payment_terms_days"),
                     ("gst_rate", "gst_rate_pct")):
        if q.get(col) is not None:
            present.append(fld)
    if q.get("spec_match") is not None:
        present.append("spec_match")
    confirmed = meta.get("confirmed") or {}
    ex_src = (meta.get("extraction") or {}).get("source") or {}
    fields: dict[str, dict] = {}
    for fld in present:
        fields[fld] = conf_mod.score_field(fld, src, checks, flags, cfg, confirmed=fld in confirmed)
        if fld in ex_src:
            fields[fld]["source"] = ex_src[fld]
    # deviations/substitutes the buyer accepted count as a confirmed spec decision
    if q.get("spec_accepted") and "spec_match" in fields:
        fields["spec_match"].update(status="confirmed", confidence=1.0)
    min_conf, review_state = conf_mod.rollup(fields)
    if meta.get("rejected"):
        review_state = "rejected"

    # ---- eligibility (quote level; the vendor-qualification gate is added in the view)
    reasons: list[str] = []
    if meta.get("rejected"):
        reasons.append("rejected")
    if "FREIGHT_UNKNOWN" in flags:
        reasons.append("freight_unknown")
    if landed is None and "FREIGHT_UNKNOWN" not in flags:
        reasons.append("no_price")
    for fld, f in fields.items():
        if f["status"] == "must_confirm":
            reasons.append(f"open_must_confirm:{fld}")
    if q.get("spec_match") == "substitute" and not q.get("spec_accepted"):
        reasons.append("spec_substitute")
    if q.get("spec_match") == "deviation" and not q.get("spec_accepted"):
        reasons.append("spec_deviation")
    if "PARTIAL_QTY" in flags:
        reasons.append("partial_qty")
    if "VALIDITY_EXPIRED" in flags:
        reasons.append("validity_expired")
    if "PRICE_NOT_FIRM" in flags and cfg.get("eligibility", {}).get("block_price_not_firm"):
        reasons.append("price_not_firm")

    snapshot = {"cost_of_funds_pct": cfg["cost_of_funds_pct"], "std_terms_days": std,
                "max_extra_days": cfg["payment_loading_max_extra_days"], "msme_cap_days": cfg["msme_max_credit_days"],
                "sanity_band_pct": cfg["sanity_band_pct"], "thresholds": cfg["thresholds"],
                "weight_formula": cfg["weight_formula"], "fx": cfg["fx"]}
    r4 = lambda x: None if x is None else round(x, 4)  # noqa: E731  money to 4 dp (SCHEMA A3)
    finish({
        "base_inr_pc": r4(base), "freight_inr_pc": r4(freight), "landed_inr_pc": r4(landed),
        "onetime_inr_pc": r4(onetime), "payment_loading_inr_pc": r4(loading), "evaluated_inr_pc": r4(evaluated),
        "payment_rate_pct": cfg["cost_of_funds_pct"], "std_terms_days_used": std,
        "effective_vendor_days": eff, "calc_snapshot_json": _dump(snapshot),
        "normalization_trace_json": _dump(trace), "validation_json": _dump(checks),
        "min_confidence": min_conf, "review_state": review_state,
        "award_eligible": 0 if reasons else 1, "ineligible_reasons_json": _dump(reasons),
        "_meta": {"fields": fields, "flags": flags},
    })


def recompute_quote(conn: sqlite3.Connection, quote_id: int, cascade: bool = True,
                    cfg: dict | None = None) -> None:
    """Recompute one quote. With cascade, the other vendors' quotes on the same line are refreshed too
    (freight imputation and the peer-median sanity check read them)."""
    _recompute(conn, quote_id, cfg or load_config(), cascade)
    conn.commit()


def recompute_all(conn: sqlite3.Connection, cfg: dict | None = None) -> int:
    """Recompute every current quote. Two passes so peer-dependent checks see settled neighbours."""
    cfg = cfg or load_config()
    ids = [r["quote_id"] for r in conn.execute("SELECT quote_id FROM quotes WHERE is_current=1 ORDER BY quote_id")]
    for _ in range(2):
        for qid in ids:
            _recompute(conn, qid, cfg, cascade=False)
    conn.commit()
    return len(ids)
