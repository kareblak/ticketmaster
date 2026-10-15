"""Live Textual swimlane board: a lane per wayfinder map, its tickets in four columns."""

from __future__ import annotations

import math
import re
import time
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Protocol

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual import events
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Footer, Input, Label, Static

from .config import (board_fetch_limit, board_interval, load_settings, parse_fetch_limit, parse_interval,
                     save_settings, seconds_text)
from .herdr import HerdrError, Session
from .model import COLUMN_ORDER, WAYFINDER_TYPES, Board, Column, Lane, Ticket, build_board, frontier
from .providers import ErrorNotice, Provider, ProviderError, Query, RateLimited, error_notice
from .start import branch_name, worktree_name

NARROW_WIDTH = 100  # below this the "auto" view shows one column at a time
COLUMN_COLORS = {
    Column.BLOCKED: "red",
    Column.FRONTIER: "green",
    Column.CLAIMED: "yellow",
    Column.DECIDED: "grey50",
}
MAP_COLOR = "cyan"
STATUS_STYLES = {  # herdr agent states
    "working": ("● working", "bold yellow"),
    "blocked": ("⚠ needs you", "bold red"),
    "idle": ("◌ idle", "cyan"),
    "done": ("✓ done", "green"),
    "unknown": ("? unknown", "grey62"),
    None: ("○ no agent", "grey50"),
}
MODE_STYLES = {"HITL": "black on yellow", "AFK": "black on cyan"}  # who a wayfinder ticket needs at the keyboard
PR_STYLE = "black on magenta"  # an open pull/merge request that closes the card
NOTICE_SECONDS = 10  # a tracker error's notification stays up this long
LIMITED_TICKETS_EVERY = 60.0  # ticket fetches while the tracker rate-limits the board
LIMITED_SESSIONS_EVERY = 10.0  # herdr session refreshes meanwhile


def session_lines(sessions: list[Session], starting: bool, claimed: bool = False) -> Text:
    """A badge per session; for a claimed ticket without one, that the claim's session is not in this herdr."""
    text = Text()
    for session in sessions:
        label, style = STATUS_STYLES.get(session.status, STATUS_STYLES["unknown"])
        text.append(f"{label} ", style=style)
        text.append(f"{Path(session.path).name}\n", style="dim")
    if starting and not sessions:
        text.append("▶ starting…\n", style="bold green")
    elif claimed and not sessions:
        text.append("◇ claimed, no session here\n", style="grey62")
    return text


def render_card(ticket: Ticket, column: Column, sessions: list[Session] = (), starting: bool = False,
                map_title: str = "", out_of_scope: bool = False, claimed: bool = False) -> Text:
    """A ticket: type · HITL/AFK (or spec/impl), an open closing PR, assignee, open blockers and its map's title."""
    text = session_lines(sessions, starting, claimed)
    text.append(f"{ticket.ref} ", style=f"bold {COLUMN_COLORS[column]}")
    text.append(ticket.title, style="bold")
    if ticket.summary:
        text.append("\n" + ticket.summary, style="dim")
    chips = Text()
    if ticket.wayfinder_type:
        chips.append(ticket.wayfinder_type, style="bold")
        if ticket.mode:
            chips.append(" · ")
            chips.append(f" {ticket.mode} ", style=MODE_STYLES[ticket.mode])
        chips.append(" ")
    elif ticket.spec_kind:
        chips.append(f"{ticket.spec_kind} ", style="bold")
    if ticket.is_open and ticket.has_open_change_request:
        chips.append(" PR open ", style=PR_STYLE)
        chips.append(" ")
    if out_of_scope:
        chips.append(" out of scope ", style="black on grey50")
        chips.append(" ")
    if ticket.is_open and (ticket.blocked_by or ticket.open_blockers):
        chips.append(f"⛔ {' '.join(ticket.blocked_by) or ticket.open_blockers} ", style="red")
    if ticket.assignees:
        chips.append(" ".join("@" + a for a in ticket.assignees) + " ", style="italic cyan")
    if map_title:
        chips.append(f"◆ {map_title}", style="grey62")
    if chips.plain:
        text.append("\n")
        text.append(chips)
    return text


def render_map(lane: Lane, sessions: list[Session] = (), starting: bool = False, claimed: bool = False) -> Text:
    """A lane's header: the map's title and its decided / open / blocked counts."""
    text = session_lines(sessions, starting, claimed)
    text.append(f"◆ {lane.map.ref} ", style=f"bold {MAP_COLOR}")
    text.append(lane.map.title, style="bold")
    if lane.collapsed:
        text.append("  closed", style="grey50")
    progress = lane.progress
    text.append(f"\n{progress.decided} decided · {progress.open} open · {progress.blocked} blocked",
                style=f"bold {MAP_COLOR}")
    return text


def matches(ticket: Ticket, needle: str) -> bool:
    if not needle:
        return True
    haystack = " ".join([str(ticket.number), ticket.title, ticket.body, ticket.author, *ticket.labels, *ticket.assignees]).lower()
    return all(word in haystack for word in needle.lower().split())


INCLUDE = "include"  # a selected picker value
TYPE, MODE, ASSIGNEE, MAP, AUTHOR = "Type", "HITL/AFK", "Assignee", "Map", "Author"  # the picker's sections
CARD_FILTERS = (TYPE, MODE, ASSIGNEE, AUTHOR)  # narrow a lane's cards; MAP narrows the lanes
SAVED_KEYS = {TYPE: "types", MODE: "modes", ASSIGNEE: "assignee", MAP: "maps", AUTHOR: "authors"}  # settings.json
CARD_TYPES = (*WAYFINDER_TYPES, "spec", "impl")
MODES = ("HITL", "AFK")
ASSIGNEES, ANYONE = ("me", "unassigned", "anyone"), "anyone"  # one at a time; anyone narrows nothing


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
    """The types, modes and authors the lanes' cards carry, which the picker offers."""
    cards = [ticket for lane in board.lanes for ticket in lane.tickets()]
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


