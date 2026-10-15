"""What the board's keys do in herdr: start or continue a ticket's /wayfinder or /implement session in its own
worktree, or go to its session."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .herdr import Herdr, Session
from .model import Ticket

DEFAULT_WAYFINDER_COMMAND = "/mattpocock-skills:wayfinder"
DEFAULT_IMPLEMENT_COMMAND = "/mattpocock-skills:implement"
DEFAULT_TO_TICKETS_COMMAND = "/mattpocock-skills:to-tickets"


class MapNotFound(ValueError):
    """A `wayfinder:<type>` ticket belongs to wayfinder, but the board could not find its map."""


class NotWayfinderWork(ValueError):
    """A ticket with no map: the board starts /wayfinder or /implement only on a map, so it does not start this one."""


def build_prompt(ticket: Ticket, wayfinder_command: str = DEFAULT_WAYFINDER_COMMAND,
                 implement_command: str = DEFAULT_IMPLEMENT_COMMAND,
                 to_tickets_command: str = DEFAULT_TO_TICKETS_COMMAND) -> str:
    """Exactly `<wayfinder_command> <map-url> [<ticket-url>]`: wayfinder claims and resolves map tickets itself.
    The spec a map led to gets /to-tickets on the spec alone, and an implementation ticket /implement on the ticket
    alone. Without a map there is nothing to start: a typed wayfinder ticket raises MapNotFound, anything else
    NotWayfinderWork.
    """
    if ticket.spec_kind == "impl":
        return f"{implement_command} {ticket.url}"
    if ticket.spec_kind == "spec":
        return f"{to_tickets_command} {ticket.url}"
    if ticket.is_wayfinder_map:
        return f"{wayfinder_command} {ticket.url}"
    if ticket.is_wayfinder_ticket:
        return f"{wayfinder_command} {ticket.parent_url} {ticket.url}"
    if ticket.map_missing:
        raise MapNotFound(f"{ticket.ref} {ticket.map_warning}")
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


def takes_focus(ticket: Ticket) -> bool:
    """HITL tickets and maps pull the developer over; research and implementation run AFK and start unfocused."""
    return ticket.spec_kind != "impl" and ticket.wayfinder_type != "research"


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
    to_tickets_command: str = DEFAULT_TO_TICKETS_COMMAND
    claude: str = "claude"
    herdr: Herdr = field(default_factory=Herdr)

    def sessions(self) -> dict[int, list[Session]]:
        return self.herdr.sessions(self.cwd)

    def prompt(self, ticket: Ticket) -> str:
        return build_prompt(ticket, self.wayfinder_command, self.implement_command, self.to_tickets_command)

    def start(self, ticket: Ticket) -> str:
        """In the ticket's own worktree, named by its key: created with the prompt, or reopened with
        `claude --continue` when it exists without an agent; never a `-2` copy."""
        focus = takes_focus(ticket)
        where = self.herdr.start_in_worktree(
            self.cwd, worktree_name(ticket), branch_name(ticket), session_label(ticket),
            claude_args(ticket, self.prompt(ticket)), continue_args(ticket), self.claude, focus,
        )
        if ticket.spec_kind == "impl":
            return f"Started {self.implement_command} on {ticket.ref} in {where}, unfocused"
        if ticket.spec_kind == "spec":
            return f"Started {self.to_tickets_command} on {ticket.ref} in {where}"
        return f"Started {ticket.ref} in {where}" + ("" if focus else ", unfocused: research runs AFK")

    def go_to(self, ticket: Ticket, session: Session) -> str:
        return f"{ticket.ref}: " + self.herdr.go_to(self.cwd, session, session_label(ticket))


def workbench_for(cwd: Path, config: dict) -> Workbench:
    """`.ticketmaster.toml` can rename the wayfinder, implement and to-tickets skills the sessions start with."""
    return Workbench(cwd, wayfinder_command=config.get("wayfinder_command", DEFAULT_WAYFINDER_COMMAND),
                     implement_command=config.get("implement_command", DEFAULT_IMPLEMENT_COMMAND),
                     to_tickets_command=config.get("to_tickets_command", DEFAULT_TO_TICKETS_COMMAND))
