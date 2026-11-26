import json

import numpy as np
import pytest

from nfh import ledger
from nfh.ingest import ingest_local, reembed
from nfh.notes import read_notes
from nfh.search import (
    read_location,
    resolve_location,
    resolve_location_parts,
    search,
)
from nfh.status import embedding_problem
from nfh.store import Store
from nfh.verify import verify_quote

from .conftest import SAMPLE, fake_vectors


def _ingest(project):
    (project.sources_local_dir / "riders.md").write_text(SAMPLE)
    (project.sources_local_dir / "riders.md.yaml").write_text(
        "title: Riders\nauthors: [A. Writer]\nyear: 2024\ncite_key: writer2024\n"
    )
    return ingest_local(project, say=lambda _: None)


def test_local_ingest_skips_readme_and_is_idempotent(project, fake_ollama):
    report = _ingest(project)
    assert report.added == ["writer2024"]
    again = ingest_local(project, say=lambda _: None)
    assert again.unchanged == ["writer2024"] and not again.added


def test_reembed_can_change_provider_and_dimensions(project, fake_ollama, monkeypatch):
    _ingest(project)
    project.models.embedding = "opencode-go/e"
    project.models.embedding_dims = 8
    monkeypatch.setattr(
        "nfh.ingest.embed_texts",
        lambda p, texts: np.zeros((len(texts), 8), dtype=np.float32),
    )
    assert reembed(project) > 0
    store = Store(project)
    assert store.get_meta("embed_ref") == "opencode-go/e"
    assert store.get_meta("embed_dims") == "8"
    blob = store.conn.execute("SELECT embedding FROM chunks LIMIT 1").fetchone()[0]
    assert len(blob) == 8 * 4
    store.close()


def test_reembed_on_an_empty_index_updates_the_vector_setup(project):
    project.models.embedding = "opencode-go/e"
    project.models.embedding_dims = 8
    assert reembed(project) == 0
    store = Store(project)
    assert store.get_meta("embed_ref") == "opencode-go/e"
    assert store.get_meta("embed_dims") == "8"
    store.close()


def test_search_refuses_an_unreembedded_embedding_change(project, fake_ollama):
    _ingest(project)
    project.models.embedding_dims = 8
    store = Store(project)
    with pytest.raises(RuntimeError, match="--reembed"):
        search(store, "deactivation")
    store.close()


def test_search_read_and_verify(project, fake_ollama):
    _ingest(project)
    store = Store(project)
    hits = search(store, "deactivation threat income", limit=3)
    assert hits and hits[0]["cite_key"] == "writer2024"
    assert hits[0]["citation"].startswith("[@writer2024, para")

    read = read_location(store, "writer2024", "para 2", radius=0)
    assert "deactivation" in read["text"]

    exact = verify_quote(
        store, "writer2024", "flexibility is largely illusory", "para 3"
    )
    assert exact["status"] == "exact"
    punctuated = verify_quote(
        store, "writer2024", "flexibility is largely illusory.", "para 3"
    )
    assert punctuated["status"] == "exact"
    near = verify_quote(
        store,
        "writer2024",
        "flexibility is mostly illusory for full-time riders",
        "para 3",
    )
    assert near["status"] in {"near", "not_found"}
    store.close()
    filler = "\n\n".join(
        f"Filler paragraph number {i} about nothing." for i in range(12)
    )
    (project.sources_local_dir / "long.md").write_text(
        "Customers rate every courier after delivery.\n\n" + filler
    )
    ingest_local(project, say=lambda _: None)
    store = Store(project)
    elsewhere = verify_quote(store, "local-long", "rate every courier", "para 12")
    assert elsewhere["status"] == "elsewhere" and elsewhere["location"] == "para 1"
    missing = verify_quote(store, "writer2024", "nothing like this appears anywhere")
    assert missing["status"] == "not_found"
    store.close()


def test_search_can_be_restricted_and_balanced_across_sources(project, fake_ollama):
    _ingest(project)
    (project.sources_local_dir / "other.md").write_text(
        "# Other work\n\nPlatform labour is debated. Managers use dashboards, "
        "and workers organise around scheduling and pay.\n"
    )
    (project.sources_local_dir / "other.md.yaml").write_text(
        "title: Other work\nauthors: [B. Writer]\nyear: 2023\n"
        "cite_key: other2023\n"
    )
    ingest_local(project, say=lambda _: None)
    store = Store(project)

    one = search(store, "platform", sources=["writer2024"], limit=10)
    assert one and {hit["cite_key"] for hit in one} == {"writer2024"}
    other = search(store, "platform", sources=["other2023"], limit=10)
    assert other and {hit["cite_key"] for hit in other} == {"other2023"}

    balanced = search(store, "platform", limit=10, per_source_limit=1)
    keys = [hit["cite_key"] for hit in balanced]
    assert len(keys) == len(set(keys))
    assert set(keys) <= {"writer2024", "other2023"}

    with pytest.raises(ValueError, match="No source matches"):
        search(store, "platform", sources=["missing"])
    store.close()


