"""Provider-neutral ticket model, and the swimlane board: a lane per wayfinder map and per standalone spec, in four
columns."""

from __future__ import annotations

import functools
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
    # A PR that closes it is merged; trackers that only close the issue on merge leave it False.
    has_merged_change_request: bool = False
    # Position inside the parent (sub-issue order) or provider order; lower sorts first.
    order: int = 0
    parent_number: int | None = None
    parent_url: str | None = None
    parent_labels: tuple[str, ...] = ()
    # The map a spec, or a spec's implementation ticket, leads to; the parent stays the spec for those tickets.
    map_number: int | None = None
    map_url: str | None = None
    # The tracker's own key (`TM-26` on Linear), and the branch it wants a ticket's work on (Linear's
    # gitBranchName); None on trackers that key issues by number alone.
    key: str | None = None
    branch_name: str | None = None
    # A spec with an implementation ticket, open or closed, so it has been broken into tickets already.
    has_tickets: bool = False
    # The standalone spec whose lane holds this ticket (its sub-issue parent, else its `## Parent` section), which
    # the provider resolved; None for any other ticket.
    lane_spec: int | None = None

    @property
    def team_key(self) -> str | None:
        """The tracker's key prefix, `TM` of Linear's `TM-26`, else None."""
        return self.key.rpartition("-")[0] or None if self.key else None

    @property
    def is_blocked(self) -> bool:
        return bool(self.open_blockers)

    @property
    def ref(self) -> str:
        """How the board names the ticket: its key, else `#<n>`."""
        return self.key or f"#{self.number}"

    def start_refusal(self, viewer: str | None) -> str | None:
        """Why this ticket must not be started by `viewer`, or None. An unknown viewer is not told apart. A live
        session is the board's to check, since only herdr knows it."""
        if not self.is_open:
            return f"{self.ref} is closed"
        if self.blocked_by:
            return f"{self.ref} is blocked by {', '.join(self.blocked_by)}"
        if self.open_blockers:
            return f"{self.ref} is blocked by {self.open_blockers} open issue{'s' if self.open_blockers > 1 else ''}"
        if viewer and self.assignees and viewer.lower() not in (a.lower() for a in self.assignees):
            return f"{self.ref} is claimed by {', '.join('@' + a for a in self.assignees)}"
        if self.spec_kind == "spec" and self.has_tickets:
            return f"{self.ref} has tickets: s starts /implement on its tickets"
        if self.spec_kind == "impl" and self.has_open_change_request:
            return f"{self.ref} has an open PR that closes it"
        if not (self.is_board_work or self.map_missing):  # a typed ticket without its map is warned about apart
            return self.not_board_work
        return None

    @property
    def not_board_work(self) -> str:
        return f"{self.ref} is not board work: the board starts work on wayfinder maps and standalone specs"

    @property
    def is_board_work(self) -> bool:
        """The only work the board starts: wayfinder work or standalone work."""
        return self.is_wayfinder_work or self.is_standalone_work

    @property
    def is_wayfinder_work(self) -> bool:
        """A map, or a ticket on one: /wayfinder, /to-tickets on its spec, or /implement on an impl card."""
        return self.is_wayfinder_map or self.is_wayfinder_ticket or self.map_url is not None

    @property
    def is_standalone_work(self) -> bool:
        """A standalone spec (/to-tickets), or a ticket in its lane (/implement)."""
        return self.is_standalone_spec or (self.spec_kind == "impl" and self.lane_spec is not None)

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
        """`spec` for the spec a map led to (`Part of #<map>`) and for a standalone spec, `impl` for an implementation
        ticket of either, else None. A map's are untyped and have a map the provider resolved; a standalone spec's
        ticket carries the ticket signature and the spec the provider resolved as `lane_spec`."""
        if wayfinder_kinds(self.labels):
            return None
        if self.map_number is not None:
            return "spec" if part_of(self.body, self.team_key) == self.map_number else "impl"
        if self.is_standalone_spec:
            return "spec"
        return "impl" if self.lane_spec is not None and has_ticket_signature(self.body) else None

    @property
    def is_standalone_spec(self) -> bool:
        """The spec signature, no map and no `wayfinder:` label; a body with both signatures is a spec."""
        return self.map_number is None and not wayfinder_kinds(self.labels) and has_spec_signature(self.body)

    @property
    def spec_number(self) -> int | None:
        """The spec an implementation ticket names: its sub-issue parent, else its `## Parent` section."""
        return self.parent_number or parent_ref(self.body, self.team_key)

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


def _heading(title: str) -> re.Pattern[str]:
    """A `##` heading, any case, alone on its line."""
    words = r"[ \t]+".join(map(re.escape, title.split()))
    return re.compile(rf"^##[ \t]*{words}[ \t]*$", re.MULTILINE | re.IGNORECASE)


