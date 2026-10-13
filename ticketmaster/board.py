"""Live Textual kanban board."""

from __future__ import annotations

import webbrowser
import zlib
from datetime import datetime

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Input, Static

from .model import COLUMN_ORDER, Board, Column, Ticket, build_board
from .providers import Provider, ProviderError, Query

TAG_COLORS = {
    "security": "white on red",
    "ux": "black on magenta",
    "ui": "black on cyan",
    "perf": "black on yellow",
    "performance": "black on yellow",
    "code": "white on blue",
    "a11y": "black on green",
    "docs": "black on white",
}
FALLBACK_COLORS = ["white on dark_green", "white on purple4", "black on orange3", "white on grey37", "black on gold3"]
COLUMN_COLORS = {
    Column.BLOCKED: "red",
    Column.READY: "green",
    Column.IN_PROGRESS: "yellow",
    Column.REVIEW: "magenta",
    Column.DONE: "grey50",
}


def tag_style(kind: str) -> str:
    return TAG_COLORS.get(kind.lower()) or FALLBACK_COLORS[zlib.crc32(kind.lower().encode()) % len(FALLBACK_COLORS)]


def render_card(ticket: Ticket, column: Column) -> Text:
    text = Text()
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
    def __init__(self, ticket: Ticket, column: Column):
        super().__init__(render_card(ticket, column), classes="card")
        self.ticket = ticket


class BoardApp(App):
    TITLE = "ticketmaster"
    AUTO_FOCUS = None  # otherwise the hidden filter input grabs focus and eats keystrokes
    CSS = """
    Screen { layout: vertical; }
    #status { height: 1; padding: 0 1; background: $panel; }
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
        Binding("enter", "open", "Open"),
        Binding("up,k", "move(0,-1)", show=False),
        Binding("down,j", "move(0,1)", show=False),
        Binding("left,h", "move(-1,0)", show=False),
        Binding("right,l", "move(1,0)", show=False),
        Binding("escape", "clear_filter", show=False),
    ]

    def __init__(self, provider: Provider, query: Query, interval: float = 5.0):
        super().__init__()
        self.provider = provider
        self.board_query = query
        self.interval = interval
        self.board = Board()
        self.needle = ""
        self.last_error: str | None = None
        self.last_ok: datetime | None = None
        scope = f"parent #{query.parent}" if query.parent else f"label {query.label}" if query.label else "my work + ready"
        self.sub_title = f"{provider.name}:{provider.repo} · {scope}"

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="status")
        with Horizontal(id="columns"):
            for column in COLUMN_ORDER:
                with Vertical(classes="column"):
                    yield Static(column.value, classes="column-title", id=f"title-{column.name}")
                    yield VerticalScroll(id=f"col-{column.name}")
        yield Input(placeholder="filter: words match title, body, labels, assignees · esc clears", id="filter")
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()
        self.set_interval(self.interval, self.action_refresh)

    def action_refresh(self) -> None:
        self.run_worker(self._fetch, thread=True, exclusive=True, group="fetch")

    def _fetch(self) -> None:
        try:
            tickets = self.provider.fetch(self.board_query)
            board = build_board(tickets, done_days=self.board_query.done_days)
            self.call_from_thread(self._apply, board, None)
        except ProviderError as exc:
            self.call_from_thread(self._apply, None, str(exc))

    def _apply(self, board: Board | None, error: str | None) -> None:
        changed = False
        if board is not None:
            changed = board.columns != self.board.columns or self.last_ok is None
            self.board = board
            self.last_ok = datetime.now()
        self.last_error = error
        if changed:
            self._render_board()  # rebuilding resets scroll, so only when tickets changed
        else:
            self._render_status()

    def _render_board(self) -> None:
        focused = self.focused.ticket.number if isinstance(self.focused, Card) else None
        refocus: Card | None = None
        for column in COLUMN_ORDER:
            tickets = [t for t in self.board.columns[column] if matches(t, self.needle)]
            title = self.query_one(f"#title-{column.name}", Static)
            title.update(Text(f"{column.value} ({len(tickets)})", style=f"bold {COLUMN_COLORS[column]}"))
            container = self.query_one(f"#col-{column.name}", VerticalScroll)
            container.remove_children()
            cards = [Card(t, column) for t in tickets]
            container.mount_all(cards)
            refocus = refocus or next((c for c in cards if c.ticket.number == focused), None)
        if refocus is not None:
            self.call_after_refresh(refocus.focus, scroll_visible=False)
        self._render_status()

    def _render_status(self) -> None:
        status = Text(self.sub_title, style="bold")
        if self.last_ok:
            status.append(" · ")
            status.append(f"updated {self.last_ok:%H:%M:%S} · {self.board.total()} tickets · every {self.interval:g}s")
        if self.needle:
            status.append(f" · filter: {self.needle}", style="yellow")
        if self.last_error:
            status.append(f"  ⚠ {self.last_error}", style="bold red")
        self.query_one("#status", Static).update(status)

    def _cards_by_column(self) -> list[list[Card]]:
        return [list(self.query_one(f"#col-{c.name}", VerticalScroll).query(Card)) for c in COLUMN_ORDER]

    def action_move(self, dx: int, dy: int) -> None:
        grid = self._cards_by_column()
        if not any(grid):
            return
        if not isinstance(self.focused, Card):
            next(col for col in grid if col)[0].focus()
            return
        col = next(i for i, cards in enumerate(grid) if self.focused in cards)
        row = grid[col].index(self.focused)
        if dy:
            grid[col][max(0, min(len(grid[col]) - 1, row + dy))].focus()
            return
        target = col + dx
        while 0 <= target < len(grid) and not grid[target]:
            target += dx
        if 0 <= target < len(grid):
            grid[target][min(row, len(grid[target]) - 1)].focus()

    def action_open(self) -> None:
        if isinstance(self.focused, Card):
            webbrowser.open(self.focused.ticket.url)

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
