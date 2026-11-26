"""Cards, maps, manuscript and briefing with a scripted stand-in for the model."""

import shutil

import pytest

from nfh import cards, corpus_map, manuscript
from nfh.brief import build_brief
from nfh.check import check_quotes
from nfh.claims import update_claim
from nfh.ingest import ingest_local
from nfh.store import Store

from .conftest import SAMPLE


def fake_chat(project, system, user, schema, think=True, retries=1):
    props = schema["properties"]
    if "summary" in props and "claims" in props:  # card unit
        return {
            "summary": "Argues flexibility is illusory.",
            "claims": [
                {
                    "claim": "Flexibility is illusory",
                    "evidence": "survey",
                    "location": "para 3",
                },
                {"claim": "Bogus", "evidence": "none", "location": "p. 999"},
            ],
            "concepts": [
                {"term": "deactivation", "definition": "removal", "location": "para 2"}
            ],
            "passages": [
                {
                    "location": "para 3",
                    "what": "key line",
                    "why": "thesis",
                    "quote": "flexibility is largely illusory",
                },
                {
                    "location": "para 2",
                    "what": "made up",
                    "why": "test",
                    "quote": "this sentence is not in the source at all",
                },
            ],
            "interlocutors": [],
            "limits": [],
        }
    if "thesis" in props:  # combine
        return {
            "thesis": "Platform work is less flexible than claimed.",
            "structure": [{"part": "Whole", "locations": "paras 1-3", "argument": "x"}],
            "concepts": [],
            "claims": [
                {
                    "claim": "Flexibility is illusory",
                    "evidence": "survey",
                    "location": "para 3",
                }
            ],
            "passages": [
                {
                    "location": "para 3",
                    "what": "key",
                    "why": "thesis",
                    "quote": "flexibility is largely illusory",
                }
            ],
            "interlocutors": [],
            "limits": [],
        }
    if "relevance" in props:
        return {
            "relevance": [
                {"question": "Q1", "role": "central", "note": "n", "read": "para 3"}
            ],
            "overall": "Useful.",
        }
    if "overview" in props:
        return {
            "overview": "One source.",
            "themes": [
                {
                    "name": "Flexibility",
                    "description": "d",
                    "members": [{"cite_key": "writer2024", "stance": "sceptical"}],
                }
            ],
            "debates": [],
            "coverage": [
                {
                    "question": "Q1",
                    "rating": "thin",
                    "sources": ["writer2024"],
                    "note": "one source",
                }
            ],
            "lineage": [],
            "gaps": ["Non-delivery platforms"],
        }
    if "argument" in props:  # chapter note
        return {
            "argument": [{"step": "Flexibility is a myth", "paras": "1-2"}],
            "claims": [
                {
                    "claim": "Most riders depend on one platform",
                    "type": "empirical",
                    "para": 1,
                }
            ],
            "terms": [],
            "threads": [],
        }
    if "argument_line" in props:
        return {
            "files": [{"file": "drafts/intro.md", "summary": "s"}],
            "argument_line": "line",
            "glossary": [],
            "promises": [],
            "overlaps": [],
        }
    raise AssertionError(f"unexpected schema {list(props)}")


@pytest.fixture
def scripted(monkeypatch, fake_ollama):
    for module in (cards, corpus_map, manuscript):
        monkeypatch.setattr(module, "chat_json", fake_chat)


def test_card_drops_bad_locations_and_quotes(project, scripted):
    project.project_md.write_text("# T\n\n## Open questions\n- Is it flexible?\n")
    (project.sources_local_dir / "r.md").write_text(SAMPLE)
    (project.sources_local_dir / "r.md.yaml").write_text("cite_key: writer2024\n")
    ingest_local(project, say=lambda _: None)
    store = Store(project)
    source = store.source("writer2024")
    path = cards.build_card(project, store, source, say=lambda _: None)
    text = path.read_text()
    assert "Platform work is less flexible" in text
    unit = store.conn.execute("SELECT output_json FROM units").fetchone()[0]
    assert "Bogus" not in unit and "not in the source at all" not in unit
    note = cards.project_note_path(project, "writer2024").read_text()
    assert "central" in note
    from nfh import ledger

    ledger.set_reading_status(project, "writer2024", "read", "chapter 2 only")
    shown = cards.read_card(project, "writer2024")
    assert "Your reading of this source: read (chapter 2 only)" in shown
    assert cards.stale_sources(project, store) == []
    assert search_card_hit(store)

    corpus_map.build_map(project, say=lambda _: None)
    assert "Flexibility" in (project.corpus_dir / "map.md").read_text()

    results = check_quotes(project)
    assert results and all(r.status == "exact" for r in results)
    store.close()


def search_card_hit(store):
    from nfh.search import search

    return search(store, "less flexible than claimed", kind="card", limit=1)


@pytest.mark.skipif(not shutil.which("pandoc"), reason="pandoc missing")
def test_manuscript_claims_and_brief(project, scripted):
    (project.drafts_dir / "intro.md").write_text(
        "# Intro\n\nMost riders depend on one platform "
        "[@writer2024, para 2].\n\nMore.\n"
    )
    done = manuscript.build_manuscript(project, say=lambda _: None)
    assert len(done) == 1
    store = Store(project)
    row = store.conn.execute("SELECT * FROM claims").fetchone()
    assert row["id"] == "intro-c01" and row["cited"] == "@writer2024, para 2"
    assert update_claim(
        store,
        "intro-c01",
        "supported",
        [{"citation": "[@writer2024, para 2]", "note": "ok"}],
    )["ok"]
    # Unchanged file: nothing re-mapped, claim keeps its status.
    assert manuscript.build_manuscript(project, say=lambda _: None) == []
    status = store.conn.execute("SELECT status FROM claims").fetchone()[0]
    assert status == "supported"
    store.close()
    text = build_brief(project)
    assert "drafts/intro.md" in text and "Claims: 1 total" in text
    assert "1 supported" in project.claims_path.read_text()
