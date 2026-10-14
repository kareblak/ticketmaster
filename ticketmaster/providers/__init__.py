"""Issue-tracker providers. Each one turns a board query into provider-neutral Tickets; none writes to the tracker."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from http import HTTPStatus
from typing import Literal, Protocol

from ..model import Ticket, parse_blockers, part_of, wayfinder_kinds

TRACKER_NAMES = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea/Forgejo"}


class ProviderError(RuntimeError):
    """A tracker call failed. `status` is the HTTP status, when the tracker or its CLI said which."""

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status if status is not None else http_status(message)


class RateLimited(ProviderError):
    """The tracker refused the call for exceeding its rate limit; the board backs off.

    `limit` names the limit hit (e.g. "secondary limit") and `resets_at` when it lifts, when the tracker says.
    """

    def __init__(self, message: str = "", status: int | None = None, limit: str | None = None,
                 resets_at: datetime | None = None, hint: str | None = None):
        super().__init__(message, status)
        self.limit = limit
        self.resets_at = resets_at
        self.hint = hint  # what the limit means, e.g. that every board on the account shares it

    def hit(self, limit: str, resets_at: datetime | None = None, hint: str | None = None) -> RateLimited:
        """The same refusal, naming the limit it hit."""
        return RateLimited(str(self), self.status, limit, resets_at, hint)


class NotFound(ProviderError):
    """The tracker has no such issue."""


RATE_LIMIT = re.compile(r"rate.?limit|too many requests|\bHTTP 429\b|\b429 Too Many\b", re.IGNORECASE)


# `… (HTTP 401)` from gh and others, or `glab: 502 Bad Gateway`.
_HTTP_STATUS = re.compile(r"\bHTTP (?P<code>[1-5]\d\d)\b|^\w+: (?P<lead>[45]\d\d) [A-Z]")


def http_status(message: str) -> int | None:
    """The HTTP status a tracker or CLI message names, or None when it names none."""
    match = _HTTP_STATUS.search(message or "")
    return int(match.group("code") or match.group("lead")) if match else None


def tracker_error(message: str) -> ProviderError:
    """A ProviderError, or RateLimited when the message is GitHub's, GitLab's or Gitea's rate-limit answer."""
    return RateLimited(message) if RATE_LIMIT.search(message) else ProviderError(message)


@dataclass(frozen=True)
class ErrorNotice:
    """What the board tells the user about a tracker error."""

    reason: str  # short: `secondary limit`, `429`, `401 unauthorized`
    text: str  # the notification: what happened, then the tracker's own message
    severity: Literal["warning", "error"]  # warning when the board retries by itself, error when it needs the user
    status_text: str  # what the status line shows
    episode: str  # an error that differs here is a new one: the reason, or the message when there is no status


def error_notice(tracker: str, error: ProviderError) -> ErrorNotice:
    """How the board reports a tracker error: rate limits and 5xx as warnings, anything else as an error."""
    name, message, status = TRACKER_NAMES.get(tracker, tracker), str(error), error.status
    if isinstance(error, RateLimited):
        reason = error.limit or (str(status) if status else "rate limit")
        head = f"{name} rate-limited the board: {reason}"
        head += f" ({error.hint})" if error.hint else ""
        head += f", lifts at {error.resets_at.astimezone():%H:%M}" if error.resets_at else ""
        return ErrorNotice(reason, f"{head}\n{message}", "warning", f"rate-limited · {reason}", reason)
    if status is None:
        return ErrorNotice("error", f"{name} error\n{message}", "error", message, message)
    try:
        reason = f"{status} {HTTPStatus(status).phrase.lower()}"
    except ValueError:
        reason = f"HTTP {status}"
    severity = "warning" if status >= 500 else "error"
    return ErrorNotice(reason, f"{name} refused the board: {reason}\n{message}", severity, f"{reason} · {message}",
                       reason)


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
    partial = not check and result.stdout.strip()
    if result.returncode != 0 and not partial:
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


# An issue by number (its URL, labels and body), or None when there is no such issue.
IssueLookup = Callable[[int], Ticket | None]


