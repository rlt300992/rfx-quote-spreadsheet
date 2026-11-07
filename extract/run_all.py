"""CLI: extract every reply in data/inbox/manifest.json into the app database.

    python -m extract.run_all            # uses the cache when present, calls Gemini otherwise
    python -m extract.run_all --force    # fresh Gemini calls, overwrites the cache entries
    python -m extract.run_all --no-draft # skip the clarification-email drafts (saves 5 calls)
"""
from __future__ import annotations

import sys
import time

from core.db import connect, db_path, init_db
from core.seed import seed_if_empty
from extract.extract import ingest_all

if __name__ == "__main__":
    force = "--force" in sys.argv
    conn = connect()
    init_db(conn)
    seed_if_empty(conn)
    t = time.time()
    res = ingest_all(conn, force=force, progress=lambda m: print(m, flush=True), draft="--no-draft" not in sys.argv)
    for r in res:
        c = r["calls"]
        print(f"{r['vendor']}: {r['quotes']} quote rows, {r['unmatched']} unmatched, {r['documents']} documents; "
              f"{c['api']} API calls, {c['cache']} from cache")
    print(f"Done in {time.time() - t:.0f}s into {db_path()}")
