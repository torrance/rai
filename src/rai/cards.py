"""Source cards: a general card per source plus a project note.

Pipeline: divide into units -> read each unit in full (structured JSON) ->
check every location and quote -> combine -> relate to the project's open
questions -> index card sections for search.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from . import ledger
from .chunk import embedding_text, make_units, marked_text
from .config import PROMPT_VERSION, Project, model_identity, same_model
from .embed import embed_texts
from .llm import chat_json, load_prompt
from .search import resolve_location, resolve_location_parts
from .store import Store, authors_list, now
from .verify import verify_quote

UNIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["claim", "evidence", "location"],
            },
        },
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "definition": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["term", "definition", "location"],
            },
        },
        "passages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "what": {"type": "string"},
                    "why": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["location", "what", "why"],
            },
        },
        "interlocutors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "who": {"type": "string"},
                    "relation": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["who", "relation", "location"],
            },
        },
        "limits": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "claims", "concepts", "passages", "interlocutors"],
}

COMBINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string"},
        "structure": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "part": {"type": "string"},
                    "locations": {"type": "string"},
                    "argument": {"type": "string"},
                },
                "required": ["part", "locations", "argument"],
            },
        },
        "concepts": UNIT_SCHEMA["properties"]["concepts"],
        "claims": UNIT_SCHEMA["properties"]["claims"],
        "passages": UNIT_SCHEMA["properties"]["passages"],
        "interlocutors": UNIT_SCHEMA["properties"]["interlocutors"],
        "limits": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["thesis", "structure", "concepts", "claims", "passages"],
}

RELEVANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevance": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": [
                            "central",
                            "supports",
                            "complicates",
                            "contradicts",
                            "background",
                        ],
                    },
                    "note": {"type": "string"},
                    "read": {"type": "string"},
                },
                "required": ["question", "role", "note", "read"],
            },
        },
        "overall": {"type": "string"},
    },
    "required": ["relevance", "overall"],
}

FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def card_model_signature(project: Project) -> str:
    """Both stages that shape a card; a change in either makes it stale."""
    return (
        f"reading={model_identity(project.models.reading)} "
        f"+ summarising={model_identity(project.models.summarising)}"
    )


def read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, text[match.end() :]


def write_with_frontmatter(path: Path, meta: dict[str, Any], body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    text = f"---\n{head}\n---\n{body.strip()}\n"
    path.write_text(text, encoding="utf-8")
    return text


LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")


def open_questions(project: Project) -> list[str]:
    """Top-level list items under the 'Open questions' heading of project.md.

    A top-level item keeps its indented continuation lines and sub-bullets as
    part of the same question. Non-indented prose ends the list, so bullets in
    a following taxonomy or explanatory paragraph are not numbered as questions.
    """
    if not project.project_md.exists():
        return []
    text = project.project_md.read_text(encoding="utf-8")
    match = re.search(
        r"^#+\s*Open questions\s*$(.*?)(?=^#+\s|\Z)", text, re.MULTILINE | re.DOTALL
    )
    if not match:
        return []
    questions: list[str] = []
    current: list[str] | None = None
    base_indent: int | None = None
    for raw in match.group(1).splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        item = LIST_ITEM.match(raw.strip())
        if current is None:
            if item is None:  # prose before the list is not a question
                continue
            base_indent = indent
            current = [item.group(1).strip()]
            continue
        if item is not None and indent <= (base_indent or 0):
            questions.append(" ".join(current))
            current = [item.group(1).strip()]
            base_indent = min(base_indent or indent, indent)
            continue
        if item is not None or indent > (base_indent or 0):
            current.append(item.group(1).strip() if item else raw.strip())
            continue
        break  # a non-indented paragraph interrupts the list
    if current is not None:
        questions.append(" ".join(current))
    return [q for q in questions if q]


def card_path(project: Project, cite_key: str) -> Path:
    return project.cards_dir / f"{cite_key}.md"


def project_note_path(project: Project, cite_key: str) -> Path:
    return project.sources_notes_dir / f"{cite_key}.md"


# Checks ----------------------------------------------------------------------
def _location_ok(
    store: Store, source_id: int, loc: str, unit: dict | None = None
) -> bool:
    parts = resolve_location_parts(store, source_id, loc)
    if parts is not None:
        if unit is None:
            return True
        return any(
            not (last < unit["first_block"] or first > unit["last_block"])
            for first, last in parts
        )
    span = resolve_location(store, source_id, loc)
    if span is None:
        return False
    if unit is None:
        return True
    first, last = span
    return not (last < unit["first_block"] or first > unit["last_block"])


def _entry_count(output: dict[str, Any]) -> int:
    return sum(
        len(output.get(field, []))
        for field in ("claims", "concepts", "passages", "interlocutors")
    )


def _check_entries(
    store: Store, source_id: int, output: dict[str, Any], unit: dict | None = None
) -> tuple[int, int]:
    ok = dropped = 0
    for field in ("claims", "concepts", "passages", "interlocutors"):
        kept = []
        for entry in output.get(field, []):
            if _location_ok(store, source_id, entry.get("location", ""), unit):
                ok += 1
                kept.append(entry)
            else:
                dropped += 1
        output[field] = kept
    return ok, dropped


def _check_passage_quotes(
    store: Store, source_id: int, output: dict[str, Any]
) -> int:
    dropped = 0
    kept = []
    for passage in output.get("passages", []):
        quote = (passage.get("quote") or "").strip()
        if not quote:
            kept.append(passage)
            continue
        result = verify_quote(store, source_id, quote, passage["location"])
        if result["status"] == "exact":
            kept.append(passage)
        elif result["status"] == "near" and result.get("actual_wording"):
            passage["quote"] = result["actual_wording"]
            kept.append(passage)
        else:
            dropped += 1
    output["passages"] = kept
    return dropped


def _check_structure(store: Store, source_id: int, card: dict[str, Any]) -> int:
    dropped = 0
    kept = []
    for row in card.get("structure", []):
        if _location_ok(store, source_id, row.get("locations", "")):
            kept.append(row)
        else:
            dropped += 1
    card["structure"] = kept
    return dropped


def check_unit(
    store: Store, source_id: int, unit: dict, output: dict[str, Any]
) -> tuple[dict[str, Any], int, int]:
    """Drop entries with impossible locations; verify or drop quotes."""
    ok, dropped = _check_entries(store, source_id, output, unit)
    dropped += _check_passage_quotes(store, source_id, output)
    return output, ok, dropped


def check_combined_card(
    store: Store, source_id: int, card: dict[str, Any]
) -> tuple[dict[str, Any], int, int]:
    """Re-check the model's combined card, which can reintroduce bad entries."""
    ok, dropped = _check_entries(store, source_id, card)
    dropped += _check_structure(store, source_id, card)
    dropped += _check_passage_quotes(store, source_id, card)
    return card, ok, dropped


