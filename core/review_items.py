"""Review ITEMS for the Review & Reconcile tab.

A buyer should decide "is a 90-day payment term capped at 45 acceptable?" once, not on 30 rows. This module turns the flags on
every quote into items: vendor-term items (one decision that applies to many lines) and line items (one per-line decision).
It only READS quotes (via core.queries.review_rows) and keeps its own small table, review_decisions, for the buyer's choices.
All value changes still go through core.actions (audit + recompute_quote)."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from core import actions, audit
from core.flags import FIELD_LABELS, FLAG_HELP, REASON_HELP
from core.queries import review_rows

OPEN = ("must_confirm", "spot_check")

# flag -> (kind, title, fields it needs the buyer to confirm). Line-level price problems come first so a sanity failure on one line
# stays its own item even when the vendor also has a vendor-wide price basis.
LINE_FLAGS = {
    "UNIT_AMBIGUOUS": ("price", "Price", ("price", "unit")), "SANITY_RANGE_FAIL": ("price", "Price", ("price", "unit")),
    "SECOND_READ_MISMATCH": ("price", "Price", ("price", "unit")), "ARITHMETIC_FAIL": ("price", "Price", ("price", "unit")),
    "LOW_CONFIDENCE_OCR": ("ocr", "Price", ("price",)),
    "SPEC_SUBSTITUTE": ("spec", "Spec", ("spec_match",)), "SPEC_DEVIATION": ("spec", "Spec", ("spec_match",)),
    "LY_SPEC_CHANGED": ("ly_spec", "Spec changed since last year", ("price", "spec_match")),
    "LY_REF_MISSING": ("ly_missing", "No last-year price", ("price",)),
    "FREIGHT_UNKNOWN": ("freight_unknown", "Freight", ("freight",)),
    "TIER_BASIS_UNCLEAR": ("tier", "Discount", ("price",)),
}
VENDOR_FLAGS = {
    "MSME_TERMS_CAPPED": ("payment", "Payment terms", ("payment_terms",)),
    "CONDITIONAL_DISCOUNT_AVAILABLE": ("cash_discount", "Cash discount", ()),
    "FREIGHT_IMPUTED": ("freight", "Freight", ("freight",)),
    "DERIVED_PER_KG": ("kg_basis", "Price basis (₹/kg)", ("price", "unit")),
    "FX_CONVERTED": ("fx", "Currency (USD to ₹)", ("currency", "price")),
    "GST_RATE_MISMATCH": ("gst", "GST rate", ("gst_rate",)),
    "TAX_BASIS_UNSPECIFIED": ("tax_basis", "Tax basis", ("tax_basis",)),
    "PRICE_NOT_FIRM": ("price_firm", "Price not firm", ()),
    "IMPLIED_BY_BLANKET": ("same_ly", "Same as last year", ("price",)),
    "MSME_CATEGORY_MISMATCH": ("msme_cat", "MSME category", ()),
}
# a flag that appears on at least this many quoted rows of a vendor is a vendor-term item
VENDOR_WIDE_MIN = 2
# kinds whose decision cannot be seen in the quote fields: they are done once the buyer has chosen
DECISION_ONLY = {"cash_discount", "price_firm", "msme_cat", "tier"}

ACTIONS = {
    "price": [("confirm", "✓ Confirm as read"), ("correct", "✎ Correct"), ("ask", "✉ Ask vendor")],
    "cash_discount": [("keep", "✓ Keep not applied"), ("apply", "Apply discount"), ("ask", "✉ Ask vendor")],
    "spec": [("accept", "✓ Accept"), ("reject", "✕ Reject (not eligible)"), ("ask", "✉ Ask vendor")],
    "tier": [("tier_order", "Per order (not applied)"), ("tier_year", "Per year (apply)"), ("ask", "✉ Ask vendor")],
    "freight": [("confirm", "✓ Accept imputed freight"), ("enter_freight", "✎ Enter vendor's freight"), ("ask", "✉ Ask vendor")],
}
DEFAULT_ACTIONS = [("confirm", "✓ Confirm as read"), ("ask", "✉ Ask vendor")]
DEFAULT_ACTIONS_NO_FIELD = [("confirm", "✓ Acknowledge"), ("ask", "✉ Ask vendor")]


def actions_for(item: dict) -> list[tuple[str, str]]:
    k = item["kind"]
    if k in ("price", "ocr", "same_ly", "kg_basis", "ly_spec", "ly_missing"):
        base = ACTIONS["price"]
        return [(a, l) for a, l in base if a != "correct" or k in ("price", "ocr")]  # only a price/unit can be corrected inline
    if k in ("freight", "freight_unknown"):
        return ACTIONS["freight"]
    if k in ACTIONS:
        return ACTIONS[k]
    return DEFAULT_ACTIONS if item["fields"] else DEFAULT_ACTIONS_NO_FIELD


# ------------------------------------------------------------------ building the items
def _could_change(conn: sqlite3.Connection) -> set[int]:
    """quote_ids that are cheaper than (or equal to) the best awardable price on their line: the existing could-change-the-winner rule."""
    q = conn.execute("""WITH best AS (SELECT line_id, MIN(evaluated_inr_pc) b FROM v_comparison_grid WHERE is_awardable=1 GROUP BY line_id)
        SELECT g.quote_id FROM v_comparison_grid g LEFT JOIN best ON best.line_id=g.line_id
        WHERE g.quote_status='quoted' AND g.qual_status<>'disqualified' AND g.evaluated_inr_pc IS NOT NULL
          AND (best.b IS NULL OR g.evaluated_inr_pc <= best.b)""")
    return {r[0] for r in q}


def _decisions(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    out: dict[str, sqlite3.Row] = {}
    for r in conn.execute("SELECT * FROM review_decisions WHERE undone=0 ORDER BY decision_id"):
        out[r["item_id"]] = r
    return out


def build_items(conn: sqlite3.Connection, rows: list[dict] | None = None) -> list[dict]:
    """Every review item for every vendor, open or not. Item = {id, vendor, scope, kind, title, line_nos, quotes: {quote_id: [fields]},
    state (open/asked/done), could_change, severity, ...}."""
    rows = rows if rows is not None else review_rows(conn)
    cc = _could_change(conn)
    dec = _decisions(conn)
    items: dict[str, dict] = {}
    by_vendor: dict[str, list[dict]] = {}
    for r in rows:
        if r["quote_status"] == "quoted":
            by_vendor.setdefault(r["vendor_code"], []).append(r)

    def add(vendor: str, scope: str, kind: str, title: str, r: dict, fields: tuple[str, ...]) -> None:
        iid = f"{vendor}:{kind}" if scope == "vendor" else f"{vendor}:{kind}:{r['line_no']}"
        it = items.setdefault(iid, {"id": iid, "vendor": vendor, "vendor_name": r["vendor_name"], "scope": scope, "kind": kind, "title": title,
                                    "quotes": {}, "line_nos": [], "fields": set(), "qual_status": r["qual_status"]})
        it["quotes"].setdefault(r["quote_id"], set()).update(fields)
        it["fields"].update(fields)
        if r["line_no"] not in it["line_nos"]:
            it["line_nos"].append(r["line_no"])

    for vendor, vrows in by_vendor.items():
        n_flag: dict[str, int] = {}
        for r in vrows:
            for f in set(r["flags"]):
                n_flag[f] = n_flag.get(f, 0) + 1
        claimed: set[tuple[int, str]] = set()
        for table, scope_rule in ((LINE_FLAGS, "line"), (VENDOR_FLAGS, "vendor_if_wide")):
            for flag, (kind, title, fields) in table.items():
                wide = scope_rule == "vendor_if_wide" and n_flag.get(flag, 0) >= VENDOR_WIDE_MIN
                for r in vrows:
                    if flag not in r["flags"]:
                        continue
                    own = tuple(f for f in fields if f in r["fields"] and (r["quote_id"], f) not in claimed)
                    if scope_rule == "vendor_if_wide" and not wide:   # a single line with a vendor-type flag stays a line item
                        add(vendor, "line", kind, title, r, own)
                    else:
                        add(vendor, "line" if scope_rule == "line" else "vendor", kind, title, r, own)
                    claimed.update((r["quote_id"], f) for f in own)
        # whatever is still open and unexplained by a flag: one vendor item ("other values read from the photo/PDF/...")
        src = {"photo": "photo", "pdf": "PDF", "excel": "Excel file", "word": "Word file", "email_text": "email"}
        for r in vrows:
            left = tuple(f for f in r["open_fields"] if (r["quote_id"], f) not in claimed)
            if left:
                add(vendor, "vendor", "terms_read", f"Other values read from the {src.get(r['source_type'], 'file')}", r, left)

    for it in items.values():
        qids = list(it["quotes"])
        rs = [r for r in rows if r["quote_id"] in it["quotes"]]
        open_fields = [(r["quote_id"], f) for r in rs for f in it["quotes"][r["quote_id"]] if r["fields"].get(f, {}).get("status") in OPEN]
        d = dec.get(it["id"])
        it["fields"] = sorted(it["fields"])
        it["quotes"] = {q: sorted(f) for q, f in it["quotes"].items()}
        it["open_fields"] = open_fields
        it["decision"] = dict(d) if d is not None else None
        if it["kind"] in DECISION_ONLY and not open_fields:
            it["state"] = "done" if d is not None else "open"
        elif open_fields:
            it["state"] = "asked" if d is not None and d["action"] == "ask" else "open"
        else:
            it["state"] = "done"
        it["could_change"] = any(q in cc for q in qids)
        confs = [r["fields"][f]["confidence"] for r in rs for f in it["quotes"][r["quote_id"]] if f in r["fields"]]
        musts = any(r["fields"][f].get("status") == "must_confirm" for r in rs for f in it["quotes"][r["quote_id"]] if f in r["fields"])
        it["min_confidence"] = min(confs) if confs else None
        it["severity"] = "green" if it["state"] == "done" else ("red" if musts and (it["min_confidence"] or 1) < 0.80 else "yellow")
        it["line_nos"] = sorted(n for n in it["line_nos"] if n is not None)
        it["first_row"] = rs[0] if rs else None
    # a corrected or confirmed item may no longer carry its flag (the recompute cleared it): keep it, as done, from the decision record
    titles = {k: t for k, t, _ in list(LINE_FLAGS.values()) + list(VENDOR_FLAGS.values())}
    titles["terms_read"] = "Other values read from the file"
    by_q = {r["quote_id"]: r for r in rows}
    for iid, d in dec.items():
        if iid in items or d["kind"] == "notquoted":
            continue
        parts = iid.split(":")
        qids = sorted({u["quote"] for u in json.loads(d["undo_json"] or "[]") if u.get("quote") in by_q})
        want = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else None
        first = by_q[qids[0]] if qids else next((r for r in by_vendor.get(d["vendor_code"], []) if want is None or r["line_no"] == want), None)
        if first is None:
            continue
        items[iid] = {"id": iid, "vendor": d["vendor_code"], "vendor_name": first["vendor_name"], "scope": "line" if len(parts) == 3 else "vendor",
                      "kind": d["kind"], "title": titles.get(d["kind"], d["kind"]), "quotes": {q: [] for q in qids} if qids else ({first["quote_id"]: []} if len(parts) == 3 else {}),
                      "line_nos": [int(parts[2])] if len(parts) == 3 and parts[2].isdigit() else sorted(by_q[q]["line_no"] for q in qids if by_q[q]["line_no"] is not None),
                      "fields": [], "qual_status": first["qual_status"], "open_fields": [], "decision": dict(d), "state": "done", "could_change": False,
                      "min_confidence": None, "severity": "green", "first_row": first}
    out = list(items.values())
    out.sort(key=lambda i: (i["qual_status"] == "disqualified", i["scope"] != "vendor", not i["could_change"], i["severity"] != "red",
                            i["line_nos"][0] if i["line_nos"] else 0, i["kind"]))
    return out


KIND_RANK = ["ocr", "price", "freight_unknown", "spec", "ly_spec", "ly_missing", "tier"]


def build_cards(items: list[dict]) -> list[dict]:
    """ONE card per vendor-term item and ONE card per line: several issues on the same line (price + spec) are merged. The card's
    id is that of its first open issue (the most important one), whose actions the card offers; resolved issues stay listed."""
    cards: list[dict] = []
    by_line: dict[tuple, list[dict]] = {}
    for it in items:
        if it["scope"] == "line" and it["line_nos"]:
            by_line.setdefault((it["vendor"], it["line_nos"][0]), []).append(it)
        else:
            cards.append({**it, "issues": [it], "primary": it, "card_id": it["id"]})
    for (_v, _ln), grp in by_line.items():
        grp.sort(key=lambda i: KIND_RANK.index(i["kind"]) if i["kind"] in KIND_RANK else 99)
        open_ = [i for i in grp if i["state"] == "open"]
        asked = [i for i in grp if i["state"] == "asked"]
        prim = (open_ or asked or grp)[0]
        state = "open" if open_ else "asked" if asked else "done"
        sev = "red" if any(i["severity"] == "red" for i in grp if i["state"] != "done") else ("yellow" if open_ or asked else "green")
        cards.append({**prim, "issues": grp, "primary": prim, "card_id": prim["id"], "state": state, "severity": sev,
                      "could_change": any(i["could_change"] for i in grp), "title": " + ".join(dict.fromkeys(i["title"].lower() for i in grp))})
    vorder = ["payment", "cash_discount", "freight", "kg_basis", "fx", "gst", "tax_basis", "price_firm", "same_ly", "msme_cat", "terms_read"]
    cards.sort(key=lambda c: (c["qual_status"] == "disqualified", c["scope"] != "vendor", not c["could_change"] if c["scope"] == "line" else False,
                              vorder.index(c["kind"]) if c["kind"] in vorder else 50, c["severity"] != "red", c["line_nos"][0] if c["line_nos"] else 0, c["kind"]))
    return cards


def open_item_count(conn: sqlite3.Connection, rows: list[dict] | None = None) -> int:
    """'N items awaiting review': open cards across all vendors, after merging issues on the same line (a card waiting on a vendor reply counts as handled)."""
    return sum(c["state"] == "open" for c in build_cards(build_items(conn, rows)))


def vendor_progress(items: list[dict], vendor: str) -> tuple[int, int]:
    mine = [c for c in build_cards([i for i in items if i["vendor"] == vendor])]
    return sum(c["state"] != "open" for c in mine), len(mine)


# ------------------------------------------------------------------ clean lines
def check_counts(r: dict) -> tuple[int, int]:
    v = r["validation"] or []
    return sum(c["result"] == "pass" for c in v), len(v)


def clean_lines(conn: sqlite3.Connection, vendor: str, rows: list[dict] | None = None, items: list[dict] | None = None) -> tuple[list[dict], list[dict], dict[int, set]]:
    """(clean rows, excluded rows, fields to skip per quote). Clean = quoted, every check passed or skipped, no open value outside a
    vendor-term item, and not in an open LINE item. Fields that belong to an open vendor-term item (e.g. the 45-day payment cap) are
    never confirmed in bulk: they keep waiting for that item's decision."""
    rows = rows if rows is not None else review_rows(conn)
    items = items if items is not None else build_items(conn, rows)
    mine = [i for i in items if i["vendor"] == vendor and i["state"] != "done"]
    line_busy = {q for i in mine if i["scope"] == "line" for q in i["quotes"]}
    skip: dict[int, set] = {}
    for i in mine:
        if i["scope"] == "vendor":
            for q, f in i["open_fields"]:
                skip.setdefault(q, set()).add(f)
            if i["kind"] in DECISION_ONLY:
                for q, fs in i["quotes"].items():
                    skip.setdefault(q, set()).update(fs)
    clean, excluded = [], []
    for r in rows:
        if r["vendor_code"] != vendor or r["quote_status"] != "quoted":
            continue
        bad = [c for c in (r["validation"] or []) if c["result"] in ("fail", "warn")]
        foreign_open = [f for f in r["open_fields"] if f not in skip.get(r["quote_id"], set())]
        if foreign_open or r["quote_id"] in line_busy or bad:
            excluded.append(r)
        elif any(f.get("status") != "confirmed" for k, f in r["fields"].items() if k not in skip.get(r["quote_id"], set())):
            clean.append(r)
    return clean, excluded, skip


