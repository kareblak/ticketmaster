"""`cleanup <map>`: what a map leaves behind, and what of it could go; `--dry-run` only lists it.

A map leaves behind the worktrees of the map itself and of its lane tickets (decision tickets, specs and their
implementation tickets), wherever they are (herdr's `~/.herdr/worktrees/<repo>/` and Claude's `.claude/worktrees/`
are both the repo's git worktrees), the herdr workspaces and agents in them, a ticket branch left without a
worktree, and an agent titled with a lane ticket that runs elsewhere. Each is removable when its ticket is closed or
a PR closing it is merged, by the tracker, and it holds no uncommitted changes. Unpushed commits say nothing: a
squash merge whose remote branch was deleted leaves every local commit unpushed. Whatever cleanup runs in is kept.

Without `--dry-run`, and only on a closed map, each removable worktree is removed (`git worktree remove`, which
refuses a worktree changed since), then its herdr workspace and agents are closed, then its branch is deleted with
`git branch -d`, which refuses a branch git calls unmerged. Nothing is forced; kept entries are left alone. This is
the one command that changes anything: the board only reads.
"""

from __future__ import annotations

import os
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
    panes: tuple[tuple[str, str | None], ...] = ()  # each agent's pane and workspace
    opened: str | None = None  # the herdr workspace open on the worktree, closed with it


@dataclass(frozen=True)
class Outcome:
    """What cleanup did with an entry: what it removed, and why anything was left."""

    entry: Leftover
    removed: tuple[str, ...]
    note: str  # the kept reason, or what of a removed entry was left; "" when all of it went
    failed: bool = False  # git or herdr refused a step; a branch git calls unmerged is no failure


def has_uncommitted(path: str) -> bool | None:
    """`git status`, without refreshing the worktree's index, so looking writes nothing."""
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", path, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bool(result.stdout.strip()) if result.returncode == 0 else None


def verdict(ticket: Ticket, dirty: bool | None, outside: bool = False, here: bool = False) -> tuple[bool, str]:
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
    if here:
        kept.append("cleanup runs in it")
    if kept:
        return False, ", ".join(kept)
    return True, "ticket closed" if not ticket.is_open else "PR merged"


def leftover(ticket: Ticket, path: str | None, branch: str | None, workspaces: list[str | None],
             agents: list[dict], dirty: bool | None, outside: bool = False, here: bool = False,
             opened: str | None = None) -> Leftover:
    removable, reason = verdict(ticket, dirty, outside, here)
    return Leftover(ticket, path, branch, tuple(dict.fromkeys(w for w in [opened, *workspaces] if w)),
                    tuple(a.get("agent_status") or "unknown" for a in agents), removable, reason,
                    tuple((a["pane_id"], a.get("workspace_id")) for a in agents if a.get("pane_id")), opened)


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
              uncommitted: Uncommitted | None = None, own_pane: str | None = None) -> list[Leftover]:
    """The map's and its lane tickets' worktrees in herdr's order with every agent running in them, then their
    branches checked out nowhere, then agents titled with one of them that run outside those worktrees. Only
    lists: herdr is asked, never told. A worktree `cwd` lies in, or with an agent in `own_pane`, is kept."""
    uncommitted = uncommitted or has_uncommitted
    where = cwd.resolve()
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
        inside = where.is_relative_to(Path(wt["path"]).resolve()) or any(a.get("pane_id") == own_pane for a in here)
        found.append(leftover(ticket, wt["path"], wt.get("branch"), [a.get("workspace_id") for a in here],
                              here, uncommitted(wt["path"]), here=inside, opened=wt.get("open_workspace_id")))
    checked_out = {wt.get("branch") for wt in checkouts}
    for branch in herdr.local_branches(cwd):
        if branch in by_branch and branch not in checked_out:
            found.append(leftover(by_branch[branch], None, branch, [], [], False))
    listed = {entry.path for entry in found}
    for path, here in running.items():
        for agent in here:
            if path not in listed and (ticket := family.get(title_number(agent))) is not None:
                found.append(leftover(ticket, path, None, [agent.get("workspace_id")], [agent], False,
                                      outside=True))
    return found


