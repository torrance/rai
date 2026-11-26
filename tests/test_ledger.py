"""The ledger of the writer's decisions, and the views onto it."""

import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from rai import ledger
from rai.cli import app
from rai.notes import read_notes

runner = CliRunner()


def draft(project, name="ch1.md", text="# One\n\nClaim.\n"):
    (project.drafts_dir / name).write_text(text)
    return f"drafts/{name}"


# Objections -------------------------------------------------------------------
def test_objections_get_stable_ids_and_a_normalised_path(project):
    rel = draft(project)
    raised = ledger.raise_objections(
        project,
        "ch1.md",
        [{"paras": "1", "summary": "Too sweeping"}, {"summary": "No evidence"}],
    )
    assert [o["id"] for o in raised] == ["O1", "O2"]
    assert raised[0]["file"] == rel
    more = ledger.raise_objections(project, rel, [{"paras": "2", "summary": "Third"}])
    assert more[0]["id"] == "O3"
    assert ledger.raise_objections(project, rel, [{"summary": "  "}]) == []


def test_objection_decisions_and_reopening(project):
    draft(project)
    ledger.raise_objections(project, "ch1.md", [{"paras": "1", "summary": "x"}] * 3)
    assert ledger.resolve_objection(project, "o1", "dismissed", "fine")["ok"]
    assert ledger.resolve_objection(project, "O2", "will_address")["ok"]
    by_status = {o["id"]: o["status"] for o in ledger.list_objections(project)}
    assert by_status == {"O1": "dismissed", "O2": "will_address", "O3": "open"}
    unresolved = {o["id"] for o in ledger.list_objections(project, status="unresolved")}
    assert unresolved == {"O2", "O3"}
    assert ledger.resolve_objection(project, "O1", "open")["ok"]
    assert ledger.list_objections(project, status="dismissed") == []
    assert (
        "No objection" in ledger.resolve_objection(project, "O99", "answered")["error"]
    )
    assert "status must be" in ledger.resolve_objection(project, "O1", "maybe")["error"]


def test_an_answered_objection_notices_when_the_draft_changes(project):
    rel = draft(project)
    ledger.raise_objections(project, rel, [{"paras": "1", "summary": "x"}])
    ledger.resolve_objection(project, "O1", "answered", "rewrote para 1")
    assert ledger.list_objections(project)[0]["draft_changed_since"] is False
    (project.root / rel).write_text("# One\n\nA rewritten claim.\n")
    assert ledger.list_objections(project)[0]["draft_changed_since"] is True
    ledger.resolve_objection(project, "O1", "answered", "checked again")
    assert ledger.list_objections(project)[0]["draft_changed_since"] is False


def test_objections_filter_by_file(project):
    ledger.raise_objections(project, draft(project, "a.md"), [{"summary": "a"}])
    ledger.raise_objections(project, draft(project, "b.md"), [{"summary": "b"}])
    assert [o["id"] for o in ledger.list_objections(project, "b.md")] == ["O2"]
    assert len(ledger.list_objections(project)) == 2


# Decisions, reading, candidates ----------------------------------------------
def test_decisions_can_be_recorded_and_revoked(project):
    assert (
        ledger.record_decision(project, "Keep the Weber comparison", "ch3")["id"]
        == "D1"
    )
    assert ledger.record_decision(project, "Drop the survey")["id"] == "D2"
    assert ledger.revoke_decision(project, "d1")["ok"]
    assert [d["id"] for d in ledger.list_decisions(project)] == ["D2"]
    assert "No active decision" in ledger.revoke_decision(project, "D1")["error"]
    assert "needs some text" in ledger.record_decision(project, " ")["error"]
    assert ledger.record_decision(project, "Another")["id"] == "D3"


def test_reading_status_latest_wins_and_is_validated(project):
    ledger.set_reading_status(project, "arendt1958", "skimmed")
    ledger.set_reading_status(project, "arendt1958", "studied", "twice")
    assert ledger.reading_statuses(project)["arendt1958"]["status"] == "studied"
    assert (
        "status must be" in ledger.set_reading_status(project, "x", "mostly")["error"]
    )


def test_reading_status_follows_a_renamed_citation_key(project):
    ledger.set_reading_status(project, "old", "read", "chapter 2")
    ledger.rename_reading_status(project, "old", "new")
    statuses = ledger.reading_statuses(project)
    assert "old" not in statuses
    assert statuses["new"]["status"] == "read"
    assert statuses["new"]["note"] == "chapter 2"