# ------------------------------------------------------------------ wording with the real numbers
def _money(x: float | None) -> str:
    return "n/a" if x is None else f"₹{x:,.2f}"


def suggested_unit(r: dict) -> str | None:
    for c in r["validation"] or []:
        if c["check"] == "unit_hypothesis" and c["result"] == "fail":
            m = re.search(r"per (\d+)", c["detail"])
            if m:
                return {"100": "per_100", "1000": "per_1000"}.get(m.group(1))
    return None


def _vendor_line(conn: sqlite3.Connection, r: dict) -> dict:
    """The vendor's own line as extracted (verbatim texts: construction, remarks), found by the cited source reference."""
    row = conn.execute("SELECT vr.raw_extraction_json FROM quotes q JOIN vendor_responses vr USING (response_id) WHERE q.quote_id=?", (r["quote_id"],)).fetchone()
    if not row or not row[0]:
        return {}
    ref = (r["fields"].get("price", {}).get("source") or {}).get("ref")
    for ln in json.loads(row[0]).get("lines", []):
        if (ln.get("source") or {}).get("ref") == ref or ln.get("raw_price_text") == r["raw_price_text"] and ln.get("vendor_item_text", "")[:8] == (r["vendor_line_ref"] or "")[-8:]:
            return ln
    return {}