# The signatures: the heading pairs `/to-spec` and `/to-tickets` write, both of a pair required.
SPEC_SIGNATURE = (_heading("Problem Statement"), _heading("Solution"))
TICKET_SIGNATURE = (_heading("What to build"), _heading("Acceptance criteria"))


def has_spec_signature(body: str) -> bool:
    return all(heading.search(body or "") for heading in SPEC_SIGNATURE)


def has_ticket_signature(body: str) -> bool:
    return all(heading.search(body or "") for heading in TICKET_SIGNATURE)


def wayfinder_kinds(labels: tuple[str, ...]) -> list[str]:
    return [m.group("kind").lower() for label in labels if (m := WAYFINDER_LABEL.match(label))]


@functools.cache
def key_ref(key: str) -> re.Pattern[str]:
    """`<key>-<n>` (Linear's `TM-12`) as a word, so `XTM-12` and another team's `ENG-12` are not it."""
    return re.compile(rf"(?<![\w-]){re.escape(key)}-(\d+)\b")


@functools.cache
def _part_of_key(key: str) -> re.Pattern[str]:
    """`Part of TM-<n>`, which Linear stores as a mention, `[TM-<n>](<issue URL>)`, or the issue's URL."""
    k = re.escape(key)
    return re.compile(rf"^Part of (?:\[?{k}-(?P<key>\d+)\]?(?:\(\S+\))?"
                      rf"|<?https?://\S+?/issue/{k}-(?P<url>\d+)(?:/\S*?)?>?)\.?$")


