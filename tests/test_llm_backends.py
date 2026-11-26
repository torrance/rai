"""Stage routing and the two chat/embedding backends, without real models."""

from __future__ import annotations

import json

import pytest

from nfh import embed, llm
from nfh.config import (
    OPENCODE_GO_CHAT_URL,
    OPENCODE_GO_EMBEDDINGS_URL,
    opencode_go_key,
    parse_ref,
)


class Response:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def ollama_reply(text: str) -> Response:
    return Response(200, {"message": {"content": text}})


def go_reply(text: str) -> Response:
    return Response(200, {"choices": [{"message": {"content": text}}]})


def test_payloads_match_the_provider(project):
    messages = [{"role": "user", "content": "hi"}]
    schema = {"type": "object"}
    local = llm._payload(
        project, parse_ref("ollama/local"), messages, schema, True, 0.2
    )
    assert local["format"] == schema and "response_format" not in local
    assert local["think"] is True and local["options"]["num_ctx"] == 262144

    remote = llm._payload(
        project, parse_ref("opencode-go/remote"), messages, schema, True, 0.2
    )
    assert remote["response_format"]["json_schema"]["schema"] == schema
    assert "format" not in remote and "think" not in remote
    assert remote["temperature"] == 0.2


def test_go_key_accepts_either_environment_name(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "fallback-key")
    assert opencode_go_key() == "fallback-key"
    monkeypatch.setenv("OPENCODE_API_KEY", "primary-key")
    assert opencode_go_key() == "primary-key"


def test_go_stage_without_a_key_fails_clearly(project, monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    project.models.reading = "opencode-go/model"
    with pytest.raises(llm.LLMError, match="OPENCODE_API_KEY"):
        llm.chat(project, "s", "u", stage="reading")


def test_go_chat_posts_to_the_openai_endpoint_with_its_headers(project, monkeypatch):
    project.models.research = "opencode-go/model"
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    captured: dict = {}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json, headers):
            captured.update(url=url, json=json, headers=headers)
            return go_reply("ok")

    monkeypatch.setattr("nfh.llm.httpx.Client", Client)
    assert llm.chat(project, "s", "u", stage="research") == "ok"
    assert captured["url"] == OPENCODE_GO_CHAT_URL
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["headers"]["User-Agent"] == "nfh/0.1"
    assert captured["headers"]["x-opencode-session"].startswith("ses_nfh_")


def test_go_schema_rejection_falls_back_to_prompted_json(project, monkeypatch):
    project.models.reading = "opencode-go/model"
    calls: list[dict] = []

    def fake_post(p, payload, stage):
        calls.append(payload)
        if "response_format" in payload:
            return Response(400, {"error": "unsupported response_format"})
        return go_reply('{"ok": true}')

    monkeypatch.setattr(llm, "_post", fake_post)
    result = llm.chat_json(project, "s", "u", {"type": "object"}, stage="reading")
    assert result == {"ok": True}
    assert len(calls) == 2
    assert "response_format" not in calls[1]
    assert "JSON only" in calls[1]["messages"][-1]["content"]


def test_chat_json_accepts_a_fenced_reply(project, monkeypatch):
    project.models.reading = "ollama/model"
    monkeypatch.setattr(
        llm, "_post", lambda p, payload, stage: ollama_reply('```json\n{"a": 1}\n```')
    )
    assert llm.chat_json(project, "s", "u", {"type": "object"}, stage="reading") == {
        "a": 1
    }


def test_embedding_dispatch_follows_the_stage_ref(project, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        embed, "_ollama_request", lambda p, batch: seen.append("ollama") or [[1.0]]
    )
    monkeypatch.setattr(
        embed, "_go_request", lambda p, batch: seen.append("go") or [[1.0]]
    )
    project.models.embedding = "ollama/e"
    embed._request(project, ["x"])
    project.models.embedding = "opencode-go/e"
    embed._request(project, ["x"])
    assert seen == ["ollama", "go"]


def test_go_embeddings_send_dimensions_and_sort_by_index(project, monkeypatch):
    project.models.embedding = "opencode-go/e"
    project.models.embedding_dims = 3
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    requests: list[dict] = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json, headers):
            requests.append({"url": url, "json": json, "headers": headers})
            return Response(
                200,
                {
                    "data": [
                        {"index": 1, "embedding": [0.0, 2.0, 0.0]},
                        {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                    ]
                },
            )

    monkeypatch.setattr("nfh.embed.httpx.Client", Client)
    vectors = embed.embed_texts(project, ["a", "b"])
    assert vectors.shape == (2, 3)
    assert requests[0]["url"] == OPENCODE_GO_EMBEDDINGS_URL
    assert requests[0]["json"]["dimensions"] == 3
    assert requests[0]["json"]["encoding_format"] == "float"
    assert requests[0]["headers"]["Authorization"] == "Bearer test-key"
    assert vectors[1][1] == pytest.approx(1.0)  # index 1 sorted into row 1


def test_go_embeddings_retry_without_dimensions(project, monkeypatch):
    project.models.embedding = "opencode-go/e"
    project.models.embedding_dims = 3
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    calls: list[dict] = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json, headers):
            calls.append(json)
            if "dimensions" in json:
                return Response(400, {"error": "dimensions are not supported"})
            return Response(
                200,
                {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]},
            )

    monkeypatch.setattr("nfh.embed.httpx.Client", Client)
    vectors = embed.embed_texts(project, ["a"])
    assert vectors.shape == (1, 3)
    assert len(calls) == 2 and "dimensions" not in calls[1]
