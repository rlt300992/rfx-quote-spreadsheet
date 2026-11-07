"""DEV-ONLY: build rfx_dev.db from the answer-key fixture so the read-only Bid Analysis grid can be viewed.

    python -m tests.populate_dev_db
    set RFX_DB=rfx_dev.db   (PowerShell: $env:RFX_DB="rfx_dev.db")  then  streamlit run app.py

The real rfx.db is never touched. App code must not import this module.
"""
from pathlib import Path

from core.db import connect
from tests.fixtures_from_answer_key import build_dev_db

if __name__ == "__main__":
    path = Path(__file__).resolve().parent.parent / "rfx_dev.db"
    path.unlink(missing_ok=True)
    conn = build_dev_db(connect(path))
    n = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    print(f"Wrote {path} with {n} quote rows")
