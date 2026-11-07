"""Gemini client helpers. Model ID comes from config.yaml only."""
from __future__ import annotations

import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_secret(name: str) -> str | None:
    """st.secrets first (Streamlit Cloud), then .env / environment."""
    try:
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    load_dotenv(ROOT / ".env")
    return os.environ.get(name)


T = TypeVar("T")
RETRY_STATUS = {429, 500, 503, 504}


@lru_cache(maxsize=1)
def get_client():
    """One process-wide client. Cached so it is never garbage-collected (which closes its HTTP session)."""
    from google import genai

    key = get_secret("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set (.env or st.secrets)")
    return genai.Client(api_key=key)


def with_retry(fn: Callable[[], T], attempts: int = 5, base_delay: float = 2.0) -> T:
    """Call fn(), retrying transient Gemini errors (429/500/503/504) with exponential backoff."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            if code not in RETRY_STATUS or i == attempts - 1:
                raise
            time.sleep(base_delay * 2**i)
    raise RuntimeError("unreachable")


def generate(contents: Any, **config: Any) -> Any:
    """Shared entry point for all Gemini calls: model from config.yaml, cached client, retry on 503."""
    cfg = load_config()["gemini"]
    config.setdefault("temperature", cfg.get("temperature", 0))
    return with_retry(
        lambda: get_client().models.generate_content(
            model=cfg["model"], contents=contents, config=config
        )
    )


def ping() -> str:
    """Smoke test: returns the model name once Gemini has answered."""
    resp = generate("Reply with the single word: ok")
    if not (resp.text or "").strip():
        raise RuntimeError("Empty response from Gemini")
    return load_config()["gemini"]["model"]
