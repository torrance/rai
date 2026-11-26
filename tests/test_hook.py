"""The post-commit hook refreshes notes after a commit that touches drafts/."""

import shutil
import subprocess
import time

import pytest
from typer.testing import CliRunner

from nfh.cli import app

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git missing")
runner = CliRunner()


def git(root, *args):
    base = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*base, *args], check=True, capture_output=True, text=True)


def log_text(root):
    path = root / ".nfh" / "logs" / "hook.log"
    return path.read_text() if path.exists() else ""


def wait_for_log(root, fragment, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if fragment in log_text(root):
            return True
        time.sleep(0.2)
    return False


def test_hook_runs_nfh_only_after_commits_that_touch_drafts(tmp_path):
    root = tmp_path / "p"
    assert runner.invoke(app, ["new", "p", "--path", str(root)]).exit_code == 0
    # Have the hook run `echo` in place of nfh, so the test needs no model.
    toml = root / "nfh.toml"
    toml.write_text(toml.read_text() + '\n[opencode]\nnfh_command = ["echo", "ran"]\n')
    assert runner.invoke(app, ["--project", str(root), "sync"]).exit_code == 0

    git(root, "add", "-A")
    git(root, "commit", "-m", "first commit, which includes drafts/")
    assert wait_for_log(root, "ran --project . brief"), "hook didn't run on commit 1"
    assert "ran --project . manuscript" in log_text(root)
    after_first = log_text(root)

    (root / "project.md").write_text("# Changed\n")  # not a draft
    git(root, "add", "-A")
    git(root, "commit", "-m", "notes only")
    time.sleep(1.5)
    assert log_text(root) == after_first, "hook ran for a commit without drafts"

    (root / "drafts" / "intro.md").write_text("# Intro\n\nText.\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "a draft")
    deadline = time.monotonic() + 15
    while log_text(root) == after_first and time.monotonic() < deadline:
        time.sleep(0.2)
    assert log_text(root).count("ran --project . manuscript") == 2