def vs_peers(price: float, median: float, cause: str = "") -> str:
    """'104× the other vendors' / '35% below the other vendors (median ₹x/pc)'. Never '1×'."""
    ratio = price / median if median else 0
    if ratio >= 2:
        body = f"**{ratio:,.0f}× the other vendors**"
    elif ratio >= 1.05:
        body = f"**{(ratio - 1) * 100:.0f}% above the other vendors**"
    elif ratio <= 0.95:
        body = f"**{(1 - ratio) * 100:.0f}% below the other vendors**"
    else:
        body = "**in line with the other vendors**"
    return f"This is {body} (median {_money(median)}/pc){cause}."


def spec_read(r: dict) -> str:
    """'Deviation: 150 GSM outer liner vs 180 specified (layers 1 and 5)' / 'Substitute: 3-ply offered, RFQ asks for 5-ply'."""
    note = r["spec_deviation_note"] or ""
    kind = "Substitute" if r["spec_match"] == "substitute" else "Deviation"
    pairs = re.findall(r"layer (\d+): (\d+) GSM offered vs (\d+) specified", note)
    if pairs and len({(o, sp) for _, o, sp in pairs}) == 1:
        layers = [int(x[0]) for x in pairs]
        o, sp = pairs[0][1], pairs[0][2]
        what = "outer liner" if layers[0] == 1 and len(layers) > 1 else "layer"
        lt = " and ".join(map(str, layers)) if len(layers) <= 2 else ", ".join(map(str, layers[:-1])) + f" and {layers[-1]}"
        return f"**{kind}**: {o} GSM {what} vs {sp} specified (layer{'s' if len(layers) > 1 else ''} {lt})"
    return f"**{kind}**: {note}"