class Pacer:
    """When the board fetches tickets and herdr sessions, given the clock's `now`.

    Normally both every `interval`. A rate limit slows tickets to once a minute and sessions to every 10s;
    the first good fetch after that restores the interval, once. A rate limit after that recovery keeps
    the board slow until it restarts.
    """

    def __init__(self, interval: float, now: float):
        self.interval = interval
        self.limited = False  # backed off right now
        self.recovered = False  # the one recovery is used up
        self.stuck = False  # slow until restart
        self.next_tickets = now
        self.next_sessions = now
        self.last_tickets = self.last_sessions = now - interval  # when each fetch last started

    def set_interval(self, interval: float) -> None:
        """A new interval from the settings menu: the next fetches are due that long after the last ones started.
        A rate-limit back-off stays: tickets keep its minute, sessions its 10s at least."""
        self.interval = interval
        if not self.limited:
            self.next_tickets = self.last_tickets + interval
        self.next_sessions = self.last_sessions + self._sessions_every()

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
            self.next_tickets = now + (LIMITED_TICKETS_EVERY if self.limited else self.interval)
        self.next_sessions = now + self._sessions_every()

    def fetched(self, now: float, ok: bool, rate_limited: bool) -> None:
        if rate_limited:
            self.stuck = self.stuck or self.recovered
            self.limited = True
            self.next_tickets = now + LIMITED_TICKETS_EVERY
        elif ok and self.limited and not self.stuck:
            self.limited, self.recovered = False, True
            self.next_tickets = min(self.next_tickets, now + self.interval)
            self.next_sessions = min(self.next_sessions, now + self.interval)

    def wait(self, now: float) -> int:
        """Whole seconds until the next ticket fetch."""
        return max(0, math.ceil(self.next_tickets - now))


class Card(Static, can_focus=True):
    """A ticket in a lane's column, or the map heading its lane (column None)."""

    # Bound on the card itself: otherwise the lanes' VerticalScroll handles up/down as scrolling.
    BINDINGS = [
        Binding("up,k", "app.move(0,-1)", show=False),
        Binding("down,j", "app.move(0,1)", show=False),
        Binding("left,h", "app.move(-1,0)", show=False),
        Binding("right,l", "app.move(1,0)", show=False),
    ]

    def __init__(self, ticket: Ticket, column: Column | None, text: Text):
        super().__init__(text, classes="card" if column is not None else "card map-card")
        self.ticket = ticket
        self.column = column

    def on_click(self, event: events.Click) -> None:
        self.focus()
        if event.chain >= 2:
            self.app.action_primary()


class Cell(Vertical):
    """One column of one lane."""

    def __init__(self, column: Column, cards: list[Card]):
        super().__init__(*cards, classes="cell")
        self.column = column


class StartPlan(NamedTuple):
    """The ticket s starts for a card, and how the confirm names it (`via` a map)."""

    ticket: Ticket
    via: str = ""


class Workbench(Protocol):
    """The herdr side of the board, see start.Workbench. Every call blocks."""

    def sessions(self) -> dict[int, list[Session]]: ...
    def start(self, ticket: Ticket) -> str: ...
    def go_to(self, ticket: Ticket, session: Session) -> str: ...


class Confirm(ModalScreen[bool]):
    """Yes/no before starting a session in a new worktree; n or Esc cancels."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    #dialog { width: 96; max-width: 100%; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    #prompt { margin-bottom: 1; }
    #buttons { height: auto; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """
    AUTO_FOCUS = "#yes"  # Enter presses the focused button
    BINDINGS = [Binding("n,escape", "answer(False)", "Cancel"), Binding("y", "answer(True)", "Yes")]

    def __init__(self, question: str, detail: str = "", yes: str = "Yes"):
        super().__init__()
        self.question = question
        self.detail = detail
        self.yes = yes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self.question, style="bold"), id="prompt")
            if self.detail:
                yield Label(Text(self.detail, style="dim"))
            with Horizontal(id="buttons"):
                yield Button("Cancel (n)", id="no")
                yield Button(f"{self.yes} (y)", id="yes", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class PickerOption(Static, can_focus=True):
    """One value of a FilterPicker section, shown as `label`; Enter, space or a click selects or deselects it."""

    def __init__(self, section: str, value: str, mode: str | None, label: str = ""):
        super().__init__(classes="option")
        self.section = section
        self.value = value
        self.label = label or value
        self.mode = mode
        self._paint()

    def _paint(self) -> None:
        mark, style = ("[x]", "bold green") if self.mode else ("[ ]", "grey50")
        self.update(Text.assemble((mark, style), " ", (self.label, style if self.mode else "")))

    def set_mode(self, mode: str | None) -> None:
        self.mode = mode
        self._paint()

    def on_click(self) -> None:
        self.focus()
        self.screen.cycle(self)  # focus lands a tick later, so cycle this option rather than the focused one


class FilterPicker(ModalScreen[None]):
    """Narrows the board live from sections of values (type, HITL/AFK, assignee, map, author).

    `sections` maps a section title to its (value, label) pairs, `modes` a title to {value: include}; the picker
    edits `modes` in place and calls `changed` after every edit. A value is selected or not; a section in `single`
    holds one value at a time, and its default value (`single[title]`) stands for none. Esc, f or q closes it,
    c clears it.
    """

    DEFAULT_CSS = """
    FilterPicker { align: center middle; }
    #picker { width: 60; max-width: 100%; height: auto; max-height: 100%; padding: 1 2; border: thick $accent; background: $surface; }
    .section-title { text-style: bold underline; margin-top: 1; }
    .option { padding: 0 1; }
    .option:focus { background: $boost; }
    #picker-help { color: $text-muted; }
    """
    BINDINGS = [
        Binding("escape,f,q", "dismiss", "Close"),
        Binding("enter,space", "cycle", "Select/deselect"),
        Binding("c", "clear", "Clear"),
        Binding("up,k", "step(-1)", show=False),
        Binding("down,j", "step(1)", show=False),
    ]

    def __init__(self, sections: dict[str, list[tuple[str, str]]], modes: dict[str, dict[str, str]],
                 changed: Callable[[], None], single: dict[str, str] | None = None):
        super().__init__()
        self.sections = sections
        self.modes = modes
        self.changed = changed
        self.single = single or {}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="picker"):
            yield Static("Enter/space selects; a card shows when it matches every section · c clears · Esc closes",
                         id="picker-help")
            for title, values in self.sections.items():
                yield Static(title, classes="section-title")
                if not values:
                    yield Static(Text("  none on this board", style="dim"))
                for value, label in values:
                    yield PickerOption(title, value, self._mode(title, value), label)

    def _mode(self, section: str, value: str) -> str | None:
        current = self.modes.get(section, {})
        if not current and self.single.get(section) == value:
            return INCLUDE
        return current.get(value)

    def on_mount(self) -> None:
        options = list(self.query(PickerOption))
        if options:
            options[0].focus()

    def action_step(self, delta: int) -> None:
        options = list(self.query(PickerOption))
        if not options:
            return
        index = options.index(self.focused) + delta if self.focused in options else 0
        options[max(0, min(len(options) - 1, index))].focus()

    def action_cycle(self) -> None:
        if isinstance(self.focused, PickerOption):
            self.cycle(self.focused)

    def cycle(self, option: PickerOption) -> None:
        current = self.modes.setdefault(option.section, {})
        if option.section in self.single:
            current.clear()
            if option.value != self.single[option.section]:
                current[option.value] = INCLUDE
        elif option.mode:
            current.pop(option.value, None)
        else:
            current[option.value] = INCLUDE
        self._paint()
        self.changed()

    def action_clear(self) -> None:
        for current in self.modes.values():
            current.clear()
        self._paint()
        self.changed()

    def _paint(self) -> None:
        for option in self.query(PickerOption):
            option.set_mode(self._mode(option.section, option.value))


