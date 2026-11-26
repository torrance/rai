from pathlib import Path

import pymupdf

from nfh.chunk import make_chunks, make_units, marked_text
from nfh.extract import _plain, extract

from .conftest import SAMPLE


def test_plain_text_blocks_have_headings_and_paras():
    result = _plain(SAMPLE)
    paras = [b for b in result.blocks if b["kind"] == "paragraph"]
    assert len(paras) == 3
    assert paras[0]["para"] == 1
    assert paras[-1]["heading_path"] == "Labour and platforms > Evidence"
    assert "algorithmic management. Workers are" in paras[0]["text"]


def test_hyphenation_is_rejoined():
    result = _plain("A long word: algo-\nrithmic management.")
    assert "algorithmic" in result.blocks[0]["text"]


def test_pdf_page_labels(tmp_path: Path):
    path = tmp_path / "doc.pdf"
    doc = pymupdf.open()
    for i in range(4):
        page = doc.new_page()
        page.insert_text((72, 72), f"Paragraph text on page {i}. " * 12)
    doc.set_page_labels(
        [
            {"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
            {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1},
        ]
    )
    doc.save(path)
    result = extract(path, pdf_engine="pymupdf")
    labels = [b["page_label"] for b in result.blocks]
    assert labels[0] == "i"
    assert "1" in labels and "2" in labels


def test_chunks_cover_every_block_and_respect_size():
    blocks = _plain(SAMPLE * 20).blocks
    chunks = make_chunks(blocks, min_tokens=30, max_tokens=120)
    covered = set()
    for c in chunks:
        covered.update(range(c["first_block"], c["last_block"] + 1))
    assert covered == {b["seq"] for b in blocks}
    assert all(c["loc_label"] for c in chunks)


def test_units_and_markers():
    blocks = _plain(SAMPLE).blocks
    units = make_units(blocks, 10, 40, single_unit_tokens=10)
    assert len(units) >= 1
    text = marked_text(units[0]["blocks"])
    assert "[para 1]" in text