def describe(conn: sqlite3.Connection, it: dict, all_rows: list[dict] | None = None) -> dict:
    """{'wrote': str, 'read': str, 'why': str, 'source': {...} | None} in plain language, from the quote data (no flag codes)."""
    r = it["first_row"]
    rows = [x for x in (all_rows if all_rows is not None else review_rows(conn)) if x["quote_id"] in it["quotes"]] if it["scope"] == "vendor" else [r]
    k = it["kind"]
    n = len(it["quotes"])
    src = None
    for f in list(it["fields"]) + ["price", "payment_terms"] + list(r["fields"]):
        s = r["fields"].get(f, {}).get("source")
        if s:
            src = {**s, "field": f}
            break
    wrote = read = why = ""
    if k == "payment":
        q = r["payment_terms_days"]
        eff = r["effective_vendor_days"]
        ms = conn.execute("SELECT msme_category FROM vendors WHERE vendor_code=?", (it["vendor"],)).fetchone()[0]
        wrote = f"“{(src or {}).get('snippet', '')}”"
        read = f"{q} days → evaluated at **{eff} days**"
        why = (f"{it['vendor']} is a {ms} enterprise (vendor master). The MSMED Act caps credit for micro and small vendors at 45 days, "
               f"so the evaluation uses {eff} days, not {q}.")
    elif k == "cash_discount":
        d = next((x for x in r["discounts"] if x.get("condition_type") != "volume_qty"), r["discounts"][0] if r["discounts"] else {})
        wrote = f"“{d.get('condition_text', '')}”"
        read = f"{d.get('pct', 0):g}% · **{'applied' if d.get('applied') else 'not applied'}** (conditional)"
        if d.get("source"):
            src = {"ref": d["source"], "snippet": d.get("condition_text", ""), "field": "discount"}
        why = "Conditional discounts are never applied automatically. Turn it on only if you will meet the condition."
    elif k in ("freight", "freight_unknown"):
        by_ply: dict[int, list[float]] = {}
        for x in rows:
            if x.get("freight_inr_pc") is not None:
                by_ply.setdefault(x["ply"], []).append(x["freight_inr_pc"])
        parts = " / ".join(f"₹{max(v):,.2f}" for _, v in sorted(by_ply.items()))
        plies = " / ".join(str(p) for p in sorted(by_ply))
        wrote = "Nothing about freight." if k == "freight" else "No freight stated."
        imputed = {1: "the highest freight another vendor quoted for the same line", 2: "last year's freight"}.get(r["freight_imputed"], "")
        read = f"**Imputed** {parts} per piece ({plies}-ply)" if parts else "No freight figure available"
        why = (f"Freight is never invented. With none quoted, we use {imputed or 'a reference figure'}, so {it['vendor']} is not favoured."
               if k == "freight" else "No vendor and no last-year figure gives a freight number, so these lines are excluded until freight is confirmed.")
    elif k == "kg_basis":
        wrote = f"“{(src or {}).get('snippet', '')}”" if src else "Price per kg"
        read = f"₹/kg × our theoretical box weight → per piece (e.g. line {r['line_no']}: {_money(r['base_inr_pc'])}/pc)"
        why = "The vendor priced by weight. The per-piece figure depends on our theoretical box weight; the formula is under Evidence."
    elif k == "fx":
        wrote = f"Prices in {r['raw_currency']}"
        read = f"Converted at the dated rate in settings (e.g. line {r['line_no']}: {_money(r['base_inr_pc'])}/pc)"
        why = "The conversion rate is fixed and dated, not live. Confirm that USD pricing is acceptable."
    elif k == "gst":
        wrote = f"“{(src or {}).get('snippet', '')}”" if src else "GST rate"
        read = "A GST rate different from the expected rate"
        why = "GST is stored but never added to the comparison price. Confirm the rate so the invoice will match."
    elif k == "tax_basis":
        wrote = "The reply does not say whether prices include GST."
        read = "Treated as ex-GST (needs your confirmation)"
        why = "If the prices include GST, every line would be about 18% too high in the comparison."
    elif k == "price_firm":
        wrote = "Prices linked to raw-material (kraft paper) costs."
        read = "Comparable today, but not firm"
        why = FLAG_HELP["PRICE_NOT_FIRM"]
    elif k == "same_ly":
        wrote = "“Same as last year”"
        read = f"Last year's price used on {n} line{'s' if n != 1 else ''}"
        why = "These lines were not quoted for this RFQ; the price comes from last year's list. Confirm it is still valid."
    elif k in ("price", "ocr", "ly_spec", "ly_missing"):
        wrote = f"“{r['raw_price_text']}”" + (f" ({r['raw_unit'].replace('_', ' ')})" if r["raw_unit"] else "")
        read = f"{_money(r['base_inr_pc'])} per piece" + (f" · {r['fields'].get('price', {}).get('confidence', 0):.0%} confidence" if k == "ocr" else "")
        failed = [c["detail"] for c in (r["validation"] or []) if c["result"] == "fail" and c["check"] != "unit_hypothesis"]
        peers = next((c["detail"] for c in (r["validation"] or []) if c["check"] == "sanity_vs_peers" and c["result"] == "fail"), "")
        m = re.search(r"median INR ([\d.,]+)", peers)
        if m and r["base_inr_pc"]:
            med = float(m.group(1).replace(",", ""))
            sub_ply = re.search(r"(\d)-ply offered", r["spec_deviation_note"] or "")
            cause = f", because it's a {sub_ply.group(1)}-ply substitute" if r["spec_match"] == "substitute" and sub_ply else ""
            why = vs_peers(r["base_inr_pc"], med, cause)
        else:
            why = ". ".join(d.rstrip(".") for d in failed) + "." if failed else FLAG_HELP.get(next((f for f in r["flags"] if f in LINE_FLAGS), ""), "")
        su = suggested_unit(r)
        if su and r["raw_price"]:
            div = 100 if su == "per_100" else 1000
            why += f" The unit is probably per {div} pieces, which gives **{_money(r['raw_price'] / div)}/pc**."
        if k == "ocr":
            why = FLAG_HELP["LOW_CONFIDENCE_OCR"] + (" " + " ".join(failed) if failed else "") + " Until confirmed, this value cannot be used in the award."
    elif k == "spec":
        vl = _vendor_line(conn, r)
        said = [f"“{t}”" for t in (vl.get("construction_text"), vl.get("remarks")) if t]
        wrote = " · ".join(said) if said else f"“{(src or {}).get('snippet', '').split('|')[-1].strip()}”"
        kind = "Substitute" if r["spec_match"] == "substitute" else "Deviation"
        read = spec_read(r)
        why = (f"A {kind.lower()} can't win until you accept it. If accepted, {_money(r['evaluated_inr_pc'])}/pc becomes eligible for this line.")
    elif k == "tier":
        t = next((x for x in r["discounts"] if x.get("condition_type") == "volume_qty"), {})
        wrote = f"“{t.get('condition_text', '')}”"
        pct, thr = t.get("pct", 0), int(t.get("condition_value") or 0)
        read = f"Not applied: unclear if per order or per year"
        why = (f"Per order: {'reached' if r['typical_order_qty'] >= thr else 'never reached'} (typical order {r['typical_order_qty']:,}). "
               f"Per year: {'reached' if r['annual_qty'] >= thr else 'not reached'} ({r['annual_qty']:,}/year). "
               f"Current {_money(r['evaluated_inr_pc'])}/pc; the {pct:g}% would apply only on a per-year reading.")
    elif k == "terms_read":
        labels = ", ".join(FIELD_LABELS.get(f, f).lower() for f in it["fields"])
        wrote = f"{labels} as read from the file"
        read = f"Taken as extracted on {n} line{'s' if n != 1 else ''}"
        why = "These values were read with lower confidence (or from a low-quality source). Confirm them against the source on the right."
    else:
        wrote = ""
        read = ""
        why = FLAG_HELP.get(next(iter(r["flags"]), ""), "")
    return {"wrote": wrote, "read": read, "why": why, "source": src}


