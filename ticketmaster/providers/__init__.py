"""Issue-tracker providers. Each one turns a board query into provider-neutral Tickets."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from ..model import Ticket, parse_blockers


class ProviderError(RuntimeError):
    pass


class RateLimited(ProviderError):
    """The tracker refused the call for exceeding its rate limit; the board backs off."""


RATE_LIMIT = re.compile(r"rate.?limit|too many requests|\bHTTP 429\b|\b429 Too Many\b", re.IGNORECASE)


def tracker_error(message: str) -> ProviderError:
    """A ProviderError, or RateLimited when the message is GitHub's, GitLab's or Gitea's rate-limit answer."""
    return RateLimited(message) if RATE_LIMIT.search(message) else ProviderError(message)


@dataclass(frozen=True)
class Query:
    """What a board shows. With neither parent nor label: every open issue plus those closed within done_days."""

    parent: int | None = None
    label: str | None = None
    done_days: int = 7
    limit: int = 300


class Provider(Protocol):
    name: str
    repo: str

    def fetch(self, query: Query) -> list[Ticket]: ...

    def viewer(self) -> str | None:
        """Login of the user the CLI or token acts as."""
        ...


def run_cli(args: list[str], stdin: str | None = None, check: bool = True) -> str:
    """stdout of a CLI call; with check=False a failing call's stdout is returned too, when it has any."""
    try:
        result = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise ProviderError(f"`{args[0]}` is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"`{' '.join(args[:3])}` timed out") from exc
    if result.returncode != 0 and not (not check and result.stdout.strip()):
        raise tracker_error((result.stderr or result.stdout).strip() or f"`{args[0]}` exited {result.returncode}")
    return result.stdout


def run_json(args: list[str], stdin: str | None = None):
    out = run_cli(args, stdin)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"`{args[0]}` returned non-JSON output: {out[:200]}") from exc


def mentions_parent(body: str, parent: int) -> bool:
    """Fallback parent link for providers without sub-issues: body mentions `#<parent>`."""
    return re.search(rf"(?<![\w&])#{parent}\b", body or "") is not None


class IssueStates:
    """Open (True) or closed (False) state of issues by number, through a provider lookup.

    A closed or missing issue is remembered, so an old `Blocked by:` line costs one lookup per board run.
    """

    def __init__(self, lookup: Callable[[list[int]], dict[int, bool]]):
        self.lookup = lookup
        self.closed: set[int] = set()

    def __call__(self, numbers: list[int]) -> dict[int, bool]:
        ask = [n for n in numbers if n not in self.closed]
        found = self.lookup(ask) if ask else {}
        states = {n: found.get(n, False) for n in numbers}
        self.closed |= {n for n, is_open in states.items() if not is_open}
        return states


def with_body_blockers(tickets: list[Ticket], states: IssueStates) -> list[Ticket]:
    """Adds the open issues a ticket's `Blocked by:` line or `## Blocked by` section lists to its native blockers.

    Issues outside the fetched tickets are looked up; one not found does not block.
    """
    listed = {t.number: parse_blockers(t.body) for t in tickets if t.is_open}
    known = {t.number: t.is_open for t in tickets}
    missing = sorted({n for numbers in listed.values() for n in numbers} - known.keys())
    if missing:
        known |= states(missing)
    out = []
    for ticket in tickets:
        numbers = [n for n in listed.get(ticket.number, ()) if n != ticket.number]
        if not numbers:
            out.append(ticket)
            continue
        refs = list(ticket.blocked_by)
        refs += [ref for n in numbers if known.get(n) and (ref := f"#{n}") not in refs]
        out.append(replace(ticket, blocked_by=tuple(refs), open_blockers=len(refs)))
    return out


def blocker_ref(number: int, repo: str, own_repo: str) -> str:
    """`#<n>` in this repo, `<owner>/<repo>#<n>` in another."""
    return f"#{number}" if not repo or repo.lower() == own_repo.lower() else f"{repo}#{number}"
