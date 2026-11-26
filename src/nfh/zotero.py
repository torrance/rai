"""Read items and attachments through Zotero's local API (read-only).

Enable it in Zotero: Settings > Advanced > "Allow other applications on this
computer to communicate with Zotero".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import ZoteroSettings

PAGE = 100
ATTACHMENT_PREFERENCE = [
    "application/pdf",
    "application/epub+zip",
    "text/html",
    "text/plain",
]


class ZoteroError(RuntimeError):
    pass


@dataclass
class ZoteroItem:
    key: str
    title: str
    authors: list[str]
    year: str
    kind: str
    doi: str
    url: str
    tags: list[str]
    collections: list[str]
    cite_key: str
    key_source: str = "zotero"  # zotero (native field) | extra (legacy) | generated
    file: Path | None = None
    file_note: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class Zotero:
    def __init__(self, settings: ZoteroSettings) -> None:
        self.settings = settings
        self.base = settings.api.rstrip("/") + "/users/0"
        self.client = httpx.Client(timeout=30)

    def _get(self, path: str, params: list[tuple[str, str]] | None = None) -> Any:
        try:
            resp = self.client.get(self.base + path, params=params or [])
        except httpx.HTTPError as exc:
            raise ZoteroError(
                f"Can't reach Zotero's local API at {self.settings.api}. Is Zotero "
                "running, with 'Allow other applications on this computer to "
                "communicate with Zotero' enabled?"
            ) from exc
        if resp.status_code != 200:
            raise ZoteroError(f"Zotero returned {resp.status_code} for {path}")
        return resp.json()

    def _paged(self, path: str, params: list[tuple[str, str]]) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            page = self._get(
                path, params + [("limit", str(PAGE)), ("start", str(start))]
            )
            out.extend(page)
            if len(page) < PAGE:
                return out
            start += PAGE

    def ping(self) -> bool:
        try:
            self._get("/collections", [("limit", "1")])
            return True
        except ZoteroError:
            return False

    def collection_key(self, name: str) -> str:
        for coll in self._paged("/collections", []):
            if coll["data"]["name"] == name:
                return coll["key"]
        raise ZoteroError(f"No Zotero collection named {name!r}.")

    def items(
        self,
        collection: str = "",
        tags: list[str] | None = None,
        tag_match: str = "any",
    ) -> list[ZoteroItem]:
        """Top-level items in scope: a collection, tags, or both (intersection)."""
        params: list[tuple[str, str]] = []
        tags = [t for t in (tags or []) if t]
        if tags:
            if tag_match == "all":
                params.extend(("tag", t) for t in tags)
            else:
                params.append(("tag", " || ".join(tags)))
        if collection:
            path = f"/collections/{self.collection_key(collection)}/items/top"
        else:
            path = "/items/top"
        raw_items = self._paged(path, params)
        items = []
        for raw in raw_items:
            if raw["data"].get("itemType") in {"attachment", "note", "annotation"}:
                continue
            item = self._to_item(raw)
            item.file, item.file_note = self._best_file(raw["key"])
            items.append(item)
        return items

    def _best_file(self, key: str) -> tuple[Path | None, str]:
        children = self._get(f"/items/{key}/children")
        attachments = [
            c["data"] for c in children if c["data"].get("itemType") == "attachment"
        ]

        def rank(att: dict) -> int:
            ctype = att.get("contentType", "")
            if ctype in ATTACHMENT_PREFERENCE:
                return ATTACHMENT_PREFERENCE.index(ctype)
            return len(ATTACHMENT_PREFERENCE)

        for att in sorted(attachments, key=rank):
            if att.get("linkMode") == "linked_url":
                continue
            path = self._attachment_path(att)
            if path and path.exists():
                return path, ""
        if not attachments:
            return None, "no attachment"
        return None, "attachment file not found on disk"

    def _attachment_path(self, att: dict) -> Path | None:
        mode = att.get("linkMode")
        if mode in {"imported_file", "imported_url"} and att.get("filename"):
            return self.settings.storage_dir / att["key"] / att["filename"]
        if mode == "linked_file" and att.get("path"):
            raw = att["path"]
            if raw.startswith("attachments:"):
                return None  # needs Zotero's base-directory setting; not supported
            return Path(raw).expanduser()
        return None

    @staticmethod
    def _to_item(raw: dict) -> ZoteroItem:
        data = raw["data"]
        authors = []
        for c in data.get("creators", []):
            if c.get("creatorType") not in {"author", "editor", None}:
                continue
            name = c.get("name") or " ".join(
                p for p in (c.get("firstName"), c.get("lastName")) if p
            )
            if name:
                authors.append(name)
        year_match = re.search(r"\d{4}", data.get("date", "") or "")
        year = year_match.group(0) if year_match else ""
        # Zotero's own Citation Key field (filled by Better BibTeX) comes first;
        # "Citation Key:" in Extra is the pre-Zotero-8 location.
        cite_key, key_source = data.get("citationKey", ""), "zotero"
        if not cite_key:
            cite_key, key_source = _extra_cite_key(data.get("extra", "")), "extra"
        if not cite_key:
            cite_key, key_source = make_cite_key(data, year), "generated"
        return ZoteroItem(
            key=raw["key"],
            title=data.get("title", ""),
            authors=authors,
            year=year,
            kind=data.get("itemType", ""),
            doi=data.get("DOI", ""),
            url=data.get("url", ""),
            tags=[t["tag"] for t in data.get("tags", [])],
            collections=list(data.get("collections", [])),
            cite_key=cite_key,
            key_source=key_source,
            raw=data,
        )


def _extra_cite_key(extra: str) -> str:
    match = re.search(r"^Citation Key:\s*(\S+)", extra or "", re.MULTILINE)
    return match.group(1) if match else ""


def _ascii(text: str) -> str:
    return (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    )


def make_cite_key(data: dict, year: str) -> str:
    """Fallback key in Better BibTeX's default style: author + year (+ word)."""
    creators = data.get("creators", [])
    last = ""
    if creators:
        last = creators[0].get("lastName") or creators[0].get("name", "")
    title_words = [
        w for w in re.findall(r"[A-Za-z]+", data.get("title", "")) if len(w) > 3
    ]
    base = _ascii(re.sub(r"\W+", "", last.split()[-1] if last else "anon"))
    word = _ascii(title_words[0]) if title_words else ""
    return f"{base}{year}{word}"
