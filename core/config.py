"""config.yaml loader (single source for FX, weight formula, thresholds)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def _load() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config() -> dict:
    """Return a fresh copy so callers can't mutate the cached config."""
    import copy

    return copy.deepcopy(_load())
