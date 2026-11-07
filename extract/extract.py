"""Extraction pipeline (CLAUDE.md section 5, steps 1-8) for one vendor reply.

preprocess -> Gemini structured extraction (+ independent second read) -> match to our lines -> expand
blanket and freight rules -> insert raw values -> layer-0 completeness -> rule engine -> recompute_all.

Nothing here is vendor-specific: behaviour comes from the prompts (extract/prompts/*.md) and the schema.
Raw values are inserted exactly as extracted; every conversion happens later in core.recompute.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core import audit
from core import confidence as conf_mod
from core.config import load_config
from core.recompute import recompute_all
from core.rules import evaluate_all
from core.validate import ensure_completeness
from extract import cache
from extract.gemini_client import file_part, generate_structured, is_daily_quota_error, model_chain
from extract.match import match_lines
from extract.preprocess import Prepared, prepare
from extract.schema import DocExtraction, QuoteExtraction, SecondRead

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
INBOX = ROOT / "data" / "inbox"
TEXT_SOURCES = ("excel", "word", "email_text")


# ------------------------------------------------------------------ prompts
def prompt_version(name: str, cfg: dict) -> str:
    """Version of one prompt: gemini.prompt_versions[name], else the default gemini.prompt_version."""
    g = cfg["gemini"]
    return (g.get("prompt_versions") or {}).get(name, g["prompt_version"])


def load_prompt(name: str, version: str) -> str:
    return (PROMPT_DIR / f"{name}_{version}.md").read_text(encoding="utf-8")


def questions_block(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT q_code, question_text, answer_type FROM rfx_questions ORDER BY question_id").fetchall()
    lines = ["THE BUYER'S QUESTIONS (use these exact q_codes):"]
    lines += [f"- {r['q_code']} ({r['answer_type']}): {r['question_text']}" for r in rows]
    return "\n".join(lines)


def _cache_version(version: str, prompt_text: str) -> str:
    """Cache key part: prompt version + a hash of the prompt text, so editing a prompt invalidates stale results."""
    return f"{version}-{hashlib.sha256(prompt_text.encode('utf-8')).hexdigest()[:8]}"


def pdf_text(data: bytes) -> str:
    """Text layer of a PDF, used ONLY to verify that quoted snippets really occur in the document."""
    try:
        import io

        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return ""


def date_in_text(iso: str | None, text: str | None) -> bool | None:
    """True/False if an ISO date (YYYY-MM-DD) can be found in the text in any common written form; None if
    there is nothing to check. Used to verify certificate dates against the document's own text layer."""
    if not iso or not text:
        return None
    from datetime import date

    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return False
    day = str(d.day)
    forms = {d.strftime(f) for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y",
                                     "%B %d, %Y", "%d-%b-%Y", "%d %b, %Y", "%d.%m.%y", "%d/%m/%y")}
    forms |= {f"{day} {d.strftime('%b')} {d.year}", f"{day} {d.strftime('%B')} {d.year}", f"{d.strftime('%b')} {day}, {d.year}",
              f"{d.strftime('%B')} {day}, {d.year}", f"{day}/{d.month}/{d.year}", f"{day}-{d.month}-{d.year}"}
    t = _norm_text(text)
    return any(_norm_text(f) in t for f in forms)


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def verify_snippet(snippet: str | None, source_text: str | None) -> bool | None:
    """True/False if we can check the snippet against the source text; None if there is no text to check."""
    if not source_text or not snippet:
        return None
    return _norm_text(snippet) in _norm_text(source_text)


# ------------------------------------------------------------------ model calls (cached)
@dataclass
class Calls:
    """Bookkeeping for one ingest: which calls hit the API and which came from the cache."""
    api: int = 0
    cached: int = 0
    log: list[str] = field(default_factory=list)

    def note(self, what: str, from_cache: bool) -> None:
        self.cached += from_cache
        self.api += not from_cache
        self.log.append(f"{what}: {'cache' if from_cache else 'API'}")


def _contents(prompt: str, prep: Prepared, label: str) -> list[Any]:
    if prep.text is not None:
        return [f"{prompt}\n\n=== {label} ===\n{prep.text}"]
    return [f"{prompt}\n\n=== {label} (the attached file) ===", file_part(prep.data, prep.mime_type)]


