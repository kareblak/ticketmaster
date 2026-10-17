"""The board behind every view: fetching and pacing, lanes, filters, settings, sessions and the start flow, as a
semantic snapshot each view draws its own way. It imports neither Textual nor Rich.

The model is not thread-safe. Each view calls it from one owner thread and owns the clock and the threads; only
`fetch`, `map_order`, `start` and `go_to`, which are pure I/O, run off it.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, Protocol

from .config import (DEFAULT_BUDGET, board_budget, board_decided_age, board_fetch_limit, board_interval, load_settings,
                     parse_budget, parse_decided_age, parse_fetch_limit, parse_interval, seconds_text, update_settings)
from .backends import BackendError
from .session import Session
from .model import (COLUMN_ORDER, WAYFINDER_TYPES, Board, Column, Lane, LaneKind, SpecProgress, Ticket, build_board,
                    frontier)
from .providers import ErrorNotice, Provider, ProviderError, Query, Quota, RateLimited, error_notice
from .start import branch_name, worktree_name

LOOSE_TITLE = "Loose tickets"
NO_ISSUE = "The Loose lane has no issue; select one of its tickets"
LIMITED_TICKETS_EVERY = 60.0  # ticket fetches while the tracker rate-limits the board
LIMITED_SESSIONS_EVERY = 10.0  # herdr session refreshes meanwhile
STARTING_SECONDS = 30  # a starting marker older than this is ignored

INCLUDE = "include"  # a selected picker value
TYPE, MODE, ASSIGNEE, MAP, AUTHOR = "Type", "HITL/AFK", "Assignee", "Map", "Author"  # the picker's sections
CARD_FILTERS = (TYPE, MODE, ASSIGNEE, AUTHOR)  # narrow a lane's cards; MAP narrows the lanes
SAVED_KEYS = {TYPE: "types", MODE: "modes", ASSIGNEE: "assignee", MAP: "maps", AUTHOR: "authors"}  # settings.json
CARD_TYPES = (*WAYFINDER_TYPES, "spec", "impl")
MODES = ("HITL", "AFK")
ASSIGNEES, ANYONE = ("me", "unassigned", "anyone"), "anyone"  # one at a time; anyone narrows nothing


def lane_title(lane: Lane) -> str:
    """What a lane's cards name it by: its header issue's title, or the Loose lane's own."""
    return lane.header.title if lane.header else LOOSE_TITLE


def matches(ticket: Ticket, needle: str) -> bool:
    if not needle:
        return True
    haystack = " ".join([str(ticket.number), ticket.title, ticket.body, ticket.author, *ticket.labels, *ticket.assignees]).lower()
    return all(word in haystack for word in needle.lower().split())


def card_type(ticket: Ticket) -> str | None:
    """The type a card is filtered by: its wayfinder type, else spec or impl."""
    return ticket.wayfinder_type or ticket.spec_kind


def passes_filters(ticket: Ticket, filters: dict[str, dict[str, str]], known: dict[str, set[str]],
                   viewer: str | None) -> bool:
    """A card passes every section with a selection, and a section when it matches any selected value. A selected type,
    mode or author missing from the board (`known`) is ignored; `me` is the tracker login, nobody while it is unknown."""
    for section, value in ((TYPE, card_type(ticket)), (MODE, ticket.mode), (AUTHOR, ticket.author)):
        chosen = known.get(section, set()).intersection(filters.get(section, {}))
        if chosen and value not in chosen:
            return False
    assignee = next(iter(filters.get(ASSIGNEE, {})), ANYONE)
    if assignee == "unassigned":
        return not ticket.assignees
    if assignee == "me":
        return viewer is not None and viewer.lower() in (a.lower() for a in ticket.assignees)
    return True


def known_values(board: Board) -> dict[str, set[str]]:
    """The types, modes and authors the lanes' cards carry, a spec lane's header included, which the picker offers."""
    cards = [ticket for lane in board.lanes for ticket in (*lane.spec_cards(), *lane.columns_tickets())]
    return {
        TYPE: {kind for t in cards if (kind := card_type(t))},
        MODE: {t.mode for t in cards if t.mode},
        AUTHOR: {t.author for t in cards if t.author},
    }


_MAP_KEY = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")  # a map picked by its tracker key, e.g. Linear's `TM-1`


def map_value(ticket: Ticket) -> str:
    """How the map filter names a map: its tracker key, else its number."""
    return ticket.key.upper() if ticket.key else str(ticket.number)


def map_ids(ticket: Ticket) -> set[str]:
    """Every map filter value that selects this map: its number, and its tracker key when it has one."""
    return {str(ticket.number), map_value(ticket)}


def load_filters(saved: dict) -> dict[str, dict[str, str]]:
    """A repo's saved selection; a value that cannot be one (a map that is no number or key, an unknown assignee) is
    dropped."""
    filters = {}
    for section, key in SAVED_KEYS.items():
        values = [str(v) for v in saved.get(key, [])] if isinstance(saved.get(key), list) else []
        if section == MAP:
            values = [v for v in values if v.isdigit() or _MAP_KEY.match(v)]
        elif section == ASSIGNEE:
            values = [v for v in values if v in ASSIGNEES and v != ANYONE][:1]
        filters[section] = dict.fromkeys(values, INCLUDE)
    return filters


