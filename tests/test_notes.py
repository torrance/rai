"""Working notes: the ledger, its views, search, health and tools."""

from __future__ import annotations

import anyio
from typer.testing import CliRunner

from rai import ledger, note_search
from rai.brief import build_brief
from rai.check import check_note_quotes
from rai.cli import app
from rai.ingest import ingest_local
from rai.notes import read_note, read_notes
from rai.server import make_server
from rai.status import compute
from rai.store import Store

from .conftest import SAMPLE

runner = CliRunner()


def _ingest(project):
    (project.sources_local_dir / "riders.md").write_text(SAMPLE)
    (project.sources_local_dir / "riders.md.yaml").write_text(
        "title: Riders\nauthors: [A. Writer]\nyear: 2024\ncite_key: writer2024\n"
    )
    return ingest_local(project, say=lambda _: None)


def note(project, **overrides):
    values = {
        "title": "Abolish markets or constrain them?",
        "abstract": "Hahnel treats markets as corrosive; Wright treats them as "
        "constrainable.",
        "body": "The crux is whether market power can be separated from exchange.",
        "sources": ["writer2024"],
    }
    return ledger.create_note(project, **{**values, **overrides})


# The ledger -------------------------------------------------------------------
def test_notes_get_stable_ids_and_required_fields(project):
    assert note(project)["id"] == "N1"
    assert note(project)["id"] == "N2"
    assert "title" in ledger.create_note(project, "", "abstract")["error"]
    assert "abstract" in ledger.create_note(project, "title", " ")["error"]
    assert "N1" in read_notes(project, "notes")


def test_a_long_abstract_is_a_warning_not_an_error(project):
    result = note(project, abstract="word " * 60)
    assert result["ok"] and any("about 50" in w for w in result["warnings"])


def test_revise_keeps_history_and_a_full_record(project):
    note(project)
    result = ledger.revise_note(
        project, "n1", abstract="A revised gist.", body="A longer body."
    )
    assert result["ok"]
    current = ledger.note(project, "N1")
    assert current["abstract"] == "A revised gist."
    assert current["body"] == "A longer body."
    assert current["title"] == "Abolish markets or constrain them?"
    history = ledger.note_history(project, "N1")
    assert [h["event"] for h in history] == ["create", "revise"]
    assert "abstract" in history[1]["changed"] and "body" in history[1]["changed"]


def test_deleted_notes_are_archived_not_reactivated(project):
    note(project)
    assert ledger.delete_note(project, "N1", "no longer needed")["ok"]
    assert ledger.delete_note(project, "N1")["already"] is True
    assert ledger.note(project, "N1")["status"] == "deleted"
    assert ledger.list_notes(project) == []
    assert [n["id"] for n in ledger.list_notes(project, status="deleted")] == ["N1"]
    assert "cannot be revised" in ledger.revise_note(project, "N1", body="x")["error"]
    assert "No note" in ledger.delete_note(project, "N9")["error"]


def test_links_are_checked_against_existing_notes(project):
    note(project)
    linked = note(project, title="Second", links={"notes": ["N1"], "decisions": ["D4"]})
    assert linked["ok"]
    assert ledger.note(project, "N2")["links"] == {"notes": ["N1"], "decisions": ["D4"]}
    bad = note(project, title="Third", links={"notes": ["N99"]})
    assert "No note" in bad["error"]
    assert (
        "No note" in ledger.revise_note(project, "N1", links={"notes": ["N1"]})["error"]
    )


def test_a_session_summary_can_point_at_notes(project):
    note(project)
    result = ledger.record_session(project, "discussed", [], "", notes=["n1"])
    assert result["ok"]
    assert ledger.sessions(project)[0]["notes"] == ["N1"]


def test_a_citation_key_rename_follows_into_notes(project):
    note(project, sources=["old"], evidence=[{"cite_key": "old", "location": "p. 1"}])
    assert ledger.rename_note_sources(project, "old", "new")["moved"] is True
    current = ledger.note(project, "N1")
    assert current["sources"] == ["new"]
    assert current["evidence"][0]["cite_key"] == "new"
    assert any(h["event"] == "rename" for h in ledger.note_history(project, "N1"))


# Evidence ---------------------------------------------------------------------
def test_evidence_quotes_are_verified_when_a_note_is_saved(project, fake_ollama):
    _ingest(project)
    result = note(
        project,
        evidence=[
            {
                "cite_key": "writer2024",
                "location": "para 3",
                "quote": "flexibility is largely illusory",
            },
            {
                "cite_key": "writer2024",
                "location": "para 12",
                "quote": "flexibility is largely illusory",
            },
            {
                "cite_key": "writer2024",
                "location": "para 1",
                "quote": "nothing like this appears anywhere",
            },
            {
                "cite_key": "C1",
                "location": "abstract",
                "quote": "candidate words",
            },
        ],
    )
    assert result["ok"]
    evidence = ledger.note(project, "N1")["evidence"]
    assert evidence[0]["verified"] == "exact"
    assert evidence[1]["verified"] == "elsewhere"
    assert evidence[1]["location"] == "para 3"
    assert evidence[2]["verified"] == "unverified" and evidence[2]["quote"] == ""
    assert evidence[3]["verified"] == "unverified"
    assert any("dropped the quotation" in w for w in result["warnings"])
    assert any("not in the library" in w for w in result["warnings"])


