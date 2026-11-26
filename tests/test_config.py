"""Settings: layering, validation, the Ollama address, and what they render."""

import dataclasses
import tomllib

import numpy as np
import pytest
from typer.testing import CliRunner

from nfh import config as cfg
from nfh import jsonc
from nfh.cli import app
from nfh.config import (
    ConfigError,
    Project,
    describe,
    load_harness_settings,
    load_project,
    normalise_host,
    ollama_host,
    opencode_base_url,
)
from nfh.embed import embed_query, embed_texts
from nfh.store import Store

runner = CliRunner()


def cli(project, *args):
    return runner.invoke(app, ["--project", str(project.root), *args])


def add_to_nfh_toml(project, text):
    path = project.root / "nfh.toml"
    path.write_text(path.read_text() + "\n" + text + "\n")


# The shipped file -----------------------------------------------------------
def test_shipped_config_is_complete_and_equals_the_builtin_defaults():
    """config.toml is fully populated with the defaults.

    If this fails after you edited config.toml, move the change to
    config.local.toml (gitignored) and restore config.toml.
    """
    data = tomllib.loads((cfg.HARNESS_ROOT / "config.toml").read_text())
    builtin = Project(root=cfg.HARNESS_ROOT)
    for section in ("ollama", "models", "opencode", "scout"):
        assert data[section] == dataclasses.asdict(getattr(builtin, section)), section


def test_shipped_config_loads_without_complaint(monkeypatch):
    monkeypatch.setattr("nfh.config.CONFIG_FILE", cfg.HARNESS_ROOT / "config.toml")
    settings = load_harness_settings()
    assert settings.models.chat == "qwen3.8-flash-next:125b-a6b-q4_K_M"
    assert settings.ollama.host == "http://127.0.0.1:11434"


# Layers ---------------------------------------------------------------------
def test_layers_override_in_order_and_report_their_source(tmp_path, monkeypatch):
    harness = tmp_path / "config.toml"
    local = tmp_path / "config.local.toml"
    harness.write_text('[models]\nchat = "from-harness"\nembed = "e1"\n')
    local.write_text('[models]\nchat = "from-local"\n')
    monkeypatch.setattr("nfh.config.CONFIG_FILE", harness)
    monkeypatch.setattr("nfh.config.LOCAL_CONFIG_FILE", local)
    root = tmp_path / "p"
    root.mkdir()
    (root / "nfh.toml").write_text('[project]\nname = "p"\n[models]\nembed = "e2"\n')
    project = load_project(root)
    assert project.models.chat == "from-local"
    assert project.models.embed == "e2"
    sources = {(s, k): src for s, k, _, src in describe(project)}
    assert sources[("models", "chat")] == "config.local.toml"
    assert sources[("models", "embed")] == "nfh.toml"
    assert sources[("models", "num_ctx")] == cfg.BUILTIN


@pytest.mark.parametrize(
    "toml, fragment",
    [
        ('[models]\nchatt = "x"\n', "unknown setting [models] chatt"),
        ("[modles]\nchat = 'x'\n", "unknown section [modles]"),
        ('[models]\nnum_ctx = "big"\n', "num_ctx should be a int"),
        ("[models]\nnum_ctx = -1\n", "num_ctx must be 0"),
        ('[models]\nquery_template = "no placeholder"\n', "must contain {query}"),
        ('[models]\ndocument_template = "none"\n', "must contain {text}"),
        ('[ollama]\nhost = " "\n', "host can't be empty"),
        ("[opencode]\nnfh_command = 3\n", "nfh_command should be a list"),
    ],
)
def test_bad_settings_are_rejected_with_a_useful_message(tmp_path, toml, fragment):
    root = tmp_path / "p"
    root.mkdir()
    (root / "nfh.toml").write_text(toml)
    with pytest.raises(ConfigError) as err:
        load_project(root)
    assert fragment in str(err.value)
    assert "nfh.toml" in str(err.value) or "nfh config" in str(err.value)


