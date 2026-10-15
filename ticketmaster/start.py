"""What the board's keys do in herdr: start or continue a ticket's /wayfinder session in its worktree, or go to
its session."""

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
    """Exactly `<wayfinder_command> <map-url> [<ticket-url>]`: wayfinder claims and resolves a map's decision
    tickets itself. A typed wayfinder ticket without its map raises MapNotFound; a spec or impl card, or a
    ticket on no map, NotWayfinderWork.
    """
    if ticket.is_wayfinder_map:
        return f"{wayfinder_command} {ticket.url}"
    if ticket.is_wayfinder_ticket:
        return f"{wayfinder_command} {ticket.parent_url} {ticket.url}"
    if ticket.map_missing:
        raise MapNotFound(f"#{ticket.number} {ticket.map_warning}")
    if ticket.card_kind:
        raise NotWayfinderWork(ticket.not_startable)
    raise NotWayfinderWork(ticket.not_wayfinder_work)


def worktree_name(ticket: Ticket) -> str:
    return ticket.slug


def session_label(ticket: Ticket) -> str:
    title = re.sub(r"\s+", " ", ticket.title)[:60]
    return f"{ticket.key} {title}"


def takes_focus(ticket: Ticket) -> bool:
    """HITL tickets and maps pull the developer over; research runs AFK and starts unfocused."""
    return ticket.wayfinder_type != "research"


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

    def start(self, ticket: Ticket, worktree: Session | None = None) -> str:
        """Starts /wayfinder in a new worktree named by the ticket's key, or, given the ticket's worktree
        without an agent, continues Claude's last conversation there; never a second worktree."""
        label, focus = session_label(ticket), takes_focus(ticket)
        if worktree is None:
            args = claude_args(ticket, build_prompt(ticket, self.wayfinder_command))
            where = self.herdr.start_in_new_worktree(self.cwd, worktree_name(ticket), label, args, self.claude, focus)
            message = f"Started {ticket.key} in {where}"
        else:
            args = ["-n", label, "--continue"]
            where = self.herdr.continue_in_worktree(self.cwd, worktree, worktree_name(ticket), label, args,
                                                    self.claude, focus)
            message = f"Continued {ticket.key} in {where}"
        return message if focus else f"{message}, unfocused: research runs AFK"

    def go_to(self, ticket: Ticket, session: Session) -> str:
        return f"{ticket.key}: " + self.herdr.go_to(self.cwd, session, session_label(ticket))


def workbench_for(cwd: Path, config: dict) -> Workbench:
    """`.ticketmaster.toml` can rename the wayfinder skill the sessions start with."""
    return Workbench(cwd, wayfinder_command=config.get("wayfinder_command", DEFAULT_WAYFINDER_COMMAND))
