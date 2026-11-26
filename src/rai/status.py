"""What's stale, flagged or missing. Used by `rai status` and the briefing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import ledger
from .cards import stale_project_notes, stale_sources
from .claims import summary
from .config import Project, same_model
from .corpus_map import map_hashes
from .manuscript import manuscript_map_stale, stale_chapters
from .store import Store


@dataclass
class Status:
    sources: int = 0
    flagged: list[tuple[str, str, str]] = field(default_factory=list)
    stale_cards: list[str] = field(default_factory=list)
    stale_project_notes: list[str] = field(default_factory=list)
    stale_chapters: list[str] = field(default_factory=list)
    corpus_map_stale: bool = False
    manuscript_map_stale: bool = False
    claims: dict = field(default_factory=dict)
    open_objections: int = 0
    decisions: int = 0
    unknown_citations: list[str] = field(default_factory=list)

    @property
    def stale_notes(self) -> list[str]:
        out = [f"card:{k}" for k in self.stale_cards]
        out += [f"project note:{k}" for k in self.stale_project_notes]
        out += [f"chapter note:{c}" for c in self.stale_chapters]
        if self.corpus_map_stale:
            out.append("corpus map")
        if self.manuscript_map_stale:
            out.append("manuscript map")
        return out


def embedding_problem(project: Project) -> str:
    """A mismatch between the configured embedding setup and the index, if any."""
    store = Store(project)
    try:
        return store.embedding_mismatch(
            project.models.embedding,
            project.models.embedding_dims,
            project.models.document_template,
        )
    finally:
        store.close()


def compute(project: Project) -> Status:
    store = Store(project)
    s = Status()
    rows = store.sources()
    s.sources = len(rows)
    s.flagged = [
        (r["cite_key"], r["status"], r["status_detail"])
        for r in rows
        if r["status"] != "ok"
    ]
    s.stale_cards = [r["cite_key"] for r in stale_sources(project, store)]
    s.stale_project_notes = [r["cite_key"] for r in stale_project_notes(project, store)]
    s.stale_chapters = [project.rel(p) for p in stale_chapters(project, store)]

    map_note = store.note(project.rel(project.corpus_dir / "map.md"))
    current = map_hashes(project, store)
    if current:
        recorded = json.loads(map_note["input_hashes"]) if map_note else {}
        s.corpus_map_stale = recorded != current or (
            map_note is not None
            and not same_model(map_note["model"], project.models.summarising)
        )
    s.manuscript_map_stale = bool(s.stale_chapters) or manuscript_map_stale(
        project, store
    )
    s.claims = summary(store)
    citations = project.state_dir / "citations.json"
    if citations.exists():
        known = {r["cite_key"] for r in rows}
        cited = json.loads(citations.read_text(encoding="utf-8"))
        s.unknown_citations = sorted(k for k in cited if k not in known)
    s.open_objections = len(ledger.list_objections(project, status="unresolved"))
    s.decisions = len(ledger.list_decisions(project))
    store.close()
    return s
