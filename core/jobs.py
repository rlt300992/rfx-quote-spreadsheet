"""Background re-extraction jobs. A job's state lives in the DB (table extraction_jobs), so closing a dialog,
a rerun or a page reload never stops or corrupts it: the work runs in a daemon thread with its own connection."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from core.db import connect

STALE_AFTER = timedelta(minutes=30)
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status_of(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM extraction_jobs WHERE vendor_code=?", (code,)).fetchone()


def is_running(conn: sqlite3.Connection, code: str) -> bool:
    r = status_of(conn, code)
    if r is None or r["status"] != "extracting":
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(r["started_at"]) < STALE_AFTER


def any_running(conn: sqlite3.Connection) -> bool:
    return any(is_running(conn, r[0]) for r in conn.execute("SELECT vendor_code FROM extraction_jobs WHERE status='extracting'"))


def recover_interrupted(conn: sqlite3.Connection) -> None:
    """At process start no thread can still be running: anything left 'extracting' was cut off by a restart."""
    conn.execute("UPDATE extraction_jobs SET status='failed', finished_at=?, error='interrupted by an app restart' WHERE status='extracting'", (_now(),))
    conn.commit()


def start(conn: sqlite3.Connection, entries: list[dict], runner: Callable[[sqlite3.Connection, dict], dict] | None = None,
          db_file: str | Path | None = None, wait: bool = False) -> list[str]:
    """Mark each vendor 'extracting' right now, then run the re-extraction in a background thread. Vendors that
    are already extracting are skipped (no double start). Returns the codes actually started."""
    with _lock:
        todo = []
        for e in entries:
            if is_running(conn, e["vendor_code"]):
                continue
            conn.execute("""INSERT INTO extraction_jobs (vendor_code, status, started_at, finished_at, source, error)
                            VALUES (?, 'extracting', ?, NULL, NULL, NULL)
                            ON CONFLICT(vendor_code) DO UPDATE SET status='extracting', started_at=excluded.started_at,
                              finished_at=NULL, source=NULL, error=NULL""", (e["vendor_code"], _now()))
            todo.append(e)
        conn.commit()
    if not todo:
        return []
    if runner is None:
        from extract.extract import ingest_vendor

        def runner(c: sqlite3.Connection, entry: dict) -> dict:
            return ingest_vendor(c, entry, force=True)
    path = db_file or conn.execute("PRAGMA database_list").fetchone()[2]

    def work() -> None:
        c = connect(path)
        c.execute("PRAGMA busy_timeout = 30000")
        try:
            for e in todo:
                code = e["vendor_code"]
                try:
                    res = runner(c, e) or {}
                    api = (res.get("calls") or {}).get("api", 0)
                    c.execute("UPDATE extraction_jobs SET status='extracted', finished_at=?, source=? WHERE vendor_code=?",
                              (_now(), "fresh" if api else "cache", code))
                except Exception as ex:  # the failure is recorded and shown on the card, never swallowed
                    c.rollback()
                    c.execute("UPDATE extraction_jobs SET status='failed', finished_at=?, error=? WHERE vendor_code=?",
                              (_now(), f"{type(ex).__name__}: {ex}"[:500], code))
                c.commit()
        finally:
            c.close()

    t = threading.Thread(target=work, name="reextract", daemon=True)
    t.start()
    if wait:
        t.join()
    return [e["vendor_code"] for e in todo]
