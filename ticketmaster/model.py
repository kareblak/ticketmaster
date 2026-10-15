"""Provider-neutral ticket model, and the swimlane board: one lane per wayfinder map, in four columns."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import NamedTuple


class Column(Enum):
    BLOCKED = "Blocked"
    FRONTIER = "Frontier"
    CLAIMED = "Claimed"
    DECIDED = "Decided"


COLUMN_ORDER = [Column.BLOCKED, Column.FRONTIER, Column.CLAIMED, Column.DECIDED]

WAYFINDER_TYPES = ("research", "prototype", "grilling", "task")
# `wayfinder:<kind>` (GitHub/Gitea) and `wayfinder::<kind>` (GitLab scoped labels).
WAYFINDER_LABEL = re.compile(r"^wayfinder::?(?P<kind>[^:].*)$", re.IGNORECASE)
# The child→map rule for trackers without a native link: the body's first line is `Part of #<map>`.
PART_OF = re.compile(r"^Part of #(?P<map>\d+)\.?$")
# HITL: worked with a human; AFK: the agent alone. A task says which in its body.
TYPE_MODES = {"grilling": "HITL", "prototype": "HITL", "research": "AFK"}
_MODE_WORD = re.compile(r"\b(HITL|AFK)\b")  # the acronyms, not "afk" in prose


@dataclass(frozen=True)
class Ticket:
    number: int
    title: str
    url: str
    is_open: bool
    body: str = ""
    closed_at: datetime | None = None
    # Closed as not planned (canceled); trackers without a close reason leave it False.
    not_planned: bool = False
    # Login/username of whoever opened the issue; empty when the provider did not say.
    author: str = ""
    assignees: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    # Open issues blocking this one; None when the provider cannot tell.
    open_blockers: int | None = None
    # Those open blockers as `#<n>`, or `<owner>/<repo>#<n>` in another repo, native links first.
    blocked_by: tuple[str, ...] = ()
    has_open_change_request: bool = False
    # Position inside the parent (sub-issue order) or provider order; lower sorts first.
    order: int = 0
    parent_number: int | None = None
    parent_url: str | None = None
    parent_labels: tuple[str, ...] = ()
    # The map a spec, or a spec's implementation ticket, leads to; the parent stays the spec for those tickets.
    map_number: int | None = None
    map_url: str | None = None

    @property
    def is_blocked(self) -> bool:
        return bool(self.open_blockers)

    def start_refusal(self, viewer: str | None) -> str | None:
        """Why this ticket must not be started by `viewer`, or None. An unknown viewer is not told apart."""
        if self.blocked_by:
            return f"#{self.number} is blocked by {', '.join(self.blocked_by)}"
        if self.open_blockers:
            return f"#{self.number} is blocked by {self.open_blockers} open issue{'s' if self.open_blockers > 1 else ''}"
        if viewer and self.assignees and viewer.lower() not in (a.lower() for a in self.assignees):
            return f"#{self.number} is claimed by {', '.join('@' + a for a in self.assignees)}"
        if not (self.is_wayfinder_work or self.map_missing):  # a typed ticket without its map is warned about apart
            return self.not_wayfinder_work
        return None

    @property
    def not_wayfinder_work(self) -> str:
        return f"#{self.number} is not wayfinder work: the board only starts /wayfinder"

    @property
    def is_wayfinder_work(self) -> bool:
        """A map, or a ticket on one: the only work the board starts, always with /wayfinder."""
        return self.is_wayfinder_map or self.is_wayfinder_ticket or self.map_url is not None

    @property
    def is_wayfinder_map(self) -> bool:
        return "map" in wayfinder_kinds(self.labels)

    @property
    def wayfinder_type(self) -> str | None:
        """`research`, `prototype`, `grilling` or `task`; any other `wayfinder:` label does not count."""
        return next((kind for kind in wayfinder_kinds(self.labels) if kind in WAYFINDER_TYPES), None)

    @property
    def is_wayfinder_ticket(self) -> bool:
        return self.wayfinder_type is not None and self.parent_url is not None and self.parent_is_map

    @property
    def parent_is_map(self) -> bool:
        return "map" in wayfinder_kinds(self.parent_labels)

    @property
    def spec_kind(self) -> str | None:
        """`spec` for the spec a map led to (`Part of #<map>`), `impl` for one of its implementation tickets, else
        None; either is untyped and has a map the provider resolved."""
        if self.map_number is None or wayfinder_kinds(self.labels):
            return None
        return "spec" if part_of(self.body) == self.map_number else "impl"

    @property
    def map_missing(self) -> bool:
        """A typed ticket whose map the board could not find: it starts neither wayfinder nor implement."""
        return self.wayfinder_type is not None and not self.is_wayfinder_ticket

    @property
    def map_warning(self) -> str:
        return f"wayfinder:{self.wayfinder_type}, map not found"

    @property
    def mode(self) -> str | None:
        """`HITL` or `AFK` for a wayfinder ticket; a task by the first of those words in its body, else None."""
        if self.wayfinder_type == "task":
            match = _MODE_WORD.search(self.body or "")
            return match.group(1) if match else None
        return TYPE_MODES.get(self.wayfinder_type)

    @property
    def summary(self) -> str:
        return summarize(self.body)


def wayfinder_kinds(labels: tuple[str, ...]) -> list[str]:
    return [m.group("kind").lower() for label in labels if (m := WAYFINDER_LABEL.match(label))]


def part_of(body: str) -> int | None:
    """The map number from a body whose first non-blank line is `Part of #<map>`."""
    first = next((line.strip() for line in (body or "").splitlines() if line.strip()), "")
    match = PART_OF.match(first)
    return int(match.group("map")) if match else None


def map_tickets(map_ticket: Ticket, tickets: list[Ticket]) -> list[Ticket]:
    """The map's typed wayfinder tickets, in map order."""
    children = [t for t in tickets if t.parent_number == map_ticket.number and t.is_wayfinder_ticket]
    return sorted(children, key=lambda t: (t.order, t.number))


