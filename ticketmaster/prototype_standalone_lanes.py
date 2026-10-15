"""PROTOTYPE, throwaway: how spec lanes and the Loose lane look on the board (wayfinder ticket #91, map #88).

Three structurally different variants on the real BoardApp with canned tickets; `[` and `]` cycle them.
Run: <ticketmaster venv>/bin/python -m ticketmaster.prototype_standalone_lanes [A|B|C]
Nothing here talks to a tracker; `s` and `o` behave as on a board with no herdr.
"""

from __future__ import annotations

import sys
from dataclasses import replace

from rich.text import Text
from textual.binding import Binding

from . import board as B
from .model import Board, Column, Lane, MapProgress, Ticket, classify, COLUMN_ORDER

REPO = "https://github.com/kareblak/ticketmaster/issues/"


def t(number, title, open_=True, **kw) -> Ticket:
    return Ticket(**{"url": f"{REPO}{number}", **kw, "number": number, "title": title, "is_open": open_})


def wf(number, title, kind, **kw) -> Ticket:
    return t(number, title, labels=(f"wayfinder:{kind}",), parent_number=88, parent_url=f"{REPO}88",
             parent_labels=("wayfinder:map",), **kw)


MAP = t(88, "Show standalone to-spec specs and to-tickets tickets on the board", labels=("wayfinder:map",),
        body="## Destination\nA /to-spec issue ready for /to-tickets.")
MAP_TICKETS = [
    wf(89, "Can each provider find to-spec and to-tickets issues by body sections cheaply?", "research",
       assignees=("kareblak",), body="## Question\nSearch cost per provider."),
    wf(90, "What does pressing s on a standalone spec start?", "grilling", assignees=("kareblak",)),
    wf(91, "How do spec lanes and the Loose lane look?", "prototype", assignees=("kareblak",)),
    wf(92, "Which issues count as standalone, and how do they coexist with map links?", "grilling",
       open_blockers=1, blocked_by=("#89",)),
]

SPEC_BODY = "## Problem Statement\nThe board cannot be shared outside the terminal.\n\n## Solution\nExport it."
TICKET_BODY = "## What to build\n{}\n\n## Acceptance criteria\n- [ ] works"
SPECS = {
    120: (t(120, "Export the board as Markdown", body=SPEC_BODY, author="kareblak"), [
        t(121, "Render one lane as a Markdown table", False, body=TICKET_BODY.format("A lane as a table.")),
        t(122, "Write the export to a file with `e`", assignees=("kareblak",), has_open_change_request=True,
          body=TICKET_BODY.format("Bind e to export.")),
        t(123, "Include closed tickets behind a flag", body=TICKET_BODY.format("A --closed flag.")),
        t(124, "Document the export in the README", open_blockers=1, blocked_by=("#123",),
          body=TICKET_BODY.format("README section.")),
    ]),
    130: (t(130, "Refresh the board from GitHub webhooks", body=SPEC_BODY, assignees=("kareblak",)), [
        t(131, "Receive issue events on a local port", body=TICKET_BODY.format("A tiny HTTP listener.")),
        t(132, "Fall back to polling when no event came in a minute", body=TICKET_BODY.format("Fallback.")),
    ]),
    110: (t(110, "Colour cards by label", False, body=SPEC_BODY), [  # closed spec, one ticket still open
        t(111, "Pick colours from the label's own colour", body=TICKET_BODY.format("Use label colours.")),
    ]),
}
LOOSE = [
    t(140, "Show the tracker's rate limit in the status line", body=TICKET_BODY.format("Remaining calls.")),
    t(141, "Back off the poll when the limit is low", open_blockers=1, blocked_by=("#140",),
      body=TICKET_BODY.format("Slower poll.")),
    t(142, "Fix the focus jump after a refresh", assignees=("alice",), body=TICKET_BODY.format("Keep focus.")),
    t(143, "Trim trailing spaces in summaries", False, body=TICKET_BODY.format("Trim.")),
]
LOOSE_HEADER = t(0, "Loose tickets", url=f"{REPO}?q=is%3Aopen", key="Loose")

KIND: dict[int, str] = {}  # card number -> "spec" | "ticket" | "loose" | "band" | "done"
LANE_OF: dict[int, int] = {}  # standalone card number -> its lane header number


def progress(tickets) -> MapProgress:
    still = [x for x in tickets if x.is_open]
    return MapProgress(len(tickets) - len(still), len(still), sum(x.is_blocked for x in still))


def lane(header: Ticket, tickets, collapsed=False) -> Lane:
    out = Lane(header, progress=progress(tickets), collapsed=collapsed)
    if not collapsed:
        for x in sorted(tickets, key=lambda x: x.number):
            out.columns[classify(x)].append(x)
    return out


