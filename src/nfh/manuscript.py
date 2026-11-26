"""Chapter notes, the project-level manuscript map, and claim extraction.

Drafts are Markdown (Pandoc citations, ``[@key, p. 79]``) or Typst
(``@key[p. 79]``). Both are parsed with ``pandoc -t json`` into the same tree,
which gives exact citations and paragraph numbers.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from .cards import read_frontmatter, text_hash, write_with_frontmatter
from .claims import render_claims
from .config import PROMPT_VERSION, Project
from .llm import chat_json, load_prompt
from .store import Store, now

DRAFT_SUFFIXES = {".md": "markdown", ".markdown": "markdown", ".typ": "typst"}
MATCH_THRESHOLD = 80

CHAPTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "argument": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"step": {"type": "string"}, "paras": {"type": "string"}},
                "required": ["step", "paras"],
            },
        },
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": [
                            "empirical",
                            "interpretive",
                            "normative",
                            "definitional",
                        ],
                    },
                    "para": {"type": "integer"},
                },
                "required": ["claim", "type", "para"],
            },
        },
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"term": {"type": "string"}, "para": {"type": "integer"}},
                "required": ["term", "para"],
            },
        },
        "threads": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["argument", "claims", "terms", "threads"],
}

MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["file", "summary"],
            },
        },
        "argument_line": {"type": "string"},
        "glossary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "meaning": {"type": "string"},
                    "where": {"type": "string"},
                },
                "required": ["term", "meaning", "where"],
            },
        },
        "promises": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "promise": {"type": "string"},
                    "where": {"type": "string"},
                    "kept": {"type": "string"},
                },
                "required": ["promise", "where", "kept"],
            },
        },
        "overlaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["files", "argument_line", "glossary", "promises", "overlaps"],
}


# Parsing ---------------------------------------------------------------------
def _stringify(inlines: list[dict]) -> str:
    out: list[str] = []
    for node in inlines:
        t = node.get("t")
        c = node.get("c")
        if t == "Str":
            out.append(c)
        elif t in {"Space", "SoftBreak", "LineBreak"}:
            out.append(" ")
        elif t in {"Emph", "Strong", "Strikeout", "Underline", "SmallCaps", "Span"}:
            out.append(_stringify(c[-1] if t == "Span" else c))
        elif t == "Quoted":
            out.append('"' + _stringify(c[1]) + '"')
        elif t == "Cite":
            out.append(_stringify(c[1]))
        elif t in {"Code", "Math"}:
            out.append(c[-1])
        elif t == "Link":
            out.append(_stringify(c[1]))
        elif t == "Note":
            pass
    return "".join(out).replace("\xa0", " ")


def _citations(inlines: list[dict]) -> list[str]:
    found: list[str] = []
    for node in inlines:
        t, c = node.get("t"), node.get("c")
        if t == "Cite":
            for cit in c[0]:
                locator = _stringify(cit.get("citationSuffix", [])).strip(" ,")
                found.append(
                    f"@{cit['citationId']}" + (f", {locator}" if locator else "")
                )
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, list) and part and isinstance(part[0], dict):
                    found.extend(_citations(part))
    return found


def _paragraphs(blocks: list[dict], out: list[dict]) -> None:
    for block in blocks:
        t, c = block.get("t"), block.get("c")
        if t in {"Para", "Plain"}:
            text = _stringify(c).strip()
            if text:
                out.append({"text": text, "citations": _citations(c)})
        elif t == "Header":
            out.append(
                {"text": _stringify(c[2]).strip(), "heading": True, "citations": []}
            )
        elif t == "BlockQuote":
            _paragraphs(c, out)
        elif t in {"BulletList", "OrderedList"}:
            items = c if t == "BulletList" else c[1]
            for item in items:
                _paragraphs(item, out)
        elif t == "Div":
            _paragraphs(c[1], out)


def parse_draft(path: Path) -> list[dict[str, Any]]:
    """Numbered paragraphs (headings unnumbered) with their citations."""
    fmt = DRAFT_SUFFIXES[path.suffix.lower()]
    if shutil.which("pandoc"):
        proc = subprocess.run(
            ["pandoc", "-f", fmt, "-t", "json", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            items: list[dict] = []
            _paragraphs(json.loads(proc.stdout)["blocks"], items)
            raw = path.read_text(encoding="utf-8")
            has_cites = any(i["citations"] for i in items)
            # Older pandoc reads Typst but not its citations; fall back then.
            if fmt == "markdown" or has_cites or not TYPST_CITE.search(raw):
                return _number(items)
    return _number(_fallback_parse(path.read_text(encoding="utf-8"), fmt))


TYPST_CITE = re.compile(r"(?<![\w.])@([\w:\-]+\w)(?:\[([^\]]*)\])?")
MD_CITE = re.compile(r"(?<![\w.])@([\w:.#$%&\-+?<>~/]+\w)(?:,\s*([^;\]]+))?")


def _fallback_parse(text: str, fmt: str) -> list[dict]:
    items = []
    heading = r"^#{1,6}\s" if fmt == "markdown" else r"^=+\s"
    pattern = MD_CITE if fmt == "markdown" else TYPST_CITE
    for raw in re.split(r"\n\s*\n", text):
        chunk = raw.strip()
        if not chunk:
            continue
        cites = [
            f"@{key}" + (f", {loc.strip()}" if loc and loc.strip() else "")
            for key, loc in pattern.findall(chunk)
        ]
        items.append(
            {
                "text": re.sub(r"\s+", " ", re.sub(r"^(#+|=+)\s+", "", chunk)),
                "heading": bool(re.match(heading, chunk)),
                "citations": cites,
            }
        )
    return items


def _number(items: list[dict]) -> list[dict]:
    n = 0
    for item in items:
        if not item.get("heading"):
            n += 1
            item["para"] = n
    return items


def draft_files(project: Project) -> list[Path]:
    if not project.drafts_dir.exists():
        return []
    return sorted(
        p
        for p in project.drafts_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in DRAFT_SUFFIXES
    )


def chapter_id(path: Path, project: Project) -> str:
    rel = path.relative_to(project.drafts_dir).with_suffix("")
    return re.sub(r"[^a-z0-9]+", "-", str(rel).lower()).strip("-")


def note_path(project: Project, path: Path) -> Path:
    return project.manuscript_dir / f"{chapter_id(path, project)}.md"


# Claims ----------------------------------------------------------------------
def _sync_claims(
    store: Store,
    chapter: str,
    file_hash: str,
    extracted: list[dict],
    paragraphs: dict[int, dict],
) -> list[str]:
    existing = list(
        store.conn.execute(
            "SELECT * FROM claims WHERE chapter=? AND active=1", (chapter,)
        )
    )
    used: set[str] = set()
    ids: list[str] = []
    numbers = [
        int(m.group(1))
        for r in store.conn.execute("SELECT id FROM claims WHERE chapter=?", (chapter,))
        if (m := re.search(r"-c(\d+)$", r["id"]))
    ]
    next_n = max(numbers, default=0) + 1
    for claim in extracted:
        para = paragraphs.get(claim["para"], {})
        cited = "; ".join(para.get("citations", [])) or None
        opening = " ".join(para.get("text", "").split()[:8])
        best, best_score = None, 0.0
        for row in existing:
            if row["id"] in used:
                continue
            score = fuzz.token_set_ratio(claim["claim"], row["claim"])
            if score > best_score:
                best, best_score = row, score
        if best is not None and best_score >= MATCH_THRESHOLD:
            used.add(best["id"])
            changed = best_score < 95 or (best["cited"] or None) != cited
            store.conn.execute(
                "UPDATE claims SET para=?, opening=?, claim=?, type=?, cited=?, "
                "chapter_hash=?, status=? WHERE id=?",
                (
                    claim["para"],
                    opening,
                    claim["claim"],
                    claim["type"],
                    cited,
                    file_hash,
                    "unchecked" if changed else best["status"],
                    best["id"],
                ),
            )
            ids.append(best["id"])
        else:
            cid = f"{chapter}-c{next_n:02d}"
            next_n += 1
            store.conn.execute(
                "INSERT INTO claims(id, chapter, para, opening, claim, type, cited, "
                "status, evidence, chapter_hash, active) "
                "VALUES(?,?,?,?,?,?,?,'unchecked','[]',?,1)",
                (
                    cid,
                    chapter,
                    claim["para"],
                    opening,
                    claim["claim"],
                    claim["type"],
                    cited,
                    file_hash,
                ),
            )
            ids.append(cid)
    for row in existing:
        if row["id"] not in used:
            store.conn.execute("UPDATE claims SET active=0 WHERE id=?", (row["id"],))
    store.conn.commit()
    return ids


# Chapter notes ---------------------------------------------------------------
def map_chapter(
    project: Project, store: Store, path: Path, say: Callable[[str], None] = print
) -> Path:
    text = path.read_text(encoding="utf-8")
    file_hash = text_hash(text)
    paragraphs = parse_draft(path)
    numbered = {p["para"]: p for p in paragraphs if "para" in p}
    lines = []
    for p in paragraphs:
        lines.append(
            f"## {p['text']}" if p.get("heading") else f"[para {p['para']}] {p['text']}"
        )
    say(f"  mapping {project.rel(path)} ({len(numbered)} paragraphs)")
    result = chat_json(
        project, load_prompt("chapter_note"), "\n\n".join(lines), CHAPTER_SCHEMA
    )
    chapter = chapter_id(path, project)
    claim_ids = _sync_claims(store, chapter, file_hash, result["claims"], numbered)
    cites: dict[str, int] = defaultdict(int)
    for p in numbered.values():
        for c in p["citations"]:
            cites[c.split(",")[0].lstrip("@")] += 1
    todos = [n for n, p in numbered.items() if "TODO" in p["text"]]
    out = note_path(project, path)
    meta = {
        "file": project.rel(path),
        "file_hash": file_hash,
        "words": len(re.findall(r"\w+", text)),
        "mapped": now()[:10],
    }
    argument = "\n".join(
        f"{i}. {a['step']} (paras {a['paras']})"
        for i, a in enumerate(result["argument"], start=1)
    )
    claims = "\n".join(
        f"- {cid}: {c['claim']} ({c['type']}, para {c['para']})"
        for cid, c in zip(claim_ids, result["claims"], strict=True)
    )
    sources = "\n".join(f"- {k} ({n} times)" for k, n in sorted(cites.items()))
    terms = "\n".join(f"- {t['term']}: para {t['para']}" for t in result["terms"])
    threads = [*result["threads"]]
    if todos:
        threads.append(f"TODO markers you left: {len(todos)} (paras {todos})")
    body = "\n\n".join(
        [
            f"# {project.rel(path)}",
            "## Argument\n" + (argument or "(no argument steps found)"),
            "## Claims\n" + (claims or "- none"),
            "## Sources cited\n" + (sources or "- none"),
            "## Terms introduced\n" + (terms or "- none"),
            "## Threads\n" + ("\n".join(f"- {t}" for t in threads) or "- none"),
        ]
    )
    write_with_frontmatter(out, meta, body)
    store.record_note(
        project.rel(out),
        "chapter",
        {"file": file_hash},
        project.models.chat,
        PROMPT_VERSION,
    )
    return out


def stale_chapters(project: Project, store: Store) -> list[Path]:
    out = []
    for path in draft_files(project):
        meta, _ = read_frontmatter(note_path(project, path))
        if meta.get("file_hash") != text_hash(path.read_text(encoding="utf-8")):
            out.append(path)
    return out


def write_citation_index(project: Project) -> None:
    index: dict[str, dict[str, int]] = defaultdict(dict)
    for path in draft_files(project):
        for p in parse_draft(path):
            for c in p.get("citations", []):
                key = c.split(",")[0].lstrip("@")
                rel = project.rel(path)
                index[key][rel] = index[key].get(rel, 0) + 1
    (project.state_dir / "citations.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )


def build_manuscript(
    project: Project,
    only: Path | None = None,
    force: bool = False,
    say: Callable[[str], None] = print,
) -> list[Path]:
    store = Store(project)
    files = [only] if only else draft_files(project)
    stale = set(stale_chapters(project, store))
    done = []
    for path in files:
        if force or path in stale:
            done.append(map_chapter(project, store, path, say=say))
    # Deactivate claims for draft files that no longer exist.
    live = {chapter_id(p, project) for p in draft_files(project)}
    for row in store.conn.execute("SELECT DISTINCT chapter FROM claims WHERE active=1"):
        if row["chapter"] not in live:
            store.conn.execute(
                "UPDATE claims SET active=0 WHERE chapter=?", (row["chapter"],)
            )
    store.conn.commit()
    write_citation_index(project)
    if done or not (project.manuscript_dir / "map.md").exists():
        build_project_map(project, store, say=say)
    render_claims(project, store)
    store.close()
    return done


def build_project_map(
    project: Project, store: Store, say: Callable[[str], None] = print
) -> Path | None:
    notes = [note_path(project, p) for p in draft_files(project)]
    notes = [n for n in notes if n.exists()]
    out = project.manuscript_dir / "map.md"
    if not notes:
        return None
    say("  writing the project-level manuscript map")
    texts = [n.read_text(encoding="utf-8") for n in notes]
    result = chat_json(
        project,
        load_prompt("manuscript_map"),
        "\n\n=====\n\n".join(texts),
        MAP_SCHEMA,
    )
    files = "\n\n".join(f"### {f['file']}\n{f['summary']}" for f in result["files"])
    glossary = "\n".join(
        f"- **{g['term']}**: {g['meaning']} ({g['where']})" for g in result["glossary"]
    )
    promises = "\n".join(
        f"- {p['promise']} ({p['where']}); kept: {p['kept']}"
        for p in result["promises"]
    )
    overlaps = "\n".join(f"- {o}" for o in result["overlaps"])
    body = "\n\n".join(
        [
            "# Manuscript map",
            "## Argument across the draft\n" + result["argument_line"],
            "## Files\n" + files,
            "## Glossary\n" + (glossary or "- none"),
            "## Promises\n" + (promises or "- none"),
            "## Overlaps\n" + (overlaps or "- none"),
        ]
    )
    out.write_text(body + "\n", encoding="utf-8")
    store.record_note(
        project.rel(out),
        "book_map",
        {project.rel(n): text_hash(t) for n, t in zip(notes, texts, strict=True)},
        project.models.chat,
        PROMPT_VERSION,
    )
    return out
