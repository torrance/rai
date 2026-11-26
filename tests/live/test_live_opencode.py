"""OpenCode loads the project, connects its MCP servers, and enforces the rules.

The agent runs (--agents) are the real test of the drafts guard: an agent told
to edit a draft must fail, while the same agent can write where it's allowed.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import time

import pytest

from rai.brief import build_brief

pytestmark = pytest.mark.live

TIMEOUT = 900


def _opencode() -> str:
    binary = shutil.which("opencode")
    if binary is None:
        pytest.fail("opencode isn't on PATH")
    return binary


def _run(project, *args: str, timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_opencode(), *args],
        cwd=project.root,
        # `opencode run` reads $PWD, not the real working directory; subprocess's
        # cwd= doesn't update it, so set it or OpenCode runs in the wrong folder.
        env={**os.environ, "PWD": str(project.root)},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def session_project(scratch_project):
    build_brief(scratch_project)  # opencode.jsonc lists .rai/brief.md
    return scratch_project


NEED_V2 = (
    "OpenCode 2 is needed: the project files use its config format. Install it "
    "with `curl -fsSL https://opencode.ai/v2/install | bash` or `npm install -g "
    "@opencode/cli`. The opencode.ai/install script and the opencode-ai package "
    "install OpenCode 1, which rejects these projects; remove it first."
)


def test_opencode_is_version_2(session_project):
    result = _run(session_project, "--version", timeout=60)
    text = (result.stdout + result.stderr).strip()
    assert result.returncode == 0, text
    match = re.search(r"(\d+)\.\d+\.\d+", text)
    assert match, f"couldn't read a version from {text!r}"
    assert match.group(1) == "2", f"`opencode --version` printed {text!r}. {NEED_V2}"
    print("OpenCode", match.group(0))


def wait_for_servers(project, seconds=180):
    """OpenCode 2 starts a background service on first use, so the servers take a
    moment to connect (and before then `mcp list` may report none at all)."""
    names = ("rai", "zotero", "papers")
    deadline = time.monotonic() + seconds
    while True:
        result = _run(project, "mcp", "list", timeout=600)
        output = (result.stdout + result.stderr).strip()
        if "configuration is invalid" in output.lower():
            pytest.fail(
                f"OpenCode rejected the project's config:\n{output}\n\n{NEED_V2}"
            )
        lines = output.lower().splitlines()
        connected = [
            n
            for n in names
            if any(
                n in ln and "connected" in ln and "disconnected" not in ln
                for ln in lines
            )
        ]
        if len(connected) == len(names):
            return
        if time.monotonic() > deadline:
            pytest.fail(
                f"Only {connected or 'none'} of {names} connected within "
                f"{seconds} s. `opencode mcp list` printed:\n{output or '(nothing)'}"
            )
        time.sleep(5)


def test_all_three_mcp_servers_connect(session_project):
    wait_for_servers(session_project)


def _effect(agent: dict, action: str, resource: str) -> str:
    """What OpenCode's resolved rules decide: the last matching rule wins."""
    effect = "unmatched"
    for rule in agent["permissions"]:
        if fnmatch.fnmatchcase(action, rule["action"]) and fnmatch.fnmatchcase(
            resource, rule["resource"]
        ):
            effect = rule["effect"]
    return effect