# The Ollama address ---------------------------------------------------------
@pytest.mark.parametrize(
    "given, expected",
    [
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("127.0.0.1:11435", "http://127.0.0.1:11435"),
        ("http://localhost:11435/", "http://localhost:11435"),
        ("http://localhost:11435/v1", "http://localhost:11435"),
        ("https://gpu.example.org/v1/", "https://gpu.example.org"),
    ],
)
def test_host_forms_are_normalised(given, expected):
    assert normalise_host(given) == expected


def test_host_env_override_and_opencode_address(project, monkeypatch):
    project.ollama.host = "127.0.0.1:11435"
    assert ollama_host(project) == "http://127.0.0.1:11435"
    assert opencode_base_url(project) == "http://127.0.0.1:11435/v1"
    monkeypatch.setenv("NFH_OLLAMA_HOST", "http://other:1")
    assert ollama_host(project) == "http://other:1"
    assert opencode_base_url(project) == "http://127.0.0.1:11435/v1"  # file only
    project.ollama.opencode_base_url = "http://elsewhere:2/v1/"
    assert opencode_base_url(project) == "http://elsewhere:2/v1"


def test_the_ollama_clis_own_variable_is_not_read(project, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "10.9.9.9:1")
    assert ollama_host(project) == "http://127.0.0.1:11434"


# What the model calls send --------------------------------------------------
class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"message": {"content": "{}"}}


@pytest.mark.parametrize("num_ctx, expected", [(131072, {"num_ctx": 131072}), (0, {})])
def test_num_ctx_is_sent_only_when_set(project, monkeypatch, num_ctx, expected):
    from nfh import llm

    seen = []
    monkeypatch.setattr(
        llm, "_post", lambda p, payload: seen.append(payload) or FakeResponse()
    )
    project.models.num_ctx = num_ctx
    llm.chat(project, "s", "u")
    assert seen[0]["options"] == expected
    assert seen[0]["model"] == project.models.chat


def test_embedding_templates_wrap_queries_and_documents(project, monkeypatch):
    seen = []
    dims = project.models.embed_dims

    def fake_request(p, batch):
        seen.append(list(batch))
        return [[1.0] * dims for _ in batch]

    monkeypatch.setattr("nfh.embed._request", fake_request)
    embed_texts(project, ["a passage"])
    embed_query(project, "a question")
    assert seen[0] == ["a passage"]  # default: documents are not wrapped
    assert seen[1] == [
        "Instruct: Given a question about a body of texts, retrieve passages that "
        "answer it\nQuery: a question"
    ]
    project.models.document_template = "search_document: {text}"
    project.models.query_template = "search_query: {query}"
    embed_texts(project, ["a passage"])
    embed_query(project, "a question")
    assert seen[2] == ["search_document: a passage"]
    assert seen[3] == ["search_query: a question"]
    assert isinstance(embed_texts(project, ["x"]), np.ndarray)


def test_index_refuses_a_changed_document_template(project):
    store = Store(project)
    store.check_embedding_model("m", 1024, "{text}")
    store.check_embedding_model("m", 1024, "{text}")
    with pytest.raises(RuntimeError, match="--reembed"):
        store.check_embedding_model("m", 1024, "search_document: {text}")
    store.conn.execute("DELETE FROM meta WHERE key='embed_doc_template'")
    store.check_embedding_model("m", 1024, "{text}")  # an index from before templates
    store.conn.execute("DELETE FROM meta WHERE key='embed_doc_template'")
    with pytest.raises(RuntimeError, match="--reembed"):
        store.check_embedding_model("m", 1024, "search_document: {text}")
    store.close()


