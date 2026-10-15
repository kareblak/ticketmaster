"""Issue-tracker providers. Each one turns a board query into provider-neutral Tickets; none writes to the tracker."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Literal, Protocol

from ..model import (WAYFINDER_TYPES, Ticket, has_spec_signature, has_ticket_signature, lane_tickets, parent_ref,
                     parent_refs, parse_blockers, part_of, wayfinder_kinds)

TRACKER_NAMES = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea/Forgejo", "linear": "Linear"}


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


# The default fetch limit: open maps read per board, and tickets per map; a tracker page beyond these is not asked for.
LIMIT = 300


@dataclass(frozen=True)
class Query:
    """What a board shows: every open wayfinder map with its tickets, every closed map with an open spec, plus
    `named_map` when that map is closed, and every standalone spec with an open spec or ticket; at most `limit` open
    maps, and `limit` tickets per map or spec."""

    named_map: int | None = None
    limit: int = LIMIT


@dataclass(frozen=True)
class Quota:
    """What a provider's last fetch cost in the tracker's rate-limit points, and what the tracker has left.

    Only GitHub reports one; a provider without a `quota` reading is not paced.
    """

    cost: int  # the points the last fetch spent, summed over its calls
    remaining: int | None  # the points left, as the latest call saw them
    resets_at: datetime | None  # when the points refill


# Issues read per `wayfinder:` label on trackers without sub-issues: the whole repo's, open and closed, before
# `Part of #<map>` picks a shown map's, so a map's tickets are not cut off by other maps' closed ones.
LABEL_LIMIT = 5000


def wayfinder_labels(names: list[str]) -> dict[str, list[str]]:
    """Label names for maps (`map`) and for the four ticket types (`ticket`), in the order given."""
    found: dict[str, list[str]] = {"map": [], "ticket": []}
    for name in dict.fromkeys(names):
        kinds = wayfinder_kinds((name,))
        if "map" in kinds:
            found["map"].append(name)
        elif any(kind in WAYFINDER_TYPES for kind in kinds):
            found["ticket"].append(name)
    return found


def map_board(maps: dict[int, Ticket], named: Ticket | None, children: list[Ticket]) -> list[Ticket]:
    """For trackers without sub-issues: the maps, the named map when it is one, then each typed ticket whose
    `Part of #<map>` line names one of them."""
    if named is not None and named.is_wayfinder_map:
        maps = {**maps, named.number: named}
    linked = {t.number: t for t in children if t.wayfinder_type is not None and part_of(t.body) in maps}
    return list(maps.values()) + [t for n, t in linked.items() if n not in maps]


# What trackers without sub-issues search their issues for: a spec's `Part of #<map>` line, and an implementation
# ticket's `## Parent` section. The search only narrows; `with_specs` checks each hit's body exactly.
SPEC_SEARCHES = ("Part of", "Parent")


def with_specs(tickets: list[Ticket], candidates: list[Ticket]) -> list[Ticket]:
    """For trackers without sub-issues: the board's tickets, then each untyped candidate whose `Part of #<map>`
    line names one of the board's maps (a spec), then each whose `## Parent` section names one of those specs."""
    maps = {t.number for t in tickets if t.is_wayfinder_map}
    seen = {t.number for t in tickets}
    plain = {t.number: t for t in candidates if t.number not in seen and not wayfinder_kinds(t.labels)}
    specs = {n: t for n, t in plain.items() if part_of(t.body) in maps}
    impls = [t for n, t in plain.items() if n not in specs and parent_ref(t.body) in specs]
    return [*tickets, *specs.values(), *impls]


def closed_map_refs(candidates: list[Ticket], shown: set[int], key: str | None = None) -> list[int]:
    """The maps named by an open, untyped candidate's `Part of` line, or by the sub-issue parent of one with the spec
    signature (an open spec), that are not `shown`: closed maps whose spec is still being worked, which the board
    shows too. The caller checks each one is a map."""
    def named(t: Ticket) -> int | None:
        return part_of(t.body, key) or (t.parent_number if has_spec_signature(t.body) else None)

    refs = {n for t in candidates if t.is_open and not wayfinder_kinds(t.labels)
            and (n := named(t)) is not None and n != t.number}
    return sorted(refs - shown)


# What a fetch cut off at the limit, as the board's status line names it: the open maps, the signature search's
# hits, or a map's or spec's tickets.
OPEN_MAPS = "open maps"
SIGNATURE_HITS = "specs and tickets"

# How long a closed spec or ticket stays a signature search hit.
RECENTLY_CLOSED = timedelta(days=7)