# ------------------------------------------------------------------ decisions and the actions behind them
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(conn: sqlite3.Connection, it: dict, action: str, reason: str, undo: list[dict], detail: str = "", batch: str | None = None) -> int:
    cur = conn.execute("INSERT INTO review_decisions (item_id, vendor_code, kind, action, reason, detail, ts, undo_json, undone, batch) VALUES (?,?,?,?,?,?,?,?,0,?)",
                       (it["id"], it["vendor"], it["kind"], action, reason, detail, _now(), json.dumps(undo), batch))
    conn.commit()
    return cur.lastrowid


def _quote_meta(conn: sqlite3.Connection, quote_id: int) -> dict:
    return actions._meta(conn, quote_id)


def ask_vendor(conn: sqlite3.Connection, vendor: str, question: str, actor: str = "buyer") -> tuple[int, str]:
    """Append the question to the vendor's clarification draft (the one shown in Responses). Returns (response_id, text added)."""
    v = conn.execute("SELECT vendor_id FROM vendors WHERE vendor_code=?", (vendor,)).fetchone()
    r = conn.execute("""SELECT response_id, clarification_draft, clarification_status FROM vendor_responses WHERE vendor_id=? AND clarification_draft IS NOT NULL
                        ORDER BY response_id DESC LIMIT 1""", (v["vendor_id"],)).fetchone()
    added = f"\n\nAdded from Review & Reconcile:\n- {question}"
    if r is None:
        r0 = conn.execute("""SELECT response_id FROM vendor_responses vr WHERE vendor_id=? AND content_role IN ('quote','mixed')
                             AND NOT EXISTS (SELECT 1 FROM vendor_responses n WHERE n.supersedes_response_id=vr.response_id) ORDER BY response_id DESC LIMIT 1""",
                          (v["vendor_id"],)).fetchone()
        rid, text = r0["response_id"], f"Dear Sir/Madam,\n\nWe have a question on your quotation:\n- {question}\n\nRegards,\nCategory Buyer, Sahyadri Consumer Products Ltd"
    else:
        rid, text = r["response_id"], r["clarification_draft"] + added
    conn.execute("UPDATE vendor_responses SET clarification_draft=?, clarification_status=CASE WHEN clarification_status='sent_stub' THEN 'drafted' ELSE 'drafted' END WHERE response_id=?",
                 (text, rid))
    audit.log(conn, "clarification_drafted", ("response", rid), field="clarification_draft", new=question, actor=actor, evidence={"from": "review"})
    conn.commit()
    return rid, question


