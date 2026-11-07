"""P2 tests that need no API: preprocessing, matching, cache, expansion logic, and the ingest pipeline
driven by a stubbed Gemini (canned structured outputs)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import confidence as conf_mod
from core.config import load_config
from core.db import connect, init_db
from core.seed import seed
from extract import cache, extract as ex
from extract.match import desc_similarity, match_lines, parse_dims
from extract.preprocess import docx_to_markdown, eml_to_text, prepare, xlsx_to_celltext
from extract.schema import (Answer, BlanketRule, DocExtraction, FreightRule, Header, Line, QuoteExtraction,
                            SecondRead, Src, SRBlanket, SRFreight, SRLine)

INBOX = Path(__file__).resolve().parent.parent / "data" / "inbox"
S = Src(ref="x", snippet="x")


# ------------------------------------------------------------------ preprocess
def test_xlsx_celltext_has_cell_addresses_and_merged_ranges():
    t = xlsx_to_celltext(INBOX / "V1_Deccan_Price_Offer_SCPL.xlsx")
    assert "Price Offer!E7: 10945" in t and "Price Offer!B7: EXPORT DETERGENT 25KG BULK BOX" in t
    assert "Merged ranges" in t and "A11:G11" in t          # section header rows are merged
    assert "Vendor Questionnaire!C2: Yes - Cert No." in t  # second sheet is included


def test_docx_markdown_has_numbered_paragraphs_and_tables():
    t = docx_to_markdown(INBOX / "V3_WesternIndiaCartons_Offer_WICL-2611-044.docx")
    assert "[P1] WESTERN INDIA CARTONS LTD" in t and "[Table 1]" in t
    assert "| Row | Item | Size (mm) | Ply | Price USD/pc |" in t
    assert "USD 1.0541 per piece" in t                      # 7-ply prices live in prose


def test_eml_keeps_the_rupee_sign_and_headers():
    t = eml_to_text(INBOX / "V5_KrishnaKraft_email.eml")
    assert "₹42/kg" in t and "Subject: Re: RFQ" in t


def test_pdf_and_photo_go_native_not_converted():
    pdf = prepare(INBOX / "V2_ShreeGanesh_Quotation_SGPI-Q-1127.pdf")
    jpg = prepare(INBOX / "V4_OmSai_ratecard_photo.jpg")
    assert pdf.text is None and pdf.mime_type == "application/pdf" and pdf.data.startswith(b"%PDF")
    assert jpg.text is None and jpg.mime_type == "image/jpeg" and jpg.source_type == "photo"
    assert prepare(INBOX / "V1_Deccan_Price_Offer_SCPL.xlsx").path_label == "xlsx→celltext"


# ------------------------------------------------------------------ matching
@pytest.fixture(scope="module")
def catalog():
    c = connect(":memory:")
    init_db(c)
    seed(c)
    rows = [dict(r) for r in c.execute("SELECT * FROM line_items")]
    c.close()
    return rows


def test_parse_dims_tolerates_spacing_and_x_variants():
    assert parse_dims("600 x 400 x 400") == parse_dims("600X400X400") == parse_dims("600×400×400 mm") == (600, 400, 400)
    assert parse_dims("no size here") is None


def test_match_by_size_and_ply_ignores_vendor_row_numbers(catalog):
    # a photo rate card whose rows are numbered differently from ours
    vl = [{"line_key": "1", "length_mm": 420, "width_mm": 300, "height_mm": 250, "ply": 5, "vendor_item_text": ""},
          {"line_key": "2", "length_mm": 300, "width_mm": 200, "height_mm": 150, "ply": 3, "vendor_item_text": ""}]
    m = match_lines(vl, catalog)
    assert [x.line_no for x in m] == [16, 1] and m[0].method == "size+ply"


def test_same_size_wrong_ply_still_matches_and_prefers_same_ply(catalog):
    m = match_lines([{"length_mm": 400, "width_mm": 300, "height_mm": 300, "ply": 3, "vendor_item_text": "mixed sku e-commerce shipper"}], catalog)[0]
    assert m.line_no == 25 and m.method == "size"          # spec comparison later calls it a substitute


def test_match_falls_back_to_description_and_leaves_unknowns_unmatched(catalog):
    vl = [{"vendor_item_text": "Edible oil 1L pouch x 16 carton", "ply": 5},
          {"length_mm": 999, "width_mm": 999, "height_mm": 999, "ply": 5, "vendor_item_text": "Mystery box"}]
    m = match_lines(vl, catalog)
    assert m[0].line_no == 19 and m[0].method == "description" and m[1].line_id is None


def test_a_catalogue_line_is_claimed_by_one_vendor_line_only(catalog):
    same = {"length_mm": 300, "width_mm": 200, "height_mm": 150, "ply": 3, "vendor_item_text": "Shampoo 180ml bottle shipper (24s)"}
    m = match_lines([same, dict(same, vendor_item_text="something else")], catalog)
    assert sorted(x.line_no is not None for x in m) == [False, True]


def test_description_similarity_is_case_and_unit_spacing_insensitive():
    assert desc_similarity("DETERGENT POWDER 1KG X 20 MASTER", "Detergent powder 1kg x 20 master") > 0.95
    assert desc_similarity("1 kg detergent", "Shampoo sachet master") < 0.4


# ------------------------------------------------------------------ cache
def test_cache_roundtrip_force_and_prompt_edit_invalidates(tmp_path):
    calls = []

    def fn():
        calls.append(1)
        return {"v": len(calls)}, {"m": 1}

    sha = cache.sha256_bytes(b"file")
    r1, _, hit1 = cache.cached_call(sha, "quote_extract", "v1-aaaa", "m", fn, cache_dir=tmp_path)
    r2, _, hit2 = cache.cached_call(sha, "quote_extract", "v1-aaaa", "m", fn, cache_dir=tmp_path)
    assert (hit1, hit2) == (False, True) and r1 == r2 and len(calls) == 1
    cache.cached_call(sha, "quote_extract", "v1-aaaa", "m", fn, force=True, cache_dir=tmp_path)
    assert len(calls) == 2                                  # force re-runs
    cache.cached_call(sha, "quote_extract", "v1-bbbb", "m", fn, cache_dir=tmp_path)
    cache.cached_call(sha, "quote_extract", "v1-aaaa", "other-model", fn, cache_dir=tmp_path)
    assert len(calls) == 4                                  # new prompt text or model = new key
    assert ex._cache_version("v1", "text A") != ex._cache_version("v1", "text B")


# ------------------------------------------------------------------ expansion helpers
def test_freight_values_by_ply_and_normalisation():
    rules = [FreightRule(scope_ply=3, basis="extra_quoted", value_inr_per_piece=1.2, text="", source=S),
             FreightRule(scope_ply=7, basis="extra_quoted", value_inr_per_piece=5.5, text="", source=S)]
    assert ex.freight_values(rules, 3)[:2] == ("extra_quoted", 1.2)
    assert ex.freight_values(rules, 7)[:2] == ("extra_quoted", 5.5)
    assert ex.freight_values(rules, 5)[:2] == ("extra_unquoted", None)   # nobody said anything for 5-ply
    inc = [FreightRule(basis="included", text="FOR", source=S)]
    assert ex.freight_values(inc, 5)[:2] == ("included", 0.0)
    unq = [FreightRule(basis="extra_quoted", value_inr_per_piece=None, text="freight extra", source=S)]
    assert ex.freight_values(unq, 3)[:2] == ("extra_unquoted", None)     # 'extra' without an amount is not a number


def test_spec_compare_substitute_deviation_exact(catalog):
    c = connect(":memory:")
    init_db(c)
    seed(c)
    cat16, cat25, cat1 = (c.execute("SELECT * FROM line_items WHERE line_no=?", (n,)).fetchone() for n in (16, 25, 1))
    L = lambda **k: Line(line_key="k", vendor_item_text="t", raw_price_text="1", price=1, unit="per_piece", source=S, **k)  # noqa: E731
    assert ex.spec_compare(cat25, L(ply=3, vendor_layers_gsm=[200, 150, 200]))[0] == "substitute"
    s, note = ex.spec_compare(cat16, L(ply=5, vendor_layers_gsm=[150, 120, 150, 120, 150]))
    assert s == "deviation" and "layer 1: 150 GSM offered vs 180 specified" in note
    assert ex.spec_compare(cat16, L(ply=5, vendor_layers_gsm=[180, 120, 150, 120, 180]))[0] == "exact"
    assert ex.spec_compare(cat1, L(ply=3))[0] == "exact"                  # nothing stated -> no deviation invented


def test_answer_confidence_is_built_from_signals():
    cfg = load_config()
    c, sig = conf_mod.score_answer("word", cfg, True, True)
    assert c == pytest.approx(0.95) and len(sig) == 3
    assert conf_mod.score_answer("word", cfg, True, None)[0] < 0.95          # one signal is not enough for a knock-out
    assert conf_mod.score_answer("excel", cfg, True, False)[0] < 0.95        # disagreeing second read caps it
    assert conf_mod.score_answer("email_text", cfg, None, None)[0] == pytest.approx(0.80)


# ------------------------------------------------------------------ ingest with a stubbed Gemini
def _canned(inbox_has_pdf: bool = False):
    hdr = Header(
        price_basis="unspecified", tax_basis="ex_gst", gst_rate_pct=18, payment_terms_days=60,
        freight_rules=[FreightRule(basis="extra_unquoted", text="freight extra", source=S)],
        blanket_rules=[BlanketRule(scope_ply=5, price=42, unit="per_kg", text="42/kg for the 5-ply", source=S),
                       BlanketRule(scope_ply=3, price=38, unit="per_kg", text="38 for the 3-ply", source=S),
                       BlanketRule(scope_rest=True, price=None, unit="as_last_year", text="rest same as last year", source=S)])
    lines = [Line(line_key="r1", vendor_item_text="Shampoo 180ml", length_mm=300, width_mm=200, height_mm=150, ply=3,
                  raw_price_text="9.50", price=9.5, unit="per_piece", source=Src(ref="row 1", snippet="9.50")),
             Line(line_key="r2", vendor_item_text="Mystery", length_mm=999, width_mm=999, height_mm=999, ply=5,
                  raw_price_text="1", price=1, unit="per_piece", source=S)]
    answers = [Answer(q_code="Q03_REJECTION", answer_text="Rejection ok", answer_value="yes", source=Src(ref="p1", snippet="Rejection ok"))]
    first = QuoteExtraction(header=hdr, lines=lines, answers=answers)
    second = SecondRead(lines=[SRLine(line_key="r1", price=9.5, unit="per_piece"), SRLine(line_key="r2", price=1, unit="per_piece")],
                        blanket_rules=[SRBlanket(scope_ply=5, price=42, unit="per_kg"), SRBlanket(scope_ply=3, price=38, unit="per_kg"),
                                       SRBlanket(scope_rest=True, price=None, unit="as_last_year")],
                        freight_rules=[SRFreight(basis="extra_unquoted")])
    return first, second


@pytest.fixture
def stub_gemini(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    first, second = _canned()
    seen = []

    def fake(contents, schema, system=None, max_output_tokens=None, model=None):
        seen.append(schema.__name__)
        return {"QuoteExtraction": first, "SecondRead": second}[schema.__name__], "{}", {"prompt_tokens": 1}

    monkeypatch.setattr(ex, "generate_structured", fake)
    monkeypatch.setattr("extract.clarify.generate", lambda *a, **k: SimpleNamespace(text="Subject: Clarification\n\nDear team, ..."))
    return seen


@pytest.fixture
def fresh_db():
    c = connect(":memory:")
    init_db(c)
    seed(c)
    return c


def _entry(tmp_path):
    (tmp_path / "mail.eml").write_text("From: a@b\nDate: Mon, 09 Nov 2026 19:42:11 +0530\nSubject: x\n\nbody", encoding="utf-8")
    return {"vendor_code": "V5", "received_at": "2026-11-09T19:42:11+05:30", "files": ["mail.eml"]}, tmp_path


def test_ingest_expands_blanket_rules_freight_and_flags_unmatched(fresh_db, stub_gemini, tmp_path):
    entry, inbox = _entry(tmp_path)
    r = ex.ingest_vendor(fresh_db, entry, inbox=inbox)
    q = {row["line_no"]: row for row in fresh_db.execute(
        "SELECT q.*, l.line_no FROM quotes q JOIN line_items l USING (line_id) WHERE q.is_current=1")}
    assert len(q) == 30                                                   # 1 explicit + 29 blanket; none missing
    assert q[1]["raw_unit"] == "per_piece" and q[1]["raw_price"] == 9.5   # explicit line wins over the blanket rule
    assert q[2]["raw_unit"] == "per_kg" and q[2]["raw_price"] == 38       # 3-ply rule
    assert q[13]["raw_unit"] == "per_kg" and q[13]["raw_price"] == 42     # 5-ply rule
    assert q[27]["raw_unit"] == "as_last_year" and q[27]["raw_price"] is None  # 'the rest'
    assert all(row["freight_basis"] == "extra_unquoted" and row["raw_freight_value"] is None for row in q.values())
    assert r["unmatched"] == 1
    um = fresh_db.execute("SELECT * FROM quotes WHERE quote_status='unmatched'").fetchone()
    assert um["line_id"] is None and "Mystery" in um["vendor_line_ref"] and um["award_eligible"] == 0
    assert fresh_db.execute("SELECT extraction_model FROM vendor_responses").fetchone()[0] == f"eml→text→{load_config()['gemini']['model']}@prompt_{ex.prompt_version('quote_extract', load_config())}"


def test_ingest_second_read_questionnaire_and_clarification_draft(fresh_db, stub_gemini, tmp_path):
    entry, inbox = _entry(tmp_path)
    ex.ingest_vendor(fresh_db, entry, inbox=inbox)
    sr = json.loads(fresh_db.execute("SELECT second_read_json FROM vendor_responses").fetchone()[0])
    assert len(sr) == 30 and all(v["raw_freight_value"] is None for v in sr.values())
    # agreeing second read: no SECOND_READ_MISMATCH anywhere
    assert not fresh_db.execute("SELECT 1 FROM quotes WHERE field_meta_json LIKE '%SECOND_READ_MISMATCH%'").fetchone()
    a = fresh_db.execute("SELECT * FROM questionnaire_answers").fetchone()
    assert a["answer_value"] == "yes" and a["confidence"] < 0.95        # email base 0.80 + second read, snippet not verifiable in this stub body
    d = fresh_db.execute("SELECT clarification_draft, clarification_status FROM vendor_responses").fetchone()
    assert d["clarification_status"] == "drafted" and d["clarification_draft"].startswith("Subject:")
    assert fresh_db.execute("SELECT COUNT(*) FROM audit_log WHERE action='clarification_drafted'").fetchone()[0] == 1


def test_second_read_disagreement_is_caught_through_the_pipeline(fresh_db, monkeypatch, tmp_path, stub_gemini):
    first, second = _canned()
    second.lines[0].price = 95.0                                           # second reader sees 95 instead of 9.50
    monkeypatch.setattr(ex, "generate_structured", lambda contents, schema, **k: (first if schema.__name__ == "QuoteExtraction" else second, "{}", {}))
    entry, inbox = _entry(tmp_path)
    ex.ingest_vendor(fresh_db, entry, inbox=inbox)
    q = fresh_db.execute("SELECT q.* FROM quotes q JOIN line_items l USING (line_id) WHERE l.line_no=1").fetchone()
    assert "SECOND_READ_MISMATCH" in json.loads(q["field_meta_json"])["flags"] and q["award_eligible"] == 0


def test_reingest_supersedes_the_old_extraction_and_second_run_uses_the_cache(fresh_db, stub_gemini, tmp_path):
    entry, inbox = _entry(tmp_path)
    ex.ingest_vendor(fresh_db, entry, inbox=inbox)
    n_api = len(stub_gemini)
    r = ex.ingest_vendor(fresh_db, entry, inbox=inbox)                     # no force: everything from the cache
    assert len(stub_gemini) == n_api and r["calls"]["api"] == 0
    assert fresh_db.execute("SELECT COUNT(*) FROM quotes WHERE is_current=1 AND quote_status<>'not_quoted'").fetchone()[0] == 31
    assert fresh_db.execute("SELECT COUNT(*) FROM quotes WHERE is_current=0").fetchone()[0] == 31   # history kept
    assert fresh_db.execute("SELECT COUNT(*) FROM audit_log WHERE action='response_superseded'").fetchone()[0] == 1
    assert fresh_db.execute("SELECT lines_quoted FROM v_response_summary").fetchone()[0] == 30        # old response hidden
    ex.ingest_vendor(fresh_db, entry, inbox=inbox, force=True)
    assert len(stub_gemini) > n_api                                         # force makes fresh calls


def test_no_vendor_specific_code_in_the_extraction_package():
    import re

    for f in (Path(ex.__file__).parent).glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"\bV[1-5]\b|Deccan|Shree Ganesh|Western India|Om Sai|Krishna", text), f.name
    for f in (Path(ex.__file__).parent / "prompts").glob("*.md"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"\bV[1-5]\b|Deccan|Shree Ganesh|Western India|Om Sai|Krishna", text), f.name


# ------------------------------------------------------------------ model fallback on daily quota
class _QuotaError(Exception):
    code = 429

    def __init__(self):
        super().__init__("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")


def test_daily_quota_falls_back_to_next_model_and_caches_under_it(monkeypatch, tmp_path):
    from extract.gemini_client import is_daily_quota_error, model_chain, with_retry

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    cfg = load_config()
    cfg["gemini"].update(model="m1", fallback_models=["m2"], task_models={})
    (tmp_path / "a.eml").write_text("From: a@b\nSubject: x\n\nbody", encoding="utf-8")
    prep = prepare(tmp_path / "a.eml")
    first, _ = _canned()
    used = []

    def fake(contents, schema, system=None, max_output_tokens=None, model=None):
        used.append(model)
        if model == "m1":
            raise _QuotaError()
        return first, "{}", {}

    monkeypatch.setattr(ex, "generate_structured", fake)
    assert model_chain("quote_extract", cfg) == ["m1", "m2"] and is_daily_quota_error(_QuotaError())
    calls = ex.Calls()
    got, meta = ex._run(prep, "quote_extract", QuoteExtraction, "prompt", cfg, calls, False, "X")
    assert used == ["m1", "m2"] and meta["model"] == "m2" and any("daily quota exhausted" in l for l in calls.log)
    used.clear()
    _, meta2 = ex._run(prep, "quote_extract", QuoteExtraction, "prompt", cfg, ex.Calls(), False, "X")
    assert used == [] and meta2["model"] == "m2"                  # served from the cache, no call at all
    cfg["gemini"]["fallback_models"] = []                         # nothing left to fall back to -> loud failure
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "other")
    with pytest.raises(RuntimeError, match="exhausted their daily quota"):
        ex._run(prep, "quote_extract", QuoteExtraction, "prompt", cfg, ex.Calls(), False, "X")


def test_daily_quota_is_not_retried_but_overload_is(monkeypatch):
    import extract.gemini_client as g

    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    n = {"quota": 0, "busy": 0}

    def quota():
        n["quota"] += 1
        raise _QuotaError()

    class Busy(Exception):
        code = 503

    def busy():
        n["busy"] += 1
        if n["busy"] < 3:
            raise Busy()
        return "ok"

    with pytest.raises(_QuotaError):
        g.with_retry(quota)
    assert n["quota"] == 1                                         # fail fast
    assert g.with_retry(busy) == "ok" and n["busy"] == 3           # 503 is retried


def test_volume_discount_qty_basis_survives_into_the_database(fresh_db, monkeypatch, tmp_path, stub_gemini):
    """Regression: qty_basis was extracted by the model but dropped before reaching quotes.discounts_json."""
    from extract.schema import Discount

    first, second = _canned()
    first.lines[0].discounts = [Discount(pct=4, condition_type="volume_qty", condition_value=20000, qty_basis="per_order",
                                         condition_text="Addl 4% off for release qty >= 20,000 nos", source=S)]
    monkeypatch.setattr(ex, "generate_structured", lambda contents, schema, **k: (first if schema.__name__ == "QuoteExtraction" else second, "{}", {}))
    entry, inbox = _entry(tmp_path)
    ex.ingest_vendor(fresh_db, entry, inbox=inbox)
    q = fresh_db.execute("SELECT q.* FROM quotes q JOIN line_items l USING (line_id) WHERE l.line_no=1").fetchone()
    assert json.loads(q["discounts_json"])[0]["qty_basis"] == "per_order"
    flags = json.loads(q["field_meta_json"])["flags"]
    assert "TIER_NOT_MET_PER_ORDER" in flags and "TIER_BASIS_UNCLEAR" not in flags   # typical order 10,000 < 20,000
