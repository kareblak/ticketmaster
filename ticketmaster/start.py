"""What the board's keys do in herdr: start a ticket in a new worktree, review it, go to its session, clean up."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .herdr import Herdr, Session, unsaved_work
from .model import Ticket

DEFAULT_WAYFINDER_COMMAND = "/mattpocock-skills:wayfinder"
DEFAULT_IMPLEMENT_COMMAND = "/mattpocock-skills:implement"


def build_prompt(
    ticket: Ticket,
    wayfinder_command: str = DEFAULT_WAYFINDER_COMMAND,
    implement_command: str = DEFAULT_IMPLEMENT_COMMAND,
) -> str:
    """Wayfinder claims and resolves map tickets itself; anything else runs the implement skill with a brief."""
    if ticket.is_wayfinder_map:
        return f"{wayfinder_command} {ticket.url}"
    if ticket.is_wayfinder_ticket:
        return f"{wayfinder_command} {ticket.parent_url} {ticket.url}"
    return (
        f"{implement_command} {ticket.url}\n\n"
        "1. Read the issue and its comments, then assign it to yourself before doing any work.\n"
        "2. Implement it via the skill (it drives /tdd and /code-review and commits).\n"
        "3. Push the branch and open a ready PR that closes the issue, with the body written via the pr skill."
    )


def review_prompt(ticket: Ticket) -> str:
    return (
        f"Review the open pull request for issue #{ticket.number}: {ticket.title}\n{ticket.url}\n\n"
        "Find the PR that closes this issue, check out its branch in this worktree, then review it "
        "against the issue. Report findings; do not push or comment without asking."
    )


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
    implement_command: str = DEFAULT_IMPLEMENT_COMMAND
    claude: str = "claude"
    herdr: Herdr = field(default_factory=Herdr)
    check_unsaved: Callable[[str], str | None] = unsaved_work  # why removing a checkout would lose work

    def sessions(self) -> dict[int, list[Session]]:
        return self.herdr.sessions(self.cwd)

    def start(self, ticket: Ticket) -> str:
        where = self.herdr.start_in_new_worktree(
            self.cwd, worktree_name(ticket), session_label(ticket),
            claude_args(ticket, build_prompt(ticket, self.wayfinder_command, self.implement_command)), self.claude,
        )
        return f"Started #{ticket.number} in {where}"

    def review(self, ticket: Ticket) -> str:
        where = self.herdr.start_in_new_worktree(
            self.cwd, f"review-{ticket.number}", f"review {session_label(ticket)}",
            claude_args(ticket, review_prompt(ticket)), self.claude,
        )
        return f"Reviewing #{ticket.number} in {where}"

    def go_to(self, ticket: Ticket, session: Session) -> str:
        return f"#{ticket.number}: " + self.herdr.go_to(self.cwd, session, session_label(ticket), self.claude)

    def remove(self, ticket: Ticket, sessions: list[Session]) -> str:
        for session in sessions:
            self.herdr.remove(self.cwd, session, session_label(ticket))
        return f"Removed {len(sessions)} worktree(s) of #{ticket.number}"

    def auto_remove(self, ticket: Ticket, sessions: list[Session]) -> str:
        """Removes the worktrees that hold no uncommitted or unpushed work; names the ones it keeps."""
        kept = {s.path: reason for s in sessions if (reason := self.check_unsaved(s.path))}
        safe = [s for s in sessions if s.path not in kept]
        if safe:
            self.remove(ticket, safe)
        parts = [f"auto-removed {', '.join(Path(s.path).name for s in safe)}"] if safe else []
        parts += [f"kept {Path(path).name} ({reason})" for path, reason in kept.items()]
        return f"#{ticket.number} done: " + "; ".join(parts)
