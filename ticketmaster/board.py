"""Live Textual kanban board."""

from __future__ import annotations

import webbrowser
import zlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual import events
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, Static

from .config import load_settings, save_settings
from .herdr import HerdrError, Session
from .model import COLUMN_ORDER, Board, Column, Ticket, build_board
from .providers import Provider, ProviderError, Query
from .wayfinding import SETUP_COMMAND as WAYFINDING_SETUP, drift

TAG_COLORS = {
    "security": "white on red",
    "sec": "white on red",
    "ux": "black on magenta",
    "ui": "black on cyan",
    "perf": "black on yellow",
    "performance": "black on yellow",
    "code": "white on blue",
    "a11y": "black on green",
    "docs": "black on white",
}
SETUP_DOC = Path("docs/agents/issue-tracker.md")  # written by /setup-matt-pocock-skills
SETUP_COMMAND = "/setup-matt-pocock-skills"
NARROW_WIDTH = 100  # below this the "auto" view shows one column at a time
FALLBACK_COLORS = ["white on dark_green", "white on purple4", "black on orange3", "white on grey37", "black on gold3"]
COLUMN_COLORS = {
    Column.BLOCKED: "red",
    Column.READY: "green",
    Column.IN_PROGRESS: "yellow",
    Column.REVIEW: "magenta",
    Column.DONE: "grey50",
}
STATUS_STYLES = {  # herdr agent states
    "working": ("● working", "bold yellow"),
    "blocked": ("⚠ needs you", "bold red"),
    "idle": ("◌ idle", "cyan"),
    "done": ("✓ done", "green"),
    "unknown": ("? unknown", "grey62"),
    None: ("○ no agent", "grey50"),
}
ACTIVE_STATES = ("working", "blocked")  # auto-remove waits until the agent is no longer busy or asking


def tag_style(kind: str) -> str:
    return TAG_COLORS.get(kind.lower()) or FALLBACK_COLORS[zlib.crc32(kind.lower().encode()) % len(FALLBACK_COLORS)]


def render_card(ticket: Ticket, column: Column, sessions: list[Session] = (), starting: bool = False) -> Text:
    text = Text()
    for session in sessions:
        label, style = STATUS_STYLES.get(session.status, STATUS_STYLES["unknown"])
        text.append(f"{label} ", style=style)
        text.append(f"{Path(session.path).name}\n", style="dim")
    if starting and not sessions:
        text.append("▶ starting…\n", style="bold green")
    text.append(f"#{ticket.number} ", style=f"bold {COLUMN_COLORS[column]}")
    text.append(ticket.title, style="bold")
    if ticket.summary:
        text.append("\n" + ticket.summary, style="dim")
    chips = Text()
    for kind in ticket.review_tags:
        chips.append(f" {kind} ", style=tag_style(kind))
        chips.append(" ")
    if column is Column.REVIEW and not ticket.review_tags and ticket.has_open_change_request:
        chips.append(" PR open ", style="black on grey70")
        chips.append(" ")
    if column is Column.BLOCKED and ticket.open_blockers:
        chips.append(f"⛔ {ticket.open_blockers} ", style="red")
    for label in ticket.other_labels[:3]:
        chips.append(f"[{label}] ", style="grey62")
    if ticket.author:
        chips.append(f"by {ticket.author} ", style="grey62")
    if ticket.assignees:
        chips.append(" ".join("@" + a for a in ticket.assignees), style="italic cyan")
    if chips.plain:
        text.append("\n")
        text.append(chips)
    return text


def setup_missing(root: Path | None) -> bool:
    """The mattpocock skills a session runs stop to ask for their setup when the repo has no issue-tracker doc."""
    return root is not None and not (root / SETUP_DOC).is_file()


def matches(ticket: Ticket, needle: str) -> bool:
    if not needle:
        return True
    haystack = " ".join([str(ticket.number), ticket.title, ticket.body, ticket.author, *ticket.labels, *ticket.assignees]).lower()
    return all(word in haystack for word in needle.lower().split())


