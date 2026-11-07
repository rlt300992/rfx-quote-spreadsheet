"""Buyer actions on quotes. Each writes an audit row and then calls recompute_quote().

They never touch raw columns: corrections are stored as overrides inside field_meta_json.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from core import audit
from core.recompute import OVERRIDABLE, recompute_quote

CONFIRMABLE = ("price", "unit", "currency", "freight", "tax_basis", "lead_time", "moq",
               "payment_terms", "gst_rate", "spec_match")


COLUMN_FIELDS = {"raw_price": ("price",), "raw_unit": ("unit", "price"), "raw_currency": ("currency", "price"),
                 "raw_freight_value": ("freight",), "freight_basis": ("freight",), "tax_basis": ("tax_basis",),
                 "gst_rate_pct": ("gst_rate",), "payment_terms_days": ("payment_terms",),
                 "lead_time_days": ("lead_time",), "moq_qty": ("moq",)}


def _meta(conn: sqlite3.Connection, quote_id: int) -> dict:
    r = conn.execute("SELECT field_meta_json FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if r is None:
        raise KeyError(f"quote {quote_id} not found")
    return json.loads(r["field_meta_json"]) if r["field_meta_json"] else {}


def _save_meta(conn: sqlite3.Connection, quote_id: int, meta: dict) -> None:
    conn.execute("UPDATE quotes SET field_meta_json=? WHERE quote_id=?",
                 (json.dumps(meta, ensure_ascii=False), quote_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def confirm_fields(conn: sqlite3.Connection, quote_id: int, fields: tuple[str, ...] | list[str] | None = None,
                   actor: str = "buyer", batch_id: str | None = None) -> list[str]:
    """Confirm fields (default: every open field). Confirming with open flags is allowed: the UI warns."""
    meta = _meta(conn, quote_id)
    all_fields = meta.get("fields") or {}
    open_fields = [f for f, v in all_fields.items() if v.get("status") in ("must_confirm", "spot_check")]
    targets = [f for f in (fields or open_fields) if f in CONFIRMABLE]
    flags_seen = {f: (all_fields.get(f) or {}).get("flags", []) for f in targets}
    meta.setdefault("confirmed", {})
    for f in targets:
        meta["confirmed"][f] = {"by": actor, "ts": _now()}
    _save_meta(conn, quote_id, meta)
    audit.log(conn, "confirm", ("quote", quote_id), field=",".join(targets), new="confirmed",
              evidence={"open_flags_at_confirm": flags_seen}, actor=actor, batch_id=batch_id)
    recompute_quote(conn, quote_id)
    return targets


def correct_field(conn: sqlite3.Connection, quote_id: int, column: str, value: Any, reason: str,
                  actor: str = "buyer") -> None:
    """Override a raw value (reason required). The raw column keeps the vendor's original."""
    if column not in OVERRIDABLE:
        raise ValueError(f"{column} cannot be overridden; allowed: {sorted(OVERRIDABLE)}")
    row = conn.execute(f"SELECT {column} FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    meta = _meta(conn, quote_id)
    old = (meta.get("overrides") or {}).get(column, {}).get("value", row[column])
    audit.log(conn, "override", ("quote", quote_id), field=column, old=old, new=value, reason=reason, actor=actor)
    meta.setdefault("overrides", {})[column] = {"value": value, "reason": reason, "by": actor, "ts": _now(),
                                                "raw_value": row[column]}
    for fld in COLUMN_FIELDS.get(column, ()):  # correcting a value is also the buyer confirming that field
        meta.setdefault("confirmed", {})[fld] = {"by": actor, "ts": _now(), "via": "correction"}
    _save_meta(conn, quote_id, meta)
    recompute_quote(conn, quote_id)


def accept_spec(conn: sqlite3.Connection, quote_id: int, reason: str, actor: str = "buyer") -> None:
    audit.log(conn, "spec_accept", ("quote", quote_id), field="spec_accepted", old=0, new=1, reason=reason, actor=actor)
    conn.execute("UPDATE quotes SET spec_accepted=1 WHERE quote_id=?", (quote_id,))
    recompute_quote(conn, quote_id)


def toggle_discount(conn: sqlite3.Connection, quote_id: int, index: int, applied: bool,
                    actor: str = "buyer") -> None:
    """D2: the buyer decides, per discount, whether it counts. Audited, then recomputed."""
    row = conn.execute("SELECT discounts_json FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    discounts = json.loads(row["discounts_json"] or "[]")
    old = bool(discounts[index].get("applied"))
    discounts[index]["applied"] = bool(applied)
    audit.log(conn, "discount_toggle", ("quote", quote_id), field=f"discounts[{index}].applied", old=old,
              new=bool(applied), evidence={"discount": discounts[index]}, actor=actor)
    conn.execute("UPDATE quotes SET discounts_json=? WHERE quote_id=?", (json.dumps(discounts), quote_id))
    recompute_quote(conn, quote_id)


def match_unmatched(conn: sqlite3.Connection, quote_id: int, line_no: int, reason: str, actor: str = "buyer") -> None:
    """Attach an unmatched vendor line to one of our lines (reason required, audited). A layer-0 `not_quoted`
    placeholder on that line is superseded; a real quote on it blocks the match."""
    if not (reason or "").strip():
        raise ValueError("a reason is required to match a vendor line")
    q = conn.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
    if q is None or q["quote_status"] != "unmatched":
        raise ValueError("only an unmatched quote can be matched")
    line = conn.execute("SELECT line_id FROM line_items WHERE line_no=?", (line_no,)).fetchone()
    if line is None:
        raise ValueError(f"no such line {line_no}")
    cur = conn.execute("SELECT quote_id, quote_status FROM quotes WHERE vendor_id=? AND line_id=? AND is_current=1",
                       (q["vendor_id"], line["line_id"])).fetchone()
    if cur is not None and cur["quote_status"] != "not_quoted":
        raise ValueError(f"this vendor already has a quote on line {line_no}")
    audit.log(conn, "override", ("quote", quote_id), field="line_id", old=None, new=line["line_id"], reason=reason, actor=actor,
              evidence={"vendor_line_ref": q["vendor_line_ref"], "replaced_placeholder": cur["quote_id"] if cur else None})
    if cur is not None:
        conn.execute("UPDATE quotes SET is_current=0 WHERE quote_id=?", (cur["quote_id"],))
    conn.execute("UPDATE quotes SET line_id=?, quote_status='quoted', supersedes_quote_id=? WHERE quote_id=?",
                 (line["line_id"], cur["quote_id"] if cur else None, quote_id))
    recompute_quote(conn, quote_id)
