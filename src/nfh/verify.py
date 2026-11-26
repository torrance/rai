"""Check that a quoted passage really appears in a source, and where."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from rapidfuzz import fuzz

from .search import resolve_location, widen
from .store import Store, citation, loc_label

NEAR_THRESHOLD = 95.0

_TRANSLATE = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u00a0": " ",
        "\u2026": "...",
    }
)


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _joined(blocks: list[Any]) -> tuple[str, list[tuple[int, int]]]:
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    for i, b in enumerate(blocks):
        t = normalise(b["text"])
        parts.append(t)
        spans.append((pos, i))
        pos += len(t) + 1
    return " ".join(parts), spans


def _block_at(spans: list[tuple[int, int]], offset: int) -> int:
    index = 0
    for start, i in spans:
        if start <= offset:
            index = i
        else:
            break
    return index


def _search(blocks: list[Any], quote: str) -> dict[str, Any] | None:
    text, spans = _joined(blocks)
    q = normalise(quote).strip("\"' ")
    if not q or not text:
        return None
    at = text.find(q)
    if at >= 0:
        i = _block_at(spans, at)
        return {"status": "exact", "block": blocks[i], "actual": None}
    alignment = fuzz.partial_ratio_alignment(q, text)
    if alignment and alignment.score >= NEAR_THRESHOLD:
        actual = text[alignment.dest_start : alignment.dest_end]
        i = _block_at(spans, alignment.dest_start)
        return {"status": "near", "block": blocks[i], "actual": actual}
    return None


def verify_quote(
    store: Store, source_ref: str | int, quote: str, loc: str | None = None
) -> dict[str, Any]:
    source = store.source(source_ref)
    if source is None:
        return {"status": "not_found", "error": f"No source matches {source_ref!r}."}
    sid = source["id"]
    if loc:
        span = resolve_location(store, sid, loc)
        if span:
            first, last = widen(store, sid, *span, radius=1)
            found = _search(store.blocks(sid, first, last), quote)
            if found:
                where = loc_label([found["block"]])
                return {
                    "status": found["status"],
                    "citation": citation(source, where),
                    "location": where,
                    "actual_wording": found["actual"],
                }
    found = _search(store.blocks(sid), quote)
    if found is None:
        return {
            "status": "not_found",
            "advice": "Don't use this quote. Paraphrase from a passage you've read.",
        }
    where = loc_label([found["block"]])
    status = "elsewhere" if loc else found["status"]
    return {
        "status": status,
        "match": found["status"],
        "citation": citation(source, where),
        "location": where,
        "actual_wording": found["actual"],
    }