def map_lane() -> Lane:
    return lane(MAP, MAP_TICKETS)


def tag(variant: str) -> None:
    KIND.clear()
    LANE_OF.clear()
    for spec, (header, tickets) in SPECS.items():
        KIND[spec] = "spec"
        for x in tickets:
            KIND[x.number], LANE_OF[x.number] = "ticket", spec
    for x in LOOSE:
        KIND[x.number], LANE_OF[x.number] = "loose", 0
    KIND[0] = "loose-header"


# --- A: a spec lane is a map lane; the spec is the header ---------------------------------------------------

def board_a() -> Board:
    specs = [lane(h, ts) for _, (h, ts) in sorted(SPECS.items())]
    return Board([map_lane(), *specs, lane(LOOSE_HEADER, LOOSE)])


def header_a(ln: Lane, sessions=(), starting=False, claimed=False) -> Text:
    kind = KIND.get(ln.map.number)
    if kind not in ("spec", "loose-header"):
        return ORIGINAL["render_map"](ln, sessions, starting, claimed)
    text = B.session_lines(sessions, starting, claimed)
    p = ln.progress
    if kind == "spec":
        text.append(f"▣ {ln.map.ref} ", style="bold magenta")
        text.append(ln.map.title, style="bold")
        if not ln.map.is_open:
            text.append("  spec closed", style="grey50")
        if ln.map.assignees:
            text.append("  " + " ".join("@" + a for a in ln.map.assignees), style="italic cyan")
        text.append(f"\nspec · {p.decided} done · {p.open} open · {p.blocked} blocked", style="bold magenta")
    else:
        text.append("◇ Loose tickets", style="bold blue")
        text.append("  to-tickets output with no parent", style="grey62")
        text.append(f"\n{p.decided} done · {p.open} open · {p.blocked} blocked", style="bold blue")
    return text


def type_a(ticket: Ticket):
    return {"ticket": "impl", "loose": "impl"}.get(KIND.get(ticket.number)) or ORIGINAL["card_type"](ticket)


# --- B: the spec is a card in the columns; lanes interleave with maps by number; Loose on top --------------

def board_b() -> Board:
    lanes = [map_lane()]
    for number, (h, ts) in SPECS.items():
        label = replace(h, number=-number)  # a thin label; the spec itself is a card
        KIND[-number] = "spec-label"
        lanes.append(lane(label, [h, *ts]))
    lanes.sort(key=lambda ln: abs(ln.map.number))
    return Board([lane(LOOSE_HEADER, LOOSE), *lanes])


def header_b(ln: Lane, sessions=(), starting=False, claimed=False) -> Text:
    kind = KIND.get(ln.map.number)
    if kind == "spec-label":
        tickets = [x for x in ln.tickets() if KIND.get(x.number) == "ticket"]
        done = sum(not x.is_open for x in tickets)
        return Text.assemble(("▣ spec ", "bold magenta"), (f"#{-ln.map.number}", "magenta"),
                             (f"  {done}/{len(tickets)} tickets done", "grey62"))
    if kind == "loose-header":
        return Text.assemble(("◇ Loose", "bold blue"), (f"  {ln.progress.open} open, no parent", "grey62"))
    return ORIGINAL["render_map"](ln, sessions, starting, claimed)


def type_b(ticket: Ticket):
    return {"spec": "spec", "ticket": "ticket", "loose": "ticket"}.get(KIND.get(ticket.number)) \
        or ORIGINAL["card_type"](ticket)


# --- C: a Standalone band under the maps; compact headers; done tickets folded into a count ----------------

def fold_done(ln: Lane, number: int) -> Lane:
    done = ln.columns[Column.DECIDED]
    if done:
        stub = t(-1000 - number, f"✓ {len(done)} done", False, url=done[0].url)
        KIND[stub.number] = "done"
        ln.columns[Column.DECIDED] = [stub]
    return ln


def board_c() -> Board:
    band = t(-1, "Standalone work", url=REPO)
    KIND[-1] = "band"
    loose = fold_done(lane(LOOSE_HEADER, LOOSE), 0)
    specs = [fold_done(lane(h, ts), n) for n, (h, ts) in sorted(SPECS.items(), key=lambda kv: -kv[0])]
    return Board([map_lane(), Lane(band, collapsed=True), loose, *specs])


def bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total) if total else 0
    return "▰" * filled + "▱" * (width - filled)