# Rendering -------------------------------------------------------------------
def _bullets(items: list[dict], fmt: Callable[[dict], str]) -> str:
    return "\n".join(f"- {fmt(i)}" for i in items) or "- (none recorded)"


def render_card(source: Any, card: dict[str, Any]) -> str:
    def cell(text: Any) -> str:
        return str(text).replace("|", "/")

    rows = "\n".join(
        f"| {cell(s['part'])} | {cell(s['locations'])} | {cell(s['argument'])} |"
        for s in card.get("structure", [])
    )

    def passage(p: dict) -> str:
        quote = f' Quote: "{p["quote"]}"' if p.get("quote") else ""
        return f"{p['location']}: {p['what']} Why it matters: {p['why']}.{quote}"

    return "\n\n".join(
        [
            f"# {source['title']}",
            f"## Thesis\n{card.get('thesis', '').strip()}",
            "## Structure\n| Part | Locations | What it argues |\n| --- | --- | --- |\n"
            + rows,
            "## Key concepts\n"
            + _bullets(
                card.get("concepts", []),
                lambda c: f"**{c['term']}**: {c['definition']} ({c['location']})",
            ),
            "## Main claims and evidence\n"
            + _bullets(
                card.get("claims", []),
                lambda c: f"{c['claim']} Evidence: {c['evidence']} ({c['location']})",
            ),
            "## Notable passages\n" + _bullets(card.get("passages", []), passage),
            "## Interlocutors\n"
            + _bullets(
                card.get("interlocutors", []),
                lambda i: f"{i['who']}: {i['relation']} ({i['location']})",
            ),
            "## Limits\n"
            + ("\n".join(f"- {x}" for x in card.get("limits", [])) or "- (none noted)"),
        ]
    )


# Pipeline --------------------------------------------------------------------
def _source_header(source: Any) -> str:
    authors = ", ".join(authors_list(source)) or "unknown author"
    return f"{source['title']} by {authors} ({source['year'] or 'n.d.'})"