def test_candidates_dedupe_and_remember_dismissals(project):
    entry = {"title": "T", "url_or_doi": "https://doi.org/10.1/ABC", "verdict": "v"}
    assert ledger.propose_candidate(project, entry)["id"] == "C1"
    assert ledger.decide_candidate(project, "c1", "dismissed", "too old")["ok"]
    again = ledger.propose_candidate(project, {**entry, "url_or_doi": "doi:10.1/abc"})
    assert again["duplicate"] and "dismissed earlier" in again["message"]
    found = ledger.find_candidate(project, "10.1/abc")
    assert found["status"] == "dismissed" and found["note"] == "too old"
    assert ledger.decide_candidate(project, "C1", "proposed")["ok"]
    assert ledger.find_candidate(project, "10.1/abc")["status"] == "proposed"
    assert (
        "No candidate" in ledger.decide_candidate(project, "C9", "dismissed")["error"]
    )


def test_a_damaged_ledger_line_does_not_lose_the_rest(project):
    ledger.record_decision(project, "First")
    path = project.ledger_dir / "decisions.jsonl"
    path.write_text(path.read_text() + "{not json\n")
    ledger.record_decision(project, "Second")
    assert [d["text"] for d in ledger.list_decisions(project)] == ["First", "Second"]


# Views ------------------------------------------------------------------------
def test_read_notes_views(project):
    rel = draft(project)
    ledger.raise_objections(project, rel, [{"paras": "1", "summary": "Too sweeping"}])
    ledger.record_decision(project, "Keep the Weber comparison")
    ledger.set_reading_status(project, "arendt1958", "read")
    ledger.propose_candidate(
        project, {"title": "T", "url_or_doi": "10.1/x", "verdict": "v"}
    )
    assert "O1 [open] drafts/ch1.md paras 1" in read_notes(project, "objections")
    assert "D1" in read_notes(project, "decisions")
    assert "arendt1958: read" in read_notes(project, "reading")
    assert "C1 [proposed] T" in read_notes(project, "candidates")
    assert "Not built yet" in read_notes(project, "corpus-map")
    assert "No sources indexed" in read_notes(project, "sources")
    assert "needs file=" in read_notes(project, "chapter_note")
    assert "Unknown notes" in read_notes(project, "journal")
    assert "No claims yet" in read_notes(project, "claims")


def test_rai_show_prints_the_internal_notes(project):
    ledger.record_decision(project, "Keep the Weber comparison")
    result = runner.invoke(app, ["--project", str(project.root), "show", "decisions"])
    assert result.exit_code == 0 and "D1" in result.output
    status = runner.invoke(app, ["--project", str(project.root), "status"]).output
    assert "decisions: 1" in status


# git ----------------------------------------------------------------------------
@pytest.mark.skipif(not shutil.which("git"), reason="git missing")
def test_git_tracks_the_ledger_and_nothing_else_in_rai(tmp_path):
    root = tmp_path / "p"
    assert runner.invoke(app, ["new", "p", "--path", str(root)]).exit_code == 0
    (root / ".rai" / "ledger").mkdir(parents=True, exist_ok=True)
    (root / ".rai" / "ledger" / "decisions.jsonl").write_text("{}\n")
    (root / ".rai" / "library.db").write_text("x")
    (root / ".rai" / "brief.md").write_text("x")

    def ignored(path):
        return (
            subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-q", path]
            ).returncode
            == 0
        )

    assert ignored(".rai/library.db") and ignored(".rai/brief.md")
    assert not ignored(".rai/ledger/decisions.jsonl")


# Session summaries ---------------------------------------------------------------
def summary(project, **overrides):
    values = {
        "discussed": "Went through O12 and O13 on the platform-labour essay.",
        "understandings": ["The 1958 survey can't carry the claim about platforms."],
        "left_off": "Rewriting para 14; next, check the Rosenblat citation.",
        "agent": "sparring",
        "reports": ["reports/feedback/02-argument.md"],
    }
    return ledger.record_session(project, **{**values, **overrides})


