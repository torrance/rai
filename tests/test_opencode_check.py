"""`nfh doctor` and `nfh open` insist on OpenCode 2."""

from types import SimpleNamespace

from typer.testing import CliRunner

from nfh import cli
from nfh.cli import app

runner = CliRunner()


class FakeTags:
    def json(self):
        return {"models": []}


def run(project, *args):
    return runner.invoke(app, ["--project", str(project.root), *args])


def fake_version(monkeypatch, which, stdout):
    monkeypatch.setattr(
        "nfh.cli.shutil.which", lambda name: "/bin/opencode" if which else None
    )
    monkeypatch.setattr(
        "nfh.cli.subprocess.run",
        lambda *a, **k: SimpleNamespace(stdout=stdout, stderr="", returncode=0),
    )


def test_version_forms_are_read(monkeypatch):
    fake_version(monkeypatch, True, "1.19.6\n")
    assert cli._opencode_info() == (True, 1, "1.19.6")
    fake_version(monkeypatch, True, "opencode v2.3.2\n")
    assert cli._opencode_info() == (True, 2, "2.3.2")
    fake_version(monkeypatch, True, "something odd")
    assert cli._opencode_info() == (True, None, "")
    fake_version(monkeypatch, False, "")
    assert cli._opencode_info() == (False, None, "")


def test_doctor_rejects_opencode_1_and_explains_how_to_install_2(project, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeTags())
    monkeypatch.setattr("nfh.cli._opencode_info", lambda: (True, 1, "1.19.6"))
    out = run(project, "doctor").output
    assert "FIX  opencode 1.19.6 is not OpenCode 2" in out
    assert "curl -fsSL https://opencode.ai/v2/install | bash" in out
    assert "npm install -g @opencode/cli" in out


def test_doctor_accepts_opencode_2_and_reports_when_missing(project, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeTags())
    monkeypatch.setattr("nfh.cli._opencode_info", lambda: (True, 2, "2.3.2"))
    assert "ok   opencode 2.3.2 (OpenCode 2)" in run(project, "doctor").output
    monkeypatch.setattr("nfh.cli._opencode_info", lambda: (False, None, ""))
    assert "FIX  opencode\n" in run(project, "doctor").output


def test_open_refuses_to_start_opencode_1(project, monkeypatch):
    monkeypatch.setattr("nfh.cli._opencode_info", lambda: (True, 1, "1.19.6"))
    result = run(project, "open")
    assert result.exit_code == 1
    assert "is version 1.19.6" in result.output
    assert "opencode.ai/v2/install" in result.output


def test_open_starts_opencode_in_the_project_even_from_elsewhere(
    project, monkeypatch, tmp_path
):
    """`opencode` reads $PWD, so `nfh open` must set it as well as changing dir."""
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    monkeypatch.setattr("nfh.cli._opencode_info", lambda: (True, 2, "2.3.2"))
    monkeypatch.setattr("nfh.cli.shutil.which", lambda name: "/bin/opencode")
    started = {}

    def fake_exec(binary, args):
        started.update(binary=binary, cwd=os.getcwd(), pwd=os.environ["PWD"])

    monkeypatch.setattr("nfh.cli.os.execv", fake_exec)
    result = run(project, "open")
    assert result.exit_code == 0, result.output
    assert started["cwd"] == str(project.root.resolve())
    assert started["pwd"] == str(project.root)
