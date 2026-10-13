"""Herdr (herdr.dev) integration: the board and its sessions as herdr panes, tabs and workspaces.

Inside a herdr pane (HERDR_ENV=1) the board talks to the herdr CLI instead of the host terminal:
`open` puts the board in a pane labelled "ticketmaster" (reusing it when one is already open),
and a started ticket gets its own git worktree workspace (`herdr worktree create`) with Claude
started as a named herdr agent, so the sidebar shows whether it is working, blocked or done.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

BOARD_LABEL = "ticketmaster"
BOARD_RATIO = 0.7  # share the caller keeps; leaves the board ~1/3, i.e. single-column mode


class HerdrError(RuntimeError):
    pass


Runner = Callable[[list[str], float], subprocess.CompletedProcess]


def default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def available(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("HERDR_ENV") == "1" and shutil.which("herdr") is not None


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

    def board_pane(self) -> str | None:
        panes = self.call("pane", "list").get("panes", [])
        return next((p["pane_id"] for p in panes if p.get("label") == BOARD_LABEL), None)

    def open_board(self, cwd: Path, command: str) -> str:
        """Restarts the board in its existing pane, or splits one off the caller. Returns the pane id."""
        existing = self.board_pane()
        if existing:
            self.call("pane", "send-keys", existing, "q")  # Textual quits on q and drops to the shell
            for _ in range(20):
                if self.at_prompt(existing):
                    self.call("pane", "run", existing, f"cd {shlex.quote(str(cwd))} && {command}")
                    return existing
                time.sleep(0.1)
            self.call("pane", "close", existing)
        pane = self.call(
            "pane", "split", "--current", "--direction", "right", "--ratio", str(BOARD_RATIO),
            "--cwd", str(cwd), "--no-focus",
        )["pane"]["pane_id"]
        self.call("pane", "rename", pane, BOARD_LABEL)
        self.call("pane", "run", pane, command)
        return pane

    def at_prompt(self, pane: str) -> bool:
        info = self.call("pane", "process-info", "--pane", pane).get("process_info", {})
        return info.get("shell_pid") is not None and info.get("foreground_process_group_id") == info.get("shell_pid")

    # --- sessions -------------------------------------------------------------------------------

    def find_worktree(self, cwd: Path, name: str) -> dict | None:
        """A worktree for this ticket made earlier, by herdr (`issue-<n>`) or `claude -w` (`worktree-issue-<n>`)."""
        worktrees = self.call("worktree", "list", "--cwd", str(cwd)).get("worktrees", [])
        branches = {name, f"worktree-{name}"}
        return next(
            (w for w in worktrees if w.get("is_linked_worktree") and (w.get("branch") in branches or Path(w["path"]).name == name)),
            None,
        )

    def worktree_pane(self, cwd: Path, name: str, label: str) -> tuple[str, str]:
        """A shell pane in the ticket's worktree workspace, creating or opening it. Returns (pane, how)."""
        found = self.find_worktree(cwd, name)
        if found is None:
            result = self.call("worktree", "create", "--cwd", str(cwd), "--branch", name, "--label", label, "--no-focus")
            return result["root_pane"]["pane_id"], f"new worktree workspace {label}"
        if found.get("open_workspace_id"):
            workspace = found["open_workspace_id"]
            result = self.call("tab", "create", "--workspace", workspace, "--cwd", found["path"], "--label", "claude", "--no-focus")
            return result["root_pane"]["pane_id"], f"a new tab in the open workspace for {found['path']}"
        result = self.call("worktree", "open", "--cwd", str(cwd), "--path", found["path"], "--label", label, "--no-focus")
        return result["root_pane"]["pane_id"], f"reopened worktree workspace {label}"

    def placed_pane(self, cwd: Path, placement: str) -> str:
        if placement == "tab":
            return self.call("tab", "create", "--cwd", str(cwd), "--no-focus")["root_pane"]["pane_id"]
        direction = "down" if placement == "split down" else "right"
        return self.call("pane", "split", "--current", "--direction", direction, "--cwd", str(cwd), "--no-focus")["pane"]["pane_id"]

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
            if str(exc) != "agent_not_ready":
                raise
            return f" as agent {unique} (waiting at a startup prompt)"
        return f" as agent {unique}"

    # --- adopting older `claude -w` worktrees ---------------------------------------------------

    def adopt(self, cwd: Path, labels: Mapping[str, str]) -> list[str]:
        """Opens a workspace for every `.claude/worktrees/*` checkout that has none. Returns their labels."""
        opened = []
        for wt in self.call("worktree", "list", "--cwd", str(cwd)).get("worktrees", []):
            path = Path(wt["path"])
            if not wt.get("is_linked_worktree") or wt.get("open_workspace_id") or path.parent.name != "worktrees" or path.parent.parent.name != ".claude":
                continue
            label = labels.get(path.name, path.name)
            self.call("worktree", "open", "--cwd", str(cwd), "--path", str(path), "--label", label, "--no-focus")
            opened.append(label)
        return opened


def error_code(stderr: str) -> str | None:
    try:
        return json.loads(stderr)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