def _run(prep: Prepared, name: str, schema: Any, prompt: str, cfg: dict, calls: Calls, force: bool,
         label: str) -> tuple[Any, dict]:
    version = _cache_version(prompt_version(name, cfg), prompt)
    sha = cache.sha256_file(prep.path)
    chain = model_chain(name, cfg)
    if not force:  # a result cached under ANY model in the chain is good enough
        for m in chain:
            hit = cache.load(sha, name, version, m)
            if hit is not None:
                calls.note(f"{name} {prep.path.name}", True)
                return schema.model_validate(hit["result"]), {**hit.get("meta", {}), "model": m}
    last: Exception | None = None
    for m in chain:  # daily quota exhausted on one model -> next model
        try:
            parsed, _raw, usage = generate_structured(_contents(prompt, prep, label), schema, model=m)
        except Exception as e:
            if is_daily_quota_error(e):
                last = e
                calls.log.append(f"{name} {prep.path.name}: {m} daily quota exhausted, trying next model")
                continue
            raise
        meta = {"usage": usage, "file": prep.path.name, "model": m}
        cache.save(sha, name, version, m, parsed.model_dump(), meta)
        calls.note(f"{name} {prep.path.name}", False)
        return parsed, meta
    raise RuntimeError(f"All models in {chain} have exhausted their daily quota for {name}") from last


def extract_quote(prep: Prepared, conn: sqlite3.Connection, cfg: dict, calls: Calls, force: bool) -> tuple[QuoteExtraction, dict]:
    prompt = load_prompt("quote_extract", prompt_version("quote_extract", cfg)).replace("{{QUESTIONS}}", questions_block(conn))
    return _run(prep, "quote_extract", QuoteExtraction, prompt, cfg, calls, force, "VENDOR REPLY")


def extract_second_read(prep: Prepared, conn: sqlite3.Connection, cfg: dict, calls: Calls, force: bool) -> SecondRead:
    prompt = load_prompt("second_read", prompt_version("second_read", cfg)).replace("{{QUESTIONS}}", questions_block(conn))
    return _run(prep, "second_read", SecondRead, prompt, cfg, calls, force, "VENDOR REPLY")[0]


def extract_document(prep: Prepared, cfg: dict, calls: Calls, force: bool, with_meta: bool = False) -> Any:
    prompt = load_prompt("doc_extract", prompt_version("doc_extract", cfg))
    res = _run(prep, "doc_extract", DocExtraction, prompt, cfg, calls, force, "DOCUMENT")
    return res if with_meta else res[0]


# ------------------------------------------------------------------ rule selection (shared by first and second read)
def pick_by_ply(rules: list[Any], ply: int | None) -> Any | None:
    """The rule scoped to this ply, else the 'rest' / all-boxes rule."""
    for r in rules:
        if r.scope_ply is not None and r.scope_ply == ply:
            return r
    for r in rules:
        if getattr(r, "scope_rest", False) or (r.scope_ply is None and not hasattr(r, "scope_rest")):
            return r
    return None


def freight_values(rules: list[Any], ply: int | None, own_basis: str | None = None,
                   own_value: float | None = None) -> tuple[str, float | None, str]:
    """(freight_basis, raw_freight_value, text). included -> 0.0, extra_quoted -> amount, extra_unquoted -> None."""
    basis, value, text = own_basis, own_value, "line"
    if basis is None:
        r = pick_by_ply(rules, ply)
        if r is None:
            return "extra_unquoted", None, "no freight statement in the reply"
        basis, value, text = r.basis, r.value_inr_per_piece, getattr(r, "text", "")
    if basis == "included":
        return "included", 0.0, text
    if basis == "extra_quoted" and value is not None:
        return "extra_quoted", float(value), text
    return "extra_unquoted", None, text


# ------------------------------------------------------------------ spec comparison (code, not the model)
def spec_compare(cat: sqlite3.Row, vline: Any) -> tuple[str, str | None]:
    """exact / deviation / substitute, from the vendor's ply and layer GSMs vs our spec."""
    ours = [l["gsm"] for l in json.loads(cat["layers_json"])]
    if vline.ply is not None and vline.ply != cat["ply"]:
        theirs = "/".join(map(str, vline.vendor_layers_gsm)) if vline.vendor_layers_gsm else "n/a"
        return "substitute", f"{vline.ply}-ply offered ({theirs} GSM) instead of the specified {cat['ply']}-ply ({'/'.join(map(str, ours))} GSM)"
    g = vline.vendor_layers_gsm
    if g and len(g) == len(ours) and list(g) != ours:
        diffs = [f"layer {i + 1}: {b} GSM offered vs {a} specified" for i, (a, b) in enumerate(zip(ours, g)) if a != b]
        return "deviation", "; ".join(diffs)
    return "exact", None


