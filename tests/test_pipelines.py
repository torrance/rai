"""Cards, maps, manuscript and briefing with a scripted stand-in for the model."""

import json
import shutil

import pytest

from rai import cards, corpus_map, manuscript
from rai.brief import build_brief
from rai.check import check_quotes
from rai.claims import update_claim
from rai.ingest import ingest_local
from rai.status import compute
from rai.store import Store

from .conftest import SAMPLE

STAGES: list[str] = []


def fake_chat(project, system, user, schema, think=True, retries=1, *, stage=""):
    STAGES.append(stage)
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
            "structure": [
                {"part": "Whole", "locations": "paras 1-3", "argument": "x"},
                {"part": "Bogus part", "locations": "p. 999", "argument": "bogus"},
            ],
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
                },
                {
                    "location": "para 3",
                    "what": "hallucinated in the combine step",
                    "why": "test",
                    "quote": "this sentence is not in the source at all",
                },
                {
                    "location": "p. 999",
                    "what": "impossible location in the combine step",
                    "why": "test",
                },
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
    STAGES.clear()
    for module in (cards, corpus_map, manuscript):
        monkeypatch.setattr(module, "chat_json", fake_chat)


def test_card_drops_bad_locations_and_quotes(project, scripted):
    project.project_md.write_text("# T\n\n## Open questions\n- Is it flexible?\n")
    (project.sources_local_dir / "r.md").write_text(SAMPLE)
    (project.sources_local_dir / "r.md.yaml").write_text("cite_key: writer2024\n")
    ingest_local(project, say=lambda _: None)
    store = Store(project)
    source = store.source("writer2024")
    messages: list[str] = []
    path = cards.build_card(
        project, store, source, say=messages.append
    )
    text = path.read_text()
    assert "Platform work is less flexible" in text
    assert "this sentence is not in the source at all" not in text
    assert "impossible location in the combine step" not in text
    assert "hallucinated in the combine step" not in text
    assert "Bogus part" not in text
    assert any(
        "locations verified" in message
        and "dropped" in message
        and message.rstrip().endswith("s)")
        for message in messages
    )
    unit = store.conn.execute("SELECT output_json FROM units").fetchone()[0]
    assert "Bogus" not in unit and "not in the source at all" not in unit
    assert "made up" not in unit
    note = cards.project_note_path(project, "writer2024").read_text()
    assert "central" in note
    from rai import ledger

    ledger.set_reading_status(project, "writer2024", "read", "chapter 2 only")
    shown = cards.read_card(project, "writer2024")
    assert "Your reading of this source: read (chapter 2 only)" in shown
    assert cards.stale_sources(project, store) == []
    assert search_card_hit(store)

    corpus_map.build_map(project, say=lambda _: None)
    assert "Flexibility" in (project.corpus_dir / "map.md").read_text()

    assert STAGES[0] == "reading"
    assert "summarising" in STAGES
    assert set(STAGES) == {"reading", "summarising"}

    # A stage-model change makes the card stale even when the source is unchanged.
    project.models.summarising = "ollama/other-summary-model"
    assert [s["cite_key"] for s in cards.stale_sources(project, store)] == [
        "writer2024"
    ]

    # An old bare model name means the same Ollama model: units are reused.
    store.conn.execute(
        "UPDATE units SET model='qwen3.8-flash-next:125b-a6b-q4_K_M'"
    )
    STAGES.clear()
    cards.build_card(project, store, source, say=lambda _: None)
    assert "reading" not in STAGES
    meta, _ = cards.read_frontmatter(cards.card_path(project, "writer2024"))
    assert int(meta["checks"].split()[0]) > 0
    assert "1 structure row dropped" in meta["checks"]

    # The corpus map is also built from the open questions, not just cards.
    project.project_md.write_text(
        "# T\n\n## Open questions\n- Is it flexible?\n- What changed later?\n"
    )
    assert compute(project).corpus_map_stale

    results = check_quotes(project)
    assert results and all(r.status == "exact" for r in results)
    store.close()


def test_render_card_marks_an_empty_structure():
    card = {
        "thesis": "t",
        "structure": [],
        "concepts": [],
        "claims": [],
        "passages": [],
        "interlocutors": [],
        "limits": [],
    }
    text = cards.render_card({"title": "T"}, card)
    assert "| (none recorded) | | |" in text


def test_render_card_escapes_pipes_in_table_cells():
    card = {
        "thesis": "t",
        "structure": [{"part": "A | B", "locations": "p. 1", "argument": "x | y"}],
        "concepts": [],
        "claims": [],
        "passages": [],
        "interlocutors": [],
        "limits": [],
    }
    text = cards.render_card({"title": "T"}, card)
    assert "| A / B | p. 1 | x / y |" in text


