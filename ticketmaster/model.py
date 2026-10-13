"""Provider-neutral ticket model and column classification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


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
MAP_LABEL = "wayfinder:map"


@dataclass(frozen=True)
class Ticket:
    number: int
    title: str
    url: str
    is_open: bool
    body: str = ""
    closed_at: datetime | None = None
    assignees: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    # Open issues blocking this one; None when the provider cannot tell.
    open_blockers: int | None = None
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
    def is_wayfinder_map(self) -> bool:
        return MAP_LABEL in self.labels

    @property
    def is_wayfinder_ticket(self) -> bool:
        has_type = any(label.startswith("wayfinder:") and label != MAP_LABEL for label in self.labels)
        return has_type and MAP_LABEL in self.parent_labels and self.parent_url is not None

    @property
    def summary(self) -> str:
        return summarize(self.body)


@dataclass
class Board:
    columns: dict[Column, list[Ticket]] = field(default_factory=lambda: {c: [] for c in COLUMN_ORDER})

    def total(self) -> int:
        return sum(len(tickets) for tickets in self.columns.values())


def classify(ticket: Ticket, now: datetime, done_days: int) -> Column | None:
    """Return the column a ticket belongs in, or None if it should not be shown."""
    if not ticket.is_open:
        if ticket.closed_at is None or now - ticket.closed_at > timedelta(days=done_days):
            return None
        return Column.DONE
    if ticket.review_tags or ticket.has_open_change_request:
        return Column.REVIEW
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


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
