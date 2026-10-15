"""`cleanup <map> --dry-run`: what a map leaves behind, and what of it could go. Nothing is changed.

A map leaves behind the worktrees of the map itself and of its lane tickets (decision tickets, specs and their
implementation tickets), wherever they are (herdr's `~/.herdr/worktrees/<repo>/` and Claude's `.claude/worktrees/`
are both the repo's git worktrees), the herdr workspaces and agents in them, a ticket branch left without a
worktree, and an agent titled with a lane ticket that runs elsewhere. Each is removable when its ticket is closed or
a PR closing it is merged, by the tracker, and it holds no uncommitted changes. Unpushed commits say nothing: a
squash merge whose remote branch was deleted leaves every local commit unpushed.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import herdr
from .herdr import Herdr, checkout_of, title_number
from .model import Ticket, lane_tickets
from .providers import Provider, Query
from .start import branch_name, worktree_name

# Whether a worktree holds uncommitted changes, untracked files included; None when git cannot tell.
Uncommitted = Callable[[str], "bool | None"]


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Leftover:
    """A worktree with its branch, herdr workspaces and agents; a branch alone (no path); or an agent outside the
    ticket's worktrees (the checkout it runs in)."""

    ticket: Ticket
    path: str | None
    branch: str | None
    workspaces: tuple[str, ...]
    agents: tuple[str, ...]  # each agent's status
    removable: bool
    reason: str


def has_uncommitted(path: str) -> bool | None:
    """`git status`, without refreshing the worktree's index, so looking writes nothing."""
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", path, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bool(result.stdout.strip()) if result.returncode == 0 else None


def verdict(ticket: Ticket, dirty: bool | None, outside: bool = False) -> tuple[bool, str]:
    """Removable, and why; or kept, with every reason."""
    kept = []
    if ticket.is_open and ticket.has_open_change_request:
        kept.append("unmerged PR")  # even beside a merged one: the ticket's work is not all in
    elif ticket.is_open and not ticket.has_merged_change_request:
        kept.append("open ticket")
    if dirty is None:
        kept.append("git status unreadable")
    elif dirty:
        kept.append("uncommitted changes")
    if outside:
        kept.append("agent outside a ticket worktree")
    if kept:
        return False, ", ".join(kept)
    return True, "ticket closed" if not ticket.is_open else "PR merged"


def leftover(ticket: Ticket, path: str | None, branch: str | None, workspaces: list[str | None],
             agents: tuple[str, ...], dirty: bool | None, outside: bool = False) -> Leftover:
    removable, reason = verdict(ticket, dirty, outside)
    return Leftover(ticket, path, branch, tuple(dict.fromkeys(w for w in workspaces if w)), agents, removable, reason)


def ticket_branches(ticket: Ticket) -> set[str]:
    """The branches a ticket's session works on: the tracker's, `issue-<n>`/`tm-<n>`, or `claude -w`'s."""
    return {branch_name(ticket), worktree_name(ticket), f"worktree-{worktree_name(ticket)}"}


# A worktree or branch named after its ticket, with any suffix: `issue-12`, `worktree-issue-12-fix-login`, `tm-12-2`.
TICKET_PREFIX = re.compile(r"^(?:worktree-)?(?:issue|tm)-(?P<number>\d+)(?:-|$)", re.IGNORECASE)


def worktree_ticket(wt: dict, family: dict[int, Ticket], by_branch: dict[str, Ticket]) -> Ticket | None:
    """The family ticket a linked worktree is named after, by its directory or branch, or whose branch it is on."""
    for name in (Path(wt["path"]).name, wt.get("branch") or ""):
        if (match := TICKET_PREFIX.match(name)) and (ticket := family.get(int(match["number"]))):
            return ticket
    return by_branch.get(wt.get("branch") or "")


