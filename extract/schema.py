"""Pydantic response schemas for Gemini structured output (quote, second read, document).

Every extracted value carries a source reference (cell, page+row, paragraph, photo row) and a short
verbatim snippet, so the Review screen can show the evidence next to the number.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Unit = Literal["per_piece", "per_box", "per_100", "per_1000", "per_kg", "as_last_year"]
FreightBasis = Literal["included", "extra_quoted", "extra_unquoted"]
TaxBasis = Literal["ex_gst", "incl_gst", "unspecified"]


class Src(BaseModel):
    ref: str = Field(description="Where the value is: cell address like 'Price Offer!E12', 'page 1 row 5', 'paragraph 6', 'photo row 14'")
    snippet: str = Field(description="Short verbatim text from the source that contains the value")


class Tier(BaseModel):
    min_qty: int
    price: float = Field(description="Price at this tier, in the SAME unit and currency as the line price")
    qty_basis: Literal["per_order", "annual", "unclear"] = Field(
        description="per_order if the vendor says per order/release/lot; annual if per year/annum; unclear if not stated")
    condition_text: str
    source: Src


class Discount(BaseModel):
    pct: float
    condition_type: Literal["payment_days_max", "volume_qty", "other"]
    condition_value: Optional[float] = Field(default=None, description="e.g. 15 (days) or 50000 (qty)")
    condition_text: str = Field(description="The condition verbatim, e.g. '2% cash discount if paid within 15 days'")
    qty_basis: Optional[Literal["per_order", "annual", "unclear"]] = Field(
        default=None, description="ONLY for condition_type volume_qty: per_order if the quantity is per order / release / lot / delivery; annual if per year; unclear if the vendor does not say")
    scope: Literal["all_lines", "this_line"] = "all_lines"
    source: Src


class Charge(BaseModel):
    type: Literal["plate", "die", "print", "other"]
    amount: float = Field(description="INR amount")
    basis: Literal["one_time", "per_unit"] = "one_time"
    per_colour: bool = Field(default=False, description="True if the amount is per print colour")
    applies_to: Literal["all_lines", "printed_items", "this_line"] = "all_lines"
    text: str
    source: Src


class FreightRule(BaseModel):
    scope_ply: Optional[int] = Field(default=None, description="3, 5 or 7 when the freight depends on ply; null = all boxes")
    basis: FreightBasis
    value_inr_per_piece: Optional[float] = None
    text: str
    source: Src


class BlanketRule(BaseModel):
    """A price rule that covers many lines at once, e.g. '42/kg for the 5-ply, rest same as last year'."""
    scope_ply: Optional[int] = Field(default=None, description="3, 5 or 7; null with scope_rest=true means 'all other lines'")
    scope_rest: bool = Field(default=False, description="True for 'the rest' / 'all other items'")
    price: Optional[float] = None
    unit: Unit
    currency: Literal["INR", "USD"] = "INR"
    text: str
    source: Src


class Line(BaseModel):
    line_key: str = Field(description="The vendor's own row number or item code if the document has one; otherwise the box size exactly as printed")
    vendor_item_text: str = Field(description="The vendor's description of the item, verbatim")
    length_mm: Optional[int] = None
    width_mm: Optional[int] = None
    height_mm: Optional[int] = None
    dim_basis: Literal["ID", "OD", "unknown"] = Field(default="unknown", description="Inside or outside dimensions, if stated")
    ply: Optional[int] = None
    construction_text: Optional[str] = Field(default=None, description="e.g. '5 Ply 180/120/150/120/180'")
    vendor_layers_gsm: Optional[list[int]] = Field(
        default=None, description="GSM of each layer as OFFERED, outermost first. Apply any remark that changes a layer (e.g. 'offered in 150 GSM outer liner'). null if not stated")
    raw_price_text: str = Field(description="The price exactly as written, e.g. 'USD 0.0866/pc', '887', 'Rs 10945 / 100 Nos'")
    price: Optional[float] = Field(default=None, description="Numeric price as written. null if there is no number for this line")
    unit: Unit = Field(description="The unit AS THE DOCUMENT STATES IT (column header or label). Do not reinterpret it")
    currency: Literal["INR", "USD"] = "INR"
    tiers: list[Tier] = Field(default_factory=list)
    discounts: list[Discount] = Field(default_factory=list, description="Discounts that apply to THIS line only (e.g. from its remarks)")
    freight_basis: Optional[FreightBasis] = Field(default=None, description="Only if freight is stated for this specific line; otherwise null")
    freight_value_inr_per_piece: Optional[float] = None
    capacity_per_month: Optional[int] = Field(default=None, description="Pieces per month the vendor can supply for THIS line, if limited")
    capacity_text: Optional[str] = None
    remarks: Optional[str] = Field(default=None, description="Any vendor remark on this line, verbatim")
    ocr_uncertain: bool = Field(default=False, description="True if any digit of the price/size is smudged, blurred, occluded or hard to read")
    source: Src


class Answer(BaseModel):
    q_code: str = Field(description="The question code given in the prompt, e.g. Q01_ISO9001")
    answer_text: str = Field(description="The vendor's answer verbatim")
    answer_value: str = Field(description="Normalised: yes / no / conditional for yes-no questions; a bare number for numeric ones; virgin / recycled / mixed for paper source; otherwise short text")
    document_attached: bool = Field(default=False, description="True if the vendor says a document for this question is attached/enclosed")
    source: Src


class HeaderSource(BaseModel):
    field: str
    source: Src


class Header(BaseModel):
    vendor_name: Optional[str] = None
    quote_ref_no: Optional[str] = None
    quote_date: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD")
    validity_text: Optional[str] = None
    validity_date: Optional[str] = Field(default=None, description="ISO date until which the offer is valid, computed from the text; null if not computable")
    price_basis: Literal["firm", "rm_linked", "unspecified"] = Field(
        description="rm_linked if prices follow a raw-material/kraft index or are passed on at actuals; firm if stated firm; else unspecified")
    rm_clause_text: Optional[str] = Field(default=None, description="Verbatim price-variation / raw-material clause")
    gst_rate_pct: Optional[float] = None
    tax_basis: TaxBasis = "unspecified"
    payment_terms_days: Optional[int] = Field(default=None, description="Days of credit from invoice as QUOTED (not capped)")
    payment_terms_text: Optional[str] = None
    lead_time_days: Optional[int] = None
    moq_qty: Optional[int] = None
    moq_text: Optional[str] = None
    declared_msme_category: Optional[Literal["micro", "small", "medium"]] = Field(
        default=None, description="Only if the document itself states the vendor's Udyam/MSME category")
    freight_rules: list[FreightRule] = Field(default_factory=list)
    discounts: list[Discount] = Field(default_factory=list, description="Discounts that apply to the whole offer (footnotes, terms)")
    charges: list[Charge] = Field(default_factory=list)
    blanket_rules: list[BlanketRule] = Field(default_factory=list)
    attached_documents: list[str] = Field(default_factory=list, description="Attachments the vendor mentions, e.g. 'ISO 9001 certificate'")
    sources: list[HeaderSource] = Field(default_factory=list, description="A source for each header value you filled in")
    notes: Optional[str] = None


class QuoteExtraction(BaseModel):
    header: Header
    lines: list[Line]
    answers: list[Answer] = Field(default_factory=list)


# ---------------------------------------------------------------- second read
class SRLine(BaseModel):
    line_key: str
    price: Optional[float] = None
    unit: Unit
    currency: Literal["INR", "USD"] = "INR"


class SRBlanket(BaseModel):
    scope_ply: Optional[int] = None
    scope_rest: bool = False
    price: Optional[float] = None
    unit: Unit
    currency: Literal["INR", "USD"] = "INR"


class SRFreight(BaseModel):
    scope_ply: Optional[int] = None
    basis: FreightBasis
    value_inr_per_piece: Optional[float] = None


class SRAnswer(BaseModel):
    q_code: str
    answer_value: str


class SecondRead(BaseModel):
    lines: list[SRLine] = Field(default_factory=list)
    blanket_rules: list[SRBlanket] = Field(default_factory=list)
    freight_rules: list[SRFreight] = Field(default_factory=list)
    answers: list[SRAnswer] = Field(default_factory=list)


# ---------------------------------------------------------------- documents
class DocExtraction(BaseModel):
    doc_type: Literal["quotation", "ISO9001", "test_report", "FSC", "udyam_msme", "gst_cert", "other"]
    issuer: Optional[str] = Field(default=None, description="Certification body / lab that issued it")
    doc_number: Optional[str] = None
    issue_date: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD")
    expiry_date: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD; null for documents without expiry (e.g. test reports)")
    holder_name: Optional[str] = Field(default=None, description="The company the certificate/report was issued TO, exactly as printed")
    site_address: Optional[str] = None
    scope_text: Optional[str] = None
    covers_text: Optional[str] = Field(default=None, description="For test reports: which products/grades/lines it covers and the result, verbatim")
    source: Src