INCLUDE, EXCLUDE = "include", "exclude"
MODE_CYCLE = {None: INCLUDE, INCLUDE: EXCLUDE, EXCLUDE: None}  # a label option cycles include → exclude → off
SELECT_CYCLE = {None: INCLUDE, INCLUDE: None}  # an author option is selected or not


def passes_labels(ticket: Ticket, modes: dict[str, str], known: set[str]) -> bool:
    """Any included label (OR) and no excluded one; included labels missing from the board (`known`) are ignored."""
    included = {label for label, mode in modes.items() if mode == INCLUDE and label in known}
    if included and not included.intersection(ticket.labels):
        return False
    return not any(modes.get(label) == EXCLUDE for label in ticket.labels)


def passes_authors(ticket: Ticket, selected: dict[str, str], known: set[str]) -> bool:
    """Opened by any selected author (OR); selected authors missing from the board (`known`) are ignored."""
    authors = {author for author in selected if author in known}
    return not authors or ticket.author in authors


def describe_modes(modes: dict[str, str], known: set[str]) -> str:
    """`+bug -wontfix` for the status line, leaving out labels that are not on the board."""
    return " ".join(("+" if mode == INCLUDE else "-") + label
                    for label, mode in sorted(modes.items()) if label in known)


class Card(Static, can_focus=True):
    # Bound on the card itself: otherwise the column's VerticalScroll handles up/down as scrolling.
    BINDINGS = [
        Binding("up,k", "app.move(0,-1)", show=False),
        Binding("down,j", "app.move(0,1)", show=False),
        Binding("left,h", "app.move(-1,0)", show=False),
        Binding("right,l", "app.move(1,0)", show=False),
    ]

    def __init__(self, ticket: Ticket, column: Column, sessions: list[Session] = (), starting: bool = False):
        super().__init__(render_card(ticket, column, sessions, starting), classes="card")
        self.ticket = ticket
        self.column = column

    def on_click(self, event: events.Click) -> None:
        self.focus()
        if event.chain >= 2:
            self.app.action_primary()


class ColumnScroll(VerticalScroll, can_focus=False):
    """Scrolls with the mouse wheel and follows the focused card, but never takes focus itself."""


class Workbench(Protocol):
    """The herdr side of the board, see start.Workbench. Every call blocks."""

    def sessions(self) -> dict[int, list[Session]]: ...
    def start(self, ticket: Ticket) -> str: ...
    def review(self, ticket: Ticket) -> str: ...
    def go_to(self, ticket: Ticket, session: Session) -> str: ...
    def remove(self, ticket: Ticket, sessions: list[Session]) -> str: ...
    def auto_remove(self, ticket: Ticket, sessions: list[Session]) -> str: ...


class Confirm(ModalScreen[bool]):
    """Yes/no before anything that makes or removes a worktree; a stray double-click is cancelled with n or Esc."""

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
    """One value of a FilterPicker section; Enter, space or a click moves it along `cycle`."""

    def __init__(self, section: str, value: str, mode: str | None, cycle: dict = MODE_CYCLE):
        super().__init__(classes="option")
        self.section = section
        self.value = value
        self.mode = mode
        self.cycle = cycle
        self._paint()

    def _paint(self) -> None:
        mark, style = {INCLUDE: ("[+]", "bold green"), EXCLUDE: ("[-]", "bold red")}.get(self.mode, ("[ ]", "grey50"))
        self.update(Text.assemble((mark, style), " ", (self.value, style if self.mode else "")))

    def set_mode(self, mode: str | None) -> None:
        self.mode = mode
        self._paint()

    def on_click(self) -> None:
        self.focus()
        self.screen.cycle(self)  # focus lands a tick later, so cycle this option rather than the focused one


