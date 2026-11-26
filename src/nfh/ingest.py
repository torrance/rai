"""Bring sources into a project's index.

Scope comes from ``nfh.toml`` (collection and/or tags). Command-line overrides
apply to one run only and never remove anything from the index.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .chunk import embedding_text, make_chunks
from .config import Project, parse_ref
from .embed import embed_texts
from .extract import SUPPORTED, extract
from .store import Store
from .zotero import Zotero, ZoteroItem

log = logging.getLogger("nfh.ingest")


@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    flagged: list[tuple[str, str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    generated_keys: list[str] = field(default_factory=list)
    placeholder_keys: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique_cite_key(store: Store, wanted: str, own_key: str) -> str:
    candidate, n = wanted, 0
    while True:
        row = store.conn.execute(
            "SELECT key FROM sources WHERE cite_key=?", (candidate,)
        ).fetchone()
        if row is None or row["key"] == own_key:
            return candidate
        n += 1
        candidate = f"{wanted}{chr(ord('a') + n - 1)}"


def _index_file(
    project: Project,
    store: Store,
    meta: dict[str, Any],
    path: Path,
    report: IngestReport,
    pdf_engine: str,
    say: Callable[[str], None],
) -> None:
    digest = sha256(path)
    existing = store.source_by_key(meta["key"])
    old_key = existing["cite_key"] if existing else ""
    label = meta.get("cite_key") or meta["key"]
    if existing and existing["file_sha256"] == digest:
        _refresh_metadata(project, store, existing, meta, report)
        report.unchanged.append(meta.get("cite_key") or label)
        return
    say(f"  extracting {label} ({path.suffix})")
    result = extract(path, pdf_engine=pdf_engine)
    if result.status == "failed" or not result.blocks:
        report.skipped.append((label, result.detail or "no text extracted"))
        return
    for key in ("title", "year", "url"):
        if not meta.get(key) and result.metadata.get(key):
            meta[key] = result.metadata[key]
    if not meta.get("authors") and result.metadata.get("authors"):
        meta["authors"] = result.metadata["authors"]
    meta["cite_key"] = _unique_cite_key(store, meta["cite_key"], meta["key"])
    meta.update(
        file_path=str(path),
        file_sha256=digest,
        status=result.status,
        status_detail=result.detail,
    )
    chunks = make_chunks(
        result.blocks, project.limits.chunk_min_tokens, project.limits.chunk_max_tokens
    )
    say(f"  embedding {len(chunks)} chunks")
    vectors = embed_texts(project, [embedding_text(meta, c) for c in chunks])
    store.write_source(meta, result.blocks, chunks, vectors)
    if old_key and old_key != meta["cite_key"]:
        from .cards import rename_source_notes

        rename_source_notes(project, store, old_key, meta["cite_key"])
        report.renamed.append((old_key, meta["cite_key"]))
    (report.updated if existing else report.added).append(meta["cite_key"])
    if result.status != "ok":
        report.flagged.append((meta["cite_key"], result.status, result.detail))
    log.info(
        "indexed %s (%s blocks, %s chunks)", label, len(result.blocks), len(chunks)
    )


META_FIELDS = ("title", "year", "kind", "doi", "url")
JSON_FIELDS = ("authors", "tags", "collections")


def _refresh_metadata(
    project: Project,
    store: Store,
    existing: Any,
    meta: dict[str, Any],
    report: IngestReport,
) -> None:
    """Pick up metadata edits in Zotero (above all citation keys) without re-reading.

    Better BibTeX may fill in or change a key after the first ingest; the card
    and project note follow it.
    """
    from .cards import rename_source_notes

    new_key = _unique_cite_key(store, meta["cite_key"], meta["key"])
    meta["cite_key"] = new_key
    updates: dict[str, Any] = {
        f: meta[f] for f in META_FIELDS if meta.get(f) and meta[f] != existing[f]
    }
    for f in JSON_FIELDS:
        if f in meta and json.dumps(meta[f]) != existing[f]:
            updates[f] = json.dumps(meta[f])
    old_key = existing["cite_key"]
    if new_key != old_key:
        updates["cite_key"] = new_key
        rename_source_notes(project, store, old_key, new_key)
        report.renamed.append((old_key, new_key))
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        store.conn.execute(
            f"UPDATE sources SET {sets} WHERE id=?", (*updates.values(), existing["id"])
        )
        store.conn.commit()


def _zotero_meta(item: ZoteroItem) -> dict[str, Any]:
    return {
        "key": f"zotero:{item.key}",
        "title": item.title,
        "authors": item.authors,
        "year": item.year,
        "kind": item.kind,
        "cite_key": item.cite_key,
        "doi": item.doi,
        "url": item.url,
        "tags": item.tags,
        "collections": item.collections,
    }


def ingest_zotero(
    project: Project,
    collection: str | None = None,
    tags: list[str] | None = None,
    tag_match: str | None = None,
    all_items: bool = False,
    dry_run: bool = False,
    pdf_engine: str = "docling",
    say: Callable[[str], None] = print,
) -> IngestReport:
    overridden = collection is not None or bool(tags) or all_items
    collection = project.zotero.collection if collection is None else collection
    tags = tags if tags else ([] if all_items else list(project.zotero.tags))
    tag_match = tag_match or project.zotero.tag_match
    if not collection and not tags and not all_items:
        raise ValueError(
            "No Zotero scope. Set [zotero] collection and/or tags in nfh.toml, "
            "pass --tag/--collection, or use --all for the whole library."
        )
    report = IngestReport()
    zotero = Zotero(project.zotero)
    scope = ", ".join(
        part
        for part in (
            f"collection {collection!r}" if collection else "",
            f"tags {tags} ({tag_match})" if tags else "",
        )
        if part
    )
    say(f"Reading Zotero items: {scope or 'whole library'}")
    items = zotero.items(collection=collection, tags=tags, tag_match=tag_match)
    if dry_run:
        for item in items:
            status = item.file.suffix if item.file else f"skip: {item.file_note}"
            report.planned.append(f"{item.cite_key}  {item.title[:60]}  [{status}]")
        return report

    store = Store(project)
    store.check_embedding_model(
        project.models.embedding,
        project.models.embedding_dims,
        project.models.document_template,
    )
    seen: set[str] = set()
    for item in items:
        meta = _zotero_meta(item)
        seen.add(meta["key"])
        if item.key_source == "generated":
            report.generated_keys.append(item.cite_key)
        elif item.key_source == "placeholder":
            report.placeholder_keys.append(item.cite_key)
        if item.file is None:
            report.skipped.append((item.cite_key, item.file_note))
            continue
        try:
            _index_file(project, store, meta, item.file, report, pdf_engine, say)
        except Exception as exc:  # noqa: BLE001 - report and carry on
            log.exception("failed on %s", item.cite_key)
            report.skipped.append((item.cite_key, f"error: {exc}"))

    if not overridden:
        for row in store.sources():
            if row["key"].startswith("zotero:") and row["key"] not in seen:
                store.delete_source(row["id"])
                report.removed.append(row["cite_key"])
        store.conn.commit()
    store.close()
    return report


def _sidecar(path: Path) -> dict[str, Any]:
    side = path.with_name(path.name + ".yaml")
    if side.exists():
        return yaml.safe_load(side.read_text(encoding="utf-8")) or {}
    return {}


def ingest_local(
    project: Project,
    dry_run: bool = False,
    pdf_engine: str = "docling",
    say: Callable[[str], None] = print,
) -> IngestReport:
    report = IngestReport()
    files = sorted(
        p
        for p in project.sources_local_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SUPPORTED
        and p.name.lower() != "readme.md"
    )
    if dry_run:
        report.planned = [project.rel(p) for p in files]
        return report
    store = Store(project)
    store.check_embedding_model(
        project.models.embedding,
        project.models.embedding_dims,
        project.models.document_template,
    )
    seen: set[str] = set()
    for path in files:
        rel = project.rel(path)
        side = _sidecar(path)
        authors = side.get("authors", side.get("author", []))
        if isinstance(authors, str):
            authors = [authors]
        meta = {
            "key": f"local:{rel}",
            "title": side.get("title", path.stem),
            "authors": authors,
            "year": str(side.get("year", side.get("date", "")))[:4],
            "kind": side.get("kind", "note"),
            "cite_key": side.get("cite_key", f"local-{path.stem}".lower()),
            "url": side.get("url", ""),
        }
        seen.add(meta["key"])
        try:
            _index_file(project, store, meta, path, report, pdf_engine, say)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed on %s", rel)
            report.skipped.append((rel, f"error: {exc}"))
    for row in store.sources():
        if row["key"].startswith("local:") and row["key"] not in seen:
            store.delete_source(row["id"])
            report.removed.append(row["cite_key"])
    store.conn.commit()
    store.close()
    return report


def reembed(project: Project, say: Callable[[str], None] = print) -> int:
    """Re-embed every chunk with the configured model, keeping all text."""
    store = Store(project)
    store.dims = project.models.embedding_dims
    rows = store.conn.execute(
        "SELECT c.id, c.heading_path, c.text, s.title, s.authors, s.year "
        "FROM chunks c JOIN sources s ON s.id = c.source_id ORDER BY c.id"
    ).fetchall()
    say(f"Re-embedding {len(rows)} chunks with {project.models.embedding}")
    texts = [
        embedding_text(
            {
                "title": r["title"],
                "authors": yaml.safe_load(r["authors"] or "[]"),
                "year": r["year"],
            },
            {"heading_path": r["heading_path"], "text": r["text"]},
        )
        for r in rows
    ]
    vectors = embed_texts(project, texts) if texts else []
    with store.conn:
        if store.has_vec:
            store.conn.execute("DROP TABLE IF EXISTS chunks_vec")
            store.conn.execute(
                "CREATE VIRTUAL TABLE chunks_vec "
                f"USING vec0(embedding float[{project.models.embedding_dims}])"
            )
        store.dims = project.models.embedding_dims
        for row, vec in zip(rows, vectors, strict=True):
            store.replace_vector(row["id"], vec)
        ref = parse_ref(project.models.embedding)
        store.set_meta("embed_ref", str(ref))
        store.set_meta("embed_model", ref.model)
        store.set_meta("embed_dims", str(project.models.embedding_dims))
        store.set_meta("embed_doc_template", project.models.document_template)
    store.close()
    return len(rows)
