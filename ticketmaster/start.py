"""What the board's keys do in herdr: start a ticket's /wayfinder session in a new worktree, or go to its session."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .herdr import Herdr, Session
from .model import Ticket

DEFAULT_WAYFINDER_COMMAND = "/mattpocock-skills:wayfinder"


class MapNotFound(ValueError):
    """A `wayfinder:<type>` ticket belongs to wayfinder, but the board could not find its map."""


class NotWayfinderWork(ValueError):
    """A ticket with no map: the board only ever starts /wayfinder, so it does not start this one."""


def build_prompt(ticket: Ticket, wayfinder_command: str = DEFAULT_WAYFINDER_COMMAND) -> str:
    """Wayfinder claims and resolves map tickets itself, and drives a ticket that has a map (the spec a map
    led to, and its implementation tickets). Without a map there is nothing to start: a typed wayfinder
    ticket raises MapNotFound, anything else NotWayfinderWork.
    """
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
    return f"issue-{ticket.number}"


def session_label(ticket: Ticket) -> str:
    title = re.sub(r"\s+", " ", ticket.title)[:60]
    return f"#{ticket.number} {title}"


def claude_args(ticket: Ticket, prompt: str) -> list[str]:
    """One line per argument: `herdr agent start` rejects newlines (invalid_agent_argument)."""
    return ["-n", session_label(ticket), one_line(prompt)]


def one_line(text: str) -> str:
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


@dataclass
class Workbench:
    """The board's herdr actions for one repo. Every call blocks; the board runs them in a worker."""

    cwd: Path
    wayfinder_command: str = DEFAULT_WAYFINDER_COMMAND
    claude: str = "claude"
    herdr: Herdr = field(default_factory=Herdr)

    def sessions(self) -> dict[int, list[Session]]:
        return self.herdr.sessions(self.cwd)

    def start(self, ticket: Ticket) -> str:
        where = self.herdr.start_in_new_worktree(
            self.cwd, worktree_name(ticket), session_label(ticket),
            claude_args(ticket, build_prompt(ticket, self.wayfinder_command)), self.claude,
        )
        return f"Started #{ticket.number} in {where}"

    def go_to(self, ticket: Ticket, session: Session) -> str:
        return f"#{ticket.number}: " + self.herdr.go_to(self.cwd, session, session_label(ticket))


def workbench_for(cwd: Path, config: dict) -> Workbench:
    """`.ticketmaster.toml` can rename the wayfinder skill the sessions start with."""
    return Workbench(cwd, wayfinder_command=config.get("wayfinder_command", DEFAULT_WAYFINDER_COMMAND))