def aged_out(ticket: Ticket, days: int | None, now: datetime) -> bool:
    """A closed ticket closed more than `days` ago, which the Decided age hides; never an open ticket or one without a
    close date, and nothing while the age is off (None)."""
    return (days is not None and not ticket.is_open and ticket.closed_at is not None
            and now - ticket.closed_at > timedelta(days=days))


def budget_pace(quota: Quota, budget: float, now: datetime) -> int:
    """Whole seconds between ticket fetches that spend at most `budget` points an hour at the last fetch's cost, and
    longer when the points left would run out before they reset at that pace; 0 for a fetch that cost nothing."""
    if quota.cost <= 0:
        return 0
    pace = quota.cost * 3600 / budget
    if quota.remaining is not None and quota.resets_at is not None:
        until_reset = (quota.resets_at - now).total_seconds()
        if until_reset > 0:
            pace = max(pace, until_reset if quota.remaining <= 0 else quota.cost * until_reset / quota.remaining)
    return math.ceil(pace)


class Pacer:
    """When the board fetches tickets and herdr sessions, given the clock's `now`.

    Normally both every `interval`; a pace (GitHub's budget, see budget_pace) longer than that stretches the ticket
    fetches to it, never the session refreshes. A rate limit slows tickets to once a minute and sessions to every 10s;
    the first good fetch after that restores the interval, once. A rate limit after that recovery keeps
    the board slow until it restarts.
    """

    def __init__(self, interval: float, now: float):
        self.interval = interval
        self.limited = False  # backed off right now
        self.recovered = False  # the one recovery is used up
        self.stuck = False  # slow until restart
        self.paced = 0.0  # seconds between ticket fetches the budget asks for; under `interval` it changes nothing
        self.next_tickets = now
        self.next_sessions = now
        self.last_tickets = self.last_sessions = now - interval  # when each fetch last started

    def set_interval(self, interval: float) -> None:
        """A new interval from the settings menu: the next fetches are due that long after the last ones started.
        A rate-limit back-off stays: tickets keep its minute, sessions its 10s at least."""
        self.interval = interval
        self._reschedule_tickets()
        self.next_sessions = self.last_sessions + self._sessions_every()

    def pace(self, seconds: float) -> None:
        """A new pace from the latest quota reading: the next ticket fetch is due `tickets_every` after the last one
        started. A rate-limit back-off stays."""
        self.paced = float(seconds)  # a float, as `seconds_text` shows it
        self._reschedule_tickets()

    def _reschedule_tickets(self) -> None:
        if not self.limited:
            self.next_tickets = self.last_tickets + self.tickets_every

    @property
    def tickets_every(self) -> float:
        """Seconds between ticket fetches outside a back-off."""
        return max(self.interval, self.paced)

    def _sessions_every(self) -> float:
        return max(LIMITED_SESSIONS_EVERY, self.interval) if self.limited else self.interval

    def due(self, now: float) -> tuple[bool, bool]:
        """(tickets, sessions) to fetch now; a ticket fetch refreshes sessions too."""
        tickets = now >= self.next_tickets
        return tickets, tickets or now >= self.next_sessions

    def started(self, now: float, tickets: bool) -> None:
        self.last_sessions = now
        if tickets:
            self.last_tickets = now
            self.next_tickets = now + (LIMITED_TICKETS_EVERY if self.limited else self.tickets_every)
        self.next_sessions = now + self._sessions_every()

    def fetched(self, now: float, ok: bool, rate_limited: bool) -> None:
        if rate_limited:
            self.stuck = self.stuck or self.recovered
            self.limited = True
            self.next_tickets = now + LIMITED_TICKETS_EVERY
        elif ok and self.limited and not self.stuck:
            self.limited, self.recovered = False, True
            self.next_tickets = min(self.next_tickets, now + self.tickets_every)
            self.next_sessions = min(self.next_sessions, now + self.interval)

    def wait(self, now: float) -> int:
        """Whole seconds until the next ticket fetch."""
        return max(0, math.ceil(self.next_tickets - now))


class Workbench(Protocol):
    """The session side of the board, see start.Workbench. Every call blocks."""

    def sessions(self) -> dict[int, list[Session]]: ...
    def start(self, ticket: Ticket) -> str: ...
    def go_to(self, ticket: Ticket, session: Session) -> str: ...


# --- what the model hands a view ------------------------------------------------------------------


class Notice(NamedTuple):
    """A message for the view to show; `severity` is information, warning or error."""

    text: str
    severity: str = "information"


@dataclass(frozen=True)
class Badge:
    """What a card says about its sessions: one per `session` (its agent `status`, None without one, and its
    `worktree`), or that the board is `starting` one, or that the ticket is `claimed` with no session here."""

    kind: str
    status: str | None = None
    worktree: str = ""


@dataclass(frozen=True)
class Chip:
    """One of a card's tags: `type` (its wayfinder type, or spec/impl), `mode` (HITL/AFK), `pr` (an open closing
    PR), `blockers` (its open blockers' refs, or their count), `assignees` (space-separated logins) or `lane` (its
    lane's title)."""

    kind: str
    text: str = ""


@dataclass(frozen=True)
class CardView:
    """A ticket's card: its badges and chips; a Decided card has neither, only its ref and title."""

    ticket: Ticket
    column: Column
    badges: tuple[Badge, ...] = ()
    chips: tuple[Chip, ...] = ()
    lane_kind: LaneKind = LaneKind.MAP

    @property
    def ref(self) -> str:
        return self.ticket.ref

    @property
    def title(self) -> str:
        return self.ticket.title

    @property
    def summary(self) -> str:
        return self.ticket.summary


