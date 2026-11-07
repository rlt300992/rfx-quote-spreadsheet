"""Map vendor lines to our line_id by size (LxWxH), ply and description similarity (CLAUDE.md step 3).

The vendor's own row numbers and codes are never used. Unmatched lines stay `unmatched`; a catalogue line is
claimed by at most one vendor line (the best-scoring one).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

_DIM_RE = re.compile(r"(\d{2,4})\s*[xX×*]\s*(\d{2,4})\s*[xX×*]\s*(\d{2,4})")
MATCH_THRESHOLD = 0.62


def parse_dims(text: str | None) -> tuple[int, int, int] | None:
    """'600 x 400 x 400', '600X400X400', '600×400×400 mm' -> (600, 400, 400)."""
    m = _DIM_RE.search(text or "")
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def _norm(s: str | None) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"(\d)\s*(kg|g|gm|ml|l|ltr)\b", r"\1\2", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def desc_similarity(a: str | None, b: str | None) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / len(ta | tb)
    return max(SequenceMatcher(None, na, nb).ratio(), jacc)


def dims_score(v: tuple[int, int, int] | None, c: tuple[int, int, int]) -> float:
    if v is None:
        return 0.0
    if v == c:
        return 1.0
    if sorted(v) == sorted(c):
        return 0.9
    if all(abs(a - b) <= max(3, round(0.02 * b)) for a, b in zip(v, c)):
        return 0.7  # inside vs outside dimension, rounding
    return 0.0


@dataclass
class Match:
    index: int                  # index into the vendor lines list
    line_id: int | None
    line_no: int | None
    score: float
    method: str                 # 'size+ply' | 'size' | 'description' | 'none'
    note: str = ""


def _score(vl: dict[str, Any], cat: dict[str, Any]) -> tuple[float, str]:
    v = (vl.get("length_mm"), vl.get("width_mm"), vl.get("height_mm"))
    vd = v if all(v) else parse_dims(vl.get("vendor_item_text")) or parse_dims(vl.get("line_key"))
    c = (cat["length_mm"], cat["width_mm"], cat["height_mm"])
    ds = dims_score(vd, c)
    ply_ok = vl.get("ply") is not None and vl["ply"] == cat["ply"]
    sim = desc_similarity(vl.get("vendor_item_text"), cat["description"])
    if ds > 0:
        total = 0.6 * ds + 0.15 * (1 if ply_ok else 0) + 0.25 * sim
        method = "size+ply" if ply_ok else "size"
        # size match with the wrong ply must still beat nothing, but a same-ply candidate must win
        return (total, method) if ds >= 0.7 else (0.0, "none")
    if sim >= 0.75 and (vl.get("ply") is None or ply_ok):
        return 0.5 * sim + 0.15 * (1 if ply_ok else 0), "description"
    return 0.0, "none"


def match_lines(vendor_lines: Iterable[dict[str, Any]], catalog: Iterable[dict[str, Any]]) -> list[Match]:
    """Greedy best-first one-to-one assignment. `catalog` rows need line_id, line_no, dims, ply, description."""
    vlines = list(vendor_lines)
    cats = [dict(c) for c in catalog]
    cand: list[tuple[float, int, int, str]] = []
    for i, vl in enumerate(vlines):
        for j, c in enumerate(cats):
            sc, method = _score(vl, c)
            if sc >= MATCH_THRESHOLD or (method == "description" and sc >= 0.45):
                cand.append((sc, i, j, method))
    cand.sort(key=lambda t: (-t[0], t[1], t[2]))
    taken_v: dict[int, Match] = {}
    taken_c: set[int] = set()
    for sc, i, j, method in cand:
        if i in taken_v or j in taken_c:
            continue
        taken_v[i] = Match(i, cats[j]["line_id"], cats[j]["line_no"], round(sc, 3), method)
        taken_c.add(j)
    out: list[Match] = []
    for i in range(len(vlines)):
        out.append(taken_v.get(i) or Match(i, None, None, 0.0, "none",
                                           "no catalogue line with this size/ply/description (or it was claimed by a better match)"))
    return out