class FilterPicker(ModalScreen[None]):
    """Narrows the board live from sections of values (labels, authors).

    `sections` maps a section title to its values, `modes` a title to {value: include|exclude}; the picker
    edits `modes` in place and calls `changed` after every edit. Values cycle include → exclude → off, or
    only on/off for the sections in `cycles`. Esc, f or q closes it, c clears it.
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
        Binding("enter,space", "cycle", "Include/exclude/off"),
        Binding("c", "clear", "Clear"),
        Binding("up,k", "step(-1)", show=False),
        Binding("down,j", "step(1)", show=False),
    ]

    def __init__(self, sections: dict[str, list[str]], modes: dict[str, dict[str, str]], changed: Callable[[], None],
                 cycles: dict[str, dict] | None = None):
        super().__init__()
        self.sections = sections
        self.modes = modes
        self.changed = changed
        self.cycles = cycles or {}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="picker"):
            yield Static("Enter/space: labels include → exclude → off, authors on/off · c clears · Esc closes",
                         id="picker-help")
            for title, values in self.sections.items():
                yield Static(title, classes="section-title")
                if not values:
                    yield Static(Text("  none on this board", style="dim"))
                current = self.modes.get(title, {})
                for value in values:
                    yield PickerOption(title, value, current.get(value), self.cycles.get(title, MODE_CYCLE))

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
        option.set_mode(option.cycle[option.mode])
        current = self.modes.setdefault(option.section, {})
        if option.mode:
            current[option.value] = option.mode
        else:
            current.pop(option.value, None)
        self.changed()

    def action_clear(self) -> None:
        for current in self.modes.values():
            current.clear()
        for option in self.query(PickerOption):
            option.set_mode(None)
        self.changed()


KEY_DETAILS = {
    "primary": "go to the ticket's herdr workspace; with none, start it (Ready, In progress) or review it (Review)",
    "start": "start the ticket in a new issue-<n> worktree, even when it already has one",
    "review": "on a Review card, a review-<n> worktree where Claude checks out the PR and runs the code-review skill",
    "remove": "on a Done card, remove its worktrees (herdr refuses if one has uncommitted changes)",
    "toggle_auto_remove": "remove Done tickets' worktrees once their agent is idle; unpushed work is kept",
    "open": "open the issue in the browser",
    "pick_filter": "include or exclude labels, select authors (Enter cycles); combines with /, Esc on the board clears all",
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

    heading("Columns (the first rule that matches)")
    for column, rule in [
        (Column.DONE, f"closed in the last {query.done_days} days; older closed issues are hidden"),
        (Column.REVIEW, "a review:<kind> label, or an open PR/MR that closes it"),
        (Column.BLOCKED, "an open blocking issue, or a 'blocked' label"),
        (Column.IN_PROGRESS, "assigned to someone"),
        (Column.READY, "everything else"),
    ]:
        row(column.value, 11, rule, f"bold {COLUMN_COLORS[column]}")

    heading("Scope")
    current = "parent" if query.parent else "--label" if query.label else "no filter"
    for name, rule in [
        ("no filter", "every open issue, and every issue closed in the last --done-days"),
        ("parent", "the sub-issues of one issue, e.g. a wayfinder map (ticketmaster 42)"),
        ("--label", "issues with that label (ticketmaster --label NAME)"),
    ]:
        row(name, 9, rule + ("  ← this board" if name == current else ""))

    heading("Starting a session")
    text.append("  s (or Enter on a card without one) asks first, then creates a git worktree workspace\n"
                "  in herdr on branch issue-<n> (issue-<n>-2… when it exists) and starts Claude in it as\n"
                "  herdr agent issue-<n>. The card then lists the session: ● working, ⚠ needs you,\n"
                "  ◌ idle, ✓ done or ○ no agent. The prompt depends on the ticket:\n")
    for kind, prompt in [
        ("wayfinder map", "the wayfinder skill with the map's URL"),
        ("wayfinder ticket", "the wayfinder skill with the map's and the ticket's URLs"),
        ("plain issue", "the implement skill with the ticket's URL, plus a brief: assign it to\n"
                        + " " * 20 + "yourself first, implement, then push and open a PR that closes it"),
    ]:
        row(kind, 16, prompt)
    text.append("  c on a Review card does the same in a review-<n> worktree with the code-review skill\n"
                "  (review_command in .ticketmaster.toml): check out the PR that closes the issue, review\n"
                "  it from the PR's base branch against the issue, and report in the session only.\n")
    text.append(f"  Those skills need the repo set up once with {SETUP_COMMAND} (it writes\n"
                f"  {SETUP_DOC}); without it a session stops to ask, so the status line warns.\n")
    text.append(f"  `{WAYFINDING_SETUP}` then writes the wayfinding linking rules (map, blocking, out of\n"
                f"  scope) into that doc's Wayfinding operations section; the status line warns while\n"
                f"  they are missing or differ from what this version writes.\n")

    heading("The board only reads")
    text.append("  It never writes to the tracker. A session claims its ticket by assigning it, so the\n"
                "  card moves to In progress on the next refresh.")
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
    #columns { height: 1fr; }
    .column { width: 1fr; border: round $panel-lighten-2; padding: 0 0; }
    .column-title { height: 1; padding: 0 1; text-style: bold; }
    .card { margin: 0 0 1 0; padding: 0 1; border-left: wide $panel-lighten-2; }
    .card:focus { background: $boost; border-left: wide $accent; }
    #filter { dock: bottom; display: none; }
    #filter.visible { display: block; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("slash", "filter", "Filter"),
        Binding("f", "pick_filter", "Labels/authors"),
        Binding("enter", "primary", "Go/start"),
        Binding("s", "start", "New worktree"),
        Binding("c", "review", "Review"),
        Binding("x", "remove", "Remove worktree"),
        Binding("a", "toggle_auto_remove", "Auto-remove"),
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
        interval: float = 5.0,
        workbench: Workbench | None = None,
        describe_start: Callable[[Ticket], str] | None = None,
        columns: str = "auto",
        settings_path: Path | None = None,
        repo_root: Path | None = None,
    ):
        super().__init__()
        self.view = columns  # "auto" (one column below NARROW_WIDTH), "one" or "all"
        self.single = False
        self.column_index = COLUMN_ORDER.index(Column.READY)
        self.provider = provider
        self.workbench = workbench
        self.describe_start = describe_start or (lambda ticket: "")
        self.starting: set[int] = set()  # started from this board, until herdr lists the worktree
        self.sessions: dict[int, list[Session]] = {}
        self.settings_path = settings_path  # None: settings live only as long as the board
        self.settings = load_settings(settings_path) if settings_path else {}
        self.auto_remove = bool(self.settings.get("auto_remove", False))
        self.auto_tried: set[str] = set()  # worktree paths auto-remove has handled, so a kept one is not retried
        self.herdr_error: str | None = None
        self.repo_root = repo_root  # None: the skills setup is not checked
        self.setup_missing = False
        self.wayfinding_drift: str | None = None  # "missing" / "outdated" when `ticketmaster setup` should run
        self.board_query = query
        self.interval = interval
        self.board = Board()
        self.needle = ""
        self.repo_key = f"{provider.name}:{provider.repo}"
        saved = self.settings.get("filters", {}).get(self.repo_key, {})
        self.filters: dict[str, dict[str, str]] = {
            "Labels": dict(saved.get("labels", {})),
            "Authors": dict.fromkeys(saved.get("authors", []), INCLUDE),
        }
        self.last_error: str | None = None
        self.last_ok: datetime | None = None
        scope = f"parent #{query.parent}" if query.parent else f"label {query.label}" if query.label else "all issues"
        self.sub_title = f"{provider.name}:{provider.repo} · {scope}"

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="status")
        yield Static(id="tabs")
        with Horizontal(id="columns"):
            for column in COLUMN_ORDER:
                with Vertical(classes="column", id=f"column-{column.name}"):
                    yield Static(column.value, classes="column-title", id=f"title-{column.name}")
                    yield ColumnScroll(id=f"col-{column.name}")
        yield Input(placeholder="filter: words match title, body, author, labels, assignees · esc clears", id="filter")
        yield Footer()

    def on_mount(self) -> None:
        self._apply_view()
        self.action_refresh()
        self.set_interval(self.interval, self.action_refresh)

    def action_refresh(self) -> None:
        self.run_worker(self._fetch, thread=True, exclusive=True, group="fetch")

    def _fetch(self) -> None:
        sessions, herdr_error = self.sessions, None
        self.setup_missing = setup_missing(self.repo_root)
        self.wayfinding_drift = None if self.repo_root is None else drift(self.repo_root, self.provider.name)
        if self.workbench is not None:
            try:
                sessions = self.workbench.sessions()
            except HerdrError as exc:
                herdr_error = f"herdr: {exc}"
        try:
            tickets = self.provider.fetch(self.board_query)
            board = build_board(tickets, done_days=self.board_query.done_days)
            self.call_from_thread(self._apply, board, None, sessions, herdr_error)
        except ProviderError as exc:
            self.call_from_thread(self._apply, None, str(exc), sessions, herdr_error)

    def _apply(self, board: Board | None, error: str | None, sessions: dict[int, list[Session]] | None = None,
               herdr_error: str | None = None) -> None:
        changed = False
        if board is not None:
            changed = board.columns != self.board.columns or self.last_ok is None
            self.board = board
            self.last_ok = datetime.now()
        if sessions is not None and sessions != self.sessions:
            self.starting -= set(sessions) - set(self.sessions)
            self.sessions = sessions
            changed = True
        self.last_error = error
        self.herdr_error = herdr_error
        if changed:
            self._render_board()  # rebuilding resets scroll, so only when tickets changed
        else:
            self._render_status()
        if board is not None and herdr_error is None:
            self._auto_remove_done()

    def _auto_remove_done(self) -> None:
        """Removes Done tickets' worktrees once their agent is idle, done or gone; once per worktree per board run."""
        if not self.auto_remove or self.workbench is None:
            return
        for ticket in self.board.columns[Column.DONE]:
            sessions = [s for s in self.sessions.get(ticket.number, [])
                        if s.path not in self.auto_tried and s.status not in ACTIVE_STATES]
            if sessions:
                self.auto_tried.update(s.path for s in sessions)
                self._run(lambda ticket=ticket, sessions=sessions: self.workbench.auto_remove(ticket, sessions))

    def _board_labels(self) -> set[str]:
        return {label for tickets in self.board.columns.values() for ticket in tickets for label in ticket.labels}

    def _board_authors(self) -> set[str]:
        return {ticket.author for tickets in self.board.columns.values() for ticket in tickets if ticket.author}

    def _visible(self, column: Column) -> list[Ticket]:
        labels, authors = self._board_labels(), self._board_authors()
        return [t for t in self.board.columns[column]
                if matches(t, self.needle) and passes_labels(t, self.filters["Labels"], labels)
                and passes_authors(t, self.filters["Authors"], authors)]

    def _render_board(self) -> None:
        focused_widget = self.query_one("#columns").screen.focused  # the board's, also under the filter picker
        focused = focused_widget.ticket.number if isinstance(focused_widget, Card) else None
        refocus: Card | None = None
        for column in COLUMN_ORDER:
            tickets = self._visible(column)
            title = self.query_one(f"#title-{column.name}", Static)
            title.update(Text(f"{column.value} ({len(tickets)})", style=f"bold {COLUMN_COLORS[column]}"))
            container = self.query_one(f"#col-{column.name}", VerticalScroll)
            container.remove_children()
            cards = [Card(t, column, self.sessions.get(t.number, []), t.number in self.starting) for t in tickets]
            container.mount_all(cards)
            refocus = refocus or next((c for c in cards if c.ticket.number == focused), None)
        if refocus is not None:
            self.call_after_refresh(refocus.focus, scroll_visible=False)
        self._render_status()
        self._render_tabs()

    def _render_status(self) -> None:
        status = Text(self.sub_title, style="bold")
        if self.last_ok:
            status.append(" · ")
            status.append(f"updated {self.last_ok:%H:%M:%S} · {self.board.total()} tickets · every {self.interval:g}s")
        if self.needle:
            status.append(f" · filter: {self.needle}", style="yellow")
        if labels := describe_modes(self.filters["Labels"], self._board_labels()):
            status.append(f" · labels: {labels}", style="yellow")
        if authors := sorted(self._board_authors().intersection(self.filters["Authors"]), key=str.lower):
            status.append(f" · authors: {', '.join(authors)}", style="yellow")
        if self.auto_remove:
            status.append(" · auto-remove on", style="green")
        if self.setup_missing:
            status.append(f"  ⚠ run {SETUP_COMMAND}, then {WAYFINDING_SETUP}, before starting sessions (no {SETUP_DOC})",
                          style="bold yellow")
        elif self.wayfinding_drift:
            state = "missing from" if self.wayfinding_drift == "missing" else "out of date in"
            status.append(f"  ⚠ run {WAYFINDING_SETUP}: wayfinding rules {state} {SETUP_DOC}", style="bold yellow")
        for error in (self.last_error, self.herdr_error):
            if error:
                status.append(f"  ⚠ {error}", style="bold red")
        self.query_one("#status", Static).update(status)

    # --- one column at a time (narrow panes, e.g. a herdr side pane) ------------------------------

    def on_resize(self, event: events.Resize) -> None:
        self._apply_view()

    def _apply_view(self) -> None:
        single = self.view == "one" or (self.view == "auto" and self.size.width < NARROW_WIDTH)
        if single and not self.single and isinstance(self.focused, Card):
            self.column_index = COLUMN_ORDER.index(self.focused.column)  # keep the selection in view
        self.single = single
        for i, column in enumerate(COLUMN_ORDER):
            self.query_one(f"#column-{column.name}").display = not single or i == self.column_index
        self.query_one("#tabs", Static).set_class(single, "visible")
        self._render_tabs()

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
        return len(self._visible(column))

    def action_toggle_view(self) -> None:
        self.view = "all" if self.single else "one"
        self._apply_view()

    def action_column(self, index: int) -> None:
        self._show_column(index)

    def _show_column(self, index: int, row: int = 0) -> None:
        self.column_index = max(0, min(len(COLUMN_ORDER) - 1, index))
        self._apply_view()
        cards = self._cards_by_column()[self.column_index]
        if cards:
            self._select(cards[min(row, len(cards) - 1)])
        elif self.single:
            self.set_focus(None)

    def _cards_by_column(self) -> list[list[Card]]:
        return [list(self.query_one(f"#col-{c.name}", VerticalScroll).query(Card)) for c in COLUMN_ORDER]

    def action_move(self, dx: int, dy: int) -> None:
        grid = self._cards_by_column()
        if self.single:
            current = grid[self.column_index]
            focused = isinstance(self.focused, Card) and self.focused in current
            row = current.index(self.focused) if focused else 0
            if dx:
                self._show_column(self.column_index + dx, row)
            elif current:
                self._select(current[max(0, min(len(current) - 1, row + dy)) if focused else 0])
            return
        if not any(grid):
            return
        if not isinstance(self.focused, Card):
            self._select(next(col for col in grid if col)[0])
            return
        col = next(i for i, cards in enumerate(grid) if self.focused in cards)
        row = grid[col].index(self.focused)
        if dy:
            self._select(grid[col][max(0, min(len(grid[col]) - 1, row + dy))])
            return
        target = col + dx
        while 0 <= target < len(grid) and not grid[target]:
            target += dx
        if 0 <= target < len(grid):
            self._select(grid[target][min(row, len(grid[target]) - 1)])

    def _select(self, card: Card) -> None:
        self.column_index = COLUMN_ORDER.index(card.column)
        self._render_tabs()
        card.focus(scroll_visible=False)
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
            self.call_from_thread(self.action_refresh)

        self.run_worker(work, thread=True, group="herdr")

    def action_primary(self) -> None:
        """Enter: go to the ticket's session; with none, start one (Ready, In progress) or review it (Review)."""
        card = self._card()
        if card is None:
            return
        ticket, sessions = card.ticket, self.sessions.get(card.ticket.number, [])
        if sessions:
            session = min(sessions, key=lambda s: (s.kind != "issue", s.status is None, s.workspace is None))
            self._run(lambda: self.workbench.go_to(ticket, session))
        elif card.column is Column.REVIEW:
            self.action_review()
        elif card.column in (Column.READY, Column.IN_PROGRESS):
            self.action_start()
        else:
            self.notify(f"#{ticket.number} has no worktree here; s starts one", severity="warning")

    def action_start(self) -> None:
        card = self._card()
        if card is None:
            return
        ticket = card.ticket
        if card.column is Column.DONE:
            self.notify(f"#{ticket.number} is done", severity="warning")
            return
        existing = [Path(s.path).name for s in self.sessions.get(ticket.number, [])]
        warn = f"\nalready has {', '.join(existing)}: this makes another" if existing else ""
        if card.column is Column.BLOCKED:
            warn += f"\nblocked by {ticket.open_blockers or 'a label'}"

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.starting.add(ticket.number)
                self._render_board()
                self._run(lambda: self.workbench.start(ticket))

        self.push_screen(Confirm(f"Start #{ticket.number} {ticket.title} in a new worktree?",
                                 self.describe_start(ticket) + warn, "Start"), confirmed)

    def action_review(self) -> None:
        card = self._card()
        if card is None:
            return
        ticket = card.ticket
        if card.column is not Column.REVIEW:
            self.notify(f"#{ticket.number} is not in Review", severity="warning")
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._run(lambda: self.workbench.review(ticket))

        self.push_screen(Confirm(f"Review #{ticket.number} {ticket.title} in a new worktree?",
                                 f"worktree: review-{ticket.number}; Claude checks out the PR and runs the code-review skill", "Review"),
                         confirmed)

    def action_remove(self) -> None:
        card = self._card()
        if card is None:
            return
        ticket, sessions = card.ticket, self.sessions.get(card.ticket.number, [])
        if card.column is not Column.DONE:
            self.notify("Only Done tickets' worktrees can be removed here", severity="warning")
            return
        if not sessions:
            self.notify(f"#{ticket.number} has no worktrees", severity="warning")
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._run(lambda: self.workbench.remove(ticket, sessions))

        paths = "\n".join(s.path for s in sessions)
        self.push_screen(Confirm(f"Remove the worktrees of #{ticket.number}?",
                                 f"{paths}\nherdr refuses if one has uncommitted changes", "Remove"), confirmed)

    def action_toggle_auto_remove(self) -> None:
        self.auto_remove = not self.auto_remove
        self.settings["auto_remove"] = self.auto_remove
        if self.settings_path:
            try:
                save_settings(self.settings_path, self.settings)
            except OSError as exc:
                self.notify(f"auto-remove not saved: {exc}", severity="warning")
        self.notify("Auto-remove on: Done tickets' worktrees go once their agent is idle; unsaved work is kept"
                    if self.auto_remove else "Auto-remove off")
        self._render_status()
        self._auto_remove_done()

    def action_help(self) -> None:
        self.push_screen(HelpScreen(key_help(self), self.board_query))

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.add_class("visible")
        box.value = self.needle
        box.cursor_position = len(self.needle)
        box.focus()

    def action_pick_filter(self) -> None:
        labels = sorted(self._board_labels(), key=str.lower)
        authors = sorted(self._board_authors(), key=str.lower)

        def changed() -> None:
            self._save_filters()
            self._render_board()

        self.push_screen(FilterPicker({"Labels": labels, "Authors": authors}, self.filters, changed,
                                      {"Authors": SELECT_CYCLE}),
                         lambda _: self.call_after_refresh(self.action_move, 0, 0))

    def _save_filters(self) -> None:
        """Saves the picker's selection per repo; a saved label or author no longer on the board stays saved."""
        filters = self.settings.setdefault("filters", {})
        filters[self.repo_key] = {"labels": dict(sorted(self.filters["Labels"].items())),
                                  "authors": sorted(self.filters["Authors"])}
        if self.settings_path:
            try:
                save_settings(self.settings_path, self.settings)
            except OSError as exc:
                self.notify(f"filter not saved: {exc}", severity="warning")

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