def lane_tickets(map_ticket: Ticket, tickets: list[Ticket]) -> list[Ticket]:
    """The map's decision tickets, its specs and their implementation tickets, in map order."""
    follow_ups = [t for t in tickets if t.spec_kind and t.map_number == map_ticket.number]
    return sorted(map_tickets(map_ticket, tickets) + follow_ups, key=lambda t: (t.order, t.number))


def frontier(map_ticket: Ticket, tickets: list[Ticket], taken: set[int] = frozenset(),
             order: list[int] | None = None) -> list[Ticket]:
    """The map's open, unblocked, unclaimed tickets in map order; `taken` are being started but not claimed yet.

    `order` is the tracker's own map order (GitHub sub-issue order), which a search board does not carry;
    tickets it does not list follow in board order.
    """
    ready = [t for t in map_tickets(map_ticket, tickets)
             if t.is_open and not t.is_blocked and not t.assignees and t.number not in taken]
    if order is None:
        return ready
    position = {number: index for index, number in enumerate(order)}
    return sorted(ready, key=lambda t: position.get(t.number, len(position)))


class MapProgress(NamedTuple):
    decided: int  # closed tickets, less those ruled out of scope
    open: int
    blocked: int


def map_progress(map_ticket: Ticket, tickets: list[Ticket]) -> MapProgress:
    children = map_tickets(map_ticket, tickets)
    still_open = [t for t in children if t.is_open]
    decided = sum(not t.is_open and not is_out_of_scope(t, map_ticket) for t in children)
    return MapProgress(decided, len(still_open), sum(t.is_blocked for t in still_open))


_OUT_OF_SCOPE = re.compile(r"^#+[ \t]*Out[- ]of[- ]scope[ \t]*:?[ \t]*$(?P<text>.*?)(?=^#+[ \t]|\Z)",
                           re.MULTILINE | re.DOTALL | re.IGNORECASE)
_HASH_REF = re.compile(r"(?<![\w&/])#(\d+)\b")
_ISSUE_URL = re.compile(r"(?P<base>https?://\S+?/issues/)(?P<number>\d+)\b")


def out_of_scope_refs(map_ticket: Ticket) -> set[int]:
    """Issues of the map's repo that its `## Out of scope` section links, as `#<n>` or by issue URL."""
    match = _OUT_OF_SCOPE.search(map_ticket.body or "")
    if match is None:
        return set()
    text, base = match.group("text"), map_ticket.url.rpartition("/")[0] + "/"
    urls = {int(m.group("number")) for m in _ISSUE_URL.finditer(text) if m.group("base") == base}
    return {int(n) for n in _HASH_REF.findall(text)} | urls