def header_c(ln: Lane, sessions=(), starting=False, claimed=False) -> Text:
    kind = KIND.get(ln.map.number)
    if kind == "band":
        return Text("━━━━ Standalone work: specs from /to-spec, tickets from /to-tickets ━━━━", style="bold")
    p = ln.progress
    total = p.decided + p.open
    if kind == "spec":
        closed = "" if ln.map.is_open else " (spec closed)"
        return Text.assemble(B.session_lines(sessions, starting, claimed), (f"▣ {ln.map.ref} ", "bold magenta"),
                             (ln.map.title + closed, "bold"), ("  " + bar(p.decided, total), "magenta"),
                             (f" {p.decided}/{total}", "grey62"))
    if kind == "loose-header":
        return Text.assemble(("◇ Loose ", "bold blue"), (bar(p.decided, total), "blue"),
                             (f" {p.decided}/{total}", "grey62"))
    return ORIGINAL["render_map"](ln, sessions, starting, claimed)


def type_c(ticket: Ticket):
    return {"ticket": "ticket", "loose": "loose"}.get(KIND.get(ticket.number)) or ORIGINAL["card_type"](ticket)


# --- shared card rendering ----------------------------------------------------------------------------------

def card(ticket: Ticket, column: Column, sessions=(), starting=False, map_title="", out_of_scope=False,
         claimed=False) -> Text:
    kind = KIND.get(ticket.number)
    if kind == "done":
        return Text(ticket.title, style="grey50")
    if kind is None:
        return ORIGINAL["render_card"](ticket, column, sessions, starting, map_title, out_of_scope, claimed)
    text = ORIGINAL["render_card"](ticket, column, sessions, starting, "", out_of_scope, claimed)
    label = {"spec": " spec ", "ticket": " ticket ", "loose": " loose "}[kind]
    style = {"spec": "black on magenta", "ticket": "black on bright_white", "loose": "black on blue"}[kind]
    if VARIANT == "A" and kind != "spec":
        label, style = " impl ", "black on bright_white"
    chips = Text("\n").append(label, style=style)
    if kind == "spec":
        chips.append(" s → see ticket 90", style="grey62")
    elif VARIANT == "C" and kind == "ticket":
        chips.append(f" ▣ #{LANE_OF[ticket.number]}", style="grey62")
    return text.append(chips)


VARIANTS = {
    "A": ("Spec lane = map lane; Loose last", board_a, header_a, type_a,
          "order: maps, specs, Loose · type filter: standalone tickets count as impl"),
    "B": ("Spec card in the columns; lanes by number; Loose on top", board_b, header_b, type_b,
          "order: Loose, then maps and specs by number · type filter: spec, ticket"),
    "C": ("Standalone band; compact headers; done folded", board_c, header_c, type_c,
          "order: maps, band, Loose, specs newest first · type filter: ticket, loose"),
}
VARIANT = "A"
ORIGINAL = {"render_map": B.render_map, "render_card": B.render_card, "card_type": B.card_type,
            "CARD_TYPES": B.CARD_TYPES}


class NoTracker:
    name, repo = "prototype", "kareblak/ticketmaster"

    def viewer(self):
        return "kareblak"

    def fetch(self, query):
        return []


def apply_variant(key: str) -> Board:
    global VARIANT
    VARIANT = key
    _, build, header, kind, _ = VARIANTS[key]
    tag(key)
    B.render_map, B.render_card, B.card_type = header, card, kind
    B.CARD_TYPES = (*ORIGINAL["CARD_TYPES"], "ticket", "loose")
    return build()


class PrototypeApp(B.BoardApp):
    BINDINGS = [*B.BoardApp.BINDINGS, Binding("left_square_bracket", "variant(-1)", "Prev variant"),
                Binding("right_square_bracket", "variant(1)", "Next variant")]

    def __init__(self, start: str):
        super().__init__(NoTracker(), B.Query(), interval=3600)
        self.variant = start

    def _fetch(self, tickets: bool = True) -> None:
        board = apply_variant(self.variant)
        self.call_from_thread(self._apply, board, None, {})

    def _title(self) -> None:
        name, *_, note = VARIANTS[self.variant]
        self.sub_title = f"PROTOTYPE {self.variant}: {name} · {note} · [ ] cycles"

    def _render_status(self) -> None:
        self._title()
        super()._render_status()

    def action_variant(self, step: int) -> None:
        keys = list(VARIANTS)
        self.variant = keys[(keys.index(self.variant) + step) % len(keys)]
        self.board = apply_variant(self.variant)
        self.known = B.known_values(self.board)
        self._render_board()


if __name__ == "__main__":
    PrototypeApp((sys.argv[1:] or ["A"])[0].upper()).run()
