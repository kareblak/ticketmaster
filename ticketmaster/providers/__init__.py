"""Issue-tracker providers. Each one turns a board query into provider-neutral Tickets."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from ..model import Ticket, part_of


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


def run_cli(args: list[str], stdin: str | None = None) -> str:
    try:
        result = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise ProviderError(f"`{args[0]}` is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"`{' '.join(args[:3])}` timed out") from exc
    if result.returncode != 0:
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


# A map issue's URL and labels, or None when there is no such issue.
MapLookup = Callable[[int], tuple[str, tuple[str, ...]] | None]


def resolve_maps(tickets: list[Ticket], fetch_map: MapLookup) -> list[Ticket]:
    """Typed wayfinder tickets without a native map parent take the map their body's `Part of #<map>` names.

    A map already on the board is used as is; any other is fetched once. A missing issue leaves the ticket
    without a map (the board warns about it); a rate limit still surfaces.
    """
    known = {t.number: (t.url, t.labels) for t in tickets}

    def lookup(number: int):
        if number not in known:
            try:
                known[number] = fetch_map(number)
            except RateLimited:
                raise
            except ProviderError:
                known[number] = None
        return known[number]

    resolved = []
    for ticket in tickets:
        number = part_of(ticket.body) if ticket.map_missing else None
        found = lookup(number) if number is not None and number != ticket.number else None
        if found:
            ticket = replace(ticket, parent_number=number, parent_url=found[0], parent_labels=found[1])
        resolved.append(ticket)
    return resolved
