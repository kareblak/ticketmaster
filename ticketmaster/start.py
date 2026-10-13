"""Start a Ready ticket in a new Claude Code session (tab or split pane) in its own git worktree."""

from __future__ import annotations

import os
import re
import shlex
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import herdr
from .auto_layout import GhosttyAutoLayout, LayoutError
from .launch import Launch, Placement, describe, open_beside, terminal_program
from .model import Ticket

DEFAULT_WAYFINDER_COMMAND = "/mattpocock-skills:wayfinder"


@dataclass(frozen=True)
class StartOptions:
    cwd: Path
    worktree: bool = True
    wayfinder_command: str = DEFAULT_WAYFINDER_COMMAND
    claude: str = "claude"
    # Shared across starts so the auto layout knows which panes it already placed.
    layout: GhosttyAutoLayout = field(default_factory=GhosttyAutoLayout, compare=False)
    layout_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)


def build_prompt(ticket: Ticket, wayfinder_command: str = DEFAULT_WAYFINDER_COMMAND) -> str:
    """Wayfinder claims and resolves map tickets itself; anything else gets a plain brief."""
    if ticket.is_wayfinder_map:
        return f"{wayfinder_command} {ticket.url}"
    if ticket.is_wayfinder_ticket:
        return f"{wayfinder_command} {ticket.parent_url} {ticket.url}"
    return (
        f"Work on issue #{ticket.number}: {ticket.title}\n{ticket.url}\n\n"
        "Read the issue and its comments first, then assign it to yourself before doing any work."
    )


def worktree_name(ticket: Ticket) -> str:
    return f"issue-{ticket.number}"


def session_label(ticket: Ticket) -> str:
    title = re.sub(r"\s+", " ", ticket.title)[:60]
    return f"#{ticket.number} {title}"


def claude_args(ticket: Ticket, options: StartOptions, worktree: bool | None = None) -> list[str]:
    """Arguments after the claude executable. `worktree=False` when the caller made the worktree."""
    args = ["-w", worktree_name(ticket)] if (options.worktree if worktree is None else worktree) else []
    return [*args, "-n", session_label(ticket), build_prompt(ticket, options.wayfinder_command)]


def session_command(ticket: Ticket, options: StartOptions) -> str:
    return shlex.join([options.claude, *claude_args(ticket, options)])


def start_session(ticket: Ticket, options: StartOptions, placement: Placement = Placement.TAB) -> str:
    """Opens the session as asked, falling back (split → tab → window). Returns a status line."""
    if herdr.available():
        try:
            return start_in_herdr(ticket, options, placement, herdr.Herdr())
        except herdr.HerdrError as exc:
            return f"herdr could not start #{ticket.number}: {exc}"
    command = session_command(ticket, options)
    if placement is Placement.AUTO and auto_layout_supported():
        try:
            with options.layout_lock:
                options.layout.open(options.cwd, shlex.join([login_shell(), "-lc", command]))
            return f"Started #{ticket.number} in {describe(Launch('Ghostty', Placement.AUTO, []))}"
        except LayoutError:
            pass  # fall back to a plain split, then tab, then window
    launch = open_beside(options.cwd, command, placement)
    if launch is None:
        return f"No terminal launcher worked; run: {command}"
    return f"Started #{ticket.number} in {describe(launch)}"


HERDR_PLACES = {
    Placement.TAB: "a new herdr tab",
    Placement.SPLIT_RIGHT: "a herdr pane to the right",
    Placement.SPLIT_DOWN: "a herdr pane below",
}


def start_in_herdr(ticket: Ticket, options: StartOptions, placement: Placement, client: herdr.Herdr) -> str:
    """A worktree workspace (the default; herdr makes the worktree, so claude runs without -w), or a
    herdr tab or split with `claude -w` as in any other terminal."""
    name = worktree_name(ticket)
    if placement not in HERDR_PLACES and options.worktree:
        pane, where = client.worktree_pane(options.cwd, name, session_label(ticket))
        note = client.start_claude(pane, name, options.claude, claude_args(ticket, options, worktree=False))
    else:
        placement = placement if placement in HERDR_PLACES else Placement.TAB
        pane, where = client.placed_pane(options.cwd, placement.value), HERDR_PLACES[placement]
        note = client.start_claude(pane, name, options.claude, claude_args(ticket, options))
    return f"Started #{ticket.number} in {where}{note}"


def auto_layout_supported() -> bool:
    return sys.platform == "darwin" and terminal_program(os.environ) == "ghostty" and not os.environ.get("TMUX")


def login_shell() -> str:
    return os.environ.get("SHELL") or "/bin/sh"
