import numpy as np
import pytest

from nfh.ingest import ingest_local, reembed
from nfh.search import read_location, search
from nfh.store import Store
from nfh.verify import verify_quote

from .conftest import SAMPLE


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


def test_removed_local_file_leaves_index(project, fake_ollama):
    _ingest(project)
    (project.sources_local_dir / "riders.md").unlink()
    report = ingest_local(project, say=lambda _: None)
    assert report.removed == ["writer2024"]


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
