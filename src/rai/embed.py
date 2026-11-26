"""Embeddings through Ollama or OpenCode Go.

Ollama adds no prompt template to embedding requests, and many embedding models
expect one (Qwen's want an instruction before queries, never before documents).
The wrapping is configured as [models] query_template and document_template.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import httpx
import numpy as np

from .config import (
    OPENCODE_GO_API_KEY_ENVS,
    OPENCODE_GO_EMBEDDINGS_URL,
    Project,
    ollama_host,
    opencode_go_key,
    parse_ref,
)

BATCH = 32


def _normalise(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype(np.float32)


def _fill(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def _session_id(project: Project) -> str:
    digest = hashlib.sha1(f"{project.root}:embedding".encode()).hexdigest()[:16]
    return f"ses_rai_{digest}"


def _ollama_request(project: Project, batch: list[str]) -> list[list[float]]:
    try:
        with httpx.Client(timeout=None) as client:
            resp = client.post(
                f"{ollama_host(project)}/api/embed",
                json={
                    "model": parse_ref(project.models.embedding).model,
                    "input": batch,
                },
            )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Ollama embeddings failed: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("Ollama embeddings returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Ollama embeddings returned an unexpected response shape.")
    rows = data.get("embeddings")
    if not isinstance(rows, list):
        raise RuntimeError("Ollama embeddings returned no vector list.")
    return rows


def _go_request(project: Project, batch: list[str]) -> list[list[float]]:
    key = opencode_go_key()
    if not key:
        raise RuntimeError(
            f"{' or '.join(OPENCODE_GO_API_KEY_ENVS)} is not set, but [models] "
            "embedding uses OpenCode Go. Add it to the harness .env."
        )
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": "rai/0.1",
        "x-opencode-session": _session_id(project),
    }
    payload = {
        "model": parse_ref(project.models.embedding).model,
        "input": batch,
        "encoding_format": "float",
        "dimensions": project.models.embedding_dims,
    }
    try:
        with httpx.Client(timeout=120) as client:
            resp = client.post(
                OPENCODE_GO_EMBEDDINGS_URL, json=payload, headers=headers
            )
            # Not every embedding model accepts dimensions. If it doesn't, ask
            # for the model's native size; _embed validates it against the config.
            if resp.status_code in {400, 422} and "dimension" in resp.text.lower():
                payload.pop("dimensions", None)
                resp = client.post(
                    OPENCODE_GO_EMBEDDINGS_URL, json=payload, headers=headers
                )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach OpenCode Go embeddings: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"OpenCode Go embeddings returned {resp.status_code}: {resp.text[:500]}"
        )
    try:
        data = resp.json().get("data", [])
        rows = sorted(data, key=lambda row: row.get("index", 0))
        return [row["embedding"] for row in rows]
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RuntimeError(
            "OpenCode Go embeddings returned an unexpected response shape."
        ) from exc


def _request(project: Project, batch: list[str]) -> list[list[float]]:
    ref = parse_ref(project.models.embedding, field="[models] embedding")
    if ref.provider == "ollama":
        return _ollama_request(project, batch)
    if ref.provider == "opencode-go":
        return _go_request(project, batch)
    raise RuntimeError(f"No embedding backend for provider {ref.provider!r}.")


def _embed(project: Project, texts: Sequence[str]) -> np.ndarray:
    dims = project.models.embedding_dims
    if not texts:
        return np.zeros((0, dims), dtype=np.float32)
    out: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        out.extend(_request(project, list(texts[start : start + BATCH])))
    vectors = np.asarray(out, dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[1] != dims:
        got = vectors.shape[1] if vectors.ndim == 2 else "no"
        raise RuntimeError(
            f"{project.models.embedding} returned {got} dimensions; "
            f"[models] embedding_dims says {dims}. Fix embedding_dims."
        )
    return _normalise(vectors)


def embed_texts(project: Project, texts: Sequence[str]) -> np.ndarray:
    """Embed documents (chunks, card sections). Returns unit-length rows."""
    template = project.models.document_template
    return _embed(project, [_fill(template, text=t) for t in texts])


def embed_query(project: Project, query: str) -> np.ndarray:
    m = project.models
    text = _fill(m.query_template, instruction=m.query_instruction, query=query)
    return _embed(project, [text])[0]
