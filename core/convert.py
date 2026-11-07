"""Unit/currency conversion to INR per piece, and the theoretical box-weight formula.

convert() is pure: no DB, no I/O. recompute_quote() is its only caller in the pipeline.
Adding a category = adding one converter to _CONVERTERS (SCHEMA §5).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from core.config import load_config

# Pieces covered by one priced unit. per_kg is handled via weight, not a piece count.
PIECES_PER_UNIT: dict[str, float] = {
    "per_piece": 1, "per_box": 1, "per_100": 100, "per_1000": 1000,
}
FX_CURRENCIES = {"INR", "USD"}


def theoretical_weight(line: dict, cfg: dict | None = None) -> tuple[str, float, str]:
    """(size_class, weight_kg rounded to 3 dp, trace). Never done by Gemini (SCHEMA line_items)."""
    cfg = cfg or load_config()
    wf = cfg["weight_formula"]
    L, W, H = float(line["length_mm"]), float(line["width_mm"]), float(line["height_mm"])
    size_class = "large" if L + W > wf["large_cutoff_L_plus_W_mm"] else "normal"
    margin = wf["sheet_margin_mm"][size_class]
    sheet_len = (L + W) * 2 + margin
    sheet_wid = W + H + wf["sheet_width_allowance_mm"]
    area_m2 = sheet_len * sheet_wid / 1_000_000
    layers = json.loads(line["layers_json"]) if isinstance(line["layers_json"], str) else line["layers_json"]
    total_gsm = 0.0
    for lay in layers:
        gsm = float(lay["gsm"])
        if lay.get("role") == "flute":
            gsm *= wf["flute_factors"][lay["flute"]]
        total_gsm += gsm
    weight = area_m2 * total_gsm / 1000 * wf["wastage_factor"]
    trace = (
        f"Sheet = ({L:g}+{W:g})x2+{margin} = {sheet_len:g} mm by {W:g}+{H:g}+{wf['sheet_width_allowance_mm']} = "
        f"{sheet_wid:g} mm; area {area_m2:.4f} m2; total GSM {total_gsm:.1f}; "
        f"weight = {area_m2:.4f} x {total_gsm:.1f} / 1000 x {wf['wastage_factor']} = {weight:.3f} kg"
    )
    return size_class, round(weight, 3), trace


def _corrugated(raw: dict[str, Any], line_spec: dict, cfg: dict) -> tuple[float, str, list[str]]:
    price, unit = raw.get("price"), raw.get("unit")
    currency = (raw.get("currency") or "INR").upper()
    if price is None:
        raise ValueError("convert(): no price to convert")
    if currency not in FX_CURRENCIES:
        raise ValueError(f"convert(): unsupported currency {currency!r}")
    trace = [f"Vendor rate: {currency} {price:g} {unit}"]

    amount = float(price)
    if currency == "USD":
        fx = float(raw.get("fx_rate") or cfg["fx"]["USD_INR"])
        amount *= fx
        trace.append(f"USD to INR at fixed rate {fx:g} ({cfg['fx']['date']}): INR {amount:.4f} per {unit}")

    if unit in PIECES_PER_UNIT:
        n = PIECES_PER_UNIT[unit]
        if n == 1:
            trace.append(f"Per piece: INR {amount:.4f}/pc (no unit conversion)")
            return amount, "direct", trace
        out = amount / n
        trace.append(f"{unit}: INR {amount:.4f} / {n:g} = INR {out:.4f}/pc")
        return out, "per_n_pieces", trace

    if unit == "per_kg":
        weight = raw.get("weight_kg") or line_spec.get("theoretical_weight_kg")
        if not weight:
            raise ValueError("convert(): per_kg needs a theoretical weight on the line")
        out = amount * float(weight)
        src = "theoretical weight" if not raw.get("weight_kg") else "vendor-stated weight"
        trace.append(f"per_kg: INR {amount:.4f}/kg x {float(weight):g} kg ({src}) = INR {out:.4f}/pc")
        if line_spec.get("weight_calc_trace") and not raw.get("weight_kg"):
            trace.append(f"Weight formula: {line_spec['weight_calc_trace']}")
        return out, "per_kg_formula", trace

    raise ValueError(f"convert(): cannot convert unit {unit!r} (as_last_year is resolved by recompute_quote)")


_CONVERTERS: dict[str, Callable[[dict, dict, dict], tuple[float, str, list[str]]]] = {
    "corrugated": _corrugated,
}


def convert(raw: dict[str, Any], line_spec: dict, category: str = "corrugated",
            cfg: dict | None = None) -> tuple[float, str, list[str]]:
    """raw = {price, currency, unit, fx_rate?, weight_kg?} -> (inr_per_unit, method, trace)."""
    if category not in _CONVERTERS:
        raise NotImplementedError(f"No converter for category {category!r}")
    return _CONVERTERS[category](raw, line_spec, cfg or load_config())
