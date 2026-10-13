import json
import subprocess
from pathlib import Path

import pytest

from ticketmaster import __version__
from ticketmaster.cli import main
from ticketmaster.upgrade import SOURCE, Install, describe, install_from, main_head, upgrade, upgrade_notice

VENVS = "/home/me/.local/pipx/venvs"
APP = f"{VENVS}/ticketmaster"
NEW_PYTHON = f"{VENVS}/ticketmaster/bin/python"
SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeRun:
    """Stands in for subprocess.run: records each command; `failing` makes a step exit non-zero."""

    def __init__(self, venvs: str = VENVS, **failing: int):
        self.calls: list[list[str]] = []
        self.venvs = venvs
        self.failing = failing

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:3] == ["pipx", "environment", "--value"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.venvs + "\n", stderr="")
        if cmd[1] == "-c":
            direct_url = {"url": "https://github.com/kareblak/ticketmaster.git", "vcs_info": {"vcs": "git", "commit_id": SHA}}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(direct_url) + "\n", stderr="")
        step = "reinstall" if cmd[:2] == ["pipx", "install"] else "skill" if cmd[-1] == "install-skill" else "other"
        code = self.failing.get(step, 0)
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="boom\n" if code else "")


def has_pipx(name):
    return f"/usr/bin/{name}"


def no_pipx(name):
    return None


GIT = Install(commit="feedface" * 5, editable_dir=None)


def test_reinstalls_from_github_then_refreshes_skill_with_the_new_code(capsys):
    run = FakeRun()
    assert upgrade(GIT, run=run, which=has_pipx, prefix=APP) == 0
    assert ["pipx", "install", "--force", SOURCE] in run.calls
    assert [NEW_PYTHON, "-m", "ticketmaster", "install-skill"] in run.calls
    assert run.calls.index([NEW_PYTHON, "-m", "ticketmaster", "install-skill"]) > 0
    out = capsys.readouterr().out
    assert SOURCE in out and SHA[:12] in out


def test_missing_pipx_fails_without_touching_anything(capsys):
    run = FakeRun()
    assert upgrade(GIT, run=run, which=no_pipx, prefix=APP) == 1
    assert run.calls == []
    assert "pipx" in capsys.readouterr().err


def test_install_outside_pipx_is_refused(capsys):
    run = FakeRun()
    assert upgrade(GIT, run=run, which=has_pipx, prefix="/usr/local") == 1
    assert not any(c[:2] == ["pipx", "install"] for c in run.calls)
    assert "/usr/local" in capsys.readouterr().err


def test_pipx_without_a_venv_dir_changes_nothing(capsys):
    run = FakeRun(venvs="")
    assert upgrade(GIT, run=run, which=has_pipx, prefix=APP) == 1
    assert not any(c[:2] == ["pipx", "install"] for c in run.calls)
    assert "pipx did not say" in capsys.readouterr().err


def test_failed_reinstall_leaves_the_skill_alone(capsys):
    run = FakeRun(reinstall=1)
    assert upgrade(GIT, run=run, which=has_pipx, prefix=APP) == 1
    assert not any(c[-1] == "install-skill" for c in run.calls)
    assert "reinstall failed" in capsys.readouterr().err


def test_failed_skill_refresh_is_reported(capsys):
    run = FakeRun(skill=1)
    assert upgrade(GIT, run=run, which=has_pipx, prefix=APP) == 1
    assert "install-skill" in capsys.readouterr().err


def test_editable_install_says_git_pull_instead(capsys, tmp_path):
    run = FakeRun()
    assert upgrade(Install(commit=None, editable_dir=tmp_path), run=run, which=has_pipx, prefix=APP) == 0
    assert run.calls == []
    out = capsys.readouterr().out
    assert "git pull" in out and str(tmp_path) in out


def write_direct_url(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "direct_url.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("data, expected", [
    ({"url": "https://github.com/kareblak/ticketmaster.git", "vcs_info": {"vcs": "git", "commit_id": SHA}},
     Install(commit=SHA, editable_dir=None)),
    ({"url": "file:///home/me/code/ticketmaster", "dir_info": {"editable": True}},
     Install(commit=None, editable_dir=Path("/home/me/code/ticketmaster"))),
    ({"url": "file:///tmp/ticketmaster-0.2.0.whl", "archive_info": {}}, Install(commit=None, editable_dir=None)),
    (None, Install(commit=None, editable_dir=None)),
])
def test_install_from_direct_url(data, expected):
    assert install_from(None if data is None else json.dumps(data)) == expected


def test_install_from_garbage_is_unknown():
    assert install_from("not json") == Install(commit=None, editable_dir=None)


def test_describe():
    assert describe(Install(commit=SHA, editable_dir=None)) == SHA[:12]
    assert describe(Install(commit=None, editable_dir=Path("/c"))) == "dev install, editable from /c"
    assert describe(Install(commit=None, editable_dir=None)) == "unknown commit"


def test_cli_version_reports_the_commit(capsys, monkeypatch):
    monkeypatch.setattr("ticketmaster.upgrade.installed", lambda: Install(commit=SHA, editable_dir=None))
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"ticketmaster {__version__} ({SHA[:12]})"


def test_cli_upgrade_runs_outside_herdr(monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    calls = []
    monkeypatch.setattr("ticketmaster.upgrade.installed", lambda: GIT)
    monkeypatch.setattr("ticketmaster.upgrade.upgrade", lambda install, **kw: calls.append(install) or 0)
    assert main(["upgrade"]) == 0
    assert calls == [GIT]


def never_asked():
    raise AssertionError("the remote must not be asked")


def test_notice_when_main_has_moved_on():
    assert upgrade_notice(GIT, lambda: SHA) == "a newer ticketmaster is available: run `ticketmaster upgrade`"


def test_no_notice_when_up_to_date():
    assert upgrade_notice(Install(commit=SHA, editable_dir=None), lambda: SHA) is None


def test_no_notice_and_no_lookup_for_editable_or_unknown_installs(tmp_path):
    assert upgrade_notice(Install(commit=None, editable_dir=tmp_path), never_asked) is None
    assert upgrade_notice(Install(commit=None, editable_dir=None), never_asked) is None


def test_no_notice_when_main_cannot_be_reached():
    assert upgrade_notice(GIT, lambda: None) is None


def ls_remote(stdout="", code=0, raises=None):
    calls = []

    def fake(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")

    return fake, calls


def test_main_head_asks_github_for_main_with_a_short_timeout():
    fake, calls = ls_remote(f"{SHA}\trefs/heads/main\n")
    assert main_head(run=fake) == SHA
    [(cmd, kwargs)] = calls
    assert cmd == ["git", "ls-remote", "https://github.com/kareblak/ticketmaster.git", "refs/heads/main"]
    assert kwargs["timeout"] <= 10
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("fake", [
    ls_remote(code=128)[0],  # offline, or GitHub unreachable
    ls_remote("")[0],  # no main branch
    ls_remote(raises=subprocess.TimeoutExpired(["git"], 5))[0],
    ls_remote(raises=FileNotFoundError("git"))[0],  # git not installed
])
def test_main_head_is_none_when_github_cannot_say(fake):
    assert main_head(run=fake) is None
