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


def _vector_hits(store: Store, qvec: np.ndarray, kind: str) -> list[int]:
    blob = np.asarray(qvec, dtype=np.float32).tobytes()
    if store.has_vec:
        rows = store.conn.execute(
            "SELECT v.rowid AS id FROM chunks_vec v JOIN chunks c ON c.id = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? AND c.kind = ? ORDER BY v.distance",
            (blob, CANDIDATES * 4, kind),
        ).fetchall()
        return [r["id"] for r in rows][:CANDIDATES]
    rows = store.conn.execute(
        "SELECT id, embedding FROM chunks WHERE kind=?", (kind,)
    ).fetchall()
    if not rows:
        return []
    matrix = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    order = np.argsort(-(matrix @ qvec))[:CANDIDATES]
    return [rows[i]["id"] for i in order]


def _keyword_hits(store: Store, query: str, kind: str) -> list[int]:
    match = _fts_query(query)
    if not match:
        return []
    rows = store.conn.execute(
        "SELECT f.rowid AS id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
        "WHERE chunks_fts MATCH ? AND c.kind = ? ORDER BY bm25(chunks_fts) LIMIT ?",
        (match, kind, CANDIDATES),
    ).fetchall()
    return [r["id"] for r in rows]


def search(
    store: Store, query: str, kind: str = "text", limit: int = 8
) -> list[dict[str, Any]]:
    store.check_embedding_model(
        store.project.models.embedding,
        store.project.models.embedding_dims,
        store.project.models.document_template,
    )
    scores: dict[int, float] = {}
    qvec = embed_query(store.project, query)
    for hits in (_vector_hits(store, qvec, kind), _keyword_hits(store, query, kind)):
        for rank, cid in enumerate(hits):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    ranked = sorted(scores, key=scores.get, reverse=True)[:limit]
    results = []
    for cid in ranked:
        chunk = store.conn.execute("SELECT * FROM chunks WHERE id=?", (cid,)).fetchone()
        source = store.source(chunk["source_id"])
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
    return results


# Locations -------------------------------------------------------------------
PAGE_RE = re.compile(
    r"^(?:pp?\.?\s*)?([\divxlcIVXLC]+)(?:\s*[-\u2013]\s*([\divxlc]+))?$"
)
CHAPTER_RE = re.compile(r"ch(?:apter)?\.?\s*(\d+)(?:\D+(\d+))?", re.IGNORECASE)


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
    if m := PAGE_RE.match(loc):
        first_label, last_label = m.group(1), m.group(2) or m.group(1)
        first = conn.execute(
            "SELECT min(seq) s FROM blocks WHERE source_id=? AND page_label=?",
            (source_id, first_label),
        ).fetchone()
        last = conn.execute(
            "SELECT max(seq) s FROM blocks WHERE source_id=? AND page_label=?",
            (source_id, last_label),
        ).fetchone()
        if first and first["s"] is not None:
            return first["s"], (
                last["s"] if last and last["s"] is not None else first["s"]
            )
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
    span = resolve_location(store, source["id"], loc)
    if span is None:
        return {
            "error": f"Couldn't find location {loc!r} in {source['cite_key']}. "
            "Use the location from a search result, e.g. 'p. 79', 'ch. 4 para 12' "
            "or 'seq:120'."
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
