"""Ollama serves both models the way the harness and OpenCode use them."""

from __future__ import annotations

import json

import numpy as np
import pytest

from rai.config import CHAT_STAGES, parse_ref
from rai.embed import embed_query, embed_texts
from rai.llm import chat_json

from .conftest import advise

pytestmark = pytest.mark.live

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def _loaded(ollama, model):
    for entry in ollama.get("/api/ps").json().get("models", []):
        if entry.get("name") == model or entry.get("model") == model:
            return entry
    return None


def _local(project, stage):
    ref = parse_ref(getattr(project.models, stage))
    return ref if ref.provider == "ollama" else None


def test_local_stage_models_are_pulled(ollama, project):
    names = {m["name"] for m in ollama.get("/api/tags").json().get("models", [])}
    local = [
        ref.model
        for stage in (*CHAT_STAGES, "embedding")
        if (ref := _local(project, stage)) is not None
    ]
    if not local:
        pytest.skip("all stages use OpenCode Go")
    for model in local:
        assert model in names, f"ollama pull {model}"


def test_chat_model_reports_tool_and_thinking_capabilities(ollama, project):
    ref = _local(project, "research")
    if ref is None:
        pytest.skip("research uses OpenCode Go")
    info = ollama.post("/api/show", json={"model": ref.model}).json()
    caps = set(info.get("capabilities", []))
    if not caps:
        pytest.skip("this Ollama version doesn't report capabilities")
    assert "tools" in caps, "OpenCode needs tool calling"
    assert "completion" in caps, "OpenCode only lists models with 'completion'"
    if "thinking" not in caps:
        advise("Ollama doesn't report 'thinking' for the chat model")


def test_server_default_context_matches_project_and_fits_on_gpu(ollama, project):
    """OpenCode sends no num_ctx, so Ollama's default must equal rai's num_ctx.

    If they differ, Ollama reloads the 120 GB model every time an agent and an
    rai job take turns.
    """
    ref = _local(project, "research")
    if ref is None:
        pytest.skip("research uses OpenCode Go")
    resp = ollama.post(
        "/v1/chat/completions",
        json={
            "model": ref.model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
        },
    )
    assert resp.status_code == 200, resp.text
    entry = _loaded(ollama, ref.model)
    assert entry, "model didn't stay loaded; check OLLAMA_KEEP_ALIVE"
    size, vram = entry.get("size", 0), entry.get("size_vram", 0)
    assert vram >= 0.99 * size, (
        f"only {vram / size:.0%} of the model is on GPU; lower OLLAMA_CONTEXT_LENGTH "
        "and num_ctx in rai.toml"
    )
    ctx = entry.get("context_length")
    if ctx is None:
        pytest.skip("this Ollama version doesn't report context_length in /api/ps")
    print(f"Ollama allocated a context of {ctx} for OpenCode's requests")
    if project.models.num_ctx == 0:
        return  # rai asks for nothing, so there is nothing to disagree with
    assert ctx == project.models.num_ctx, (
        f"Ollama's default context is {ctx} but [models] num_ctx says "
        f"{project.models.num_ctx}. Set num_ctx = {ctx} (or 0 to use the server's "
        "own default), or make a model variant with that context (README)."
    )


def test_structured_output_follows_the_schema(project):
    schema = {
        "type": "object",
        "properties": {
            "capital": {"type": "string"},
            "population_millions": {"type": "number"},
            "rivers": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["capital", "population_millions", "rivers"],
    }
    out = chat_json(
        project,
        "Answer with JSON matching the schema.",
        "Give the capital of France, its population in millions, and two rivers.",
        schema,
        think=True,
        stage="summarising",
    )
    assert set(schema["required"]) <= set(out)
    assert "paris" in out["capital"].lower()
    assert isinstance(out["rivers"], list)


def _tool_call(ollama, model, extra=None):
    body = {
        "model": model,
        "messages": [
            {"role": "user", "content": "What's the weather in Perth? Use the tool."}
        ],
        "tools": [WEATHER_TOOL],
        **(extra or {}),
    }
    resp = ollama.post("/v1/chat/completions", json=body)
    assert resp.status_code == 200, resp.text
    calls = resp.json()["choices"][0]["message"].get("tool_calls") or []
    if not calls or calls[0]["function"]["name"] != "get_weather":
        return False
    try:
        args = json.loads(calls[0]["function"]["arguments"])
    except (TypeError, json.JSONDecodeError):
        return False
    return "perth" in str(args.get("city", "")).lower()


def test_tool_calling_through_the_openai_endpoint(ollama, project):
    """This is how OpenCode talks to the model: /v1 with tools."""
    ref = _local(project, "research")
    if ref is None:
        pytest.skip("research uses OpenCode Go")
    good = sum(_tool_call(ollama, ref.model) for _ in range(10))
    assert good >= 9, (
        f"only {good}/10 well-formed tool calls; try LM Studio or vLLM (README)"
    )


def test_reasoning_effort_field_is_accepted(ollama, project):
    """The 'quick' variant in opencode.jsonc sends reasoning_effort."""
    ref = _local(project, "research")
    if ref is None:
        pytest.skip("research uses OpenCode Go")
    resp = ollama.post(
        "/v1/chat/completions",
        json={
            "model": ref.model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "reasoning_effort": "low",
            "max_tokens": 64,
        },
    )
    assert resp.status_code == 200, (
        "Ollama rejected reasoning_effort; remove or change the 'quick' variant's "
        f"body in opencode.jsonc. Response: {resp.text[:300]}"
    )
    assert _tool_call(ollama, ref.model, {"reasoning_effort": "low"}), (
        "tool calling fails with reasoning_effort=low"
    )


def test_embeddings_have_the_right_shape_and_sense(project):
    docs = embed_texts(
        project,
        [
            "A puppy is a young dog.",
            "Corporate tax rates fell across the OECD.",
        ],
    )
    assert docs.shape == (2, project.models.embedding_dims), (
        "fix embedding_dims in config.toml"
    )
    assert np.allclose(np.linalg.norm(docs, axis=1), 1, atol=1e-3)
    query = embed_query(project, "What do we call a baby dog?")
    assert float(docs[0] @ query) > float(docs[1] @ query)


@pytest.mark.slow
def test_long_context_recall(ollama, project):
    """A fact buried mid-way through a long prompt is still found."""
    ref = _local(project, "research")
    if ref is None:
        pytest.skip("research uses OpenCode Go")
    window = project.models.num_ctx or 131_072
    tokens = min(int(window * 0.6), 150_000)
    filler = "The committee met again and discussed routine matters. " * (
        tokens * 4 // 56 // 2
    )
    needle = "The archivist's code word is MARMALADE-42."
    prompt = f"{filler}\n{needle}\n{filler}\nWhat is the archivist's code word?"
    resp = ollama.post(
        "/api/chat",
        json={
            "model": ref.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": (
                {"num_ctx": project.models.num_ctx} if project.models.num_ctx else {}
            ),
        },
    )
    assert resp.status_code == 200, resp.text
    assert "MARMALADE-42" in resp.json()["message"]["content"]
