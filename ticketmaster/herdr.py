"""Herdr (herdr.dev): the board lives in a herdr pane, and every ticket session in a worktree workspace.

Starting a ticket always creates a new git worktree (`herdr worktree create`) with Claude started in it
as a named herdr agent. A ticket's sessions are found again through its worktrees (`issue-<n>`, from
herdr or `claude -w`), so the board can show each agent's state, jump to it, and resume a dead one with
`claude --continue`. The board never removes or adopts a worktree.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

BOARD_LABEL = "ticketmaster"
BOARD_ENV = "TICKETMASTER_BOARD"  # set on the `open` a board pane runs, so it knows it is the board
WRAPPER_ENV = "TICKETMASTER_WRAPPER"  # the plugin's bin/ticketmaster, exported by the wrapper itself
BOARD_RATIO = 0.7  # share the caller keeps; leaves the board ~1/3, i.e. single-column mode
# `issue-12`, `worktree-issue-12` (claude -w), `issue-12-2` (a second start).
WORKTREE_NAME = re.compile(r"^(?:worktree-)?issue-(?P<number>\d+)(?:-\d+)?$")


class HerdrError(RuntimeError):
    pass


Runner = Callable[[list[str], float], subprocess.CompletedProcess]


def default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def available(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("HERDR_ENV") == "1" and shutil.which("herdr") is not None


def open_command(args: list[str], env: Mapping[str, str] | None = None) -> str:
    """`open` again, run in the board pane, where it finds itself and runs the board in place. Under the plugin
    it is the `bin/ticketmaster` wrapper by absolute path, with its data dir, since the pane has neither plugin
    `bin/` on PATH nor the plugin's env. Otherwise it re-runs this interpreter (a dev venv, `python -m`)."""
    env = os.environ if env is None else env
    if wrapper := env.get(WRAPPER_ENV):
        prefix = f"CLAUDE_PLUGIN_DATA={shlex.quote(env['CLAUDE_PLUGIN_DATA'])} " if env.get("CLAUDE_PLUGIN_DATA") else ""
        return prefix + shlex.join([wrapper, "open", *args])
    return shlex.join([sys.executable, "-m", "ticketmaster", "open", *args])


@dataclass(frozen=True)
class Session:
    """A worktree belonging to a ticket, with its herdr workspace and agent when they are open."""

    number: int
    path: str
    branch: str | None = None
    workspace: str | None = None
    status: str | None = None  # the agent's idle/working/blocked/done/unknown; None without one


@dataclass(frozen=True)
class BoardOpened:
    pane: str
    beside: str  # the caller pane it was split off, or whose workspace board was restarted
    restarted: bool


def pane_workspace(pane: dict) -> str:
    return pane.get("workspace_id") or pane["pane_id"].split(":")[0]


def belongs(pane: dict, cwd: Path, session: str | None) -> bool:
    """Whether a listed pane is the calling Claude session's: by herdr's agent session id when both sides
    have one, else by its working directory lying in the repo being opened."""
    agent_session = (pane.get("agent_session") or {}).get("value")
    if agent_session and session:
        return agent_session == session
    root = repo_root(cwd)
    return any(d and Path(d).resolve().is_relative_to(root) for d in (pane.get("cwd"), pane.get("foreground_cwd")))


def ticket_number(worktree: dict) -> int | None:
    for name in (Path(worktree["path"]).name, worktree.get("branch") or ""):
        if match := WORKTREE_NAME.match(name):
            return int(match["number"])
    return None


