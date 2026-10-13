"""Start a Ready ticket in a new Claude Code session, in a new tab and its own git worktree."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .launch import Placement, describe, open_beside
from .model import Ticket

DEFAULT_WAYFINDER_COMMAND = "/mattpocock-skills:wayfinder"


@dataclass(frozen=True)
class StartOptions:
    cwd: Path
    worktree: bool = True
    wayfinder_command: str = DEFAULT_WAYFINDER_COMMAND
    claude: str = "claude"


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


def start_session(ticket: Ticket, options: StartOptions) -> str:
    """Opens the session in a new tab (a window where tabs aren't supported). Returns a status line."""
    command = session_command(ticket, options)
    launch = open_beside(options.cwd, command, Placement.TAB)
    if launch is None:
        return f"No terminal launcher worked; run: {command}"
    return f"Started #{ticket.number} in {describe(launch)}"