# ------------------------------------------------------------------ DB insertion
def _insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> int:
    cols = list(row)
    cur = conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                       [row[c] for c in cols])
    return int(cur.lastrowid)


def _j(v: Any) -> str | None:
    return json.dumps(v, ensure_ascii=False) if v else None


def _header_source_map(header: Any) -> dict[str, dict]:
    """Map our field names to the model's header source entries (by keyword in the field name)."""
    want = {"price": ("price", "rate"), "freight": ("freight",), "payment_terms": ("payment",), "gst_rate": ("gst",),
            "tax_basis": ("tax", "gst"), "lead_time": ("lead", "delivery"), "moq": ("moq", "minimum")}
    out: dict[str, dict] = {}
    for hs in header.sources:
        f = hs.field.lower()
        for ours, keys in want.items():
            if ours not in out and any(k in f for k in keys):
                out[ours] = {"ref": hs.source.ref, "snippet": hs.source.snippet}
    return out


def _charges_for(charges: list[Any], cat: sqlite3.Row) -> list[dict]:
    out = []
    for c in charges:
        if c.applies_to == "printed_items" and not cat["print_colours"]:
            continue
        item = {"type": c.type, "amount": c.amount, "basis": c.basis, "source": c.source.ref + ": " + c.text}
        if c.per_colour:
            item["colours"] = cat["print_colours"] or 1
        out.append(item)
    return out


def _discounts_for(header: Any, line: Any | None) -> list[dict]:
    items = list(header.discounts) + (list(line.discounts) if line is not None else [])
    return [{"pct": d.pct, "condition_type": d.condition_type, "condition_value": d.condition_value,
             "condition_text": d.condition_text, "qty_basis": d.qty_basis, "source": d.source.ref, "applied": False}
            for d in items]


def _terms(header: Any) -> dict[str, Any]:
    return {"payment_terms_days": header.payment_terms_days, "lead_time_days": header.lead_time_days,
            "moq_qty": header.moq_qty}


def _supersede(conn: sqlite3.Connection, vendor_id: int, actor: str) -> int | None:
    """Mark the vendor's previous extraction as superseded; returns its main response id (or None)."""
    old = conn.execute("""SELECT response_id, version FROM vendor_responses WHERE vendor_id=?
                          AND NOT EXISTS (SELECT 1 FROM vendor_responses n WHERE n.supersedes_response_id=vendor_responses.response_id)
                          ORDER BY response_id""", (vendor_id,)).fetchall()
    if not old:
        return None
    conn.execute("UPDATE quotes SET is_current=0 WHERE vendor_id=? AND is_current=1", (vendor_id,))
    conn.execute("UPDATE questionnaire_answers SET is_current=0 WHERE vendor_id=? AND is_current=1", (vendor_id,))
    conn.execute("UPDATE questionnaire_answers SET evidence_doc_id=NULL WHERE vendor_id=?", (vendor_id,))
    conn.execute("DELETE FROM documents WHERE vendor_id=?", (vendor_id,))
    audit.log(conn, "response_superseded", ("vendor", vendor_id), evidence={"responses": [r["response_id"] for r in old]},
              actor=actor)
    return old[-1]["response_id"]


