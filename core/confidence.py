"""Signal-based confidence and per-field routing (CLAUDE.md steps 7-8).

Confidence is built from the source type (config base score), raised by validation checks that
passed, and capped by failed checks or must-confirm flags. The LLM's self-reported confidence is
never used.
"""
from __future__ import annotations

from typing import Any

CORE_FIELDS = ("price", "unit", "currency", "freight", "tax_basis")
TERMS_FIELDS = ("lead_time", "moq", "payment_terms")
EXTRA_FIELDS = ("gst_rate", "spec_match")  # routed with the core thresholds
ALL_FIELDS = CORE_FIELDS + TERMS_FIELDS + EXTRA_FIELDS

# flag -> [(field, floor)]; floor 'must_confirm' or 'spot_check' (the worst status the flag allows)
FLAG_RULES: dict[str, list[tuple[str, str]]] = {
    "DERIVED_PER_KG": [("price", "must_confirm")],
    "IMPLIED_BY_BLANKET": [("price", "must_confirm")],
    "LY_SPEC_CHANGED": [("price", "must_confirm")],
    "LY_REF_MISSING": [("price", "must_confirm")],
    "SANITY_RANGE_FAIL": [("price", "must_confirm")],
    "UNIT_AMBIGUOUS": [("unit", "must_confirm")],
    "SECOND_READ_MISMATCH": [("price", "must_confirm")],
    "ARITHMETIC_FAIL": [("price", "must_confirm")],
    "LOW_CONFIDENCE_OCR": [("price", "must_confirm")],
    "WEIGHT_MISMATCH": [("price", "spot_check")],
    "TIER_BASIS_UNCLEAR": [("price", "spot_check")],
    "PRICE_NOT_FIRM": [("price", "spot_check")],
    "FREIGHT_IMPUTED": [("freight", "spot_check")],
    "FREIGHT_UNKNOWN": [("freight", "must_confirm")],
    "TAX_BASIS_UNSPECIFIED": [("tax_basis", "must_confirm")],
    "TAX_BASIS_INCL_GST": [("tax_basis", "must_confirm")],
    "GST_RATE_MISMATCH": [("gst_rate", "spot_check")],
    "MSME_TERMS_CAPPED": [("payment_terms", "spot_check")],
    "MSME_CATEGORY_MISMATCH": [("payment_terms", "spot_check")],
    "SPEC_DEVIATION": [("spec_match", "must_confirm")],
    "SPEC_SUBSTITUTE": [("spec_match", "must_confirm")],
    "PARTIAL_QTY": [("moq", "spot_check")],
}

# validation check name -> fields whose confidence it supports
CHECK_FIELDS: dict[str, tuple[str, ...]] = {
    "qty_x_price": ("price", "unit"),
    "vendor_line_matched": ("price", "spec_match"),
    "unit_known": ("unit",),
    "currency_known": ("currency",),
    "freight_basis_stated": ("freight",),
    "spec_exact": ("spec_match",),
    "gst_rate_expected": ("gst_rate",),
    "tax_basis_stated": ("tax_basis",),
    "tax_basis_consistent": ("tax_basis", "gst_rate"),
    "sanity_vs_last_year": ("price", "unit"),
    "sanity_vs_peers": ("price", "unit"),
    "unit_hypothesis": ("unit",),
    "second_read": ("price", "unit", "currency", "freight"),
}

_RANK = {"auto_ok": 0, "confirmed": 0, "spot_check": 1, "must_confirm": 2}


def thresholds_for(field: str, cfg: dict) -> tuple[float, float]:
    grp = "terms" if field in TERMS_FIELDS else "core"
    t = cfg["thresholds"][grp]
    return t["auto"], t["spot"]


def route(field: str, conf: float, cfg: dict) -> str:
    auto, spot = thresholds_for(field, cfg)
    if conf >= auto - 1e-9:
        return "auto_ok"
    if conf >= spot - 1e-9:
        return "spot_check"
    return "must_confirm"


def score_field(field: str, source_type: str, checks: list[dict], flags: list[str],
                cfg: dict, confirmed: bool = False) -> dict[str, Any]:
    """Confidence, status and the signals behind them for one field."""
    auto, spot = thresholds_for(field, cfg)
    base = cfg["confidence_base"][source_type]
    conf = base
    signals = [f"base {base:.2f} from source type '{source_type}'"]
    bonus = cfg.get("confidence", {}).get("check_bonus", 0.04)
    cap = cfg.get("confidence", {}).get("cap", 0.99)

    failed_check = False
    if field in CORE_FIELDS or field in EXTRA_FIELDS:
        for c in checks:
            if field not in CHECK_FIELDS.get(c["check"], ()):
                continue
            if c["result"] == "pass":
                conf += bonus
                signals.append(f"+{bonus:.2f} {c['check']} passed")
            elif c["result"] == "fail":
                failed_check = True
                signals.append(f"check {c['check']} FAILED")
    conf = min(conf, cap)

    field_flags: list[str] = []
    floor = "auto_ok"
    for fl in flags:
        for f, fl_floor in FLAG_RULES.get(fl, []):
            if f == field:
                field_flags.append(fl)
                if _RANK[fl_floor] > _RANK[floor]:
                    floor = fl_floor
                signals.append(f"flag {fl} -> {fl_floor}")
    if failed_check or floor == "must_confirm":
        conf = min(conf, spot - 0.01)
    elif floor == "spot_check":
        conf = min(conf, auto - 0.01)

    conf = round(conf, 4)
    status = route(field, conf, cfg)
    if confirmed:
        status, conf = "confirmed", 1.0
        signals.append("buyer confirmed")
    return {"confidence": conf, "status": status, "flags": field_flags, "signals": signals}


def rollup(fields: dict[str, dict]) -> tuple[float | None, str]:
    """(min_confidence over open fields, review_state)."""
    if not fields:
        return None, "auto_ok"
    open_conf = [f["confidence"] for f in fields.values() if f["status"] != "confirmed"]
    worst = max((_RANK[f["status"]] for f in fields.values()), default=0)
    if worst == 2:
        state = "must_confirm"
    elif worst == 1:
        state = "spot_check"
    elif any(f["status"] == "confirmed" for f in fields.values()):
        state = "confirmed"
    else:
        state = "auto_ok"
    return (min(open_conf) if open_conf else 1.0), state
