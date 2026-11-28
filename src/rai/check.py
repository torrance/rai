"""Verify every cited quotation in the project's notes and cards.

Recognised forms:
  "quoted words" [@citekey, p. 79]      (reports, any file under reports/)
  - p. 7: ... Quote: "quoted words"     (source cards; key from frontmatter)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import ledger
from .cards import read_frontmatter
from .config import Project
from .store import Store
from .verify import verify_quote

NOTE_QUOTE = re.compile(
    r"[\"\u201c]([^\"\u201d]{12,}?)[\"\u201d][.,;:]?\s*\[@([\w:.\-]+)"
    r"(?:,\s*([^;\]]+))?(?:;[^\]]*)?\]"
)
CARD_QUOTE = re.compile(
    r'^- ([^:]+):.*?Quote: "(.*)"[ \t]*$', re.MULTILINE
)


@dataclass
class QuoteCheck:
    file: str
    cite_key: str
    location: str
    quote: str
    status: str
    detail: str = ""


def _check(store: Store, file: str, key: str, loc: str, quote: str) -> QuoteCheck:
    result = verify_quote(store, key, quote, loc or None)
    detail = ""
    if result["status"] in {"near", "elsewhere"}:
        wording = result.get("actual_wording") or ""
        detail = f"source has: {wording} at {result.get('location')}"
    elif result.get("error"):
        detail = result["error"]
    return QuoteCheck(file, key, loc, quote, result["status"], detail)


def check_note_quotes(
    project: Project, store: Store, include_deleted: bool = False
) -> list[QuoteCheck]:
    """Verify quotations stored in working notes."""
    results: list[QuoteCheck] = []
    status = "all" if include_deleted else "active"
    for note in ledger.list_notes(project, status=status):
        for entry in note["evidence"]:
            quote = entry.get("quote", "")
            if not quote:
                continue
            key = entry["cite_key"]
            loc = entry.get("location", "")
            if store.source(key) is None:
                results.append(
                    QuoteCheck(
                        f"note {note['id']}",
                        key,
                        loc,
                        quote,
                        "unverified",
                        "source is not in the library",
                    )
                )
                continue
            results.append(_check(store, f"note {note['id']}", key, loc, quote))
    return results


def check_quotes(
    project: Project,
    paths: list[Path] | None = None,
    include_deleted: bool = False,
) -> list[QuoteCheck]:
    store = Store(project)
    results: list[QuoteCheck] = []
    try:
        files = paths or sorted(project.reports_dir.rglob("*.md"))
        for path in files:
            text = path.read_text(encoding="utf-8")
            for m in NOTE_QUOTE.finditer(text):
                results.append(
                    _check(
                        store,
                        project.rel(path),
                        m.group(2),
                        m.group(3) or "",
                        m.group(1),
                    )
                )
        if paths is None:
            for card in sorted(project.cards_dir.glob("*.md")):
                if card.name.endswith(".prev.md"):
                    continue
                meta, body = read_frontmatter(card)
                for m in CARD_QUOTE.finditer(body):
                    results.append(
                        _check(
                            store,
                            project.rel(card),
                            meta.get("cite_key", ""),
                            m.group(1),
                            m.group(2),
                        )
                    )
            results += check_note_quotes(project, store, include_deleted)
    finally:
        store.close()
    return results