# Rendering and sync ---------------------------------------------------------
def test_defaults_render_to_the_previous_hardcoded_values(project):
    config = jsonc.load(project.root / "opencode.jsonc")
    assert config["model"] == "ollama/qwen-flash"
    provider = config["providers"]["ollama"]
    assert provider["package"] == "@opencode/ai/providers/openai-compatible"
    assert provider["settings"]["baseURL"] == "http://127.0.0.1:11434/v1"
    model = provider["models"]["qwen-flash"]
    assert model["modelID"] == "qwen3.8-flash-next:125b-a6b-q4_K_M"
    assert model["variants"] == [{"id": "quick", "body": {"reasoning_effort": "low"}}]
    assert config["mcp"]["servers"]["nfh"]["command"] == [
        "uv", "run", "--project", str(cfg.HARNESS_ROOT), "nfh",
        "--project", ".", "serve",
    ]  # fmt: skip
    agents = project.root / ".opencode" / "agents"
    assert 'model: "ollama/qwen-flash"' in (agents / "assistant.md").read_text()
    assert 'model: "ollama/qwen-flash"' in (agents / "sparring.md").read_text()
    hook = (project.root / ".githooks" / "post-commit").read_text()
    assert f"NFH='uv run --project {cfg.HARNESS_ROOT} nfh'" in hook


def test_a_fresh_project_is_in_sync(project):
    result = cli(project, "sync", "--check")
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_changing_settings_then_syncing_updates_every_generated_value(project):
    add_to_nfh_toml(
        project,
        '[ollama]\nhost = "127.0.0.1:11435"\n'
        '[models]\nchat = "qwen-flash-128k"\n'
        '[opencode]\nmodel_alias = "mine"\nquick_variant = "fast"\n'
        'quick_reasoning_effort = ""\nnfh_command = ["nfh"]\n',
    )
    check = cli(project, "sync", "--check")
    assert check.exit_code == 1 and "opencode.jsonc" in check.output
    assert cli(project, "sync").exit_code == 0
    assert cli(project, "sync", "--check").exit_code == 0

    config = jsonc.load(project.root / "opencode.jsonc")
    assert config["model"] == "ollama/mine"
    provider = config["providers"]["ollama"]
    assert provider["settings"]["baseURL"] == "http://127.0.0.1:11435/v1"
    assert provider["models"]["mine"]["modelID"] == "qwen-flash-128k"
    assert provider["models"]["mine"]["variants"] == [{"id": "fast", "body": {}}]
    assert config["mcp"]["servers"]["nfh"]["command"] == [
        "nfh", "--project", ".", "serve",
    ]  # fmt: skip
    agents = project.root / ".opencode" / "agents"
    assert 'model: "ollama/mine"' in (agents / "assistant.md").read_text()
    assert 'model: "ollama/mine"' in (agents / "sparring.md").read_text()
    assert "NFH='nfh'" in (project.root / ".githooks" / "post-commit").read_text()
    # only the model line of an agent changes; its instructions stay as they were
    assert "You are the Research Assistant" in (agents / "assistant.md").read_text()


def test_hand_edited_generated_files_are_not_overwritten(project):
    path = project.root / "opencode.jsonc"
    path.write_text(path.read_text() + "// my own note\n")
    add_to_nfh_toml(project, '[models]\nchat = "other-model"\n')
    result = cli(project, "sync")
    assert result.exit_code == 1 and "edited by hand" in result.output
    assert "my own note" in path.read_text()
    assert "other-model" not in path.read_text()
    assert "other-model" in (project.root / "opencode.jsonc.nfh-new").read_text()
    forced = cli(project, "sync", "--force")
    assert forced.exit_code == 0
    assert "other-model" in path.read_text()
    assert not (project.root / "opencode.jsonc.nfh-new").exists()


def test_sync_all_covers_every_project_in_projects(tmp_path, monkeypatch):
    monkeypatch.setattr("nfh.cli.PROJECTS_DIR", tmp_path / "projects")
    for name in ("alpha", "beta"):
        assert runner.invoke(app, ["new", name, "--no-git"]).exit_code == 0
    shared = tmp_path / "config.toml"
    shared.write_text('[ollama]\nhost = "http://gpu:9"\n')
    monkeypatch.setattr("nfh.config.CONFIG_FILE", shared)
    result = runner.invoke(app, ["sync", "--all"])
    assert result.exit_code == 0, result.output
    for name in ("alpha", "beta"):
        text = (tmp_path / "projects" / name / "opencode.jsonc").read_text()
        assert "http://gpu:9/v1" in text


