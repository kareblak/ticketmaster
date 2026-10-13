"""Live Textual kanban board."""

from __future__ import annotations

import webbrowser
import zlib
from collections.abc import Callable
from datetime import datetime

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual import events
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, Static

from .launch import Placement
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
NARROW_WIDTH = 100  # below this the "auto" view shows one column at a time
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


def render_card(ticket: Ticket, column: Column, started: bool = False) -> Text:
    text = Text()
    if started:
        text.append("▶ session started\n", style="bold green")
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

    def __init__(self, ticket: Ticket, column: Column, started: bool = False):
        super().__init__(render_card(ticket, column, started), classes="card")
        self.ticket = ticket
        self.column = column

    def on_click(self, event: events.Click) -> None:
        self.focus()
        if event.chain >= 2:
            self.app.action_start()


class ColumnScroll(VerticalScroll, can_focus=False):
    """Scrolls with the mouse wheel and follows the focused card, but never takes focus itself."""


PLACEMENT_KEYS = {
    Placement.WORKSPACE: ("w", "Worktree workspace"),
    Placement.TAB: ("t", "New tab"),
    Placement.SPLIT_RIGHT: ("r", "Pane right"),
    Placement.SPLIT_DOWN: ("d", "Pane down"),
    Placement.AUTO: ("a", "Auto"),
}
DEFAULT_PLACEMENTS = (Placement.TAB, Placement.SPLIT_RIGHT, Placement.SPLIT_DOWN, Placement.AUTO)
HERDR_PLACEMENTS = (Placement.WORKSPACE, Placement.TAB, Placement.SPLIT_RIGHT, Placement.SPLIT_DOWN)


class ConfirmStart(ModalScreen[Placement | None]):
    """Asks where to open the session; a stray double-click is cancelled with n or Esc."""

    DEFAULT_CSS = """
    ConfirmStart { align: center middle; }
    #dialog { width: 96; max-width: 100%; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    #prompt { margin-bottom: 1; }
    #buttons { height: auto; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """
    BINDINGS = [Binding("n,escape", "answer('')", "Cancel")]

    def __init__(self, ticket: Ticket, detail: str, placements: tuple[Placement, ...] = DEFAULT_PLACEMENTS):
        super().__init__()
        self.ticket = ticket
        self.detail = detail
        self.placements = placements

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(f"Start #{self.ticket.number} {self.ticket.title} in a new session?", style="bold"), id="prompt")
            yield Label(Text(self.detail, style="dim"))
            with Horizontal(id="buttons"):
                yield Button("Cancel (n)", id="cancel")
                for placement in self.placements:
                    key, text = PLACEMENT_KEYS[placement]
                    yield Button(f"{text} ({key})", id=placement.value.replace(" ", "-"), variant="primary")

    def on_key(self, event: events.Key) -> None:
        for placement in self.placements:
            if event.key == PLACEMENT_KEYS[placement][0]:
                event.stop()
                self.dismiss(placement)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_answer(event.button.id.replace("-", " "))

    def action_answer(self, answer: str) -> None:
        chosen = next((p for p in self.placements if p.value == answer), None)
        self.dismiss(chosen)