@dataclass(frozen=True)
class SpecLine:
    """A map header's line for one of its specs: its counts, None while it has no tickets yet, and its badges."""

    spec: Ticket
    counts: SpecProgress | None
    badges: tuple[Badge, ...] = ()


@dataclass(frozen=True)
class HeaderView:
    """A lane's header: the lane (its kind, header issue and progress), its badges, and per map spec its line."""

    lane: Lane
    badges: tuple[Badge, ...] = ()
    specs: tuple[SpecLine, ...] = ()

    @property
    def ticket(self) -> Ticket | None:
        return self.lane.header


@dataclass(frozen=True)
class LaneView:
    header: HeaderView
    cells: tuple[tuple[Column, tuple[CardView, ...]], ...]

    @property
    def collapsed(self) -> bool:
        return self.lane.collapsed

    @property
    def lane(self) -> Lane:
        return self.header.lane


class Empty(NamedTuple):
    """Why a loaded board shows no lane: every card is `filtered` away, or the tracker has no board work."""

    text: str
    filtered: bool


class Segment(NamedTuple):
    """A piece of the status line; `style` is title, warn, filter, error or empty."""

    text: str
    style: str = ""


@dataclass(frozen=True)
class Snapshot:
    """The board as the filters show it: its lanes, each column's card count, and why it is empty."""

    lanes: tuple[LaneView, ...]
    counts: dict[Column, int]
    empty: Empty | None = None


def session_badges(sessions: list[Session], starting: bool, claimed: bool = False) -> tuple[Badge, ...]:
    """A badge per session; for a claimed ticket without one, that the claim's session is not in this herdr."""
    badges = [Badge("session", session.status, Path(session.path).name) for session in sessions]
    if starting and not sessions:
        badges.append(Badge("starting"))
    elif claimed and not sessions:
        badges.append(Badge("claimed"))
    return tuple(badges)


def card_view(ticket: Ticket, column: Column, sessions: list[Session] = (), starting: bool = False,
              lane_title: str = "", claimed: bool = False, lane_kind: LaneKind = LaneKind.MAP) -> CardView:
    """A ticket's card: type (or spec/impl) and HITL/AFK, an open closing PR, open blockers, assignees and its lane's
    title. A Decided ticket is only its ref and title."""
    if column is Column.DECIDED:
        return CardView(ticket, column, lane_kind=lane_kind)
    chips = []
    if kind := card_type(ticket):
        chips.append(Chip("type", kind))
        if ticket.wayfinder_type and ticket.mode:
            chips.append(Chip("mode", ticket.mode))
    if ticket.is_open and ticket.has_open_change_request:
        chips.append(Chip("pr"))
    if ticket.is_open and (ticket.blocked_by or ticket.open_blockers):
        chips.append(Chip("blockers", " ".join(ticket.blocked_by) or str(ticket.open_blockers)))
    if ticket.assignees:
        chips.append(Chip("assignees", " ".join(ticket.assignees)))
    if lane_title:
        chips.append(Chip("lane", lane_title))
    return CardView(ticket, column, session_badges(sessions, starting, claimed), tuple(chips), lane_kind)


# --- the start flow --------------------------------------------------------------------------------


class StartPlan(NamedTuple):
    """The ticket a start begins for a card, and how the confirm names it (`via` a map)."""

    ticket: Ticket
    via: str = ""


class Refusal(NamedTuple):
    """Why a card starts nothing; `live` when that is its live session, which the view says how to go to."""

    text: str
    live: bool = False


class ConfirmText(NamedTuple):
    question: str
    detail: str
    yes: str


class GoTo(NamedTuple):
    """The session the primary action goes to."""

    ticket: Ticket
    session: Session


class Hint(NamedTuple):
    """A card with no session: what a start would do there, or why it refuses."""

    ticket: Ticket
    plan: StartPlan | Refusal


# --- fetching --------------------------------------------------------------------------------------


class FetchPlan(NamedTuple):
    """One fetch: herdr sessions always, tickets too when `tickets`; `sessions` stand while herdr fails."""

    tickets: bool
    query: Query
    viewer: bool  # the tracker login is still unknown, so ask for it first
    sessions: dict[int, list[Session]]


@dataclass
class FetchResult:
    plan: FetchPlan
    sessions: dict[int, list[Session]]
    session_error: str | None = None
    board: Board | None = None
    error: ProviderError | None = None
    rate_limited: bool = False
    cut_off: str | None = None
    quota: Quota | None = None
    viewer: str | None = None


class Applied(NamedTuple):
    """What a fetch's result changed: whether the board did (else only the status line), and a fetch asked for
    while it ran, now due."""

    changed: bool
    next: FetchPlan | None


class Setting(NamedTuple):
    """One field of the settings menu: its key in the repo's settings, its label, its current value as text, and
    `parse`, which turns the typed text into the value or raises ValueError saying what is wrong."""

    key: str
    label: str
    value: str
    parse: Callable[[str], object]


class SettingsApplied(NamedTuple):
    """What applying the settings changed: the interval (the view reschedules its ticker), the cards shown (the view
    redraws), and a notice when the save failed."""

    interval: bool
    board: bool
    notice: Notice | None