def test_agents_load_and_the_drafts_guard_holds_in_opencodes_own_rules(
    session_project, project
):
    """No model needed: `opencode debug agents` prints the permissions OpenCode
    actually resolved, so this tests the guard, not just the files it came from."""
    result = _run(session_project, "debug", "agents", timeout=120)
    assert result.returncode == 0, result.stderr or result.stdout
    try:
        agents = {a["id"]: a for a in json.loads(result.stdout)}
    except json.JSONDecodeError:
        pytest.fail(f"`opencode debug agents` didn't print JSON: {result.stdout[:600]}")
    ours = ("assistant", "sparring", "reader")
    for name in ours:
        assert name in agents, f"{name} didn't load: {sorted(agents)}"
    assert "build" not in agents and "plan" not in agents, "built-in agents are on"

    from rai.config import parse_ref

    expected_models = {
        "assistant": project.models.research,
        "sparring": project.models.sparring,
        "reader": project.models.reading,
    }
    for name in ours:
        model = agents[name].get("model") or {}
        expected = parse_ref(expected_models[name])
        assert (model.get("providerID"), model.get("id")) == (
            expected.provider,
            expected.model,
        ), f"{name} uses {model}; run `rai sync`"

    for name in ours:
        for path in ("drafts/ch1.md", "drafts/sub/ch2.typ"):
            assert _effect(agents[name], "edit", path) == "deny", f"{name} {path}"
        for action, resource in (
            ("shell", "rm -rf x"),
            ("webfetch", "http://x"),
            ("execute", "*"),
        ):
            assert _effect(agents[name], action, resource) == "deny", f"{name} {action}"

    for name in ours:  # nobody edits the internal notes or the ledger directly
        for path in (".rai/library.db", ".rai/ledger/decisions.jsonl"):
            assert _effect(agents[name], "edit", path) == "deny", f"{name} {path}"

    expected = {
        ("assistant", "edit", "reports/plans/x.md"): "allow",
        ("assistant", "edit", "reports/research/x.md"): "allow",
        ("assistant", "edit", "reports/feedback/x.md"): "deny",
        ("assistant", "edit", "project.md"): "ask",
        ("assistant", "subagent", "reader"): "allow",
        ("assistant", "rai_verify_quote", "*"): "allow",
        ("assistant", "rai_update_claim", "*"): "allow",
        ("assistant", "rai_save_candidate", "*"): "allow",
        ("assistant", "rai_save_candidates", "*"): "allow",
        ("assistant", "rai_decide_candidate", "*"): "allow",
        ("assistant", "rai_record_session", "*"): "allow",
        ("assistant", "rai_raise_objections", "*"): "deny",
        ("assistant", "websearch", "anything"): "allow",
        ("assistant", "papers_search_papers", "*"): "allow",
        ("assistant", "zotero_zotero_get_item_metadata", "*"): "allow",
        ("assistant", "zotero_zotero_delete_item", "*"): "deny",
        ("sparring", "edit", "reports/feedback/x.md"): "allow",
        ("sparring", "edit", "reports/research/x.md"): "deny",
        ("sparring", "edit", "project.md"): "deny",
        ("sparring", "rai_update_claim", "*"): "deny",
        ("sparring", "rai_decide_candidate", "*"): "allow",
        ("sparring", "rai_verify_quote", "*"): "allow",
        ("sparring", "rai_raise_objections", "*"): "allow",
        ("sparring", "rai_resolve_objection", "*"): "allow",
        ("sparring", "rai_record_session", "*"): "allow",
        ("sparring", "rai_read_candidate", "*"): "allow",
        ("sparring", "rai_save_candidate", "*"): "allow",
        ("sparring", "rai_save_candidates", "*"): "allow",
        ("sparring", "websearch", "anything"): "allow",
        ("sparring", "papers_search_papers", "*"): "allow",
        ("sparring", "zotero_zotero_get_item_metadata", "*"): "deny",
        ("reader", "edit", "reports/research/x.md"): "deny",
        ("reader", "rai_record_decision", "*"): "deny",
        ("reader", "rai_record_session", "*"): "deny",
        ("reader", "rai_read_notes", "*"): "allow",
        ("reader", "rai_verify_quote", "*"): "allow",
        ("reader", "rai_read_candidate", "*"): "allow",
        ("reader", "rai_read_candidate_section", "*"): "allow",
        ("reader", "websearch", "anything"): "deny",
    }
    wrong = {
        key: _effect(agents[key[0]], key[1], key[2])
        for key, want in expected.items()
        if _effect(agents[key[0]], key[1], key[2]) != want
    }
    assert not wrong, f"OpenCode resolved these differently than intended: {wrong}"


@pytest.fixture(scope="module")
def agent_run(session_project):
    help_text = _run(session_project, "run", "--help", timeout=60)
    if (
        help_text.returncode != 0
        or "--agent" not in help_text.stdout + help_text.stderr
    ):
        pytest.skip("this OpenCode version has no `opencode run --agent`")

    wait_for_servers(session_project)  # else the first prompt has no rai tools

    def run(agent: str, prompt: str) -> str:
        result = _run(session_project, "run", "--agent", agent, prompt)
        return result.stdout + result.stderr

    return run


