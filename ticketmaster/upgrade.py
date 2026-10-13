"""`ticketmaster upgrade`: reinstall from the GitHub source with pipx, then refresh the skill with the new code.

Which commit an install runs comes from the PEP 610 `direct_url.json` pip records at install time: a git
install carries the commit it was built from, an editable install (a dev clone set up by ./install.sh) says so.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from . import __version__

SOURCE = "git+https://github.com/kareblak/ticketmaster.git"
READ_DIRECT_URL = "from importlib import metadata; print(metadata.distribution('ticketmaster').read_text('direct_url.json') or '')"


@dataclass(frozen=True)
class Install:
    commit: str | None  # the source commit of a git install
    editable_dir: Path | None  # the clone an editable install follows


def install_from(direct_url: str | None) -> Install:
    try:
        data = json.loads(direct_url) if direct_url else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if (data.get("dir_info") or {}).get("editable"):
        url = urlparse(data.get("url", ""))
        return Install(commit=None, editable_dir=Path(unquote(url.path)) if url.scheme == "file" else None)
    return Install(commit=(data.get("vcs_info") or {}).get("commit_id"), editable_dir=None)


def installed() -> Install:
    try:
        return install_from(metadata.distribution("ticketmaster").read_text("direct_url.json"))
    except metadata.PackageNotFoundError:
        return install_from(None)


def describe(install: Install) -> str:
    if install.editable_dir is not None:
        return f"dev install, editable from {install.editable_dir}"
    return install.commit[:12] if install.commit else "unknown commit"


def version_line() -> str:
    return f"ticketmaster {__version__} ({describe(installed())})"


def upgrade(
    install: Install,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    prefix: str = sys.prefix,
) -> int:
    if install.editable_dir is not None:
        print(f"ticketmaster is an editable install following the clone at {install.editable_dir}; "
              f"run `git pull` there instead.")
        return 0
    if which("pipx") is None:
        print("ticketmaster: upgrade needs pipx (brew install pipx, or python3 -m pip install --user pipx)",
              file=sys.stderr)
        return 1

    def step(cmd: list[str]) -> subprocess.CompletedProcess:
        return run(cmd, capture_output=True, text=True)

    found = step(["pipx", "environment", "--value", "PIPX_LOCAL_VENVS"])
    if found.returncode != 0 or not found.stdout.strip():
        print(f"ticketmaster: pipx did not say where its apps live; nothing changed.\n{found.stderr.strip()}",
              file=sys.stderr)
        return 1
    venv = Path(found.stdout.strip()) / "ticketmaster"
    # Reinstalling a copy that is not the pipx app would leave this one, still first on PATH, on the old code.
    if Path(prefix).resolve() != venv.resolve():
        print(f"ticketmaster: this ticketmaster runs from {prefix}, not the pipx app at {venv}; "
              f"upgrade it the way it was installed.", file=sys.stderr)
        return 1

    print(f"Reinstalling ticketmaster from {SOURCE} …")
    done = step(["pipx", "install", "--force", SOURCE])
    if done.returncode != 0:
        print(f"ticketmaster: reinstall failed; the skill is unchanged.\n{done.stderr.strip()}", file=sys.stderr)
        return 1
    # The skill must come from the new code, so run it in the venv pipx just rebuilt, not in this process.
    python = str(venv / "bin" / "python")
    done = step([python, "-m", "ticketmaster", "install-skill"])
    if done.returncode != 0:
        print(f"ticketmaster: reinstalled, but `ticketmaster install-skill` failed; run it again.\n"
              f"{done.stderr.strip()}", file=sys.stderr)
        return 1
    print(done.stdout.strip())
    # Read the new install's record from outside, so this works whatever version was installed.
    now = install_from(step([python, "-c", READ_DIRECT_URL]).stdout.strip() or None)
    print(f"Upgraded: now running {describe(now)}.")
    return 0