class Setting(NamedTuple):
    """One field of the settings menu: its key in the repo's settings, its label, its current value as text, and
    `parse`, which turns the typed text into the value or raises ValueError saying what is wrong."""

    key: str
    label: str
    value: str
    parse: Callable[[str], object]


class SettingsMenu(ModalScreen[dict | None]):
    """The board's settings for this repo, an input per Setting. Enter checks every field and closes with their
    values; an invalid one says why and keeps the menu open. Esc closes it changing nothing."""

    DEFAULT_CSS = """
    SettingsMenu { align: center middle; }
    #settings { width: 60; max-width: 100%; height: auto; max-height: 100%; padding: 1 2; border: thick $accent; background: $surface; }
    #settings-help { color: $text-muted; margin-bottom: 1; }
    .setting-label { text-style: bold; }
    #settings-error { height: auto; }
    """
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, fields: list[Setting]):
        super().__init__()
        self.fields = fields

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="settings"):
            yield Static("Enter applies and saves for this repo · Esc closes without changes", id="settings-help")
            for field in self.fields:
                yield Label(field.label, classes="setting-label")
                yield Input(field.value, id=field.key)
            yield Static(id="settings-error")

    def on_mount(self) -> None:
        self.query(Input).first().focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # the board's own Input.Submitted is its text filter
        values = {}
        for field in self.fields:
            box = self.query_one(f"#{field.key}", Input)
            try:
                values[field.key] = field.parse(box.value)
            except ValueError as exc:
                self.query_one("#settings-error", Static).update(Text(f"{field.label}: {exc}", style="bold red"))
                box.focus()
                return
        self.dismiss(values)


KEY_DETAILS = {
    "primary": "go to the card's most urgent session, else its worktree with a shell, else say what s would do; "
               "never starts or resumes Claude",
    "start": "start /wayfinder for the ticket, or /implement on an impl card, in its own issue-<n> worktree, or "
             "continue Claude in it when it has no agent; not when closed, blocked, claimed by someone else or live "
             "(press Enter), never for a spec or an issue that is not wayfinder work",
    "open": "open the issue in the browser",
    "pick_filter": "narrow by type, HITL/AFK, assignee (me, unassigned, anyone), map and author; combines with /, "
                   "Esc on the board clears all, saved per repo",
    "settings": "the refresh interval, applied at once, and the fetch limit, from the next fetch; saved per repo",
}


def key_help(app: App) -> list[tuple[str, str]]:
    """(keys, description) for each described binding; neighbours sharing a description merge."""
    rows: list[tuple[str, str]] = []
    for binding in app.BINDINGS:
        if not isinstance(binding, Binding) or not binding.description:
            continue
        keys = " ".join(app.get_key_display(Binding(key.strip(), binding.action)) for key in binding.key.split(","))
        if rows and rows[-1][1] == binding.description:
            keys = f"{rows.pop()[0]} {keys}"
        detail = KEY_DETAILS.get(binding.action)
        rows.append((keys, f"{binding.description}: {detail}" if detail else binding.description))
    return rows