@dataclass
class Herdr:
    runner: Runner = default_runner

    def call(self, *args: str, timeout: float = 30) -> dict:
        """Runs `herdr <args>` and returns `.result`. Errors are JSON on stderr."""
        try:
            done = self.runner(["herdr", *args], timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HerdrError(f"herdr {args[0]} {args[1]}: {exc}") from exc
        if done.returncode != 0:
            raise HerdrError(error_code(done.stderr) or done.stderr.strip() or f"herdr exited {done.returncode}")
        return json.loads(done.stdout or "{}").get("result", {})

    # --- the board pane -------------------------------------------------------------------------

    def caller_pane(self, panes: list[dict], cwd: Path, env_pane: str | None, session: str | None) -> tuple[str, bool]:
        """The pane `open` was run from, and whether it is this process's own pane. The inherited
        HERDR_PANE_ID counts only when it is listed and belongs to this Claude session (or, without a
        session id from herdr, sits in this repo): a background session may carry another project's
        pane, or one that is gone. Otherwise the focused pane, where the user just typed the command."""
        listed = next((p for p in panes if p["pane_id"] == env_pane), None)
        if listed is not None and belongs(listed, cwd, session):
            return env_pane, True
        focused = next((p["pane_id"] for p in panes if p.get("focused")), None)
        if focused is None:
            raise HerdrError(f"pane {env_pane or '(unset)'} is not this session's and no pane is focused")
        return focused, False

    def board_pane(self, panes: list[dict], caller: str) -> str | None:
        """The board in the caller's workspace, which may be the caller itself. One board per workspace:
        a board in another workspace belongs to other work, even when it shows this repo."""
        workspace = next(pane_workspace(p) for p in panes if p["pane_id"] == caller)
        return next((p["pane_id"] for p in panes if p.get("label") == BOARD_LABEL and pane_workspace(p) == workspace), None)

    def open_board(self, cwd: Path, command: str, caller: str | None = None, session: str | None = None) -> BoardOpened | None:
        """Restarts the board in the caller's workspace, or splits one off the caller. Returns where it
        went, or None when this process runs in the board pane itself, which then runs the board in place."""
        if os.environ.get(BOARD_ENV):
            return None  # launched by a board pane; it may not be in `pane list` yet, so don't look it up
        command = f"{BOARD_ENV}=1 {command}"
        panes = self.call("pane", "list").get("panes", [])
        caller, own = self.caller_pane(panes, cwd, caller, session)
        existing = self.board_pane(panes, caller)
        if existing and existing == caller and own:
            return None  # restarting would send q to, and close, the caller's own pane
        if existing:
            if not self.at_prompt(existing):  # a q typed at the shell would prefix the command below
                self.call("pane", "send-keys", existing, "q")  # Textual quits on q and drops to the shell
            for _ in range(20):
                if self.at_prompt(existing):
                    self.call("pane", "run", existing, f"cd {shlex.quote(str(cwd))} && {command}")
                    return BoardOpened(existing, caller, restarted=True)
                time.sleep(0.1)
            self.call("pane", "close", existing)
        pane = self.call(
            "pane", "split", "--pane", caller, "--direction", "right", "--ratio", str(BOARD_RATIO),
            "--cwd", str(cwd), "--no-focus",
        )["pane"]["pane_id"]
        self.call("pane", "rename", pane, BOARD_LABEL)
        self.call("pane", "run", pane, command)
        return BoardOpened(pane, caller, restarted=False)

    def at_prompt(self, pane: str) -> bool:
        info = self.call("pane", "process-info", "--pane", pane).get("process_info", {})
        return info.get("shell_pid") is not None and info.get("foreground_process_group_id") == info.get("shell_pid")

    # --- ticket sessions ------------------------------------------------------------------------

    def worktrees(self, cwd: Path) -> list[dict]:
        return [w for w in self.call("worktree", "list", "--cwd", str(cwd)).get("worktrees", []) if w.get("is_linked_worktree")]

    def sessions(self, cwd: Path) -> dict[int, list[Session]]:
        """Every ticket worktree of the repo, by ticket number, with its agent's state."""
        agents = self.call("agent", "list").get("agents", [])
        rank = {"blocked": 0, "working": 1, "idle": 2, "done": 3, "unknown": 4}
        status_by_workspace: dict[str, str] = {}
        for agent in agents:
            workspace, status = agent.get("workspace_id"), agent.get("agent_status", "unknown")
            if workspace and rank.get(status, 9) < rank.get(status_by_workspace.get(workspace, ""), 9):
                status_by_workspace[workspace] = status  # the one needing attention wins
        found: dict[int, list[Session]] = {}
        for wt in self.worktrees(cwd):
            if (number := ticket_number(wt)) is None:
                continue
            workspace = wt.get("open_workspace_id")
            found.setdefault(number, []).append(Session(
                number, wt["path"], wt.get("branch"), workspace, status_by_workspace.get(workspace) if workspace else None,
            ))
        return found

    def free_branch(self, cwd: Path, name: str) -> str:
        """`name`, or `name-2`, `name-3`… when a worktree or local branch already has it."""
        worktrees = self.worktrees(cwd)
        taken = {w.get("branch") for w in worktrees} | {Path(w["path"]).name for w in worktrees} | set(local_branches(cwd))
        return next(n for n in [name, *(f"{name}-{i}" for i in range(2, 100))] if n not in taken and f"worktree-{n}" not in taken)

    def start_in_new_worktree(self, cwd: Path, name: str, label: str, claude_args: list[str], claude: str = "claude") -> str:
        """A fresh worktree workspace with Claude in it. Returns where it went, for the status line."""
        branch = self.free_branch(cwd, name)
        result = self.call("worktree", "create", "--cwd", str(cwd), "--branch", branch, "--label", label, "--no-focus")
        note = self.start_claude(result["root_pane"]["pane_id"], branch, claude, claude_args)
        return f"new worktree {branch}{note}"

    def go_to(self, cwd: Path, session: Session, label: str, claude: str = "claude") -> str:
        """Focuses the session's workspace, opening it and resuming Claude (`--continue`) when needed."""
        if session.workspace and session.status is not None:
            self.call("workspace", "focus", session.workspace)
            return f"focused {session.workspace}"
        if session.workspace:  # open, but Claude has exited: resume in a new tab
            pane = self.call("tab", "create", "--workspace", session.workspace, "--cwd", session.path, "--label", "claude", "--focus")["root_pane"]["pane_id"]
            self.call("workspace", "focus", session.workspace)
        else:
            pane = self.call("worktree", "open", "--cwd", str(cwd), "--path", session.path, "--label", label, "--focus")["root_pane"]["pane_id"]
        note = self.start_claude(pane, Path(session.path).name, claude, ["--continue"])
        return f"resumed {Path(session.path).name}{note}"

    def start_claude(self, pane: str, name: str, claude: str, claude_args: list[str]) -> str:
        """Starts Claude as the herdr agent `name`. Returns a short state note for the status line."""
        if Path(claude).name != "claude":
            self.call("pane", "run", pane, shlex.join([claude, *claude_args]))
            return ""
        taken = {a.get("name") for a in self.call("agent", "list").get("agents", [])}
        unique = next(n for n in [name, *(f"{name}-{i}" for i in range(2, 100))] if n not in taken)
        try:
            self.call("agent", "start", unique, "--kind", "claude", "--pane", pane, "--timeout", "60000", "--", *claude_args, timeout=90)
        except HerdrError as exc:
            if str(exc) == "agent_not_ready":
                return f" as agent {unique} (waiting at a startup prompt)"
            if str(exc) == "timeout":  # Claude went straight to work on the prompt, so herdr never saw it idle
                return f" as agent {unique} (still starting)"
            raise
        return f" as agent {unique}"


def repo_root(cwd: Path) -> Path:
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 and result.stdout.strip() else cwd.resolve()


def local_branches(cwd: Path) -> list[str]:
    result = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=cwd, capture_output=True, text=True)
    return result.stdout.split() if result.returncode == 0 else []


def error_code(stderr: str) -> str | None:
    try:
        return json.loads(stderr)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