PLACEMENT_HELP = {
    Placement.WORKSPACE: "herdr creates a worktree workspace and starts Claude in it as a herdr agent",
    Placement.TAB: "a new tab running claude -w issue-<n>",
    Placement.SPLIT_RIGHT: "a pane to the right running claude -w issue-<n>",
    Placement.SPLIT_DOWN: "a pane below running claude -w issue-<n>",
    Placement.AUTO: "tiles session panes around the board (Ghostty on macOS)",
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
        rows.append((keys, binding.description))
    return rows


def help_text(keys: list[tuple[str, str]], placements: tuple[Placement, ...], query: Query) -> Text:
    text = Text()

    def heading(title: str) -> None:
        text.append(("\n" if text else "") + title + "\n", style="bold underline")

    heading("Keys")
    width = max(len(k) for k, _ in keys)
    for keys_, description in keys:
        text.append(f"  {keys_:<{width}}  ", style="bold cyan")
        text.append(description + "\n")
    text.append("  Click selects a card, double-click starts a session on it.\n", style="dim")

    heading("Columns")
    for column, rule in [
        (Column.BLOCKED, "open, with an open blocking issue or a 'blocked' label"),
        (Column.READY, "open, unassigned and not blocked: these can be started"),
        (Column.IN_PROGRESS, "open and assigned to someone"),
        (Column.REVIEW, "a review:<kind> label or an open pull/merge request"),
        (Column.DONE, f"closed in the last {query.done_days} days"),
    ]:
        text.append(f"  {column.value:<13}", style=f"bold {COLUMN_COLORS[column]}")
        text.append(rule + "\n")

    heading("Scope")
    current = "parent" if query.parent else "--label" if query.label else "no filter"
    for name, rule in [
        ("no filter", "your open issues, unassigned open issues and your recently closed ones"),
        ("parent", "the sub-issues of one issue, e.g. a wayfinder map (ticketmaster 42)"),
        ("--label", "issues with that label (ticketmaster --label NAME)"),
    ]:
        active = name == current
        text.append(f"  {name:<11}", style="bold" if active else "")
        text.append(rule + ("  ← this board" if active else "") + "\n")

    heading("Starting a session")
    text.append("  Select a Ready card and press s. Pick where it opens (n or Esc cancels):\n")
    for placement in placements:
        key, label = PLACEMENT_KEYS[placement]
        text.append(f"  {key}  ", style="bold cyan")
        text.append(f"{label}: {PLACEMENT_HELP[placement]}\n")
    text.append("  Each issue gets its own git worktree, issue-<n>. The prompt depends on the ticket:\n")
    for kind, prompt in [
        ("wayfinder map", "the wayfinder skill with the map's URL"),
        ("wayfinder ticket", "the wayfinder skill with the map's and the ticket's URLs"),
        ("plain issue", "work on the issue, assigning it to yourself first"),
    ]:
        text.append(f"  {kind:<18}", style="bold")
        text.append(prompt + "\n")

    heading("The board only reads")
    text.append("  It never writes to the tracker. A session claims its ticket by assigning it, so the\n"
                "  card moves to In progress on the next refresh; until then it shows ▶ session started.")
    return text


class HelpScreen(ModalScreen[None]):
    """What the board shows and how to drive it. Esc, ? or q closes it."""

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help { width: 100; max-width: 100%; height: auto; max-height: 100%; padding: 1 2; border: thick $accent; background: $surface; }
    """
    BINDINGS = [Binding("escape,question_mark,q", "dismiss", "Close")]

    def __init__(self, keys: list[tuple[str, str]], placements: tuple[Placement, ...], query: Query):
        super().__init__()
        self.text = help_text(keys, placements, query)

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
        Binding("enter", "open", "Open"),
        Binding("s", "start", "Start session"),
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
        starter: Callable[[Ticket, Placement], str] | None = None,
        describe_start: Callable[[Ticket], str] | None = None,
        placements: tuple[Placement, ...] = DEFAULT_PLACEMENTS,
        columns: str = "auto",
    ):
        super().__init__()
        self.placements = placements
        self.view = columns  # "auto" (one column below NARROW_WIDTH), "one" or "all"
        self.single = False
        self.column_index = COLUMN_ORDER.index(Column.READY)
        self.provider = provider
        self.starter = starter
        self.describe_start = describe_start or (lambda ticket: "")
        self.started: set[int] = set()
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
            cards = [Card(t, column, t.number in self.started) for t in tickets]
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
        if self.last_error:
            status.append(f"  ⚠ {self.last_error}", style="bold red")
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

    def action_start(self) -> None:
        card = self.focused
        if not isinstance(card, Card):
            self.notify("Select a Ready ticket first", severity="warning")
            return
        ticket = card.ticket
        if card.column is not Column.READY:
            self.notify(f"Only Ready tickets can be started; #{ticket.number} is {card.column.value}", severity="warning")
            return
        if self.starter is None:
            self.notify("Starting sessions is not available here", severity="warning")
            return
        if ticket.number in self.started:
            self.notify(f"A session for #{ticket.number} was already started from this board", severity="warning")
            return

        def confirmed(placement: Placement | None) -> None:
            if placement is not None:
                self.started.add(ticket.number)
                self._render_board()
                self.run_worker(lambda: self._start(ticket, placement), thread=True, group="start")

        self.push_screen(ConfirmStart(ticket, self.describe_start(ticket), self.placements), confirmed)

    def _start(self, ticket: Ticket, placement: Placement) -> None:
        message = self.starter(ticket, placement)
        self.call_from_thread(self.notify, message)

    def action_help(self) -> None:
        self.push_screen(HelpScreen(key_help(self), self.placements, self.board_query))

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
