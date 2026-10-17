"""Live Textual swimlane board: a lane per wayfinder map and per standalone spec, and the Loose lane, its tickets in
four columns. It draws a BoardModel's snapshot with Rich."""

from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual import events
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Footer, Input, Label, Static

from .board_model import (ANYONE, ASSIGNEE, INCLUDE, LOOSE_TITLE, NO_ISSUE, Badge, BoardModel, CardView, GoTo,
                          HeaderView, Notice, Refusal, Setting, StartPlan, Workbench)
from .model import COLUMN_ORDER, Column, Lane, LaneKind, Ticket
from .providers import Provider, Query

NARROW_WIDTH = 100  # below this the "auto" view shows one column at a time
COLUMN_COLORS = {
    Column.BLOCKED: "red",
    Column.FRONTIER: "green",
    Column.CLAIMED: "yellow",
    Column.DECIDED: "grey50",
}
MAP_COLOR = "cyan"
SPEC_COLOR = "magenta"  # a standalone spec's lane
LOOSE_COLOR = "blue"  # the Loose lane
LANE_GLYPHS = {LaneKind.MAP: "◆", LaneKind.SPEC: "▣", LaneKind.LOOSE: "◇"}
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
SEGMENT_STYLES = {"title": "bold", "warn": "bold yellow", "filter": "yellow", "error": "bold red", "": ""}
NOTICE_SECONDS = 10  # a tracker error's notification stays up this long
DECIDED_LINES = 2  # a Decided card's height: its ref and title, truncated
GO_TO_LIVE = ": press Enter to go to it"  # how this board goes to the live session a start refuses for


def session_lines(badges: tuple[Badge, ...]) -> Text:
    """A line per badge: a session's agent state and worktree, starting, or claimed with no session here."""
    text = Text()
    for badge in badges:
        if badge.kind == "session":
            label, style = STATUS_STYLES.get(badge.status, STATUS_STYLES["unknown"])
            text.append(f"{label} ", style=style)
            text.append(f"{badge.worktree}\n", style="dim")
        elif badge.kind == "starting":
            text.append("▶ starting…\n", style="bold green")
        elif badge.kind == "claimed":
            text.append("◇ claimed, no session here\n", style="grey62")
    return text


def render_card(view: CardView) -> Text:
    """A ticket: its badges, ref, title and summary, then its chips, the lane's title after the lane's glyph. A
    Decided ticket is only its ref and title, which its Card clamps to DECIDED_LINES."""
    ticket, column = view.ticket, view.column
    if column is Column.DECIDED:
        return Text.assemble((f"{ticket.ref} ", f"bold {COLUMN_COLORS[column]}"), (ticket.title, "bold"))
    text = session_lines(view.badges)
    text.append(f"{ticket.ref} ", style=f"bold {COLUMN_COLORS[column]}")
    text.append(ticket.title, style="bold")
    if ticket.summary:
        text.append("\n" + ticket.summary, style="dim")
    chips = Text()
    moded = any(chip.kind == "mode" for chip in view.chips)
    for chip in view.chips:
        if chip.kind == "type":
            chips.append(chip.text if moded else f"{chip.text} ", style="bold")
        elif chip.kind == "mode":
            chips.append(" · ")
            chips.append(f" {chip.text} ", style=MODE_STYLES[chip.text])
            chips.append(" ")
        elif chip.kind == "pr":
            chips.append(" PR open ", style=PR_STYLE)
            chips.append(" ")
        elif chip.kind == "blockers":
            chips.append(f"⛔ {chip.text} ", style="red")
        elif chip.kind == "assignees":
            chips.append(" ".join("@" + a for a in chip.text.split()) + " ", style="italic cyan")
        elif chip.kind == "lane":
            chips.append(f"{LANE_GLYPHS[view.lane_kind]} {chip.text}", style="grey62")
    if chips.plain:
        text.append("\n")
        text.append(chips)
    return text


