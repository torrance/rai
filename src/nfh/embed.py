"""Embeddings through Ollama's /api/embed.

Ollama adds no prompt template to embedding requests, and many embedding models
expect one (Qwen's want an instruction before queries, never before documents).
The wrapping is configured as [models] query_template and document_template.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import numpy as np

from .config import Project, ollama_host

BATCH = 32


def _normalise(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype(np.float32)


def _fill(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def _request(project: Project, batch: list[str]) -> list[list[float]]:
    with httpx.Client(timeout=None) as client:
        resp = client.post(
            f"{ollama_host(project)}/api/embed",
            json={"model": project.models.embed, "input": batch},
        )
    resp.raise_for_status()
    return resp.json()["embeddings"]


def _embed(project: Project, texts: Sequence[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, project.models.embed_dims), dtype=np.float32)
    out: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        out.extend(_request(project, list(texts[start : start + BATCH])))
    vectors = np.asarray(out, dtype=np.float32)
    if vectors.shape[1] != project.models.embed_dims:
        raise RuntimeError(
            f"{project.models.embed} returned {vectors.shape[1]} dimensions; "
            f"[models] embed_dims says {project.models.embed_dims}. Fix embed_dims."
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
