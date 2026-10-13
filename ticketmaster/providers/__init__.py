"""Issue-tracker providers. Each one turns a board query into provider-neutral Tickets, and can delete an issue."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from ..model import Ticket, part_of, wayfinder_kinds


class ProviderError(RuntimeError):
    pass


class RateLimited(ProviderError):
    """The tracker refused the call for exceeding its rate limit; the board backs off."""


class NotFound(ProviderError):
    """The tracker has no such issue."""


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
    def delete(self, number: int) -> None: ...


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

    A map already on the board is used as is; any other is fetched once. A missing issue (fetch_map returns
    None) leaves the ticket without a map, which the board warns about; any other tracker error surfaces.
    """
    known = {t.number: (t.url, t.labels) for t in tickets}

    def lookup(number: int):
        if number not in known:
            known[number] = fetch_map(number)
        return known[number]

    resolved = []
    for ticket in tickets:
        number = part_of(ticket.body) if ticket.map_missing else None
        found = lookup(number) if number is not None and number != ticket.number else None
        if found:
            ticket = with_map(ticket, number, found)
        resolved.append(ticket)
    return resolved


def with_map(ticket: Ticket, number: int, found: tuple[str, tuple[str, ...]]) -> Ticket:
    url, labels = found
    return replace(ticket, parent_number=number, parent_url=url, parent_labels=labels)


def on_map_board(tickets: list[Ticket], number: int, found: tuple[str, tuple[str, ...]] | None) -> list[Ticket]:
    """A map board's map is each ticket's parent, unless its `Part of #<map>` line names another map."""
    if not found:
        return tickets
    return [with_map(t, number, found) if part_of(t.body) in (None, number) else t for t in tickets]


# How a map's spec and the spec's implementation tickets link back (`ticketmaster setup` pins them): the
# spec's first line is `Part of #<map>`, an implementation ticket's `## Parent` section names the spec.
_PARENT_SECTION = re.compile(r"^##\s*Parent\s*$(?P<text>.*?)(?=^#{1,2}\s|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)


def names_parent(body: str, parent: int) -> bool:
    """The body's `## Parent` section refers to `#<parent>` or to the parent's issue URL."""
    match = _PARENT_SECTION.search(body or "")
    if match is None:
        return False
    text = match.group("text")
    return mentions_parent(text, parent) or re.search(rf"/issues/{parent}\b", text) is not None


def is_map(found: tuple[str, tuple[str, ...]] | None) -> bool:
    return bool(found) and "map" in wayfinder_kinds(found[1])


def is_spec(ticket: Ticket, map_number: int) -> bool:
    """The spec a map led to: `Part of #<map>` and no `wayfinder:` label, so it is no decision ticket."""
    return part_of(ticket.body) == map_number and not wayfinder_kinds(ticket.labels)


def under_spec(tickets: list[Ticket], spec: Ticket) -> list[Ticket]:
    return [with_map(t, spec.number, (spec.url, spec.labels)) for t in tickets]


def map_family(tickets: list[Ticket], number: int, found: tuple[str, tuple[str, ...]] | None) -> list[Ticket]:
    """For trackers without sub-issues: the parent's children (they mention `#<parent>`) and, when the
    parent is a map, the implementation tickets of its spec (`## Parent` names the spec), parented to it."""
    children = [t for t in tickets if t.number != number and mentions_parent(t.body, number)]
    family = on_map_board(children, number, found)
    if not is_map(found):
        return family
    taken = {number} | {t.number for t in children}
    for spec in [t for t in children if is_spec(t, number)]:
        linked = [t for t in tickets if t.number not in taken and names_parent(t.body, spec.number)]
        taken |= {t.number for t in linked}
        family += under_spec(linked, spec)
    return family
