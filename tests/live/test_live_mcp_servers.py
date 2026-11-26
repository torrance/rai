"""Each MCP server starts the way opencode.jsonc starts it, and lists its tools."""

from __future__ import annotations

import fnmatch
import os
import re

import anyio
import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from rai import jsonc

pytestmark = pytest.mark.live


def code_defined_tools(project) -> set[str]:
    """The tools rai's server defines, read from the code rather than a second list."""
    from rai.server import make_server

    return {t.name for t in anyio.run(make_server(project).list_tools)}


def _tools(project, name: str, call: tuple[str, dict] | None = None):
    server = jsonc.load(project.root / "opencode.jsonc")["mcp"]["servers"][name]
    command, *args = server["command"]
    params = StdioServerParameters(
        command=command,
        args=args,
        env={**os.environ, **server.get("environment", {})},
        cwd=str(project.root),
    )

    async def run():
        with anyio.fail_after(600):  # first uvx run downloads the server
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    reply = None
                    if call:
                        result = await session.call_tool(*call)
                        reply = result.content[0].text if result.content else ""
                    return {t.name for t in listed.tools}, reply

    return anyio.run(run)


def test_rai_server(scratch_project):
    names, reply = _tools(scratch_project, "rai", ("read_card", {"cite_key": "x"}))
    assert names == code_defined_tools(scratch_project), (
        "the server started by opencode.jsonc exposes different tools than the code "
        "defines; is the project pointing at another copy of rai?"
    )
    assert "No card yet" in reply


def test_paper_search_server_has_the_allowed_tool(scratch_project):
    names, _ = _tools(scratch_project, "papers")
    assert "search_papers" in names, (
        "the agents' allow-lists name papers_search_papers; this version calls it "
        f"something else. Tools: {sorted(names)}"
    )


WRITE_WORDS = (
    "add",
    "create",
    "update",
    "delete",
    "attach_file",
    "set_",
    "manage",
    "write",
    "authorize",
    "switch",
    "batch",
)


def _allowed(project, server: str, names: set[str]) -> dict[str, set[str]]:
    """Per agent: which of a server's tools its permission rules allow."""
    out = {}
    for path in (project.root / ".opencode" / "agents").glob("*.md"):
        head = re.match(r"^---\n(.*?)\n---\n", path.read_text(), re.DOTALL)
        rules = yaml.safe_load(head.group(1))["permissions"]
        allowed = set()
        for tool in names:
            full = f"{server}_{tool}"
            effect = "deny"  # opencode.jsonc denies every MCP tool globally
            for rule in rules:  # last matching rule wins
                if fnmatch.fnmatchcase(full, rule["action"]):
                    effect = rule["effect"]
            if effect == "allow":
                allowed.add(tool)
        out[path.stem] = allowed
    return out


def test_zotero_server_and_agents_get_read_tools_only(scratch_project):
    names, _ = _tools(scratch_project, "zotero")
    assert names, "zotero-mcp started but exposes no tools"
    assert "zotero_semantic_search" not in names, (
        "the template installs zotero-mcp without its [semantic] extra; rai's "
        "search_passages is the project's passage search. If you enable Zotero "
        "semantic search on purpose, grant it and update the agents' guidance."
    )
    allowed = _allowed(scratch_project, "zotero", names)
    for agent, tools in allowed.items():
        writes = sorted(t for t in tools if any(w in t for w in WRITE_WORDS))
        assert not writes, f"{agent} may call Zotero write tools: {writes}"
    assert allowed["assistant"], "the assistant's Zotero rules match no tools"


def test_only_the_unified_paper_search_is_granted(scratch_project):
    names, _ = _tools(scratch_project, "papers")
    allowed = _allowed(scratch_project, "papers", names)
    assert allowed["assistant"] == {"search_papers"}
    assert allowed["sparring"] == {"search_papers"}
    assert not allowed["reader"]


def test_every_rai_allow_rule_matches_a_real_tool(scratch_project):
    tools = code_defined_tools(scratch_project)
    allowed = _allowed(scratch_project, "rai", tools)
    assert allowed["assistant"] >= {"search_passages", "read_location", "verify_quote"}
    assert allowed["assistant"] >= {
        "read_candidate",
        "save_candidate",
        "save_candidates",
        "decide_candidate",
    }
    assert allowed["assistant"] >= {"record_session"}
    assert allowed["sparring"] >= {"record_session"}
    assert "record_session" not in allowed["reader"]
    assert allowed["sparring"] >= {"raise_objections", "resolve_objection"}
    assert allowed["sparring"] >= {
        "read_candidate",
        "save_candidate",
        "save_candidates",
        "decide_candidate",
        "verify_quote",
    }
    assert "update_claim" not in allowed["sparring"]
    assert "raise_objections" not in allowed["assistant"]
    assert allowed["reader"] >= {
        "verify_quote",
        "read_candidate",
        "read_candidate_section",
    }
    assert not allowed["reader"] & {"record_decision", "resolve_objection"}
    assert set().union(*allowed.values()) == tools, "a tool no agent can use"


def _search(project, **arguments):
    import json

    _, reply = _tools(project, "papers", ("search_papers", arguments))
    return json.loads(reply)


def test_paper_sources_outside_the_configured_list_are_unreachable(project):
    """Enforced in the server, so it holds whatever an agent asks for."""
    allowed = set(project.scout.paper_sources)
    excluded = "arxiv,biorxiv,medrxiv,pubmed,pmc,europepmc,dblp,iacr,citeseerx"
    refused = _search(project, query="algorithmic management", sources=excluded)
    assert refused["papers"] == [] and "No valid sources" in str(refused["errors"])
    mixed = _search(
        project, query="algorithmic management", sources=f"arxiv,{sorted(allowed)[0]}"
    )
    tried = set(mixed.get("sources_used", [])) | set(mixed.get("source_results", {}))
    assert tried <= allowed, f"searched outside the configured list: {tried - allowed}"
    everything = _search(project, query="algorithmic management", sources="all")
    tried = set(everything.get("sources_used", [])) | set(everything["source_results"])
    assert tried <= allowed, f"'all' reached: {tried - allowed}"


def test_paper_sources_each_answer(project):
    """Not every database is reliable (keys, rate limits, scraping). This tells you
    which of yours work, so you can edit [scout] paper_sources."""
    from .conftest import advise

    working, silent = [], []
    for source in project.scout.paper_sources:
        data = _search(
            project,
            query="platform labour algorithmic management",
            sources=source,
            max_results_per_source=3,
        )
        (working if data["papers"] else silent).append(source)
        print(source, len(data["papers"]), "papers", data.get("errors") or "")
    if silent:
        advise(
            f"no results from {silent}; they may need a key or be rate-limited. "
            f"Working: {working}. Edit [scout] paper_sources in config.local.toml."
        )
    assert working, "none of the configured paper sources returned anything"
