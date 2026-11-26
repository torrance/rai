"""`nfh sync` follows settings and newer templates without losing your edits."""

import shutil

import pytest

from nfh.config import TEMPLATE_DIR, load_project
from nfh.render import _load_records, _save_records, _sha, sync_project

ASSISTANT = ".opencode/agents/assistant.md"


@pytest.fixture
def newer_template(tmp_path, monkeypatch):
    """A copy of the template, as a later version of nfh might ship it."""
    copy = tmp_path / "template"
    shutil.copytree(TEMPLATE_DIR, copy)
    monkeypatch.setattr("nfh.render.TEMPLATE_DIR", copy)
    return copy


def assistant(project):
    return project.root / ASSISTANT


def add_line(path, line):
    path.write_text(path.read_text() + f"\n{line}\n")


def test_an_untouched_file_follows_a_newer_template(project, newer_template):
    add_line(newer_template / ASSISTANT, "A new rule.")
    assert ASSISTANT in sync_project(project, check=True).drift
    assert ASSISTANT in sync_project(project).written
    assert "A new rule." in assistant(project).read_text()
    assert sync_project(project, check=True).drift == []


def test_your_edit_is_kept_when_nothing_new_arrives(project):
    add_line(assistant(project), "My own rule.")
    result = sync_project(project)
    assert ASSISTANT in result.kept and not result.conflicts
    assert "My own rule." in assistant(project).read_text()
    assert not assistant(project).with_name("assistant.md.nfh-new").exists()
    assert sync_project(project, check=True).drift == []


def test_your_edit_and_a_newer_template_conflict(project, newer_template):
    add_line(assistant(project), "My own rule.")
    add_line(newer_template / ASSISTANT, "Upstream rule.")
    result = sync_project(project)
    assert ASSISTANT in result.conflicts
    mine = assistant(project).read_text()
    assert "My own rule." in mine and "Upstream rule." not in mine
    beside = assistant(project).with_name("assistant.md.nfh-new").read_text()
    assert "Upstream rule." in beside
    assert ASSISTANT in sync_project(project, check=True).drift  # until you merge


def test_a_model_change_updates_only_the_model_line_of_an_edited_agent(project):
    add_line(assistant(project), "My own rule.")
    add_line(project.root / "nfh.toml", '[models]\nresearch = "ollama/mine"')
    project = load_project(project.root)
    result = sync_project(project)
    text = assistant(project).read_text()
    assert 'model: "ollama/mine"' in text and "My own rule." in text
    assert ASSISTANT in result.written and ASSISTANT not in result.conflicts
    assert not assistant(project).with_name("assistant.md.nfh-new").exists()


def test_a_deleted_agent_stays_deleted_until_forced(project):
    assistant(project).unlink()
    assert ASSISTANT in sync_project(project).kept
    assert not assistant(project).exists()
    sync_project(project, force=True)
    assert assistant(project).exists()


def test_a_project_from_an_earlier_version_gets_new_files_beside_its_own(project):
    (project.state_dir / "sync.json").unlink()  # no record of what nfh wrote
    assistant(project).write_text("an old-style agent\n")
    result = sync_project(project)
    assert ASSISTANT in result.conflicts
    assert assistant(project).read_text() == "an old-style agent\n"
    assert assistant(project).with_name("assistant.md.nfh-new").exists()
    sync_project(project, force=True)  # the way to accept everything
    assert "You are the Research Assistant" in assistant(project).read_text()
    assert not assistant(project).with_name("assistant.md.nfh-new").exists()
    assert sync_project(project, check=True).drift == []


# Agent files nfh stopped shipping -------------------------------------------------
RETIRED = ".opencode/agents/scout.md"


def retire(project, text, *, record_text=None):
    """Plant a file nfh once wrote and no longer ships (as after an upgrade)."""
    path = project.root / RETIRED
    path.write_text(text)
    records = _load_records(project)
    records[RETIRED] = {
        "sha": _sha(record_text if record_text is not None else text),
        "body": "",
    }
    _save_records(project, records)
    return path


def test_an_untouched_retired_agent_is_removed(project):
    path = retire(project, "the old scout\n")
    assert RETIRED in sync_project(project, check=True).drift
    result = sync_project(project)
    assert RETIRED in result.retired and not path.exists()
    assert sync_project(project, check=True).drift == []
    assert RETIRED not in _load_records(project)


def test_a_retired_agent_you_edited_is_left_as_yours(project):
    path = retire(project, "my edited scout\n", record_text="the old scout\n")
    assert sync_project(project, check=True).drift == []  # nothing nfh would change
    result = sync_project(project)
    assert RETIRED in result.kept and RETIRED not in result.retired
    assert path.read_text() == "my edited scout\n"
    assert RETIRED not in _load_records(project)  # nfh stops tracking it
    assert RETIRED not in sync_project(project).kept


def test_a_retired_agent_you_already_deleted_is_forgotten(project):
    path = retire(project, "the old scout\n")
    path.unlink()
    result = sync_project(project)
    assert not result.retired and RETIRED not in _load_records(project)
