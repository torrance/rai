"""Read-only views of the project's internal notes.

Used by the ``read_notes`` tool (so agents never touch ``.nfh/`` directly) and
by ``nfh show`` (so you can look whenever you're curious).
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
}
NOT_BUILT = "Not built yet. Run `nfh refresh`."


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
        out.append("\n".join(lines))
    if len(items) > len(shown):
        out.append(
            f"({len(items) - len(shown)} earlier; `nfh show sessions --limit N`)"
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
            return "No sources indexed yet. Run `nfh ingest`."
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


def read_notes(
    project: Project, what: str, file: str = "", status: str = "", limit: int = 0
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
            "(`nfh manuscript`).",
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
    options = ", ".join(WHAT)
    return f"Unknown notes {what!r}. Choose one of: {options}."
