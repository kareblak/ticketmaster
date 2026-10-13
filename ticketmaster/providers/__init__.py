"""Issue-tracker providers. Each one turns a board query into provider-neutral Tickets, and can delete an issue."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, replace
from typing import Protocol

from ..model import Ticket


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


# How a map's spec and the spec's implementation tickets link back (`ticketmaster setup` pins them):
# the spec's body has a `Part of #<map>` line, an implementation ticket's `## Parent` section names the spec.
_PARENT_SECTION = re.compile(r"^##\s*Parent\s*$(?P<text>.*?)(?=^#{1,2}\s|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)


def part_of(body: str, parent: int) -> bool:
    """A line of the body starts with `Part of #<parent>`."""
    return re.search(rf"^\s*Part of #{parent}\b", body or "", re.MULTILINE | re.IGNORECASE) is not None


def names_parent(body: str, parent: int) -> bool:
    """The body's `## Parent` section refers to `#<parent>` or to the parent's issue URL."""
    match = _PARENT_SECTION.search(body or "")
    if match is None:
        return False
    text = match.group("text")
    return mentions_parent(text, parent) or re.search(rf"/issues/{parent}\b", text) is not None


def is_spec(ticket: Ticket, parent: Ticket) -> bool:
    """The spec a map led to: `Part of #<map>` and no `wayfinder:` label, so it is no decision ticket."""
    return (parent.is_wayfinder_map and part_of(ticket.body, parent.number)
            and not any(label.startswith("wayfinder:") for label in ticket.labels))


def with_parent(tickets: list[Ticket], parent: Ticket) -> list[Ticket]:
    return [replace(t, parent_number=parent.number, parent_url=parent.url, parent_labels=parent.labels) for t in tickets]


def map_family(tickets: list[Ticket], parent: Ticket) -> list[Ticket]:
    """For trackers without sub-issues: the parent's children (they mention `#<parent>`) and, when the
    parent is a map, the implementation tickets of its spec (`## Parent` names the spec), parented to it."""
    children = [t for t in tickets if t.number != parent.number and mentions_parent(t.body, parent.number)]
    taken = {parent.number} | {t.number for t in children}
    family = with_parent(children, parent)
    for spec in [t for t in children if is_spec(t, parent)]:
        found = [t for t in tickets if t.number not in taken and names_parent(t.body, spec.number)]
        taken |= {t.number for t in found}
        family += with_parent(found, spec)
    return family
