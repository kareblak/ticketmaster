"""`cleanup <map>`: what a map leaves behind, and what of it could go; `--dry-run` only lists it.

A map leaves behind the worktrees of the map itself and of its lane tickets (decision tickets, specs and their
implementation tickets), wherever they are (herdr's `~/.herdr/worktrees/<repo>/` and Claude's `.claude/worktrees/`
are both the repo's git worktrees), what the session backend has in them (herdr's workspaces and agents), a ticket
branch left without a worktree, and an agent titled with a lane ticket that runs elsewhere. Each is removable when
its ticket is closed or a PR closing it is merged, by the tracker, and it holds no uncommitted changes and nothing
the backend keeps it for. Unpushed commits say nothing: a squash merge whose remote branch was deleted leaves every
local commit unpushed. The worktree cleanup runs in (its directory, or what the backend calls cleanup's own, such as
its herdr pane's workspace or agent) is the current one, judged like any other and removed last.

Without `--dry-run`, and only on a closed map, the backend lets go of each removable worktree (herdr closes its
workspace and agents), then the worktree is removed (`git worktree remove`, which refuses a worktree changed since),
then its branch is deleted with `git branch -d`, which refuses a branch git calls unmerged. Nothing is forced; kept
entries are left alone. This is the one command that changes anything: the board only reads. A removable current
worktree goes after every other entry, and what the backend has there that would end cleanup itself (its herdr
workspace or pane) is let go of only after the summary is printed.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import git as gitrepo
from .backends import Backend, CleanupView, Occupant
from .git import has_uncommitted
from .model import Ticket, lane_tickets
from .providers import Provider, Query
from .start import branch_name, worktree_name

# Whether a worktree holds uncommitted changes, untracked files included; None when git cannot tell.
Uncommitted = Callable[[str], "bool | None"]


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Leftover:
    """A worktree with its branch and what the backend has in it; a branch alone (no path); or an occupant outside
    the ticket's worktrees (the checkout it runs in)."""

    ticket: Ticket
    path: str | None
    branch: str | None
    occupants: tuple[Occupant, ...]
    removable: bool
    reason: str
    current: bool = False  # cleanup runs in it, so it goes last


@dataclass(frozen=True)
class Outcome:
    """What cleanup did with an entry: what it removed, and why anything was left."""

    entry: Leftover
    removed: tuple[str, ...]
    note: str  # the kept reason, or what of a removed entry was left; "" when all of it went
    failed: bool = False  # git or the backend refused a step; a branch git calls unmerged is no failure
    last: bool = False  # the backend left what would end cleanup in it, for a release after the summary


@dataclass(frozen=True)
class Cleaned:
    """The summary, whether every step went through, and the worktrees the backend lets go of only after the
    summary is printed: what cleanup runs in."""

    summary: str
    ok: bool
    last: tuple[str, ...]


