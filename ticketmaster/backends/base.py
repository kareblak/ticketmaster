"""The seam every session backend fills: what opens the board, finds, starts and goes to sessions, and lets go of a
worktree for cleanup. Nothing outside a backend calls the backend's own commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..session import Session


class BackendError(RuntimeError):
    """A backend refused or failed; `source` names it on the status line (`⚠ herdr: …`)."""

    source = "backend"

    def __init__(self, message: str, source: str | None = None):
        super().__init__(message)
        if source is not None:
            self.source = source


class SessionsUnreadable(BackendError):
    """The backend could not read its sessions. `sessions` is what the board shows meanwhile, or None to keep what it
    shows."""

    def __init__(self, message: str, source: str | None = None, sessions: dict[int, list[Session]] | None = None):
        super().__init__(message, source)
        self.sessions = sessions


@dataclass(frozen=True)
class Occupant:
    """Something a backend has in a worktree, as cleanup names and judges it."""

    kind: str  # what the summary groups it under: "workspace", "agent"
    name: str  # a workspace's id, an agent's state
    own: bool = False  # cleanup runs in it, so its worktree goes last
    blocks: str | None = None  # why its worktree is kept while it is there, e.g. "live session"
    ticket: int | None = None  # the ticket it names itself after, wherever it runs
    within: str | None = None  # the name of the occupant it runs in, listed with it


@dataclass(frozen=True)
class CleanupView:
    """Per checkout path, what the backend has there."""

    occupants: dict[str, tuple[Occupant, ...]]
    unreadable: str | None = None  # the backend could not look, so every worktree is kept for this reason


@dataclass(frozen=True)
class Released:
    """What `release` closed, what refused to close (each with why), and what it left because closing it would end
    cleanup itself: a second release of the same path closes that, once cleanup has nothing left to say."""

    closed: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    later: tuple[str, ...] = ()


class Backend(Protocol):
    name: str
    where: str  # where s puts a new session, for its confirm text: "in its own herdr workspace"

    def open_board(self, cwd: Path, args: list[str]) -> str | None:
        """Opens the board somewhere else and says where in one line; None: the board runs in place."""

    def sessions(self, cwd: Path) -> dict[int, list[Session]]:
        """The sessions working each ticket, most urgent first."""

    def start_in_worktree(self, cwd: Path, name: str, branch: str, label: str, claude_args: list[str],
                          continue_args: list[str]) -> str:
        """Claude in the ticket's own worktree, created or continued; where it went, for the status line."""

    def go_to(self, cwd: Path, session: Session, label: str) -> str:
        """Goes to a session, or its worktree; never starts or resumes Claude."""

    def cleanup_view(self, cwd: Path) -> CleanupView:
        """What the backend has in each of the repo's checkouts, for cleanup to judge and name."""

    def release(self, cwd: Path, path: str) -> Released:
        """Closes what the backend has in a worktree, before cleanup removes it."""
