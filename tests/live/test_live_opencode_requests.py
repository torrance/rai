"""What OpenCode really sends the model, checked against a stand-in endpoint.

No model is needed: a tiny local server plays Ollama, records each chat request
OpenCode makes, and answers with a fixed reply. That shows, per agent, whether
the project's briefing reaches the model, which tools it is offered (denied
tools should be hidden), and which settings go with the request.
"""

from __future__ import annotations

import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from typer.testing import CliRunner

from rai.cli import app
from rai.config import TEMPLATE_DIR, load_project

from .test_live_opencode import _run, wait_for_servers

pytestmark = pytest.mark.live
MARKER = "PINEAPPLE-7"
runner = CliRunner()


class FakeOllama(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *args):
        pass

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json({"version": "0.30.10", "models": [], "data": []})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0))).decode()
        if "/chat/completions" not in self.path:
            return self._json({})
        request = json.loads(body)
        FakeOllama.requests.append(request)

        def chunk(delta, finish=None, usage=None):
            data = {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": request.get("model", "m"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if usage:
                data["usage"] = usage
            return data

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        for data in (
            chunk({"role": "assistant", "content": "FAKE-REPLY"}),
            chunk({}, "stop", usage),
        ):
            self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture(scope="module")
def fake_project(tmp_path_factory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    root = tmp_path_factory.mktemp("fake") / "p"
    assert runner.invoke(app, ["new", "p", "--path", str(root)]).exit_code == 0
    toml = root / "rai.toml"
    toml.write_text(
        toml.read_text()
        + f'\n[ollama]\nhost = "http://127.0.0.1:{port}"\n'
        + '\n[models]\nreading = "ollama/fake-model"\n'
        + 'summarising = "ollama/fake-model"\n'
        + 'research = "ollama/fake-model"\n'
        + 'sparring = "ollama/fake-model"\n'
    )
    assert runner.invoke(app, ["--project", str(root), "sync"]).exit_code == 0
    project = load_project(root)
    assert runner.invoke(app, ["--project", str(root), "brief"]).exit_code == 0
    project.brief_path.write_text(
        project.brief_path.read_text() + f"\nMarker for tests: {MARKER}\n"
    )
    wait_for_servers(project)
    yield project
    server.shutdown()


def frontmatter(agent: str) -> dict:
    path = TEMPLATE_DIR / ".opencode" / "agents" / f"{agent}.md"
    head = re.match(r"^---\n(.*?)\n---\n", path.read_text(), re.DOTALL)
    return yaml.safe_load(head.group(1))


def last_request_for(project, agent: str) -> dict:
    FakeOllama.requests.clear()
    result = _run(project, "run", "--agent", agent, "hello", timeout=300)
    if not FakeOllama.requests:
        agents = _run(project, "debug", "agents", timeout=120)
        try:
            known = [a["id"] for a in json.loads(agents.stdout)]
        except json.JSONDecodeError:
            known = agents.stdout[-300:] or agents.stderr[-300:]
        pytest.fail(
            "OpenCode never reached the model endpoint.\n"
            f"run printed: {result.stdout[-600:]}{result.stderr[-600:]}\n"
            f"agents it knows: {known}"
        )
    return FakeOllama.requests[-1]


@pytest.mark.parametrize("agent", ["assistant", "sparring"])
def test_the_model_gets_the_briefing_and_exactly_the_agents_tools(fake_project, agent):
    request = last_request_for(fake_project, agent)
    assert MARKER in json.dumps(request["messages"]), (
        "the briefing in .rai/brief.md didn't reach the model"
    )
    offered = {t["function"]["name"] for t in request.get("tools", [])}
    allowed = {
        r["action"]
        for r in frontmatter(agent)["permissions"]
        if r["effect"] == "allow" and r["action"].startswith("rai_")
    }
    offered_rai = {n for n in offered if n.startswith("rai_")}
    assert offered_rai == allowed, (
        f"{agent}: offered {sorted(offered_rai)}, intended {sorted(allowed)}"
    )
    writes = ("add_", "create_", "update_", "delete_", "attach_file", "manage_", "set_")
    zotero_writes = {
        n for n in offered if n.startswith("zotero_") and any(w in n for w in writes)
    }
    assert not zotero_writes, f"{agent} is offered Zotero write tools: {zotero_writes}"
    assert "execute" not in offered, "Code Mode should be off"
    papers = {n for n in offered if n.startswith("papers_")}
    assert papers == {"papers_search_papers"}


@pytest.mark.parametrize("agent, effort", [("assistant", None), ("sparring", None)])
def test_the_quick_variant_is_applied_only_where_intended(fake_project, agent, effort):
    request = last_request_for(fake_project, agent)
    assert request.get("reasoning_effort") == effort
