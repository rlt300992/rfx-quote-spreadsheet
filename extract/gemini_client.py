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
    from google.genai import types

    key = get_secret("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set (.env or st.secrets)")
    timeout_ms = int(load_config()["gemini"].get("timeout_s", 180)) * 1000
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout_ms))


def is_daily_quota_error(e: Exception) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    msg = str(e)
    return code == 429 and ("PerDay" in msg or "per day" in msg)


def model_chain(task: str | None = None, cfg: dict | None = None) -> list[str]:
    """Models to try for a task, best first, without duplicates: task model, gemini.model, then fallback_models."""
    g = (cfg or load_config())["gemini"]
    chain = [model_for(task, cfg), g["model"], *(g.get("fallback_models") or [])]
    return list(dict.fromkeys(chain))


def with_retry(fn: Callable[[], T], attempts: int = 6, base_delay: float = 2.0) -> T:
    """Call fn(), retrying transient Gemini errors (429/500/503/504) with exponential backoff."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            if is_daily_quota_error(e):  # waiting a few seconds cannot fix a per-day cap: fail fast so callers can fall back
                raise
            # network-level failures (read timeout, dropped connection) are as transient as a 503
            transient = code in RETRY_STATUS or any(
                k in type(e).__name__ for k in ("Timeout", "ConnectError", "ReadError", "RemoteProtocolError", "ConnectionError"))
            if "client has been closed" in str(e):  # the SDK closed the shared session after a failure: rebuild it
                get_client.cache_clear()
                transient = True
            if not transient or i == attempts - 1:
                raise
            time.sleep(base_delay * 2**i)
    raise RuntimeError("unreachable")


def model_for(task: str | None = None, cfg: dict | None = None) -> str:
    """Model ID for a task (config gemini.task_models override), else gemini.model. The only place IDs are read."""
    g = (cfg or load_config())["gemini"]
    return (g.get("task_models") or {}).get(task or "", g["model"])


def generate(contents: Any, model: str | None = None, **config: Any) -> Any:
    """Shared entry point for all Gemini calls: model from config.yaml, cached client, retry on 503."""
    cfg = load_config()["gemini"]
    config.setdefault("temperature", cfg.get("temperature", 0))
    return with_retry(
        lambda: get_client().models.generate_content(
            model=model or cfg["model"], contents=contents, config=config
        )
    )


def file_part(data: bytes, mime_type: str) -> Any:
    """Inline bytes (PDF / JPG) for native multimodal input."""
    from google.genai import types

    return types.Part.from_bytes(data=data, mime_type=mime_type)


def generate_structured(contents: Any, schema: Any, system: str | None = None,
                        max_output_tokens: int | None = None, model: str | None = None) -> tuple[Any, str, dict]:
    """JSON-mode call validated against a pydantic `schema`. Returns (parsed model, raw JSON text, usage).

    Transient API errors are retried with backoff; a response that is not valid JSON for the schema is
    retried once before failing loudly (never silently dropped).
    """
    cfg = load_config()["gemini"]
    config: dict[str, Any] = {
        "response_mime_type": "application/json",
        "response_schema": schema,
        "max_output_tokens": max_output_tokens or cfg.get("max_output_tokens", 32768),
    }
    if system:
        config["system_instruction"] = system
    last: Exception | None = None
    for _ in range(2):
        resp = generate(contents, model=model, **config)
        text = (resp.text or "").strip()
        try:
            parsed = schema.model_validate_json(text)
        except Exception as e:  # truncated / malformed JSON
            last = e
            continue
        usage = {}
        um = getattr(resp, "usage_metadata", None)
        if um is not None:
            usage = {"prompt_tokens": getattr(um, "prompt_token_count", None),
                     "output_tokens": getattr(um, "candidates_token_count", None)}
        return parsed, text, usage
    raise RuntimeError(f"Gemini returned invalid JSON for {schema.__name__}: {last}")


def ping() -> str:
    """Smoke test: returns the model name once Gemini has answered."""
    resp = generate("Reply with the single word: ok")
    if not (resp.text or "").strip():
        raise RuntimeError("Empty response from Gemini")
    return load_config()["gemini"]["model"]
