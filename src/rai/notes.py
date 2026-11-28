"""Read-only views of the project's internal notes.

Used by the ``read_notes`` tool (so agents never touch ``.rai/`` directly) and
by ``rai show`` (so you can look whenever you're curious).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import ledger
from .config import Project
from .store import Store, authors_list

WHAT = {
    "brief": "the short summary of the whole project",
    "corpus_map": "themes, debates, coverage of the open questions, gaps",
    "manuscript_map": "the argument across the draft, glossary, unkept promises",
    "chapter_note": "one draft file's argument, claims, terms, threads (file=)",
    "claims": "every claim in the drafts and whether the library supports it",
    "objections": "objections raised and what was decided (file=, status=)",
    "decisions": "decisions the writer has stated",
    "reading": "which sources the writer says they've read",
    "sources": "the library inventory: authors, titles, sizes, reading and citations",
    "candidates": "works proposed for reading and what the writer decided",
    "sessions": "past conversations: what was discussed, understood, where it stopped",
    "notes": "working notes: durable, revisable knowledge the assistant recorded",
    "note": "one working note in full (note_id=), with evidence and links",
}
NOT_BUILT = "Not built yet. Run `rai refresh`."


def _file(path: Path, missing: str = NOT_BUILT) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else missing


def _objections(items: list[dict]) -> str:
    if not items:
        return "No objections recorded."
    lines = []
    for o in items:
        where = f"{o['file']} paras {o['paras']}" if o["paras"] else o["file"]
        line = (
            f"{o['id']} [{o['status']}] {where} (raised {o['raised']}): {o['summary']}"
        )
        if o["note"]:
            line += f"\n    note: {o['note']}"
        if o["draft_changed_since"] and o["status"] != "open":
            line += "\n    (the draft has changed since this was decided)"
        elif o["draft_changed_since"]:
            line += "\n    (the draft has changed since this was raised)"
        lines.append(line)
    return "\n".join(lines)


def _sessions(project: Project, limit: int) -> str:
    items = ledger.sessions(project)
    if not items:
        return "No conversations have been summarised yet (the writer runs /wrap)."
    shown = items[: limit or 5]
    out = []
    for item in shown:
        who = f", {item['agent']}" if item["agent"] else ""
        lines = [f"{item['id']} ({item['date']}{who})"]
        lines.append(f"  Discussed: {item['discussed'] or '-'}")
        if item["understandings"]:
            lines.append("  New understandings:")
            lines += [f"    - {u}" for u in item["understandings"]]
        lines.append(f"  Left off: {item['left_off'] or '-'}")
        if item["reports"]:
            lines.append(f"  Reports: {', '.join(item['reports'])}")
        if item.get("notes"):
            lines.append(f"  Notes: {', '.join(item['notes'])}")
        out.append("\n".join(lines))
    if len(items) > len(shown):
        out.append(
            f"({len(items) - len(shown)} earlier; `rai show sessions --limit N`)"
        )
    return "\n\n".join(out)


def _citation_counts(project: Project) -> dict[str, int]:
    index = project.state_dir / "citations.json"
    if not index.exists():
        return {}
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    counts: dict[str, int] = {}
    for key, uses in data.items():
        if isinstance(uses, dict):
            counts[key] = sum(n for n in uses.values() if isinstance(n, int))
    return counts


def _sources(project: Project) -> str:
    store = Store(project)
    try:
        rows = store.sources()
        if not rows:
            return "No sources indexed yet. Run `rai ingest`."
        reading = ledger.reading_statuses(project)
        cited = _citation_counts(project)
        lines = [
            "| Cite key | Author (year) | Title | Kind | Blocks / pages | "
            "Reading | Cited |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for source in rows:
            counts = store.conn.execute(
                "SELECT count(*) n, count(DISTINCT page_index) p FROM blocks "
                "WHERE source_id=?",
                (source["id"],),
            ).fetchone()
            pages = counts["p"] or 0
            size = f"{counts['n']} / {pages or '-'}"
            authors = ", ".join(authors_list(source)) or "unknown author"
            year = source["year"] or "n.d."
            status = reading.get(source["cite_key"], {}).get("status", "not read")
            values = [
                source["cite_key"] or f"src{source['id']}",
                f"{authors} ({year})",
                source["title"] or "-",
                source["kind"] or "-",
                size,
                status,
                str(cited.get(source["cite_key"], 0) or "-"),
            ]
            lines.append(
                "| " + " | ".join(value.replace("|", "/") for value in values) + " |"
            )
        return f"{len(rows)} sources indexed.\n\n" + "\n".join(lines)
    finally:
        store.close()


def _notes_survey(project: Project, status: str, limit: int, source: str = "") -> str:
    if status not in {"", "active", "deleted", "all"}:
        return "status for notes must be active, deleted or all."
    items = ledger.list_notes(project, status=status, source=source)
    if not items:
        if status == "deleted":
            return "No deleted working notes."
        if source:
            return f"No working notes cite {source!r}."
        return "No working notes yet. /remember records them when the writer asks."
    shown = items[: limit or 20]
    out = []
    for note in shown:
        marker = " [deleted]" if note["status"] == "deleted" else ""
        lines = [f"{note['id']}{marker}  {note['title']}", f"    {note['abstract']}"]
        if note["sources"]:
            lines.append(f"    Sources: {', '.join(note['sources'])}")
        lines.append(f"    Revised: {note['revised']}")
        out.append("\n".join(lines))
    if len(items) > len(shown):
        out.append(f'({len(items) - len(shown)} more; read_notes("notes", limit=N))')
    return "\n\n".join(out)


def _note_evidence(note: dict) -> str:
    lines = []
    for entry in note["evidence"]:
        key = entry["cite_key"]
        location = entry.get("location", "")
        line = f"  - [@{key}, {location}]" if location else f"  - [@{key}]"
        if entry.get("quote"):
            line += f' "{entry["quote"]}"'
        if entry.get("verified"):
            line += f" ({entry['verified']})"
        lines.append(line)
    return "\n".join(lines)


def _note_links(note: dict) -> str:
    parts = []
    for key in ("notes", "decisions", "objections", "sessions", "questions"):
        values = note["links"].get(key) or []
        if values:
            parts.append(f"{key} {', '.join(values)}")
    return "; ".join(parts)


def read_note(project: Project, note_id: str, include_deleted: bool = False) -> str:
    note = ledger.note(project, note_id)
    if note is None:
        return f"No note {note_id!r}."
    if note["status"] == "deleted" and not include_deleted:
        return (
            f"{note['id']} is deleted. It remains in the ledger as an archive; "
            "read it with include_deleted=True. Deleted notes cannot be revised "
            "or reactivated."
        )
    marker = " [deleted]" if note["status"] == "deleted" else ""
    lines = [
        f"{note['id']}{marker}",
        f"Title: {note['title']}",
        f"Abstract: {note['abstract']}",
    ]
    if note["body"]:
        lines += ["Body:", note["body"]]
    lines.append("Sources: " + (", ".join(note["sources"]) if note["sources"] else "-"))
    if note["evidence"]:
        lines += ["Evidence:", _note_evidence(note)]
    if links := _note_links(note):
        lines.append(f"Links: {links}")
    lines.append(f"Created: {note['created']}; Revised: {note['revised']}")
    if note["deleted_at"]:
        lines.append(f"Deleted: {note['deleted_at']}")
        if note["delete_note"]:
            lines.append(f"Delete note: {note['delete_note']}")
    return "\n".join(lines)


def note_history_text(project: Project, note_id: str) -> str:
    events = ledger.note_history(project, note_id)
    if not events:
        return f"No note {note_id!r}."
    out = []
    for event in events:
        line = f"{event['ts'][:16]} {event['event']}"
        if event.get("title"):
            line += f": {event['title']}"
        if event.get("from"):
            line += f": {event['from']} -> {event['to']}"
        if event.get("changed"):
            line += f" ({', '.join(event['changed'])})"
        if event.get("note"):
            line += f" - {event['note']}"
        out.append(line)
    return "\n".join(out)


def read_notes(
    project: Project,
    what: str,
    file: str = "",
    status: str = "",
    limit: int = 0,
    note_id: str = "",
    include_deleted: bool = False,
    source: str = "",
) -> str:
    key = what.strip().lower().replace("-", "_").replace(" ", "_")
    if key == "brief":
        return _file(project.brief_path)
    if key == "corpus_map":
        return _file(project.corpus_dir / "map.md")
    if key == "manuscript_map":
        return _file(project.manuscript_dir / "map.md")
    if key == "chapter_note":
        from .manuscript import note_path

        if not file:
            return "chapter_note needs file= (a draft path such as drafts/intro.md)."
        rel = ledger.draft_rel(project, file)
        try:
            return _file(note_path(project, project.root / rel))
        except ValueError:
            return f"{file!r} isn't a file under {project.drafts_dir_name}/."
    if key == "claims":
        return _file(
            project.claims_path,
            "No claims yet. They are extracted when drafts are mapped "
            "(`rai manuscript`).",
        )
    if key == "objections":
        return _objections(ledger.list_objections(project, file, status))
    if key == "decisions":
        items = ledger.list_decisions(project)
        if not items:
            return "No decisions recorded."
        return "\n".join(
            f"{d['id']} ({d['date']}): {d['text']}"
            + (f" [{d['refs']}]" if d["refs"] else "")
            for d in items
        )
    if key == "reading":
        items = ledger.reading_statuses(project)
        if not items:
            return "No reading status recorded: every source counts as not read."
        return "\n".join(
            f"{k}: {v['status']} ({v['date']})"
            + (f" - {v['note']}" if v["note"] else "")
            for k, v in sorted(items.items())
        )
    if key == "sources":
        return _sources(project)
    if key == "candidates":
        items = ledger.candidates(project)
        if not items:
            return "No candidates proposed yet."
        return "\n".join(
            f"{c['id']} [{c['status']}] {c['title']} <{c['ref']}> "
            f"verdict: {c['verdict'] or '-'}"
            + (f" (note: {c['note']})" if c["note"] else "")
            for c in items
        )
    if key == "sessions":
        return _sessions(project, limit)
    if key == "notes":
        return _notes_survey(project, status, limit, source)
    if key == "note":
        target = (note_id or file).strip()
        if not target:
            return "note needs note_id= (for example N7)."
        return read_note(project, target, include_deleted)
    options = ", ".join(WHAT)
    return f"Unknown notes {what!r}. Choose one of: {options}."
