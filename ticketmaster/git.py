"""What every backend and cleanup read from git: the repo, its branches and worktrees, and which worktree is a
ticket's."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# `issue-12`, `tm-12` (the lowercased Linear key), `worktree-issue-12` (claude -w), `issue-12-2` (an older copy).
WORKTREE_NAME = re.compile(r"^(?:worktree-)?(?:issue|tm)-(?P<number>\d+)(?:-\d+)?$", re.IGNORECASE)


def repo_root(cwd: Path) -> Path:
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 and result.stdout.strip() else cwd.resolve()


def local_branches(cwd: Path) -> list[str]:
    result = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=cwd, capture_output=True, text=True)
    return result.stdout.split() if result.returncode == 0 else []


def worktrees(cwd: Path) -> list[dict]:
    """`git worktree list --porcelain`: each checkout's `path`, `branch` (None when detached) and
    `is_linked_worktree`, the main checkout first. Empty when git cannot tell."""
    try:
        result = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=cwd, capture_output=True, text=True,
                                timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_worktrees(result.stdout) if result.returncode == 0 else []


def parse_worktrees(porcelain: str) -> list[dict]:
    found: list[dict] = []
    for line in porcelain.splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            found.append({"path": value, "branch": None, "is_linked_worktree": bool(found)})
        elif key == "branch" and found:
            found[-1]["branch"] = value.removeprefix("refs/heads/")
    return found


def ticket_number(worktree: dict) -> int | None:
    """The ticket a worktree is named after, by its directory or else its branch."""
    for name in (Path(worktree["path"]).name, worktree.get("branch") or ""):
        if match := WORKTREE_NAME.match(name):
            return int(match["number"])
    return None


def checkout_of(where: str | None, worktrees: list[dict]) -> dict | None:
    """The repo checkout a directory lies in; the deepest wins, since `claude -w` worktrees lie inside the main
    checkout. None: not in this repo."""
    if not where:
        return None
    where_path = Path(where).resolve()
    inside = [w for w in worktrees if where_path.is_relative_to(Path(w["path"]).resolve())]
    return max(inside, key=lambda w: len(Path(w["path"]).parts), default=None)


def has_uncommitted(path: str) -> bool | None:
    """`git status`, without refreshing the worktree's index, so looking writes nothing; None when git cannot tell."""
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", path, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bool(result.stdout.strip()) if result.returncode == 0 else None
