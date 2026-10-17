"""A fake session backend implementing the `Backend` Protocol: answers from what a test sets and records calls."""

from pathlib import Path

from ticketmaster.backends import BackendError, CleanupView, Released
from ticketmaster.git import ticket_number


class FakeBackend:
    """`current` is what `sessions` reports; `occupants` what `cleanup_view` reports per path. `calls` records
    ("start", number), ("go_to", number, path) and ("release", path)."""

    name = "fake"
    where = "in a fake place"

    def __init__(self, sessions=None, occupants=None, unreadable=None):
        self.calls = []
        self.current = sessions or {}
        self.occupants = occupants or {}
        self.unreadable = unreadable
        self.boards = []

    def open_board(self, cwd, args):
        self.boards.append(list(args))
        return None

    def sessions(self, cwd):
        return self.current

    def start_in_worktree(self, cwd, name, branch, label, claude_args, continue_args):
        self.calls.append(("start", ticket_number({"path": name})))
        return f"new worktree {name}"

    def go_to(self, cwd, session, label):
        self.calls.append(("go_to", session.number, session.path))
        return "ok"

    def cleanup_view(self, cwd):
        return CleanupView(self.occupants, self.unreadable)

    def release(self, cwd, path):
        self.calls.append(("release", path))
        return Released(tuple(f"{o.kind} {o.name}" for o in self.occupants.get(path, ())))


class FailingBackend(FakeBackend):
    def start_in_worktree(self, *args):
        super().start_in_worktree(*args)
        raise BackendError("agent_start_failed", source="fake")


class FlakySessions(FakeBackend):
    """Answers `sessions` from a queue: a dict, or an exception to raise."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)

    def sessions(self, cwd):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def workbench(backend, cwd=Path("/code/repo")):
    from ticketmaster.start import Workbench

    return Workbench(cwd, backend)