def test_session_summaries_are_stored_newest_first(project):
    assert summary(project)["id"] == "S1"
    assert summary(project, left_off="Chasing the second seed.")["id"] == "S2"
    items = ledger.sessions(project)
    assert [i["id"] for i in items] == ["S2", "S1"]
    assert items[1]["understandings"] == [
        "The 1958 survey can't carry the claim about platforms."
    ]
    assert items[1]["agent"] == "sparring"
    assert items[1]["reports"] == ["reports/feedback/02-argument.md"]


def test_a_corrected_summary_replaces_the_earlier_one(project):
    summary(project)
    fixed = summary(project, left_off="Actually stopped at para 22.", replaces="s1")
    assert fixed["ok"]
    items = ledger.sessions(project)
    assert [i["id"] for i in items] == ["S2"]
    assert "para 22" in items[0]["left_off"]
    assert "No session summary" in summary(project, replaces="S9")["error"]


def test_an_empty_summary_is_refused(project):
    result = ledger.record_session(project, " ", ["x"], "  ")
    assert "needs what was discussed or where you left off" in result["error"]
    assert ledger.sessions(project) == []


def test_sessions_view_and_limit(project):
    for n in range(7):
        summary(project, left_off=f"Stopped at point {n}.")
    text = read_notes(project, "sessions")
    assert text.count("Left off:") == 5 and "S7" in text and "S2 " not in text
    assert "(2 earlier;" in text
    assert read_notes(project, "sessions", limit=7).count("Left off:") == 7
    assert "New understandings:" in text and "Reports: reports/feedback" in text
    first = read_notes(project, "sessions").splitlines()[0]
    assert first.startswith("S7 (") and first.endswith(", sparring)")


def test_sessions_view_says_so_when_there_are_none(project):
    assert "No conversations have been summarised" in read_notes(project, "sessions")


def test_the_briefing_says_where_we_left_off(project):
    from rai.brief import build_brief

    assert "Where we left off" not in build_brief(project)
    summary(project)
    summary(
        project,
        left_off="Chasing the second seed.",
        agent="assistant",
        understandings=["Rosenblat is the key seed.", "Two seeds overlap."],
    )
    text = build_brief(project)
    assert "## Where we left off" in text
    assert "with the assistant (S2): Chasing the second seed." in text
    assert "- Rosenblat is the key seed." in text
    assert "1 earlier summaries" in text
    assert text.index("## Where we left off") < text.index("## Where to look")
    assert "Rewriting para 14" not in text  # only the latest session's


def test_the_briefing_includes_all_active_decisions(project):
    from rai.brief import build_brief

    ledger.record_decision(project, "Keep the Weber comparison", "ch. 3")
    ledger.record_decision(project, "Drop the survey")
    temporary = ledger.record_decision(project, "Temporary choice")
    ledger.revoke_decision(project, temporary["id"])
    text = build_brief(project)
    assert "## Decisions" in text
    assert "- D1: Keep the Weber comparison [ch. 3]" in text
    assert "- D2: Drop the survey" in text
    assert "Temporary choice" not in text
    assert text.index("## Decisions") < text.index("## Where to look")


def test_many_long_decisions_stay_within_the_briefing_budget(project):
    from rai.brief import BUDGET_TOKENS, build_brief
    from rai.config import estimate_tokens

    for n in range(30):
        ledger.record_decision(project, f"Decision {n} " + "word " * 300)
    text = build_brief(project)
    assert estimate_tokens(text) < BUDGET_TOKENS
    assert "D1:" in text and "D30:" in text


def test_a_very_long_summary_is_clipped_in_the_briefing(project):
    from rai.brief import BUDGET_TOKENS, build_brief
    from rai.config import estimate_tokens

    summary(project, left_off="word " * 2000, understandings=["long " * 500] * 9)
    text = build_brief(project)
    assert estimate_tokens(text) < BUDGET_TOKENS
    assert "…" in text and text.count("- long") == 5


def test_a_very_long_thesis_is_clipped_in_the_briefing(project):
    from rai.brief import BUDGET_TOKENS, build_brief
    from rai.config import estimate_tokens

    draft(project)
    project.project_md.write_text("# T\n\n## Thesis\n" + "word " * 2000 + "\n")
    text = build_brief(project)
    assert estimate_tokens(text) < BUDGET_TOKENS
    assert "…" in text
    assert "drafts/ch1.md" in text


def test_rai_show_sessions(project):
    summary(project)
    out = runner.invoke(
        app, ["--project", str(project.root), "show", "sessions"]
    ).output
    assert "S1" in out and "Left off: Rewriting para 14" in out
