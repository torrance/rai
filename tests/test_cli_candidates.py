import httpx
from typer.testing import CliRunner

from rai import candidates
from rai.cli import app

runner = CliRunner()


def test_status_and_brief_on_empty_project(project):
    for cmd in (["status"], ["brief"]):
        result = runner.invoke(app, ["--project", str(project.root), *cmd])
        assert result.exit_code == 0, result.output
    assert project.brief_path.read_text().startswith("# Briefing")


def test_status_warns_when_generated_files_are_out_of_sync(project):
    (project.root / "opencode.jsonc").unlink()
    result = runner.invoke(app, ["--project", str(project.root), "status"])
    assert result.exit_code == 0, result.output
    assert "out of sync" in result.output and "rai sync" in result.output


def test_show_help_lists_sources():
    result = runner.invoke(app, ["show", "--help"])
    assert result.exit_code == 0 and "sources" in result.output


def test_ingest_without_zotero_explains(project):
    project_toml = project.root / "rai.toml"
    project_toml.write_text(
        project_toml.read_text().replace(
            'api = "http://127.0.0.1:23119/api"', 'api = "http://127.0.0.1:9/api"'
        )
    )
    result = runner.invoke(app, ["--project", str(project.root), "ingest", "--dry-run"])
    assert result.exit_code == 1
    assert "local API" in result.output


ARTICLE = (
    "<html><head><title>Riders</title></head><body><article>"
    + "<h1>Riders and platforms</h1>"
    + "".join(
        f"<p>Paragraph {i} about couriers and ratings and pay.</p>" for i in range(30)
    )
    + "</article></body></html>"
)


def test_read_candidate_full_text_and_profile(project, monkeypatch, fake_ollama):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.openalex.org":
            return httpx.Response(
                200,
                json={
                    "display_name": "Riders",
                    "publication_year": 2020,
                    "authorships": [{"author": {"display_name": "B. Author"}}],
                    "type": "article",
                    "cited_by_count": 12,
                    "is_retracted": False,
                    "primary_location": {
                        "source": {"display_name": "J. Work", "type": "journal"}
                    },
                    "open_access": {"is_oa": True},
                    "best_oa_location": {
                        "is_oa": True,
                        "landing_page_url": "https://example.org/a",
                    },
                    "abstract_inverted_index": {"Couriers": [0], "matter": [1]},
                },
            )
        if host == "api.unpaywall.org":
            return httpx.Response(404)
        return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})

    monkeypatch.setattr(
        candidates,
        "_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    full = candidates.read_candidate(project, "https://doi.org/10.1234/ABC")
    assert full["read_depth"] == "full text"
    assert full["metadata"]["abstract"] == "Couriers matter"
    assert full["metadata"]["cited_by_count"] == 12
    assert "Paragraph 3 about couriers" in full["text"]
    assert full["in_library"] is None

    profile = candidates.read_candidate(project, "10.1234/abc", max_tokens=50)
    assert profile["read_depth"].startswith("profile")
    cid = profile["candidate_id"]
    heading = profile["table_of_contents"][0]["heading"]
    section = candidates.read_candidate_section(project, cid, heading)
    assert "couriers" in section["text"]