def test_read_location_toc_returns_the_heading_outline(project):
    store = Store(project)
    cur = store.conn.execute(
        "INSERT INTO sources(key, title, cite_key) VALUES('local:toc', 'Book', 'b')"
    )
    source_id = cur.lastrowid
    rows = [
        (source_id, 0, "Preface", "Preface", 1, "i"),
        (source_id, 1, "Preface > Chapter 1", "Chapter 1", 2, "1"),
        (source_id, 2, "Preface > Chapter 1 > Section 1.1", "Section 1.1", 3, "2"),
    ]
    store.conn.executemany(
        "INSERT INTO blocks(source_id, seq, kind, heading_path, text, "
        "page_index, page_label) VALUES(?,?,?,?,?,?,?)",
        [
            (sid, seq, "heading", path, text, page, label)
            for sid, seq, path, text, page, label in rows
        ],
    )
    store.conn.commit()
    toc = read_location(store, source_id, "toc")
    assert toc["range"] == "toc" and toc["cite_key"] == "b"
    assert [h["level"] for h in toc["headings"]] == [1, 2, 3]
    assert [h["location"] for h in toc["headings"]] == ["p. i", "p. 1", "p. 2"]
    assert "  - Chapter 1 (p. 1)" in toc["text"]
    assert read_location(store, source_id, "TOC")["headings"] == toc["headings"]
    store.close()


def test_toc_level_ignores_separators_inside_a_title(project):
    store = Store(project)
    cur = store.conn.execute(
        "INSERT INTO sources(key, title, cite_key) VALUES('local:toc2', 'T', 't')"
    )
    source_id = cur.lastrowid
    store.conn.executemany(
        "INSERT INTO blocks(source_id, seq, kind, heading_path, text, "
        "page_index, page_label) VALUES(?,?,?,?,?,?,?)",
        [
            (
                source_id,
                0,
                "heading",
                "Book > Part > Piece > Two",
                "Piece > Two",
                1,
                "1",
            ),
            (source_id, 1, "heading", "", "Loose heading", 2, "2"),
        ],
    )
    store.conn.commit()
    toc = read_location(store, source_id, "toc")
    assert [h["level"] for h in toc["headings"]] == [3, 1]
    store.close()


def test_read_location_toc_handles_sources_without_headings(project):
    store = Store(project)
    cur = store.conn.execute(
        "INSERT INTO sources(key, title, cite_key) VALUES('local:plain', 'P', 'p')"
    )
    source_id = cur.lastrowid
    store.conn.execute(
        "INSERT INTO blocks(source_id, seq, kind, text, para) "
        "VALUES(?, 0, 'paragraph', 'No headings here.', 1)",
        (source_id,),
    )
    store.conn.commit()
    toc = read_location(store, source_id, "toc")
    assert toc["headings"] == [] and "No headings were extracted" in toc["text"]
    assert "error" in read_location(store, "missing", "toc")
    store.close()


def test_toc_for_markdown_uses_seq_locations(project, fake_ollama):
    _ingest(project)
    store = Store(project)
    toc = read_location(store, "writer2024", "toc")
    assert [h["title"] for h in toc["headings"]] == [
        "Labour and platforms",
        "Introduction",
        "Evidence",
    ]
    assert all(h["location"].startswith("seq:") for h in toc["headings"])
    store.close()


def test_read_notes_sources_inventory(project, fake_ollama):
    _ingest(project)
    ledger.set_reading_status(project, "writer2024", "read")
    (project.state_dir / "citations.json").write_text(
        json.dumps({"writer2024": {"drafts/intro.md": 2}})
    )
    text = read_notes(project, "sources")
    assert text.startswith("1 sources indexed.")
    assert "| Cite key | Author (year) | Title | Kind |" in text
    assert "| writer2024 | A. Writer (2024) | Riders | note |" in text
    assert "| read | 2 |" in text


def test_embedding_status_on_empty_index_is_read_only(project):
    store = Store(project)
    assert (
        store.embedding_mismatch(
            project.models.embedding,
            project.models.embedding_dims,
            project.models.document_template,
        )
        == ""
    )
    assert store.get_meta("embed_ref") is None
    store.close()


