"""The project template's agents, commands and config agree with the code."""

import re

import anyio
import yaml

from nfh import jsonc
from nfh.config import TEMPLATE_DIR
from nfh.server import make_server

AGENTS = TEMPLATE_DIR / ".opencode" / "agents"
COMMANDS = TEMPLATE_DIR / ".opencode" / "commands"


def frontmatter(path):
    match = re.match(r"^---\n(.*?)\n---\n", path.read_text(), re.DOTALL)
    assert match, f"{path.name} has no frontmatter"
    return yaml.safe_load(match.group(1))


def test_jsonc_handles_urls_comments_and_trailing_commas():
    data = jsonc.loads('{"a": "https://x.org//y", // note\n "b": [1, 2,],}')
    assert data == {"a": "https://x.org//y", "b": [1, 2]}


def test_rendered_config_parses_and_names_servers(project):
    config = jsonc.load(project.root / "opencode.jsonc")
    assert set(config["mcp"]["servers"]) == {"nfh", "zotero", "papers"}
    assert config["instructions"] == [".nfh/brief.md"]


def test_every_nfh_tool_named_in_permissions_exists(project):
    tools = {t.name for t in anyio.run(make_server(project).list_tools)}
    for path in AGENTS.glob("*.md"):
        for rule in frontmatter(path)["permissions"]:
            action = rule["action"]
            if action.startswith("nfh_"):
                assert action[4:] in tools, f"{path.name}: no tool {action}"


def test_every_agent_denies_drafts_last_and_never_allows_them():
    for path in AGENTS.glob("*.md"):
        rules = frontmatter(path)["permissions"]
        edits = [r for r in rules if r["action"] == "edit"]
        assert edits[-1] == {
            "action": "edit",
            "resource": "drafts/**",
            "effect": "deny",
        }
        for rule in edits:
            if rule["effect"] in {"allow", "ask"}:
                assert not rule["resource"].startswith(("drafts", "*", "**"))


def test_commands_point_at_existing_agents():
    agents = {p.stem for p in AGENTS.glob("*.md")}
    for path in COMMANDS.glob("*.md"):
        agent = frontmatter(path).get("agent")
        assert agent is None or agent in agents


def test_subagent_permission_targets_the_reader():
    reader = frontmatter(AGENTS / "reader.md")
    assert reader["mode"] == "subagent"
    for path in AGENTS.glob("*.md"):
        for rule in frontmatter(path)["permissions"]:
            if rule["action"] == "subagent":
                assert rule["resource"] == "reader"


def test_no_agent_allows_every_zotero_tool():
    for path in AGENTS.glob("*.md"):
        for rule in frontmatter(path)["permissions"]:
            if rule["effect"] == "allow":
                assert rule["action"] != "zotero_*", f"{path.name} allows Zotero writes"


def test_pytorch_comes_from_the_cpu_only_index():
    import tomllib

    from nfh.config import HARNESS_ROOT

    data = tomllib.loads((HARNESS_ROOT / "pyproject.toml").read_text())
    indexes = {i["name"]: i for i in data["tool"]["uv"]["index"]}
    assert indexes["pytorch-cpu"]["url"].endswith("/whl/cpu")
    assert indexes["pytorch-cpu"]["explicit"] is True
    for package in ("torch", "torchvision"):
        assert data["tool"]["uv"]["sources"][package][0]["index"] == "pytorch-cpu"
        assert any(d.startswith(package) for d in data["project"]["dependencies"])


def test_model_cache_lives_in_the_harness(monkeypatch):
    from nfh.config import HARNESS_ROOT, MODEL_CACHE, load_env

    monkeypatch.delenv("HF_HOME", raising=False)
    load_env()
    import os

    assert os.environ["HF_HOME"] == str(MODEL_CACHE)
    assert MODEL_CACHE.is_relative_to(HARNESS_ROOT)


