"""Start a Ready ticket in a new Claude Code session (tab or split pane) in its own git worktree."""

from __future__ import annotations

import os
import re
import shlex
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

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


def session_command(ticket: Ticket, options: StartOptions) -> str:
    args = [options.claude]
    if options.worktree:
        args += ["-w", worktree_name(ticket)]
    title = re.sub(r"\s+", " ", ticket.title)[:60]
    args += ["-n", f"#{ticket.number} {title}", build_prompt(ticket, options.wayfinder_command)]
    return shlex.join(args)


def start_session(ticket: Ticket, options: StartOptions, placement: Placement = Placement.TAB) -> str:
    """Opens the session as asked, falling back (split → tab → window). Returns a status line."""
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


def auto_layout_supported() -> bool:
    return sys.platform == "darwin" and terminal_program(os.environ) == "ghostty" and not os.environ.get("TMUX")


def login_shell() -> str:
    return os.environ.get("SHELL") or "/bin/sh"
