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
    if ticket.assignees:
        chips.append(" ".join("@" + a for a in ticket.assignees), style="italic cyan")
    if chips.plain:
        text.append("\n")
        text.append(chips)
    return text


def matches(ticket: Ticket, needle: str) -> bool:
    if not needle:
        return True
    haystack = " ".join([str(ticket.number), ticket.title, ticket.body, *ticket.labels, *ticket.assignees]).lower()
    return all(word in haystack for word in needle.lower().split())


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
        Binding("enter", "primary", "Go/start"),
        Binding("s", "start", "New worktree"),
        Binding("c", "review", "Review"),
        Binding("x", "remove", "Remove worktree"),
        Binding("a", "toggle_auto_remove", "Auto-remove"),
        Binding("o", "open", "Browser"),
        Binding("v", "toggle_view", "One/all columns"),
        *[Binding(str(i + 1), f"column({i})", show=False) for i in range(len(COLUMN_ORDER))],
        Binding("up,k", "move(0,-1)", show=False),
        Binding("down,j", "move(0,1)", show=False),
        Binding("left,h", "move(-1,0)", show=False),
        Binding("right,l", "move(1,0)", show=False),
        Binding("escape", "clear_filter", show=False),
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
        self.board_query = query
        self.interval = interval
        self.board = Board()
        self.needle = ""
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
        yield Input(placeholder="filter: words match title, body, labels, assignees · esc clears", id="filter")
        yield Footer()

    def on_mount(self) -> None:
        self._apply_view()
        self.action_refresh()
        self.set_interval(self.interval, self.action_refresh)

    def action_refresh(self) -> None:
        self.run_worker(self._fetch, thread=True, exclusive=True, group="fetch")

    def _fetch(self) -> None:
        sessions, herdr_error = self.sessions, None
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

    def _render_board(self) -> None:
        focused = self.focused.ticket.number if isinstance(self.focused, Card) else None
        refocus: Card | None = None
        for column in COLUMN_ORDER:
            tickets = [t for t in self.board.columns[column] if matches(t, self.needle)]
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
        if self.auto_remove:
            status.append(" · auto-remove on", style="green")
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
        return sum(1 for t in self.board.columns[column] if matches(t, self.needle))

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
                                 f"worktree: review-{ticket.number}; Claude checks out the PR and reviews it", "Review"),
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

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.add_class("visible")
        box.value = self.needle
        box.cursor_position = len(self.needle)
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.remove_class("visible")
        self.needle = ""
        self._render_board()
        self.call_after_refresh(self.action_move, 0, 0)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.needle = event.value.strip()
        event.input.remove_class("visible")
        self._render_board()
        self.call_after_refresh(self.action_move, 0, 0)
