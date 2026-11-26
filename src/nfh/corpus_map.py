"""The corpus map: the shape of the project's library, built only from cards."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

import numpy as np

from .cards import card_path, open_questions, project_note_path, text_hash
from .config import PROMPT_VERSION, Project
from .llm import chat_json, load_prompt
from .search import search
from .store import Store

MEMBERS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"cite_key": {"type": "string"}, "stance": {"type": "string"}},
        "required": ["cite_key", "stance"],
    },
}
THEME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "members": MEMBERS,
    },
    "required": ["name", "description", "members"],
}
MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "themes": {"type": "array", "items": THEME_SCHEMA},
        "debates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "positions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "position": {"type": "string"},
                                "sources": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "where": {"type": "string"},
                            },
                            "required": ["position", "sources", "where"],
                        },
                    },
                },
                "required": ["question", "positions"],
            },
        },
        "coverage": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "rating": {"type": "string", "enum": ["strong", "thin", "none"]},
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                "required": ["question", "rating", "sources", "note"],
            },
        },
        "lineage": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "responds_to": {"type": "string"},
                    "how": {"type": "string"},
                },
                "required": ["source", "responds_to", "how"],
            },
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "themes", "debates", "coverage", "gaps"],
}
SYNTHESIS_SCHEMA: dict[str, Any] = {
    **MAP_SCHEMA,
    "required": ["overview", "debates", "coverage", "gaps"],
}


def _cards(project: Project, store: Store) -> list[dict[str, Any]]:
    out = []
    for source in store.sources():
        path = card_path(project, source["cite_key"])
        if not path.exists():
            continue
        note = project_note_path(project, source["cite_key"])
        out.append(
            {
                "cite_key": source["cite_key"],
                "source_id": source["id"],
                "card": path.read_text(encoding="utf-8"),
                "note": note.read_text(encoding="utf-8") if note.exists() else "",
            }
        )
    return out


def _thesis_vectors(store: Store, cards: list[dict]) -> np.ndarray:
    vectors = []
    for c in cards:
        row = store.conn.execute(
            "SELECT embedding FROM chunks WHERE source_id=? AND kind='card' "
            "ORDER BY id LIMIT 1",
            (c["source_id"],),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Card for {c['cite_key']} isn't indexed; rerun cards.")
        vectors.append(np.frombuffer(row["embedding"], dtype=np.float32))
    return np.stack(vectors)


def _groups(project: Project, store: Store, cards: list[dict]) -> list[dict]:
    """One group if the library is small, else clusters of similar sources."""
    groups: list[dict] = []
    rest = list(cards)
    if len(rest) <= 15:
        if rest:
            groups.append({"cards": rest})
        return groups
    from sklearn.cluster import AgglomerativeClustering

    n_clusters = max(2, math.ceil(len(rest) / 12))
    labels = AgglomerativeClustering(
        n_clusters=n_clusters, metric="cosine", linkage="average"
    ).fit_predict(_thesis_vectors(store, rest))
    for label in sorted(set(labels)):
        groups.append(
            {
                "cards": [
                    c for c, lab in zip(rest, labels, strict=True) if lab == label
                ],
            }
        )
    return groups


def _coverage_hits(store: Store, questions: list[str]) -> str:
    lines = []
    for i, q in enumerate(questions, start=1):
        keys: list[str] = []
        for kind in ("card", "text"):
            for hit in search(store, q, kind=kind, limit=6):
                if hit["cite_key"] not in keys:
                    keys.append(hit["cite_key"])
        lines.append(f"Q{i}: {q}\n  search hits: {', '.join(keys) or 'none'}")
    return "\n".join(lines)


def build_map(
    project: Project, say: Callable[[str], None] = print, rebuild: bool = False
) -> str:
    store = Store(project)
    cards = _cards(project, store)
    if not cards:
        store.close()
        raise RuntimeError("No cards yet. Run `nfh cards` first.")
    questions = open_questions(project)
    coverage_hits = _coverage_hits(store, questions) if questions else "(none)"
    question_text = "\n".join(f"Q{i + 1}: {q}" for i, q in enumerate(questions))
    map_dir = project.corpus_dir
    map_dir.mkdir(parents=True, exist_ok=True)
    json_path = map_dir / "map.json"
    previous = json.loads(json_path.read_text()) if json_path.exists() else {}
    theme_cache: dict[str, Any] = {} if rebuild else previous.get("theme_cache", {})

    if len(cards) <= project.limits.corpus_single_pass_cards:
        say(f"Building the corpus map from {len(cards)} cards in one pass")
        result = chat_json(
            project,
            load_prompt("corpus_map"),
            f"Open questions:\n{question_text or '(none)'}\n\n"
            f"Search hits per question:\n{coverage_hits}\n\nCards:\n\n"
            + "\n\n=====\n\n".join(c["card"] + "\n" + c["note"] for c in cards),
            MAP_SCHEMA,
        )
        new_cache: dict[str, Any] = {}
    else:
        groups = _groups(project, store, cards)
        themes = []
        new_cache = {}
        for n, group in enumerate(groups, start=1):
            keys = sorted(c["cite_key"] for c in group["cards"])
            digest = text_hash("".join(c["card"] for c in group["cards"]))
            if digest in theme_cache:
                themes.append(theme_cache[digest])
                new_cache[digest] = theme_cache[digest]
                continue
            say(f"  theme {n}/{len(groups)}: {len(keys)} sources")
            theme = chat_json(
                project,
                load_prompt("corpus_theme"),
                "\n\n=====\n\n".join(c["card"] for c in group["cards"]),
                THEME_SCHEMA,
            )
            themes.append(theme)
            new_cache[digest] = theme
        say("  synthesising overview, debates, coverage and gaps")
        result = chat_json(
            project,
            load_prompt("corpus_map"),
            f"Open questions:\n{question_text or '(none)'}\n\n"
            f"Search hits per question:\n{coverage_hits}\n\n"
            f"Themes (already written):\n{json.dumps(themes, ensure_ascii=False)}\n\n"
            "Claims by source:\n"
            + "\n\n".join(
                c["cite_key"] + ":\n" + _claims_section(c["card"]) for c in cards
            ),
            SYNTHESIS_SCHEMA,
        )
        result["themes"] = themes

    markdown = render_map(result, len(cards))
    (map_dir / "map.md").write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "themes": [
                    {
                        "name": t["name"],
                        "members": [m["cite_key"] for m in t["members"]],
                    }
                    for t in result.get("themes", [])
                ],
                "coverage": result.get("coverage", []),
                "gaps": result.get("gaps", []),
                "theme_cache": new_cache,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store.record_note(
        project.rel(map_dir / "map.md"),
        "corpus_map",
        {c["cite_key"]: text_hash(c["card"]) for c in cards},
        project.models.chat,
        PROMPT_VERSION,
    )
    store.close()
    return markdown


def _claims_section(card: str) -> str:
    start = card.find("## Main claims and evidence")
    if start < 0:
        return ""
    end = card.find("\n## ", start + 5)
    return card[start : end if end > 0 else None].strip()


def render_map(m: dict[str, Any], n_cards: int) -> str:
    parts = [
        f"# Corpus map\n\n{n_cards} sources.\n\n## Overview\n{m.get('overview', '')}"
    ]
    themes = []
    for t in m.get("themes", []):
        members = "\n".join(f"- {x['cite_key']}: {x['stance']}" for x in t["members"])
        themes.append(f"### {t['name']}\n{t['description']}\n\n{members}")
    parts.append("## Themes\n" + ("\n\n".join(themes) or "(none)"))
    debates = []
    for d in m.get("debates", []):
        positions = "\n".join(
            f"- {p['position']} ({', '.join(p['sources'])}; {p['where']})"
            for p in d["positions"]
        )
        debates.append(f"### {d['question']}\n{positions}")
    parts.append("## Debates\n" + ("\n\n".join(debates) or "(none found)"))
    rows = "\n".join(
        f"| {c['question']} | {c['rating']} | {', '.join(c['sources'])} | {c['note']} |"
        for c in m.get("coverage", [])
    )
    parts.append(
        "## Coverage\n| Question | Coverage | Sources | Note |\n"
        "| --- | --- | --- | --- |\n" + rows
    )
    lineage = "\n".join(
        f"- {x['source']} responds to {x['responds_to']}: {x['how']}"
        for x in m.get("lineage", [])
    )
    parts.append("## Lineage\n" + (lineage or "(none recorded)"))
    parts.append("## Gaps\n" + ("\n".join(f"- {g}" for g in m.get("gaps", [])) or "-"))
    return "\n\n".join(parts) + "\n"
