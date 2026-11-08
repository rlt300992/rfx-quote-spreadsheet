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


def build_from_cache(conn: sqlite3.Connection) -> dict:
    """Ingest the five replies from the committed cache (0 API calls) and restore any cached clarification drafts. Used on a cold start and by Reset."""
    from core.config import load_config
    from extract import cache
    from extract.clarify import draft_for_vendor
    from extract.extract import ingest_all

    api = 0
    if list(cache.CACHE_DIR.glob("*.json")):
        res = ingest_all(conn, draft=False, only_missing=True)
        api = sum(r["calls"]["api"] for r in res)
        cfg = load_config()
        for (code,) in conn.execute("SELECT vendor_code FROM vendors ORDER BY vendor_id").fetchall():
            try:
                draft_for_vendor(conn, code, cfg=cfg, cache_only=True)
            except Exception:  # a draft that is not cached is left out, never generated
                pass
    return {"api_calls": api, "quotes": conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]}


def reset_demo(conn: sqlite3.Connection) -> dict:
    """Drop every table (including the append-only audit log, its triggers go first), seed again and re-ingest the five replies from the cache."""
    _drop_everything(conn)
    init_db(conn)
    seed_if_empty(conn)
    create_views(conn)
    return build_from_cache(conn)
