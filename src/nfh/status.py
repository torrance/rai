"""What's stale, flagged or missing. Used by `nfh status` and the briefing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import ledger
from .cards import card_path, stale_project_notes, stale_sources, text_hash
from .claims import summary
from .config import Project
from .manuscript import stale_chapters
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
    current = {
        r["cite_key"]: text_hash(card_path(project, r["cite_key"]).read_text("utf-8"))
        for r in rows
        if card_path(project, r["cite_key"]).exists()
    }
    if current:
        recorded = json.loads(map_note["input_hashes"]) if map_note else {}
        s.corpus_map_stale = recorded != current
    s.manuscript_map_stale = bool(s.stale_chapters)
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