def shown(path: str, home: Path) -> str:
    return "~/" + str(Path(path).relative_to(home)) if Path(path).is_relative_to(home) else path


def parts(entry: Leftover, home: Path) -> list[str]:
    out = []
    if entry.path:
        out.append(f"worktree {shown(entry.path, home)}")
    if entry.branch:
        out.append(f"branch {entry.branch}")
    if entry.workspaces:
        out.append(f"workspace{'s' if len(entry.workspaces) > 1 else ''} {' '.join(entry.workspaces)}")
    if entry.agents:
        out.append(f"agent{'s' if len(entry.agents) > 1 else ''} {', '.join(entry.agents)}")
    return out


def render(map_ticket: Ticket, found: list[Leftover], home: Path | None = None) -> str:
    state = "open" if map_ticket.is_open else "closed"
    lines = [f"{map_ticket.ref} {map_ticket.title} ({state}): dry run, nothing changed"]
    if not found:
        return "\n".join([*lines, "Nothing left behind."])
    home = Path.home() if home is None else home
    for entry in found:
        mark = "removable" if entry.removable else "kept"
        lines.append(f"{mark:<9}  {entry.ticket.ref}  {', '.join(parts(entry, home))} · {entry.reason}")
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


def remove(cwd: Path, found: list[Leftover], bench: Herdr, home: Path | None = None) -> list[Outcome]:
    """Removes each removable entry: its worktree, then the herdr workspace open on it and the panes of its agents
    in other workspaces, then its branch if git calls it merged. A worktree git refuses to remove keeps everything of it.
    Kept entries are not touched."""
    home = Path.home() if home is None else home
    outcomes = []
    for entry in found:
        if not entry.removable:
            outcomes.append(Outcome(entry, (), entry.reason))
            continue
        removed: list[str] = []
        left: list[str] = []
        failed = False
        if entry.path:
            ok, complaint = git(cwd, "worktree", "remove", entry.path)
            if not ok:
                outcomes.append(Outcome(entry, (), f"git worktree remove refused: {complaint}", failed=True))
                continue
            removed.append(f"worktree {shown(entry.path, home)}")
        closing = [("workspace", entry.opened)] if entry.opened else []
        closing += [("pane", pane) for pane, workspace in entry.panes if workspace != entry.opened]
        for group, target in closing:
            try:
                bench.call(group, "close", target)
            except herdr.HerdrError as exc:
                left.append(f"{group} {target} not closed: {exc}")
                failed = True
            else:
                removed.append(f"workspace {target}" if group == "workspace" else f"agent {target}")
        if entry.branch:
            ok, complaint = git(cwd, "branch", "-d", entry.branch)
            if ok:
                removed.append(f"branch {entry.branch}")
            elif "not fully merged" in complaint:
                left.append(f"branch {entry.branch} kept: not merged in git")
            else:
                left.append(f"branch {entry.branch} kept: {complaint}")
                failed = True
        outcomes.append(Outcome(entry, tuple(removed), ", ".join(left), failed))
    return outcomes


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


def dry_run(cwd: Path, provider: Provider, named: int | str, bench: Herdr,
            uncommitted: Uncommitted | None = None, own_pane: str | None = None) -> str:
    """The named map's leftovers, open or closed."""
    map_ticket, tickets = named_map(provider, named)
    return render(map_ticket, leftovers(cwd, map_ticket, tickets, bench, uncommitted, own_pane))


def clean(cwd: Path, provider: Provider, named: int | str, bench: Herdr,
          uncommitted: Uncommitted | None = None, own_pane: str | None = None) -> tuple[str, bool]:
    """Removes the named closed map's removable leftovers; the summary, and whether every step went through (a
    branch git calls unmerged is no failure). An open map is refused before herdr or git is touched: a human
    closes it in the tracker."""
    map_ticket, tickets = named_map(provider, named)
    if map_ticket.is_open:
        raise CleanupError(f"{map_ticket.ref} is still open: close the map in the tracker first; nothing changed")
    outcomes = remove(cwd, leftovers(cwd, map_ticket, tickets, bench, uncommitted, own_pane), bench)
    return render_outcomes(map_ticket, outcomes), not any(outcome.failed for outcome in outcomes)
