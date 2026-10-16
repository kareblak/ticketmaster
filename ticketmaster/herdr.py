"""Herdr (herdr.dev): the board lives in a herdr pane, and every ticket session in a worktree workspace.

Starting a ticket creates its own git worktree (`herdr worktree create`) with Claude started in it as a named
herdr agent, or continues Claude in that worktree with `claude --continue` when it has no agent; never a `-2`
copy. The board finds every session working a ticket, including ones it did not start: a worktree named after
the ticket (`issue-<n>`/`tm-<n>`, from herdr or `claude -w`), or an agent in the repo whose terminal title
starts with the ticket's key. It shows each agent's state and jumps to it, and never removes or adopts a
worktree.
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
CLAUDE_ENV = "CLAUDECODE"  # set in Claude's shell; without it `open` was typed at a shell, so the board runs there
WRAPPER_ENV = "TICKETMASTER_WRAPPER"  # the plugin's bin/ticketmaster, exported by the wrapper itself
DATA_ENV = "CLAUDE_PLUGIN_DATA"  # the wrapper's venv lives here; exported with WRAPPER_ENV
BOARD_RATIO = 0.7  # share the caller keeps; leaves the board ~1/3, i.e. single-column mode
# `issue-12`, `tm-12` (the lowercased Linear key), `worktree-issue-12` (claude -w), `issue-12-2` (an older copy).
WORKTREE_NAME = re.compile(r"^(?:worktree-)?(?:issue|tm)-(?P<number>\d+)(?:-\d+)?$", re.IGNORECASE)
# A terminal title starting with the key, `TM-12 …` or `#12 …`, after any spinner glyph Claude puts first.
TITLE_KEY = re.compile(r"^[^\w#]*(?:#|tm-)(?P<number>\d+)(?!\w)", re.IGNORECASE)
# Which session Enter goes to first: needs you > working > idle, any agent before a worktree without one (None).
URGENCY = {"blocked": 0, "working": 1, "idle": 2, "done": 3, "unknown": 4, None: 9}


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
        return f"{DATA_ENV}={shlex.quote(env[DATA_ENV])} " + shlex.join([wrapper, "open", *args])
    return shlex.join([sys.executable, "-m", "ticketmaster", "open", *args])


@dataclass(frozen=True)
class Session:
    """An agent working a ticket, in the worktree it runs in; or a ticket's worktree with no agent (status None)."""

    number: int
    path: str
    branch: str | None = None
    workspace: str | None = None  # the agent's workspace, or the herdr workspace open on an agentless worktree
    status: str | None = None  # the agent's idle/working/blocked/done/unknown; None without one
    pane: str | None = None  # the agent's pane

    @property
    def urgency(self) -> tuple[int, bool]:
        """Sorts the one to go to first: needs you, working, idle, then a worktree without an agent."""
        return URGENCY.get(self.status, URGENCY["unknown"]), self.workspace is None


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


def title_number(agent: dict) -> int | None:
    """The ticket an agent's terminal title starts with (`TM-5 …`, `#5 …`), as `claude -n` sets it."""
    title = agent.get("terminal_title_stripped") or agent.get("terminal_title") or ""
    match = TITLE_KEY.match(title)
    return int(match["number"]) if match else None