# ------------------------------------------------------------------ concurrent prefetch
def prefetch(conn: sqlite3.Connection, entries: list[dict], cfg: dict, force: bool = False, workers: int = 6,
             inbox: Path = INBOX, progress: Callable[[str], None] | None = None) -> Calls:
    """Run every Gemini call for these manifest entries concurrently and leave the results in the cache.

    The calls are independent (keyed by file hash), so the slow part (model latency) overlaps. Ingestion
    afterwards reads everything from the cache. Phase 1 classifies PDFs/images, phase 2 extracts the quotes.
    """
    calls = Calls()
    preps = [prepare(inbox / f) for e in entries for f in e["files"]]
    native = [p for p in preps if p.source_type not in TEXT_SOURCES]
    quote_preps = [p for p in preps if p.source_type in TEXT_SOURCES]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        if progress:
            progress(f"Reading {len(native)} PDF/image files ...")
        kinds = list(ex.map(lambda p: extract_document(p, cfg, calls, force).doc_type, native))
        quote_preps += [p for p, k in zip(native, kinds) if k == "quotation"]
        qprompt = load_prompt("quote_extract", prompt_version("quote_extract", cfg)).replace("{{QUESTIONS}}", questions_block(conn))
        sprompt = load_prompt("second_read", prompt_version("second_read", cfg)).replace("{{QUESTIONS}}", questions_block(conn))
        if progress:
            progress(f"Extracting {len(quote_preps)} vendor replies (first read + second read) ...")
        jobs = [(lambda p=p: _run(p, "quote_extract", QuoteExtraction, qprompt, cfg, calls, force, "VENDOR REPLY")) for p in quote_preps]
        jobs += [(lambda p=p: _run(p, "second_read", SecondRead, sprompt, cfg, calls, force, "VENDOR REPLY")) for p in quote_preps]
        list(ex.map(lambda j: j(), jobs))
    return calls


