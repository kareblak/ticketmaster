"""The plugin's `bin/ticketmaster` wrapper, run against a fake `python3` that logs what it was asked to do."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from ticketmaster import __version__

WRAPPER = Path(__file__).parent.parent / "bin" / "ticketmaster"

FAKE_PYTHON3 = """#!/usr/bin/env bash
echo "python3 $*" >> "$FAKE_LOG"
case "$1" in
  -c) exit "${FAKE_TOO_OLD:-0}" ;;
  -m) mkdir -p "$3/bin" && cp "$FAKE_VENV_PYTHON" "$3/bin/python" ;;
esac
"""

FAKE_VENV_PYTHON = """#!/usr/bin/env bash
echo "venv $*" >> "$FAKE_LOG"
case "$2" in
  pip) exit "${FAKE_PIP_EXIT:-0}" ;;
  ticketmaster) echo "ran $* wrapper=$TICKETMASTER_WRAPPER data=$CLAUDE_PLUGIN_DATA" ;;
esac
"""


def executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Plugin:
    """A copy of the plugin (the wrapper plus a versioned package) and the fake Pythons it builds with."""

    def __init__(self, tmp: Path, version: str = "0.3.0", clone: bool = False):
        self.root = tmp / "plugin"
        self.data = tmp / "data"
        self.log = tmp / "log"
        self.wrapper = self.root / "bin" / "ticketmaster"
        self.wrapper.parent.mkdir(parents=True)
        shutil.copy(WRAPPER, self.wrapper)
        self.set_version(version)
        if clone:
            (self.root / ".git").mkdir()
        fakes = tmp / "fakes"
        executable(fakes / "python3", FAKE_PYTHON3)
        self.env = {
            **os.environ,
            "PATH": f"{fakes}:{os.environ['PATH']}",
            "CLAUDE_PLUGIN_DATA": str(self.data),
            "FAKE_LOG": str(self.log),
            "FAKE_VENV_PYTHON": str(executable(tmp / "venv-python", FAKE_VENV_PYTHON)),
        }

    def set_version(self, version: str) -> None:
        package = self.root / "ticketmaster"
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text(f'"""ticketmaster."""\n\n__version__ = "{version}"\n')

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.wrapper), *args], env={**self.env, **env}, capture_output=True, text=True)

    def calls(self) -> list[str]:
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return calls


def builds(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("python3 -m venv ") or " -m pip " in c]


def test_first_run_builds_the_venv_from_the_plugin_source_and_runs_the_cli(tmp_path):
    plugin = Plugin(tmp_path)
    done = plugin.run("open", "42")
    assert done.returncode == 0, done.stderr
    venv = plugin.data / "venv"
    assert builds(plugin.calls()) == [
        f"python3 -m venv {venv}",
        f"venv -m pip install --quiet --disable-pip-version-check {plugin.root}",
    ]
    assert done.stdout.startswith("ran -m ticketmaster open 42 ")


def test_the_next_run_reuses_the_venv(tmp_path):
    plugin = Plugin(tmp_path)
    plugin.run()
    plugin.calls()
    assert plugin.run("--version").returncode == 0
    assert plugin.calls() == ["venv -m ticketmaster --version"]


def test_a_new_version_rebuilds_the_venv(tmp_path):
    plugin = Plugin(tmp_path)
    plugin.run()
    plugin.calls()
    plugin.set_version("0.4.0")
    plugin.run()
    assert builds(plugin.calls())[0] == f"python3 -m venv {plugin.data / 'venv'}"
    plugin.run()
    assert builds(plugin.calls()) == []


def test_a_git_clone_is_installed_editable(tmp_path):
    plugin = Plugin(tmp_path, clone=True)
    plugin.run()
    assert f"venv -m pip install --quiet --disable-pip-version-check -e {plugin.root}" in plugin.calls()


def test_the_build_goes_to_stderr_so_stdout_is_only_the_clis(tmp_path):
    plugin = Plugin(tmp_path)
    done = plugin.run("open")
    assert done.stdout.count("\n") == 1
    assert "building" in done.stderr


def test_a_failed_pip_install_reports_a_venv_failure_and_retries_on_the_next_run(tmp_path):
    plugin = Plugin(tmp_path)
    done = plugin.run("open", FAKE_PIP_EXIT="1")
    assert done.returncode == 3
    assert "building the plugin venv" in done.stderr and "failed" in done.stderr
    plugin.calls()
    assert plugin.run("open").returncode == 0
    assert builds(plugin.calls())[0].startswith("python3 -m venv")


def test_a_python_older_than_3_11_is_refused_before_building(tmp_path):
    plugin = Plugin(tmp_path)
    done = plugin.run("open", FAKE_TOO_OLD="1")
    assert done.returncode == 3
    assert "3.11" in done.stderr
    assert builds(plugin.calls()) == []


@pytest.mark.parametrize("how", ["absolute", "relative", "path"])
def test_the_cli_learns_the_wrappers_absolute_path_and_data_dir(tmp_path, how):
    plugin = Plugin(tmp_path)
    if how == "path":
        argv, cwd, extra = ["ticketmaster"], tmp_path, {"PATH": f"{plugin.wrapper.parent}:{plugin.env['PATH']}"}
    else:
        argv = [str(plugin.wrapper) if how == "absolute" else "plugin/bin/ticketmaster"]
        cwd, extra = tmp_path, {}
    done = subprocess.run(argv, cwd=cwd, env={**plugin.env, **extra}, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert f"wrapper={plugin.wrapper} data={plugin.data}" in done.stdout


def test_the_plugin_manifest_carries_the_package_version():
    """`/plugin update` compares plugin.json's version, and the wrapper rebuilds on `__version__`: they move together."""
    manifest = json.loads((WRAPPER.parent.parent / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == __version__