def checkout_of(agent: dict, worktrees: list[dict]) -> dict | None:
    """The repo checkout the agent runs in, by its foreground cwd (else its pane's cwd); the deepest wins, since
    `claude -w` worktrees lie inside the main checkout. None: the agent is not in this repo."""
    where = agent.get("foreground_cwd") or agent.get("cwd")
    if not where:
        return None
    where = Path(where).resolve()
    inside = [w for w in worktrees if where.is_relative_to(Path(w["path"]).resolve())]
    return max(inside, key=lambda w: len(Path(w["path"]).parts), default=None)


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

    def board_pane(self, panes: list[dict], caller: str, own: bool) -> str | None:
        """The board in the caller's workspace, which may be the caller unless it is this process's own pane.
        One board per workspace: a board in another workspace belongs to other work, even when it shows this
        repo. A pane counts by its label only while it runs the board or the shell one left, never an agent:
        a Claude pane may carry the label too, and restarting there would send q to Claude."""
        workspace = next(pane_workspace(p) for p in panes if p["pane_id"] == caller)
        return next((
            p["pane_id"] for p in panes
            if p.get("label") == BOARD_LABEL and pane_workspace(p) == workspace and not p.get("agent")
            and not (own and p["pane_id"] == caller)
        ), None)

    def open_board(self, cwd: Path, command: str, caller: str | None = None, session: str | None = None) -> BoardOpened | None:
        """Restarts the board in the caller's workspace, or splits one off the caller. Returns where it
        went, or None when this process was launched by a board pane, or typed at a shell rather than run by
        Claude, which then runs the board in place. Run by Claude, it never returns None: its pane runs Claude."""
        if os.environ.get(BOARD_ENV):
            return None  # launched by a board pane; it may not be in `pane list` yet, so don't look it up
        if not os.environ.get(CLAUDE_ENV):
            return None  # typed at a shell: the board takes this pane, no split
        command = f"{BOARD_ENV}=1 {command}"
        panes = self.call("pane", "list").get("panes", [])
        caller, own = self.caller_pane(panes, cwd, caller, session)
        existing = self.board_pane(panes, caller, own)
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
        """The sessions working each ticket, most urgent first. An agent in the repo works a ticket when its
        worktree is named after it or its terminal title starts with its key; a ticket worktree without an
        agent is a session too, with no status. Agents outside the repo's checkouts are ignored."""
        checkouts = self.call("worktree", "list", "--cwd", str(cwd)).get("worktrees", [])
        found: dict[int, list[Session]] = {}
        busy: set[str] = set()  # worktrees with an agent in them
        for agent in self.call("agent", "list").get("agents", []):
            if (checkout := checkout_of(agent, checkouts)) is None:
                continue
            busy.add(checkout["path"])
            by_name = ticket_number(checkout) if checkout.get("is_linked_worktree") else None
            for number in sorted({n for n in (by_name, title_number(agent)) if n is not None}):
                found.setdefault(number, []).append(Session(
                    number, checkout["path"], checkout.get("branch"), agent.get("workspace_id"),
                    agent.get("agent_status") or "unknown", agent.get("pane_id"),
                ))
        for wt in checkouts:
            if wt.get("is_linked_worktree") and wt["path"] not in busy and (number := ticket_number(wt)) is not None:
                found.setdefault(number, []).append(Session(number, wt["path"], wt.get("branch"), wt.get("open_workspace_id")))
        return {number: sorted(sessions, key=lambda s: s.urgency) for number, sessions in found.items()}

    def start_in_worktree(self, cwd: Path, name: str, branch: str, label: str, claude_args: list[str],
                          continue_args: list[str], claude: str = "claude") -> str:
        """Claude in the ticket's own worktree, never a `-2` copy: an existing one (by its name or branch) is
        reopened with `continue_args`, else one is created on `branch` and gets `claude_args`. A worktree whose
        branch is not its name gets the name as its directory, so it is still found. A leftover `branch` without
        a worktree is not taken over. Never focuses: the developer stays on the board and goes over with Enter.
        Returns where it went, for the status line."""
        existing = next((w for w in self.worktrees(cwd)
                         if Path(w["path"]).name in (name, f"worktree-{name}") or w.get("branch") == branch), None)
        if existing is not None:
            workspace = existing.get("open_workspace_id")
            pane = self.idle_pane(workspace, existing["path"])
            if pane is None:
                pane = self.call("worktree", "open", "--cwd", str(cwd), "--path", existing["path"], "--label", label,
                                 "--no-focus")["root_pane"]["pane_id"]
            note = self.start_claude(pane, name, claude, continue_args)
            return f"worktree {Path(existing['path']).name} with claude --continue{note}"
        if branch in local_branches(cwd):
            raise HerdrError(f"branch {branch} exists without a worktree; check it out in one or delete it, then s again")
        place = [] if branch == name else ["--path", str(worktree_home(cwd) / name)]
        result = self.call("worktree", "create", "--cwd", str(cwd), "--branch", branch, *place, "--label", label,
                           "--no-focus")
        note = self.start_claude(result["root_pane"]["pane_id"], name, claude, claude_args)
        return f"new worktree {name}{note}"

    def idle_pane(self, workspace: str | None, path: str) -> str | None:
        """A pane of a worktree's open workspace at its shell prompt, else a new one split off its first pane, where
        Claude is started; None when it has none open (or herdr lists no pane there). An agent that started there
        since the board last looked is a live session, which gets no second Claude."""
        if workspace is None:
            return None
        if any(a.get("workspace_id") == workspace for a in self.call("agent", "list").get("agents", [])):
            raise HerdrError("it has a live session now: press Enter")
        panes = [p["pane_id"] for p in self.call("pane", "list").get("panes", []) if pane_workspace(p) == workspace]
        if not panes:
            return None
        if idle := next((p for p in panes if self.at_prompt(p)), None):
            return idle
        return self.call("pane", "split", "--pane", panes[0], "--direction", "right", "--cwd", path,
                         "--no-focus")["pane"]["pane_id"]

    def go_to(self, cwd: Path, session: Session, label: str) -> str:
        """Focuses a live session's agent; for a worktree without one, its herdr workspace, opened with a shell
        when none is open. Going there never starts or resumes Claude."""
        name = Path(session.path).name
        if session.status is not None and session.pane:
            self.call("agent", "focus", session.pane)
            return f"focused {name}"
        if session.workspace:
            self.call("workspace", "focus", session.workspace)
            return f"focused {name}" if session.status is not None else f"focused {name} (no Claude running)"
        self.call("worktree", "open", "--cwd", str(cwd), "--path", session.path, "--label", label, "--focus")
        return f"opened {name} with a shell"

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


def worktree_home(cwd: Path) -> Path:
    """Where herdr puts a repo's worktrees by default: `~/.herdr/worktrees/<repo>/`."""
    return Path.home() / ".herdr" / "worktrees" / repo_root(cwd).name


def local_branches(cwd: Path) -> list[str]:
    result = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=cwd, capture_output=True, text=True)
    return result.stdout.split() if result.returncode == 0 else []


def error_code(stderr: str) -> str | None:
    try:
        return json.loads(stderr)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