def render_lane_header(view: HeaderView) -> Text:
    """A lane's header. A map's: its title and its decided / open / blocked counts, then a line per spec with its
    tickets' done / open / blocked counts, or that it has none yet, and its sessions' badges. A standalone spec's: its
    title, whether it is closed, its assignees and its tickets' done / open / blocked counts. The Loose lane's: what it
    holds and its tickets' done / open / blocked counts."""
    lane = view.lane
    text = session_lines(view.badges)
    if lane.kind is LaneKind.LOOSE:
        return render_loose_lane(lane, text)
    if not lane.is_map:
        return render_spec_lane(lane, text)
    text.append(f"◆ {lane.header.ref} ", style=f"bold {MAP_COLOR}")
    text.append(lane.header.title, style="bold")
    if lane.collapsed:
        text.append("  closed", style="grey50")
    progress = lane.progress
    text.append(f"\n{progress.decided} decided · {progress.open} open · {progress.blocked} blocked",
                style=f"bold {MAP_COLOR}")
    for line in view.specs:
        spec, counts = line.spec, line.counts
        text.append(f"\n▣ {spec.ref} {'open' if spec.is_open else 'closed'}", style="bold")
        tickets = (f"{counts.done} done · {counts.open} open · {counts.blocked} blocked" if counts is not None
                   else "no tickets yet")
        text.append(f"  {tickets}", style="grey62")
        badges = session_lines(line.badges)
        badges.rstrip()
        if badges.plain:
            text.append("\n")
            text.append(badges)
    return text


def render_spec_lane(lane: Lane, text: Text) -> Text:
    """A spec lane's header after `text` (its sessions' badges): the spec, `spec closed` once it is, its assignees,
    then its tickets' done / open / blocked counts."""
    spec, progress = lane.header, lane.progress
    text.append(f"▣ {spec.ref} ", style=f"bold {SPEC_COLOR}")
    text.append(spec.title, style="bold")
    if not spec.is_open:
        text.append("  spec closed", style="grey50")
    if spec.assignees:
        text.append("  " + " ".join("@" + a for a in spec.assignees), style="italic cyan")
    text.append(f"\nspec · {progress.done} done · {progress.open} open · {progress.blocked} blocked",
                style=f"bold {SPEC_COLOR}")
    return text


def render_loose_lane(lane: Lane, text: Text) -> Text:
    """The Loose lane's header after `text`: what it holds, then its tickets' done / open / blocked counts."""
    progress = lane.progress
    text.append(f"{LANE_GLYPHS[LaneKind.LOOSE]} {LOOSE_TITLE}", style=f"bold {LOOSE_COLOR}")
    text.append("  to-tickets output with no parent", style="grey62")
    text.append(f"\n{progress.done} done · {progress.open} open · {progress.blocked} blocked",
                style=f"bold {LOOSE_COLOR}")
    return text



def clamp_lines(text: Text, width: int, lines: int) -> Text:
    """`text` wrapped to `width`, cut to its first `lines` lines, the last ending in … when anything was cut."""
    if width < 1:
        return text
    wrapped = text.wrap(Console(width=width), width)
    if len(wrapped) <= lines:
        return text
    kept = wrapped[:lines]
    last = kept[-1].copy()
    last.rstrip()
    last.truncate(width - 1)
    last.append("…", style="bold")
    return Text("\n").join([*kept[:-1], last])



class Card(Static, can_focus=True):
    """A ticket in a lane's column, or the issue heading its lane (column None); the Loose lane's header has none."""

    # Bound on the card itself: otherwise the lanes' VerticalScroll handles up/down as scrolling.
    BINDINGS = [
        Binding("up,k", "app.move(0,-1)", show=False),
        Binding("down,j", "app.move(0,1)", show=False),
        Binding("left,h", "app.move(-1,0)", show=False),
        Binding("right,l", "app.move(1,0)", show=False),
    ]

    def __init__(self, ticket: Ticket | None, column: Column | None, text: Text):
        classes = "card map-card" if column is None else "card decided-card" if column is Column.DECIDED else "card"
        super().__init__(text, classes=classes)
        self.ticket = ticket
        self.column = column
        self.text = text

    def render(self):
        """A Decided card is clamped to DECIDED_LINES at its current width; any other renders whole."""
        if self.column is Column.DECIDED:
            return clamp_lines(self.text, self.size.width, DECIDED_LINES)
        return super().render()

    @property
    def key(self) -> int | LaneKind:
        """What finds the card again after a refresh: its issue number, or the Loose lane for its header."""
        return self.ticket.number if self.ticket else LaneKind.LOOSE

    def on_click(self, event: events.Click) -> None:
        self.focus()
        if event.chain >= 2:
            self.app.action_primary()