def is_signature_hit(ticket: Ticket, since: datetime) -> bool:
    """A signature search hit the board keeps: the spec or ticket signature in its body (the search only narrows), no
    `wayfinder:` label, and open or closed since `since`."""
    recent = ticket.is_open or ticket.closed_at is not None and ticket.closed_at >= since
    signed = has_spec_signature(ticket.body) or has_ticket_signature(ticket.body)
    return recent and signed and not wayfinder_kinds(ticket.labels)


def is_standalone_candidate(ticket: Ticket, maps: set[int]) -> bool:
    """The spec signature, no `wayfinder:` label, no `Part of` line naming one of `maps` and no sub-issue parent among
    them."""
    return (has_spec_signature(ticket.body) and not wayfinder_kinds(ticket.labels)
            and part_of(ticket.body, ticket.team_key) not in maps and ticket.parent_number not in maps)


def is_spec_ticket(ticket: Ticket, spec: int | None) -> bool:
    """The ticket signature, and not the spec one, with `spec` as its sub-issue parent, else its first `## Parent`
    reference."""
    return (spec is not None and ticket.spec_number == spec and has_ticket_signature(ticket.body)
            and not has_spec_signature(ticket.body) and not wayfinder_kinds(ticket.labels))


def in_spec_lane(tickets: list[Ticket], spec: Ticket) -> list[Ticket]:
    """The standalone spec is each ticket's parent, and their lane's."""
    return [replace(with_map(t, spec), lane_spec=spec.number) for t in tickets]


def lane_cut(map_ticket: Ticket) -> str:
    return f"{map_ticket.ref}'s tickets"


def capped(items: list, limit: int, what: str, cut: list[str]) -> list:
    """The first `limit` items; when there are more, `what` goes on `cut`. Callers read `limit + 1` items where they
    can, so the one past the limit tells that the tracker had more."""
    if len(items) > limit:
        cut.append(what)
    return items[:limit]


def cap_lanes(tickets: list[Ticket], limit: int, cut: list[str]) -> list[Ticket]:
    """The board's tickets with each map's first `limit` in map order (its lane's tickets); a map with more goes on
    `cut`. Run after resolve_maps, which links specs and their tickets to their map."""
    dropped: set[int] = set()
    for map_ticket in [t for t in tickets if t.is_wayfinder_map]:
        family = lane_tickets(map_ticket, tickets)
        kept = capped(family, limit, lane_cut(map_ticket), cut)
        dropped |= {t.number for t in family[len(kept):]}
    return [t for t in tickets if t.number not in dropped]


class Provider(Protocol):
    name: str
    repo: str
    # Each provider also sets `cut_off: list[str]` in fetch: what that fetch cut off at its query's limit, e.g.
    # `open maps`, `#12's tickets`.

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


def with_body_blockers(tickets: list[Ticket], states: IssueStates, key: str | None = None) -> list[Ticket]:
    """Adds the open issues a ticket's `Blocked by:` line or `## Blocked by` section lists to its native blockers,
    as `#<n>`, or as `<key>-<n>` on a tracker with issue keys (Linear's `TM`).

    Issues outside the fetched tickets are looked up; one not found does not block.
    """
    listed = {t.number: parse_blockers(t.body, key) for t in tickets if t.is_open}
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
        refs += [ref for n in numbers if known.get(n) and (ref := f"{key}-{n}" if key else f"#{n}") not in refs]
        out.append(replace(ticket, blocked_by=tuple(refs), open_blockers=len(refs)))
    return out


def blocker_ref(number: int, repo: str, own_repo: str) -> str:
    """`#<n>` in this repo, `<owner>/<repo>#<n>` in another."""
    return f"#{number}" if not repo or repo.lower() == own_repo.lower() else f"{repo}#{number}"


def names_parent(body: str, parent: int, key: str | None = None) -> bool:
    """The body's `## Parent` section refers to `#<parent>`, to the parent's issue URL or to its key."""
    return parent in parent_refs(body, key)


def is_map(found: Ticket | None) -> bool:
    return found is not None and found.is_wayfinder_map


def as_spec(ticket: Ticket, map_ticket: Ticket) -> Ticket:
    """The map is the spec's parent, and the map its session starts wayfinder on."""
    return on_map(with_map(ticket, map_ticket), map_ticket.number, map_ticket.url)


def with_spec_tickets(tickets: list[Ticket]) -> list[Ticket]:
    """Marks each spec that an implementation ticket, open or closed, names as its parent: the sub-issue parent,
    else the `## Parent` section."""
    named = {t.spec_number for t in tickets if t.spec_kind == "impl"}
    return [replace(t, has_tickets=True) if t.spec_kind == "spec" and t.number in named else t for t in tickets]


def under_spec(tickets: list[Ticket], spec: Ticket) -> list[Ticket]:
    """The spec is each ticket's parent; the spec's map is the one their sessions start wayfinder on."""
    return [on_map(with_map(t, spec), spec.map_number, spec.map_url) for t in tickets]
