"""Cold start: an EMPTY database file must self-build from seed + committed cache, offline, and no tab may raise."""
import io

import openpyxl
import pytest

from core.db import connect, init_db
from core.seed import seed_if_empty
from export.excel import build_workbook
from tests.conftest import run_app


def _need_cache():
    from extract import cache
    if len(list(cache.CACHE_DIR.glob("*.json"))) < 25:
        pytest.skip("committed extraction cache not present")


def test_app_cold_start_loads_quotes_from_cache(tmp_path):
    _need_cache()
    db = tmp_path / "empty.db"  # does not exist yet
    at = run_app(db)
    assert not at.exception
    c = connect(db)
    assert c.execute("SELECT COUNT(*) FROM quotes WHERE is_current=1").fetchone()[0] > 100
    assert c.execute("SELECT COUNT(*) FROM vendor_responses").fetchone()[0] >= 5
    c.close()
    assert not [w for w in at.warning if "hit a problem" in w.value]
    assert any("items awaiting review" in w.value for w in at.warning)


def test_seed_only_db_does_not_crash_any_tab(tmp_path, monkeypatch):
    """Quotes absent AND cache unavailable: every tab still renders, the analyst tab says no award yet."""
    from extract import cache
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "no_cache")
    db = tmp_path / "seed_only.db"
    c = connect(db)
    init_db(c)
    seed_if_empty(c)
    c.close()
    at = run_app(db)
    assert not at.exception
    assert not [w for w in at.warning if "hit a problem" in w.value]


def test_empty_workbook_builds_without_award(tmp_path):
    c = connect(tmp_path / "e.db")
    init_db(c)
    seed_if_empty(c)
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(c)))
    assert "NO AWARD PROPOSED YET" in wb["Award"]["A1"].value
    c.close()
