"""A session working a ticket, whichever backend found it: a Claude session in the ticket's worktree, or the worktree
alone when nothing runs there (status None)."""

from __future__ import annotations

from dataclasses import dataclass

# What the board knows of a session. `unknown`: live, in a state the source does not name. `unavailable`: the
# source has never answered, so whether anything runs there is unknown; it counts as live for s.
STATUSES = ("blocked", "working", "idle", "done", "unknown", "unavailable")
# Which session Enter goes to first: needs you > working > idle, any session before a worktree without one (None).
URGENCY = {"blocked": 0, "working": 1, "idle": 2, "done": 3, "unknown": 4, "unavailable": 5, None: 9}


@dataclass(frozen=True)
class Session:
    """A session working a ticket, in the worktree it runs in; or a ticket's worktree with none (status None).
    `handle` is the backend's own: the board only passes it back. `reason`: what a blocked session waits for, when
    the backend says."""

    number: int
    path: str
    branch: str | None = None
    status: str | None = None
    handle: object = None
    reason: str | None = None

    @property
    def urgency(self) -> tuple[int, bool]:
        """Sorts the one to go to first: needs you, working, idle, then a worktree without a session."""
        return URGENCY.get(self.status, URGENCY["unknown"]), self.handle is None
