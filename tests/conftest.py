"""Shared fixtures. The dev DB is built ONCE per session from the answer-key fixture (tests only)."""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from core.db import connect
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
