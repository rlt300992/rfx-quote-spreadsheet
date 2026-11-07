"""Append-only audit log. Every buyer action and award goes through log()."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

REASON_REQUIRED = {"override", "spec_accept"}


def new_batch_id() -> str:
    return uuid.uuid4().hex


def _s(v: Any) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)


def log(
    conn: sqlite3.Connection,
    action: str,
    entity: tuple[str, int | None] | str,
    field: str | None = None,
    old: Any = None,
    new: Any = None,
    reason: str | None = None,
    evidence: Any = None,
    batch_id: str | None = None,
    actor: str = "system",
    scenario: Any = None,
    chat_prompt: str | None = None,
) -> int:
    """Insert one audit row. `entity` is (type, id) or 'type:id'. Returns audit_id."""
    if isinstance(entity, str):
        etype, _, eid = entity.partition(":")
        entity = (etype, int(eid) if eid else None)
    from core.db import AUDIT_ACTIONS

    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action '{action}'")
    if action in REASON_REQUIRED and not (reason or "").strip():
        raise ValueError(f"audit action '{action}' requires a reason")
    cur = conn.execute(
        """INSERT INTO audit_log (ts, actor, batch_id, action, entity_type, entity_id, field_name,
                                  old_value, new_value, reason, evidence_json, scenario_json, chat_prompt)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            actor, batch_id, action, entity[0], entity[1], field,
            _s(old), _s(new), reason, _s(evidence), _s(scenario), chat_prompt,
        ),
    )
    return int(cur.lastrowid)