def verdict(ticket: Ticket, dirty: bool | None, outside: bool = False, held: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Removable, and why; or kept, with every reason. `held`: what the backend keeps the worktree for."""
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
    kept += [reason for reason in dict.fromkeys(held) if reason]
    if kept:
        return False, ", ".join(kept)
    return True, "ticket closed" if not ticket.is_open else "PR merged"


def leftover(ticket: Ticket, path: str | None, branch: str | None, occupants: tuple[Occupant, ...],
             dirty: bool | None, outside: bool = False, current: bool = False,
             unreadable: str | None = None) -> Leftover:
    """`current`: cleanup runs in it. `unreadable`: the backend could not look in the worktree."""
    held = (*(o.blocks for o in occupants if o.blocks), *([unreadable] if unreadable and path else []))
    removable, reason = verdict(ticket, dirty, outside, held)
    return Leftover(ticket, path, branch, occupants, removable, reason, current)


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


def real(path: str) -> str:
    """A path as the filesystem resolves it, so git's and a backend's spelling of one worktree compare equal."""
    return os.path.realpath(path)


def strays(view: CleanupView, listed: set[str], family: dict[int, Ticket]) -> list[tuple[Ticket, str, tuple[Occupant, ...]]]:
    """Occupants naming a family ticket in a checkout that is not one of its worktrees (`listed`, resolved), each with
    what it runs in."""
    found = []
    for path, occupants in view.occupants.items():
        if real(path) in listed:
            continue
        for occupant in occupants:
            if (ticket := family.get(occupant.ticket)) is not None:
                within = [o for o in occupants if o is not occupant and o.name == occupant.within][:1]
                found.append((ticket, path, (*within, occupant)))
    return found


def leftovers(cwd: Path, map_ticket: Ticket, tickets: list[Ticket], backend: Backend,
              uncommitted: Uncommitted | None = None) -> list[Leftover]:
    """The map's and its lane tickets' worktrees in git's order with what the backend has in them, then their
    branches checked out nowhere, then occupants naming one of them that run outside those worktrees. Only lists:
    the backend is asked, never told. A worktree `cwd` lies in, or with an occupant of cleanup's own, is current."""
    uncommitted = uncommitted or has_uncommitted
    where = cwd.resolve()
    family = {t.number: t for t in (map_ticket, *lane_tickets(map_ticket, tickets))}
    by_branch = {name: t for t in family.values() for name in ticket_branches(t)}
    checkouts = gitrepo.worktrees(cwd)
    view = backend.cleanup_view(cwd)
    occupied = {real(path): occupants for path, occupants in view.occupants.items()}
    found: list[Leftover] = []
    for wt in checkouts:
        ticket = worktree_ticket(wt, family, by_branch) if wt.get("is_linked_worktree") else None
        if ticket is None:
            continue
        here = occupied.get(real(wt["path"]), ())
        current = where.is_relative_to(Path(wt["path"]).resolve()) or any(o.own for o in here)
        found.append(leftover(ticket, wt["path"], wt.get("branch"), here, uncommitted(wt["path"]),
                              current=current, unreadable=view.unreadable))
    checked_out = {wt.get("branch") for wt in checkouts}
    for branch in gitrepo.local_branches(cwd):
        if branch in by_branch and branch not in checked_out:
            found.append(leftover(by_branch[branch], None, branch, (), False))
    for ticket, path, occupants in strays(view, {real(entry.path) for entry in found if entry.path}, family):
        found.append(leftover(ticket, path, None, occupants, False, outside=True))
    return found


def shown(path: str, home: Path) -> str:
    return "~/" + str(Path(path).relative_to(home)) if Path(path).is_relative_to(home) else path


def parts(entry: Leftover, home: Path) -> list[str]:
    out = []
    if entry.path:
        out.append(f"worktree {shown(entry.path, home)}")
    if entry.branch:
        out.append(f"branch {entry.branch}")
    kinds: dict[str, list[str]] = {}
    for occupant in entry.occupants:
        kinds.setdefault(occupant.kind, []).append(occupant.name)
    for kind, names in kinds.items():
        joined = " ".join(names) if kind == "workspace" else ", ".join(names)  # ids side by side, states listed
        out.append(f"{kind}{'s' if len(names) > 1 else ''} {joined}")
    return out


def render(map_ticket: Ticket, found: list[Leftover], home: Path | None = None) -> str:
    state = "open" if map_ticket.is_open else "closed"
    lines = [f"{map_ticket.ref} {map_ticket.title} ({state}): dry run, nothing changed"]
    if not found:
        return "\n".join([*lines, "Nothing left behind."])
    home = Path.home() if home is None else home
    for entry in found:
        mark = "removable" if entry.removable else "kept"
        last = ", removed last: cleanup runs in it" if entry.removable and entry.current else ""
        lines.append(f"{mark:<9}  {entry.ticket.ref}  {', '.join(parts(entry, home))} · {entry.reason}{last}")
    removable = sum(entry.removable for entry in found)
    lines.append(f"{removable} removable, {len(found) - removable} kept")
    return "\n".join(lines)


def git(cwd: Path, *args: str) -> tuple[bool, str]:
    """Runs git in `cwd`; whether it succeeded, and its first line of complaint."""
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "LC_ALL": "C"})  # English, for "not fully merged"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    complaint = (result.stderr or result.stdout).strip().splitlines()
    return result.returncode == 0, complaint[0] if complaint else ""


def main_checkout(cwd: Path) -> Path:
    """The repo's main checkout, where git runs while worktrees go, the one cleanup runs in among them."""
    ok, common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common).parent if ok and common else cwd


def remove(cwd: Path, found: list[Leftover], backend: Backend, home: Path | None = None) -> list[Outcome]:
    """Removes each removable entry: the backend lets go of its worktree (herdr closes the workspace open on it and
    the panes of its agents in other workspaces), then its worktree goes, then its branch if git calls it merged. A
    worktree git refuses to remove keeps its branch. Kept entries are not touched. The current entries go last, and
    what the backend leaves because it would end cleanup itself is marked `last`, for a release after the summary; a
    current worktree git refuses to remove leaves it open. Outcomes come in the order entries were handled."""
    home = Path.home() if home is None else home
    checkout = main_checkout(cwd)
    ordered = [entry for entry in found if not entry.current] + [entry for entry in found if entry.current]
    return [remove_one(cwd, checkout, entry, backend, home) for entry in ordered]


