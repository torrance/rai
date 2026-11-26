"""Chat calls to the stage models, with JSON-schema structured output.

Each stage has its own model reference. Ollama uses its native ``/api/chat``
(which supports ``format`` and ``think``); OpenCode Go uses its
OpenAI-compatible endpoint. Callers name the stage, never a model directly.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from .config import (
    CHAT_STAGES,
    OPENCODE_GO_API_KEY_ENVS,
    OPENCODE_GO_CHAT_URL,
    PROMPT_DIR,
    ModelRef,
    Project,
    ollama_host,
    opencode_go_key,
    parse_ref,
)


class LLMError(RuntimeError):
    pass


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def stage_ref(project: Project, stage: str) -> ModelRef:
    if stage not in CHAT_STAGES:
        raise LLMError(
            f"Unknown model stage {stage!r}; choose one of {', '.join(CHAT_STAGES)}."
        )
    return parse_ref(getattr(project.models, stage), field=f"[models] {stage}")


def _session_id(project: Project, stage: str) -> str:
    digest = hashlib.sha1(f"{project.root}:{stage}".encode()).hexdigest()[:16]
    return f"ses_rai_{digest}"


def _headers(project: Project, stage: str) -> dict[str, str]:
    key = opencode_go_key()
    if not key:
        raise LLMError(
            f"{' or '.join(OPENCODE_GO_API_KEY_ENVS)} is not set, but the {stage} "
            "stage uses OpenCode Go. Add it to the harness .env."
        )
    return {
        "Authorization": f"Bearer {key}",
        "User-Agent": "rai/0.1",
        "x-opencode-session": _session_id(project, stage),
    }


def _post(project: Project, payload: dict[str, Any], stage: str) -> httpx.Response:
    ref = stage_ref(project, stage)
    try:
        if ref.provider == "ollama":
            with httpx.Client(timeout=None) as client:
                return client.post(f"{ollama_host(project)}/api/chat", json=payload)
        if ref.provider == "opencode-go":
            with httpx.Client(timeout=300) as client:
                return client.post(
                    OPENCODE_GO_CHAT_URL,
                    json=payload,
                    headers=_headers(project, stage),
                )
    except httpx.HTTPError as exc:
        raise LLMError(
            f"Could not reach the {stage} model {ref} ({ref.provider}): {exc}"
        ) from exc
    raise LLMError(f"No chat backend for provider {ref.provider!r}.")


def _payload(
    project: Project,
    ref: ModelRef,
    messages: list[dict[str, str]],
    schema: dict[str, Any] | None,
    think: bool | str,
    temperature: float | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": ref.model,
        "messages": messages,
        "stream": False,
    }
    if ref.provider == "ollama":
        payload["think"] = think
        payload["options"] = {}
        if project.models.num_ctx > 0:  # 0 = leave it to the server's own default
            payload["options"]["num_ctx"] = project.models.num_ctx
        if temperature is not None:
            payload["options"]["temperature"] = temperature
        if schema is not None:
            payload["format"] = schema
    else:
        if temperature is not None:
            payload["temperature"] = temperature
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "rai_reply", "schema": schema},
            }
    return payload


def _without_schema(payload: dict[str, Any]) -> dict[str, Any]:
    fallback = {k: v for k, v in payload.items() if k != "response_format"}
    messages = [dict(message) for message in fallback["messages"]]
    if "Reply with JSON only." not in messages[-1]["content"]:
        messages[-1]["content"] += "\n\nReply with JSON only."
    fallback["messages"] = messages
    return fallback


def _content(response: httpx.Response, ref: ModelRef, stage: str) -> str:
    if response.status_code != 200:
        raise LLMError(
            f"The {stage} model {ref} ({ref.provider}) returned "
            f"{response.status_code}: {response.text[:500]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError(
            f"The {stage} model {ref} ({ref.provider}) returned invalid JSON."
        ) from exc
    if not isinstance(data, dict):
        raise LLMError(
            f"The {stage} model {ref} ({ref.provider}) returned an unexpected "
            "response shape."
        )
    if ref.provider == "ollama":
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
    else:
        choices = data.get("choices")
        choice = (
            choices[0]
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else {}
        )
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):  # some OpenAI-compatible servers return parts
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
    if not isinstance(content, str):
        raise LLMError(
            f"The {stage} model {ref} ({ref.provider}) returned no text content."
        )
    return content


def chat(
    project: Project,
    system: str,
    user: str,
    schema: dict[str, Any] | None = None,
    think: bool | str = True,
    temperature: float | None = None,
    *,
    stage: str,
) -> str:
    ref = stage_ref(project, stage)
    payload = _payload(
        project,
        ref,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        schema,
        think,
        temperature,
    )
    response = _post(project, payload, stage)
    # OpenCode Go's models don't all accept response_format. If one rejects it,
    # ask for JSON in the prompt instead; chat_json still validates the reply.
    if (
        response.status_code in {400, 404, 422}
        and ref.provider == "opencode-go"
        and "response_format" in payload
    ):
        response = _post(project, _without_schema(payload), stage)
    return _content(response, ref, stage)


def _loads(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


def chat_json(
    project: Project,
    system: str,
    user: str,
    schema: dict[str, Any],
    think: bool | str = True,
    retries: int = 1,
    temperature: float | None = None,
    *,
    stage: str,
) -> dict[str, Any]:
    """Structured call; retries once if the reply isn't valid JSON."""
    last_error: Exception | None = None
    for _ in range(retries + 1):
        text = chat(
            project,
            system,
            user,
            schema=schema,
            think=think,
            temperature=temperature,
            stage=stage,
        )
        try:
            return _loads(text)
        except json.JSONDecodeError as exc:
            last_error = exc
            user = (
                user
                + "\n\nYour previous reply was not valid JSON. Reply with JSON only."
            )
    raise LLMError(f"The {stage} model did not return valid JSON: {last_error}")