def leftovers(cwd: Path, map_ticket: Ticket, tickets: list[Ticket], bench: Herdr,
              uncommitted: Uncommitted | None = None) -> list[Leftover]:
    """The map's and its lane tickets' worktrees in herdr's order with every agent running in them, then their
    branches checked out nowhere, then agents titled with one of them that run outside those worktrees. Only
    lists: herdr is asked, never told."""
    uncommitted = uncommitted or has_uncommitted
    family = {t.number: t for t in (map_ticket, *lane_tickets(map_ticket, tickets))}
    by_branch = {name: t for t in family.values() for name in ticket_branches(t)}
    checkouts = bench.call("worktree", "list", "--cwd", str(cwd)).get("worktrees", [])
    agents = bench.call("agent", "list").get("agents", [])
    running: dict[str, list[dict]] = {}  # agents by the checkout they run in
    for agent in agents:
        if (checkout := checkout_of(agent, checkouts)) is not None:
            running.setdefault(checkout["path"], []).append(agent)
    found: list[Leftover] = []
    for wt in checkouts:
        ticket = worktree_ticket(wt, family, by_branch) if wt.get("is_linked_worktree") else None
        if ticket is None:
            continue
        here = running.get(wt["path"], [])
        found.append(leftover(ticket, wt["path"], wt.get("branch"),
                              [wt.get("open_workspace_id"), *(a.get("workspace_id") for a in here)],
                              tuple(a.get("agent_status") or "unknown" for a in here), uncommitted(wt["path"])))
    checked_out = {wt.get("branch") for wt in checkouts}
    for branch in herdr.local_branches(cwd):
        if branch in by_branch and branch not in checked_out:
            found.append(leftover(by_branch[branch], None, branch, [], (), False))
    listed = {entry.path for entry in found}
    for path, here in running.items():
        for agent in here:
            if path not in listed and (ticket := family.get(title_number(agent))) is not None:
                found.append(leftover(ticket, path, None, [agent.get("workspace_id")],
                                      (agent.get("agent_status") or "unknown",), False, outside=True))
    return found


def render(map_ticket: Ticket, found: list[Leftover], home: Path | None = None) -> str:
    state = "open" if map_ticket.is_open else "closed"
    lines = [f"{map_ticket.ref} {map_ticket.title} ({state}): dry run, nothing changed"]
    if not found:
        return "\n".join([*lines, "Nothing left behind."])
    home = Path.home() if home is None else home
    for entry in found:
        parts = []
        if entry.path:
            path = Path(entry.path)
            parts.append(f"worktree {'~/' + str(path.relative_to(home)) if path.is_relative_to(home) else path}")
        if entry.branch:
            parts.append(f"branch {entry.branch}")
        if entry.workspaces:
            parts.append(f"workspace{'s' if len(entry.workspaces) > 1 else ''} {' '.join(entry.workspaces)}")
        if entry.agents:
            parts.append(f"agent{'s' if len(entry.agents) > 1 else ''} {', '.join(entry.agents)}")
        mark = "removable" if entry.removable else "kept"
        lines.append(f"{mark:<9}  {entry.ticket.ref}  {', '.join(parts)} · {entry.reason}")
    removable = sum(entry.removable for entry in found)
    lines.append(f"{removable} removable, {len(found) - removable} kept")
    return "\n".join(lines)


def dry_run(cwd: Path, provider: Provider, named: int | str, bench: Herdr,
            uncommitted: Uncommitted | None = None) -> str:
    """The named map's leftovers, open or closed; `named` is its number or tracker key (`TM-1`)."""
    from .cli import named_number

    tickets = provider.fetch(Query(named_map=named_number(named, provider)))
    map_ticket = next((t for t in tickets if t.is_wayfinder_map
                       and (t.key == named if isinstance(named, str) else t.number == named)), None)
    if map_ticket is None:
        ref = named if isinstance(named, str) else f"#{named}"
        raise CleanupError(f"{ref} is not a wayfinder map in {provider.name}:{provider.repo}")
    return render(map_ticket, leftovers(cwd, map_ticket, tickets, bench, uncommitted))