# ------------------------------------------------------------------ ingest one vendor
def ingest_vendor(conn: sqlite3.Connection, entry: dict, force: bool = False, finalize: bool = True,
                  inbox: Path = INBOX, actor: str = "gemini", cfg: dict | None = None,
                  prefetched: Calls | None = None) -> dict[str, Any]:
    """Extract one manifest entry (one vendor reply with its attachments) into the database."""
    cfg = cfg or load_config()
    calls = Calls()
    if prefetched is None:  # warm the cache concurrently; everything below then reads from it
        calls = prefetch(conn, [entry], cfg, force=force, inbox=inbox)
        force = False
    model = cfg["gemini"]["model"]
    vendor = conn.execute("SELECT * FROM vendors WHERE vendor_code=?", (entry["vendor_code"],)).fetchone()
    if vendor is None:
        raise KeyError(f"unknown vendor {entry['vendor_code']}")
    rfq = conn.execute("SELECT * FROM rfqs ORDER BY rfq_id LIMIT 1").fetchone()
    catalog = conn.execute("SELECT * FROM line_items WHERE rfq_id=? ORDER BY line_no", (rfq["rfq_id"],)).fetchall()
    cat_by_id = {c["line_id"]: c for c in catalog}

    # 1. preprocess + classify: text formats are quotes; PDFs/images are classified by content
    quote_files: list[Prepared] = []
    docs: list[tuple[Prepared, DocExtraction, dict]] = []
    for f in entry["files"]:
        prep = prepare(inbox / f)
        if prep.source_type in TEXT_SOURCES:
            quote_files.append(prep)
            continue
        d, dmeta0 = extract_document(prep, cfg, calls, force, with_meta=True)
        if d.doc_type == "quotation":
            quote_files.append(prep)
        else:
            docs.append((prep, d, dmeta0))
    if not quote_files:
        raise ValueError(f"{entry['vendor_code']}: no quotation file found among {entry['files']}")

    prev = _supersede(conn, vendor["vendor_id"], actor)
    received = entry["received_at"]
    summary: dict[str, Any] = {"vendor": entry["vendor_code"], "responses": [], "quotes": 0, "unmatched": 0}

    for prep in quote_files:
        ext, qmeta = extract_quote(prep, conn, cfg, calls, force)
        sr = extract_second_read(prep, conn, cfg, calls, force)
        h = ext.header
        src_text = prep.text if prep.text is not None else (pdf_text(prep.data) if prep.mime_type == "application/pdf" else None)
        resp_id = _insert(conn, "vendor_responses", {
            "rfq_id": rfq["rfq_id"], "vendor_id": vendor["vendor_id"], "file_name": prep.path.name,
            "file_path": str(prep.path.relative_to(ROOT)) if prep.path.is_relative_to(ROOT) else str(prep.path),
            "source_type": prep.source_type, "content_role": "mixed" if ext.answers else "quote",
            "received_at": received, "received_after_due": int(received[:10] > rfq["response_due_date"]),
            "version": 1 + (conn.execute("SELECT COALESCE(MAX(version),0) FROM vendor_responses WHERE vendor_id=?",
                                         (vendor["vendor_id"],)).fetchone()[0]),
            "revision_scope": "full", "supersedes_response_id": prev, "extraction_status": "extracted",
            "extraction_model": f"{prep.path_label}→{qmeta.get('model', model)}@prompt_{prompt_version('quote_extract', cfg)}",
            "raw_extraction_json": json.dumps(ext.model_dump(), ensure_ascii=False),
            "quote_ref_no": h.quote_ref_no, "quote_date": h.quote_date, "validity_date": h.validity_date,
            "price_basis": h.price_basis, "rm_clause_text": h.rm_clause_text,
            "header_terms_json": json.dumps({
                "freight": [r.text for r in h.freight_rules], "payment": h.payment_terms_text, "gst_rate_pct": h.gst_rate_pct,
                "tax_basis": h.tax_basis, "validity": h.validity_text, "moq": h.moq_text,
                "discounts": [d.condition_text for d in h.discounts], "charges": [c.text for c in h.charges],
                "blanket_rules": [b.text for b in h.blanket_rules], "notes": h.notes}, ensure_ascii=False),
            "declared_msme_category": h.declared_msme_category,
        })
        summary["responses"].append(resp_id)
        hdr_src = _header_source_map(h)

        # 3. match vendor lines to our lines (by size, ply, description; never by row number)
        matches = match_lines([ln.model_dump() for ln in ext.lines], [dict(c) for c in catalog])
        created: list[tuple[int, str, Any, Any]] = []   # (quote_id, kind, vendor line or rule, catalogue row)
        claimed: set[int] = set()

        def quote_row(cat: sqlite3.Row | None, **kw: Any) -> dict[str, Any]:
            base = {"response_id": resp_id, "vendor_id": vendor["vendor_id"], "line_id": cat["line_id"] if cat else None,
                    "is_current": 1, "tax_basis": h.tax_basis, "gst_rate_pct": h.gst_rate_pct, **_terms(h)}
            base.update(kw)
            return base

        for m, ln in zip(matches, ext.lines):
            cat = cat_by_id.get(m.line_id) if m.line_id else None
            src = {"ref": ln.source.ref, "snippet": ln.source.snippet}
            meta = {"extraction": {"flags": ["LOW_CONFIDENCE_OCR"] if ln.ocr_uncertain else [],
                                   "source": {"price": src, **{k: v for k, v in hdr_src.items() if k != "price"}},
                                   "match": {"method": m.method, "score": m.score}}}
            if cat is None:
                qid = _insert(conn, "quotes", quote_row(None, quote_status="unmatched", vendor_line_ref=f"{ln.line_key} | {ln.vendor_item_text}",
                                                        raw_price_text=ln.raw_price_text, raw_price=ln.price, raw_currency=ln.currency,
                                                        raw_unit=ln.unit, spec_deviation_note=m.note, field_meta_json=json.dumps(meta, ensure_ascii=False)))
                summary["unmatched"] += 1
                continue
            claimed.add(cat["line_id"])
            fb, fv, ftext = freight_values(h.freight_rules, cat["ply"], ln.freight_basis, ln.freight_value_inr_per_piece)
            spec_match, note = spec_compare(cat, ln)
            offered = None
            if ln.capacity_per_month and ln.capacity_per_month * 12 < (cat["annual_qty"] or 0):
                offered = ln.capacity_per_month * 12
            qid = _insert(conn, "quotes", quote_row(
                cat, quote_status="quoted", vendor_line_ref=f"{ln.line_key} | {ln.vendor_item_text}",
                raw_price_text=ln.raw_price_text, raw_price=ln.price, raw_currency=ln.currency, raw_unit=ln.unit,
                price_tiers_json=_j([{"min_qty": t.min_qty, "price": t.price, "qty_basis": t.qty_basis} for t in ln.tiers]),
                discounts_json=_j(_discounts_for(h, ln)), charges_json=_j(_charges_for(h.charges, cat)),
                freight_basis=fb, raw_freight_text=ftext, raw_freight_value=fv,
                offered_qty=offered, capacity_text=ln.capacity_text,
                vendor_spec_json=json.dumps({"ply": ln.ply, "construction": ln.construction_text, "layers_gsm": ln.vendor_layers_gsm,
                                             "dim_basis": ln.dim_basis, "dims": [ln.length_mm, ln.width_mm, ln.height_mm], "remarks": ln.remarks}),
                spec_match=spec_match, spec_deviation_note=note, field_meta_json=json.dumps(meta, ensure_ascii=False)))
            created.append((qid, "line", ln, cat))

        # 3b. blanket rules ("42/kg for 5-ply ... rest same as last year") expand over lines the vendor did not itemise
        if h.blanket_rules:
            for cat in catalog:
                if cat["line_id"] in claimed:
                    continue
                rule = pick_by_ply(h.blanket_rules, cat["ply"])
                if rule is None:
                    continue
                fb, fv, ftext = freight_values(h.freight_rules, cat["ply"])
                meta = {"extraction": {"flags": [], "source": {"price": {"ref": rule.source.ref, "snippet": rule.source.snippet},
                                                              **{k: v for k, v in hdr_src.items() if k != "price"}},
                                       "match": {"method": "blanket_rule", "score": 1.0}}}
                qid = _insert(conn, "quotes", quote_row(
                    cat, quote_status="quoted", vendor_line_ref=f"blanket: {rule.text}",
                    raw_price_text=rule.text, raw_price=rule.price, raw_currency=rule.currency, raw_unit=rule.unit,
                    discounts_json=_j(_discounts_for(h, None)), charges_json=_j(_charges_for(h.charges, cat)),
                    freight_basis=fb, raw_freight_text=ftext, raw_freight_value=fv, spec_match="exact",
                    field_meta_json=json.dumps(meta, ensure_ascii=False)))
                created.append((qid, "blanket", rule, cat))
        summary["quotes"] += len(created)

        # 6b. second read -> per-quote view of the 4 core fields (same expansion logic as the first read)
        srl = {l.line_key: l for l in sr.lines}
        second: dict[str, dict] = {}
        for qid, kind, obj, cat in created:
            if kind == "line":
                s = srl.get(obj.line_key)
                price, unit, cur = (s.price, s.unit, s.currency) if s else (None, None, None)
                have = s is not None
            else:
                s = pick_by_ply(sr.blanket_rules, cat["ply"])
                price, unit, cur = (s.price, s.unit, s.currency) if s else (None, None, None)
                have = s is not None
            if not have:
                continue
            fb2, fv2, _ = freight_values(sr.freight_rules, cat["ply"])
            second[str(qid)] = {"raw_price": price, "raw_unit": unit, "raw_currency": cur, "raw_freight_value": fv2}
        conn.execute("UPDATE vendor_responses SET second_read_json=? WHERE response_id=?", (json.dumps(second), resp_id))

        # 7. questionnaire answers: confidence from signals (verbatim in source, second read agrees)
        qid_by_code = {r["q_code"]: r["question_id"] for r in conn.execute("SELECT question_id, q_code FROM rfx_questions")}
        sr_ans = {a.q_code: a.answer_value for a in sr.answers}
        for a in ext.answers:
            if a.q_code not in qid_by_code:
                continue
            agree = None if a.q_code not in sr_ans else _same_answer(a.answer_value, sr_ans[a.q_code])
            conf, signals = conf_mod.score_answer(prep.source_type, cfg, verify_snippet(a.source.snippet, src_text), agree)
            _insert(conn, "questionnaire_answers", {
                "response_id": resp_id, "vendor_id": vendor["vendor_id"], "question_id": qid_by_code[a.q_code], "is_current": 1,
                "answer_raw": a.answer_text, "answer_value": a.answer_value,
                "source_ref_json": json.dumps({"ref": a.source.ref, "snippet": a.source.snippet, "document_attached": a.document_attached}),
                "confidence": conf, "confidence_signals_json": json.dumps(signals)})

    # 8. attachments -> documents (+ one response row per file so the Responses screen lists every file)
    for prep, d, dmeta in docs:
        text = pdf_text(prep.data) if prep.mime_type == "application/pdf" else None
        conf, _ = conf_mod.score_document(prep.source_type, cfg, [
            verify_snippet(d.holder_name, text), verify_snippet(d.doc_number, text), date_in_text(d.expiry_date or d.issue_date, text)])
        rid = _insert(conn, "vendor_responses", {
            "rfq_id": rfq["rfq_id"], "vendor_id": vendor["vendor_id"], "file_name": prep.path.name,
            "file_path": str(prep.path.relative_to(ROOT)) if prep.path.is_relative_to(ROOT) else str(prep.path),
            "source_type": prep.source_type, "content_role": "certificate" if d.doc_type != "test_report" else "mixed",
            "received_at": received, "received_after_due": int(received[:10] > rfq["response_due_date"]),
            "version": 1, "revision_scope": "full", "extraction_status": "extracted",
            "extraction_model": f"{prep.path_label}→{dmeta.get('model', model)}@prompt_{prompt_version('doc_extract', cfg)}", "raw_extraction_json": json.dumps(d.model_dump())})
        _insert(conn, "documents", {
            "rfq_id": rfq["rfq_id"], "vendor_id": vendor["vendor_id"], "response_id": rid, "file_name": prep.path.name,
            "file_path": str(prep.path), "doc_type": d.doc_type if d.doc_type != "quotation" else "other",
            "issuer": d.issuer, "doc_number": d.doc_number, "issue_date": d.issue_date, "expiry_date": d.expiry_date,
            "holder_name": d.holder_name, "site_address": d.site_address, "scope_text": d.scope_text,
            "extracted_json": json.dumps(d.model_dump(), ensure_ascii=False),
            "source_ref_json": json.dumps({"ref": d.source.ref, "snippet": d.source.snippet}), "confidence": conf})
        summary["responses"].append(rid)
    conn.commit()
    summary["documents"] = len(docs)
    if prefetched is not None:  # attribute the shared prefetch's API calls to this vendor by file name
        mine = [l for l in prefetched.log if any(f in l for f in entry["files"])]
        calls = Calls(api=sum(l.endswith("API") for l in mine), cached=sum(l.endswith("cache") for l in mine), log=mine)
    summary["calls"] = {"api": calls.api, "cache": calls.cached, "log": calls.log}

    if finalize:
        finalize_all(conn, cfg=cfg, vendors=[entry["vendor_code"]], force=force)
    return summary


