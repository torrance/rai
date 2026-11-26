"""Reading candidate works: metadata, open full text, profiles, the queue.

Metadata comes from OpenAlex (DOI lookups are free with a free key). Full text
is looked for in OpenAlex's open-access locations, then Unpaywall, then arXiv.
Everything fetched is cached under ``<project>/.nfh/candidates/`` and kept
separate from the library.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from . import ledger
from .config import Project, estimate_tokens
from .extract import extract, extract_html_string
from .store import Store, now

UA = "nfh/0.1 (non-fiction research harness; mailto:{email})"


def normalise_doi(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    return value.lower()


def is_doi(value: str) -> bool:
    return bool(re.match(r"^10\.\d{4,9}/\S+$", normalise_doi(value)))


def candidate_id(ref: str) -> str:
    key = normalise_doi(ref) if is_doi(ref) else ref.strip().lower()
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _client() -> httpx.Client:
    email = os.environ.get("UNPAYWALL_EMAIL", "unknown@example.org")
    return httpx.Client(
        timeout=60,
        follow_redirects=True,
        headers={"User-Agent": UA.format(email=email)},
    )


def _abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions = [(i, word) for word, idx in inverted.items() for i in idx]
    return " ".join(w for _, w in sorted(positions))


def openalex(client: httpx.Client, doi: str) -> dict[str, Any] | None:
    params = {}
    if key := os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = key
    resp = client.get(f"https://api.openalex.org/works/doi:{doi}", params=params)
    if resp.status_code != 200:
        return None
    w = resp.json()
    primary = (w.get("primary_location") or {}).get("source") or {}
    oa_urls = []
    for loc in [w.get("best_oa_location") or {}, *(w.get("locations") or [])]:
        for k in ("pdf_url", "landing_page_url"):
            if loc.get(k) and loc.get("is_oa"):
                oa_urls.append(loc[k])
    return {
        "title": w.get("display_name", ""),
        "authors": [a["author"]["display_name"] for a in w.get("authorships", [])[:8]],
        "year": w.get("publication_year"),
        "type": w.get("type", ""),
        "venue": primary.get("display_name", ""),
        "peer_reviewed_venue": primary.get("type") == "journal",
        "cited_by_count": w.get("cited_by_count"),
        "is_retracted": w.get("is_retracted", False),
        "is_oa": (w.get("open_access") or {}).get("is_oa", False),
        "abstract": _abstract(w.get("abstract_inverted_index")),
        "oa_urls": list(dict.fromkeys(oa_urls)),
    }


def unpaywall(client: httpx.Client, doi: str) -> list[str]:
    email = os.environ.get("UNPAYWALL_EMAIL")
    if not email:
        return []
    resp = client.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": email})
    if resp.status_code != 200:
        return []
    data = resp.json()
    urls = []
    for loc in [data.get("best_oa_location") or {}, *(data.get("oa_locations") or [])]:
        for k in ("url_for_pdf", "url"):
            if loc.get(k):
                urls.append(loc[k])
    return list(dict.fromkeys(urls))


def _arxiv_pdf(ref: str) -> str | None:
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$", ref)
    if not m:
        m = re.search(r"10\.48550/arxiv\.([\w.\-]+)", ref, re.IGNORECASE)
    return f"https://arxiv.org/pdf/{m.group(1)}" if m else None


def _fetch(client: httpx.Client, url: str, folder: Path) -> Path | None:
    try:
        resp = client.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200 or not resp.content:
        return None
    ctype = resp.headers.get("content-type", "")
    if "pdf" in ctype or resp.content[:5] == b"%PDF-":
        path = folder / "source.pdf"
    elif "html" in ctype or "text" in ctype:
        path = folder / "source.html"
    else:
        return None
    path.write_bytes(resp.content)
    return path


def _assign_pdf_headings(path: Path, blocks: list[dict]) -> None:
    """PyMuPDF text has no headings; borrow them from the PDF outline."""
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            toc = doc.get_toc()
    except Exception:  # noqa: BLE001
        return
    starts = sorted((page, title) for level, title, page in toc if level <= 2)
    for b in blocks:
        current = ""
        for page, title in starts:
            if page <= (b.get("page_index") or 0):
                current = title
        b["heading_path"] = current


def _sections(blocks: list[dict]) -> list[dict]:
    sections: list[dict] = []
    for b in blocks:
        name = (b.get("heading_path") or "Untitled").split(" > ")[0]
        if not sections or sections[-1]["heading"] != name:
            sections.append({"heading": name, "blocks": []})
        sections[-1]["blocks"].append(b)
    return sections


def _text(blocks: list[dict], max_tokens: int | None = None) -> str:
    out, used = [], 0
    last_page = None
    for b in blocks:
        line = b["text"]
        if b.get("page_label") and b["page_label"] != last_page:
            line = f"[p. {b['page_label']}] {line}"
            last_page = b["page_label"]
        used += estimate_tokens(line)
        if max_tokens and used > max_tokens:
            out.append("[... truncated]")
            break
        out.append(line)
    return "\n\n".join(out)


def _previous_decision(
    project: Project, ref: str, data: dict[str, Any]
) -> dict[str, str] | None:
    found = ledger.find_candidate(project, data.get("doi") or data.get("url") or ref)
    if found is None:
        return None
    return {"id": found["id"], "status": found["status"], "note": found["note"]}


def read_candidate(
    project: Project, ref: str, max_tokens: int | None = None
) -> dict[str, Any]:
    max_tokens = max_tokens or project.limits.candidate_max_tokens
    cid = candidate_id(ref)
    folder = project.candidates_dir / cid
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / "candidate.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        data = _build_candidate(project, ref, folder, cid)
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    store = Store(project)
    in_library = store.source_by_doi_or_url(data.get("doi", ""), data.get("url", ""))
    store.close()
    header = {
        "candidate_id": cid,
        "metadata": data["metadata"],
        "doi": data.get("doi", ""),
        "url": data.get("url", ""),
        "in_library": in_library["cite_key"] if in_library else None,
        "previous_decision": _previous_decision(project, ref, data),
        "full_text_source": data.get("text_url"),
    }
    blocks = data.get("blocks", [])
    if not blocks:
        return {
            **header,
            "read_depth": "abstract only",
            "access": "no free copy found; may need library access",
            "abstract": data["metadata"].get("abstract", ""),
        }
    total = sum(estimate_tokens(b["text"]) for b in blocks)
    if total <= max_tokens:
        return {
            **header,
            "read_depth": "full text",
            "tokens": total,
            "text": _text(blocks),
        }
    sections = _sections(blocks)
    toc = [
        {
            "heading": s["heading"],
            "tokens": sum(estimate_tokens(b["text"]) for b in s["blocks"]),
        }
        for s in sections
    ]
    intro = _text(sections[0]["blocks"], max_tokens // 4)
    conclusion = (
        _text(sections[-1]["blocks"], max_tokens // 4) if len(sections) > 1 else ""
    )
    return {
        **header,
        "read_depth": "profile (text too long to return whole)",
        "tokens": total,
        "abstract": data["metadata"].get("abstract", ""),
        "table_of_contents": toc,
        "opening_section": intro,
        "closing_section": conclusion,
        "next_step": "Call read_candidate_section with a heading from the table "
        "of contents to read more.",
    }


def _build_candidate(project: Project, ref: str, folder: Path, cid: str) -> dict:
    data: dict[str, Any] = {"ref": ref, "fetched": now()}
    urls: list[str] = []
    with _client() as client:
        if is_doi(ref):
            doi = normalise_doi(ref)
            data["doi"] = doi
            meta = openalex(client, doi) or {}
            data["metadata"] = meta or {"note": "not found in OpenAlex"}
            urls += meta.get("oa_urls", [])
            urls += unpaywall(client, doi)
        else:
            data["url"] = ref
            data["metadata"] = {}
            urls.append(ref)
        if arxiv := _arxiv_pdf(ref):
            urls.insert(0, arxiv)
        urls.sort(key=lambda u: 0 if u.lower().endswith(".pdf") or "/pdf" in u else 1)
        for url in dict.fromkeys(urls):
            path = _fetch(client, url, folder)
            if path is None:
                continue
            if path.suffix == ".pdf":
                result = extract(path, pdf_engine="pymupdf")
                _assign_pdf_headings(path, result.blocks)
            else:
                result = extract_html_string(path.read_text("utf-8", errors="replace"))
                for k, v in result.metadata.items():
                    data["metadata"].setdefault(k, v)
            if result.blocks and result.status != "needs_ocr":
                data["text_url"] = url
                data["blocks"] = result.blocks
                break
    return data


def read_candidate_section(
    project: Project, cid: str, heading: str, max_tokens: int = 20000
) -> dict[str, Any]:
    cache = project.candidates_dir / cid / "candidate.json"
    if not cache.exists():
        return {"error": f"No cached candidate {cid}. Call read_candidate first."}
    data = json.loads(cache.read_text(encoding="utf-8"))
    sections = _sections(data.get("blocks", []))
    wanted = heading.strip().lower()
    for s in sections:
        if wanted in s["heading"].lower():
            return {
                "candidate_id": cid,
                "heading": s["heading"],
                "text": _text(s["blocks"], max_tokens),
            }
    return {
        "error": f"No section matching {heading!r}.",
        "headings": [s["heading"] for s in sections],
    }


READING_LIST_FIELDS = (
    ("Serves", "serves"),
    ("Claim", "claim"),
    ("Evidence", "evidence"),
    ("Standing", "standing"),
    ("Fit", "fit"),
    ("Read depth", "read_depth"),
    ("Access", "access"),
    ("Verdict", "verdict"),
)


def render_reading_list(project: Project) -> Path:
    """Write reports/reading-list.md from proposed works and your decisions."""
    items = ledger.candidates(project)
    lines = [
        "# Reading list",
        "",
        "Written by the assistant from the works it proposed and your decisions. "
        "Don't edit "
        'this file: tell the assistant in chat ("shortlist C3", "skip C5") and it '
        "updates. To read a source, add it to Zotero (Connector, or 'Add item by "
        "identifier'), then run `nfh refresh`.",
        "",
    ]
    for title, status in (
        ("Shortlisted", "shortlisted"),
        ("Proposed", "proposed"),
        ("Dismissed", "dismissed"),
    ):
        group = [c for c in items if c["status"] == status]
        lines += [f"## {title} ({len(group)})", ""]
        for c in group:
            if status == "dismissed":
                note = f" ({c['note']})" if c["note"] else ""
                lines.append(f"- {c['id']}: {c['title']}{note}")
                continue
            lines += [f"### {c['id']}: {c['title']}", f"- Reference: {c['ref']}"]
            lines += [
                f"- {label}: {str(c.get(key, '')).strip() or '-'}"
                for label, key in READING_LIST_FIELDS
            ]
            if c["note"]:
                lines.append(f"- Your note: {c['note']}")
            lines.append("")
        lines.append("")
    path = project.reading_list
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def save_candidate(project: Project, entry: dict[str, Any]) -> dict[str, Any]:
    """Record a proposed work (unless it's a duplicate) and refresh the list."""
    result = ledger.propose_candidate(project, entry)
    render_reading_list(project)
    result["list"] = project.rel(project.reading_list)
    return result


CANDIDATE_REQUIRED = ("url_or_doi", *ledger.CANDIDATE_FIELDS)


def save_candidates(
    project: Project, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """Record several assessed works in one call, reporting each result."""
    if not entries:
        return {"ok": False, "error": "No candidates supplied."}
    results: list[dict[str, Any]] = []
    saved = duplicates = errors = 0
    for entry in entries:
        if not isinstance(entry, dict):
            results.append({"ok": False, "error": "Each candidate must be an object."})
            errors += 1
            continue
        missing = [
            field
            for field in CANDIDATE_REQUIRED
            if not str(entry.get(field, "")).strip()
        ]
        if missing:
            results.append(
                {
                    "ok": False,
                    "error": "Missing required fields: " + ", ".join(missing) + ".",
                    "title": str(entry.get("title", "")).strip(),
                }
            )
            errors += 1
            continue
        result = ledger.propose_candidate(project, entry)
        if result.get("ok"):
            saved += 1
        elif result.get("duplicate"):
            duplicates += 1
        else:
            errors += 1
        results.append(result)
    if results:
        render_reading_list(project)
    path = project.rel(project.reading_list)
    for result in results:
        result["list"] = path
    return {
        "ok": True,
        "saved": saved,
        "duplicates": duplicates,
        "errors": errors,
        "results": results,
        "list": path,
    }