def test_a_fuzzy_quote_found_elsewhere_keeps_the_source_wording(project, fake_ollama):
    _ingest(project)
    note(
        project,
        evidence=[
            {
                "cite_key": "writer2024",
                "location": "p. 99",
                "quote": (
                    "The authors argue that flexibility is largely ilusory "
                    "for full-time riders."
                ),
            }
        ],
    )
    entry = ledger.note(project, "N1")["evidence"][0]
    assert entry["verified"] == "elsewhere"
    assert "largely illusory" in entry["quote"]
    assert entry["location"] != "p. 99"


def test_check_and_status_see_note_evidence(project, fake_ollama):
    _ingest(project)
    note(
        project,
        evidence=[
            {
                "cite_key": "writer2024",
                "location": "para 3",
                "quote": "flexibility is largely illusory",
            }
        ],
    )
    note(
        project,
        title="Missing source",
        evidence=[
            {
                "cite_key": "gone2020",
                "location": "p. 2",
                "quote": "a quotation with no home",
            }
        ],
    )
    checks = check_note_quotes(project, Store(project))
    assert sorted(c.status for c in checks) == ["exact", "unverified"]
    status = compute(project)
    assert status.active_notes == 2 and status.deleted_notes == 0
    assert status.notes_citing_missing_sources == ["N2"]


# Views and the briefing -------------------------------------------------------
def test_the_survey_and_full_note_views(project):
    note(project)
    survey = read_notes(project, "notes")
    assert "N1  Abolish markets or constrain them?" in survey
    assert "Hahnel treats markets as corrosive" in survey
    assert "Sources: writer2024" in survey
    full = read_note(project, "N1")
    assert "Abstract:" in full and "Body:" in full and "Created:" in full
    assert "No note" in read_note(project, "N9")
    assert "needs note_id" in read_notes(project, "note")


def test_the_briefing_carries_only_a_count_and_pointer(project):
    assert "## Working notes" in build_brief(project)
    assert "No working notes yet" in build_brief(project)
    note(project)
    text = build_brief(project)
    assert "- 1 active" in text
    assert 'read_notes("notes")' in text
    assert "Abolish markets" not in text
    ledger.delete_note(project, "N1")
    assert "- 0 active, 1 deleted" in build_brief(project)


def test_rai_show_notes_and_history(project):
    note(project)
    ledger.revise_note(project, "N1", abstract="A revised gist.")
    out = runner.invoke(app, ["--project", str(project.root), "show", "notes"]).output
    assert "N1" in out and "A revised gist." in out
    full = runner.invoke(
        app, ["--project", str(project.root), "show", "note", "N1"]
    ).output
    assert "Title:" in full and "Revised:" in full
    history = runner.invoke(
        app,
        ["--project", str(project.root), "show", "note", "N1", "--history"],
    ).output
    assert "create" in history and "revise" in history
    ledger.delete_note(project, "N1")
    hidden = runner.invoke(
        app, ["--project", str(project.root), "show", "note", "N1"]
    ).output
    assert "is deleted" in hidden
    shown = runner.invoke(
        app,
        [
            "--project",
            str(project.root),
            "show",
            "note",
            "N1",
            "--include-deleted",
        ],
    ).output
    assert "Title:" in shown
    deleted = runner.invoke(
        app,
        ["--project", str(project.root), "show", "notes", "--status", "deleted"],
    ).output
    assert "N1 [deleted]" in deleted


# Search -----------------------------------------------------------------------
def test_hybrid_note_search_prefers_active_notes(project, fake_ollama):
    note(project, title="Markets", abstract="Markets and exchange.")
    note(
        project,
        title="Planning",
        abstract="Participatory planning.",
        body="Deliberation and consent.",
    )
    hits = note_search.search_notes(project, "markets", limit=5)
    assert hits and hits[0]["id"] == "N1"
    assert hits[0]["title"] == "Markets"
    ledger.delete_note(project, "N1")
    assert [h["id"] for h in note_search.search_notes(project, "markets")] == []
    archived = note_search.search_notes(project, "markets", include_deleted=True)
    assert [h["id"] for h in archived] == ["N1"]