COMBINE_BATCH_UNITS = 25


def _compact(value: Any) -> Any:
    """Drop empty values from JSON sent to the combiner; keep every real entry."""
    if isinstance(value, dict):
        return {
            key: compact
            for key, item in value.items()
            if (compact := _compact(item)) not in ("", [], {}, None)
        }
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def _combine_units(
    project: Project,
    source: Any,
    outputs: list[dict[str, Any]],
    say: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Combine unit readings, batching very long books to bound the payload."""
    header = f"Source: {_source_header(source)}"
    card: dict[str, Any] | None = None
    batches = [
        outputs[start : start + COMBINE_BATCH_UNITS]
        for start in range(0, len(outputs), COMBINE_BATCH_UNITS)
    ] or [[]]
    if len(batches) > 1:
        say(f"  combining {len(outputs)} unit readings in {len(batches)} batches")
    for n, batch in enumerate(batches, start=1):
        payload = json.dumps(_compact(batch), ensure_ascii=False)
        if card is None:
            card = chat_json(
                project,
                load_prompt("card_combine"),
                f"{header}\n\nUnit readings (JSON):\n{payload}",
                COMBINE_SCHEMA,
                think=True,
                stage="summarising",
            )
            continue
        say(f"    merging batch {n}/{len(batches)}")
        card = chat_json(
            project,
            load_prompt("card_merge"),
            f"{header}\n\nExisting card (JSON):\n"
            + json.dumps(_compact(card), ensure_ascii=False)
            + f"\n\nFurther unit readings (JSON):\n{payload}"
            + "\n\nReturn the merged card.",
            COMBINE_SCHEMA,
            think=True,
            stage="summarising",
        )
    return card or {}


def build_card(
    project: Project,
    store: Store,
    source: Any,
    say: Callable[[str], None] = print,
    reuse_units: bool = True,
) -> Path:
    store.check_embedding_model(
        project.models.embedding,
        project.models.embedding_dims,
        project.models.document_template,
    )
    blocks = store.blocks(source["id"])
    units = make_units(
        blocks,
        project.limits.unit_min_tokens,
        project.limits.unit_max_tokens,
        project.limits.single_unit_tokens,
    )
    system_unit = load_prompt("card_unit")
    outputs: list[dict[str, Any]] = []
    checked = dropped_total = 0
    for n, unit in enumerate(units):
        cached = store.conn.execute(
            "SELECT * FROM units WHERE source_id=? AND unit_no=?", (source["id"], n)
        ).fetchone()
        if (
            reuse_units
            and cached
            and cached["prompt_version"] == PROMPT_VERSION
            and same_model(cached["model"], project.models.reading)
            and cached["first_block"] == unit["first_block"]
            and cached["last_block"] == unit["last_block"]
        ):
            cached_output = json.loads(cached["output_json"])
            outputs.append(cached_output)
            checked += _entry_count(cached_output)
            continue
        say(
            f"  reading unit {n + 1}/{len(units)}: "
            f"{unit['title']} ({unit['loc_label']})"
        )
        user = (
            f"Source: {_source_header(source)}\n"
            f"Unit {n + 1} of {len(units)}: {unit['title']} ({unit['loc_label']})\n\n"
            f"{marked_text(unit['blocks'])}"
        )
        started = time.monotonic()
        output = chat_json(
            project, system_unit, user, UNIT_SCHEMA, think=True, stage="reading"
        )
        output, ok, dropped = check_unit(store, source["id"], unit, output)
        elapsed = time.monotonic() - started
        checked += ok
        dropped_total += dropped
        output["_unit"] = {"title": unit["title"], "loc": unit["loc_label"]}
        outputs.append(output)
        store.conn.execute(
            "INSERT OR REPLACE INTO units(source_id, unit_no, first_block, last_block, "
            "title, output_json, prompt_version, model) VALUES(?,?,?,?,?,?,?,?)",
            (
                source["id"],
                n,
                unit["first_block"],
                unit["last_block"],
                unit["title"],
                json.dumps(output),
                PROMPT_VERSION,
                model_identity(project.models.reading),
            ),
        )
        store.conn.commit()
        say(
            f"    {ok} locations verified, {dropped} dropped "
            f"({elapsed:.1f}s)"
        )
    store.conn.execute(
        "DELETE FROM units WHERE source_id=? AND unit_no>=?", (source["id"], len(units))
    )

    say("  combining")
    combined = _combine_units(project, source, outputs, say=say)
    combined, ok, dropped = check_combined_card(store, source["id"], combined)
    checked += ok
    dropped_total += dropped
    body = render_card(source, combined)
    path = card_path(project, source["cite_key"])
    if path.exists():
        path.with_suffix(".prev.md").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    meta = {
        "cite_key": source["cite_key"],
        "title": source["title"],
        "authors": authors_list(source),
        "year": source["year"],
        "kind": source["kind"],
        "source_hash": "sha256:" + (source["file_sha256"] or "")[:16],
        "model": card_model_signature(project),
        "prompt_version": PROMPT_VERSION,
        "generated": now()[:10],
        "checks": f"{checked} locations ok, {dropped_total} entries or quotes dropped",
    }
    text = write_with_frontmatter(path, meta, body)
    store.record_note(
        project.rel(path),
        "card",
        {"source": source["file_sha256"] or ""},
        card_model_signature(project),
        PROMPT_VERSION,
    )
    _index_card(project, store, source, combined)
    build_project_note(project, store, source, text, say=say)
    return path


def _index_card(project: Project, store: Store, source: Any, card: dict) -> None:
    store.check_embedding_model(
        project.models.embedding,
        project.models.embedding_dims,
        project.models.document_template,
    )
    sections = [
        (
            "Thesis and structure",
            card.get("thesis", "")
            + "\n"
            + "\n".join(
                f"{s['part']}: {s['argument']}" for s in card.get("structure", [])
            ),
        ),
        (
            "Concepts",
            "\n".join(
                f"{c['term']}: {c['definition']}" for c in card.get("concepts", [])
            ),
        ),
        ("Claims", "\n".join(c["claim"] for c in card.get("claims", []))),
    ]
    chunks = [
        {"heading_path": f"Card > {name}", "text": text, "loc_label": "card"}
        for name, text in sections
        if text.strip()
    ]
    meta = {
        "title": source["title"],
        "authors": authors_list(source),
        "year": source["year"],
    }
    vectors = embed_texts(project, [embedding_text(meta, c) for c in chunks])
    store.replace_card_chunks(source["id"], chunks, vectors)


def build_project_note(
    project: Project,
    store: Store,
    source: Any,
    card_text: str | None = None,
    say: Callable[[str], None] = print,
) -> Path:
    path = project_note_path(project, source["cite_key"])
    card_text = card_text or card_path(project, source["cite_key"]).read_text(
        encoding="utf-8"
    )
    questions = open_questions(project)
    questions_text = "\n".join(questions)
    existing_meta, existing_body = read_frontmatter(path)
    reuse = (
        bool(existing_body)
        and existing_meta.get("card_hash") == text_hash(card_text)
        and existing_meta.get("questions_hash") == text_hash(questions_text)
        and same_model(existing_meta.get("model"), project.models.summarising)
        and store.note_prompt_current(project.rel(path))
    )
    relevance = (
        existing_body.split("\n## Cited in drafts", 1)[0].strip() if reuse else ""
    )
    if not relevance.startswith("## Relevance"):
        if questions:
            say("  relating to the project's open questions")
            rel = chat_json(
                project,
                load_prompt("card_relevance"),
                "Open questions:\n"
                + "\n".join(f"Q{i + 1}: {q}" for i, q in enumerate(questions))
                + f"\n\nSource card:\n{card_text}",
                RELEVANCE_SCHEMA,
                think=True,
                stage="summarising",
            )
            lines = [f"## Relevance\n{rel.get('overall', '').strip()}\n"]
            for r in rel.get("relevance", []):
                lines.append(
                    f"- {r['question']} ({r['role']}): {r['note']} Read: {r['read']}"
                )
            relevance = "\n".join(lines)
        else:
            relevance = "## Relevance\nproject.md has no 'Open questions' section yet."
    cited = _cited_in_drafts(project, source["cite_key"])
    citations_hash = _citations_hash(project, source["cite_key"])
    meta = {
        "cite_key": source["cite_key"],
        "card_hash": text_hash(card_text),
        "questions_hash": text_hash(questions_text),
        "citations_hash": citations_hash,
        "model": model_identity(project.models.summarising),
    }
    write_with_frontmatter(path, meta, f"{relevance}\n\n## Cited in drafts\n{cited}")
    store.record_note(
        project.rel(path),
        "project_note",
        {
            "card": meta["card_hash"],
            "questions": meta["questions_hash"],
            "citations": citations_hash,
        },
        model_identity(project.models.summarising),
        PROMPT_VERSION,
    )
    return path


def _cited_in_drafts(project: Project, cite_key: str) -> str:
    index = project.state_dir / "citations.json"
    if not index.exists():
        return "- (run rai manuscript to fill this in)"
    data = json.loads(index.read_text(encoding="utf-8"))
    uses = data.get(cite_key, {})
    if not uses:
        return "- Not cited yet"
    return "\n".join(f"- {f}: {n} time(s)" for f, n in sorted(uses.items()))


def _citations_hash(project: Project, cite_key: str) -> str:
    index = project.state_dir / "citations.json"
    if not index.exists():
        return ""
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return text_hash(json.dumps(data.get(cite_key, {}), sort_keys=True))


def stale_sources(project: Project, store: Store) -> list[Any]:
    out = []
    for source in store.sources():
        note = store.note(project.rel(card_path(project, source["cite_key"])))
        if note is None:
            out.append(source)
            continue
        hashes = json.loads(note["input_hashes"] or "{}")
        if (
            hashes.get("source") != (source["file_sha256"] or "")
            or note["prompt_version"] != PROMPT_VERSION
            or note["model"] != card_model_signature(project)
        ):
            out.append(source)
    return out


def stale_project_notes(project: Project, store: Store) -> list[Any]:
    questions_hash = text_hash("\n".join(open_questions(project)))
    out = []
    for source in store.sources():
        card = card_path(project, source["cite_key"])
        if not card.exists():
            continue
        note = project_note_path(project, source["cite_key"])
        meta, _ = read_frontmatter(note)
        card_hash = text_hash(card.read_text(encoding="utf-8"))
        if (
            meta.get("card_hash") != card_hash
            or meta.get("questions_hash") != questions_hash
            or meta.get("citations_hash")
            != _citations_hash(project, source["cite_key"])
            or not same_model(meta.get("model"), project.models.summarising)
            or not store.note_prompt_current(project.rel(note))
        ):
            out.append(source)
    return out


def card_diff(project: Project, cite_key: str) -> str:
    current = card_path(project, cite_key)
    previous = current.with_suffix(".prev.md")
    if not previous.exists():
        return "No previous version of this card."
    return "".join(
        difflib.unified_diff(
            previous.read_text(encoding="utf-8").splitlines(keepends=True),
            current.read_text(encoding="utf-8").splitlines(keepends=True),
            fromfile="previous",
            tofile="current",
        )
    )


def read_card(project: Project, cite_key: str, part: str = "both") -> str:
    reading = ledger.reading_statuses(project).get(cite_key)
    out = [
        "Your reading of this source: "
        + (reading["status"] if reading else "not read")
        + (f" ({reading['note']})" if reading and reading["note"] else "")
    ]
    if part in {"general", "both"}:
        path = card_path(project, cite_key)
        out.append(
            path.read_text(encoding="utf-8") if path.exists() else "No card yet."
        )
    if part in {"project", "both"}:
        path = project_note_path(project, cite_key)
        out.append(
            path.read_text(encoding="utf-8")
            if path.exists()
            else "No project note yet."
        )
    return "\n\n---\n\n".join(out)


def rename_source_notes(project: Project, store: Store, old: str, new: str) -> None:
    """Follow a citation key change in Zotero: move the card and project note."""
    pairs = [
        (card_path(project, old), card_path(project, new)),
        (
            card_path(project, old).with_suffix(".prev.md"),
            card_path(project, new).with_suffix(".prev.md"),
        ),
        (project_note_path(project, old), project_note_path(project, new)),
    ]
    reading = ledger.reading_statuses(project).get(old)
    if reading:
        ledger.rename_reading_status(project, old, new)
    for src, dest in pairs:
        if not src.exists():
            continue
        text = src.read_text(encoding="utf-8")
        text = re.sub(
            rf"^cite_key: {re.escape(old)}$", f"cite_key: {new}", text, flags=re.M
        )
        dest.write_text(text, encoding="utf-8")
        src.unlink()
        store.conn.execute(
            "UPDATE notes SET path=? WHERE path=?",
            (project.rel(dest), project.rel(src)),
        )
    store.conn.commit()
