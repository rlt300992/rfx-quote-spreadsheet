"""Shared fixtures. The dev DB is built ONCE per session from the answer-key fixture (tests only)."""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from core.db import connect, init_db
from tests.fixtures_from_answer_key import build_dev_db


@pytest.fixture(scope="session")
def dev_db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "dev.db"
    conn = connect(path)
    build_dev_db(conn)
    conn.close()
    return path


@pytest.fixture(scope="session")
def ro(dev_db_path):
    """Shared connection for tests that only READ."""
    conn = connect(dev_db_path)
    yield conn
    conn.close()


@pytest.fixture
def conn(dev_db_path, tmp_path):
    """A private copy for tests that mutate (corrections, toggles, synthetic edge cases)."""
    dst = tmp_path / "copy.db"
    shutil.copy(dev_db_path, dst)
    c = connect(dst)
    yield c
    c.close()


def get_quote(c: sqlite3.Connection, vendor: str, line_no: int) -> sqlite3.Row:
    return c.execute(
        """SELECT q.* FROM quotes q JOIN vendors v USING (vendor_id) JOIN line_items l USING (line_id)
           WHERE v.vendor_code=? AND l.line_no=? AND q.is_current=1""", (vendor, line_no)).fetchone()


# ------------------------------------------------------------------ real extraction, rebuilt offline from the committed cache
@pytest.fixture(scope="session")
def real_db_path(tmp_path_factory):
    """The app database as P2 builds it, rebuilt from cache/extractions (no API calls). Skips if the cache is absent."""
    from extract import cache
    from extract.extract import ingest_all
    from core.seed import seed_if_empty

    if len(list(cache.CACHE_DIR.glob("*.json"))) < 25:
        pytest.skip("committed extraction cache not present")
    path = tmp_path_factory.mktemp("real") / "real.db"
    c = connect(path)
    init_db(c)
    seed_if_empty(c)
    res = ingest_all(c, draft=True)
    assert sum(r["calls"]["api"] for r in res) == 0, "building the test DB must not call the API"
    c.close()
    return path


@pytest.fixture
def real_conn(real_db_path, tmp_path):
    dst = tmp_path / "real_copy.db"
    shutil.copy(real_db_path, dst)
    c = connect(dst)
    yield c, dst
    c.close()


def run_app(db_path, setup=None):
    """Start the real Streamlit app headless against a database file; returns the AppTest after the first run."""
    import os

    import streamlit as st
    from streamlit.testing.v1 import AppTest

    os.environ["RFX_DB"] = str(db_path)
    st.cache_resource.clear()
    at = AppTest.from_file(str(__import__("pathlib").Path(__file__).resolve().parent.parent / "app.py"), default_timeout=90)
    at.session_state["authed"] = True
    if setup:
        setup(at)
    return at.run()
