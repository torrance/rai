"""The project index: one SQLite file at ``<project>/.nfh/library.db``.

Full text lives in ``blocks`` (reading order, with locations). ``chunks`` group
blocks for retrieval and carry both an FTS5 row and a vector. Vectors are also
kept as plain BLOBs so search still works if sqlite-vec is unavailable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np

from .config import Project, parse_ref

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE,               -- zotero:<itemKey> or local:<relative path>
  title TEXT, authors TEXT, year TEXT, kind TEXT,
  cite_key TEXT, doi TEXT, url TEXT,
  file_path TEXT, file_sha256 TEXT,
  tags TEXT, collections TEXT,
  status TEXT, status_detail TEXT,
  ingested_at TEXT
);
CREATE INDEX IF NOT EXISTS sources_cite_key ON sources(cite_key);

CREATE TABLE IF NOT EXISTS blocks (
  id INTEGER PRIMARY KEY,
  source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
  seq INTEGER,
  kind TEXT,
  heading_path TEXT,
  text TEXT,
  page_index INTEGER,
  page_label TEXT,
  chapter INTEGER,
  para INTEGER,
  anchor TEXT
);
CREATE INDEX IF NOT EXISTS blocks_source_seq ON blocks(source_id, seq);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
  kind TEXT DEFAULT 'text',
  first_block INTEGER, last_block INTEGER,
  heading_path TEXT, text TEXT,
  loc_label TEXT,
  embedding BLOB
);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source_id, kind);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, heading_path, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS units (
  source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
  unit_no INTEGER, first_block INTEGER, last_block INTEGER,
  title TEXT, output_json TEXT, prompt_version TEXT, model TEXT,
  PRIMARY KEY (source_id, unit_no)
);

CREATE TABLE IF NOT EXISTS notes (
  path TEXT PRIMARY KEY,
  kind TEXT,
  input_hashes TEXT,
  model TEXT, prompt_version TEXT, generated_at TEXT
);

CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  chapter TEXT, para INTEGER, opening TEXT,
  claim TEXT, type TEXT,
  cited TEXT,
  status TEXT DEFAULT 'unchecked',
  evidence TEXT,
  checked_at TEXT, chapter_hash TEXT,
  active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _load_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:  # noqa: BLE001 - fall back to NumPy search
        return False


class Store:
    """Thin wrapper around the project's SQLite index."""

    def __init__(self, project: Project, dims: int | None = None) -> None:
        self.project = project
        project.state_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(project.db_path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.has_vec = _load_vec(self.conn)
        self.dims = dims or project.models.embedding_dims
        self.conn.executescript(SCHEMA)
        if self.has_vec:
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec "
                f"USING vec0(embedding float[{self.dims}])"
            )
        self.set_meta("schema_version", SCHEMA_VERSION)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # Meta --------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _set_embedding_meta(
        self, ref: str, dims: int, doc_template: str
    ) -> None:
        parsed = parse_ref(ref, field="[models] embedding")
        self.set_meta("embed_ref", str(parsed))
        self.set_meta("embed_model", parsed.model)
        self.set_meta("embed_dims", str(dims))
        self.set_meta("embed_doc_template", doc_template)
        self.conn.commit()

    def embedding_mismatch(
        self, ref: str, dims: int, doc_template: str = "{text}"
    ) -> str:
        """Return a description of an embedding mismatch, without changing meta."""
        parsed = parse_ref(ref, field="[models] embedding")
        if self.conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is None:
            return ""
        stored_ref = self.get_meta("embed_ref")
        stored_model = self.get_meta("embed_model")
        stored_dims = self.get_meta("embed_dims")
        stored_template = self.get_meta("embed_doc_template")
        if stored_ref is None and stored_model is None:
            return ""
        if stored_template is None:
            stored_template = "{text}"  # indexes built before this was configurable
        if stored_ref is None:
            # An index from before the embedding provider was recorded. It can
            # only have been Ollama, so don't force a needless re-embed.
            matches = (
                parsed.provider == "ollama"
                and stored_model == parsed.model
                and int(stored_dims or 0) == dims
                and stored_template == doc_template
            )
            stored_label = f"ollama/{stored_model}"
        else:
            matches = (
                stored_ref == str(parsed)
                and int(stored_dims or 0) == dims
                and stored_template == doc_template
            )
            stored_label = stored_ref
        if matches:
            return ""
        return (
            f"Index was built with {stored_label} ({stored_dims} dims, document "
            f"template {stored_template!r}) but the settings now say {parsed} "
            f"({dims} dims, {doc_template!r}). "
            "Run `nfh ingest --reembed` to rebuild the vectors."
        )

    def check_embedding_model(
        self, ref: str, dims: int, doc_template: str = "{text}"
    ) -> None:
        """Refuse to mix vectors made with different embedding setups."""
        parsed = parse_ref(ref, field="[models] embedding")
        if self.conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is None:
            self._set_embedding_meta(str(parsed), dims, doc_template)
            return
        problem = self.embedding_mismatch(ref, dims, doc_template)
        if problem:
            raise RuntimeError(problem)
        if self.get_meta("embed_ref") is None:  # upgrade the metadata in place
            self._set_embedding_meta(str(parsed), dims, doc_template)

    # Sources -----------------------------------------------------------------
    def source_by_key(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()

    def source(self, ref: str | int) -> sqlite3.Row | None:
        """Find a source by numeric id, cite key or stored key."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            return self.conn.execute(
                "SELECT * FROM sources WHERE id=?", (int(ref),)
            ).fetchone()
        row = self.conn.execute(
            "SELECT * FROM sources WHERE cite_key=?", (ref,)
        ).fetchone()
        return row or self.source_by_key(str(ref))

    def sources(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM sources ORDER BY cite_key, title"))

    def source_by_doi_or_url(self, doi: str = "", url: str = "") -> sqlite3.Row | None:
        if doi:
            row = self.conn.execute(
                "SELECT * FROM sources WHERE lower(doi)=lower(?)", (doi,)
            ).fetchone()
            if row:
                return row
        if url:
            return self.conn.execute(
                "SELECT * FROM sources WHERE url=?", (url,)
            ).fetchone()
        return None

    def delete_source(self, source_id: int, *, remove_notes: bool = True) -> None:
        source = self.source(source_id)
        chunk_ids = [
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM chunks WHERE source_id=?", (source_id,)
            )
        ]
        self._delete_chunk_index(chunk_ids)
        if remove_notes and source is not None and source["cite_key"]:
            from .cards import card_path, project_note_path

            card = card_path(self.project, source["cite_key"])
            for path in (
                card,
                card.with_suffix(".prev.md"),
                project_note_path(self.project, source["cite_key"]),
            ):
                path.unlink(missing_ok=True)
                self.conn.execute(
                    "DELETE FROM notes WHERE path=?", (self.project.rel(path),)
                )
        self.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def _delete_chunk_index(self, chunk_ids: Sequence[int]) -> None:
        for cid in chunk_ids:
            self.conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
            if self.has_vec:
                self.conn.execute("DELETE FROM chunks_vec WHERE rowid=?", (cid,))

    def write_source(
        self,
        meta: dict[str, Any],
        blocks: Sequence[dict[str, Any]],
        chunks: Sequence[dict[str, Any]],
        vectors: np.ndarray,
    ) -> int:
        """Replace a source and everything derived from its text, atomically."""
        with self.conn:
            existing = self.source_by_key(meta["key"])
            if existing:
                # Replacing the text is not a deletion: keep the cards and
                # notes on disk until the source is rebuilt or actually removed.
                self.delete_source(existing["id"], remove_notes=False)
            cur = self.conn.execute(
                "INSERT INTO sources(key, title, authors, year, kind, cite_key, doi, "
                "url, file_path, file_sha256, tags, collections, status, "
                "status_detail, ingested_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    meta["key"],
                    meta.get("title", ""),
                    json.dumps(meta.get("authors", [])),
                    meta.get("year", ""),
                    meta.get("kind", ""),
                    meta.get("cite_key", ""),
                    meta.get("doi", ""),
                    meta.get("url", ""),
                    meta.get("file_path", ""),
                    meta.get("file_sha256", ""),
                    json.dumps(meta.get("tags", [])),
                    json.dumps(meta.get("collections", [])),
                    meta.get("status", "ok"),
                    meta.get("status_detail", ""),
                    now(),
                ),
            )
            source_id = int(cur.lastrowid)
            self.conn.executemany(
                "INSERT INTO blocks(source_id, seq, kind, heading_path, text, "
                "page_index, page_label, chapter, para, anchor) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        source_id,
                        b["seq"],
                        b.get("kind", "paragraph"),
                        b.get("heading_path", ""),
                        b["text"],
                        b.get("page_index"),
                        b.get("page_label"),
                        b.get("chapter"),
                        b.get("para"),
                        b.get("anchor"),
                    )
                    for b in blocks
                ],
            )
            self._insert_chunks(source_id, chunks, vectors, kind="text")
        return source_id

    def _insert_chunks(
        self,
        source_id: int,
        chunks: Sequence[dict[str, Any]],
        vectors: np.ndarray,
        kind: str,
    ) -> None:
        for chunk, vec in zip(chunks, vectors, strict=True):
            blob = np.asarray(vec, dtype=np.float32).tobytes()
            cur = self.conn.execute(
                "INSERT INTO chunks(source_id, kind, first_block, last_block, "
                "heading_path, text, loc_label, embedding) VALUES(?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    kind,
                    chunk.get("first_block"),
                    chunk.get("last_block"),
                    chunk.get("heading_path", ""),
                    chunk["text"],
                    chunk.get("loc_label", ""),
                    blob,
                ),
            )
            cid = int(cur.lastrowid)
            self.conn.execute(
                "INSERT INTO chunks_fts(rowid, text, heading_path) VALUES(?,?,?)",
                (cid, chunk["text"], chunk.get("heading_path", "")),
            )
            if self.has_vec:
                self.conn.execute(
                    "INSERT INTO chunks_vec(rowid, embedding) VALUES(?, ?)",
                    (cid, blob),
                )

    def replace_card_chunks(
        self,
        source_id: int,
        chunks: Sequence[dict[str, Any]],
        vectors: np.ndarray,
    ) -> None:
        with self.conn:
            ids = [
                r["id"]
                for r in self.conn.execute(
                    "SELECT id FROM chunks WHERE source_id=? AND kind='card'",
                    (source_id,),
                )
            ]
            self._delete_chunk_index(ids)
            self.conn.execute(
                "DELETE FROM chunks WHERE source_id=? AND kind='card'", (source_id,)
            )
            self._insert_chunks(source_id, chunks, vectors, kind="card")

    def all_text_chunks(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT id, source_id, heading_path, text FROM chunks ORDER BY id"
            )
        )

    def replace_vector(self, chunk_id: int, vec: np.ndarray) -> None:
        blob = np.asarray(vec, dtype=np.float32).tobytes()
        self.conn.execute("UPDATE chunks SET embedding=? WHERE id=?", (blob, chunk_id))
        if self.has_vec:
            self.conn.execute("DELETE FROM chunks_vec WHERE rowid=?", (chunk_id,))
            self.conn.execute(
                "INSERT INTO chunks_vec(rowid, embedding) VALUES(?, ?)",
                (chunk_id, blob),
            )

    # Blocks ------------------------------------------------------------------
    def blocks(
        self, source_id: int, first: int | None = None, last: int | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM blocks WHERE source_id=?"
        args: list[Any] = [source_id]
        if first is not None:
            sql += " AND seq>=?"
            args.append(first)
        if last is not None:
            sql += " AND seq<=?"
            args.append(last)
        return list(self.conn.execute(sql + " ORDER BY seq", args))

    # Notes -------------------------------------------------------------------
    def record_note(
        self,
        path: str,
        kind: str,
        input_hashes: dict[str, str],
        model: str,
        prompt_version: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO notes(path, kind, input_hashes, model, prompt_version, "
            "generated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "kind=excluded.kind, input_hashes=excluded.input_hashes, "
            "model=excluded.model, prompt_version=excluded.prompt_version, "
            "generated_at=excluded.generated_at",
            (path, kind, json.dumps(input_hashes), model, prompt_version, now()),
        )
        self.conn.commit()

    def note(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM notes WHERE path=?", (path,)).fetchone()

    def notes(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return list(self.conn.execute("SELECT * FROM notes WHERE kind=?", (kind,)))
        return list(self.conn.execute("SELECT * FROM notes"))


def authors_list(row: sqlite3.Row) -> list[str]:
    try:
        return list(json.loads(row["authors"] or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []


def _field(block: sqlite3.Row | dict[str, Any], key: str) -> Any:
    try:
        return block[key]
    except (KeyError, IndexError):
        return None


def loc_label(blocks: Iterable[sqlite3.Row | dict[str, Any]]) -> str:
    """Human-readable location for a run of blocks, used in citations."""
    items = list(blocks)
    if not items:
        return ""
    labels = [_field(b, "page_label") for b in items if _field(b, "page_label")]
    if labels:
        first, last = labels[0], labels[-1]
        return f"p. {first}" if first == last else f"pp. {first}-{last}"
    chapters = [_field(b, "chapter") for b in items if _field(b, "chapter") is not None]
    paras = [_field(b, "para") for b in items if _field(b, "para") is not None]
    if chapters:
        ch = chapters[0]
        if paras:
            lo, hi = paras[0], paras[-1]
            span = f"para {lo}" if lo == hi else f"paras {lo}-{hi}"
            return f"ch. {ch}, {span}"
        return f"ch. {ch}"
    if paras:
        lo, hi = paras[0], paras[-1]
        return f"para {lo}" if lo == hi else f"paras {lo}-{hi}"
    anchors = [_field(b, "anchor") for b in items if _field(b, "anchor")]
    return f"section '{anchors[0]}'" if anchors else ""


def citation(row: sqlite3.Row, loc: str) -> str:
    key = row["cite_key"] or f"src{row['id']}"
    return f"[@{key}, {loc}]" if loc else f"[@{key}]"