class Cell(Vertical):
    """One column of one lane."""

    def __init__(self, column: Column, cards: list[Card]):
        super().__init__(*cards, classes="cell")
        self.column = column



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
    "start": "start /wayfinder for the ticket, /implement on an impl card, or /to-tickets on a map card whose spec "
             "has no tickets yet, in its own issue-<n> worktree, or continue Claude in it when it has no agent; not "
             "when closed, blocked, claimed by someone else or live (press Enter), never for an issue that is not "
             "wayfinder work",
    "open": "open the issue in the browser",
    "pick_filter": "narrow by type, HITL/AFK, assignee (me, unassigned, anyone), map and author; combines with /, "
                   "Esc on the board clears all, saved per repo",
    "settings": "the refresh interval and the Decided age, applied at once, the fetch limit, from the next fetch, "
                "and the budget on GitHub; saved per repo",
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
    text.append("  Map lanes come first, by number, then spec lanes, by number, then the Loose lane. A map\n"
                "  lane is an open wayfinder map, headed by its map card (◆): its decided (closed, not out of\n"
                "  scope), open and blocked tickets. Naming a map (ticketmaster <map>, a number or a tracker\n"
                "  key) filters this run's board to it, unsaved, and hides the spec lanes and the Loose lane;\n"
                "  Esc shows every lane again.\n"
                "  A map named by number shows also when closed, as its header card\n"
                f"  alone.{'  ← this board' if query.named_map else ''}\n"
                "  A spec whose first line is `Part of #<map>` is a line of the map card, ▣ #<spec> open or\n"
                "  closed, with its tickets' done (closed, less canceled), open and blocked counts, or no\n"
                "  tickets yet. Its implementation tickets (sub-issues, or a `## Parent` section naming the\n"
                "  spec) sit in the map's lane, tagged impl. The spec type filter keeps a lane with a spec.\n"
                "  A spec lane is a standalone spec, from /to-spec with no map link: `## Problem Statement`\n"
                "  and `## Solution`, no wayfinder: label. Its header card (▣) shows the spec's assignees,\n"
                "  spec closed once it is, and `spec · N done · N open · N blocked`. Its tickets, from\n"
                "  /to-tickets (`## What to build` and `## Acceptance criteria`, the spec as sub-issue parent\n"
                "  or in `## Parent`), fill the columns, tagged impl; the spec is tagged spec. The lane stays\n"
                "  while the spec or any of its tickets is open.\n"
                "  The Loose lane (◇) holds /to-tickets tickets whose parent is no spec, or that have none:\n"
                "  the open ones and those closed in the last 7 days, tagged impl, with their done (closed,\n"
                "  less canceled), open and blocked counts. It has no issue behind it, and shows only when it\n"
                "  has tickets.\n"
                "  PR open: an open pull request will close the card's issue.\n")

    heading("Columns (the first rule that matches)")
    for column, rule in [
        (Column.DECIDED, "closed, of any age; shown as its ref and title, cut to two lines"),
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
        ("wayfinder map", "s on the lane's header card, while its open spec has no tickets (open\n"
                          + " " * 22 + "or closed), starts to_tickets_command (default\n"
                          + " " * 22 + "/mattpocock-skills:to-tickets) with the spec's URL, focused, in the\n"
                          + " " * 22 + "spec's own worktree, also on a closed map. Else the map's first\n"
                          + " " * 22 + "frontier ticket (open, unblocked, unclaimed, no live session) in\n"
                          + " " * 22 + "that ticket's own worktree; with an empty frontier, the map itself\n"
                          + " " * 22 + "in its own worktree"),
        ("wayfinder ticket", "a wayfinder:research/prototype/grilling/task ticket (or wayfinder::…);\n"
                             + " " * 22 + "its map is the native parent, else `Part of #<map>` on the body's\n"
                             + " " * 22 + "first line"),
        ("standalone spec", "s on a spec lane's header card (▣), while the spec has no tickets\n"
                            + " " * 22 + "(neither open nor closed), starts to_tickets_command with the spec's URL,\n"
                            + " " * 22 + "focused, in the spec's own worktree. Once it has tickets, s\n"
                            + " " * 22 + "refuses it: s starts /implement on its tickets, one by one"),
        ("impl ticket", "an impl card, in a map lane or a spec lane: implement_command\n"
                        + " " * 22 + "(default /mattpocock-skills:implement) with the\n"
                        + " " * 22 + "ticket's URL, unfocused (AFK). On Linear the branch is the issue's\n"
                        + " " * 22 + "gitBranchName. Not with an open PR that closes it. The session owns\n"
                        + " " * 22 + "review, push and PR; the board starts nothing else"),
        ("refused", "a closed, blocked or someone else's ticket or ticketless spec, and\n"
                    + " " * 22 + "one with a live session (press Enter instead); a map's spec closed\n"
                    + " " * 22 + "without tickets is passed over. Any other issue is not board\n"
                    + " " * 22 + "work: the board starts work on wayfinder maps, standalone specs\n"
                    + " " * 22 + "and loose tickets. The board never removes a spec's worktree"),
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
                "  A map card goes to the map's own session, else its spec's (badged on the spec line),\n"
                "  never a ticket's.\n")

    heading("Settings")
    text.append("  c opens the settings menu: Enter applies the values at once and saves them per repo, Esc\n"
                "  closes it unchanged. Refresh interval: --interval for this run, then the repo's saved\n"
                "  interval, then the default (5 s, 30 s on Linear). Any positive number of seconds; a rate\n"
                "  limit still slows the board down. Fetch limit: the open maps the board reads, and the\n"
                "  tickets it reads per map (default 300), from the next fetch; a lower one asks the tracker\n"
                "  less. A fetch that reaches it says so in the status line, e.g. `cut off at 300: open maps,\n"
                "  #12's tickets`, so a missing ticket can be traced to the limit.\n"
                "  Decided age (days): hides Decided tickets closed more than N days ago, at once and without\n"
                "  a fetch; column heads and tabs count only the cards shown, lane headers keep the full\n"
                "  progress. Open tickets and those without a close date always show. Empty (the default)\n"
                "  shows every Decided ticket; 0 hides every one with a close date.\n"
                "  GitHub budget: the board spends about 1,000 GraphQL points an hour by default (any\n"
                "  positive number, saved per repo). Ticket fetches come every --interval, or every\n"
                "  last fetch's cost × 3600 / budget when that is longer, and longer still when the points\n"
                "  left would not last until the quota resets; the status line shows `every 22s · 3,412 pts\n"
                "  left`. Session refreshes keep the interval; r fetches at once, the next fetch one pace later.\n")

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
    .decided-card { max-height: 2; }
    .card:focus { background: $boost; border-left: wide $accent; }
    .empty { padding: 1 2; color: $text-muted; }
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
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        starting_dir: Path | None = None,
    ):
        """The board's state lives in its BoardModel, see there for the arguments; `columns` and the selection are
        this view's own."""
        super().__init__()
        self.view = columns  # "auto" (one column below NARROW_WIDTH), "one" or "all"
        self.single = False
        self.column_index = COLUMN_ORDER.index(Column.FRONTIER)
        self.grid: list[tuple[Card, list[list[Card]]]] = []  # per lane: its header and its cells' cards
        self.provider = provider
        self.workbench = workbench
        self.clock = clock
        self.model = BoardModel(provider, query, interval=interval, workbench=workbench, describe_start=describe_start,
                                settings_path=settings_path, clock=clock, map_key=map_key, wall=wall,
                                starting_dir=starting_dir)
        self.ticker: Timer | None = None
        self.sub_title = self.model.sub_title

    @property
    def interval(self) -> float:
        return self.model.interval

    @property
    def pacer(self):
        return self.model.pacer

    @property
    def filters(self) -> dict[str, dict[str, str]]:
        return self.model.filters

    @property
    def needle(self) -> str:
        return self.model.needle

    @property
    def starting(self) -> set[int]:
        return self.model.starting

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
        """Reads the starting markers, then fetches whatever the pacer says is due."""
        if self.model.read_markers():
            self._render_board()
        elif self.model.limited:
            self._render_status()
        self._fetch_in_background(self.model.due())

    def action_refresh(self) -> None:
        """Fetches now, except tickets while rate-limited: then only herdr sessions refresh."""
        if notice := self.model.refresh_notice():
            lasting = {} if self.model.limited else {"timeout": NOTICE_SECONDS}
            self.notify(notice.text, severity=notice.severity, **lasting)
        self._refresh_now()

    def _refresh_now(self) -> None:
        self._fetch_in_background(self.model.refresh())

    def _fetch_in_background(self, plan) -> None:
        if plan is None:
            return

        def work() -> None:
            try:
                result = self.model.fetch(plan)
            except BaseException:
                self.call_from_thread(self.model.fetch_failed)
                raise
            self.call_from_thread(self._apply, result)

        self.run_worker(work, thread=True, group="fetch")

    def _apply(self, result) -> None:
        applied = self.model.apply(result)
        for notice in self.model.drain_notices():
            self.notify(notice.text, severity=notice.severity, timeout=NOTICE_SECONDS)
        self._fetch_in_background(applied.next)
        if applied.changed:
            self._render_board()  # rebuilding resets scroll, so only when tickets changed
        else:
            self._render_status()

    def _notify(self, notice: Notice | None) -> None:
        if notice is not None:
            self.notify(notice.text, severity=notice.severity)

    def _render_board(self) -> None:
        container = self.query_one("#lanes", VerticalScroll)
        focused_widget = container.screen.focused  # the board's, also under the filter picker
        focused = focused_widget.key if isinstance(focused_widget, Card) else None
        container.remove_children()
        snapshot = self.model.snapshot()
        self.grid, lanes = [], []
        for lane in snapshot.lanes:
            header = Card(lane.header.ticket, None, render_lane_header(lane.header))
            cells = [[Card(view.ticket, column, render_card(view)) for view in views] for column, views in lane.cells]
            self.grid.append((header, cells))
            if lane.collapsed:
                lanes.append(Vertical(header, classes="lane"))
                continue
            row = [Cell(column, cards) for column, cards in zip(COLUMN_ORDER, cells)]
            for cell in row:
                cell.display = self._shown(cell.column)
            lanes.append(Vertical(header, Horizontal(*row, classes="lane-row"), classes="lane"))
        if snapshot.empty is not None:
            empty = snapshot.empty.text + ("; Esc clears them." if snapshot.empty.filtered else "")
            lanes.append(Static(empty, classes="empty"))  # no id: the previous one may still be attached
        container.mount_all(lanes)
        for column in COLUMN_ORDER:
            head = self.query_one(f"#head-{column.name}", Static)
            head.update(Text(f"{column.value} ({snapshot.counts[column]})", style=f"bold {COLUMN_COLORS[column]}"))
        refocus = next((card for card in self._cards() if card.key == focused), None)
        if refocus is not None:
            self.call_after_refresh(refocus.focus, scroll_visible=False)
        self._render_status()
        self._render_tabs()

    def _cards(self) -> list[Card]:
        """Every card in board order: each lane's header, then its cells left to right."""
        return [card for header, cells in self.grid for card in (header, *(c for cards in cells for c in cards))]

    def _render_status(self) -> None:
        status = Text()
        for segment in self.model.status():
            status.append(segment.text, style=SEGMENT_STYLES[segment.style])
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
        return sum(len(self.model.visible(lane, column)) for lane in self.model.shown_lanes())

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
            if self.focused.ticket is None:
                self.notify(NO_ISSUE, severity="warning")
            else:
                webbrowser.open(self.focused.ticket.url)

    # --- herdr actions ----------------------------------------------------------------------------

    def _card(self) -> Card | None:
        if not isinstance(self.focused, Card):
            self.notify("Select a ticket first", severity="warning")
            return None
        if self.focused.ticket is None:
            self.notify(NO_ISSUE, severity="warning")
            return None
        if self.workbench is None:
            self.notify("Sessions need herdr; run the board from a herdr pane", severity="warning")
            return None
        return self.focused

    def _run(self, job: Callable[[], Notice]) -> None:
        def work() -> None:
            notice = job()
            self.call_from_thread(self.notify, notice.text, severity=notice.severity)
            self.call_from_thread(self._refresh_now)

        self.run_worker(work, thread=True, group="herdr")

    def action_primary(self) -> None:
        """Enter, on any card: go to the card's own most urgent session, else its worktree with a shell; with
        neither, say what s would do there or why it refuses. It never starts or resumes Claude."""
        card = self._card()
        if card is None:
            return
        found = self.model.primary(card.ticket)
        if isinstance(found, GoTo):
            self._run(lambda: self.model.go_to(found))
        else:
            self.notify(self.model.hint_text(found, "s", GO_TO_LIVE), severity="warning")

    def action_start(self) -> None:
        """s: start /to-tickets on a map card's ticketless spec or a ticketless standalone spec card, /wayfinder on
        the card's ticket (/implement on an impl card), or on a map's first frontier ticket, else on the map itself;
        or say why it refuses."""
        card = self._card()
        if card is None:
            return
        ticket = card.ticket
        if not self.model.needs_map_order(ticket):
            self._offer_start(ticket, None)
            return

        def work() -> None:  # the tracker's own map order (GitHub sub-issue order) picks the frontier ticket
            self.call_from_thread(self._offer_start, ticket, self.model.map_order(ticket))

        self.run_worker(work, thread=True, group="herdr")

    def _offer_start(self, ticket: Ticket, order: list[int] | None) -> None:
        plan = self.model.start_plan(ticket, order)
        if isinstance(plan, Refusal):
            self.notify(plan.text + (GO_TO_LIVE if plan.live else ""), severity="warning")
        else:
            self._confirm_start(plan)

    def _confirm_start(self, plan: StartPlan) -> None:
        """Asks first; herdr then starts Claude in the ticket's own worktree, new, or continued with
        `claude --continue` when it exists without an agent."""
        ticket = plan.ticket

        def start() -> Notice:
            notice = self.model.start(ticket)
            if notice.severity == "error":
                self.call_from_thread(self.model.start_failed, ticket)
            return notice

        def confirmed(yes: bool | None) -> None:
            if not yes:
                return
            if refusal := self.model.begin_start(ticket):  # taken while the confirm was open
                self.notify(refusal.text + (GO_TO_LIVE if refusal.live else ""), severity="warning")
                return
            self._render_board()
            self._run(start)

        self.push_screen(Confirm(*self.model.confirm(plan)), confirmed)

    def action_help(self) -> None:
        self.push_screen(HelpScreen(key_help(self), self.model.board_query))

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.add_class("visible")
        box.value = self.needle
        box.cursor_position = len(self.needle)
        box.focus()

    def action_pick_filter(self) -> None:
        def changed() -> None:
            self._notify(self.model.save_filters())
            self._render_board()

        self.push_screen(FilterPicker(self.model.picker_sections(), self.model.filters, changed, {ASSIGNEE: ANYONE}),
                         lambda _: self.call_after_refresh(self.action_move, 0, 0))

    def action_settings(self) -> None:
        self.push_screen(SettingsMenu(self.model.settings_fields()), self._apply_settings)

    def _apply_settings(self, values: dict | None) -> None:
        """Applies and saves the menu's values (see BoardModel.apply_settings); Esc (None) changes nothing."""
        if values is None:
            return
        applied = self.model.apply_settings(values)
        if applied.interval:
            self._start_ticker()
        self._render_status()
        if applied.board:
            self._render_board()
            self.call_after_refresh(self.action_move, 0, 0)
        self._notify(applied.notice)

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.remove_class("visible")
        self._notify(self.model.clear_filters())
        self._render_board()
        self.call_after_refresh(self.action_move, 0, 0)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.model.needle = event.value.strip()
        event.input.remove_class("visible")
        self._render_board()
        self.call_after_refresh(self.action_move, 0, 0)
