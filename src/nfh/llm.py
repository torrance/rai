"""Chat calls to Ollama, with JSON-schema structured output for pipelines."""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import PROMPT_DIR, Project, ollama_host


class LLMError(RuntimeError):
    pass


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def _post(project: Project, payload: dict[str, Any]) -> httpx.Response:
    with httpx.Client(timeout=None) as client:
        return client.post(f"{ollama_host(project)}/api/chat", json=payload)


def chat(
    project: Project,
    system: str,
    user: str,
    schema: dict[str, Any] | None = None,
    think: bool | str = True,
    temperature: float | None = None,
) -> str:
    payload: dict[str, Any] = {
        "model": project.models.chat,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": think,
        "options": {},
    }
    if project.models.num_ctx > 0:  # 0 = leave it to the server's own default
        payload["options"]["num_ctx"] = project.models.num_ctx
    if temperature is not None:
        payload["options"]["temperature"] = temperature
    if schema is not None:
        payload["format"] = schema
    resp = _post(project, payload)
    if resp.status_code != 200:
        raise LLMError(f"Ollama returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()["message"]["content"]


def chat_json(
    project: Project,
    system: str,
    user: str,
    schema: dict[str, Any],
    think: bool | str = True,
    retries: int = 1,
) -> dict[str, Any]:
    """Structured call; retries once if the reply isn't valid JSON."""
    last_error: Exception | None = None
    for _ in range(retries + 1):
        text = chat(project, system, user, schema=schema, think=think)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            last_error = exc
            user = (
                user
                + "\n\nYour previous reply was not valid JSON. Reply with JSON only."
            )
    raise LLMError(f"Model did not return valid JSON: {last_error}")
