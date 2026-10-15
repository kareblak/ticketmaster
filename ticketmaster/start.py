"""What the board's keys do in herdr: start a ticket's /wayfinder or /implement session, or go to its session."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .herdr import Herdr, Session
from .model import Ticket

DEFAULT_WAYFINDER_COMMAND = "/mattpocock-skills:wayfinder"
DEFAULT_IMPLEMENT_COMMAND = "/mattpocock-skills:implement"


class MapNotFound(ValueError):
    """A `wayfinder:<type>` ticket belongs to wayfinder, but the board could not find its map."""


class NotWayfinderWork(ValueError):
    """A ticket with no map: the board only ever starts /wayfinder, so it does not start this one."""


def build_prompt(ticket: Ticket, wayfinder_command: str = DEFAULT_WAYFINDER_COMMAND,
                 implement_command: str = DEFAULT_IMPLEMENT_COMMAND) -> str:
    """Wayfinder claims and resolves map tickets itself, and drives a ticket that has a map (the spec a map
    led to). An implementation ticket gets /implement on the ticket alone. Without a map there is nothing to
    start: a typed wayfinder ticket raises MapNotFound, anything else NotWayfinderWork.
    """
    if ticket.spec_kind == "impl":
        return f"{implement_command} {ticket.url}"
    if ticket.is_wayfinder_map:
        return f"{wayfinder_command} {ticket.url}"
    if ticket.is_wayfinder_ticket:
        return f"{wayfinder_command} {ticket.parent_url} {ticket.url}"
    if ticket.map_missing:
        raise MapNotFound(f"#{ticket.number} {ticket.map_warning}")
    if ticket.map_url:
        return f"{wayfinder_command} {ticket.map_url} {ticket.url}"
    raise NotWayfinderWork(ticket.not_wayfinder_work)


def worktree_name(ticket: Ticket) -> str:
    """`tm-<n>` for a Linear key, else `issue-<n>`."""
    return ticket.key.lower() if ticket.key else f"issue-{ticket.number}"


def branch_name(ticket: Ticket) -> str:
    """The branch the tracker wants (Linear's gitBranchName), else the worktree's name."""
    return ticket.branch_name or worktree_name(ticket)


def session_label(ticket: Ticket) -> str:
    title = re.sub(r"\s+", " ", ticket.title)[:60]
    return f"{ticket.ref} {title}"


def claude_args(ticket: Ticket, prompt: str) -> list[str]:
    """One line per argument: `herdr agent start` rejects newlines (invalid_agent_argument)."""
    return ["-n", session_label(ticket), one_line(prompt)]


def continue_args(ticket: Ticket) -> list[str]:
    """Reopens the worktree's last conversation; its first prompt is already in it."""
    return ["-n", session_label(ticket), "--continue"]


def one_line(text: str) -> str:
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


@dataclass
class Workbench:
    """The board's herdr actions for one repo. Every call blocks; the board runs them in a worker."""

    cwd: Path
    wayfinder_command: str = DEFAULT_WAYFINDER_COMMAND
    implement_command: str = DEFAULT_IMPLEMENT_COMMAND
    claude: str = "claude"
    herdr: Herdr = field(default_factory=Herdr)

    def sessions(self) -> dict[int, list[Session]]:
        return self.herdr.sessions(self.cwd)

    def prompt(self, ticket: Ticket) -> str:
        return build_prompt(ticket, self.wayfinder_command, self.implement_command)

    def start(self, ticket: Ticket) -> str:
        if ticket.spec_kind == "impl":
            return self.start_implement(ticket)
        where = self.herdr.start_in_new_worktree(
            self.cwd, worktree_name(ticket), session_label(ticket), claude_args(ticket, self.prompt(ticket)), self.claude,
        )
        return f"Started #{ticket.number} in {where}"

    def start_implement(self, ticket: Ticket) -> str:
        """AFK: in the ticket's own worktree, reopened with `claude --continue` when it has one, never focused."""
        where = self.herdr.start_in_worktree(
            self.cwd, worktree_name(ticket), branch_name(ticket), session_label(ticket),
            claude_args(ticket, self.prompt(ticket)), continue_args(ticket), self.claude,
        )
        return f"Started {self.implement_command} on {ticket.ref} in {where}, unfocused"

    def go_to(self, ticket: Ticket, session: Session) -> str:
        return f"#{ticket.number}: " + self.herdr.go_to(self.cwd, session, session_label(ticket))


def workbench_for(cwd: Path, config: dict) -> Workbench:
    """`.ticketmaster.toml` can rename the wayfinder and implement skills the sessions start with."""
    return Workbench(cwd, wayfinder_command=config.get("wayfinder_command", DEFAULT_WAYFINDER_COMMAND),
                     implement_command=config.get("implement_command", DEFAULT_IMPLEMENT_COMMAND))