def test_combine_batches_long_books(project, monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake(project, system, user, schema, think=True, retries=1, *, stage=""):
        calls.append((system, user))
        return {
            "thesis": "t",
            "structure": [],
            "concepts": [],
            "claims": [],
            "passages": [],
            "interlocutors": [],
            "limits": [],
        }

    monkeypatch.setattr(cards, "chat_json", fake)
    monkeypatch.setattr(cards, "load_prompt", lambda name: name)
    outputs = [
        {
            "summary": f"unit {n}",
            "claims": [],
            "concepts": [],
            "passages": [],
            "interlocutors": [],
            "limits": [],
        }
        for n in range(cards.COMBINE_BATCH_UNITS + 1)
    ]
    result = cards._combine_units(
        project,
        {"title": "T", "year": "2024", "authors": "[]"},
        outputs,
        say=lambda _: None,
    )
    assert result["thesis"] == "t"
    assert [system for system, _ in calls] == ["card_combine", "card_merge"]
    assert "Further unit readings" in calls[1][1]


def test_render_map_escapes_pipes_and_avoids_an_empty_gap(project):
    m = {
        "overview": "o",
        "themes": [],
        "debates": [],
        "coverage": [
            {
                "question": "A | B",
                "rating": "thin",
                "sources": ["writer2024"],
                "note": "n | m",
            }
        ],
        "lineage": [],
        "gaps": [],
    }
    text = corpus_map.render_map(m, 1)
    assert "| A / B | thin | writer2024 | n / m |" in text
    assert "## Gaps\n- (none noted)" in text


def test_interlocutors_section_is_available_for_large_libraries():
    card = (
        "## Main claims and evidence\n- A claim\n\n"
        "## Interlocutors\n- Wright: responds to Hahnel\n\n"
        "## Limits\n- none\n"
    )
    assert corpus_map._interlocutors_section(card) == (
        "## Interlocutors\n- Wright: responds to Hahnel"
    )


def test_quote_patterns_handle_internal_quotes_and_multi_citations():
    from rai.check import CARD_QUOTE, NOTE_QUOTE

    line = (
        '- para 3: key line Why it matters: thesis. Quote: '
        '"What we need, then, is "real utopias": utopian ideals"'
    )
    card_match = CARD_QUOTE.search(line)
    assert card_match
    assert card_match.group(2) == (
        'What we need, then, is "real utopias": utopian ideals'
    )
    note = '"a quoted passage that is long enough" [@one, p. 4; @two, p. 8]'
    note_match = NOTE_QUOTE.search(note)
    assert note_match
    assert note_match.group(2) == "one" and note_match.group(3) == "p. 4"
    punctuated = '"a quoted passage that is long enough". [@one, p. 4]'
    punctuated_match = NOTE_QUOTE.search(punctuated)
    assert punctuated_match
    assert punctuated_match.group(2) == "one"
    assert punctuated_match.group(3) == "p. 4"


def test_project_note_staleness_tracks_draft_citations(project, scripted):
    project.project_md.write_text("# T\n\n## Open questions\n- Is it flexible?\n")
    card = cards.card_path(project, "writer2024")
    cards.write_with_frontmatter(card, {"cite_key": "writer2024"}, "# Card\n")
    store = Store(project)
    store.conn.execute(
        "INSERT INTO sources(key, title, cite_key) "
        "VALUES('local:x', 'X', 'writer2024')"
    )
    store.conn.commit()
    source = store.source("writer2024")
    cards.build_project_note(project, store, source, say=lambda _: None)
    assert cards.stale_project_notes(project, store) == []

    citations = project.state_dir / "citations.json"
    citations.write_text(json.dumps({"writer2024": {"drafts/a.md": 2}}))
    assert [s["cite_key"] for s in cards.stale_project_notes(project, store)] == [
        "writer2024"
    ]
    STAGES.clear()
    cards.build_project_note(project, store, source, say=lambda _: None)
    assert STAGES == []  # citation-only change reuses the existing relevance
    note = cards.project_note_path(project, "writer2024").read_text()
    assert "drafts/a.md: 2 time(s)" in note
    assert cards.stale_project_notes(project, store) == []

    citations.write_text(json.dumps({"writer2024": {"drafts/a.md": 3}}))
    assert [s["cite_key"] for s in cards.stale_project_notes(project, store)] == [
        "writer2024"
    ]
    store.close()


def test_prompt_version_invalidates_every_generated_note(project, scripted):
    project.project_md.write_text("# T\n\n## Open questions\n- Is it flexible?\n")
    (project.sources_local_dir / "r.md").write_text(SAMPLE)
    (project.sources_local_dir / "r.md.yaml").write_text("cite_key: writer2024\n")
    ingest_local(project, say=lambda _: None)
    draft = project.drafts_dir / "intro.md"
    draft.write_text("Riders depend on one platform [@writer2024].\n")
    # Map the draft first so the project note sees the citation index.
    manuscript.build_manuscript(project, say=lambda _: None)
    store = Store(project)
    source = store.source("writer2024")
    cards.build_card(project, store, source, say=lambda _: None)
    corpus_map.build_map(project, say=lambda _: None)

    assert cards.stale_project_notes(project, store) == []
    assert manuscript.stale_chapters(project, store) == []
    assert not manuscript.manuscript_map_stale(project, store)
    assert not compute(project).corpus_map_stale

    # Notes built by an older prompt set must all be invalidated, not just cards.
    store.conn.execute("UPDATE notes SET prompt_version='old'")
    store.conn.commit()

    assert [s["cite_key"] for s in cards.stale_project_notes(project, store)] == [
        "writer2024"
    ]
    assert manuscript.stale_chapters(project, store) == [draft]
    assert manuscript.manuscript_map_stale(project, store)
    assert compute(project).corpus_map_stale

    # Rebuilding must actually call the model, not reuse the cached relevance.
    STAGES.clear()
    cards.build_project_note(project, store, source, say=lambda _: None)
    assert "summarising" in STAGES
    assert cards.stale_project_notes(project, store) == []
    store.close()


def test_project_md_is_passed_whole_and_invalidates_project_notes(
    project, scripted, monkeypatch
):
    project.project_md.write_text(
        "# T\n\n"
        "## Thesis\nFlexibility is contested.\n\n"
        "## Reader\nGeneral readers.\n\n"
        "## Open questions\n- Is it flexible?\n"
    )
    (project.sources_local_dir / "r.md").write_text(SAMPLE)
    (project.sources_local_dir / "r.md.yaml").write_text("cite_key: writer2024\n")
    ingest_local(project, say=lambda _: None)
    draft = project.drafts_dir / "intro.md"
    draft.write_text("Riders depend on one platform [@writer2024].\n")
    manuscript.build_manuscript(project, say=lambda _: None)
    store = Store(project)
    source = store.source("writer2024")
    cards.build_card(project, store, source, say=lambda _: None)
    corpus_map.build_map(project, say=lambda _: None)

    assert cards.stale_project_notes(project, store) == []
    assert not manuscript.manuscript_map_stale(project, store)
    assert not compute(project).corpus_map_stale

    # A change outside the open questions still invalidates every project-aware
    # note, because the whole file is now part of their input.
    project.project_md.write_text(
        project.project_md.read_text() + "\n## Scope notes\nRide-hailing only.\n"
    )
    assert [s["cite_key"] for s in cards.stale_project_notes(project, store)] == [
        "writer2024"
    ]
    assert manuscript.manuscript_map_stale(project, store)
    assert compute(project).corpus_map_stale

    seen: list[str] = []

    def capture(module):
        original = module.chat_json

        def wrapper(*args, **kwargs):
            seen.append(args[2])
            return original(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(cards, "chat_json", capture(cards))
    monkeypatch.setattr(manuscript, "chat_json", capture(manuscript))
    monkeypatch.setattr(corpus_map, "chat_json", capture(corpus_map))

    cards.build_project_note(project, store, source, say=lambda _: None)
    manuscript.build_project_map(project, store, say=lambda _: None)
    corpus_map.build_map(project, say=lambda _: None)

    assert len(seen) == 3
    assert all("Ride-hailing only." in user for user in seen)
    store.close()


def test_open_questions_heading_is_case_insensitive(project):
    project.project_md.write_text("# T\n\n## Open Questions\n- Why?\n")
    assert cards.open_questions(project) == ["Why?"]


def test_open_questions_keeps_sub_bullets_and_stops_at_prose(project):
    project.project_md.write_text(
        "# T\n\n"
        "## Open questions\n"
        "1. What is the central claim?\n"
        "2. How does it relate to the debate?\n"
        "- For each utopia:\n"
        "  - What political tradition is it from?\n"
        "  - What does it assume about human nature?\n"
        "  - What does it offer as a model?\n"
        "- What changed after 1990?\n\n"
        "A paragraph that introduces a taxonomy, not more questions.\n\n"
        "- The explicit utopias\n"
        "- The utopia of fiction\n\n"
        "## Next section\n"
        "- Not a question\n"
    )
    assert cards.open_questions(project) == [
        "What is the central claim?",
        "How does it relate to the debate?",
        "For each utopia: What political tradition is it from? "
        "What does it assume about human nature? What does it offer as a model?",
        "What changed after 1990?",
    ]


def search_card_hit(store):
    from rai.search import search

    return search(store, "less flexible than claimed", kind="card", limit=1)


@pytest.mark.skipif(not shutil.which("pandoc"), reason="pandoc missing")
def test_manuscript_claims_and_brief(project, scripted):
    (project.drafts_dir / "intro.md").write_text(
        "# Intro\n\nMost riders depend on one platform "
        "[@writer2024, para 2].\n\nMore.\n"
    )
    done = manuscript.build_manuscript(project, say=lambda _: None)
    assert len(done) == 1
    assert set(STAGES) == {"summarising"}
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

    # Deleting the only draft must remove the stale manuscript map.
    (project.drafts_dir / "intro.md").unlink()
    manuscript.build_manuscript(project, say=lambda _: None)
    assert not (project.manuscript_dir / "map.md").exists()


def test_manuscript_map_with_no_chapters_is_stale(project):
    map_path = project.manuscript_dir / "map.md"
    map_path.write_text("# stale map\n")
    store = Store(project)
    store.record_note(
        project.rel(map_path),
        "book_map",
        {},
        project.models.summarising,
        "1",
    )
    assert manuscript.manuscript_map_stale(project, store)
    store.close()
    manuscript.build_manuscript(project, say=lambda _: None)
    assert not map_path.exists()