# How a map's spec and the spec's implementation tickets link back: the spec's first line is `Part of #<map>`, an
# implementation ticket's `## Parent` section names the spec, as `#<n>`, by issue URL, or with the tracker's key as
# Linear's `TM-<n>` (a mention links it, `[TM-<n>](…/issue/TM-<n>/…)`).
_PARENT_SECTION = re.compile(r"^##\s*Parent\s*$(?P<text>.*?)(?=^#{1,2}\s|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)
_PARENT_REF = re.compile(r"(?<![\w&])#(\d+)\b|/issues/(\d+)\b")


def parent_refs(body: str, key: str | None = None) -> list[int]:
    """The issues the body's `## Parent` section names, in order: `#<n>`, issue URLs, and with the tracker's `key`
    (Linear's `TM`) `TM-<n>`."""
    match = _PARENT_SECTION.search(body or "")
    if match is None:
        return []
    text = match.group("text")
    patterns = (_PARENT_REF, key_ref(key)) if key else (_PARENT_REF,)
    found = sorted((ref.start(), int(next(n for n in ref.groups() if n))) for p in patterns for ref in p.finditer(text))
    return [number for _, number in found]


def parent_ref(body: str, key: str | None = None) -> int | None:
    """The issue the body's `## Parent` section names first."""
    return next(iter(parent_refs(body, key)), None)


def part_of(body: str, key: str | None = None) -> int | None:
    """The map number from a body whose first non-blank line is `Part of #<map>`, or with the tracker's `key`
    (Linear's `TM`) `Part of TM-<map>` as the key, a mention or the issue URL."""
    first = next((line.strip() for line in (body or "").splitlines() if line.strip()), "")
    match = PART_OF.match(first) or (_part_of_key(key).match(first) if key else None)
    return int(next(group for group in match.groups() if group)) if match else None


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


def spec_tickets(spec: Ticket, tickets: list[Ticket]) -> list[Ticket]:
    """The spec's implementation tickets: those whose sub-issue parent, else `## Parent` section, is the spec."""
    return [t for t in tickets if t.spec_kind == "impl" and t.spec_number == spec.number]


class SpecProgress(NamedTuple):
    done: int  # closed tickets, less those canceled
    open: int
    blocked: int


def spec_progress(spec: Ticket, tickets: list[Ticket]) -> SpecProgress:
    """The spec's implementation tickets: done (closed, less canceled), open and blocked."""
    children = spec_tickets(spec, tickets)
    still_open = [t for t in children if t.is_open]
    done = sum(not t.is_open and not t.not_planned for t in children)
    return SpecProgress(done, len(still_open), sum(t.is_blocked for t in still_open))


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


class LaneKind(Enum):
    MAP = "map"
    SPEC = "spec"


@dataclass
class Lane:
    """A header issue and its tickets by column. A map lane holds the map's decision tickets and implementation
    tickets; its specs are no cards but lines of the map card, each with its tickets' progress, and `progress` counts
    the decision tickets only. A closed map with no open spec or implementation ticket is collapsed to its header. A
    spec lane holds a standalone spec's tickets, and `progress` counts them."""

    header: Ticket
    kind: LaneKind = LaneKind.MAP
    columns: dict[Column, list[Ticket]] = field(default_factory=lambda: {c: [] for c in COLUMN_ORDER})
    progress: MapProgress | SpecProgress = MapProgress(0, 0, 0)
    out_of_scope: set[int] = field(default_factory=set)
    collapsed: bool = False
    specs: list[Ticket] = field(default_factory=list)
    spec_counts: dict[int, SpecProgress] = field(default_factory=dict)

    @property
    def is_map(self) -> bool:
        return self.kind is LaneKind.MAP

    def tickets(self) -> list[Ticket]:
        """A map lane's specs, then the lane's cards column by column."""
        return [*self.specs, *self.columns_tickets()]

    def columns_tickets(self) -> list[Ticket]:
        """The lane's cards, column by column."""
        return [ticket for tickets in self.columns.values() for ticket in tickets]

    def spec_cards(self) -> list[Ticket]:
        """The specs the lane shows outside its columns: a map card's spec lines, or a spec lane's header."""
        return self.specs if self.is_map else [self.header]

    @property
    def ticketless_spec(self) -> Ticket | None:
        """The first open spec without an implementation ticket, open or closed: the one s on the map card breaks into
        tickets. A spec closed without tickets is passed over."""
        return next((spec for spec in self.specs if spec.is_open and not spec.has_tickets), None)


@dataclass
class Board:
    lanes: list[Lane] = field(default_factory=list)

    def ticket_count(self) -> int:
        """The lanes' ticket cards, not counting the headers."""
        return sum(len(lane.tickets()) for lane in self.lanes)

    def tickets(self) -> list[Ticket]:
        """Every card: each lane's header, then its tickets."""
        return [ticket for lane in self.lanes for ticket in (lane.header, *lane.tickets())]


def is_live_map(map_ticket: Ticket, tickets: list[Ticket]) -> bool:
    """An open map, or a closed one whose spec or implementation tickets are still being worked."""
    return map_ticket.is_open or any(t.is_open and t.spec_kind and t.map_number == map_ticket.number for t in tickets)


def standalone_tickets(spec: Ticket, tickets: list[Ticket]) -> list[Ticket]:
    """The standalone spec's tickets, in spec order."""
    return sorted((t for t in tickets if t.lane_spec == spec.number and t.spec_kind == "impl"),
                  key=lambda t: (t.order, t.number))


def build_board(tickets: list[Ticket], named_map: int | None = None) -> Board:
    """A lane per live map, by number, plus the named map when it is closed; then a lane per standalone spec, by
    number, while the spec or any of its tickets is open. Anything else is not shown."""
    live = {t.number for t in tickets if t.is_wayfinder_map and is_live_map(t, tickets)}
    maps = sorted((t for t in tickets if t.is_wayfinder_map and (t.number in live or t.number == named_map)),
                  key=lambda t: t.number)
    board = Board()
    for map_ticket in maps:
        children = lane_tickets(map_ticket, tickets)
        specs = [t for t in children if t.spec_kind == "spec"]
        lane = Lane(map_ticket, progress=map_progress(map_ticket, tickets),
                    out_of_scope={t.number for t in children if is_out_of_scope(t, map_ticket)},
                    collapsed=map_ticket.number not in live, specs=specs,
                    spec_counts={spec.number: spec_progress(spec, tickets) for spec in specs})
        if not lane.collapsed:
            for ticket in children:
                if ticket.spec_kind != "spec":
                    lane.columns[classify(ticket)].append(ticket)
        board.lanes.append(lane)
    for spec in sorted((t for t in tickets if t.is_standalone_spec), key=lambda t: t.number):
        children = standalone_tickets(spec, tickets)
        if not (spec.is_open or any(t.is_open for t in children)):
            continue
        lane = Lane(spec, LaneKind.SPEC, progress=spec_progress(spec, tickets))
        for ticket in children:
            lane.columns[classify(ticket)].append(ticket)
        board.lanes.append(lane)
    return board


# The section a card's summary prefers: a wayfinder ticket's question, else what a `/to-tickets` ticket builds.
_SUMMARY_SECTIONS = [re.compile(rf"^##\s*{title}\s*$(?P<text>.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)
                     for title in (r"Question", r"What\s+to\s+build")]


def summarize(body: str, max_chars: int = 90) -> str:
    """First meaningful prose of an issue body; prefers a wayfinder `## Question` section, then a `## What to build`
    section."""
    if not body:
        return ""
    match = next((m for pattern in _SUMMARY_SECTIONS if (m := pattern.search(body))), None)
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


def parse_blockers(body: str, key: str | None = None) -> tuple[int, ...]:
    """Issue numbers in this repo that a body's `Blocked by:` lines and `## Blocked by` sections list, in order;
    with the tracker's `key` (Linear's `TM`) as `<key>-<n>`, a mention's link included, else as `#<n>`."""
    ref = key_ref(key) if key else _REF
    refs = [m.group("refs") for pattern in (_BLOCKED_LINE, _BLOCKED_SECTION) for m in pattern.finditer(body or "")]
    return tuple(dict.fromkeys(int(n) for text in refs for n in ref.findall(text)))


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
