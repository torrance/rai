"""Docling and PyMuPDF extract real PDFs with printed page labels."""

from __future__ import annotations

import pymupdf
import pytest

from nfh.extract import extract
from nfh.zotero import Zotero

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def sample_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("pdf") / "sample.pdf"
    doc = pymupdf.open()
    for i in range(4):
        page = doc.new_page()
        page.insert_text((72, 80), f"Chapter {i + 1}", fontsize=22)
        page.insert_text(
            (72, 130), f"Body text on printed page {i}. " * 10, fontsize=10
        )
    doc.set_page_labels(
        [
            {"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
            {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1},
        ]
    )
    doc.save(path)
    return path


def test_docling_is_installed_and_used(sample_pdf):
    pytest.importorskip("docling", reason="run `uv sync` to install Docling")
    result = extract(sample_pdf, pdf_engine="docling")
    assert "docling unavailable" not in result.detail
    assert result.blocks, "Docling returned no text"
    labels = {b.get("page_label") for b in result.blocks}
    assert "i" in labels and "1" in labels, f"page labels lost: {labels}"


def test_a_real_pdf_from_the_project(live_project):
    if live_project is None:
        pytest.skip("pass --live-project to extract one of its PDFs")
    z = live_project.zotero
    items = Zotero(z).items(collection=z.collection, tags=z.tags, tag_match=z.tag_match)
    pdfs = [i for i in items if i.file and i.file.suffix.lower() == ".pdf"]
    if not pdfs:
        pytest.skip("no PDFs in the project's scope")
    result = extract(pdfs[0].file, pdf_engine="docling")
    assert result.status in {"ok", "low_text"}, (
        f"{pdfs[0].cite_key}: {result.status} ({result.detail})"
    )
    assert any(b.get("kind") == "heading" for b in result.blocks), (
        "no headings detected; cards will treat the whole text as one unit"
    )
