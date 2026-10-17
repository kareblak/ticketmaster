"""Plain: any terminal, no multiplexer. The board opens in a new window of the user's terminal (`terminal`), and
session status comes from `claude agents --json`, matched to the repo's ticket worktrees by cwd.

Typed at a shell, the board runs in place. Run by Claude, whose Bash tool has no TTY, it opens in a new window: one
board per Claude session (`CLAUDE_CODE_SESSION_ID`, else per repo), kept in a pidfile under the git common dir, so
a re-run replaces the old board.

`s` opens Claude in a new window, in the background, in the card's worktree: a new one under the repo's
`.claude/worktrees/`, or any maker's existing one, continued. Enter never opens Claude: a live session is reported,
since the board cannot raise another terminal's window, and a worktree without one gets a shell window. Cleanup
removes git worktrees and branches only: a live Claude session in a worktree keeps it, and nothing is closed.
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
from ..base import BackendError, CleanupView, Occupant, Released, SessionsUnreadable
from .terminal import OVERRIDE_HINT, LaunchFailed, Launcher, Runner, Terminal, exited, paste_line

NAME = "plain"
SOURCE = "claude agents"  # where session status comes from, and errors about it on the status line
CLAUDE_ENV = "CLAUDECODE"  # set in Claude's shell; without it `open` was typed at a shell, so the board runs there
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
SLOT_ENV = "TICKETMASTER_SLOT"  # set on the board a window runs: whose board it is, for its pidfile
WRAPPER_ENV = "TICKETMASTER_WRAPPER"  # the plugin's bin/ticketmaster, exported by the wrapper itself
DATA_ENV = "CLAUDE_PLUGIN_DATA"  # the wrapper's venv lives here; exported with WRAPPER_ENV
# `claude agents --json` statuses, as the board names them; anything else is `unknown`, never `done`.
STATUSES = {"busy": "working", "waiting": "blocked", "idle": "idle"}
WORKTREES = Path(".claude") / "worktrees"  # a card's new worktree, under the repo; kept out of git status
STARTING_SECONDS = 30  # how long a window this board opened counts as a session starting


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
    ever_read: bool = False  # `claude agents` has answered this run
    launched: dict[str, float] = field(default_factory=dict)  # worktree name: when this board opened Claude there

    # --- the board ---------------------------------------------------------------------------------------------

    def open_board(self, cwd: Path, args: list[str]) -> str | None:
        """None when the board runs here: typed at a shell, or the board a window was opened for, which records
        itself in its pidfile. Run by Claude, a new window; once it opened, this Claude session's previous board
        stops, so a failed launch leaves it running."""
        if slot := self.env.get(SLOT_ENV):
            self.record(cwd, slot)
            return None
        if not self.env.get(CLAUDE_ENV):
            return None
        slot = re.sub(r"[^A-Za-z0-9_.-]", "_", self.env.get(SESSION_ENV) or "repo")
        previous = self.live_board(cwd, slot)
        try:
            terminal = self.launcher().open(str(cwd), self.board_command(args, slot), front=True)
        except LaunchFailed as exc:
            paste = paste_line(str(cwd), self.board_command(args, slot, window=False))  # the user's shell has a PATH
            raise BackendError(f"{exc}; run it yourself:\n  {paste}\n{OVERRIDE_HINT}", source=NAME) from None
        replaced = previous is not None and self.stop(previous)
        return f"Board opened in a new {terminal.name} window" + (" (replaced the previous one)" if replaced else "")

    def launcher(self) -> Launcher:
        return Launcher(self.runner, self.env, self.system, self.which, self.configured)

    def board_command(self, args: list[str], slot: str, window: bool = True) -> list[str]:
        """`open` again, in the new window, by absolute path: the window has neither the Bash tool's PATH nor its
        env (iTerm2 and Ghostty run it without a login shell), so PATH, the slot and the wrapper's data dir go along."""
        settings = [f"PATH={path}"] if window and (path := self.env.get("PATH")) else []
        settings.append(f"{SLOT_ENV}={slot}")
        if wrapper := self.env.get(WRAPPER_ENV):
            settings.append(f"{DATA_ENV}={self.env[DATA_ENV]}")
            program = [wrapper]
        else:
            program = [sys.executable, "-m", "ticketmaster"]
        return ["/usr/bin/env", *settings, *program, "open", *args]

    def common_dir(self, cwd: Path) -> Path | None:
        """The repo's git common dir, shared by all its worktrees; None when git cannot tell."""
        try:
            done = self.runner(["git", "rev-parse", "--git-common-dir"], cwd=str(cwd), timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0 or not done.stdout.strip():
            return None
        return Path(os.path.normpath(cwd / done.stdout.strip()))

    def pidfile(self, cwd: Path, slot: str) -> Path | None:
        common = self.common_dir(cwd)
        return None if common is None else common / "ticketmaster" / f"board-{slot}.pid"

    def record(self, cwd: Path, slot: str) -> None:
        if (path := self.pidfile(cwd, slot)) is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{self.pid}\n")

        def forget() -> None:
            if read_pid(path) == self.pid:
                path.unlink(missing_ok=True)

        self.at_exit(forget)

    def live_board(self, cwd: Path, slot: str) -> int | None:
        """The PID of the slot's board, if its pidfile names a live ticketmaster."""
        if (path := self.pidfile(cwd, slot)) is None or (pid := read_pid(path)) is None or not self.is_board(pid):
            return None
        return pid

    def stop(self, pid: int) -> bool:
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
            shown = None if self.ever_read else {
                n: [Session(n, w["path"], w.get("branch"), "unavailable")] for n, w in ticket_worktrees(checkouts)}
            raise SessionsUnreadable(str(exc), SOURCE, shown) from None
        self.ever_read = True
        found: dict[int, list[Session]] = {}
        busy: set[str] = set()
        for agent in agents:
            checkout = checkout_of(agent.get("cwd"), checkouts)
            if checkout is None or not checkout["is_linked_worktree"] or (n := ticket_number(checkout)) is None:
                continue
            busy.add(checkout["path"])
            status = status_of(agent)
            reason = agent.get("waitingFor") if status == "blocked" else None
            found.setdefault(n, []).append(Session(n, checkout["path"], checkout.get("branch"), status,
                                                   agent.get("pid") or agent.get("id"), reason))
        for n, w in ticket_worktrees(checkouts):
            if w["path"] not in busy:
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
            raise BackendError(exited("claude agents", done).removeprefix("claude agents "), SOURCE)
        try:
            listed = json.loads(done.stdout)
        except ValueError:
            listed = None
        if not isinstance(listed, list) or not all(isinstance(a, dict) for a in listed):
            raise BackendError("unparseable output", SOURCE)
        return listed

    # --- starting and going to a card ---------------------------------------------------------------------------

    def start_in_worktree(self, cwd: Path, name: str, branch: str, label: str, claude_args: list[str],
                          continue_args: list[str]) -> str:
        """Claude in a new window, in the background, in the ticket's own worktree, never a `-2` copy: an existing
        one of any maker (by its name or branch) continues with `continue_args`, else one is cut from HEAD on
        `branch` under `.claude/worktrees/` and gets `claude_args`. Refused when a session is in the worktree (read
        again just now), when this board opened one there in the last STARTING_SECONDS, when sessions cannot be
        read, or when `branch` is left over without a worktree. A window that cannot open leaves the line to paste.
        Returns where it went, for the status line."""
        if not (checkouts := self.worktrees(cwd)):
            raise BackendError(f"git can't list the worktrees of {cwd}", NAME)
        existing = next((w for w in checkouts if w["is_linked_worktree"] and (
            Path(w["path"]).name in (name, f"worktree-{name}") or w.get("branch") == branch)), None)
        try:
            agents = self.agents()
        except BackendError as exc:
            raise BackendError(f"can't check for a live session: {exc}", SOURCE) from None
        if existing is not None and any(checkout_of(a.get("cwd"), checkouts) is existing for a in agents):
            raise BackendError("live session: press Enter", NAME)
        if (opened := self.launched.get(name)) is not None and self.clock() - opened < STARTING_SECONDS:
            raise BackendError(f"a session is starting in {name}: wait for it, then press Enter", NAME)
        if existing is not None:
            path, args, what = existing["path"], continue_args, f"worktree {Path(existing['path']).name} with claude --continue"
        else:
            path, args, what = self.add_worktree(cwd, Path(checkouts[0]["path"]), name, branch), claude_args, \
                f"new worktree {name}"
        terminal = self.open_window(path, [self.which("claude") or "claude", *args])
        self.launched[name] = self.clock()
        return f"{what} in a new {terminal.name} window"

    def add_worktree(self, cwd: Path, repo: Path, name: str, branch: str) -> str:
        """A new worktree at `<repo>/.claude/worktrees/<name>` (`repo`: the main checkout) on a new `branch` from
        HEAD, with the worktrees kept out of git status first; its path. A leftover `branch` is not taken over."""
        if (common := self.common_dir(cwd)) is None:
            raise BackendError(f"{cwd} is not in a git repo", NAME)
        if self.git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
            raise BackendError(f"branch {branch} exists without a worktree; check it out in one or delete it, then s again",
                               NAME)
        self.exclude_worktrees(repo, common)
        path = str(repo / WORKTREES / name)
        done = self.git(repo, "worktree", "add", "-b", branch, path, "HEAD")
        if done.returncode != 0:
            raise BackendError(exited("git worktree add", done), NAME)
        return path

    def exclude_worktrees(self, repo: Path, common: Path) -> None:
        """Appends `.claude/worktrees/` to the repo's `.git/info/exclude` unless git already ignores it; never
        committed."""
        if self.git(repo, "check-ignore", "-q", f"{WORKTREES}/").returncode != 1:
            return  # ignored already, or git cannot tell
        exclude = common / "info" / "exclude"
        try:
            text = exclude.read_text() if exclude.exists() else ""
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(text + ("" if not text or text.endswith("\n") else "\n") + f"{WORKTREES}/\n")
        except OSError as exc:
            raise BackendError(f"couldn't write {exclude}: {exc.strerror or exc}", NAME) from None

    def git(self, repo: Path, *args: str) -> subprocess.CompletedProcess:
        try:
            return self.runner(["git", *args], cwd=str(repo), timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"git {args[0]}: {getattr(exc, 'strerror', None) or exc}", NAME) from None

    def go_to(self, cwd: Path, session: Session, label: str) -> str:
        """A live session is reported with its worktree, since the board cannot raise another terminal's window; a
        worktree without one gets a shell window. Going there never starts or resumes Claude."""
        if session.status == "unavailable":
            return f"can't tell whether a session runs in {session.path}"
        if session.status is not None:
            return f"running in {session.path}"
        terminal = self.open_window(session.path, [self.env.get("SHELL") or "/bin/sh"])
        return f"opened {Path(session.path).name} with a shell in a new {terminal.name} window"

    def open_window(self, path: str, command: list[str]) -> Terminal:
        """A card's window, in the background: only the board's own window comes forward."""
        try:
            return self.launcher().open(path, command, front=False)
        except LaunchFailed as exc:
            raise BackendError(f"{exc}; run it yourself: {exc.paste}", NAME) from None

    # --- cleanup ----------------------------------------------------------------------------------------------

    def cleanup_view(self, cwd: Path) -> CleanupView:
        """Per linked worktree, its `claude agents` sessions by status. One with a process (a pid) is live and keeps
        the worktree, unless it is this Claude session (CLAUDE_CODE_SESSION_ID): cleanup's own, whose worktree goes
        last. When `claude agents` fails, or git cannot list the worktrees to match sessions to, every worktree is
        kept as unreadable."""
        unreadable = CleanupView({}, unreadable="sessions unreadable")
        if not (checkouts := self.worktrees(cwd)):
            return unreadable
        try:
            agents = self.agents()
        except BackendError:
            return unreadable
        own = self.env.get(SESSION_ENV)
        occupants: dict[str, list[Occupant]] = {}
        for agent in agents:
            checkout = checkout_of(agent.get("cwd"), checkouts)
            if checkout is None or not checkout["is_linked_worktree"]:
                continue
            mine = bool(own) and agent.get("sessionId") == own
            live = agent.get("pid") is not None and not mine
            occupants.setdefault(checkout["path"], []).append(Occupant(
                "session", status_of(agent), own=mine,
                blocks="live session" if live else None))
        return CleanupView({path: tuple(found) for path, found in occupants.items()})

    def release(self, cwd: Path, path: str) -> Released:
        """Nothing to close: a session in the worktree kept it, and the windows this backend opens are the user's."""
        return Released()


def status_of(agent: dict) -> str:
    """An agent's `claude agents` status as the board names it; anything else is `unknown`, never `done`."""
    return STATUSES.get(agent.get("status"), "unknown")


def ticket_worktrees(checkouts: list[dict]) -> list[tuple[int, dict]]:
    """The linked worktrees named after a ticket, with its number."""
    return [(n, w) for w in checkouts if w["is_linked_worktree"] and (n := ticket_number(w)) is not None]


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None
