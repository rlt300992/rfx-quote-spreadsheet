"""Reset demo: rebuild the database from the seed files and the committed extraction cache. No API calls: the extraction cache is read-only here and
a clarification draft is restored only if it is already cached (a missing one is simply left out)."""
from __future__ import annotations

import sqlite3

from core.db import create_views, init_db
from core.seed import seed_if_empty


def _drop_everything(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall():
        conn.execute(f'DROP VIEW IF EXISTS "{name}"')
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def reset_demo(conn: sqlite3.Connection) -> dict:
    """Drop every table (including the append-only audit log, its triggers go first), seed again and re-ingest the five replies from the cache."""
    from extract import cache
    from extract.extract import ingest_all

    _drop_everything(conn)
    init_db(conn)
    seed_if_empty(conn)
    create_views(conn)
    api = 0
    if list(cache.CACHE_DIR.glob("*.json")):
        res = ingest_all(conn, draft=False)
        api = sum(r["calls"]["api"] for r in res)
        from core.config import load_config
        from extract.clarify import draft_for_vendor

        cfg = load_config()
        for (code,) in conn.execute("SELECT vendor_code FROM vendors ORDER BY vendor_id").fetchall():
            try:
                draft_for_vendor(conn, code, cfg=cfg, cache_only=True)
            except Exception:  # a draft that is not cached is left out, never generated
                pass
    return {"api_calls": api, "quotes": conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]}
