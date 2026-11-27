"""Hybrid search (vectors + BM25 merged by reciprocal rank fusion) and reading."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

import numpy as np

from .config import estimate_tokens
from .embed import embed_query
from .store import Store, authors_list, citation, loc_label

RRF_K = 60
CANDIDATES = 50


def _fts_query(query: str) -> str:
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in terms)


def _vector_hits(
    store: Store,
    qvec: np.ndarray,
    kind: str,
    source_ids: list[int] | None = None,
    pool: int | None = CANDIDATES,
    exact: bool = False,
) -> list[int]:
    blob = np.asarray(qvec, dtype=np.float32).tobytes()
    clause, source_args = _source_clause(source_ids)
    if store.has_vec and not exact:
        k = (pool or CANDIDATES) * 4
        rows = store.conn.execute(
            "SELECT v.rowid AS id FROM chunks_vec v JOIN chunks c ON c.id = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? AND c.kind = ?"
            + clause
            + " ORDER BY v.distance",
            (blob, k, kind, *source_args),
        ).fetchall()
        return [r["id"] for r in rows][:pool] if pool else [r["id"] for r in rows]
    rows = store.conn.execute(
        "SELECT c.id, c.embedding FROM chunks c WHERE c.kind = ?" + clause,
        (kind, *source_args),
    ).fetchall()
    if not rows:
        return []
    matrix = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    order = np.argsort(-(matrix @ qvec))
    if pool:
        order = order[:pool]
    return [rows[i]["id"] for i in order]


def _keyword_hits(
    store: Store,
    query: str,
    kind: str,
    source_ids: list[int] | None = None,
    pool: int = CANDIDATES,
) -> list[int]:
    match = _fts_query(query)
    if not match:
        return []
    clause, source_args = _source_clause(source_ids)
    rows = store.conn.execute(
        "SELECT f.rowid AS id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
        "WHERE chunks_fts MATCH ? AND c.kind = ?"
        + clause
        + " ORDER BY bm25(chunks_fts) LIMIT ?",
        (match, kind, *source_args, pool),
    ).fetchall()
    return [r["id"] for r in rows]


def _source_clause(source_ids: list[int] | None) -> tuple[str, list[int]]:
    if not source_ids:
        return "", []
    marks = ", ".join("?" for _ in source_ids)
    return f" AND c.source_id IN ({marks})", list(source_ids)


def _resolve_sources(
    store: Store, sources: list[str] | tuple[str, ...] | None
) -> list[int]:
    if not sources:
        return []
    ids: list[int] = []
    missing: list[str] = []
    for ref in sources:
        source = store.source(ref)
        if source is None:
            missing.append(str(ref))
        elif source["id"] not in ids:
            ids.append(source["id"])
    if missing:
        joined = ", ".join(repr(ref) for ref in missing)
        raise ValueError(f"No source matches {joined}.")
    return ids


def search(
    store: Store,
    query: str,
    kind: str = "text",
    limit: int = 8,
    sources: list[str] | tuple[str, ...] | None = None,
    per_source_limit: int = 0,
) -> list[dict[str, Any]]:
    """Hybrid search, optionally inside named sources and balanced across sources."""
    store.check_embedding_model(
        store.project.models.embedding,
        store.project.models.embedding_dims,
        store.project.models.document_template,
    )
    source_ids = _resolve_sources(store, sources)
    per_source_limit = max(0, per_source_limit)
    pool = max(CANDIDATES, limit * 10) if per_source_limit else CANDIDATES
    exact = bool(source_ids) or kind == "card" or per_source_limit > 0
    vector_pool = None if per_source_limit else pool
    scores: dict[int, float] = {}
    qvec = embed_query(store.project, query)
    for hits in (
        _vector_hits(store, qvec, kind, source_ids, vector_pool, exact),
        _keyword_hits(store, query, kind, source_ids, pool),
    ):
        for rank, cid in enumerate(hits):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    ranked = sorted(scores, key=scores.get, reverse=True)
    results = []
    per_source: dict[int, int] = {}
    for cid in ranked:
        chunk = store.conn.execute("SELECT * FROM chunks WHERE id=?", (cid,)).fetchone()
        source_id = chunk["source_id"]
        if (
            per_source_limit
            and per_source.get(source_id, 0) >= per_source_limit
        ):
            continue
        source = store.source(source_id)
        words = chunk["text"].split()
        snippet = " ".join(words[:150]) + (" ..." if len(words) > 150 else "")
        results.append(
            {
                "citation": citation(source, chunk["loc_label"]),
                "source_id": source["id"],
                "cite_key": source["cite_key"],
                "title": source["title"],
                "authors": authors_list(source),
                "year": source["year"],
                "location": chunk["loc_label"],
                "first_block": chunk["first_block"],
                "heading_path": chunk["heading_path"],
                "snippet": snippet,
            }
        )
        per_source[source_id] = per_source.get(source_id, 0) + 1
        if len(results) >= limit:
            break
    return results


# Locations -------------------------------------------------------------------
PAGE_PREFIX = re.compile(r"^(?:pp?\.?|pages?)\s*(.+)$", re.IGNORECASE)
PAGE_TOKEN = re.compile(r"^([A-Za-z0-9]+)(?:\s*[-\u2013]\s*([A-Za-z0-9]+))?$")
BARE_PAGE_SPAN = re.compile(
    r"^[A-Za-z0-9]+(?:\s*[-\u2013]\s*[A-Za-z0-9]+)?"
    r"(?:\s*,\s*[A-Za-z0-9]+(?:\s*[-\u2013]\s*[A-Za-z0-9]+)?)*$"
)
CHAPTER_RE = re.compile(r"ch(?:apter)?\.?\s*(\d+)(?:\D+(\d+))?", re.IGNORECASE)


def _page_parts(loc: str) -> list[tuple[str, str]] | None:
    # Models often add a sentence-ending period or a parenthetical gloss.
    loc = loc.strip()
    loc = re.sub(r"[.,;:]+$", "", loc).strip()
    loc = re.sub(r"\s*\([^)]*\)\s*$", "", loc).strip()
    match = PAGE_PREFIX.match(loc)
    if match:
        body = match.group(1)
    elif BARE_PAGE_SPAN.match(loc):
        body = loc
    else:
        return None
    parts = []
    for piece in re.split(r"\s*,\s*|\s+(?:and|&)\s+", body.strip()):
        token = PAGE_TOKEN.match(piece.strip())
        if token is None:
            return None
        parts.append((token.group(1), token.group(2) or token.group(1)))
    return parts or None


def _page_index_bounds(
    store: Store,
    source_id: int,
    label: str,
    *,
    start: bool,
    allow_fallback: bool = True,
) -> tuple[int, int] | None:
    """Page indexes carrying ``label``; when it has no blocks, use its neighbours."""
    conn = store.conn
    row = conn.execute(
        "SELECT min(page_index) a, max(page_index) b FROM blocks "
        "WHERE source_id=? AND page_label IS NOT NULL "
        "AND lower(page_label)=lower(?)",
        (source_id, label),
    ).fetchone()
    if row and row["a"] is not None:
        return row["a"], row["b"]
    if not allow_fallback or not label.isdigit():
        return None
    # A citation can name a page with no text blocks (e.g. a full-page figure).
    # Only bridge a small gap; never snap p. 0 or pp. 79-999 to the book's edge.
    target = int(label)
    rows = conn.execute(
        "SELECT DISTINCT page_index, page_label FROM blocks "
        "WHERE source_id=? AND page_index IS NOT NULL",
        (source_id,),
    ).fetchall()
    numeric = [
        (r["page_index"], int(r["page_label"]))
        for r in rows
        if r["page_label"] and r["page_label"].isdigit()
    ]
    if not numeric:
        return None
    numbers = [number for _, number in numeric]
    if target < min(numbers) or target > max(numbers):
        return None
    candidates = [
        index
        for index, number in numeric
        if abs(number - target) <= 2
        and (number >= target if start else number <= target)
    ]
    if not candidates:
        return None
    index = min(candidates) if start else max(candidates)
    return index, index


def _page_part_range(
    store: Store, source_id: int, first_label: str, last_label: str
) -> tuple[int, int, int, int] | None:
    """(first page, last page, first seq, last seq) for one page interval."""
    conn = store.conn
    if first_label == last_label:
        first = _page_index_bounds(
            store, source_id, first_label, start=True, allow_fallback=False
        )
        if first is None:
            return None
        last = first
    else:
        first = _page_index_bounds(
            store, source_id, first_label, start=True, allow_fallback=True
        )
        last = _page_index_bounds(
            store, source_id, last_label, start=False, allow_fallback=True
        )
        if first is None or last is None or last[1] < first[0]:
            return None
    a = conn.execute(
        "SELECT min(seq) s FROM blocks WHERE source_id=? AND page_index=?",
        (source_id, first[0]),
    ).fetchone()
    b = conn.execute(
        "SELECT max(seq) s FROM blocks WHERE source_id=? "
        "AND page_index BETWEEN ? AND ?",
        (source_id, first[0], last[1]),
    ).fetchone()
    if a and a["s"] is not None and b and b["s"] is not None:
        return first[0], last[1], a["s"], b["s"]
    return None


def resolve_location_parts(
    store: Store, source_id: int, loc: str
) -> list[tuple[int, int]] | None:
    """Page intervals in a location, or None when it is not a page location."""
    parts = _page_parts(loc)
    if parts is None:
        return None
    spans = [
        (span[2], span[3])
        for first_label, last_label in parts
        if (
            span := _page_part_range(store, source_id, first_label, last_label)
        )
        is not None
    ]
    return spans or None


def _page_span(store: Store, source_id: int, loc: str) -> tuple[int, int] | None:
    parts = _page_parts(loc)
    if parts is None:
        return None
    ranges = [
        span
        for first_label, last_label in parts
        if (
            span := _page_part_range(store, source_id, first_label, last_label)
        )
        is not None
    ]
    if not ranges:
        return None
    ranges.sort()
    if len(ranges) == 1:
        return ranges[0][2], ranges[0][3]
    # Do not bridge a discontinuous citation from page 5 to page 310.
    if all(
        next_start - last_end <= 1
        for (_, last_end, _, _), (next_start, _, _, _) in zip(
            ranges, ranges[1:], strict=False
        )
    ):
        return ranges[0][2], ranges[-1][3]
    return ranges[0][2], ranges[0][3]


def resolve_location(store: Store, source_id: int, loc: str) -> tuple[int, int] | None:
    """Turn a location string into a (first_seq, last_seq) range of blocks."""
    loc = (loc or "").strip()
    conn = store.conn
    if loc.startswith("seq:"):
        parts = loc[4:].split("-")
        first = int(parts[0])
        return first, int(parts[1]) if len(parts) > 1 else first
    if m := CHAPTER_RE.search(loc):
        chapter = int(m.group(1))
        sql = (
            "SELECT min(seq) a, max(seq) b FROM blocks WHERE source_id=? AND chapter=?"
        )
        args: list[Any] = [source_id, chapter]
        if m.group(2):
            sql += " AND para=?"
            args.append(int(m.group(2)))
        row = conn.execute(sql, args).fetchone()
        if row and row["a"] is not None:
            return row["a"], row["b"]
    span = _page_span(store, source_id, loc)
    if span is not None:
        return span
    if m := re.match(r"^paras?\.?\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?$", loc):
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        row = conn.execute(
            "SELECT min(seq) a, max(seq) b FROM blocks WHERE source_id=? "
            "AND para BETWEEN ? AND ?",
            (source_id, lo, hi),
        ).fetchone()
        if row and row["a"] is not None:
            return row["a"], row["b"]
    if loc:
        row = conn.execute(
            "SELECT seq FROM blocks WHERE source_id=? AND kind='heading' "
            "AND text LIKE ? ORDER BY seq LIMIT 1",
            (source_id, f"%{loc}%"),
        ).fetchone()
        if row:
            return row["seq"], row["seq"] + 20
    return None


def widen(
    store: Store, source_id: int, first: int, last: int, radius: int
) -> tuple[int, int]:
    """Extend a range by ``radius`` pages (PDF) or ``radius * 3`` blocks."""
    conn = store.conn
    edge = conn.execute(
        "SELECT page_index FROM blocks WHERE source_id=? AND seq IN (?, ?)",
        (source_id, first, last),
    ).fetchall()
    pages = [r["page_index"] for r in edge if r["page_index"] is not None]
    if pages:
        row = conn.execute(
            "SELECT min(seq) a, max(seq) b FROM blocks WHERE source_id=? "
            "AND page_index BETWEEN ? AND ?",
            (source_id, min(pages) - radius, max(pages) + radius),
        ).fetchone()
        return row["a"], row["b"]
    return max(0, first - radius * 3), last + radius * 3


def format_blocks(
    blocks: list[sqlite3.Row], max_tokens: int
) -> tuple[str, list[sqlite3.Row], bool]:
    out: list[str] = []
    shown: list[sqlite3.Row] = []
    used = 0
    truncated = False
    last_marker = None
    for b in blocks:
        marker = loc_label([b])
        line = ("## " if b["kind"] == "heading" else "") + b["text"]
        if marker and marker != last_marker:
            line = f"[{marker}] {line}"
            last_marker = marker
        used += estimate_tokens(line)
        if used > max_tokens:
            truncated = True
            break
        out.append(line)
        shown.append(b)
    return "\n\n".join(out), shown, truncated


def outline(store: Store, source_ref: str | int) -> dict[str, Any]:
    """Every extracted heading with a location the agent can read next."""
    source = store.source(source_ref)
    if source is None:
        return {"error": f"No source matches {source_ref!r}."}
    rows = store.conn.execute(
        "SELECT * FROM blocks WHERE source_id=? AND kind='heading' ORDER BY seq",
        (source["id"],),
    ).fetchall()
    headings = []
    lines = []
    for row in rows:
        path = row["heading_path"] or ""
        title = row["text"] or ""
        if path:
            level = path.count(" > ") + 1 - title.count(" > ")
        else:
            level = 1
        level = max(1, level)
        location = loc_label([row]) or f"seq:{row['seq']}"
        headings.append(
            {
                "level": level,
                "title": row["text"],
                "location": location,
                "seq": row["seq"],
            }
        )
        lines.append(f"{'  ' * (level - 1)}- {row['text']} ({location})")
    text = "\n".join(lines) if lines else (
        "No headings were extracted for this source. Use page, paragraph or "
        "seq: locations instead."
    )
    return {
        "source_id": source["id"],
        "cite_key": source["cite_key"],
        "title": source["title"],
        "range": "toc",
        "headings": headings,
        "text": text,
    }


def read_location(
    store: Store,
    source_ref: str | int,
    loc: str,
    radius: int = 2,
    max_tokens: int = 8000,
) -> dict[str, Any]:
    source = store.source(source_ref)
    if source is None:
        return {"error": f"No source matches {source_ref!r}."}
    if (loc or "").strip().casefold() == "toc":
        return outline(store, source_ref)
    span = resolve_location(store, source["id"], loc)
    if span is None:
        return {
            "error": f"Couldn't find location {loc!r} in {source['cite_key']}. "
            "Use the location from a search result, e.g. 'p. 79', 'ch. 4 para 12', "
            "'seq:120', or 'toc' for the source's outline."
        }
    first, last = widen(store, source["id"], *span, radius=radius)
    blocks = store.blocks(source["id"], first, last)
    text, shown, truncated = format_blocks(blocks, max_tokens)
    return {
        "citation": citation(source, loc_label(shown)),
        "source_id": source["id"],
        "title": source["title"],
        "range": f"seq:{blocks[0]['seq']}-{shown[-1]['seq']}" if shown else "",
        "truncated": truncated,
        "text": text,
    }