def test_embedding_settings_can_change_before_ingest(project):
    store = Store(project)
    store.check_embedding_model(
        project.models.embedding, project.models.embedding_dims
    )
    store.check_embedding_model(project.models.embedding, 8)
    store.close()


def test_status_reports_an_embedding_mismatch(project, fake_ollama):
    _ingest(project)
    project.models.embedding_dims = 8
    assert "--reembed" in embedding_problem(project)


def test_source_filter_uses_exact_vector_search(project, fake_ollama, monkeypatch):
    monkeypatch.setattr("nfh.search.CANDIDATES", 1)
    store = Store(project)
    if not store.has_vec:
        pytest.skip("sqlite-vec is not available")
    target_id = store.conn.execute(
        "INSERT INTO sources(key, title, cite_key) "
        "VALUES('local:target', 'Target', 'target')"
    ).lastrowid
    filler_id = store.conn.execute(
        "INSERT INTO sources(key, title, cite_key) "
        "VALUES('local:filler', 'Filler', 'filler')"
    ).lastrowid
    qvec = fake_vectors(["targetword"])[0]
    target_vec = fake_vectors(["unrelated text"])[0]
    store._insert_chunks(
        target_id,
        [
            {
                "first_block": 0,
                "last_block": 0,
                "heading_path": "",
                "text": "unrelated text",
                "loc_label": "para 1",
            }
        ],
        np.array([target_vec], dtype=np.float32),
        kind="text",
    )
    filler = [
        {
            "first_block": n,
            "last_block": n,
            "heading_path": "",
            "text": "targetword",
            "loc_label": f"para {n + 1}",
        }
        for n in range(5)
    ]
    store._insert_chunks(
        filler_id,
        filler,
        np.array([qvec] * len(filler), dtype=np.float32),
        kind="text",
    )
    store.conn.commit()
    hits = search(store, "targetword", sources=["target"], limit=1)
    assert hits and hits[0]["cite_key"] == "target"
    store.close()


def test_store_sets_a_busy_timeout(project):
    store = Store(project)
    assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    store.close()


def test_read_location_reports_only_the_blocks_it_shows(project):
    store = Store(project)
    cur = store.conn.execute(
        "INSERT INTO sources(key, title, cite_key) VALUES('local:table', 'Table', 't')"
    )
    source_id = cur.lastrowid
    blocks = [
        (source_id, 0, "paragraph", "A short block."),
        (source_id, 1, "table", "Row one.\n\nRow two."),
        (source_id, 2, "paragraph", "x " * 2000),
    ]
    store.conn.executemany(
        "INSERT INTO blocks(source_id, seq, kind, text) VALUES(?,?,?,?)", blocks
    )
    store.conn.commit()
    read = read_location(store, source_id, "seq:0-2", radius=0, max_tokens=20)
    assert read["truncated"] is True
    assert read["range"] == "seq:0-1"
    assert "Row two." in read["text"] and "x x x" not in read["text"]
    store.close()


def test_page_locations_handle_roman_cover_and_missing_end_pages(project):
    store = Store(project)
    cur = store.conn.execute(
        "INSERT INTO sources(key, title, cite_key) VALUES('local:pages', 'P', 'p')"
    )
    source_id = cur.lastrowid
    labels = ["C1", "i", "iv", "v", "viii", "1", "2", "79", "80", "82"]
    store.conn.executemany(
        "INSERT INTO blocks(source_id, seq, kind, text, page_index, page_label) "
        "VALUES(?,?,?,?,?,?)",
        [
            (source_id, seq, "paragraph", f"text {label}", seq + 1, label)
            for seq, label in enumerate(labels)
        ],
    )
    store.conn.commit()
    assert resolve_location(store, source_id, "pp. C1-2") == (0, 6)
    assert resolve_location(store, source_id, "pp. IV-VIII") == (2, 4)
    assert resolve_location(store, source_id, "IV-VIII") == (2, 4)
    assert resolve_location(store, source_id, "pp. 79-81") == (7, 8)
    assert resolve_location(store, source_id, "pp. 79, 82") == (7, 7)
    assert resolve_location_parts(store, source_id, "pp. 79, 82") == [(7, 7), (9, 9)]
    assert resolve_location(store, source_id, "p. 0") is None
    assert resolve_location(store, source_id, "pp. 79-999") is None
    read = read_location(store, source_id, "pp. C1-2", radius=0)
    assert read["range"] == "seq:0-6" and read["text"].startswith("[p. C1]")
    store.close()