def remove_one(cwd: Path, checkout: Path, entry: Leftover, backend: Backend, home: Path) -> Outcome:
    if not entry.removable:
        return Outcome(entry, (), entry.reason)
    removed: list[str] = []
    left: list[str] = []
    later: tuple[str, ...] = ()
    if entry.path:
        released = backend.release(cwd, entry.path)
        removed += released.closed
        left += released.refused
        later = released.later
    failed = bool(left)
    if entry.path:
        ok, complaint = git(checkout, "worktree", "remove", entry.path)
        if not ok:
            left.append(f"worktree kept: git worktree remove refused: {complaint}")
            return Outcome(entry, tuple(removed), ", ".join(left), failed=True)
        removed.insert(0, f"worktree {shown(entry.path, home)}")
    if entry.branch:
        ok, complaint = git(checkout, "branch", "-d", entry.branch)
        if ok:
            removed.append(f"branch {entry.branch}")
        elif "not fully merged" in complaint:
            left.append(f"branch {entry.branch} kept: not merged in git")
        else:
            left.append(f"branch {entry.branch} kept: {complaint}")
            failed = True
    if later:
        left.append(f"cleanup runs in it: {' and '.join(later)} close{'s' if len(later) == 1 else ''} after this summary")
    elif entry.current:
        left.append("cleanup ran in it")  # with nothing of its own to close: whatever runs there is left in a removed directory
    return Outcome(entry, tuple(removed), ", ".join(left), failed, bool(later))


def close_last(cwd: Path, cleaned: Cleaned, backend: Backend) -> list[str]:
    """Lets go of what cleanup runs in, once its summary is out; what the backend refused to close."""
    return [complaint for path in cleaned.last for complaint in backend.release(cwd, path).refused]


def render_outcomes(map_ticket: Ticket, outcomes: list[Outcome], home: Path | None = None) -> str:
    lines = [f"{map_ticket.ref} {map_ticket.title} (closed): cleaned up"]
    if not outcomes:
        return "\n".join([*lines, "Nothing left behind."])
    home = Path.home() if home is None else home
    for outcome in outcomes:
        what = outcome.removed or parts(outcome.entry, home)
        mark = "removed" if outcome.removed else "kept"
        note = f" · {outcome.note}" if outcome.note else ""
        lines.append(f"{mark:<7}  {outcome.entry.ticket.ref}  {', '.join(what)}{note}")
    removed = sum(bool(outcome.removed) for outcome in outcomes)
    lines.append(f"{removed} removed, {len(outcomes) - removed} kept")
    return "\n".join(lines)


def named_map(provider: Provider, named: int | str) -> tuple[Ticket, list[Ticket]]:
    """The named map, open or closed, and the tickets fetched with it; `named` is its number or key (`TM-1`)."""
    from .cli import named_number

    tickets = provider.fetch(Query(named_map=named_number(named, provider)))
    map_ticket = next((t for t in tickets if t.is_wayfinder_map
                       and (t.key == named if isinstance(named, str) else t.number == named)), None)
    if map_ticket is None:
        ref = named if isinstance(named, str) else f"#{named}"
        raise CleanupError(f"{ref} is not a wayfinder map in {provider.name}:{provider.repo}")
    return map_ticket, tickets


def dry_run(cwd: Path, provider: Provider, named: int | str, backend: Backend,
            uncommitted: Uncommitted | None = None) -> str:
    """The named map's leftovers, open or closed."""
    map_ticket, tickets = named_map(provider, named)
    return render(map_ticket, leftovers(cwd, map_ticket, tickets, backend, uncommitted))


def clean(cwd: Path, provider: Provider, named: int | str, backend: Backend,
          uncommitted: Uncommitted | None = None) -> Cleaned:
    """Removes the named closed map's removable leftovers, the current worktree last; the summary, whether every
    step went through (a branch git calls unmerged is no failure), and the worktrees `close_last` lets go of once
    the summary is printed. An open map is refused before the backend or git is touched: a human closes it in the
    tracker."""
    map_ticket, tickets = named_map(provider, named)
    if map_ticket.is_open:
        raise CleanupError(f"{map_ticket.ref} is still open: close the map in the tracker first; nothing changed")
    outcomes = remove(cwd, leftovers(cwd, map_ticket, tickets, backend, uncommitted), backend)
    return Cleaned(render_outcomes(map_ticket, outcomes), not any(outcome.failed for outcome in outcomes),
                   tuple(outcome.entry.path for outcome in outcomes if outcome.last and outcome.entry.path))