@pytest.mark.agents
def test_model_answers_through_opencode(agent_run):
    out = agent_run("assistant", "Reply with the single word READY and nothing else.")
    assert "READY" in out, out[-2000:]


@pytest.mark.agents
def test_agent_can_write_where_allowed(session_project, agent_run):
    target = session_project.reports_dir / "research" / "guard-check.md"
    target.unlink(missing_ok=True)
    agent_run(
        "assistant",
        "Create the file reports/research/guard-check.md containing exactly the "
        "text: allowed. Do nothing else.",
    )
    assert target.exists(), "the positive control failed: the agent can't write at all"


@pytest.mark.agents
@pytest.mark.parametrize("agent", ["sparring", "assistant"])
def test_agents_cannot_edit_drafts(session_project, agent_run, agent):
    draft = session_project.drafts_dir / "guard.md"
    original = "This sentence has a deliberate typo: teh.\n"
    draft.write_text(original)
    agent_run(
        agent,
        "Open drafts/guard.md and fix the typo 'teh' to 'the' by editing the "
        "file directly. This is an explicit instruction from the writer.",
    )
    assert draft.read_text() == original, f"{agent} edited a draft"
    assert not (session_project.drafts_dir / "guard.md.orig").exists()


@pytest.mark.agents
def test_rai_tool_permission_names_work(agent_run):
    """The allow rules name tools as rai_<tool>; this proves OpenCode agrees."""
    out = agent_run(
        "assistant",
        "Call the read_card tool with cite_key 'no-such-source' and repeat its "
        "reply word for word.",
    )
    assert "No card yet" in out, out[-2000:]


def test_project_files_match_rai_settings(live_project):
    """opencode.jsonc, the agents' models and the git hook follow rai's settings."""
    from rai.render import sync_project

    if live_project is None:
        pytest.skip("pass --live-project to check a real project")
    drift = sync_project(live_project, check=True).drift
    assert not drift, f"out of date: {', '.join(drift)}; run `rai sync`"


@pytest.mark.agents
def test_the_briefing_is_loaded_from_rai_into_the_session(session_project, agent_run):
    """opencode.jsonc loads .rai/brief.md as an instruction; .rai is not tracked by
    git, so this confirms OpenCode reads it anyway."""
    marker = "PINEAPPLE-7"
    session_project.brief_path.write_text(
        session_project.brief_path.read_text() + f"\nMarker for tests: {marker}\n"
    )
    out = agent_run(
        "assistant",
        "Without calling any tool: what is the 'Marker for tests' in your briefing? "
        "Reply with the marker only.",
    )
    assert marker in out, out[-1500:]


@pytest.mark.agents
def test_sparring_records_an_objection_and_its_dismissal(session_project, agent_run):
    from rai import ledger

    (session_project.drafts_dir / "ledger-check.md").write_text("# T\n\nClaims all.\n")
    agent_run(
        "sparring",
        "Call raise_objections for drafts/ledger-check.md with exactly one objection "
        "(paras '1', summary 'ledger test objection'). Then call resolve_objection "
        "on the id it returns with status dismissed and note 'fine'. Reply DONE.",
    )
    found = [
        o
        for o in ledger.list_objections(session_project, "ledger-check.md")
        if "ledger test" in o["summary"]
    ]
    assert found, "the objection was never registered; did the model call the tool?"
    assert found[0]["status"] == "dismissed", found[0]


@pytest.mark.agents
def test_the_assistant_records_a_session_summary_for_the_next_conversation(
    session_project, agent_run
):
    from rai import ledger

    agent_run(
        "assistant",
        "Call record_session with agent 'assistant', discussed 'wrap test', "
        "understandings ['wrap understanding'] and left_off 'wrap left off here'. "
        "Reply DONE.",
    )
    found = [
        s for s in ledger.sessions(session_project) if "wrap test" in s["discussed"]
    ]
    assert found, "the summary was never recorded; did the model call the tool?"
    assert found[0]["understandings"] == ["wrap understanding"]
    assert "wrap left off here" in session_project.brief_path.read_text()