def is_out_of_scope(ticket: Ticket, map_ticket: Ticket) -> bool:
    """Closed as canceled, or closed and linked from the map's `## Out of scope` section."""
    return not ticket.is_open and (ticket.not_planned or ticket.number in out_of_scope_refs(map_ticket))


def classify(ticket: Ticket) -> Column:
    """The first rule that matches: closed, an open blocker, an assignee, else the frontier."""
    if not ticket.is_open:
        return Column.DECIDED
    if ticket.is_blocked:
        return Column.BLOCKED
    if ticket.assignees:
        return Column.CLAIMED
    return Column.FRONTIER


@dataclass
class Lane:
    """A map and its decision tickets, specs and implementation tickets by column; a closed map is collapsed to its
    header. `progress` counts the decision tickets only."""

    map: Ticket
    columns: dict[Column, list[Ticket]] = field(default_factory=lambda: {c: [] for c in COLUMN_ORDER})
    progress: MapProgress = MapProgress(0, 0, 0)
    out_of_scope: set[int] = field(default_factory=set)

    @property
    def collapsed(self) -> bool:
        return not self.map.is_open

    def tickets(self) -> list[Ticket]:
        return [ticket for tickets in self.columns.values() for ticket in tickets]


@dataclass
class Board:
    lanes: list[Lane] = field(default_factory=list)

    def ticket_count(self) -> int:
        """The lanes' ticket cards, not counting the maps."""
        return sum(len(lane.tickets()) for lane in self.lanes)

    def tickets(self) -> list[Ticket]:
        """Every card: each lane's map, then its tickets."""
        return [ticket for lane in self.lanes for ticket in (lane.map, *lane.tickets())]


def build_board(tickets: list[Ticket], named_map: int | None = None) -> Board:
    """A lane per open map, by number, plus the named map when it is closed; anything else is not shown."""
    maps = sorted((t for t in tickets if t.is_wayfinder_map and (t.is_open or t.number == named_map)),
                  key=lambda t: t.number)
    board = Board()
    for map_ticket in maps:
        children = lane_tickets(map_ticket, tickets)
        lane = Lane(map_ticket, progress=map_progress(map_ticket, tickets),
                    out_of_scope={t.number for t in children if is_out_of_scope(t, map_ticket)})
        if not lane.collapsed:
            for ticket in children:
                lane.columns[classify(ticket)].append(ticket)
        board.lanes.append(lane)
    return board


_QUESTION = re.compile(r"^##\s*Question\s*$(?P<text>.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)


def summarize(body: str, max_chars: int = 90) -> str:
    """First meaningful prose of an issue body; prefers a wayfinder `## Question` section."""
    if not body:
        return ""
    match = _QUESTION.search(body)
    text = match.group("text") if match else body
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "```", "|", "---")):
            continue
        line = re.sub(r"^[-*>]\s+", "", line)
        lines.append(re.sub(r"^\[[ xX]\]\s*", "", line))
    flat = " ".join(lines)
    return flat if len(flat) <= max_chars else flat[: max_chars - 1].rstrip() + "…"


# `Blocked by: #3, #4` (pinned by the setup ticket), or the older Norwegian `Blokkert av: #3`.
_BLOCKED_LINE = re.compile(r"^[ \t>*-]*(?:\*\*)?(?:blocked by|blokkert av)(?:\*\*)?[ \t]*:(?:\*\*)?(?P<refs>.*)$",
                           re.MULTILINE | re.IGNORECASE)
# `## Blocked by` with one `- #3` bullet per blocker, as `to-tickets` writes it.
_BLOCKED_SECTION = re.compile(r"^##+[ \t]*(?:blocked by|blokkert av)[ \t]*:?[ \t]*$(?P<refs>.*?)(?=^#+[ \t]|\Z)",
                              re.MULTILINE | re.DOTALL | re.IGNORECASE)
_REF = re.compile(r"(?<![\w&/])#(\d+)\b")


def parse_blockers(body: str) -> tuple[int, ...]:
    """Issue numbers in this repo that a body's `Blocked by:` lines and `## Blocked by` sections list, in order."""
    refs = [m.group("refs") for pattern in (_BLOCKED_LINE, _BLOCKED_SECTION) for m in pattern.finditer(body or "")]
    return tuple(dict.fromkeys(int(n) for text in refs for n in _REF.findall(text)))


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