@dataclass(eq=False)
class BoardModel:
    """One view's board. `query.named_map`, or `map_key` (a tracker key such as `TM-1`), presets the map filter for
    this run. `interval` is `--interval`, which wins over the repo's saved interval for this run without replacing
    it. `clock` schedules the fetches; `wall`, the time of day, tells how long until a tracker quota resets and how old
    a starting marker is. `starting_dir` holds the repo's starting markers, shared by every view of it."""

    provider: Provider
    query: Query
    interval: float | None = None
    workbench: Workbench | None = None
    describe_start: Callable[[Ticket], str] | None = None
    settings_path: Path | None = None  # None: settings live only as long as the board
    clock: Callable[[], float] = time.monotonic
    map_key: str | None = None
    wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    starting_dir: Path | None = None
    notices: list[Notice] = field(default_factory=list)  # tracker error episodes, for the view to drain

    def __post_init__(self):
        query, provider = self.query, self.provider
        self.describe_start = self.describe_start or (lambda ticket: "")
        self.own_starting: set[int] = set()  # started from this board, until herdr lists its agent or the start fails
        self.marked: set[int] = set()  # started by any view of the repo in the last STARTING_SECONDS
        self.sessions: dict[int, list[Session]] = {}
        self.settings = load_settings(self.settings_path) if self.settings_path else {}
        self.session_error: str | None = None
        self.repo_key = f"{provider.name}:{provider.repo}"
        self.board_query = replace(query, limit=board_fetch_limit(self._repo_settings()))
        self.cut_off: str | None = None  # what the last ticket fetch cut off at the fetch limit
        self.interval = board_interval(self.interval, self._repo_settings(), provider.name)
        self.pacer = Pacer(self.interval, self.clock())
        self.budget = board_budget(self._repo_settings())  # GitHub points an hour, see budget_pace
        self.decided_age = board_decided_age(self._repo_settings())  # days; None shows every Decided ticket
        self.quota: Quota | None = None  # the provider's reading after the last good ticket fetch; GitHub only
        self.fetching = False
        self.queued: bool | None = None  # a refresh asked for mid-fetch: whether it includes tickets
        self.board = Board()
        self.viewer: str | None = None  # the tracker login; asked for with each ticket fetch until known
        self.needle = ""
        saved = self.settings.get("filters", {}).get(self.repo_key, {})
        self.filters = load_filters(saved if isinstance(saved, dict) else {})
        self.saved_maps = dict(self.filters[MAP])  # what the map section saves while the preset stands
        named = self.map_key.upper() if self.map_key else str(query.named_map) if query.named_map is not None else None
        # `ticketmaster <map>`: the full board, filtered to that map for this run only; never saved
        self.map_preset = {named: INCLUDE} if named else None
        if self.map_preset:
            self.filters[MAP] = dict(self.map_preset)
        self.known: dict[str, set[str]] = {}  # the filter values on the board, see known_values
        self.last_error: str | None = None
        self.notice: ErrorNotice | None = None  # the tracker error standing since the last good fetch
        self.last_ok: datetime | None = None
        named = f" + #{query.named_map}" if query.named_map is not None else ""
        self.sub_title = f"{provider.name}:{provider.repo} · open maps{named}"

    # --- fetching ----------------------------------------------------------------------------------

    @property
    def limited(self) -> bool:
        """The tracker rate-limits the board: tickets come once a minute."""
        return self.pacer.limited

    def due(self) -> FetchPlan | None:
        """The fetch the pacer says is due now, counted as started; None while one runs or none is due."""
        tickets, sessions = self.pacer.due(self.clock())
        return self._begin(tickets) if sessions and not self.fetching else None

    def refresh(self) -> FetchPlan | None:
        """A fetch now, except tickets while rate-limited: then only herdr sessions refresh. Mid-fetch it is queued
        for when the fetch in flight lands (see Applied.next)."""
        tickets = not self.pacer.limited
        if self.fetching:
            self.queued = tickets or bool(self.queued)
            return None
        return self._begin(tickets)

    def refresh_notice(self) -> Notice | None:
        """What a refresh asked for by hand says: how long a rate limit holds tickets back, or the standing error."""
        if self.pacer.limited:
            reason = f" ({self.notice.reason})" if self.notice else ""
            return Notice(f"Rate-limited{reason}: next ticket fetch in {self.pacer.wait(self.clock())}s", "warning")
        if self.notice:
            return Notice(self.notice.text, self.notice.severity)
        return None

    def _begin(self, tickets: bool) -> FetchPlan:
        self.fetching = True  # one fetch at a time; the pacer schedules the next when this one started
        self.pacer.started(self.clock(), tickets)
        return FetchPlan(tickets, self.board_query, self.viewer is None, self.sessions)

    def fetch(self, plan: FetchPlan) -> FetchResult:
        """Runs the plan's tracker and herdr calls, off the owner thread; touches no state."""
        result = FetchResult(plan, plan.sessions)
        if self.workbench is not None:
            try:
                result.sessions = self.workbench.sessions()
            except BackendError as exc:
                result.session_error = f"{exc.source}: {exc}"
        if not plan.tickets:
            return result
        try:
            if plan.viewer:
                result.viewer = self._viewer()
            result.board = build_board(self.provider.fetch(plan.query), plan.query.named_map)
            cut = getattr(self.provider, "cut_off", [])  # what the fetch cut off at the limit, when it says
            result.cut_off = f"cut off at {plan.query.limit}: {', '.join(cut)}" if cut else None
            result.quota = getattr(self.provider, "quota", None)  # GitHub's, after this fetch
        except RateLimited as exc:
            explain = getattr(self.provider, "explain", None)  # GitHub can say which limit it was
            result.error, result.rate_limited = explain(exc) if explain else exc, True
        except ProviderError as exc:
            result.error = exc
        return result

    def _viewer(self) -> str | None:
        try:
            return self.provider.viewer()
        except RateLimited:
            raise
        except ProviderError:
            return None  # claimed tickets then start after the confirm, as before the login was known

    def fetch_failed(self) -> None:
        """A fetch that raised past `fetch`: the next one may start."""
        self.fetching = False

    def apply(self, result: FetchResult) -> Applied:
        """Takes a fetch's result in; a new tracker error episode queues a notice (see drain_notices)."""
        self.fetching = False
        if result.viewer is not None:
            self.viewer = result.viewer
        if result.plan.tickets:
            self.pacer.fetched(self.clock(), ok=result.board is not None, rate_limited=result.rate_limited)
            self._tracker_error(result.error)
            if result.quota is not None:
                self.quota = result.quota
            self._pace()
        queued = None
        if self.queued is not None:
            tickets, self.queued = self.queued and not self.pacer.limited, None
            queued = self._begin(tickets)
        changed = False
        if result.board is not None:
            changed = result.board.lanes != self.board.lanes or self.last_ok is None
            self.board = result.board
            self.cut_off = result.cut_off
            self.known = known_values(result.board)
            self.last_ok = datetime.now()
        sessions = result.sessions
        if sessions != self.sessions:
            live = {n for n in self.own_starting | self.marked if any(s.status is not None for s in sessions.get(n, []))}
            self.own_starting -= live
            for number in live:
                self._unmark(number)
            self.sessions = sessions
            changed = True
        self.session_error = result.session_error
        return Applied(changed, queued)

    def drain_notices(self) -> list[Notice]:
        notices, self.notices = self.notices, []
        return notices

    def _pace(self) -> None:
        """Paces the ticket fetches to the budget at the last quota reading; without one, they keep the interval."""
        if self.quota is not None:
            self.pacer.pace(budget_pace(self.quota, self.budget, self.wall()))

    def _tracker_error(self, error: ProviderError | None) -> None:
        """One notice per error episode: when an error appears, or turns into another; a good fetch ends it."""
        notice = None if error is None else error_notice(self.provider.name, error)
        if notice is not None and (self.notice is None or self.notice.episode != notice.episode):
            self.notices.append(Notice(notice.text, notice.severity))
        self.notice = notice
        self.last_error = None if notice is None or isinstance(error, RateLimited) else notice.status_text

    # --- starting markers --------------------------------------------------------------------------

    @property
    def starting(self) -> set[int]:
        """Tickets a session is starting for: from this board, or from any view of the repo in the last
        STARTING_SECONDS."""
        return self.own_starting | self.marked

    def read_markers(self) -> bool:
        """Reads the repo's starting markers, ignoring those older than STARTING_SECONDS; whether that changed what is
        starting."""
        if self.starting_dir is None:
            return False
        now, marked = self.wall().timestamp(), set()
        try:
            markers = list(self.starting_dir.iterdir())
        except OSError:
            markers = []
        for marker in markers:
            try:
                started = float(marker.read_text())
            except (OSError, ValueError):
                continue
            if marker.name.isdigit() and now - started <= STARTING_SECONDS:
                marked.add(int(marker.name))
        before = self.starting
        self.marked = marked
        return self.starting != before

    def _mark(self, number: int) -> bool:
        """Writes the ticket's marker, created exclusively so two boards confirming at once cannot both start it;
        False when a fresh marker is already there. A stale one is replaced."""
        if self.starting_dir is None:
            return True
        marker = self.starting_dir / str(number)
        try:
            self.starting_dir.mkdir(parents=True, exist_ok=True)
            try:
                with marker.open("x") as out:
                    out.write(f"{self.wall().timestamp()}\n")
            except FileExistsError:
                self.read_markers()
                if number in self.marked:
                    return False
                marker.write_text(f"{self.wall().timestamp()}\n")
        except OSError:
            pass  # this board still knows; other views find out from herdr
        return True

    def _unmark(self, number: int) -> None:
        self.marked.discard(number)
        if self.starting_dir is not None:
            try:
                (self.starting_dir / str(number)).unlink(missing_ok=True)
            except OSError:
                pass

    # --- lanes and filters -------------------------------------------------------------------------

    def visible(self, lane: Lane, column: Column) -> list[Ticket]:
        """The column's cards that pass the filters, less those the Decided age hides."""
        now = self.wall()
        return [t for t in lane.columns[column] if self.passes(t) and not aged_out(t, self.decided_age, now)]

    def chosen(self, section: str) -> list[str]:
        """The section's selected values that are on the board, in the picker's order. The preset map counts even
        when no lane is that map, so a map that is not on the board shows an empty board, not every map."""
        if section == ASSIGNEE:
            return list(self.filters[ASSIGNEE])
        selected = self.filters[section]
        chosen = [value for value, _ in self.picker_values(section) if value in selected]
        if section == MAP:
            on_board = {value for lane in self.board.lanes if lane.is_map for value in map_ids(lane.header)}
            preset = self.map_preset is not None and selected == self.map_preset
            chosen += [value for value in selected if value not in chosen and (preset or value in on_board)]
        return chosen

    def shown_lanes(self) -> list[Lane]:
        """The lanes the map filter keeps, which are map lanes only; while cards are filtered, only those with a card
        that passes (a spec lane's header is its spec card), and a named closed map, which has no cards to pass."""
        maps = set(self.chosen(MAP))
        lanes = [lane for lane in self.board.lanes if not maps or lane.is_map and maps & map_ids(lane.header)]
        if self.needle or any(self.filters[section] for section in CARD_FILTERS):
            lanes = [lane for lane in lanes if lane.collapsed or any(self.passes(spec) for spec in lane.spec_cards())
                     or any(self.visible(lane, column) for column in COLUMN_ORDER)]
        return lanes

    def passes(self, ticket: Ticket) -> bool:
        return matches(ticket, self.needle) and passes_filters(ticket, self.filters, self.known, self.viewer)

    def lane(self, header: Ticket) -> Lane | None:
        return next((lane for lane in self.board.lanes if lane.header and lane.header.number == header.number), None)

    def claimed(self, ticket: Ticket) -> bool:
        """Open and assigned; only herdr can tell whether its session is here, so without it nothing is said."""
        return self.workbench is not None and ticket.is_open and bool(ticket.assignees)

    def picker_values(self, section: str) -> list[tuple[str, str]]:
        """(value, label) for each of the section's options, from the board's tickets."""
        if section == ASSIGNEE:
            return [(value, value) for value in ASSIGNEES]
        if section == MAP:
            return [(map_value(lane.header), f"{lane.header.ref} {lane.header.title}") for lane in self.board.lanes
                    if lane.is_map]
        known = self.known.get(section, set())
        if section == AUTHOR:
            return [(author, author) for author in sorted(known, key=str.lower)]
        order = CARD_TYPES if section == TYPE else MODES
        return [(value, value) for value in order if value in known]

    def picker_sections(self) -> dict[str, list[tuple[str, str]]]:
        return {section: self.picker_values(section) for section in SAVED_KEYS}

    def clear_filters(self) -> Notice | None:
        """Clears the text filter and every picker section, saving the cleared selection."""
        self.needle = ""
        if any(self.filters.values()):
            for selection in self.filters.values():
                selection.clear()
            return self.save_filters()
        return None

    def save_filters(self) -> Notice | None:
        """Saves the picker's selection per repo; a saved value no longer on the board stays saved. While the map
        preset stands, the map section keeps what was saved before; a map number is saved as a number."""
        if self.map_preset is None or self.filters[MAP] != self.map_preset:
            self.saved_maps = dict(self.filters[MAP])
        selection = {**self.filters, MAP: self.saved_maps}
        saved = {key: [int(v) if v.isdigit() else v for v in sorted(selection[section], key=lambda v: (len(v), v))]
                 if section == MAP else sorted(selection[section]) for section, key in SAVED_KEYS.items()}

        def change(settings: dict) -> None:
            if not isinstance(settings.get("filters"), dict):
                settings["filters"] = {}
            settings["filters"][self.repo_key] = saved

        return self._write_settings("filter", change)

    def _write_settings(self, what: str, change: Callable[[dict], None]) -> Notice | None:
        """Makes `change` here and in settings.json, re-read first so keys another view saved stay; a failure is a
        warning naming `what` was not saved."""
        change(self.settings)
        if self.settings_path:
            try:
                update_settings(self.settings_path, change)
            except OSError as exc:
                return Notice(f"{what} not saved: {exc}", "warning")
        return None

    # --- settings ----------------------------------------------------------------------------------

    def settings_fields(self) -> list[Setting]:
        fields = [Setting("interval", "Refresh interval (seconds)", seconds_text(self.interval), parse_interval),
                  Setting("fetch_limit", "Fetch limit (open maps, and tickets per map)", str(self.board_query.limit),
                          parse_fetch_limit),
                  Setting("decided_age", "Decided age (days; empty shows every Decided ticket)",
                          "" if self.decided_age is None else str(self.decided_age), parse_decided_age)]
        if self.provider.name == "github":
            fields.append(Setting("budget", "GitHub budget (GraphQL points an hour)", seconds_text(self.budget),
                                  parse_budget))
        return fields

    def apply_settings(self, values: dict) -> SettingsApplied:
        """Applies the menu's values, the interval and the Decided age at once and the fetch limit from the next
        fetch, and saves those that differ from the repo's saved ones, also a value that `--interval` already set for
        this run, but not an unchanged default fetch limit; an empty Decided age removes the saved one."""
        interval, limit, budget = values["interval"], values["fetch_limit"], values.get("budget", self.budget)
        interval_changed = interval != self.interval
        if interval_changed:
            self.interval = interval
            self.pacer.set_interval(interval)
        if interval_changed or budget != self.budget:
            self.budget = budget
            self._pace()
        saved = {"interval": int(interval) if interval.is_integer() else interval}
        if limit != self.board_query.limit or "fetch_limit" in self._repo_settings():
            self.board_query = replace(self.board_query, limit=limit)
            saved["fetch_limit"] = limit
        if "budget" in values and (budget != DEFAULT_BUDGET or "budget" in self._repo_settings()):
            saved["budget"] = int(budget) if budget.is_integer() else budget  # an untouched default stays a default
        age = values["decided_age"]
        board_changed = age != self.decided_age
        self.decided_age = age
        stale = age is None and "decided_age" in self._repo_settings()
        if age is not None:
            saved["decided_age"] = age
        notice = None
        changed = {key: value for key, value in saved.items() if self._repo_settings().get(key) != value}
        if stale or changed:
            def change(settings: dict) -> None:  # only what changed, so a key another board saved stays
                repo = self._repo_settings(create=True, settings=settings)
                repo.update(changed)
                if stale:
                    repo.pop("decided_age", None)

            notice = self._write_settings("settings", change)
        return SettingsApplied(interval_changed, board_changed, notice)

    def _repo_settings(self, create: bool = False, settings: dict | None = None) -> dict:
        """This repo's entry under `repos` in `settings` (the board's own by default); `create` makes it, replacing
        one that is no dict."""
        settings = self.settings if settings is None else settings
        repos = settings.get("repos")
        if not isinstance(repos, dict):
            if not create:
                return {}
            repos = settings["repos"] = {}
        saved = repos.get(self.repo_key)
        if not isinstance(saved, dict):
            if not create:
                return {}
            saved = repos[self.repo_key] = {}
        return saved

    # --- the snapshot ------------------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        starting, lanes = self.starting, []
        for lane in self.shown_lanes():
            lanes.append(LaneView(self.header_view(lane, starting), tuple(
                (column, tuple(card_view(t, column, self.sessions.get(t.number, []), t.number in starting,
                                         lane_title(lane), self.claimed(t), lane.kind)
                               for t in self.visible(lane, column)))
                for column in COLUMN_ORDER)))
        counts = {column: sum(len(cards) for lane in lanes for col, cards in lane.cells if col is column)
                  for column in COLUMN_ORDER}
        empty = None
        if not lanes and self.last_ok:
            empty = (Empty("No cards match the filters", True) if self.board.lanes
                     else Empty(f"No open wayfinder maps or specs in {self.provider.repo}.", False))
        return Snapshot(tuple(lanes), counts, empty)

    def header_view(self, lane: Lane, starting: set[int]) -> HeaderView:
        """A lane's header: its sessions' badges and, on a map, a line per spec with its tickets' counts."""
        head = lane.header
        if head is None:
            return HeaderView(lane)
        specs = []
        for spec in lane.specs if lane.is_map else ():
            counts = lane.spec_counts.get(spec.number, SpecProgress(0, 0, 0))
            specs.append(SpecLine(spec, counts if spec.has_tickets or any(counts) else None,
                                  session_badges(self.sessions.get(spec.number, []), spec.number in starting)))
        return HeaderView(lane, session_badges(self.sessions.get(head.number, []), head.number in starting,
                                               self.claimed(head)), tuple(specs))

    def status(self) -> list[Segment]:
        segments = [Segment(self.sub_title, "title")]
        if self.last_ok:
            maps = sum(lane.is_map for lane in self.board.lanes)
            specs = sum(lane.kind is LaneKind.SPEC for lane in self.board.lanes)
            tickets = self.board.ticket_count()
            text = (f" · updated {self.last_ok:%H:%M:%S} · {maps} map{'s' * (maps != 1)}"
                    + (f" · {specs} spec{'s' * (specs != 1)}" if specs else "") + f" · {tickets} tickets")
            if not self.pacer.limited:
                text += f" · every {seconds_text(self.pacer.tickets_every)}s"
                if self.quota is not None and self.quota.remaining is not None:
                    text += f" · {self.quota.remaining:,} pts left"
            segments.append(Segment(text))
            if self.cut_off:
                segments.append(Segment(f" · {self.cut_off}", "warn"))
        if self.pacer.limited:
            slow = "rate-limited again · once a minute until restart" if self.pacer.stuck else "rate-limited"
            reason = f" · {self.notice.reason}" if self.notice else ""
            segments.append(Segment(f" · {slow} · next fetch in {self.pacer.wait(self.clock())}s{reason}", "warn"))
        if self.needle:
            segments.append(Segment(f" · filter: {self.needle}", "filter"))
        for section in SAVED_KEYS:
            if chosen := self.chosen(section):
                values = [f"#{v}" if v.isdigit() else v for v in chosen] if section == MAP else chosen
                segments.append(Segment(f" · {section.lower()}: {', '.join(values)}", "filter"))
        for error in (self.last_error, self.session_error):
            if error:
                segments.append(Segment(f"  ⚠ {error}", "error"))
        return segments

    # --- the start flow ----------------------------------------------------------------------------

    def needs_map_order(self, ticket: Ticket) -> bool:
        """A start on an open map picks its frontier ticket in the tracker's own map order (GitHub sub-issue order),
        which `map_order` fetches first; not while the map's spec has no tickets yet."""
        lane = self.lane(ticket)
        return (ticket.is_wayfinder_map and ticket.is_open and getattr(self.provider, "map_order", None) is not None
                and not (lane and lane.ticketless_spec))

    def map_order(self, ticket: Ticket) -> list[int] | None:
        """The tracker's order of the map's tickets, off the owner thread; None falls back to the board's own."""
        try:
            return self.provider.map_order(ticket.number)
        except ProviderError:
            return None

    def start_plan(self, ticket: Ticket, order: list[int] | None = None) -> StartPlan | Refusal:
        """What a start does on a card, or why it refuses. A map whose open spec has no tickets yet breaks the spec
        into tickets, the map open or closed; else an open map is worked through its first frontier ticket without a
        live session, else, with an empty frontier, itself."""
        if (lane := self.lane(ticket)) is not None and (spec := lane.ticketless_spec) is not None:
            if refusal := spec.start_refusal(self.viewer):
                return Refusal(refusal)
            if refusal := self._busy(spec):
                return refusal
            return StartPlan(spec)
        if ticket.is_wayfinder_map and ticket.is_open:
            taken = {n for n in self.sessions if self.live(n)} | self.starting
            if ready := frontier(ticket, self.board.tickets(), taken, order):
                return StartPlan(ready[0], f" (next on map {ticket.ref})")
            via = f" (map {ticket.ref}'s frontier is empty)"
        else:
            via = ""
        if refusal := ticket.start_refusal(self.viewer):
            return Refusal(refusal)
        if refusal := self._busy(ticket):
            return refusal
        return StartPlan(ticket, via)

    def _busy(self, ticket: Ticket) -> Refusal | None:
        """Why a ticket with a session, or one being started, starts nothing: another board's start has no session
        to go to yet."""
        number = ticket.number
        if number in self.marked and number not in self.own_starting and not any(
                s.status is not None for s in self.sessions.get(number, [])):
            return Refusal(f"{ticket.ref} is being started from another board")
        if self.live(number):
            return Refusal(f"{ticket.ref} has a live session", live=True)
        return None

    def live(self, number: int) -> bool:
        """An agent works the ticket, or a view is starting one."""
        return number in self.starting or any(s.status is not None for s in self.sessions.get(number, []))

    def confirm(self, plan: StartPlan) -> ConfirmText:
        """What to ask before starting: Claude in the ticket's own worktree, new, or continued with
        `claude --continue` when it exists without an agent."""
        ticket, via = plan
        name, branch = worktree_name(ticket), branch_name(ticket)
        worktree = next((s for s in self.sessions.get(ticket.number, [])  # no live session: each is agentless
                         if Path(s.path).name in (name, f"worktree-{name}") or s.branch == branch), None)
        if ticket.spec_kind:
            where = "its worktree with claude --continue" if worktree else "a new worktree"
            skill = "/implement" if ticket.spec_kind == "impl" else "/to-tickets"
            return ConfirmText(f"Start {skill} on {ticket.ref} {ticket.title} in {where}?",
                               self.describe_start(ticket), "Start")
        if worktree is None:
            return ConfirmText(f"Start {ticket.ref} {ticket.title}{via} in a new worktree?",
                               self.describe_start(ticket), "Start")
        return ConfirmText(f"Continue {ticket.ref} {ticket.title}{via} in its worktree {Path(worktree.path).name}?",
                           f"claude --continue in {worktree.path}", "Continue")

    def begin_start(self, ticket: Ticket) -> Refusal | None:
        """A confirmed start: the ticket is starting here, and for every view of the repo through its marker; or
        why not, when a session or another board's start took it while the confirm was open."""
        self.read_markers()
        if refusal := self._busy(ticket):
            return refusal
        if not self._mark(ticket.number):
            return self._busy(ticket)
        self.own_starting.add(ticket.number)
        return None

    def start(self, ticket: Ticket) -> Notice:
        """Starts the session, off the owner thread; an error notice means it failed (see start_failed)."""
        try:
            return Notice(self.workbench.start(ticket))
        except BackendError as exc:
            return Notice(f"{exc.source}: {exc}", "error")

    def start_failed(self, ticket: Ticket) -> None:
        """Frees the ticket for another start."""
        self.own_starting.discard(ticket.number)
        self._unmark(ticket.number)

    def go_to(self, goto: GoTo) -> Notice:
        """Goes to the session, off the owner thread."""
        try:
            return Notice(self.workbench.go_to(goto.ticket, goto.session))
        except BackendError as exc:
            return Notice(f"{exc.source}: {exc}", "error")

    def primary(self, ticket: Ticket) -> GoTo | Hint:
        """The card's own most urgent session, a map card's else its spec's; with none, what a start would do."""
        sessions = self.sessions.get(ticket.number, [])
        if not sessions and (lane := self.lane(ticket)) is not None:
            sessions = next((found for spec in lane.specs if (found := self.sessions.get(spec.number))), [])
        if sessions:
            return GoTo(ticket, min(sessions, key=lambda s: s.urgency))
        return Hint(ticket, self.start_plan(ticket))

    def hint_text(self, hint: Hint, start: str, refusal_suffix: str = "") -> str:
        """The hint in words, `start` naming the view's start key or button; `refusal_suffix` follows a refusal for
        a live session, saying how this view goes to it."""
        ticket, plan = hint
        if isinstance(plan, Refusal):
            would = f"{start} refuses: {plan.text}" + (refusal_suffix if plan.live else "")
        elif plan.ticket.spec_kind == "spec":
            prompt = self.describe_start(plan.ticket).partition("\n")[0] or "/to-tickets"
            would = f"{start} starts {prompt}" + ("" if plan.ticket is ticket else f" on its spec {plan.ticket.ref}")
        elif plan.ticket is not ticket:
            would = f"{start} starts {plan.ticket.ref} {plan.ticket.title}, next on the map"
        elif ticket.is_wayfinder_map:
            would = f"{start} starts /wayfinder on the map, its frontier is empty"
        elif ticket.spec_kind == "impl":
            would = f"{start} starts " + (self.describe_start(ticket).partition("\n")[0] or "/implement")
        else:
            would = f"{start} starts /wayfinder on it"
        return f"{ticket.ref} has no session here; {would}"