def help_text(keys: list[tuple[str, str]], query: Query) -> Text:
    text = Text()

    def heading(title: str) -> None:
        text.append(("\n" if text else "") + title + "\n", style="bold underline")

    def row(name: str, width: int, rule: str, style: str = "bold") -> None:
        text.append(f"  {name:<{width}}  ", style=style)
        text.append(rule + "\n")

    heading("Keys")
    width = max(len(k) for k, _ in keys)
    for keys_, description in keys:
        row(keys_, width, description, "bold cyan")
    text.append("  Click selects a card, double-click acts like Enter.\n", style="dim")

    heading("Lanes")
    text.append("  Each open wayfinder map is a lane, headed by its map card: its decided (closed, not out\n"
                "  of scope), open and blocked tickets. Naming a map (ticketmaster <map>, a number or a\n"
                "  tracker key) filters this run's board to it, unsaved; Esc shows every open map again.\n"
                "  A map named by number shows also when closed, as its header card\n"
                f"  alone.{'  ← this board' if query.named_map else ''}\n"
                "  A spec whose first line is `Part of #<map>` and its implementation tickets (sub-issues,\n"
                "  or a `## Parent` section naming the spec) sit in the map's lane, tagged spec and impl.\n"
                "  Other issues are not shown. PR open: an open pull request will close the card's issue.\n")

    heading("Columns (the first rule that matches)")
    for column, rule in [
        (Column.DECIDED, "closed, of any age; canceled, or linked from the map's ## Out of scope\n"
                         + " " * 12 + "section, marks it out of scope"),
        (Column.BLOCKED, "an open blocking issue (native, or a 'Blocked by:' line or section)"),
        (Column.CLAIMED, "assigned to someone"),
        (Column.FRONTIER, "everything else; the first is the one /wayfinder <map> picks"),
    ]:
        row(column.value, 8, rule, f"bold {COLUMN_COLORS[column]}")
    text.append("  Within a column: sub-issue order, then issue number.\n", style="dim")

    heading("Starting a session")
    text.append("  s asks first, then creates a git worktree workspace in herdr on branch issue-<n> (tm-<n> on\n"
                "  Linear), labelled '#<n> <title>' ('TM-<n> <title>'), and starts Claude in it as\n"
                "  herdr agent issue-<n> with `<wayfinder_command> <map-url> [<ticket-url>]`\n"
                "  (wayfinder_command in .ticketmaster.toml).\n"
                "  When the ticket's worktree exists without an agent, it runs claude --continue there instead;\n"
                "  it never makes a second worktree. What it starts depends on the card:\n")
    for kind, prompt in [
        ("wayfinder map", "s on the lane's header card starts the map's first frontier ticket\n"
                          + " " * 22 + "(open, unblocked, unclaimed, no live session) on that ticket's own\n"
                          + " " * 22 + "worktree; with an empty frontier, the map itself in its own worktree"),
        ("wayfinder ticket", "a wayfinder:research/prototype/grilling/task ticket (or wayfinder::…);\n"
                             + " " * 22 + "its map is the native parent, else `Part of #<map>` on the body's\n"
                             + " " * 22 + "first line"),
        ("impl ticket", "implement_command (default /mattpocock-skills:implement) with the\n"
                        + " " * 22 + "ticket's URL, unfocused (AFK). On Linear the branch is the issue's\n"
                        + " " * 22 + "gitBranchName. Not with an open PR that closes it. The session owns\n"
                        + " " * 22 + "review, push and PR; the board starts nothing else"),
        ("refused", "a closed, blocked or someone else's ticket, one with a live session\n"
                    + " " * 22 + "(press Enter instead), and a spec: s starts /implement on its\n"
                    + " " * 22 + "implementation tickets"),
    ]:
        row(kind, 18, prompt)
    text.append("  A wayfinder ticket's card says who it needs: HITL (you at the keyboard) for grilling and\n"
                "  prototype, AFK (the agent alone) for research, and for a task whichever of the two\n"
                "  words its body names first. Maps and HITL tickets take focus when started; research\n"
                "  starts unfocused, and the status line says so.\n")

    heading("Finding sessions")
    text.append("  A herdr agent in this repo works #<n> when its worktree or branch is issue-<n> or tm-<n>\n"
                "  (worktree- first and -<k> after allowed), or its terminal title starts with TM-<n> or #<n>.\n"
                "  Each card shows a badge per session: ⚠ needs you, ● working, ◌ idle, ✓ done, or ○ no agent\n"
                "  for a worktree without Claude; a claimed ticket with none says so. Enter, on any card, goes\n"
                "  to the most urgent one, else opens its worktree with a shell; it never starts Claude.\n"
                "  A map card goes to the map's own session, never a ticket's.\n")

    heading("Settings")
    text.append("  c opens the settings menu: Enter applies the values at once and saves them per repo, Esc\n"
                "  closes it unchanged. Refresh interval: --interval for this run, then the repo's saved\n"
                "  interval, then the default (5 s, 30 s on Linear). Any positive number of seconds; a rate\n"
                "  limit still slows the board down. Fetch limit: the open maps the board reads, and the\n"
                "  tickets it reads per map (default 300), from the next fetch; a lower one asks the tracker\n"
                "  less. A fetch that reaches it says so in the status line, e.g. `cut off at 300: open maps,\n"
                "  #12's tickets`, so a missing ticket can be traced to the limit.\n")

    heading("The board only reads")
    text.append("  It never writes to the tracker or the repo, and never removes a worktree. A session\n"
                "  claims its ticket by assigning it, so the card moves to Claimed on the next refresh.")
    return text


