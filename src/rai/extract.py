"""Turn source files into ordered blocks with citable locations.

Every extractor returns the same shape: a list of dicts with ``seq``, ``kind``,
``heading_path``, ``text`` and whichever location fields the format supports
(``page_index``/``page_label`` for PDF, ``chapter``/``para`` for EPUB,
``para``/``anchor`` for HTML and text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PDF_SUFFIXES = {".pdf"}
EPUB_SUFFIXES = {".epub"}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
SUPPORTED = PDF_SUFFIXES | EPUB_SUFFIXES | HTML_SUFFIXES | TEXT_SUFFIXES

SKIP_LABELS = {"page_header", "page_footer", "picture"}
HEADING_LABELS = {"title", "section_header"}


@dataclass
class Extracted:
    blocks: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    detail: str = ""
    pages: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def clean_text(text: str) -> str:
    """Rejoin line-end hyphenation and collapse whitespace."""
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class _Headings:
    """Track the current heading path as headings of different levels appear."""

    def __init__(self) -> None:
        self.stack: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> None:
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        self.stack.append((level, title))

    @property
    def path(self) -> str:
        return " > ".join(t for _, t in self.stack)


def extract(path: Path, pdf_engine: str = "docling") -> Extracted:
    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        if pdf_engine == "docling":
            try:
                result = _pdf_docling(path)
            except ImportError:
                result = _pdf_pymupdf(path)
                result.detail = "docling unavailable; used PyMuPDF"
        else:
            result = _pdf_pymupdf(path)
        _apply_page_labels(path, result)
        _quality_gate(result)
        return result
    if suffix in EPUB_SUFFIXES:
        return _epub(path)
    if suffix in HTML_SUFFIXES:
        return _html(path.read_text(encoding="utf-8", errors="replace"))
    if suffix in TEXT_SUFFIXES:
        return _plain(path.read_text(encoding="utf-8", errors="replace"))
    return Extracted(status="failed", detail=f"unsupported file type {suffix}")


# PDF -------------------------------------------------------------------------
def _pdf_docling(path: Path) -> Extracted:
    import os

    from .config import MODEL_CACHE

    os.environ.setdefault("HF_HOME", str(MODEL_CACHE))  # before Docling imports
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = False  # scanned PDFs are flagged; OCR them with OCRmyPDF
    # Layout models run on this machine's CPU, never a local GPU. The chat and
    # embedding models run on the Ollama server.
    options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU)
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    doc = converter.convert(str(path)).document
    result = Extracted(pages=len(getattr(doc, "pages", {}) or {}))
    headings = _Headings()
    for item, _level in doc.iterate_items():
        raw_label = getattr(item, "label", "")
        label = str(getattr(raw_label, "value", raw_label))
        if label in SKIP_LABELS:
            continue
        if label == "table" and hasattr(item, "export_to_markdown"):
            text = item.export_to_markdown(doc)
        else:
            text = getattr(item, "text", "") or ""
        text = clean_text(text)
        if not text:
            continue
        prov = getattr(item, "prov", None) or []
        page_no = prov[0].page_no if prov else None
        if label in HEADING_LABELS:
            level = 0 if label == "title" else int(getattr(item, "level", 1) or 1)
            headings.push(level, text[:120])
            kind = "heading"
        elif label == "footnote":
            kind = "footnote"
        elif label == "table":
            kind = "table"
        else:
            kind = "paragraph"
        result.blocks.append(
            {
                "seq": len(result.blocks),
                "kind": kind,
                "heading_path": headings.path,
                "text": text,
                "page_index": page_no,
            }
        )
    return result


def _pdf_pymupdf(path: Path) -> Extracted:
    import pymupdf

    result = Extracted()
    with pymupdf.open(path) as doc:
        result.pages = doc.page_count
        for index, page in enumerate(doc, start=1):
            for block in page.get_text("blocks", sort=True):
                text = clean_text(block[4])
                if not text or re.fullmatch(r"[\divxlc]+", text, re.IGNORECASE):
                    continue  # bare page numbers
                result.blocks.append(
                    {
                        "seq": len(result.blocks),
                        "kind": "paragraph",
                        "heading_path": "",
                        "text": text,
                        "page_index": index,
                    }
                )
    return result


def _apply_page_labels(path: Path, result: Extracted) -> None:
    """Map 1-based page indices to printed page labels where the PDF has them."""
    labels: dict[int, str] = {}
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            result.pages = result.pages or doc.page_count
            for index, page in enumerate(doc, start=1):
                label = page.get_label()
                if label:
                    labels[index] = label
    except Exception:  # noqa: BLE001 - labels are a nicety, not a requirement
        labels = {}
    for block in result.blocks:
        index = block.get("page_index")
        if index is None:
            continue
        # Without printed labels, fall back to the file's page number.
        block["page_label"] = labels.get(index, str(index))
    if not labels:
        note = "no printed page labels; locations use the file's page numbers"
        result.detail = f"{result.detail}; {note}".strip("; ")


def _quality_gate(result: Extracted) -> None:
    words = sum(len(b["text"].split()) for b in result.blocks)
    pages = max(result.pages, 1)
    per_page = words / pages
    if per_page < 30:
        result.status = "needs_ocr"
        result.detail = f"{per_page:.0f} words per page; run OCRmyPDF and re-ingest"
    elif per_page < 100:
        result.status = "low_text"
        result.detail = f"only {per_page:.0f} words per page; check with rai inspect"


# EPUB ------------------------------------------------------------------------
def _epub(path: Path) -> Extracted:
    from bs4 import BeautifulSoup
    from ebooklib import ITEM_DOCUMENT, epub

    book = epub.read_epub(str(path), options={"ignore_ncx": True})
    result = Extracted()
    titles = book.get_metadata("DC", "title")
    creators = book.get_metadata("DC", "creator")
    dates = book.get_metadata("DC", "date")
    result.metadata = {
        "title": titles[0][0] if titles else "",
        "authors": [c[0] for c in creators],
        "year": (dates[0][0][:4] if dates else ""),
    }
    chapter = 0
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        elements = soup.find_all(["h1", "h2", "h3", "h4", "p", "blockquote", "li"])
        if not any(clean_text(e.get_text(" ")) for e in elements):
            continue
        chapter += 1
        headings = _Headings()
        para = 0
        for el in elements:
            text = clean_text(el.get_text(" "))
            if not text:
                continue
            if el.name in {"h1", "h2", "h3", "h4"}:
                headings.push(int(el.name[1]), text[:120])
                kind = "heading"
            else:
                para += 1
                kind = "quote" if el.name == "blockquote" else "paragraph"
            result.blocks.append(
                {
                    "seq": len(result.blocks),
                    "kind": kind,
                    "heading_path": headings.path,
                    "text": text,
                    "chapter": chapter,
                    "para": para if kind != "heading" else None,
                }
            )
    return result


# HTML and text ---------------------------------------------------------------
def _html(html: str) -> Extracted:
    import trafilatura

    markdown = trafilatura.extract(
        html,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    metadata = trafilatura.extract_metadata(html)
    result = _plain(markdown or "", anchors=True)
    if metadata is not None:
        result.metadata = {
            "title": metadata.title or "",
            "authors": [a.strip() for a in (metadata.author or "").split(";") if a],
            "year": (metadata.date or "")[:4],
            "url": metadata.url or "",
        }
    if not result.blocks:
        result.status = "low_text"
        result.detail = "no main text found in the page"
    return result


def _plain(text: str, anchors: bool = False) -> Extracted:
    result = Extracted()
    headings = _Headings()
    para = 0
    for raw in re.split(r"\n\s*\n", text):
        chunk = raw.strip()
        if not chunk:
            continue
        match = re.match(r"^(#{1,6})\s+(.*)", chunk)
        if match and "\n" not in chunk:
            title = clean_text(match.group(2))
            headings.push(len(match.group(1)), title[:120])
            result.blocks.append(
                {
                    "seq": len(result.blocks),
                    "kind": "heading",
                    "heading_path": headings.path,
                    "text": title,
                    "anchor": title if anchors else None,
                }
            )
            continue
        para += 1
        result.blocks.append(
            {
                "seq": len(result.blocks),
                "kind": "quote" if chunk.startswith(">") else "paragraph",
                "heading_path": headings.path,
                "text": clean_text(chunk.lstrip("> ")),
                "para": para,
                "anchor": (
                    headings.stack[-1][1] if anchors and headings.stack else None
                ),
            }
        )
    return result


def extract_html_string(html: str) -> Extracted:
    """Used by the candidate reader for fetched web pages."""
    return _html(html)
