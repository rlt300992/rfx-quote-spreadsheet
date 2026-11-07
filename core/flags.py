"""Plain-language explanations of flags, exclusion reasons and fields, shared by every screen.

Every flag the pipeline can raise must have an entry here (a test enforces it), so nothing on screen is an
unexplained code.
"""
from __future__ import annotations

FLAG_HELP: dict[str, str] = {
    "DERIVED_PER_KG": "Vendor quoted per kg. The per-piece price is rate x our theoretical box weight (formula shown in 'How computed'). Confirm the weight basis.",
    "IMPLIED_BY_BLANKET": "Vendor said 'same as last year' for this line. The price is taken from last year's price list, not quoted for this RFQ.",
    "LY_SPEC_CHANGED": "Last year's specification differs from this RFQ's (ply, flute or GSM), so last year's price may not apply.",
    "LY_REF_MISSING": "'Same as last year' but there is no last-year price for this vendor and SKU. No price is invented.",
    "SANITY_RANGE_FAIL": "The price is more than 25% away from last year's price and/or the other vendors' median for this line.",
    "UNIT_AMBIGUOUS": "The value looks like a per-100 (or per-1000) rate although the column says per box/piece. Confirm the unit.",
    "SECOND_READ_MISMATCH": "An independent second reading of the document disagrees with the first on price, unit, currency or freight.",
    "ARITHMETIC_FAIL": "The vendor's own line total does not equal quantity x price.",
    "LOW_CONFIDENCE_OCR": "Part of this value is smudged or hard to read in the photo/scan. Check the evidence image.",
    "WEIGHT_MISMATCH": "The vendor's stated box weight differs from our theoretical weight by more than 5%.",
    "TIER_BASIS_UNCLEAR": "A volume discount/tier does not say whether its quantity is per order or per year. Treated as per order (the higher price).",
    "TIER_NOT_MET_PER_ORDER": "A volume discount needs a larger quantity per order than our typical order, so it is not applied.",
    "PRICE_NOT_FIRM": "Prices are linked to raw-material (kraft paper) costs and can move. Still comparable, but not firm.",
    "FREIGHT_IMPUTED": "Vendor did not quote freight. The highest freight quoted by another vendor on this line (else last year's) is used.",
    "FREIGHT_UNKNOWN": "No freight quoted by anyone and no last-year figure. The line is excluded until freight is confirmed. Nothing is invented.",
    "TAX_BASIS_UNSPECIFIED": "The reply does not say whether prices include GST.",
    "TAX_BASIS_INCL_GST": "Prices include GST; GST was backed out for comparison.",
    "GST_RATE_MISMATCH": "GST rate quoted differs from the expected rate. GST is stored but never added to the comparison price.",
    "MSME_TERMS_CAPPED": "Micro/small vendor quoted credit above 45 days; evaluation caps it at 45 (MSMED Act s.15).",
    "MSME_CATEGORY_MISMATCH": "The MSME category in the reply differs from the vendor master. The master value is used until confirmed.",
    "SPEC_DEVIATION": "The vendor offers different layer GSMs than specified. Needs QA acceptance before it can win.",
    "SPEC_SUBSTITUTE": "The vendor offers a different construction (for example 3-ply for 5-ply). Needs QA acceptance before it can win.",
    "PARTIAL_QTY": "Vendor capacity is below our annual requirement for this line, so it cannot take the whole line.",
    "FX_CONVERTED": "Priced in USD; converted at the fixed, dated rate in config.",
    "CONDITIONAL_DISCOUNT_AVAILABLE": "A conditional discount is offered (for example for fast payment). It is never applied automatically; toggle it to compare.",
    "VALIDITY_EXPIRED": "The offer's validity ended before the award reference date.",
}

REASON_HELP: dict[str, str] = {
    "not_quoted": "The vendor did not quote this line. A missing line is never treated as zero.",
    "no_bid": "The vendor declined to bid on this line.",
    "unmatched": "This vendor line could not be matched to any of our lines. Match it or ignore it.",
    "vendor_disqualified": "The vendor failed a knock-out question (for example an expired ISO certificate).",
    "vendor_pending_qualification": "A knock-out answer is missing, unknown or conditional, so the vendor is pending, not qualified.",
    "freight_unknown": "Freight is unknown (see FREIGHT_UNKNOWN). The line is excluded until it is confirmed.",
    "spec_substitute": "Substitute construction not yet accepted by QA.",
    "spec_deviation": "Spec deviation not yet accepted by QA.",
    "partial_qty": "Vendor capacity is below the line's annual quantity.",
    "validity_expired": "The offer had expired on the award reference date.",
    "price_not_firm": "Price is not firm (blocking is switched on in config).",
    "rejected": "The buyer rejected this quote.",
    "no_price": "No usable price for this line.",
}

FIELD_LABELS = {"price": "Price", "unit": "Unit", "currency": "Currency", "freight": "Freight", "tax_basis": "Tax basis",
                "lead_time": "Lead time", "moq": "MOQ", "payment_terms": "Payment terms", "gst_rate": "GST rate",
                "spec_match": "Spec match"}


def explain_reason(reason: str | None) -> str:
    """Human sentence for an exclusion_reason / ineligible reason code."""
    if not isinstance(reason, str) or not reason:  # pandas hands NaN (a float) for "no reason"
        return ""
    if reason.startswith("open_must_confirm:"):
        f = reason.split(":", 1)[1]
        return f"{FIELD_LABELS.get(f, f)} is still a must-confirm value. It is excluded from the award until you confirm or correct it."
    return REASON_HELP.get(reason, reason)


def confidence_chip(conf: float | None, status: str | None = None) -> str:
    """Markdown colour chip: green >= 95%, amber 80-95%, red < 80% (core thresholds)."""
    if conf is None:
        return ":gray[n/a]"
    colour = "green" if conf >= 0.95 - 1e-9 else "orange" if conf >= 0.80 - 1e-9 else "red"
    label = f"{conf:.0%}"
    if status == "confirmed":
        return ":blue[confirmed]"
    return f":{colour}[{label}]"
