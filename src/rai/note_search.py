"""Hybrid search over working notes (keyword + vectors, fused by rank).

The ledger at ``.rai/ledger/notes.jsonl`` is the source of truth. This module
keeps a derived, rebuildable index in ``library.db`` and never writes notes
itself. Vectors are also stored as plain BLOBs so search still works when
sqlite-vec is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import numpy as np

from . import ledger
from .config import Project, parse_ref
from .embed import embed_query, embed_texts
from .store import Store

RRF_K = 60
CANDIDATES = 50
# Vectors are normalised, so an L2 distance of d is a cosine similarity of
# 1 - d^2/2. Below this, a "nearest" note is unrelated and only adds noise.
MIN_SIMILARITY = 0.25
_MAX_DISTANCE = (2 - 2 * MIN_SIMILARITY) ** 0.5


def _fts_query(query: str) -> str:
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in terms)


def _embed_text(note: dict[str, Any]) -> str:
    return f"{note['title']}\n\n{note['abstract']}"


def _content_hash(note: dict[str, Any]) -> str:
    payload = {
        key: note.get(key)
        for key in (
            "title",
            "abstract",
            "body",
            "status",
            "sources",
            "evidence",
            "links",
            "revised",
        )
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _embed_hash(note: dict[str, Any]) -> str:
    return hashlib.sha256(_embed_text(note).encode("utf-8")).hexdigest()[:16]


def _delete_row(store: Store, rowid: int) -> None:
    store.conn.execute("DELETE FROM working_notes WHERE rowid=?", (rowid,))
    store.conn.execute("DELETE FROM working_notes_fts WHERE rowid=?", (rowid,))
    if store.has_vec:
        store.conn.execute("DELETE FROM working_notes_vec WHERE rowid=?", (rowid,))


def _upsert(
    store: Store, note: dict[str, Any], vector: np.ndarray | None = None
) -> None:
    blob = (
        np.asarray(vector, dtype=np.float32).tobytes() if vector is not None else None
    )
    existing = store.conn.execute(
        "SELECT rowid, embedding, embed_hash FROM working_notes WHERE note_id=?",
        (note["id"],),
    ).fetchone()
    if existing is None:
        cur = store.conn.execute(
            "INSERT INTO working_notes(note_id, title, abstract, body, status, "
            "sources_json, links_json, revised, content_hash, embed_hash, embedding) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                note["id"],
                note["title"],
                note["abstract"],
                note["body"],
                note["status"],
                json.dumps(note["sources"], ensure_ascii=False),
                json.dumps(note["links"], ensure_ascii=False),
                note["revised"],
                _content_hash(note),
                _embed_hash(note),
                blob,
            ),
        )
        rowid = int(cur.lastrowid)
    else:
        rowid = int(existing["rowid"])
        if blob is not None:
            store.conn.execute(
                "UPDATE working_notes SET note_id=?, title=?, abstract=?, body=?, "
                "status=?, sources_json=?, links_json=?, revised=?, content_hash=?, "
                "embed_hash=?, embedding=? WHERE rowid=?",
                (
                    note["id"],
                    note["title"],
                    note["abstract"],
                    note["body"],
                    note["status"],
                    json.dumps(note["sources"], ensure_ascii=False),
                    json.dumps(note["links"], ensure_ascii=False),
                    note["revised"],
                    _content_hash(note),
                    _embed_hash(note),
                    blob,
                    rowid,
                ),
            )
        else:
            # If the embedded text changed but no new vector could be built,
            # clear the stale one rather than matching the old meaning.
            stale = blob is None and existing["embed_hash"] != _embed_hash(note)
            store.conn.execute(
                "UPDATE working_notes SET note_id=?, title=?, abstract=?, body=?, "
                "status=?, sources_json=?, links_json=?, revised=?, content_hash=?, "
                "embed_hash=?, embedding=? WHERE rowid=?",
                (
                    note["id"],
                    note["title"],
                    note["abstract"],
                    note["body"],
                    note["status"],
                    json.dumps(note["sources"], ensure_ascii=False),
                    json.dumps(note["links"], ensure_ascii=False),
                    note["revised"],
                    _content_hash(note),
                    _embed_hash(note),
                    None if stale else existing["embedding"],
                    rowid,
                ),
            )
            if stale and store.has_vec:
                store.conn.execute(
                    "DELETE FROM working_notes_vec WHERE rowid=?", (rowid,)
                )
    store.conn.execute("DELETE FROM working_notes_fts WHERE rowid=?", (rowid,))
    store.conn.execute(
        "INSERT INTO working_notes_fts(rowid, note_id, title, abstract, body) "
        "VALUES(?,?,?,?,?)",
        (rowid, note["id"], note["title"], note["abstract"], note["body"]),
    )
    if store.has_vec and blob is not None:
        store.conn.execute("DELETE FROM working_notes_vec WHERE rowid=?", (rowid,))
        store.conn.execute(
            "INSERT INTO working_notes_vec(rowid, embedding) VALUES(?, ?)",
            (rowid, blob),
        )


def _set_meta(store: Store, project: Project) -> None:
    parsed = parse_ref(project.models.embedding, field="[models] embedding")
    store.set_meta("wn_embed_ref", str(parsed))
    store.set_meta("wn_embed_dims", str(project.models.embedding_dims))
    store.set_meta("wn_embed_doc_template", project.models.document_template)
    store.conn.commit()


def sync_index(
    project: Project, store: Store | None = None, rebuild: bool = False
) -> dict[str, Any]:
    """Bring the derived note index in line with the ledger.

    Notes are always indexed for keyword search. Vectors are best-effort: if the
    embedding model is unavailable the rows are still written without vectors,
    and the next refresh or search fills them in.
    """
    owns = store is None
    store = store or Store(project)
    try:
        # A settings change makes every stored vector wrong; notes are few, so
        # rebuild rather than compare note by note.
        if not rebuild and _embedding_mismatch(store, project):
            rebuild = True
        if rebuild:
            store.conn.execute("DELETE FROM working_notes")
            store.conn.execute("DELETE FROM working_notes_fts")
            if store.has_vec:
                # Recreate the vector table too: a dimension change leaves the
                # old table in place, and inserts would then fail.
                store.conn.execute("DROP TABLE IF EXISTS working_notes_vec")
                store.conn.execute(
                    "CREATE VIRTUAL TABLE working_notes_vec "
                    f"USING vec0(embedding float[{project.models.embedding_dims}])"
                )
        notes = ledger.notes_state(project)
        existing = {
            row["note_id"]: row
            for row in store.conn.execute("SELECT * FROM working_notes")
        }
        for note_id, row in existing.items():
            if note_id not in notes:
                _delete_row(store, int(row["rowid"]))
        need_vectors = [
            note
            for note in notes.values()
            if (
                (row := existing.get(note["id"])) is None
                or row["embed_hash"] != _embed_hash(note)
                or row["embedding"] is None
            )
        ]
        vectors: dict[str, np.ndarray] = {}
        warning = ""
        if need_vectors:
            try:
                matrix = embed_texts(project, [_embed_text(n) for n in need_vectors])
                vectors = {note["id"]: matrix[i] for i, note in enumerate(need_vectors)}
            except Exception as exc:  # noqa: BLE001 - the index is best-effort
                warning = (
                    "Working-note vectors could not be built "
                    f"({exc}); keyword search still works and `rai refresh` "
                    "will try again."
                )
        for note in notes.values():
            row = existing.get(note["id"])
            vector = vectors.get(note["id"])
            if (
                row is None
                or row["content_hash"] != _content_hash(note)
                or row["embed_hash"] != _embed_hash(note)
                or (vector is not None and row["embedding"] is None)
            ):
                _upsert(store, note, vector)
        if vectors:
            _set_meta(store, project)
        store.conn.commit()
        return {
            "ok": True,
            "indexed": len(notes),
            "embedded": len(vectors),
            "warning": warning,
        }
    finally:
        if owns:
            store.close()


def _embedding_mismatch(store: Store, project: Project) -> str:
    row = store.conn.execute("SELECT count(*) n FROM working_notes").fetchone()
    if not row or not row["n"]:
        return ""
    parsed = parse_ref(project.models.embedding, field="[models] embedding")
    stored_ref = store.get_meta("wn_embed_ref")
    stored_dims = store.get_meta("wn_embed_dims")
    stored_template = store.get_meta("wn_embed_doc_template")
    if stored_ref is None:
        return ""
    if (
        stored_ref == str(parsed)
        and int(stored_dims or 0) == project.models.embedding_dims
        and (stored_template or "{text}") == project.models.document_template
    ):
        return ""
    return (
        f"Working-note index was built with {stored_ref} ({stored_dims} dims, "
        f"document template {stored_template!r}) but the settings now say "
        f"{parsed} ({project.models.embedding_dims} dims, "
        f"{project.models.document_template!r}). Run `rai refresh` to rebuild it."
    )


def index_problem(project: Project, store: Store | None = None) -> str:
    """A description of a missing, stale or mismatched note index, if any."""
    owns = store is None
    store = store or Store(project)
    try:
        notes = ledger.notes_state(project)
        rows = {
            row["note_id"]: str(row["content_hash"])
            for row in store.conn.execute(
                "SELECT note_id, content_hash FROM working_notes"
            )
        }
        if not notes and not rows:
            return ""
        if len(notes) != len(rows) or set(notes) != set(rows):
            return (
                f"Working-note search index has {len(rows)} of {len(notes)} notes; "
                "run `rai refresh`."
            )
        stale = [nid for nid, note in notes.items() if rows[nid] != _content_hash(note)]
        if stale:
            return (
                "Working-note search index is stale for "
                + ", ".join(sorted(stale))
                + "; run `rai refresh`."
            )
        missing = store.conn.execute(
            "SELECT count(*) AS n FROM working_notes WHERE embedding IS NULL"
        ).fetchone()
        if missing and int(missing["n"]):
            return (
                "Working-note search index is missing vectors for "
                f"{int(missing['n'])} note(s); run `rai refresh`."
            )
        return _embedding_mismatch(store, project)
    finally:
        if owns:
            store.close()


def _vector_hits(
    store: Store,
    qvec: np.ndarray,
    pool: int = CANDIDATES,
    active_only: bool = True,
) -> list[int]:
    blob = np.asarray(qvec, dtype=np.float32).tobytes()
    if store.has_vec:
        status_clause = " AND w.status = 'active'" if active_only else ""
        rows = store.conn.execute(
            "SELECT v.rowid AS id, v.distance AS distance FROM working_notes_vec v "
            "JOIN working_notes w ON w.rowid = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ?"
            + status_clause
            + " ORDER BY v.distance",
            (blob, pool * 4),
        ).fetchall()
        return [int(r["id"]) for r in rows if r["distance"] <= _MAX_DISTANCE][:pool]
    status_clause = " AND status = 'active'" if active_only else ""
    rows = store.conn.execute(
        "SELECT rowid AS id, embedding FROM working_notes "
        "WHERE embedding IS NOT NULL" + status_clause
    ).fetchall()
    if not rows:
        return []
    matrix = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    sims = matrix @ qvec
    order = np.argsort(-sims)
    return [int(rows[i]["id"]) for i in order if sims[i] >= MIN_SIMILARITY][:pool]


def _keyword_hits(
    store: Store, query: str, pool: int = CANDIDATES, active_only: bool = True
) -> list[int]:
    match = _fts_query(query)
    if not match:
        return []
    status_clause = " AND w.status = 'active'" if active_only else ""
    rows = store.conn.execute(
        "SELECT f.rowid AS id FROM working_notes_fts f "
        "JOIN working_notes w ON w.rowid = f.rowid "
        "WHERE working_notes_fts MATCH ?"
        + status_clause
        + " ORDER BY bm25(working_notes_fts) LIMIT ?",
        (match, pool),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def _fallback(notes: list[dict[str, Any]], query: str, limit: int) -> list[dict]:
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
    scored = []
    for note in notes:
        words = re.findall(
            r"\w+", f"{note['title']} {note['abstract']} {note['body']}".lower()
        )
        score = sum(words.count(term) for term in terms)
        if score:
            scored.append((score, note))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [_result(note, score / 10.0) for score, note in scored[:limit]]


def _result(note: dict[str, Any], score: float) -> dict[str, Any]:
    return {
        "id": note["id"],
        "title": note["title"],
        "abstract": note["abstract"],
        "status": note["status"],
        "sources": note["sources"],
        "revised": note["revised"],
        "score": round(float(score), 4),
    }


def search_notes(
    project: Project,
    query: str,
    limit: int = 8,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """Hybrid search over working notes, active by default.

    Results carry the title and abstract only; call ``read_note`` for the body.
    Notes are prior thinking, not source evidence.
    """
    limit = max(1, min(int(limit or 8), 20))
    store = Store(project)
    try:
        try:
            sync_index(project, store)
        except Exception:  # noqa: BLE001 - search must still work
            pass
        notes = {
            note["id"]: note
            for note in ledger.notes_state(project).values()
            if include_deleted or note["status"] == "active"
        }
        if not notes:
            return []
        scores: dict[int, float] = {}
        qvec = None
        try:
            qvec = embed_query(project, query)
        except Exception:  # noqa: BLE001 - keyword search still works
            qvec = None
        active_only = not include_deleted
        by_rowid = {
            int(row["rowid"]): row["note_id"]
            for row in store.conn.execute("SELECT rowid, note_id FROM working_notes")
        }
        for hits in (
            _vector_hits(store, qvec, active_only=active_only)
            if qvec is not None
            else [],
            _keyword_hits(store, query, active_only=active_only),
        ):
            for rank, rowid in enumerate(hits):
                scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (RRF_K + rank + 1)
        ranked = sorted(scores, key=scores.get, reverse=True)
        results = []
        for rowid in ranked:
            note_id = by_rowid.get(rowid)
            if note_id not in notes:
                continue
            results.append(_result(notes[note_id], scores[rowid]))
            if len(results) >= limit:
                break
        if results:
            return results
        # The ledger fallback is for a missing index or a failed embedding
        # call, not for a healthy index that genuinely found nothing.
        if by_rowid and qvec is not None:
            return []
        return _fallback(list(notes.values()), query, limit)
    finally:
        store.close()
