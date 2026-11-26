"""Group blocks into retrieval chunks and into reading units for cards."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .config import estimate_tokens
from .store import loc_label


def _top(path: str, depth: int = 2) -> str:
    return " > ".join(path.split(" > ")[:depth]) if path else ""


def make_chunks(
    blocks: Sequence[dict[str, Any]], min_tokens: int, max_tokens: int
) -> list[dict[str, Any]]:
    """Chunks of whole blocks, breaking at section changes once big enough.

    Neighbouring chunks overlap by one short block so a passage split across
    a boundary is still found whole.
    """
    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        body = [b for b in current if b.get("kind") != "heading"] or current
        if body:
            chunks.append(
                {
                    "first_block": current[0]["seq"],
                    "last_block": current[-1]["seq"],
                    "heading_path": body[-1].get("heading_path", ""),
                    "text": "\n\n".join(b["text"] for b in current),
                    "loc_label": loc_label(body),
                }
            )
        tail = current[-1] if current else None
        current, size = [], 0
        if tail and tail.get("kind") != "heading":
            tail_size = estimate_tokens(tail["text"])
            if tail_size < max_tokens // 4:
                current, size = [tail], tail_size

    for block in blocks:
        tokens = estimate_tokens(block["text"])
        section_changed = bool(current) and _top(block.get("heading_path", "")) != (
            _top(current[-1].get("heading_path", ""))
        )
        if current and (
            size + tokens > max_tokens or (section_changed and size >= min_tokens)
        ):
            flush()
        current.append(block)
        size += tokens
    if current and any(
        b["seq"] > (chunks[-1]["last_block"] if chunks else -1) for b in current
    ):
        flush()
    return chunks


def embedding_text(meta: dict[str, Any], chunk: dict[str, Any]) -> str:
    """Prefix chunks with where they come from; improves retrieval noticeably."""
    authors = ", ".join(meta.get("authors", [])[:3])
    head = f"{meta.get('title', '')} - {authors} ({meta.get('year', '')})"
    if chunk.get("heading_path"):
        head += f" > {chunk['heading_path']}"
    return f"{head}\n\n{chunk['text']}"


def make_units(
    blocks: Sequence[Any],
    min_tokens: int,
    max_tokens: int,
    single_unit_tokens: int,
) -> list[dict[str, Any]]:
    """Divide a source into chapter-like units for card building."""
    if not blocks:
        return []
    total = sum(estimate_tokens(b["text"]) for b in blocks)
    if total <= single_unit_tokens:
        return [_unit(blocks, "Whole text")]

    groups: list[list[Any]] = []
    for block in blocks:
        top = _top(block["heading_path"] or "", depth=1)
        if not groups or _top(groups[-1][-1]["heading_path"] or "", 1) != top:
            groups.append([block])
        else:
            groups[-1].append(block)

    merged: list[list[Any]] = []
    for group in groups:
        size = sum(estimate_tokens(b["text"]) for b in group)
        if merged and (
            sum(estimate_tokens(b["text"]) for b in merged[-1]) < min_tokens
            or size < min_tokens // 4
        ):
            merged[-1].extend(group)
        else:
            merged.append(list(group))

    units: list[dict[str, Any]] = []
    for group in merged:
        part: list[Any] = []
        size = 0
        for block in group:
            tokens = estimate_tokens(block["text"])
            if part and size + tokens > max_tokens:
                units.append(_unit(part))
                part, size = [], 0
            part.append(block)
            size += tokens
        if part:
            units.append(_unit(part))
    return units


def _unit(blocks: Sequence[Any], title: str | None = None) -> dict[str, Any]:
    heading = next((b["heading_path"] for b in blocks if b["heading_path"]), "") or ""
    return {
        "first_block": blocks[0]["seq"],
        "last_block": blocks[-1]["seq"],
        "title": title or _top(heading, 1) or "Untitled section",
        "loc_label": loc_label(blocks),
        "blocks": list(blocks),
    }


def marked_text(blocks: Sequence[Any]) -> str:
    """Unit text with inline location markers the model can cite."""
    lines: list[str] = []
    last_marker = None
    for b in blocks:
        marker = loc_label([b])
        if marker and marker != last_marker:
            lines.append(f"[{marker}]")
            last_marker = marker
        prefix = "## " if b["kind"] == "heading" else ""
        lines.append(prefix + b["text"])
    return "\n".join(lines)