def question_for(conn: sqlite3.Connection, it: dict) -> str:
    d = describe(conn, it)
    lines = f"line{'s' if len(it['line_nos']) != 1 else ''} {', '.join(map(str, it['line_nos'][:6]))}{'…' if len(it['line_nos']) > 6 else ''}"
    t = it["title"]
    if it["kind"] == "freight":
        return "Please state the freight per box up to our Chakan MIDC plant for 3-ply, 5-ply and 7-ply boxes, or confirm your rates are delivered (FOR Chakan)."
    if it["kind"] == "payment":
        return "Please confirm your credit terms. As a micro/small enterprise the payment period is capped at 45 days under the MSMED Act."
    if it["kind"] == "cash_discount":
        return "Please confirm the conditions of your cash discount offer."
    if it["kind"] in ("price", "ocr"):
        return f"Please confirm the price and the unit (per piece, per 100, per 1000) for {lines}: you wrote {d['wrote']}."
    if it["kind"] == "spec":
        return f"Please confirm the specification offered for {lines} and send supporting test data: {d['read']}."
    if it["kind"] == "tier":
        return f"Is the volume discount on {lines} per order or per year?"
    return f"Please confirm: {t} ({lines}). {d['wrote']}"


def act(conn: sqlite3.Connection, it: dict, action: str, reason: str = "", value: Any = None, apply_to: list[dict] | None = None,
        actor: str = "buyer") -> int:
    """Run one buyer decision on an item. Every change is audited and recomputed by core.actions; a decision row is kept for undo and progress.
    `apply_to`: other items of the same kind and vendor that get the same correction (the 'apply to all' choice)."""
    batch = audit.new_batch_id()
    undo: list[dict] = []
    targets = [it] + [x for x in (apply_to or []) if x["id"] != it["id"]]
    detail = ""

    for t in targets:
        for qid, fields in t["quotes"].items():
            meta = _quote_meta(conn, qid)
            if action == "tier_order":
                for i, d in enumerate(json.loads(conn.execute("SELECT discounts_json FROM quotes WHERE quote_id=?", (qid,)).fetchone()[0] or "[]")):
                    if d.get("condition_type") == "volume_qty" and (d.get("qty_basis") or "unclear") == "unclear":
                        old = actions.set_discount_basis(conn, qid, i, "per_order", reason or "Buyer reads the discount as per order", actor=actor)
                        undo.append({"op": "basis", "quote": qid, "index": i, "prev": old})
            if action in ("confirm", "keep", "tier_order") and fields:
                done = actions.confirm_fields(conn, qid, fields, actor=actor, batch_id=batch)
                undo.append({"op": "unconfirm", "quote": qid, "fields": [f for f in done if f not in (meta.get("confirmed") or {})]})
            elif action == "apply":
                for i, d in enumerate(json.loads(conn.execute("SELECT discounts_json FROM quotes WHERE quote_id=?", (qid,)).fetchone()[0] or "[]")):
                    if d.get("condition_type") != "volume_qty" and not d.get("applied"):
                        actions.toggle_discount(conn, qid, i, True, actor=actor)
                        undo.append({"op": "toggle", "quote": qid, "index": i, "prev": False})
            elif action == "accept":
                actions.accept_spec(conn, qid, reason, actor=actor)
                undo.append({"op": "unspec", "quote": qid})
            elif action == "reject":
                actions.reject_quote(conn, qid, reason, actor=actor)
                undo.append({"op": "unreject", "quote": qid})
            elif action == "correct":
                unit, price = value
                prev = (meta.get("overrides") or {})
                actions.correct_field(conn, qid, "raw_unit", unit, reason, actor=actor)
                actions.correct_field(conn, qid, "raw_price", price if t is it else conn.execute("SELECT raw_price FROM quotes WHERE quote_id=?", (qid,)).fetchone()[0],
                                      reason, actor=actor)
                undo.append({"op": "unoverride", "quote": qid, "columns": ["raw_unit", "raw_price"], "prev": {c: prev.get(c) for c in ("raw_unit", "raw_price")}})
                undo.append({"op": "unconfirm", "quote": qid, "fields": [f for f in (_quote_meta(conn, qid).get("confirmed") or {}) if f not in (meta.get("confirmed") or {})]})
            elif action == "enter_freight":
                prev = meta.get("overrides") or {}
                actions.correct_field(conn, qid, "freight_basis", "extra_quoted", reason, actor=actor)
                actions.correct_field(conn, qid, "raw_freight_value", float(value), reason, actor=actor)
                undo.append({"op": "unoverride", "quote": qid, "columns": ["freight_basis", "raw_freight_value"],
                             "prev": {c: prev.get(c) for c in ("freight_basis", "raw_freight_value")}})
                undo.append({"op": "unconfirm", "quote": qid, "fields": [f for f in (_quote_meta(conn, qid).get("confirmed") or {}) if f not in (meta.get("confirmed") or {})]})
            elif action in ("tier_year",):
                for i, d in enumerate(json.loads(conn.execute("SELECT discounts_json FROM quotes WHERE quote_id=?", (qid,)).fetchone()[0] or "[]")):
                    if d.get("condition_type") == "volume_qty" and (d.get("qty_basis") or "unclear") == "unclear":
                        old = actions.set_discount_basis(conn, qid, i, "annual", reason or "Buyer reads the discount as per year", actor=actor)
                        undo.append({"op": "basis", "quote": qid, "index": i, "prev": old})
    if action == "ask":
        q = value or question_for(conn, it)
        rid, _ = ask_vendor(conn, it["vendor"], q, actor=actor)
        undo.append({"op": "unask", "response": rid, "question": q})
        detail = q
    did = _record(conn, it, action, reason, undo, detail, batch)
    for t in targets[1:]:                      # the other items that got the same correction are done too; undoing the first undoes them all
        _record(conn, t, action, reason, [], detail, batch)
    return did