def _same_answer(a: str, b: str) -> bool:
    na, nb = _norm_text(a), _norm_text(b)
    if na == nb:
        return True
    fa, fb = re.search(r"-?\d+(?:\.\d+)?", na), re.search(r"-?\d+(?:\.\d+)?", nb)
    if fa and fb and not re.search(r"[a-z]", na.replace("t", "")) and not re.search(r"[a-z]", nb.replace("t", "")):
        return float(fa.group()) == float(fb.group())
    return False


# ------------------------------------------------------------------ whole run
def finalize_all(conn: sqlite3.Connection, cfg: dict | None = None, vendors: list[str] | None = None,
                 force: bool = False, draft: bool = True) -> None:
    """Layer 0 completeness BEFORE ranking, the questionnaire rule engine, recompute, then clarification drafts."""
    cfg = cfg or load_config()
    for r in conn.execute("""SELECT response_id FROM vendor_responses vr WHERE content_role IN ('quote','mixed')
                             AND revision_scope='full' AND EXISTS (SELECT 1 FROM quotes q WHERE q.response_id=vr.response_id)""").fetchall():
        ensure_completeness(conn, r["response_id"])
    evaluate_all(conn, cfg)
    recompute_all(conn, cfg)
    if draft:
        from extract.clarify import draft_for_vendor

        codes = vendors or [r["vendor_code"] for r in conn.execute("SELECT vendor_code FROM vendors ORDER BY vendor_id")]
        for code in codes:
            try:
                draft_for_vendor(conn, code, cfg=cfg, force=force)
            except RuntimeError as e:  # e.g. every model's daily quota is used up: keep going, the draft can be made later
                print(f"  clarification draft for {code} skipped: {e}")


def load_manifest(inbox: Path = INBOX) -> list[dict]:
    return json.loads((inbox / "manifest.json").read_text(encoding="utf-8"))


def ingest_all(conn: sqlite3.Connection, force: bool = False, progress: Callable[[str], None] | None = None,
               inbox: Path = INBOX, draft: bool = True) -> list[dict]:
    cfg = load_config()
    out = []
    entries = load_manifest(inbox)
    pre = prefetch(conn, entries, cfg, force=force, inbox=inbox, progress=progress)
    for entry in entries:
        if progress:
            progress(f"Ingesting {entry['vendor_code']} ...")
        out.append(ingest_vendor(conn, entry, force=False, finalize=False, inbox=inbox, cfg=cfg, prefetched=pre))
    if progress:
        progress("Validating, scoring and drafting clarification emails ...")
    finalize_all(conn, cfg=cfg, force=force, draft=draft)
    return out
