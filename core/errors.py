"""Plain-language messages for Gemini failures. The app never shows a stack trace for a model problem: it says what happened in one line
and carries on with the saved data."""
from __future__ import annotations


def kind(e: BaseException) -> str:
    """quota / busy / timeout / key / other."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    name = type(e).__name__.lower()
    msg = str(e).lower()
    if "api_key" in msg or "gemini_api_key" in msg or "api key" in msg:
        return "key"
    if code == 429 or "quota" in msg or "resource_exhausted" in msg or "exhausted" in msg:
        return "quota"
    if "timeout" in name or "timed out" in msg or "deadline" in msg or code == 504:
        return "timeout"
    if code in (500, 502, 503) or "unavailable" in msg or "overloaded" in msg or "connect" in name or "connection" in msg:
        return "busy"
    return "other"


MESSAGES = {
    "quota": "Gemini's quota is used up for now, so I can't call it. Everything already extracted and every comparison on the other tabs still works.",
    "busy": "Gemini is busy right now (it returned a temporary error). Nothing was lost; try again in a minute.",
    "timeout": "Gemini took too long to answer. Nothing was lost; try again in a minute.",
    "key": "No Gemini key is configured for this app, so the AI parts are off. The saved extraction and all comparisons still work.",
    "other": "Gemini couldn't complete that request. Nothing was lost; try again in a minute.",
}


def friendly(e: BaseException) -> str:
    return MESSAGES[kind(e)]