def undo(conn: sqlite3.Connection, decision_id: int, actor: str = "buyer") -> None:
    """Reverse a decision: restores what the action changed, audits the undo and recomputes."""
    from core.recompute import recompute_quote

    d = conn.execute("SELECT * FROM review_decisions WHERE decision_id=? AND undone=0", (decision_id,)).fetchone()
    if d is None:
        raise ValueError("nothing to undo")
    touched = set()
    for u in json.loads(d["undo_json"]):
        q = u.get("quote")
        if u["op"] == "unconfirm":
            meta = _quote_meta(conn, q)
            for f in u["fields"]:
                (meta.get("confirmed") or {}).pop(f, None)
            actions._save_meta(conn, q, meta)
        elif u["op"] == "unoverride":
            meta = _quote_meta(conn, q)
            for c in u["columns"]:
                if u["prev"].get(c) is None:
                    (meta.get("overrides") or {}).pop(c, None)
                else:
                    meta["overrides"][c] = u["prev"][c]
            actions._save_meta(conn, q, meta)
        elif u["op"] == "toggle":
            discounts = json.loads(conn.execute("SELECT discounts_json FROM quotes WHERE quote_id=?", (q,)).fetchone()[0] or "[]")
            discounts[u["index"]]["applied"] = u["prev"]
            conn.execute("UPDATE quotes SET discounts_json=? WHERE quote_id=?", (json.dumps(discounts), q))
        elif u["op"] == "basis":
            discounts = json.loads(conn.execute("SELECT discounts_json FROM quotes WHERE quote_id=?", (q,)).fetchone()[0] or "[]")
            discounts[u["index"]]["qty_basis"] = u["prev"]
            conn.execute("UPDATE quotes SET discounts_json=? WHERE quote_id=?", (json.dumps(discounts), q))
        elif u["op"] == "unspec":
            conn.execute("UPDATE quotes SET spec_accepted=0 WHERE quote_id=?", (q,))
        elif u["op"] == "unreject":
            meta = _quote_meta(conn, q)
            meta.pop("rejected", None)
            actions._save_meta(conn, q, meta)
        elif u["op"] == "unask":
            r = conn.execute("SELECT clarification_draft FROM vendor_responses WHERE response_id=?", (u["response"],)).fetchone()
            txt = (r[0] or "").replace(f"\n\nAdded from Review & Reconcile:\n- {u['question']}", "")
            conn.execute("UPDATE vendor_responses SET clarification_draft=? WHERE response_id=?", (txt, u["response"]))
        if q:
            touched.add(q)
    conn.execute("UPDATE review_decisions SET undone=1 WHERE decision_id=? OR (batch IS NOT NULL AND batch=?)", (decision_id, d["batch"]))
    audit.log(conn, "review_undo", ("review_decision", decision_id), field=d["action"], old=d["item_id"], new="undone", actor=actor)
    conn.commit()
    for q in touched:
        recompute_quote(conn, q)


def preview_correction(conn: sqlite3.Connection, quote_id: int, unit: str, price: float) -> dict:
    """What the price would be with this unit and price: the same recompute on a throw-away copy of the database. Nothing is saved."""
    from core.recompute import recompute_quote

    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    conn.backup(mem)
    mem.execute("PRAGMA foreign_keys = OFF")
    actions.correct_field(mem, quote_id, "raw_unit", unit, "preview", actor="preview")
    actions.correct_field(mem, quote_id, "raw_price", float(price), "preview", actor="preview")
    r = mem.execute("SELECT base_inr_pc, landed_inr_pc, evaluated_inr_pc FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    out = dict(r)
    mem.close()
    return out