WRITES = {
    "assistant": {
        ("reports/plans/**", "allow"),
        ("reports/research/**", "allow"),
        ("project.md", "ask"),
    },
    "sparring": {("reports/feedback/**", "allow")},
    "reader": set(),
}


def test_each_agent_can_write_only_where_intended():
    assert {p.stem for p in AGENTS.glob("*.md")} == set(WRITES)
    for path in AGENTS.glob("*.md"):
        rules = {
            (r["resource"], r["effect"])
            for r in frontmatter(path)["permissions"]
            if r["action"] == "edit" and r["effect"] != "deny"
        }
        assert rules == WRITES[path.stem], path.name


def test_the_template_has_no_references_to_the_old_layout():
    import re

    banned = (
        r"notes/",
        r"\bjournal\b",
        r"my_reading",
        r"my_status",
        r"themes\.yaml",
        r"reading-queue",
        r"research/readings",
    )
    for path in TEMPLATE_DIR.rglob("*"):
        if path.is_file():
            text = path.read_text()
            for pattern in banned:
                assert not re.search(pattern, text), f"{path.name}: {pattern}"


def test_global_permissions_deny_code_mode_and_the_shell(project):
    rules = jsonc.load(project.root / "opencode.jsonc")["permissions"]
    denied = {
        r["action"] for r in rules if r["effect"] == "deny" and r["resource"] == "*"
    }
    assert {"edit", "shell", "execute", "webfetch", "external_directory"} <= denied


def test_every_server_tool_is_given_to_at_least_one_agent(project):
    tools = {t.name for t in anyio.run(make_server(project).list_tools)}
    granted = {
        r["action"][4:]
        for path in AGENTS.glob("*.md")
        for r in frontmatter(path)["permissions"]
        if r["action"].startswith("nfh_") and r["effect"] == "allow"
    }
    assert tools - granted == set(), f"no agent can use: {sorted(tools - granted)}"
    assert granted - tools == set(), (
        f"agents are granted tools that don't exist: {sorted(granted - tools)}"
    )


def test_there_are_three_agents_and_no_planner_researcher_or_scout():
    assert {p.stem for p in AGENTS.glob("*.md")} == {"assistant", "sparring", "reader"}
    assert frontmatter(AGENTS / "reader.md")["mode"] == "subagent"
    config = (TEMPLATE_DIR / "opencode.jsonc").read_text()
    assert '"default_agent": "assistant"' in config


def test_outside_search_goes_to_the_assistant_and_sparring_only():
    for name in ("assistant", "sparring"):
        allowed = {
            r["action"]
            for r in frontmatter(AGENTS / f"{name}.md")["permissions"]
            if r["effect"] == "allow"
        }
        assert {"websearch", "papers_search_papers"} <= allowed, name
        assert {"nfh_read_candidate", "nfh_save_candidate"} <= allowed, name
    reader = {r["action"] for r in frontmatter(AGENTS / "reader.md")["permissions"]}
    assert not reader & {"websearch", "papers_search_papers", "nfh_save_candidate"}


def test_the_assistant_is_told_to_search_outside_only_when_asked():
    text = (AGENTS / "assistant.md").read_text()
    assert "## Stay in the library unless asked" in text
    assert "only when the writer asks you to" in text
    assert "Never go looking on your own initiative" in text


def test_wrap_asks_for_the_three_parts_and_both_conversational_agents_can_record_them():
    wrap = (COMMANDS / "wrap.md").read_text()
    for part in ("record_session", "discussed", "understandings", "left_off"):
        assert part in wrap, part
    for name in ("assistant", "sparring"):
        allowed = {
            r["action"]
            for r in frontmatter(AGENTS / f"{name}.md")["permissions"]
            if r["effect"] == "allow"
        }
        assert "nfh_record_session" in allowed, name
    reader = {r["action"] for r in frontmatter(AGENTS / "reader.md")["permissions"]}
    assert "nfh_record_session" not in reader