def test_config_command_shows_values_and_sources(project):
    add_to_nfh_toml(project, '[models]\nchat = "mine"\n')
    out = cli(project, "config").output
    assert "[models] chat = 'mine'   <- nfh.toml" in out
    assert "[models] num_ctx = 262144   <- built-in default" in out
    assert "OpenCode reaches it at http://127.0.0.1:11434/v1" in out


# doctor ---------------------------------------------------------------------
class FakeTags:
    def json(self):
        return {"models": [{"name": "qwen3.6:27b"}, {"name": "gemma4:31b"}]}


def test_doctor_lists_what_the_server_has_when_a_name_is_missing(project, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeTags())
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    out = cli(project, "doctor").output
    assert "FIX  chat model qwen3.8-flash-next:125b-a6b-q4_K_M" in out
    assert "the server has: gemma4:31b, qwen3.6:27b" in out
    assert "nfh ignores it" in out


def test_doctor_flags_project_files_that_lag_the_settings(project, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeTags())
    add_to_nfh_toml(project, '[models]\nchat = "qwen3.6:27b"\n')
    out = cli(project, "doctor").output
    assert "ok   chat model qwen3.6:27b" in out
    assert "FIX  opencode.jsonc, agent models and git hook match the settings" in out
    assert "run `nfh sync`" in out


def test_doctor_outside_a_project_uses_the_harness_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeTags())
    monkeypatch.chdir(tmp_path)
    out = runner.invoke(app, ["doctor"]).output
    assert "Using the harness settings" in out


def test_serve_reports_a_bad_setting_plainly_instead_of_crashing(project):
    add_to_nfh_toml(project, '[models]\nchatt = "x"\n')
    result = cli(project, "serve")
    assert result.exit_code == 1
    assert "unknown setting [models] chatt" in result.output
    assert "Traceback" not in result.output


# The paper search ------------------------------------------------------
def papers_code(project):
    config = jsonc.load(project.root / "opencode.jsonc")
    command = config["mcp"]["servers"]["papers"]["command"]
    assert command[:5] == [
        "uvx",
        "--from",
        "paper-search-mcp==0.1.4",
        "--with",
        "mcp<2",
    ]
    assert command[5:7] == ["python", "-c"]
    compile(command[7], "<papers launcher>", "exec")  # the generated code is valid
    return command[7]


def test_the_paper_search_server_is_started_with_the_default_sources(project):
    code = papers_code(project)
    assert "ALL_SOURCES[:] = ['openalex', 'crossref', 'doaj', 'core', 'ssrn']" in code
    for excluded in ("arxiv", "biorxiv", "medrxiv", "pubmed", "dblp"):
        assert excluded not in code


def test_changing_the_paper_sources_follows_through_sync(project):
    add_to_nfh_toml(project, '[scout]\npaper_sources = ["crossref", "hal"]')
    assert cli(project, "sync").exit_code == 0
    assert "ALL_SOURCES[:] = ['crossref', 'hal']" in papers_code(project)


@pytest.mark.parametrize(
    "toml, fragment",
    [
        ('[scout]\npaper_sources = ["arxv"]\n', "unknown sources ['arxv']"),
        ("[scout]\npaper_sources = []\n", "can't be empty"),
        ('[scout]\nsources = ["crossref"]\n', "unknown setting [scout] sources"),
    ],
)
def test_bad_paper_sources_are_rejected(tmp_path, toml, fragment):
    root = tmp_path / "p"
    root.mkdir()
    (root / "nfh.toml").write_text(toml)
    with pytest.raises(ConfigError) as err:
        load_project(root)
    assert fragment in str(err.value)