class HelpScreen(ModalScreen[None]):
    """What the board shows and how to drive it. Esc, ? or q closes it."""

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help { width: 100; max-width: 100%; height: auto; max-height: 100%; padding: 1 2; border: thick $accent; background: $surface; }
    """
    BINDINGS = [Binding("escape,question_mark,q", "dismiss", "Close")]

    def __init__(self, keys: list[tuple[str, str]], query: Query):
        super().__init__()
        self.text = help_text(keys, query)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield Static(self.text)


class BoardApp(App):
    TITLE = "ticketmaster"
    AUTO_FOCUS = None  # otherwise the hidden filter input grabs focus and eats keystrokes
    CSS = """
    Screen { layout: vertical; }
    #status { height: auto; padding: 0 1; background: $panel; }
    #tabs { height: auto; padding: 0 1; display: none; }
    #tabs.visible { display: block; }
    #heads { height: 1; padding: 0 1; }  /* lines up with the cells inside each lane's border */
    .head { width: 1fr; padding: 0 1; text-style: bold; }
    #lanes { height: 1fr; }
    .lane { height: auto; border: round $panel-lighten-2; }
    .lane-row { height: auto; }
    .cell { width: 1fr; height: auto; }
    .card { margin: 0 0 1 0; padding: 0 1; border-left: wide $panel-lighten-2; }
    .map-card { border-left: wide $accent-darken-2; }
    .card:focus { background: $boost; border-left: wide $accent; }
    #empty { padding: 1 2; color: $text-muted; }
    #filter { dock: bottom; display: none; }
    #filter.visible { display: block; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("slash", "filter", "Filter"),
        Binding("f", "pick_filter", "Filters"),
        Binding("c", "settings", "Settings"),
        Binding("enter", "primary", "Go to session"),
        Binding("s", "start", "Start session"),
        Binding("o", "open", "Browser"),
        Binding("v", "toggle_view", "One/all columns"),
        Binding("question_mark", "help", "Help"),
        *[Binding(str(i + 1), f"column({i})", "Show column", show=False) for i in range(len(COLUMN_ORDER))],
        Binding("up,k", "move(0,-1)", "Move up", show=False),
        Binding("down,j", "move(0,1)", "Move down", show=False),
        Binding("left,h", "move(-1,0)", "Move left", show=False),
        Binding("right,l", "move(1,0)", "Move right", show=False),
        Binding("escape", "clear_filter", "Clear the filter", show=False),
    ]

    def __init__(
        self,
        provider: Provider,
        query: Query,
        interval: float | None = None,
        workbench: Workbench | None = None,
        describe_start: Callable[[Ticket], str] | None = None,
        columns: str = "auto",
        settings_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        map_key: str | None = None,
    ):
        """`query.named_map`, or `map_key` (a tracker key such as `TM-1`), presets the map filter for this run.
        `interval` is `--interval`, which wins over the repo's saved interval for this run without replacing it."""
        super().__init__()
        self.view = columns  # "auto" (one column below NARROW_WIDTH), "one" or "all"
        self.single = False
        self.column_index = COLUMN_ORDER.index(Column.FRONTIER)
        self.grid: list[tuple[Card, list[list[Card]]]] = []  # per lane: its header and its cells' cards
        self.provider = provider
        self.workbench = workbench
        self.describe_start = describe_start or (lambda ticket: "")
        self.starting: set[int] = set()  # started from this board, until herdr lists its agent or the start fails
        self.sessions: dict[int, list[Session]] = {}
        self.settings_path = settings_path  # None: settings live only as long as the board
        self.settings = load_settings(settings_path) if settings_path else {}
        self.herdr_error: str | None = None
        self.repo_key = f"{provider.name}:{provider.repo}"
        self.board_query = replace(query, limit=board_fetch_limit(self._repo_settings()))
        self.cut_off: str | None = None  # what the last ticket fetch cut off at the fetch limit
        self.interval = board_interval(interval, self._repo_settings(), provider.name)
        self.clock = clock
        self.pacer = Pacer(self.interval, clock())
        self.ticker: Timer | None = None
        self.fetching = False
        self.queued: bool | None = None  # a refresh asked for mid-fetch: whether it includes tickets
        self.board = Board()
        self.viewer: str | None = None  # the tracker login; asked for with each ticket fetch until known
        self.needle = ""
        saved = self.settings.get("filters", {}).get(self.repo_key, {})
        self.filters = load_filters(saved if isinstance(saved, dict) else {})
        self.saved_maps = dict(self.filters[MAP])  # what the map section saves while the preset stands
        named = map_key.upper() if map_key else str(query.named_map) if query.named_map is not None else None
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

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="status")
        yield Static(id="tabs")
        with Horizontal(id="heads"):
            for column in COLUMN_ORDER:
                yield Static(column.value, classes="head", id=f"head-{column.name}")
        yield VerticalScroll(id="lanes")
        yield Input(placeholder="filter: words match title, body, author, labels, assignees · esc clears", id="filter")
        yield Footer()

    def on_mount(self) -> None:
        self._apply_view()
        self.tick()
        self._start_ticker()

    def _start_ticker(self) -> None:
        if self.ticker is not None:
            self.ticker.stop()
        self.ticker = self.set_interval(min(self.interval, 1.0), self.tick)  # 1s at most, for the back-off countdown

    def tick(self) -> None:
        """Fetches whatever the pacer says is due."""
        if self.pacer.limited:
            self._render_status()
        tickets, sessions = self.pacer.due(self.clock())
        if sessions:
            self._fetch_in_background(tickets)

    def action_refresh(self) -> None:
        """Fetches now, except tickets while rate-limited: then only herdr sessions refresh."""
        if self.pacer.limited:
            reason = f" ({self.notice.reason})" if self.notice else ""
            self.notify(f"Rate-limited{reason}: next ticket fetch in {self.pacer.wait(self.clock())}s", severity="warning")
        elif self.notice:
            self.notify(self.notice.text, severity=self.notice.severity, timeout=NOTICE_SECONDS)
        self._refresh_now()

    def _refresh_now(self) -> None:
        tickets = not self.pacer.limited
        if self.fetching:
            self.queued = tickets or bool(self.queued)  # runs once the fetch in flight lands
        else:
            self._fetch_in_background(tickets)

    def _fetch_in_background(self, tickets: bool) -> None:
        if self.fetching:
            return  # one fetch at a time; the pacer schedules the next when this one started
        self.fetching = True
        self.pacer.started(self.clock(), tickets)
        self.run_worker(lambda: self._fetch(tickets), thread=True, group="fetch")

    def _fetch(self, tickets: bool = True) -> None:
        sessions, herdr_error = self.sessions, None
        if self.workbench is not None:
            try:
                sessions = self.workbench.sessions()
            except HerdrError as exc:
                herdr_error = f"herdr: {exc}"
        if not tickets:
            self.call_from_thread(self._apply, None, None, sessions, herdr_error, fetched_tickets=False)
            return
        query = self.board_query
        try:
            if self.viewer is None:
                self.viewer = self._viewer()
            board = build_board(self.provider.fetch(query), query.named_map)
            cut = getattr(self.provider, "cut_off", [])
            cut_off = f"cut off at {query.limit}: {', '.join(cut)}" if cut else None
            self.call_from_thread(self._apply, board, None, sessions, herdr_error, cut_off=cut_off)
        except RateLimited as exc:
            explain = getattr(self.provider, "explain", None)  # GitHub can say which limit it was
            self.call_from_thread(self._apply, None, explain(exc) if explain else exc, sessions, herdr_error,
                                  rate_limited=True)
        except ProviderError as exc:
            self.call_from_thread(self._apply, None, exc, sessions, herdr_error)
        except BaseException:
            self.call_from_thread(setattr, self, "fetching", False)
            raise

    def _viewer(self) -> str | None:
        try:
            return self.provider.viewer()
        except RateLimited:
            raise
        except ProviderError:
            return None  # claimed tickets then start after the confirm, as before the login was known

    def _apply(self, board: Board | None, error: ProviderError | None, sessions: dict[int, list[Session]] | None = None,
               herdr_error: str | None = None, fetched_tickets: bool = True, rate_limited: bool = False,
               cut_off: str | None = None) -> None:
        self.fetching = False
        if fetched_tickets:
            self.pacer.fetched(self.clock(), ok=board is not None, rate_limited=rate_limited)
            self._tracker_error(error)
        if self.queued is not None:
            tickets, self.queued = self.queued and not self.pacer.limited, None
            self._fetch_in_background(tickets)
        changed = False
        if board is not None:
            changed = board.lanes != self.board.lanes or self.last_ok is None
            self.board = board
            self.cut_off = cut_off
            self.known = known_values(board)
            self.last_ok = datetime.now()
        if sessions is not None and sessions != self.sessions:
            self.starting -= {n for n in self.starting if any(s.status is not None for s in sessions.get(n, []))}
            self.sessions = sessions
            changed = True
        self.herdr_error = herdr_error
        if changed:
            self._render_board()  # rebuilding resets scroll, so only when tickets changed
        else:
            self._render_status()

    def _tickets(self) -> list[Ticket]:
        return self.board.tickets()

    def _visible(self, lane: Lane, column: Column) -> list[Ticket]:
        return [t for t in lane.columns[column]
                if matches(t, self.needle) and passes_filters(t, self.filters, self.known, self.viewer)]

    def _chosen(self, section: str) -> list[str]:
        """The section's selected values that are on the board, in the picker's order. The preset map counts even
        when no lane is that map, so a map that is not on the board shows an empty board, not every map."""
        if section == ASSIGNEE:
            return list(self.filters[ASSIGNEE])
        selected = self.filters[section]
        chosen = [value for value, _ in self._picker_values(section) if value in selected]
        if section == MAP:
            on_board = {value for lane in self.board.lanes for value in map_ids(lane.map)}
            preset = self.map_preset is not None and selected == self.map_preset
            chosen += [value for value in selected if value not in chosen and (preset or value in on_board)]
        return chosen

    def _shown_lanes(self) -> list[Lane]:
        """The lanes the map filter keeps; while cards are filtered, only those with a card that passes, and a named
        closed map, which has no cards to pass."""
        maps = set(self._chosen(MAP))
        lanes = [lane for lane in self.board.lanes if not maps or maps & map_ids(lane.map)]
        if self.needle or any(self.filters[section] for section in CARD_FILTERS):
            lanes = [lane for lane in lanes
                     if lane.collapsed or any(self._visible(lane, column) for column in COLUMN_ORDER)]
        return lanes

    def _render_board(self) -> None:
        container = self.query_one("#lanes", VerticalScroll)
        focused_widget = container.screen.focused  # the board's, also under the filter picker
        focused = focused_widget.ticket.number if isinstance(focused_widget, Card) else None
        container.remove_children()
        self.grid, lanes = [], []
        for lane in self._shown_lanes():
            header = Card(lane.map, None, render_map(lane, self.sessions.get(lane.map.number, []),
                                                     lane.map.number in self.starting, self._claimed(lane.map)))
            cells = [[Card(t, column, render_card(t, column, self.sessions.get(t.number, []), t.number in self.starting,
                                                  lane.map.title, t.number in lane.out_of_scope, self._claimed(t)))
                      for t in self._visible(lane, column)] for column in COLUMN_ORDER]
            self.grid.append((header, cells))
            if lane.collapsed:
                lanes.append(Vertical(header, classes="lane"))
                continue
            row = [Cell(column, cards) for column, cards in zip(COLUMN_ORDER, cells)]
            for cell in row:
                cell.display = self._shown(cell.column)
            lanes.append(Vertical(header, Horizontal(*row, classes="lane-row"), classes="lane"))
        if not lanes and self.last_ok:
            empty = ("No cards match the filters; Esc clears them." if self.board.lanes
                     else f"No open wayfinder maps in {self.provider.repo}.")
            lanes.append(Static(empty, id="empty"))
        container.mount_all(lanes)
        for column in COLUMN_ORDER:
            head = self.query_one(f"#head-{column.name}", Static)
            head.update(Text(f"{column.value} ({self._visible_count(column)})", style=f"bold {COLUMN_COLORS[column]}"))
        refocus = next((card for card in self._cards() if card.ticket.number == focused), None)
        if refocus is not None:
            self.call_after_refresh(refocus.focus, scroll_visible=False)
        self._render_status()
        self._render_tabs()

    def _claimed(self, ticket: Ticket) -> bool:
        """Open and assigned; only herdr can tell whether its session is here, so without it nothing is said."""
        return self.workbench is not None and ticket.is_open and bool(ticket.assignees)

    def _cards(self) -> list[Card]:
        """Every card in board order: each lane's header, then its cells left to right."""
        return [card for header, cells in self.grid for card in (header, *(c for cards in cells for c in cards))]

    def _tracker_error(self, error: ProviderError | None) -> None:
        """One notification per error episode: when an error appears, or turns into another; a good fetch ends it."""
        notice = None if error is None else error_notice(self.provider.name, error)
        if notice is not None and (self.notice is None or self.notice.episode != notice.episode):
            self.notify(notice.text, severity=notice.severity, timeout=NOTICE_SECONDS)
        self.notice = notice
        self.last_error = None if notice is None or isinstance(error, RateLimited) else notice.status_text

    def _render_status(self) -> None:
        status = Text(self.sub_title, style="bold")
        if self.last_ok:
            status.append(" · ")
            maps, tickets = len(self.board.lanes), self.board.ticket_count()
            status.append(f"updated {self.last_ok:%H:%M:%S} · {maps} map{'s' * (maps != 1)} · {tickets} tickets")
            if not self.pacer.limited:
                status.append(f" · every {seconds_text(self.interval)}s")
            if self.cut_off:
                status.append(f" · {self.cut_off}", style="bold yellow")
        if self.pacer.limited:
            slow = "rate-limited again · once a minute until restart" if self.pacer.stuck else "rate-limited"
            reason = f" · {self.notice.reason}" if self.notice else ""
            status.append(f" · {slow} · next fetch in {self.pacer.wait(self.clock())}s{reason}", style="bold yellow")
        if self.needle:
            status.append(f" · filter: {self.needle}", style="yellow")
        for section in SAVED_KEYS:
            if chosen := self._chosen(section):
                values = [f"#{v}" if v.isdigit() else v for v in chosen] if section == MAP else chosen
                status.append(f" · {section.lower()}: {', '.join(values)}", style="yellow")
        for error in (self.last_error, self.herdr_error):
            if error:
                status.append(f"  ⚠ {error}", style="bold red")
        self.query_one("#status", Static).update(status)

    # --- one column at a time (narrow panes, e.g. a herdr side pane) ------------------------------

    def on_resize(self, event: events.Resize) -> None:
        self._apply_view()

    def _apply_view(self) -> None:
        single = self.view == "one" or (self.view == "auto" and self.size.width < NARROW_WIDTH)
        if single and not self.single and isinstance(self.focused, Card) and self.focused.column is not None:
            self.column_index = COLUMN_ORDER.index(self.focused.column)  # keep the selection in view
        self.single = single
        for column in COLUMN_ORDER:
            self.query_one(f"#head-{column.name}").display = self._shown(column)
        for cell in self.query(Cell):
            cell.display = self._shown(cell.column)
        self.query_one("#tabs", Static).set_class(single, "visible")
        self._render_tabs()

    def _shown(self, column: Column) -> bool:
        return not self.single or column is COLUMN_ORDER[self.column_index]

    def _render_tabs(self) -> None:
        tabs = Text("‹ ", style="dim")
        for i, column in enumerate(COLUMN_ORDER):
            if i:
                tabs.append(" · ", style="dim")
            label = f"{i + 1} {column.value} {self._visible_count(column)}"
            tabs.append(f" {label} " if i == self.column_index else label,
                        style=f"bold black on {COLUMN_COLORS[column]}" if i == self.column_index else COLUMN_COLORS[column])
        tabs.append(" ›", style="dim")
        self.query_one("#tabs", Static).update(tabs)

    def _visible_count(self, column: Column) -> int:
        return sum(len(self._visible(lane, column)) for lane in self._shown_lanes())

    def action_toggle_view(self) -> None:
        self.view = "all" if self.single else "one"
        self._apply_view()

    def action_column(self, index: int) -> None:
        """1–4: that column, its first card on the board selected."""
        self._show_column(index)
        cards = [card for _, cells in self.grid for card in cells[self.column_index]]
        if cards:
            self._select(cards[0])
        elif self.single and isinstance(self.focused, Card) and self.focused.column is not None:
            self.set_focus(None)  # its card is hidden now

    def _show_column(self, index: int) -> None:
        self.column_index = max(0, min(len(COLUMN_ORDER) - 1, index))
        self._apply_view()

    def _position(self, card: Card) -> tuple[int, int | None, int] | None:
        """(lane, column index or None for the lane's header, row) of a card on the board."""
        for lane, (header, cells) in enumerate(self.grid):
            if card is header:
                return lane, None, 0
            for column, cards in enumerate(cells):
                if card in cards:
                    return lane, column, cards.index(card)
        return None

    def action_move(self, dx: int, dy: int) -> None:
        """Up/down walk each lane's header and then its cards in the current column; left/right change column
        within the lane."""
        if not self.grid:
            return
        focused = self.focused if isinstance(self.focused, Card) else None
        position = self._position(focused) if focused is not None else None
        if position is None:
            self._select(self.grid[0][0])
            return
        lane, column, row = position
        if dy:
            current = self.column_index if column is None else column
            path = [card for header, cells in self.grid for card in (header, *cells[current])]
            self._select(path[max(0, min(len(path) - 1, path.index(focused) + dy))])
            return
        header, cells = self.grid[lane]
        if self.single:
            self._show_column(self.column_index + dx)
            cards = cells[self.column_index]
            self._select(cards[min(row, len(cards) - 1)] if cards and column is not None else header)
            return
        target = (column if column is not None else (-1 if dx > 0 else len(cells))) + dx
        while 0 <= target < len(cells) and not cells[target]:
            target += dx
        if 0 <= target < len(cells):
            self._select(cells[target][min(row, len(cells[target]) - 1)])

    def _select(self, card: Card) -> None:
        if card.column is not None:
            self.column_index = COLUMN_ORDER.index(card.column)
        self._render_tabs()
        card.focus(scroll_visible=False)
        if card.column is None:  # a header shows its lane from the top, border included
            self.query_one("#lanes", VerticalScroll).scroll_to_widget(card.parent, animate=False, top=True)
        else:
            card.scroll_visible(animate=False)

    def action_open(self) -> None:
        if isinstance(self.focused, Card):
            webbrowser.open(self.focused.ticket.url)

    # --- herdr actions ----------------------------------------------------------------------------

    def _card(self) -> Card | None:
        if not isinstance(self.focused, Card):
            self.notify("Select a ticket first", severity="warning")
            return None
        if self.workbench is None:
            self.notify("Sessions need herdr; run the board from a herdr pane", severity="warning")
            return None
        return self.focused

    def _run(self, job: Callable[[], str]) -> None:
        def work() -> None:
            try:
                message, severity = job(), "information"
            except HerdrError as exc:
                message, severity = f"herdr: {exc}", "error"
            self.call_from_thread(self.notify, message, severity=severity)
            self.call_from_thread(self._refresh_now)

        self.run_worker(work, thread=True, group="herdr")

    def action_primary(self) -> None:
        """Enter, on any card: go to the card's own most urgent session, else its worktree with a shell; with
        neither, say what s would do there or why it refuses. It never starts or resumes Claude."""
        card = self._card()
        if card is None:
            return
        ticket, sessions = card.ticket, self.sessions.get(card.ticket.number, [])
        if sessions:
            session = min(sessions, key=lambda s: s.urgency)
            self._run(lambda: self.workbench.go_to(ticket, session))
            return
        plan = self._start_plan(ticket)
        if isinstance(plan, str):
            would = f"s refuses: {plan}"
        elif plan.ticket is not ticket:
            would = f"s starts {plan.ticket.ref} {plan.ticket.title}, next on the map"
        elif ticket.is_wayfinder_map:
            would = "s starts /wayfinder on the map, its frontier is empty"
        elif ticket.spec_kind == "impl":
            would = "s starts " + (self.describe_start(ticket).partition("\n")[0] or "/implement")  # the prompt line
        else:
            would = "s starts /wayfinder on it"
        self.notify(f"{ticket.ref} has no session here; {would}", severity="warning")

    def action_start(self) -> None:
        """s: start /wayfinder on the card's ticket (/implement on an impl card), or on a map's first frontier
        ticket, else on the map itself; or say why it refuses."""
        card = self._card()
        if card is None:
            return
        ticket = card.ticket
        map_order = getattr(self.provider, "map_order", None)
        if not (ticket.is_wayfinder_map and ticket.is_open and map_order):
            self._offer_start(ticket, None)
            return

        def work() -> None:  # the tracker's own map order (GitHub sub-issue order) picks the frontier ticket
            try:
                order = map_order(ticket.number)
            except ProviderError:
                order = None  # the board's own order is the fallback
            self.call_from_thread(self._offer_start, ticket, order)

        self.run_worker(work, thread=True, group="herdr")

    def _offer_start(self, ticket: Ticket, order: list[int] | None) -> None:
        plan = self._start_plan(ticket, order)
        if isinstance(plan, str):
            self.notify(plan, severity="warning")
        else:
            self._confirm_start(plan.ticket, plan.via)

    def _start_plan(self, ticket: Ticket, order: list[int] | None = None) -> StartPlan | str:
        """What s does on a card, or why it refuses. A map is
        worked through its first frontier ticket without a live session, else, with an empty frontier, itself."""
        if ticket.is_wayfinder_map and ticket.is_open:
            taken = {n for n in self.sessions if self._live(n)} | self.starting
            if ready := frontier(ticket, self._tickets(), taken, order):
                return StartPlan(ready[0], f" (next on map {ticket.ref})")
            via = f" (map {ticket.ref}'s frontier is empty)"
        else:
            via = ""
        if refusal := ticket.start_refusal(self.viewer):
            return refusal
        if self._live(ticket.number):
            return f"{ticket.ref} has a live session: press Enter to go to it"
        return StartPlan(ticket, via)

    def _live(self, number: int) -> bool:
        """An agent works the ticket, or this board is starting one."""
        return number in self.starting or any(s.status is not None for s in self.sessions.get(number, []))

    def _confirm_start(self, ticket: Ticket, via: str = "") -> None:
        """Asks first; herdr then starts Claude in the ticket's own worktree, new, or continued with
        `claude --continue` when it exists without an agent."""
        name, branch = worktree_name(ticket), branch_name(ticket)
        worktree = next((s for s in self.sessions.get(ticket.number, [])  # no live session: each is agentless
                         if Path(s.path).name in (name, f"worktree-{name}") or s.branch == branch), None)

        def start() -> str:
            try:
                return self.workbench.start(ticket)
            except HerdrError:
                self.call_from_thread(self.starting.discard, ticket.number)
                raise

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.starting.add(ticket.number)
                self._render_board()
                self._run(start)

        if ticket.spec_kind == "impl":
            where = "its worktree with claude --continue" if worktree else "a new worktree"
            question = f"Start /implement on {ticket.ref} {ticket.title} in {where}?"
            detail, yes = self.describe_start(ticket) + "\nunfocused (AFK)", "Start"
        elif worktree is None:
            question = f"Start {ticket.ref} {ticket.title}{via} in a new worktree?"
            detail, yes = self.describe_start(ticket), "Start"
        else:
            question = f"Continue {ticket.ref} {ticket.title}{via} in its worktree {Path(worktree.path).name}?"
            detail, yes = f"claude --continue in {worktree.path}", "Continue"
        self.push_screen(Confirm(question, detail, yes), confirmed)

    def action_help(self) -> None:
        self.push_screen(HelpScreen(key_help(self), self.board_query))

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.add_class("visible")
        box.value = self.needle
        box.cursor_position = len(self.needle)
        box.focus()

    def _picker_values(self, section: str) -> list[tuple[str, str]]:
        """(value, label) for each of the section's options, from the board's tickets."""
        if section == ASSIGNEE:
            return [(value, value) for value in ASSIGNEES]
        if section == MAP:
            return [(map_value(lane.map), f"{lane.map.ref} {lane.map.title}") for lane in self.board.lanes]
        known = self.known.get(section, set())
        if section == AUTHOR:
            return [(author, author) for author in sorted(known, key=str.lower)]
        order = CARD_TYPES if section == TYPE else MODES
        return [(value, value) for value in order if value in known]

    def action_pick_filter(self) -> None:
        def changed() -> None:
            self._save_filters()
            self._render_board()

        sections = {section: self._picker_values(section) for section in SAVED_KEYS}
        self.push_screen(FilterPicker(sections, self.filters, changed, {ASSIGNEE: ANYONE}),
                         lambda _: self.call_after_refresh(self.action_move, 0, 0))

    def _save_filters(self) -> None:
        """Saves the picker's selection per repo; a saved value no longer on the board stays saved. While the map
        preset stands, the map section keeps what was saved before; a map number is saved as a number."""
        if self.map_preset is None or self.filters[MAP] != self.map_preset:
            self.saved_maps = dict(self.filters[MAP])
        selection = {**self.filters, MAP: self.saved_maps}
        filters = self.settings.setdefault("filters", {})
        filters[self.repo_key] = {
            key: [int(v) if v.isdigit() else v for v in sorted(selection[section], key=lambda v: (len(v), v))]
            if section == MAP else sorted(selection[section]) for section, key in SAVED_KEYS.items()}
        self._write_settings("filter")

    def _write_settings(self, what: str) -> None:
        """Writes settings.json; a failure is a warning naming `what` was not saved."""
        if self.settings_path:
            try:
                save_settings(self.settings_path, self.settings)
            except OSError as exc:
                self.notify(f"{what} not saved: {exc}", severity="warning")

    def action_settings(self) -> None:
        fields = [Setting("interval", "Refresh interval (seconds)", seconds_text(self.interval), parse_interval),
                  Setting("fetch_limit", "Fetch limit (open maps, and tickets per map)", str(self.board_query.limit),
                          parse_fetch_limit)]
        self.push_screen(SettingsMenu(fields), self._apply_settings)

    def _apply_settings(self, values: dict | None) -> None:
        """Applies the menu's values, the interval at once and the fetch limit from the next fetch, and saves those
        that differ from the repo's saved ones, also a value that `--interval` already set for this run, but not an
        unchanged default fetch limit; Esc (None) changes nothing."""
        if values is None:
            return
        interval, limit = values["interval"], values["fetch_limit"]
        if interval != self.interval:
            self.interval = interval
            self.pacer.set_interval(interval)
            self._start_ticker()
            self._render_status()
        saved = {"interval": int(interval) if interval.is_integer() else interval}
        if limit != self.board_query.limit or "fetch_limit" in self._repo_settings():
            self.board_query = replace(self.board_query, limit=limit)
            saved["fetch_limit"] = limit
        if any(self._repo_settings().get(key) != value for key, value in saved.items()):
            self._repo_settings(create=True).update(saved)
            self._write_settings("settings")

    def _repo_settings(self, create: bool = False) -> dict:
        """This repo's entry under `repos` in settings.json; `create` makes it, replacing one that is no dict."""
        repos = self.settings.get("repos")
        if not isinstance(repos, dict):
            if not create:
                return {}
            repos = self.settings["repos"] = {}
        saved = repos.get(self.repo_key)
        if not isinstance(saved, dict):
            if not create:
                return {}
            saved = repos[self.repo_key] = {}
        return saved

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.remove_class("visible")
        self.needle = ""
        if any(self.filters.values()):
            for selection in self.filters.values():
                selection.clear()
            self._save_filters()
        self._render_board()
        self.call_after_refresh(self.action_move, 0, 0)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.needle = event.value.strip()
        event.input.remove_class("visible")
        self._render_board()
        self.call_after_refresh(self.action_move, 0, 0)
