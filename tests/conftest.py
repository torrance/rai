"""Shared fixtures: a temporary project and stand-ins for Ollama."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from rai.cli import app
from rai.config import load_project

DIMS = 1024
TEST_CHAT_MODEL = "ollama/qwen3.8-flash-next:125b-a6b-q4_K_M"
TEST_EMBED_MODEL = "ollama/qwen3-embedding:0.6b"


def pytest_addoption(parser):
    group = parser.getgroup("rai live")
    group.addoption("--live", action="store_true", help="Run tests against your setup")
    group.addoption("--slow", action="store_true", help="Include slow live tests")
    group.addoption("--agents", action="store_true", help="Include OpenCode agent runs")
    group.addoption(
        "--live-project",
        default=None,
        help="A real project folder to check (its Zotero scope and config)",
    )


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "live" in item.keywords and not config.getoption("--live"):
            item.add_marker(pytest.mark.skip(reason="live test: pass --live"))
        elif "slow" in item.keywords and not config.getoption("--slow"):
            item.add_marker(pytest.mark.skip(reason="slow live test: pass --slow"))
        elif "agents" in item.keywords and not config.getoption("--agents"):
            item.add_marker(pytest.mark.skip(reason="agent run: pass --agents"))


def fake_vectors(texts: list[str]) -> np.ndarray:
    """Deterministic bag-of-words vectors: shared words mean nearby vectors."""
    out = np.zeros((len(texts), DIMS), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in re.findall(r"\w+", text.lower()):
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            out[i, h % DIMS] += 1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return out / norms


@pytest.fixture(autouse=True)
def _hermetic_config(request, monkeypatch, tmp_path):
    """Fast tests use a local test profile, not the shipped Go defaults."""
    if "live" in request.keywords:
        return
    test_config = tmp_path / "test-config.toml"
    test_config.write_text(
        "[models]\n"
        f'reading = "{TEST_CHAT_MODEL}"\n'
        f'summarising = "{TEST_CHAT_MODEL}"\n'
        f'research = "{TEST_CHAT_MODEL}"\n'
        f'sparring = "{TEST_CHAT_MODEL}"\n'
        f'embedding = "{TEST_EMBED_MODEL}"\n'
        f"embedding_dims = {DIMS}\n"
    )
    monkeypatch.setattr("rai.config.CONFIG_FILE", test_config)
    monkeypatch.setattr("rai.config.LOCAL_CONFIG_FILE", tmp_path / "absent-local.toml")
    monkeypatch.delenv("RAI_OLLAMA_HOST", raising=False)


@pytest.fixture
def fake_ollama(monkeypatch):
    def embed_texts(project, texts):
        return fake_vectors(list(texts))

    def embed_query(project, query):
        return fake_vectors([query])[0]

    for module in ("rai.embed", "rai.ingest", "rai.cards", "rai.note_search"):
        monkeypatch.setattr(f"{module}.embed_texts", embed_texts, raising=False)
    monkeypatch.setattr("rai.search.embed_query", embed_query)
    monkeypatch.setattr("rai.note_search.embed_query", embed_query, raising=False)
    return embed_texts


@pytest.fixture
def project(tmp_path: Path):
    result = CliRunner().invoke(
        app,
        [
            "new",
            "test-project",
            "--path",
            str(tmp_path / "proj"),
            "--no-git",
            "--tag",
            "lit-review",
        ],
    )
    assert result.exit_code == 0, result.output
    return load_project(tmp_path / "proj")


SAMPLE = """# Labour and platforms

## Introduction

Platform work reorganises labour through algorithmic management. Workers
are rated by customers and dispatched by software.

## Evidence

Surveys of delivery riders found that most workers relied on the platform for
the majority of their income, which made deactivation a severe threat.

The authors argue that flexibility is largely illusory for full-time riders.
"""
