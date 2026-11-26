"""Fixtures for the live suite: your real project, or a scratch one."""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from nfh.cli import app
from nfh.config import Project, load_env, load_project, ollama_host


@pytest.fixture(scope="session", autouse=True)
def _env():
    load_env()


@pytest.fixture(scope="session")
def live_project(request) -> Project | None:
    path = request.config.getoption("--live-project")
    return load_project(Path(path)) if path else None


@pytest.fixture(scope="session")
def scratch_project(tmp_path_factory, live_project) -> Project:
    """A fresh project from the template, with the live project's settings."""
    root = tmp_path_factory.mktemp("live") / "scratch"
    args = ["new", "scratch", "--path", str(root), "--no-git"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    if live_project is not None:
        (root / "nfh.toml").write_text(
            (live_project.root / "nfh.toml").read_text(encoding="utf-8")
        )
    return load_project(root)


@pytest.fixture(scope="session")
def project(live_project, scratch_project) -> Project:
    """Settings under test: the real project if given, else the scratch one."""
    return live_project or scratch_project


@pytest.fixture(scope="session")
def ollama(project):
    client = httpx.Client(base_url=ollama_host(project), timeout=None)
    try:
        client.get("/api/version", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(
            f"Ollama isn't reachable at {ollama_host(project)}: {exc}. Check the "
            "port forward and [ollama] host (`nfh config` shows what is in force)."
        )
    return client


def need_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} isn't set in the harness .env")
    return value


def advise(message: str) -> None:
    """A non-fatal finding, shown in pytest's warnings summary."""
    warnings.warn(message, UserWarning, stacklevel=2)
