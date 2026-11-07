"""Extraction cache keyed by (file SHA-256, prompt name + version, model). Committed to the repo so the
hosted app works without API calls. A forced re-run overwrites the entry.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache" / "extractions"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def cache_path(sha: str, prompt: str, version: str, model: str, cache_dir: Path | None = None) -> Path:
    safe_model = model.replace("/", "_")
    return (cache_dir or CACHE_DIR) / f"{sha[:16]}__{prompt}_{version}__{safe_model}.json"


def load(sha: str, prompt: str, version: str, model: str, cache_dir: Path | None = None) -> dict | None:
    p = cache_path(sha, prompt, version, model, cache_dir)
    if not p.exists():
        return None
    entry = json.loads(p.read_text(encoding="utf-8"))
    return entry if entry.get("sha256") == sha else None  # guards against 16-char prefix collisions


def save(sha: str, prompt: str, version: str, model: str, result: Any, meta: dict | None = None,
         cache_dir: Path | None = None) -> Path:
    p = cache_path(sha, prompt, version, model, cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"sha256": sha, "prompt": prompt, "prompt_version": version, "model": model,
             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "meta": meta or {}, "result": result}
    p.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def cached_call(sha: str, prompt: str, version: str, model: str, fn: Callable[[], tuple[Any, dict]],
                force: bool = False, cache_dir: Path | None = None) -> tuple[Any, dict, bool]:
    """Return (result, meta, from_cache). `fn` makes the real call and returns (result, meta)."""
    if not force:
        hit = load(sha, prompt, version, model, cache_dir)
        if hit is not None:
            return hit["result"], hit.get("meta", {}), True
    result, meta = fn()
    save(sha, prompt, version, model, result, meta, cache_dir)
    return result, meta, False