def test_note_search_indexes_the_body_by_keyword(project, fake_ollama):
    note(project, title="Alpha", abstract="A short gist.", body="quixotic detail")
    hits = note_search.search_notes(project, "quixotic")
    assert hits and hits[0]["id"] == "N1"


def test_the_note_index_rebuilds_from_the_ledger(project, fake_ollama):
    note(project, title="Markets", abstract="Markets and exchange.")
    note_search.sync_index(project)
    store = Store(project)
    store.conn.execute("DELETE FROM working_notes")
    store.conn.execute("DELETE FROM working_notes_fts")
    store.conn.commit()
    store.close()
    assert note_search.search_notes(project, "markets")
    store = Store(project)
    assert note_search.index_problem(project, store) == ""
    store.close()


def test_status_reports_a_missing_note_index(project):
    note(project)
    status = compute(project)
    assert "0 of 1 notes" in status.note_index_problem


def test_a_changed_embedding_setting_rebuilds_the_note_index(project, fake_ollama):
    note(project, title="Markets", abstract="Markets and exchange.")
    note_search.sync_index(project)
    store = Store(project)
    store.set_meta("wn_embed_ref", "ollama/some-old-model")
    store.conn.commit()
    store.close()
    assert "was built with" in compute(project).note_index_problem
    note_search.sync_index(project)
    assert compute(project).note_index_problem == ""


def test_deleted_notes_do_not_crowd_active_notes_out_of_the_pools(project, fake_ollama):
    # More matching deleted notes than the candidate pool, plus one active note
    # that must still be found.
    for _ in range(55):
        ledger.create_note(project, "Old markets note", "Markets and exchange.")
    active = ledger.create_note(project, "Live markets note", "Markets and exchange.")
    for number in range(1, 56):
        ledger.delete_note(project, f"N{number}")
    note_search.sync_index(project, rebuild=True)
    hits = note_search.search_notes(project, "markets", limit=5)
    assert [h["id"] for h in hits] == [active["id"]]


def test_search_survives_an_embedding_outage(project, monkeypatch):
    saved = ledger.create_note(
        project,
        "Offline",
        "A gist recorded while embeddings are down.",
        body="quixotic detail",
    )

    def down(*args, **kwargs):
        raise RuntimeError("embedding service offline")

    monkeypatch.setattr("rai.note_search.embed_texts", down)
    monkeypatch.setattr("rai.note_search.embed_query", down)
    result = note_search.sync_index(project)
    assert result["warning"]
    assert "missing vectors" in compute(project).note_index_problem
    hits = note_search.search_notes(project, "quixotic")
    assert [h["id"] for h in hits] == [saved["id"]]


def test_search_uses_the_numpy_fallback_without_sqlite_vec(
    project, fake_ollama, monkeypatch
):
    note(project, title="Markets", abstract="Markets and exchange.")
    monkeypatch.setattr("rai.store._load_vec", lambda conn: False)
    hits = note_search.search_notes(project, "markets")
    assert hits and hits[0]["id"] == "N1"


# The MCP tools ----------------------------------------------------------------
def call(server, name, **arguments):
    return anyio.run(lambda: server.call_tool(name, arguments))


def test_note_tools_round_trip_through_the_server(project, fake_ollama):
    server = make_server(project)
    saved = call(
        server,
        "save_note",
        title="Markets",
        abstract="Markets and exchange.",
        body="A longer body.",
        sources=["writer2024"],
    )
    assert "N1" in str(saved)
    assert "N1" in str(call(server, "read_notes", what="notes"))
    assert "N1" in str(call(server, "search_notes", query="markets"))
    revised = call(server, "revise_note", note_id="N1", abstract="A revised gist.")
    assert "N1" in str(revised)
    assert "A revised gist." in str(call(server, "read_note", note_id="N1"))
    assert "deleted" in str(
        call(server, "set_note_status", note_id="N1", status="deleted")
    )
    assert "is deleted" in str(call(server, "read_note", note_id="N1"))
    assert "Title:" in str(
        call(server, "read_note", note_id="N1", include_deleted=True)
    )
    reactivate = call(server, "set_note_status", note_id="N1", status="active")
    assert "cannot be reactivated" in str(reactivate)


def test_save_notes_batch_reports_each_entry(project, fake_ollama):
    server = make_server(project)
    result = call(
        server,
        "save_notes",
        notes=[
            {"title": "One", "abstract": "First gist."},
            {"title": "Two", "abstract": "Second gist."},
            {"title": "Missing abstract"},
        ],
    )
    assert "saved" in str(result) and "errors" in str(result)
    assert [n["id"] for n in ledger.list_notes(project)] == ["N2", "N1"]


def test_saving_a_note_refreshes_the_brief_count(project, fake_ollama):
    server = make_server(project)
    call(server, "save_note", title="One", abstract="First gist.")
    assert "- 1 active" in project.brief_path.read_text()
