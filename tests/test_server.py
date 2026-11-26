import anyio

from nfh.server import make_server


def test_server_exposes_the_tools(project):
    server = make_server(project)
    tools = anyio.run(server.list_tools)
    names = {t.name for t in tools}
    assert names == {
        "search_passages",
        "read_location",
        "verify_quote",
        "read_card",
        "read_notes",
        "refresh_card",
        "refresh_chapter",
        "update_claim",
        "raise_objections",
        "resolve_objection",
        "record_decision",
        "revoke_decision",
        "set_reading_status",
        "decide_candidate",
        "record_session",
        "read_candidate",
        "read_candidate_section",
        "save_candidate",
    }


def call(server, name, **arguments):
    return anyio.run(lambda: server.call_tool(name, arguments))


def test_decision_tools_round_trip_through_the_server(project):
    """Calls the tools as an agent would, which checks their signatures too."""
    from nfh import ledger

    server = make_server(project)
    (project.drafts_dir / "a.md").write_text("# A\n\nText.\n")
    call(
        server,
        "raise_objections",
        file="a.md",
        objections=[{"paras": "1", "summary": "Too sweeping"}],
    )
    assert ledger.list_objections(project)[0]["file"] == "drafts/a.md"
    call(
        server, "resolve_objection", objection_id="o1", status="dismissed", note="fine"
    )
    assert ledger.list_objections(project)[0]["status"] == "dismissed"
    call(server, "record_decision", text="Keep the Weber comparison", refs="ch. 3")
    call(server, "revoke_decision", decision_id="D1")
    assert ledger.list_decisions(project) == []
    call(
        server,
        "save_candidate",
        title="A paper",
        url_or_doi="10.1/abc",
        serves="Q1",
        claim="c",
        evidence="e",
        standing="s",
        fit="f",
        read_depth="full",
        access="open",
        verdict="read in full",
    )
    call(server, "decide_candidate", candidate_id="C1", decision="shortlisted")
    text = project.reading_list.read_text()
    assert "## Shortlisted (1)" in text and "### C1: A paper" in text
    notes = call(server, "read_notes", what="decisions")
    assert notes is not None


def test_refresh_chapter_rejects_paths_outside_drafts(project):
    server = make_server(project)
    (project.root.parent / "outside.md").write_text("# Outside\n")
    (project.drafts_dir / ".gitkeep").write_text("")
    for path in ("project.md", "../outside.md", "drafts/.gitkeep"):
        result = call(server, "refresh_chapter", path=path)
        assert "not a draft file" in str(result)


def test_set_reading_status_checks_the_source_exists(project):
    server = make_server(project)
    call(server, "set_reading_status", cite_key="nobody", status="read")
    from nfh import ledger

    assert ledger.reading_statuses(project) == {}


def test_save_candidate_builds_the_reading_list(project):
    from nfh.candidates import save_candidate

    entry = {"title": "A paper", "url_or_doi": "10.1/abc", "verdict": "skip: old"}
    first = save_candidate(project, entry)
    assert first["ok"] and first["id"] == "C1"
    again = save_candidate(project, {**entry, "url_or_doi": "https://doi.org/10.1/ABC"})
    assert again["duplicate"] and again["id"] == "C1"
    text = project.reading_list.read_text()
    assert text.count("A paper") == 1 and "Verdict: skip: old" in text
    assert "Don't edit this file" in text


def test_record_session_round_trips_and_refreshes_the_briefing(project):
    from nfh import ledger

    server = make_server(project)
    call(
        server,
        "record_session",
        discussed="Looked at the Rosenblat seeds.",
        understandings=["The seeds overlap heavily."],
        left_off="About to search around the second seed.",
        agent="assistant",
        reports=["reports/research/seeds.md"],
    )
    item = ledger.sessions(project)[0]
    assert item["agent"] == "assistant" and item["reports"] == [
        "reports/research/seeds.md"
    ]
    brief = project.brief_path.read_text()  # rebuilt by the tool, ready for next time
    assert "About to search around the second seed." in brief