def resolve_maps(tickets: list[Ticket], fetch_issue: IssueLookup, beside: tuple[Ticket, ...] = ()) -> list[Ticket]:
    """Finds each ticket's map. A typed wayfinder ticket without a native map parent takes the map its body's
    `Part of #<map>` names; a spec (`Part of #<map>`, no `wayfinder:` label) takes that map, and so does a
    ticket whose spec it is, by the GitHub sub-issue parent, else the `## Parent` section.

    An issue already on the board, or `beside` it (a parent board's parent), is used as is; any other is
    fetched at most once. A missing issue (fetch_issue
    returns None) ends the chain, so a typed ticket stays without a map, which the board warns about, and any
    other ticket stays plain; any other tracker error surfaces.
    """
    known: dict[int, Ticket | None] = {t.number: t for t in (*beside, *tickets)}

    def lookup(number: int) -> Ticket | None:
        if number not in known:
            known[number] = fetch_issue(number)
        return known[number]

    def spec_map(number: int) -> tuple[int, str] | None:
        """The map issue #<number> leads to as a spec, or None when it is no spec of a map."""
        spec = lookup(number)
        if spec is None or wayfinder_kinds(spec.labels):
            return None
        if spec.map_number is not None and spec.map_url:
            return spec.map_number, spec.map_url
        map_number = part_of(spec.body)
        found = lookup(map_number) if map_number not in (None, number) else None
        return (map_number, found.url) if is_map(found) else None

    resolved = []
    for ticket in tickets:
        if ticket.map_missing:
            number = part_of(ticket.body)
            found = lookup(number) if number not in (None, ticket.number) else None
            if found:
                ticket = with_map(ticket, found)
        elif ticket.map_url is None and ticket.wayfinder_type is None and not ticket.is_wayfinder_map:
            spec_number = ticket.parent_number or parent_ref(ticket.body)
            in_spec = spec_map(spec_number) if spec_number not in (None, ticket.number) else None
            if map_ref := spec_map(ticket.number) or in_spec:
                ticket = on_map(ticket, *map_ref)
        resolved.append(ticket)
    return resolved


def on_map(ticket: Ticket, number: int | None, url: str | None) -> Ticket:
    """The map a ticket's session starts wayfinder on; its parent stays as it is."""
    return replace(ticket, map_number=number, map_url=url)


def with_map(ticket: Ticket, parent: Ticket) -> Ticket:
    return replace(ticket, parent_number=parent.number, parent_url=parent.url, parent_labels=parent.labels)


def on_map_board(tickets: list[Ticket], found: Ticket | None) -> list[Ticket]:
    """A map board's map is each ticket's parent, unless its `Part of #<map>` line names another map."""
    if not found:
        return tickets
    return [with_map(t, found) if part_of(t.body) in (None, found.number) else t for t in tickets]


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


# How a map's spec and the spec's implementation tickets link back: the
# spec's first line is `Part of #<map>`, an implementation ticket's `## Parent` section names the spec.
_PARENT_SECTION = re.compile(r"^##\s*Parent\s*$(?P<text>.*?)(?=^#{1,2}\s|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)
_PARENT_REF = re.compile(r"(?<![\w&])#(?P<hash>\d+)\b|/issues/(?P<url>\d+)\b")


def names_parent(body: str, parent: int) -> bool:
    """The body's `## Parent` section refers to `#<parent>` or to the parent's issue URL."""
    match = _PARENT_SECTION.search(body or "")
    if match is None:
        return False
    text = match.group("text")
    return mentions_parent(text, parent) or re.search(rf"/issues/{parent}\b", text) is not None


def parent_ref(body: str) -> int | None:
    """The issue the body's `## Parent` section names first, as `#<n>` or by its issue URL."""
    match = _PARENT_SECTION.search(body or "")
    ref = _PARENT_REF.search(match.group("text")) if match else None
    return int(ref.group("hash") or ref.group("url")) if ref else None


def is_map(found: Ticket | None) -> bool:
    return found is not None and found.is_wayfinder_map


def is_spec(ticket: Ticket, map_number: int) -> bool:
    """The spec a map led to: `Part of #<map>` and no `wayfinder:` label, so it is no decision ticket."""
    return part_of(ticket.body) == map_number and not wayfinder_kinds(ticket.labels)


def as_spec(ticket: Ticket, map_ticket: Ticket) -> Ticket:
    """The map is the spec's parent, and the map its session starts wayfinder on."""
    return on_map(with_map(ticket, map_ticket), map_ticket.number, map_ticket.url)


def under_spec(tickets: list[Ticket], spec: Ticket) -> list[Ticket]:
    """The spec is each ticket's parent; the spec's map is the one their sessions start wayfinder on."""
    return [on_map(with_map(t, spec), spec.map_number, spec.map_url) for t in tickets]


def map_family(tickets: list[Ticket], number: int, found: Ticket | None) -> list[Ticket]:
    """For trackers without sub-issues: the parent's children (they mention `#<parent>`) and, when the
    parent is a map, the spec and its implementation tickets (`## Parent` names the spec), parented to it."""
    children = [t for t in tickets if t.number != number and mentions_parent(t.body, number)]
    family = on_map_board(children, found)
    if not is_map(found):
        return family
    family = [as_spec(t, found) if is_spec(t, number) else t for t in family]
    taken = {number} | {t.number for t in children}
    for spec in [t for t in family if is_spec(t, number)]:
        linked = [t for t in tickets if t.number not in taken and names_parent(t.body, spec.number)]
        taken |= {t.number for t in linked}
        family += under_spec(linked, spec)
    return family