def test_discontinuous_page_locations_do_not_bridge(project):
    from nfh.cards import _location_ok

    store = Store(project)
    cur = store.conn.execute(
        "INSERT INTO sources(key, title, cite_key) VALUES('local:gap', 'G', 'g')"
    )
    source_id = cur.lastrowid
    store.conn.executemany(
        "INSERT INTO blocks(source_id, seq, kind, text, page_index, page_label) "
        "VALUES(?,?,?,?,?,?)",
        [
            (source_id, 0, "paragraph", "page five", 1, "5"),
            (source_id, 1, "paragraph", "page one ten", 2, "110"),
            (source_id, 2, "paragraph", "page three ten", 3, "310"),
        ],
    )
    store.conn.commit()
    assert resolve_location(store, source_id, "pp. 5, 310") == (0, 0)
    assert not _location_ok(
        store, source_id, "pp. 5, 310", {"first_block": 1, "last_block": 1}
    )
    assert _location_ok(
        store, source_id, "pp. 5, 310", {"first_block": 0, "last_block": 0}
    )
    store.close()


def test_removed_local_file_leaves_index(project, fake_ollama):
    _ingest(project)
    (project.sources_local_dir / "riders.md").unlink()
    report = ingest_local(project, say=lambda _: None)
    assert report.removed == ["writer2024"]


def test_removed_source_cleans_up_cards_notes_and_index(project, fake_ollama):
    from nfh.cards import card_path, project_note_path, write_with_frontmatter

    _ingest(project)
    card = card_path(project, "writer2024")
    previous = card.with_suffix(".prev.md")
    note = project_note_path(project, "writer2024")
    for path in (card, previous, note):
        write_with_frontmatter(path, {"cite_key": "writer2024"}, "x")
    store = Store(project)
    for path in (card, previous, note):
        store.record_note(project.rel(path), "card", {}, "m", "1")
    store.close()

    (project.sources_local_dir / "riders.md").unlink()
    report = ingest_local(project, say=lambda _: None)
    assert report.removed == ["writer2024"]
    assert not any(path.exists() for path in (card, previous, note))
    store = Store(project)
    assert all(store.note(project.rel(path)) is None for path in (card, previous, note))
    store.close()


def test_content_change_with_a_new_key_moves_the_card(project, fake_ollama):
    from nfh.cards import card_path, project_note_path, write_with_frontmatter

    _ingest(project)
    old_card = card_path(project, "writer2024")
    old_note = project_note_path(project, "writer2024")
    write_with_frontmatter(old_card, {"cite_key": "writer2024"}, "card")
    write_with_frontmatter(old_note, {"cite_key": "writer2024"}, "note")

    (project.sources_local_dir / "riders.md").write_text(
        SAMPLE + "\n\nA new paragraph.\n"
    )
    (project.sources_local_dir / "riders.md.yaml").write_text(
        "title: Riders\nauthors: [A. Writer]\nyear: 2024\ncite_key: writer2024new\n"
    )
    report = ingest_local(project, say=lambda _: None)
    assert report.renamed == [("writer2024", "writer2024new")]
    assert report.updated == ["writer2024new"]
    assert not old_card.exists() and not old_note.exists()
    assert "cite_key: writer2024new" in card_path(project, "writer2024new").read_text()
    assert "cite_key: writer2024new" in project_note_path(
        project, "writer2024new"
    ).read_text()


def test_key_change_in_zotero_moves_card_and_note(project, fake_ollama):
    from nfh.cards import card_path, project_note_path, write_with_frontmatter
    from nfh.status import compute

    _ingest(project)
    from nfh import ledger

    ledger.set_reading_status(project, "writer2024", "read")
    write_with_frontmatter(
        card_path(project, "writer2024"), {"cite_key": "writer2024"}, "x"
    )
    write_with_frontmatter(
        project_note_path(project, "writer2024"),
        {"cite_key": "writer2024"},
        "y",
    )
    (project.sources_local_dir / "riders.md.yaml").write_text(
        "title: Riders\nauthors: [A. Writer]\nyear: 2024\ncite_key: writer2024riders\n"
    )
    report = ingest_local(project, say=lambda _: None)
    assert report.renamed == [("writer2024", "writer2024riders")]
    assert not report.added and not report.updated
    assert not card_path(project, "writer2024").exists()
    note = project_note_path(project, "writer2024riders").read_text()
    assert "cite_key: writer2024riders" in note
    from nfh import ledger

    assert ledger.reading_statuses(project)["writer2024riders"]["status"] == "read"
    store = Store(project)
    assert store.source("writer2024riders") is not None
    store.close()

    (project.state_dir / "citations.json").write_text('{"writer2024": {"d.md": 1}}')
    assert compute(project).unknown_citations == ["writer2024"]
