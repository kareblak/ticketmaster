"""Plain: any terminal, no multiplexer. The board opens in a new window of the user's terminal (`terminal`), and
session status comes from `claude agents --json`, matched to the repo's ticket worktrees by cwd.

Typed at a shell, the board runs in place. Run by Claude, whose Bash tool has no TTY, it opens in a new window: one
board per Claude session (`CLAUDE_CODE_SESSION_ID`, else per repo), kept in a pidfile under the git common dir, so
a re-run replaces the old board. Starting a card, going to it and cleanup land later.
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ...git import checkout_of, parse_worktrees, ticket_number
from ...session import Session
from ..base import BackendError, CleanupView, Released, SessionsUnreadable
from .terminal import OVERRIDE_HINT, LaunchFailed, Launcher, Runner, paste_line

NAME = "plain"
SOURCE = "claude agents"  # where session status comes from, and errors about it on the status line
CLAUDE_ENV = "CLAUDECODE"  # set in Claude's shell; without it `open` was typed at a shell, so the board runs there
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
SLOT_ENV = "TICKETMASTER_SLOT"  # set on the board a window runs: whose board it is, for its pidfile
WRAPPER_ENV = "TICKETMASTER_WRAPPER"  # the plugin's bin/ticketmaster, exported by the wrapper itself
DATA_ENV = "CLAUDE_PLUGIN_DATA"  # the wrapper's venv lives here; exported with WRAPPER_ENV
# `claude agents --json` statuses, as the board names them; anything else is `unknown`, never `done`.
STATUSES = {"busy": "working", "waiting": "blocked", "idle": "idle"}
LATER = "lands in a later ticket"


def default_runner(argv: list[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None,
                   timeout: float = 30.0, detach: bool = False) -> subprocess.CompletedProcess:
    if detach:
        subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return subprocess.CompletedProcess(argv, 0, "", "")
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def unavailable(env: Mapping[str, str] | None = None) -> str | None:
    return None  # any terminal will do


def make(cwd: Path, config: dict, env: Mapping[str, str]) -> PlainBackend:
    return PlainBackend(env=env, configured=config.get("terminal"))


@dataclass
class PlainBackend:
    runner: Runner = default_runner
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    clock: Callable[[], float] = time.monotonic
    system: str = field(default_factory=platform.system)
    which: Callable[[str], str | None] = shutil.which
    kill: Callable[[int, int], None] = os.kill
    at_exit: Callable[[Callable[[], None]], object] = atexit.register
    pid: int = field(default_factory=os.getpid)
    configured: str | None = None  # `terminal` in .ticketmaster.toml
    name: str = NAME
    where: str = "in a new terminal window"
    answered: bool = False  # `claude agents` has answered this run

    # --- the board ---------------------------------------------------------------------------------------------

    def open_board(self, cwd: Path, args: list[str]) -> str | None:
        """None when the board runs here: typed at a shell, or the board a window was opened for, which records
        itself in its pidfile. Run by Claude, a new window, after stopping this Claude session's previous board."""
        if slot := self.env.get(SLOT_ENV):
            self.record(cwd, slot)
            return None
        if not self.env.get(CLAUDE_ENV):
            return None
        slot = re.sub(r"[^A-Za-z0-9_.-]", "_", self.env.get(SESSION_ENV) or "repo")
        command, launcher = self.board_command(args, slot), self.launcher()
        try:
            terminal = launcher.detect()
            replaced = self.stop_board(cwd, slot)
            launcher.launch(terminal, str(cwd), command, front=True)
        except LaunchFailed as exc:
            raise BackendError(f"{exc}; run it yourself:\n  {paste_line(str(cwd), command)}\n{OVERRIDE_HINT}",
                               source=NAME) from None
        return f"Board opened in a new {terminal.name} window" + (" (replaced the previous one)" if replaced else "")

    def launcher(self) -> Launcher:
        return Launcher(self.runner, self.env, self.system, self.which, self.configured)

    def board_command(self, args: list[str], slot: str) -> list[str]:
        """`open` again, in the new window, by absolute path: the window has neither the Bash tool's PATH nor its
        env, so the slot and the wrapper's data dir go along."""
        settings = [f"{SLOT_ENV}={slot}"]
        if wrapper := self.env.get(WRAPPER_ENV):
            settings.append(f"{DATA_ENV}={self.env[DATA_ENV]}")
            program = [wrapper]
        else:
            program = [sys.executable, "-m", "ticketmaster"]
        return ["/usr/bin/env", *settings, *program, "open", *args]

    def pidfile(self, cwd: Path, slot: str) -> Path | None:
        try:
            done = self.runner(["git", "rev-parse", "--git-common-dir"], cwd=str(cwd), timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0 or not done.stdout.strip():
            return None
        return (cwd / done.stdout.strip()) / "ticketmaster" / f"board-{slot}.pid"

    def record(self, cwd: Path, slot: str) -> None:
        if (path := self.pidfile(cwd, slot)) is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{self.pid}\n")

        def forget() -> None:
            if read_pid(path) == self.pid:
                path.unlink(missing_ok=True)

        self.at_exit(forget)

    def stop_board(self, cwd: Path, slot: str) -> bool:
        """SIGTERM to the slot's board, if its pidfile names a live ticketmaster; whether there was one."""
        if (path := self.pidfile(cwd, slot)) is None or (pid := read_pid(path)) is None or not self.is_board(pid):
            return False
        try:
            self.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True

    def is_board(self, pid: int) -> bool:
        try:
            self.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass  # alive, though not ours to signal; ps tells what it runs
        try:
            done = self.runner(["ps", "-p", str(pid), "-o", "command="], timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0 and "ticketmaster" in done.stdout

    # --- sessions ----------------------------------------------------------------------------------------------

    def sessions(self, cwd: Path) -> dict[int, list[Session]]:
        """The Claude sessions in each ticket worktree, most urgent first, and a worktree without one as a session
        with no status. When `claude agents` fails, SessionsUnreadable: before it ever answered, with every worktree
        `unavailable`; after, with nothing, so the board keeps the last good list."""
        checkouts = self.worktrees(cwd)
        try:
            agents = self.agents()
        except BackendError as exc:
            shown = None if self.answered else {
                n: [Session(n, w["path"], w.get("branch"), "unavailable")] for w in checkouts
                if w["is_linked_worktree"] and (n := ticket_number(w)) is not None}
            raise SessionsUnreadable(str(exc), SOURCE, shown) from None
        self.answered = True
        found: dict[int, list[Session]] = {}
        busy: set[str] = set()
        for agent in agents:
            checkout = checkout_of(agent.get("cwd"), checkouts)
            if checkout is None or not checkout["is_linked_worktree"] or (n := ticket_number(checkout)) is None:
                continue
            busy.add(checkout["path"])
            status = STATUSES.get(agent.get("status"), "unknown")
            reason = agent.get("waitingFor") if status == "blocked" else None
            found.setdefault(n, []).append(Session(n, checkout["path"], checkout.get("branch"), status,
                                                   agent.get("pid") or agent.get("id"), reason))
        for w in checkouts:
            if w["is_linked_worktree"] and w["path"] not in busy and (n := ticket_number(w)) is not None:
                found.setdefault(n, []).append(Session(n, w["path"], w.get("branch")))
        return {n: sorted(sessions, key=lambda s: s.urgency) for n, sessions in found.items()}

    def worktrees(self, cwd: Path) -> list[dict]:
        try:
            done = self.runner(["git", "worktree", "list", "--porcelain"], cwd=str(cwd), timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return []
        return parse_worktrees(done.stdout) if done.returncode == 0 else []

    def agents(self) -> list[dict]:
        """Every live Claude session, from `claude agents --json`; BackendError saying why not."""
        if (claude := self.which("claude")) is None:
            raise BackendError("claude not on PATH", SOURCE)
        try:
            done = self.runner([claude, "agents", "--json"], timeout=30)
        except subprocess.TimeoutExpired:
            raise BackendError("timed out", SOURCE) from None
        except OSError as exc:
            raise BackendError(str(exc.strerror or exc), SOURCE) from None
        if done.returncode != 0:
            error = (done.stderr.strip().splitlines() or [""])[-1]
            raise BackendError(f"exited {done.returncode}" + (f": {error}" if error else ""), SOURCE)
        try:
            listed = json.loads(done.stdout)
        except ValueError:
            listed = None
        if not isinstance(listed, list) or not all(isinstance(a, dict) for a in listed):
            raise BackendError("unparseable output", SOURCE)
        return listed

    # --- later tickets -----------------------------------------------------------------------------------------

    def start_in_worktree(self, cwd: Path, name: str, branch: str, label: str, claude_args: list[str],
                          continue_args: list[str]) -> str:
        raise BackendError(f"starting a card {LATER}", NAME)

    def go_to(self, cwd: Path, session: Session, label: str) -> str:
        raise BackendError(f"going to a card {LATER}", NAME)

    def cleanup_view(self, cwd: Path) -> CleanupView:
        raise BackendError(f"cleanup {LATER}", NAME)

    def release(self, cwd: Path, path: str) -> Released:
        raise BackendError(f"cleanup {LATER}", NAME)


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None
