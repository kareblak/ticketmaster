"""Provider-neutral ticket model and column classification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import NamedTuple


class Column(Enum):
    BLOCKED = "Blocked"
    READY = "Ready"
    IN_PROGRESS = "In progress"
    REVIEW = "Review"
    DONE = "Done"


COLUMN_ORDER = [Column.BLOCKED, Column.READY, Column.IN_PROGRESS, Column.REVIEW, Column.DONE]

# `review:security` (GitHub/Gitea) and `review::security` (GitLab scoped labels).
REVIEW_LABEL = re.compile(r"^review::?(?P<kind>.+)$", re.IGNORECASE)
BLOCKED_LABEL = "blocked"
# Marks an issue closed from the board as not planned; GitLab and Gitea have no close reason, so it is the marker.
WONTFIX_LABEL = "wontfix"
WAYFINDER_TYPES = ("research", "prototype", "grilling", "task")
# `wayfinder:<kind>` (GitHub/Gitea) and `wayfinder::<kind>` (GitLab scoped labels).
WAYFINDER_LABEL = re.compile(r"^wayfinder::?(?P<kind>[^:].*)$", re.IGNORECASE)
# The child→map rule for trackers without a native link: the body's first line is `Part of #<map>`.
PART_OF = re.compile(r"^Part of #(?P<map>\d+)\.?$")
OUT_OF_SCOPE_LABEL = "out-of-scope"  # pinned by `ticketmaster setup`, always English
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
    # Closed as not planned (GitHub's close reason); trackers without one leave it False.
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

    @property
    def review_tags(self) -> list[str]:
        return [m.group("kind") for label in self.labels if (m := REVIEW_LABEL.match(label))]

    @property
    def other_labels(self) -> list[str]:
        return [label for label in self.labels if not REVIEW_LABEL.match(label)]

    @property
    def is_blocked(self) -> bool:
        if self.open_blockers:
            return True
        return any(label.lower() == BLOCKED_LABEL for label in self.labels)

    @property
    def is_wont_fix(self) -> bool:
        """Closed as not planned or labelled `wontfix`: not finished work, so it stays off Done."""
        return self.not_planned or any(label.lower() == WONTFIX_LABEL for label in self.labels)

    def start_refusal(self, viewer: str | None, holds: dict[str, str] | None = None) -> str | None:
        """Why this ticket must not be started by `viewer`, or None. An unknown viewer is not told apart.

        `holds` maps the triage labels that keep a ticket from an AFK start to why, see triage.hold_labels.
        """
        if self.blocked_by:
            return f"#{self.number} is blocked by {', '.join(self.blocked_by)}"
        if self.open_blockers:
            return f"#{self.number} is blocked by {self.open_blockers} open issue{'s' if self.open_blockers > 1 else ''}"
        if self.is_blocked:
            return f"#{self.number} has the '{BLOCKED_LABEL}' label"
        if viewer and self.assignees and viewer.lower() not in (a.lower() for a in self.assignees):
            return f"#{self.number} is claimed by {', '.join('@' + a for a in self.assignees)}"
        if hold := self.triage_hold(holds or {}):
            reason = next(why for label, why in holds.items() if label.lower() == hold.lower())
            return f"#{self.number} is labelled {hold}: {reason}; not started"
        return None

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
    def map_missing(self) -> bool:
        """A typed ticket whose map the board could not find: it starts neither wayfinder nor implement."""
        return self.wayfinder_type is not None and not self.is_wayfinder_ticket

    @property
    def map_warning(self) -> str:
        return f"wayfinder:{self.wayfinder_type}, map not found"

    @property
    def is_decision(self) -> bool:
        """A wayfinder ticket: it resolves with a comment and closes, never through a PR. The map is not one."""
        return self.wayfinder_type is not None

    @property
    def mode(self) -> str | None:
        """`HITL` or `AFK` for a wayfinder ticket; a task by the first of those words in its body, else None."""
        if self.wayfinder_type == "task":
            match = _MODE_WORD.search(self.body or "")
            return match.group(1) if match else None
        return TYPE_MODES.get(self.wayfinder_type)

    @property
    def is_out_of_scope(self) -> bool:
        """Closed as ruled out of scope, rather than resolved."""
        return not self.is_open and any(label.lower() == OUT_OF_SCOPE_LABEL for label in self.labels)

    def triage_hold(self, holds: dict[str, str]) -> str | None:
        """This ticket's first label among `holds` (label → reason, see triage.hold_labels), or None."""
        wanted = {label.lower() for label in holds}
        return next((label for label in self.labels if label.lower() in wanted), None)

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
    decided: int  # decisions so far, from the map's body
    open: int  # the map's open tickets on the board
    blocked: int


def map_progress(map_ticket: Ticket, tickets: list[Ticket]) -> MapProgress:
    still_open = [t for t in map_tickets(map_ticket, tickets) if t.is_open]
    return MapProgress(decisions_so_far(map_ticket.body), len(still_open), sum(t.is_blocked for t in still_open))


_DECISIONS = re.compile(r"^#+[ \t]*Decisions[- ]so[- ]far[ \t]*:?[ \t]*$(?P<text>.*?)(?=^#+[ \t]|\Z)",
                        re.MULTILINE | re.DOTALL | re.IGNORECASE)
_TOP_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+\S", re.MULTILINE)


def decisions_so_far(body: str) -> int:
    """Top-level bullets in the map's Decisions-so-far section: wayfinder appends one per resolved ticket."""
    match = _DECISIONS.search(body or "")
    return len(_TOP_BULLET.findall(match.group("text"))) if match else 0


@dataclass
class Board:
    columns: dict[Column, list[Ticket]] = field(default_factory=lambda: {c: [] for c in COLUMN_ORDER})

    def total(self) -> int:
        return sum(len(tickets) for tickets in self.columns.values())


def classify(ticket: Ticket, now: datetime, done_days: int) -> Column | None:
    """Return the column a ticket belongs in, or None if it should not be shown."""
    if not ticket.is_open:
        if ticket.is_wont_fix or ticket.closed_at is None or now - ticket.closed_at > timedelta(days=done_days):
            return None
        return Column.DONE
    if (ticket.review_tags or ticket.has_open_change_request) and not ticket.is_decision:
        return Column.REVIEW
    if ticket.is_wayfinder_map:
        return Column.IN_PROGRESS  # a map is worked through its tickets, never started itself
    if ticket.is_blocked:
        return Column.BLOCKED
    if ticket.assignees:
        return Column.IN_PROGRESS
    return Column.READY


def build_board(tickets: list[Ticket], done_days: int = 7, now: datetime | None = None) -> Board:
    now = now or datetime.now(timezone.utc)
    board = Board()
    for ticket in tickets:
        column = classify(ticket, now, done_days)
        if column is not None:
            board.columns[column].append(ticket)
    for column, items in board.columns.items():
        if column is Column.DONE:
            items.sort(key=lambda t: t.closed_at or now, reverse=True)
        else:
            items.sort(key=lambda t: (t.order, t.number))
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
