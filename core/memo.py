"""A tiny memo for read-only results that several tabs need in one run (review rows, review items, scenarios).
The key changes whenever the database changes (our own writes via total_changes, other connections via data_version), so a result is never stale."""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

_STORE: dict[tuple, tuple[tuple, Any]] = {}


def db_key(conn: sqlite3.Connection) -> tuple:
    return (id(conn), conn.execute("PRAGMA database_list").fetchone()[2], conn.total_changes, conn.execute("PRAGMA data_version").fetchone()[0])


def memo(conn: sqlite3.Connection, name: str, fn: Callable[[], Any]) -> Any:
    key = db_key(conn)
    hit = _STORE.get((name, id(conn)))
    if hit is not None and hit[0] == key:
        return hit[1]
    val = fn()
    _STORE[(name, id(conn))] = (key, val)
    return val
