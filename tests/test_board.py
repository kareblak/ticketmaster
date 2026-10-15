import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from textual.widgets import Input

from ticketmaster.board import (BoardApp, Card, Cell, FilterPicker, HelpScreen, Pacer, PickerOption, SettingsMenu,
                               passes_filters, render_card)
from ticketmaster.herdr import HerdrError, Session
from ticketmaster.model import Column, Ticket
from ticketmaster.providers import ProviderError, Query, RateLimited
from ticketmaster.config import load_config
from ticketmaster.start import build_prompt, workbench_for


class FakeProvider:
    name = "fake"
    repo = "o/r"

    def __init__(self, tickets, login="me"):
        self.tickets = tickets
        self.calls = 0
        self.queries = []
        self.login = login

    def viewer(self):
        if isinstance(self.login, Exception):
            raise self.login
        return self.login

    def fetch(self, query):
        self.calls += 1
        self.queries.append(query)
        if isinstance(self.tickets, Exception):
            raise self.tickets
        return self.tickets


def a_map(number, title="", **kw):
    base = dict(number=number, title=title or f"Map {number}", url=f"https://gh/o/r/issues/{number}", is_open=True,
                labels=("wayfinder:map",))
    return Ticket(**{**base, **kw})


def on(map_number, number, title="", kind="grilling", **kw):
    """A decision ticket of map #<map_number>."""
    base = dict(number=number, title=title or f"Question {number}", url=f"u{number}", is_open=True,
                labels=(f"wayfinder:{kind}",), parent_number=map_number,
                parent_url=f"https://gh/o/r/issues/{map_number}", parent_labels=("wayfinder:map",))
    return Ticket(**{**base, **kw})


MAP = a_map(9, "Pick a stack")
TICKETS = [
    MAP,
    on(9, 1, "Ready one", "task", body="## Question\nWhat now?"),
    on(9, 2, "Doing", author="alice", assignees=("me",)),
    on(9, 3, "Waits", open_blockers=1, blocked_by=("#1",)),
    on(9, 4, "Shipped", is_open=False, closed_at=datetime.now(timezone.utc)),
]


def run(coro):
    return asyncio.run(coro)


def shown_lanes(app):
    """{map: {column: [ticket numbers]}} for the lanes on screen; an empty column is left out."""
    lanes = {}
    for lane in app.query(".lane"):
        header = lane.query(Card).first()
        lanes[header.ticket.number] = {cell.column.name: [c.ticket.number for c in cell.query(Card)]
                                       for cell in lane.query(Cell) if cell.query(Card)}
    return lanes


def headers(app):
    return [c.ticket.number for c in app.query(Card) if c.column is None]


def card_text(app, number):
    return str(next(c for c in app.query(Card) if c.ticket.number == number).render())


async def select(app, pilot, number):
    next(c for c in app.query(Card) if c.ticket.number == number).focus()
    await pilot.pause()


def test_each_open_map_is_a_lane_headed_by_its_map_card_and_other_issues_are_absent():
    async def scenario():
        tickets = [a_map(20, "Ship billing"), *TICKETS, on(20, 21, kind="research"),
                   Ticket(number=30, title="Plain bug", url="u30", is_open=True, labels=("bug",))]
        app = BoardApp(FakeProvider(tickets), Query(), interval=60)
        async with app.run_test(size=(160, 50)) as pilot:
            await loaded(app, pilot)
            assert headers(app) == [9, 20]
            assert shown_lanes(app) == {9: {"BLOCKED": [3], "FRONTIER": [1], "CLAIMED": [2], "DECIDED": [4]},
                                        20: {"FRONTIER": [21]}}
            assert "Pick a stack" in card_text(app, 9) and "Ship billing" in card_text(app, 20)
            assert 30 not in [c.ticket.number for c in app.query(Card)]
            assert "2 maps · 5 tickets" in status_of(app)

    run(scenario())


def test_tickets_land_by_the_classification_and_out_of_scope_ones_are_marked_in_decided():
    closed = dict(is_open=False, closed_at=datetime.now(timezone.utc))
    map_ticket = a_map(1, body="## Out of scope\n\n- Mobile (#23)\n")
    tickets = [
        map_ticket,
        on(1, 10, open_blockers=1, blocked_by=("#11",), assignees=("bob",)),  # a blocker wins over the claim
        on(1, 11, assignees=("bob",)),
        on(1, 12, labels=("wayfinder:grilling", "blocked", "review:ux"), has_open_change_request=True),
        on(1, 21, not_planned=True, **closed),  # canceled
        on(1, 22, **closed),  # decided
        on(1, 23, **closed),  # linked from ## Out of scope
        on(1, 24, labels=("wayfinder:grilling", "wontfix"), closed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
           is_open=False),  # old and wontfix: still decided
    ]

    async def scenario():
        app = BoardApp(FakeProvider(tickets), Query(), interval=60)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            assert shown_lanes(app) == {1: {"BLOCKED": [10], "FRONTIER": [12], "CLAIMED": [11],
                                            "DECIDED": [21, 22, 23, 24]}}
            assert "out of scope" in card_text(app, 21) and "out of scope" in card_text(app, 23)
            assert "out of scope" not in card_text(app, 22) and "out of scope" not in card_text(app, 24)

    run(scenario())


def test_cards_follow_map_then_sub_issue_order_then_number_and_the_map_card_counts():
    map_ticket = a_map(1, body="## Out of scope\n- #9\n")
    tickets = [map_ticket, on(1, 5, order=1), on(1, 6, order=2), on(1, 3, order=1), on(1, 7, order=0),
               on(1, 8, open_blockers=1), on(1, 2, order=3, open_blockers=1),
               on(1, 4, is_open=False, order=1), on(1, 9, is_open=False, order=0)]

    async def scenario():
        app = BoardApp(FakeProvider(tickets), Query(), interval=60)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            assert shown_lanes(app) == {1: {"BLOCKED": [8, 2], "FRONTIER": [7, 3, 5, 6], "DECIDED": [9, 4]}}
            assert "1 decided · 6 open · 2 blocked" in card_text(app, 1)  # #9 is out of scope, no decision

    run(scenario())


def test_a_closed_map_is_hidden_unless_named_and_then_shows_as_its_header_alone():
    tickets = [*TICKETS, a_map(40, "Old effort", is_open=False), on(40, 41, is_open=False), on(40, 42)]

    async def scenario(query, expect_headers):
        provider = FakeProvider(tickets)
        app = BoardApp(provider, query, interval=60)
        async with app.run_test(size=(160, 50)) as pilot:
            await loaded(app, pilot)
            assert provider.queries[0] == query and headers(app) == expect_headers
            if 40 in expect_headers:
                assert 41 not in [c.ticket.number for c in app.query(Card)]
                assert 42 not in [c.ticket.number for c in app.query(Card)]
                assert "closed" in card_text(app, 40) and "1 decided · 1 open · 0 blocked" in card_text(app, 40)
                assert "open maps + #40" in status_of(app)

    run(scenario(Query(), [9]))
    run(scenario(Query(named_map=40), [40]))  # the map filter is preset to it; Esc shows the open maps too


def test_ticket_cards_show_type_mode_assignee_blockers_and_the_maps_name():
    text = render_card(on(9, 3, kind="research", assignees=("bob",), open_blockers=2, blocked_by=("#1", "o/x#4")),
                       Column.BLOCKED, lane_title="Pick a stack").plain
    assert "research ·  AFK " in text and "@bob" in text and "⛔ #1 o/x#4" in text and "◆ Pick a stack" in text


def test_board_starts_on_the_maps_header_and_arrows_walk_the_lane():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down")
            assert app.focused.ticket.number == 9
            await pilot.press("down")
            assert app.focused.ticket.number == 1  # into the Frontier column
            await pilot.press("right")
            assert app.focused.ticket.number == 2
            await pilot.press("right")
            assert app.focused.ticket.number == 4
            await pilot.press("left", "left", "left")
            assert app.focused.ticket.number == 3
            await pilot.press("up")
            assert app.focused.ticket.number == 9

    run(scenario())


def test_filter_and_refresh_keeps_focus():
    async def scenario():
        provider = FakeProvider(TICKETS)
        app = BoardApp(provider, Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("slash", *"doing", "enter")
            await pilot.pause()
            assert shown_numbers(app) == [2] and headers(app) == [9]
            await select(app, pilot, 2)
            await pilot.press("r")
            await loaded(app, pilot)
            assert provider.calls >= 2 and app.focused.ticket.number == 2
            await pilot.press("escape")
            await pilot.pause()
            assert shown_numbers(app) == [1, 2, 3, 4]

    run(scenario())


def test_text_filter_matches_the_author():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("slash", *"alice", "enter")
            await pilot.pause()
            assert shown_numbers(app) == [2]

    run(scenario())


def test_no_open_maps_or_specs_says_so():
    async def scenario():
        app = BoardApp(FakeProvider([Ticket(number=1, title="Plain", url="u1", is_open=True)]), Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert "No open wayfinder maps or specs in o/r." in str(app.query_one("#empty").render())
            assert not list(app.query(Card))

    run(scenario())


def test_provider_error_is_shown_in_status():
    async def scenario():
        app = BoardApp(FakeProvider(ProviderError("gh: not logged in")), Query(), interval=60)
        async with app.run_test(size=(120, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "not logged in" in str(app.query_one("#status").render())

    run(scenario())


class FakeBench:
    """Records the board's herdr actions; `sessions` is what herdr reports."""

    def __init__(self, sessions=None):
        self.calls = []
        self.current = sessions or {}

    def sessions(self):
        return self.current

    def start(self, ticket):
        self.calls.append(("start", ticket.number))
        return f"Started #{ticket.number}"

    def go_to(self, ticket, session):
        self.calls.append(("go_to", ticket.number, session.path))
        return "ok"


def bench_app(bench, tickets=TICKETS, login="me"):
    return BoardApp(FakeProvider(tickets, login), Query(), interval=60, workbench=bench)


async def loaded(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


def is_confirm(app):
    return type(app.screen).__name__ == "Confirm"


def test_start_frontier_ticket_after_confirm():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("s")
            await pilot.pause()
            assert is_confirm(app)
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 1)]
            assert "starting" in card_text(app, 1)

    run(scenario())


BLOCKED = on(9, 5, "Waits", blocked_by=("#1", "#9"), open_blockers=2)
THEIRS = on(9, 6, "Bob's", assignees=("bob",))


def test_s_refuses_a_blocked_ticket_naming_its_blockers_and_enter_says_it_has_no_session():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, [MAP, BLOCKED])
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 5)
            for key in ("s", "enter"):
                await pilot.press(key)
                await pilot.pause()
                assert not is_confirm(app)
            await loaded(app, pilot)
            assert bench.calls == []
            assert [m for m, _ in notifications(app)][-2:] == [
                "#5 is blocked by #1, #9", "#5 has no session here; s refuses: #5 is blocked by #1, #9"]

    run(scenario())


def test_s_refuses_a_ticket_claimed_by_someone_else_and_enter_says_it_has_no_session():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, [MAP, THEIRS])
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 6)
            await pilot.press("s")
            await pilot.pause()
            assert not is_confirm(app)
            await pilot.press("enter")
            await pilot.pause()
            assert not is_confirm(app) and bench.calls == []
            assert [m for m, _ in notifications(app)][-2:] == [
                "#6 is claimed by @bob", "#6 has no session here; s refuses: #6 is claimed by @bob"]

    run(scenario())


def test_own_claimed_ticket_still_starts():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, [MAP, THEIRS], login="Bob")
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 6)
            await pilot.press("s")
            await pilot.pause()
            assert is_confirm(app)
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 6)]

    run(scenario())


def test_unknown_viewer_does_not_refuse_a_claimed_ticket():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, [MAP, THEIRS], login=ProviderError("gh: not logged in"))
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 6)
            await pilot.press("s")
            await pilot.pause()
            assert is_confirm(app)

    run(scenario())


def test_s_refuses_a_decided_ticket_and_a_closed_map():
    async def scenario():
        bench = FakeBench()
        app = BoardApp(FakeProvider([*TICKETS, a_map(40, is_open=False)]), Query(named_map=40), interval=60,
                       workbench=bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("escape")  # off the map preset: every open map and the named closed one
            await pilot.pause()
            for number in (4, 40):
                await select(app, pilot, number)
                await pilot.press("s")
                await pilot.pause()
                assert not is_confirm(app)
            assert bench.calls == []
            assert [m for m, _ in notifications(app)][-2:] == ["#4 is closed", "#40 is closed"]

    run(scenario())


def test_cancel_does_not_start():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("s", "n")
            await loaded(app, pilot)
            assert bench.calls == []

    run(scenario())


def test_enter_on_a_card_without_session_never_starts_and_says_so():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == []
            assert notifications(app)[-1][0] == "#1 has no session here; s starts /wayfinder on it"

    run(scenario())


def test_double_click_acts_like_enter_and_never_starts():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            card = next(c for c in app.query(Card) if c.ticket.number == 1)
            await pilot.click(card, times=2)
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == []
            assert notifications(app)[-1][0] == "#1 has no session here; s starts /wayfinder on it"

    run(scenario())


def live(number, path, status, pane="w5:p1"):
    return Session(number, path, workspace=pane.split(":")[0], status=status, pane=pane)


def test_each_session_shows_its_badge_and_a_claimed_ticket_without_one_says_so():
    async def scenario():
        bench = FakeBench({
            1: [live(1, "/w/tm-1", "working"), live(1, "/code/r", "idle", "w6:p2"), Session(1, "/w/issue-1-2")],
            3: [live(3, "/w/issue-3", "blocked")],
        })
        tickets = [*TICKETS, on(9, 7, "Bob works", assignees=("bob",)), on(9, 8, "Mine, live", assignees=("me",))]
        bench.current[8] = [live(8, "/w/tm-8", "idle")]
        app = bench_app(bench, tickets)
        async with app.run_test(size=(160, 60)) as pilot:
            await loaded(app, pilot)
            one = card_text(app, 1)
            assert "● working tm-1" in one and "◌ idle r" in one and "○ no agent issue-1-2" in one
            assert "⚠ needs you issue-3" in card_text(app, 3)
            for claimed in (2, 7):  # mine or someone else's, with no session found
                assert "claimed, no session here" in card_text(app, claimed)
            for other in (1, 3, 4, 8, 9):  # a session, unclaimed, or closed
                assert "claimed, no session here" not in card_text(app, other)

    run(scenario())


def test_without_herdr_a_claimed_card_does_not_claim_there_is_no_session():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert "no session here" not in card_text(app, 2)

    run(scenario())


def test_enter_focuses_the_most_urgent_live_session_on_any_card():
    """Closed, blocked, claimed by someone else or a map: a live session is always where Enter goes."""
    sessions = {
        number: [Session(number, f"/w/dead-{number}", workspace="wD"), live(number, f"/w/idle-{number}", "idle"),
                 live(number, f"/w/needs-{number}", "blocked"), live(number, f"/w/busy-{number}", "working")]
        for number in (9, 3, 4, 6)
    }

    async def scenario():
        bench = FakeBench(sessions)
        app = bench_app(bench, [*TICKETS, THEIRS])
        async with app.run_test(size=(160, 50)) as pilot:
            await loaded(app, pilot)
            for number in (9, 3, 4, 6):
                await select(app, pilot, number)
                await pilot.press("enter")
                await loaded(app, pilot)
            assert not is_confirm(app)
            assert bench.calls == [("go_to", n, f"/w/needs-{n}") for n in (9, 3, 4, 6)]

    run(scenario())


def test_enter_prefers_working_over_idle_and_any_agent_over_a_dead_worktree():
    async def scenario():
        bench = FakeBench({1: [Session(1, "/w/dead", workspace="wD"), live(1, "/w/idle", "idle"),
                               live(1, "/w/busy", "working")],
                           2: [Session(2, "/w/dead-2"), live(2, "/w/idle-2", "idle")]})
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            for number in (1, 2):
                await select(app, pilot, number)
                await pilot.press("enter")
                await loaded(app, pilot)
            assert bench.calls == [("go_to", 1, "/w/busy"), ("go_to", 2, "/w/idle-2")]

    run(scenario())


def test_enter_on_a_dead_worktree_goes_to_it_and_never_starts():
    async def scenario():
        bench = FakeBench({1: [Session(1, "/w/issue-1")]})
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == [("go_to", 1, "/w/issue-1")]

    run(scenario())


def test_enter_on_a_map_card_targets_the_maps_own_session_never_a_tickets():
    async def scenario():
        bench = FakeBench({1: [live(1, "/w/tm-1", "blocked")], 2: [live(2, "/w/tm-2", "working")]})
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 9)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert bench.calls == []  # its tickets' live sessions take its frontier, and Enter never starts
            assert notifications(app)[-1][0] == "#9 has no session here; s starts /wayfinder on the map, its frontier is empty"
            bench.current = {**bench.current, 9: [live(9, "/w/tm-9", "idle")]}
            await pilot.press("r")
            await loaded(app, pilot)
            await select(app, pilot, 9)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert bench.calls == [("go_to", 9, "/w/tm-9")]

    run(scenario())


def test_cards_show_agent_state_and_enter_goes_to_the_session():
    async def scenario():
        bench = FakeBench({2: [Session(2, "/w/issue-2", workspace="w5", status="blocked")]})
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert "needs you" in card_text(app, 2) and "issue-2" in card_text(app, 2)
            await select(app, pilot, 2)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == [("go_to", 2, "/w/issue-2")]

    run(scenario())


def test_new_herdr_session_shows_up_on_refresh():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            bench.current = {1: [Session(1, "/w/issue-1", workspace="w9", status="working")]}
            await pilot.press("r")
            await loaded(app, pilot)
            assert "working" in card_text(app, 1)

    run(scenario())


REMOVED_KEYS = ("x", "a", "backspace", "delete")  # c, once close, now opens the settings menu


def test_removed_keys_do_nothing_on_any_card(tmp_path):
    settings = tmp_path / "settings.json"

    async def scenario():
        sessions = {n: [Session(n, f"/w/issue-{n}")] for n in (1, 2, 3, 4, 9)}
        bench = FakeBench(sessions)
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60, workbench=bench, settings_path=settings)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            before = notifications(app)
            for number in (9, 1, 2, 3, 4):
                await select(app, pilot, number)
                for key in REMOVED_KEYS:
                    await pilot.press(key)
                    await loaded(app, pilot)
                    assert not is_confirm(app)
            assert bench.calls == [] and notifications(app) == before
            assert not settings.exists() and len(app.query(Card)) == 5

    run(scenario())


def test_removed_keys_are_not_bound_shown_in_the_footer_or_in_help():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            bound = {key.strip() for binding in BoardApp.BINDINGS for key in binding.key.split(",")}
            assert bound.isdisjoint(REMOVED_KEYS)
            footer = " ".join(str(w.render()) for w in app.query("FooterKey"))
            for gone in ("Review", "Remove", "Auto-remove", "Close"):
                assert gone not in footer
            await pilot.press("question_mark")
            await pilot.pause()
            text = app.screen.text.plain
            for gone in ("review-<n>", "auto-remove", "close the issue", "ticketmaster upgrade", "ticketmaster setup",
                         "needs-info", "implement skill", "/setup-matt-pocock-skills"):
                assert gone not in text

    run(scenario())


def test_without_herdr_nothing_starts():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("s")
            await pilot.pause()
            assert not is_confirm(app)

    run(scenario())


def test_up_down_walk_lanes_and_scroll_the_focused_card_into_view():
    async def scenario():
        tickets = [*TICKETS, on(9, 6, "Ready two", order=1), on(9, 7, "Ready three", order=2),
                   a_map(20), on(20, 21)]
        app = BoardApp(FakeProvider(tickets), Query(), interval=60)
        async with app.run_test(size=(160, 12)) as pilot:
            await loaded(app, pilot)
            lanes = app.query_one("#lanes")
            await pilot.press("down", "down")
            assert app.focused.ticket.number == 1
            await pilot.press("down", "down", "down")
            await pilot.pause()
            assert app.focused.ticket.number == 20  # past the lane's last Frontier card: the next map
            assert lanes.max_scroll_y > 0 and lanes.scroll_y > 0  # focused card scrolled into view
            await pilot.press("down")
            assert app.focused.ticket.number == 21
            for _ in range(5):
                await pilot.press("up")
            await pilot.pause()
            assert app.focused.ticket.number == 9 and lanes.scroll_y == 0

    run(scenario())


def visible_columns(app):
    return [head.id.removeprefix("head-") for head in app.query(".head") if head.display]


def visible_cells(app):
    return sorted({cell.column.name for cell in app.query(Cell) if cell.display})


def test_narrow_board_shows_one_column_and_switches_with_arrows_and_1_to_4():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(60, 40)) as pilot:
            await loaded(app, pilot)
            assert app.single and visible_columns(app) == ["FRONTIER"] and visible_cells(app) == ["FRONTIER"]
            assert "2 Frontier 1" in str(app.query_one("#tabs").render())
            await pilot.press("down", "down")
            assert app.focused.ticket.number == 1
            await pilot.press("right")
            assert visible_cells(app) == ["CLAIMED"] and app.focused.ticket.number == 2
            await pilot.press("left", "left")
            assert visible_cells(app) == ["BLOCKED"] and app.focused.ticket.number == 3
            for key, column, number in (("4", "DECIDED", 4), ("1", "BLOCKED", 3), ("3", "CLAIMED", 2),
                                        ("2", "FRONTIER", 1)):
                await pilot.press(key)
                assert visible_columns(app) == [column] and visible_cells(app) == [column]
                assert app.focused.ticket.number == number
            await pilot.press("5")
            assert visible_columns(app) == ["FRONTIER"]  # there is no fifth column
            await pilot.press("v")
            assert visible_columns(app) == ["BLOCKED", "FRONTIER", "CLAIMED", "DECIDED"]
            assert app.focused.ticket.number == 1

    run(scenario())


def test_there_is_no_review_or_done_column():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            heads = " ".join(str(head.render()) for head in app.query(".head"))
            assert heads == "Blocked (1) Frontier (1) Claimed (1) Decided (1)"
            assert not {"5", "6"} & {key for binding in BoardApp.BINDINGS for key in binding.key.split(",")}

    run(scenario())


def test_wide_board_can_be_forced_to_one_column():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60, columns="one")
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert visible_columns(app) == ["FRONTIER"] and visible_cells(app) == ["FRONTIER"]

    run(scenario())


def test_help_lists_every_visible_binding_and_closes_without_quitting():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 50)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            for close in ("escape", "question_mark", "q"):
                await pilot.press("question_mark")
                await pilot.pause()
                assert isinstance(app.screen, HelpScreen)
                text = app.screen.text.plain
                for binding in app.BINDINGS:
                    if binding.show:
                        assert binding.description in text
                        assert app.get_key_display(binding) in text
                assert "herdr agent issue-<n>" in text and "assignee (me, unassigned, anyone)" in text
                assert "The board only reads" in text
                await pilot.press(close)
                await pilot.pause()
                assert not isinstance(app.screen, HelpScreen) and app.is_running

    run(scenario())


def test_help_explains_lanes_and_the_four_column_rules():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            text = " ".join(app.screen.text.plain.split())
            assert "A map lane is an open wayfinder map, headed by its map card (◆)" in text
            assert "A spec lane is a standalone spec, from /to-spec with no map link" in text
            assert "Other issues are not shown" not in text and "Each open wayfinder map is a lane" not in text
            for rule in ("Decided closed, of any age", "Blocked an open blocking issue", "Claimed assigned to someone",
                         "Frontier everything else; the first is the one /wayfinder <map> picks"):
                assert rule in text
            for gone in ("Review", "Done", "In progress", "Ready"):
                assert gone not in text
            assert "is a line of the map card, ▣ #<spec> open or closed" in text
            assert ("s on the lane's header card, while its open spec has no tickets (open or closed), starts "
                    "to_tickets_command (default /mattpocock-skills:to-tickets)") in text

    run(scenario())


def shown_numbers(app):
    """The ticket cards on the board; map headers always show."""
    return sorted(c.ticket.number for c in app.query(Card) if c.column is not None)


async def pick(pilot, app, value, section=None, times=1):
    """Focuses `value` (of `section`, when given) in the open picker and presses Enter on it."""
    option = next(o for o in app.screen.query(PickerOption)
                  if o.value == value and section in (None, o.section))
    option.focus()
    await pilot.pause()
    for _ in range(times):
        await pilot.press("enter")
    await pilot.pause()


def test_pacer_backs_off_to_a_minute_and_sessions_to_ten_seconds():
    pacer = Pacer(5, now=0)
    assert pacer.due(0) == (True, True)
    pacer.started(0, tickets=True)
    pacer.fetched(1, ok=False, rate_limited=True)
    assert pacer.limited and not pacer.stuck and pacer.wait(1) == 60
    assert pacer.due(5) == (False, True)  # sessions were last fetched with the tickets at 0
    pacer.started(5, tickets=False)
    assert pacer.due(14) == (False, False) and pacer.due(15) == (False, True)
    assert pacer.due(61) == (True, True)


def test_pacer_recovers_once_then_stays_slow_until_restart():
    pacer = Pacer(5, now=0)
    pacer.started(0, tickets=True)
    pacer.fetched(1, ok=False, rate_limited=True)
    pacer.started(61, tickets=True)
    pacer.fetched(62, ok=False, rate_limited=True)  # still limited on the minute-poll: not yet recovered
    assert pacer.limited and not pacer.stuck
    pacer.started(122, tickets=True)
    pacer.fetched(123, ok=True, rate_limited=False)
    assert not pacer.limited and pacer.due(128) == (True, True)  # normal interval again
    pacer.started(128, tickets=True)
    pacer.fetched(128, ok=False, rate_limited=True)
    assert pacer.limited and pacer.stuck
    pacer.started(188, tickets=True)
    pacer.fetched(189, ok=True, rate_limited=False)
    assert pacer.limited and pacer.due(197) == (False, False) and pacer.due(198) == (False, True)
    assert pacer.due(248) == (True, True)


def test_pacer_other_errors_keep_the_interval():
    pacer = Pacer(5, now=0)
    pacer.started(0, tickets=True)
    pacer.fetched(1, ok=False, rate_limited=False)
    assert not pacer.limited and pacer.due(5) == (True, True)


def test_a_new_interval_reschedules_from_the_last_fetch():
    pacer = Pacer(60, now=0)
    pacer.started(0, tickets=True)
    pacer.set_interval(5)
    assert pacer.due(4) == (False, False) and pacer.due(5) == (True, True)
    pacer.started(5, tickets=True)
    pacer.set_interval(30)
    assert pacer.due(34) == (False, False) and pacer.due(35) == (True, True)


def test_a_new_interval_keeps_the_rate_limit_back_off():
    pacer = Pacer(5, now=0)
    pacer.started(0, tickets=True)
    pacer.fetched(1, ok=False, rate_limited=True)
    pacer.set_interval(1)
    assert pacer.limited and pacer.wait(1) == 60
    assert pacer.due(9) == (False, False) and pacer.due(10) == (False, True)  # sessions every 10s at least
    pacer.set_interval(20)
    assert pacer.due(19) == (False, False) and pacer.due(20) == (False, True)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class SequenceProvider(FakeProvider):
    """Answers each fetch with the next outcome: tickets, or an exception to raise."""

    def __init__(self, outcomes):
        super().__init__(TICKETS)
        self.outcomes = list(outcomes)

    def fetch(self, query):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else TICKETS
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_rate_limit_backs_off_shows_countdown_and_r_does_not_bypass():
    async def scenario():
        clock, bench = Clock(), FakeBench()
        bench_calls = []
        bench.sessions = lambda: bench_calls.append(clock.now) or {}
        provider = SequenceProvider([TICKETS, RateLimited("gh: API rate limit exceeded"), TICKETS,
                                     RateLimited("gh: You have exceeded a secondary rate limit")])
        app = BoardApp(provider, Query(), interval=5, workbench=bench, clock=clock)

        async def tick(seconds):
            clock.now += seconds
            app.tick()
            await loaded(app, pilot)

        def status():
            return str(app.query_one("#status").render())

        async with app.run_test(size=(200, 30)) as pilot:
            await loaded(app, pilot)
            assert provider.calls == 1 and "every 5s" in status()
            await tick(5)
            assert provider.calls == 2 and "rate-limited · next fetch in 60s" in status() and "⚠" not in status()
            await tick(10)
            await tick(8)
            assert "next fetch in 42s" in status()
            await pilot.press("r")
            await loaded(app, pilot)
            assert provider.calls == 2  # r refreshes sessions only
            assert bench_calls[-3:] == [1005.0, 1015.0, 1023.0]  # every 10s, plus r
            await tick(42)
            assert provider.calls == 3 and "rate-limited" not in status() and "every 5s" in status()
            await tick(5)
            assert provider.calls == 4 and "once a minute until restart · next fetch in 60s" in status()
            await tick(60)
            assert provider.calls == 5 and "once a minute until restart" in status()
            await tick(5)
            assert provider.calls == 5

    run(scenario())


def status_of(app):
    return str(app.query_one("#status").render())


def test_non_rate_limit_error_keeps_the_interval():
    async def scenario():
        clock = Clock()
        provider = SequenceProvider([ProviderError("gh: boom"), TICKETS])
        app = BoardApp(provider, Query(), interval=5, clock=clock)
        async with app.run_test(size=(160, 30)) as pilot:
            await loaded(app, pilot)
            status = str(app.query_one("#status").render())
            assert "⚠ gh: boom" in status and "rate-limited" not in status
            clock.now += 5
            app.tick()
            await loaded(app, pilot)
            assert provider.calls == 2

    run(scenario())


class ExplainingProvider(SequenceProvider):
    """Like GitHub: names the limit a rate limit hit."""

    name = "github"

    def explain(self, error):
        return error.hit("secondary limit", hint="shared")


def tracker_notices(app):
    return [(m, severity) for m, severity in notifications(app) if "the board" in m or " error\n" in m]


def test_a_rate_limit_notifies_its_reason_once_per_episode():
    async def scenario():
        clock = Clock()
        limited = RateLimited("gh: API rate limit already exceeded for user ID 1.")
        provider = ExplainingProvider([TICKETS, limited, limited, TICKETS, limited])
        app = BoardApp(provider, Query(), interval=5, clock=clock)

        async def tick(seconds):
            clock.now += seconds
            app.tick()
            await loaded(app, pilot)

        async with app.run_test(size=(200, 30)) as pilot:
            await loaded(app, pilot)
            await tick(5)
            notice = ("GitHub rate-limited the board: secondary limit (shared)\n"
                      "gh: API rate limit already exceeded for user ID 1.", "warning")
            assert tracker_notices(app) == [notice]
            assert "rate-limited · next fetch in 60s · secondary limit" in status_of(app)
            await tick(60)  # still limited: the same episode
            assert provider.calls == 3 and tracker_notices(app) == [notice]
            await pilot.press("r")
            await loaded(app, pilot)
            assert ("Rate-limited (secondary limit): next ticket fetch in 60s", "warning") in notifications(app)
            await tick(60)  # recovered
            assert provider.calls == 4 and "rate-limited" not in status_of(app)
            await tick(5)  # limited again: a new episode
            assert tracker_notices(app) == [notice, notice]

    run(scenario())


def test_other_tracker_errors_notify_with_status_once_until_they_change():
    async def scenario():
        clock = Clock()
        unauthorized = ProviderError("gh: Bad credentials (HTTP 401)")
        provider = SequenceProvider([unauthorized, unauthorized, ProviderError("glab: 502 Bad Gateway"),
                                     TICKETS, unauthorized])
        app = BoardApp(provider, Query(), interval=5, clock=clock)

        async def tick(seconds):
            clock.now += seconds
            app.tick()
            await loaded(app, pilot)

        async with app.run_test(size=(200, 30)) as pilot:
            await loaded(app, pilot)
            auth = ("fake refused the board: 401 unauthorized\ngh: Bad credentials (HTTP 401)", "error")
            assert tracker_notices(app) == [auth]
            assert "⚠ 401 unauthorized · gh: Bad credentials (HTTP 401)" in status_of(app)
            await tick(5)
            assert tracker_notices(app) == [auth]
            await pilot.press("r")  # repeats the standing error, then fetches (the 502)
            await loaded(app, pilot)
            gateway = ("fake refused the board: 502 bad gateway\nglab: 502 Bad Gateway", "warning")
            assert tracker_notices(app) == [auth, auth, gateway]
            await tick(5)  # a good fetch ends the episode
            await tick(5)
            assert tracker_notices(app) == [auth, auth, gateway, auth]

    run(scenario())


def test_errors_without_a_status_notify_each_time_the_message_changes():
    async def scenario():
        clock = Clock()
        provider = SequenceProvider([ProviderError("gh: not logged in"), ProviderError("`gh` timed out")])
        app = BoardApp(provider, Query(), interval=5, clock=clock)
        async with app.run_test(size=(200, 30)) as pilot:
            await loaded(app, pilot)
            clock.now += 5
            app.tick()
            await loaded(app, pilot)
            assert tracker_notices(app) == [("fake error\ngh: not logged in", "error"),
                                            ("fake error\n`gh` timed out", "error")]

    run(scenario())


def notifications(app):
    return [(n.message, n.severity) for n in app._notifications]


def test_typed_ticket_with_its_map_starts():
    async def scenario():
        bench = FakeBench()
        typed = on(1, 5, "Which store?", labels=("wayfinder::grilling",))
        app = bench_app(bench, [a_map(1), typed])
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 5)
            await pilot.press("s", "y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 5)]

    run(scenario())


GRILL = on(9, 11, "Which store?")
DIG = on(9, 12, "Read the API", "research")
CHORE = on(9, 13, "Sign up", "task", body="## Question\nAFK: the agent can sign up alone.")


def test_wayfinder_cards_show_hitl_or_afk():
    async def scenario():
        app = bench_app(FakeBench(), [GRILL, DIG, CHORE, *TICKETS])
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert "HITL" in card_text(app, 11)
            assert "AFK" in card_text(app, 12) and "HITL" not in card_text(app, 12)
            assert "AFK" in card_text(app, 13)
            assert "HITL" not in card_text(app, 1) and "AFK" not in card_text(app, 1)

    run(scenario())


MAP_CARD = a_map(1, "Pick a stack")


def map_child(number, **kw):
    return on(1, number, **kw)


MAP_BOARD = [MAP_CARD, map_child(5, order=1), map_child(6, order=2), map_child(7, order=0, open_blockers=1),
             map_child(8, is_open=False)]


async def focus_map(app, pilot):
    await loaded(app, pilot)
    await select(app, pilot, 1)


def test_status_line_has_no_setup_drift_or_upgrade_warning():
    async def scenario():
        provider = FakeProvider(TICKETS)
        provider.name = "github"
        app = BoardApp(provider, Query(), interval=60)
        async with app.run_test(size=(240, 40)) as pilot:
            await loaded(app, pilot)
            status = status_of(app)
            assert "⚠" not in status and "setup" not in status and "upgrade" not in status

    run(scenario())


def test_no_code_reads_or_writes_the_issue_tracker_doc():
    import ticketmaster

    root = Path(ticketmaster.__file__).parent
    assert not [p for p in root.rglob("*.py") if "issue-tracker.md" in p.read_text()]


def test_s_on_the_map_starts_its_first_frontier_ticket_on_that_tickets_card():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, MAP_BOARD)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("s")
            await pilot.pause()
            assert is_confirm(app) and app.screen.question.startswith("Start #5 Question 5")
            assert "map #1" in app.screen.question
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 5)]
            card = next(c for c in app.query(Card) if c.ticket.number == 5)
            assert "starting" in str(card.render())
            assert "starting" not in str(next(c for c in app.query(Card) if c.ticket.number == 1).render())

    run(scenario())


def test_two_quick_presses_on_the_map_start_two_different_frontier_tickets():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, MAP_BOARD)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("s", "y")
            await pilot.pause()
            await focus_map(app, pilot)
            await pilot.press("s", "y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 5), ("start", 6)]

    run(scenario())


def test_frontier_skips_tickets_that_already_have_a_live_session():
    async def scenario():
        bench = FakeBench({5: [live(5, "/w/issue-5", "working")]})
        app = bench_app(bench, MAP_BOARD)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("s", "y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 6)]

    run(scenario())


def test_frontier_continues_a_ticket_whose_worktree_has_no_agent():
    async def scenario():
        bench = FakeBench({5: [Session(5, "/w/issue-5")]})
        app = bench_app(bench, MAP_BOARD)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("s")
            await pilot.pause()
            assert app.screen.question == "Continue #5 Question 5 (next on map #1) in its worktree issue-5?"
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 5)]

    run(scenario())


EMPTY_FRONTIER = [MAP_CARD, map_child(5, assignees=("bob",)), map_child(7, open_blockers=1)]


def test_empty_frontier_starts_the_map_itself_and_enter_says_so_first():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, EMPTY_FRONTIER)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == []  # Enter never starts
            assert notifications(app)[-1][0] == \
                "#1 has no session here; s starts /wayfinder on the map, its frontier is empty"
            await pilot.press("s")
            await pilot.pause()
            assert app.screen.question == "Start #1 Pick a stack (map #1's frontier is empty) in a new worktree?"
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 1)]

    run(scenario())


def test_empty_frontier_continues_the_maps_own_agentless_worktree():
    async def scenario():
        bench = FakeBench({1: [Session(1, "/w/issue-1")]})
        app = bench_app(bench, EMPTY_FRONTIER)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("s", "y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 1)]

    run(scenario())


def test_empty_frontier_with_a_live_map_session_says_press_enter():
    async def scenario():
        bench = FakeBench({1: [live(1, "/w/issue-1", "idle")]})
        app = bench_app(bench, EMPTY_FRONTIER)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("s")
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == []
            assert notifications(app)[-1] == ("#1 has a live session: press Enter to go to it", "warning")

    run(scenario())


def test_s_refuses_a_ticket_with_a_live_session_and_names_enter():
    async def scenario():
        bench = FakeBench({1: [Session(1, "/w/old"), live(1, "/w/issue-1", "working")]})
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("s")
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == []
            assert notifications(app)[-1] == ("#1 has a live session: press Enter to go to it", "warning")

    run(scenario())


def test_s_on_my_claimed_ticket_with_only_an_agentless_worktree_continues_it():
    async def scenario():
        bench = FakeBench({2: [Session(2, "/w/issue-2")]})
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 2)
            await pilot.press("s")
            await pilot.pause()
            assert app.screen.question == "Continue #2 Doing in its worktree issue-2?"
            assert "claude --continue" in app.screen.detail
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 2)]

    run(scenario())


class FailingBench(FakeBench):
    def start(self, ticket):
        super().start(ticket)
        raise HerdrError("agent_start_failed")


def test_a_failed_start_frees_the_ticket_for_another_s():
    async def scenario():
        bench = FailingBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("s", "y")
            await loaded(app, pilot)
            assert notifications(app)[-1] == ("herdr: agent_start_failed", "error")
            await pilot.press("s")
            await pilot.pause()
            assert is_confirm(app)

    run(scenario())


def test_s_stays_refused_while_the_new_worktree_waits_for_its_agent():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("s", "y")
            await loaded(app, pilot)
            bench.current = {1: [Session(1, "/w/issue-1")]}  # herdr lists the worktree before Claude is ready
            await pilot.press("r")
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("s")
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == [("start", 1)]
            assert notifications(app)[-1][0] == "#1 has a live session: press Enter to go to it"
            bench.current = {1: [live(1, "/w/issue-1", "idle")]}
            await pilot.press("r")
            await loaded(app, pilot)
            assert 1 not in app.starting

    run(scenario())


def test_s_offers_to_continue_only_the_worktree_named_by_the_key():
    async def scenario():
        bench = FakeBench({1: [Session(1, "/w/issue-1-2"), Session(1, "/w/issue-1")]})
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("s")
            await pilot.pause()
            assert app.screen.question == "Continue #1 Ready one in its worktree issue-1?"
            bench.current = {1: [Session(1, "/w/issue-1-2")]}  # a copy alone: herdr makes the key's worktree
            await pilot.press("n", "r")
            await loaded(app, pilot)
            await select(app, pilot, 1)
            await pilot.press("s")
            await pilot.pause()
            assert app.screen.question == "Start #1 Ready one in a new worktree?"

    run(scenario())


def test_s_starts_decision_tickets_of_all_four_types():
    async def scenario():
        bench = FakeBench()
        kinds = ["research", "prototype", "grilling", "task"]
        app = bench_app(bench, [MAP, *(on(9, 20 + i, kind=kind) for i, kind in enumerate(kinds))])
        async with app.run_test(size=(160, 60)) as pilot:
            await loaded(app, pilot)
            for i in range(len(kinds)):
                await select(app, pilot, 20 + i)
                await pilot.press("s", "y")
                await loaded(app, pilot)
            assert bench.calls == [("start", 20), ("start", 21), ("start", 22), ("start", 23)]

    run(scenario())


def test_enter_on_a_map_with_a_frontier_names_the_ticket_s_would_start():
    async def scenario():
        app = bench_app(FakeBench(), MAP_BOARD)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert notifications(app)[-1][0] == "#1 has no session here; s starts #5 Question 5, next on the map"

    run(scenario())


class OrderedProvider(FakeProvider):
    """A tracker that knows the map's own order (GitHub sub-issue order)."""

    def __init__(self, tickets, order):
        super().__init__(tickets)
        self.order = order
        self.asked = []

    def map_order(self, number):
        self.asked.append(number)
        if isinstance(self.order, Exception):
            raise self.order
        return self.order


def test_s_on_the_map_follows_the_trackers_map_order():
    async def scenario():
        bench = FakeBench()
        provider = OrderedProvider(MAP_BOARD, [6, 7, 5])
        app = BoardApp(provider, Query(), interval=60, workbench=bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("s")
            await loaded(app, pilot)
            assert provider.asked == [1] and app.screen.question.startswith("Start #6 ")
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 6)]

    run(scenario())


def test_map_order_lookup_failure_falls_back_to_board_order():
    async def scenario():
        bench = FakeBench()
        app = BoardApp(OrderedProvider(MAP_BOARD, ProviderError("boom")), Query(), interval=60, workbench=bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("s")
            await loaded(app, pilot)
            assert app.screen.question.startswith("Start #5 ")

    run(scenario())


def blocked_card(**fields) -> str:
    return render_card(Ticket(number=5, title="Waits", url="u5", is_open=True, **fields), Column.BLOCKED).plain


def test_blocked_card_lists_each_open_blocker_in_order():
    assert "⛔ #12 " in blocked_card(blocked_by=("#12",), open_blockers=1)
    many = tuple(f"#{n}" for n in range(20, 0, -1))
    assert f"⛔ {' '.join(many)} " in blocked_card(blocked_by=many, open_blockers=len(many))


def test_blocked_card_names_blockers_in_another_repo():
    assert "⛔ #3 other/lib#12 " in blocked_card(blocked_by=("#3", "other/lib#12"), open_blockers=2)


def test_blocked_card_without_refs_keeps_the_count_and_a_label_only_card_shows_no_chip():
    assert "⛔ 2 " in blocked_card(open_blockers=2)
    assert "⛔" not in blocked_card(labels=("blocked",))
    assert "⛔" not in blocked_card(labels=("blocked",), open_blockers=0)


def spec_of(map_number, number, title="", **kw):
    """A spec of map #<map_number>: `Part of #<map>`, no `wayfinder:` label, and the map as its parent."""
    map_url = f"https://gh/o/r/issues/{map_number}"
    base = dict(number=number, title=title or f"Spec {number}", url=f"u{number}", is_open=True,
                body=f"Part of #{map_number}\n\n## Problem Statement", labels=("ready-for-agent",),
                parent_number=map_number, parent_url=map_url, parent_labels=("wayfinder:map",),
                map_number=map_number, map_url=map_url)
    return Ticket(**{**base, **kw})


def impl_of(spec, number, title="", **kw):
    """An implementation ticket of `spec`, linked by its `## Parent` section."""
    base = dict(number=number, title=title or f"Slice {number}", url=f"u{number}", is_open=True,
                body=f"## Parent\n\n#{spec.number}\n\n## What to build", map_number=spec.map_number,
                map_url=spec.map_url)
    return Ticket(**{**base, **kw})


def chips_of(app, number):
    return card_text(app, number).split("\n")[-1]


def test_a_maps_spec_and_its_implementation_tickets_sit_in_its_lane_tagged_and_classified():
    closed = dict(is_open=False, closed_at=datetime.now(timezone.utc))
    spec = spec_of(9, 50, assignees=("me",))
    other_spec = spec_of(20, 60)
    tickets = [*TICKETS, a_map(20, "Ship billing"), spec, other_spec,
               impl_of(spec, 51, open_blockers=1, blocked_by=("#52",)), impl_of(spec, 52), impl_of(spec, 53, **closed),
               impl_of(spec, 54, assignees=("bob",)), impl_of(other_spec, 61),
               Ticket(number=70, title="Stray", url="u70", is_open=True, body="## Parent\n\n#50\n")]  # no map found

    async def scenario():
        app = BoardApp(FakeProvider(tickets), Query(), interval=60)
        async with app.run_test(size=(200, 70)) as pilot:
            await loaded(app, pilot)
            assert shown_lanes(app) == {9: {"BLOCKED": [3, 51], "FRONTIER": [1, 52], "CLAIMED": [2, 54],
                                            "DECIDED": [4, 53]},
                                        20: {"FRONTIER": [61]}}  # the specs are no cards
            assert chips_of(app, 52).startswith("impl ") and "spec" not in chips_of(app, 1)
            assert "◆ Pick a stack" in chips_of(app, 51) and "⛔ #52" in chips_of(app, 51)
            assert "1 decided · 3 open · 1 blocked" in card_text(app, 9)  # the counts are the map's decisions
            assert 70 not in [c.ticket.number for c in app.query(Card)]

    run(scenario())


def test_an_open_closing_pr_shows_as_a_badge_and_none_without_one():
    spec = spec_of(9, 50)
    tickets = [*TICKETS, spec, impl_of(spec, 51, has_open_change_request=True), impl_of(spec, 52),
               on(9, 5, has_open_change_request=True),
               impl_of(spec, 53, has_open_change_request=True, is_open=False)]  # decided: no badge

    async def scenario():
        app = BoardApp(FakeProvider(tickets), Query(), interval=60)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            assert "PR open" in chips_of(app, 51) and "PR open" in chips_of(app, 5)
            assert not any("PR open" in card_text(app, n) for n in (1, 2, 52, 53, 9))

    run(scenario())


SPEC = spec_of(9, 50, assignees=("me",), has_tickets=True)
SLICES = [impl_of(SPEC, 51), impl_of(SPEC, 52, is_open=False, closed_at=datetime.now(timezone.utc)),
          impl_of(SPEC, 53, open_blockers=1, blocked_by=("#51",)), impl_of(SPEC, 54, assignees=("bob",)),
          impl_of(SPEC, 55), impl_of(SPEC, 56, has_open_change_request=True), impl_of(SPEC, 57, assignees=("me",))]
LIVE = {55: [Session(55, "/w/issue-55", "issue-55", "wA", "working", "wA:p1")]}


def implement_app(bench):
    return BoardApp(FakeProvider([MAP, SPEC, *SLICES]), Query(), interval=60, workbench=bench,
                    describe_start=build_prompt)


def test_s_on_an_open_unblocked_unclaimed_impl_card_starts_implement_unfocused():
    async def scenario():
        bench = FakeBench()
        app = implement_app(bench)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 51)
            await pilot.press("s")
            await pilot.pause()
            assert is_confirm(app)
            assert app.screen.question == "Start /implement on #51 Slice 51 in a new worktree?"
            assert app.screen.detail == "/mattpocock-skills:implement u51\nunfocused (AFK)"
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 51)]
            assert notifications(app)[-1][0] == "Started #51"  # the workbench's own report

    run(scenario())


def test_s_on_an_impl_card_refuses_and_leaves_the_workbench_untouched():
    async def scenario():
        bench = FakeBench(LIVE)
        app = implement_app(bench)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            for number in (52, 53, 54, 55, 56):
                await select(app, pilot, number)
                await pilot.press("s")
                await pilot.pause()
                assert not is_confirm(app)
            assert bench.calls == []
            assert [m for m, _ in notifications(app)][-5:] == [
                "#52 is closed", "#53 is blocked by #51", "#54 is claimed by @bob",
                "#55 has a live session: press Enter to go to it", "#56 has an open PR that closes it"]

    run(scenario())


def test_s_on_my_claimed_impl_card_starts_while_its_spec_is_open_and_reuses_an_agentless_worktree():
    async def scenario():
        bench = FakeBench({57: [Session(57, "/w/issue-57", "issue-57")]})
        app = implement_app(bench)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 57)
            await pilot.press("s")
            await pilot.pause()
            assert app.screen.question == "Start /implement on #57 Slice 57 in its worktree with claude --continue?"
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 57)]

    run(scenario())


def test_a_maps_spec_is_a_line_of_its_map_card_and_no_card_in_any_column():
    bare, spec = spec_of(9, 80), spec_of(20, 60, has_tickets=True)
    closed = dict(is_open=False, closed_at=datetime.now(timezone.utc))
    tickets = [*TICKETS, bare, a_map(20, "Ship billing"), spec, impl_of(spec, 61), impl_of(spec, 62, **closed),
               impl_of(spec, 63, not_planned=True, **closed), impl_of(spec, 64, open_blockers=1, blocked_by=("#61",))]

    async def scenario():
        app = BoardApp(FakeProvider(tickets), Query(), interval=60)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            assert not {60, 80} & set(shown_numbers(app))
            assert "▣ #80 open  no tickets yet" in card_text(app, 9)
            assert "▣ #60 open  1 done · 2 open · 1 blocked" in card_text(app, 20)  # done less canceled
            assert "1 decided · 3 open · 1 blocked" in card_text(app, 9)  # the map's own counts stay its decisions

    run(scenario())


def spec_app(bench, spec=None, describe_start=build_prompt, map_ticket=MAP):
    tickets = [map_ticket, *TICKETS[1:], spec or spec_of(9, 80)]
    return BoardApp(FakeProvider(tickets), Query(), interval=60, workbench=bench, describe_start=describe_start)


def test_s_on_the_map_card_of_a_ticketless_spec_starts_to_tickets_on_the_spec():
    async def scenario(map_ticket):
        bench = FakeBench()
        app = spec_app(bench, map_ticket=map_ticket)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 9)
            await pilot.press("s")
            await pilot.pause()
            assert app.screen.question == "Start /to-tickets on #80 Spec 80 in a new worktree?"
            assert app.screen.detail == "/mattpocock-skills:to-tickets u80"  # focused: no AFK note
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 80)]  # the spec, not the map's frontier ticket #1

    run(scenario(MAP))
    run(scenario(a_map(9, "Pick a stack", is_open=False)))  # a closed map whose spec is open


def test_s_on_a_map_card_takes_to_tickets_command_from_ticketmaster_toml(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".ticketmaster.toml").write_text('to_tickets_command = "/to-tickets"\n')
    workbench = workbench_for(tmp_path, load_config(tmp_path))

    async def scenario():
        bench = FakeBench()
        app = spec_app(bench, describe_start=workbench.prompt)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 9)
            await pilot.press("enter")
            await pilot.pause()
            assert notifications(app)[-1][0] == "#9 has no session here; s starts /to-tickets u80 on its spec #80"
            await pilot.press("s")
            await pilot.pause()
            assert app.screen.detail == "/to-tickets u80"

    run(scenario())


def test_s_on_a_map_card_whose_spec_has_tickets_is_closed_or_is_missing_starts_the_frontier_ticket():
    with_open = spec_of(9, 80, has_tickets=True)
    with_closed = spec_of(9, 80, has_tickets=True)
    cases = [(with_open, [impl_of(with_open, 81)]), (with_closed, [impl_of(with_closed, 81, is_open=False)]),
             (spec_of(9, 80, is_open=False), []), (None, [])]  # a spec closed without tickets is passed over

    async def scenario(spec, impls):
        bench = FakeBench()
        tickets = [*TICKETS, *([spec] if spec else []), *impls]
        app = BoardApp(FakeProvider(tickets), Query(), interval=60, workbench=bench, describe_start=build_prompt)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 9)
            await pilot.press("s")
            await pilot.pause()
            assert app.screen.question == "Start #1 Ready one (next on map #9) in a new worktree?"
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 1)]

    for spec, impls in cases:
        run(scenario(spec, impls))


def test_s_on_a_map_card_refuses_a_blocked_or_someone_elses_spec_or_one_with_a_live_session():
    live = {80: [Session(80, "/w/issue-80", "issue-80", "wA", "working", "wA:p1")]}

    async def scenario(spec, sessions, refusal):
        bench = FakeBench(sessions)
        app = spec_app(bench, spec)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            await select(app, pilot, 9)
            await pilot.press("s")
            await pilot.pause()
            assert not is_confirm(app) and bench.calls == []
            assert notifications(app)[-1][0] == refusal

    run(scenario(spec_of(9, 80, open_blockers=1, blocked_by=("#3",)), {}, "#80 is blocked by #3"))
    run(scenario(spec_of(9, 80, assignees=("bob",)), {}, "#80 is claimed by @bob"))
    run(scenario(spec_of(9, 80), live, "#80 has a live session: press Enter to go to it"))


def test_the_map_card_badges_its_specs_session_and_enter_goes_there_when_the_map_has_none():
    async def scenario():
        bench = FakeBench({80: [Session(80, "/w/issue-80", "issue-80", "wA", "working", "wA:p1")]})
        app = spec_app(bench)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            assert "▣ #80 open  no tickets yet\n● working issue-80" in card_text(app, 9)
            await select(app, pilot, 9)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert bench.calls == [("go_to", 9, "/w/issue-80")]

    run(scenario())


def test_the_spec_type_filter_keeps_a_lane_whose_map_card_carries_a_spec(tmp_path):
    async def scenario():
        app = filters_app(tmp_path)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            await pick(pilot, app, "spec")
            assert headers(app) == [20] and shown_numbers(app) == []
            assert "▣ #30 open" in card_text(app, 20)

    run(scenario())


def test_enter_on_an_impl_card_without_a_worktree_names_the_implement_start_or_the_refusal():
    async def scenario():
        bench = FakeBench()
        app = implement_app(bench)
        async with app.run_test(size=(200, 60)) as pilot:
            await loaded(app, pilot)
            for number in (51, 54):
                await select(app, pilot, number)
                await pilot.press("enter")
                await pilot.pause()
            assert bench.calls == []
            assert [m for m, _ in notifications(app)][-2:] == [
                "#51 has no session here; s starts /mattpocock-skills:implement u51",
                "#54 has no session here; s refuses: #54 is claimed by @bob"]

    run(scenario())


def test_a_linear_maps_lane_shows_keys_display_names_and_key_blockers():
    """The Linear provider over a fake Linear: issue keys, `@displayName` assignees, `TM-<n>` blockers."""
    from fake_linear import FakeLinear
    from ticketmaster.providers.linear import LinearProvider

    fake = FakeLinear()
    the_map = fake.add_issue("Read-only ticketmaster", labels=["wayfinder:map"])  # TM-1
    survey = fake.add_issue("Survey", parent=the_map, labels=["wayfinder:research"], assignee="kaare.blakstad",
                            state="In Progress")  # TM-2
    decide = fake.add_issue("Decide", parent=the_map, labels=["wayfinder:grilling"])  # TM-3
    fake.block(survey, decide)
    spec = fake.add_issue("Spec", "Part of [TM-1](https://linear.app/fabricskill/issue/TM-1/slug)")  # TM-4
    fake.add_issue("Slice", "## Parent\n\nTM-4\n\nBlocked by: TM-3", parent=spec)  # TM-5

    async def scenario():
        app = BoardApp(LinearProvider("TM", transport=fake), Query(), interval=30)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert shown_lanes(app) == {1: {"BLOCKED": [3, 5], "CLAIMED": [2]}}
            assert "▣ TM-4 open  0 done · 1 open · 1 blocked" in card_text(app, 1)
            assert "TM-1 Read-only ticketmaster" in card_text(app, 1) and "#1" not in card_text(app, 1)
            assert "TM-2 Survey" in card_text(app, 2) and "@kaare.blakstad" in card_text(app, 2)
            assert "TM-3 Decide" in card_text(app, 3) and "⛔ TM-2" in card_text(app, 3)
            assert chips_of(app, 5).startswith("impl ") and "⛔ TM-3" in chips_of(app, 5)
            assert "linear:TM" in app.sub_title and "every 30s" in status_of(app)

    run(scenario())


FILTER_SPEC = spec_of(20, 30, author="dana")
FILTERED = [
    MAP,
    on(9, 1, "Which store?", author="alice"),  # grilling · HITL, unassigned
    on(9, 2, "Read the API", "research", author="bob", assignees=("me",)),  # AFK, mine
    on(9, 3, "Sign up", "task", body="HITL: needs a card", author="bob", assignees=("carol",)),  # HITL, theirs
    a_map(20, "Ship billing"),
    on(20, 21, "Try a mock", "prototype", author="carol"),  # HITL
    FILTER_SPEC,
    impl_of(FILTER_SPEC, 31, author="dana", assignees=("ME",)),
]


def filters_app(tmp_path, query=Query(), tickets=FILTERED):
    return BoardApp(FakeProvider(tickets), query, interval=60, settings_path=tmp_path / "settings.json")


def picker_values(app, section):
    return [o.value for o in app.screen.query(PickerOption) if o.section == section]


def test_passes_filters_ors_within_a_section_ands_across_and_ignores_values_not_on_the_board():
    known = {"Type": {"grilling", "research", "task", "prototype", "spec", "impl"}, "HITL/AFK": {"HITL", "AFK"},
             "Author": {"alice", "bob", "carol", "dana"}}
    cards = [t for t in FILTERED if not t.is_wayfinder_map]
    shown = lambda filters, viewer="me": [t.number for t in cards if passes_filters(t, filters, known, viewer)]
    chosen = lambda *values: dict.fromkeys(values, "include")
    assert shown({}) == [1, 2, 3, 21, 30, 31]
    assert shown({"Type": chosen("research", "impl")}) == [2, 31]
    assert shown({"HITL/AFK": chosen("HITL")}) == [1, 3, 21]
    assert shown({"HITL/AFK": chosen("HITL"), "Author": chosen("bob")}) == [3]
    assert shown({"Assignee": chosen("me")}) == [2, 31]  # the login, in any case
    assert shown({"Assignee": chosen("me")}, viewer=None) == []  # an unknown login is nobody
    assert shown({"Assignee": chosen("unassigned")}) == [1, 21, 30]
    assert shown({"Author": chosen("gone"), "Type": chosen("spec")}) == [30]  # a saved author no longer on the board


def test_f_picker_offers_values_from_the_board_and_each_filter_narrows_it(tmp_path):
    async def scenario():
        app = filters_app(tmp_path)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            assert isinstance(app.screen, FilterPicker)
            assert picker_values(app, "Type") == ["research", "prototype", "grilling", "task", "spec", "impl"]
            assert picker_values(app, "HITL/AFK") == ["HITL", "AFK"]
            assert picker_values(app, "Assignee") == ["me", "unassigned", "anyone"]
            assert picker_values(app, "Map") == ["9", "20"]
            assert picker_values(app, "Author") == ["alice", "bob", "carol", "dana"]
            assert not picker_values(app, "Labels")  # the old label filter is gone

            await pick(pilot, app, "task")
            await pick(pilot, app, "prototype")
            assert shown_numbers(app) == [3, 21] and headers(app) == [9, 20]
            await pick(pilot, app, "task")  # off again
            assert shown_numbers(app) == [21] and headers(app) == [20]  # a lane with no match is hidden
            assert "type: prototype" in status_of(app)
            await pilot.press("c")
            await pilot.pause()

            await pick(pilot, app, "AFK")
            assert shown_numbers(app) == [2] and headers(app) == [9]
            await pilot.press("c")
            await pilot.pause()

            await pick(pilot, app, "me")
            assert shown_numbers(app) == [2, 31] and "assignee: me" in status_of(app)
            await pick(pilot, app, "unassigned")  # one choice at a time
            assert shown_numbers(app) == [1, 21] and app.filters["Assignee"] == {"unassigned": "include"}
            await pick(pilot, app, "anyone")
            assert shown_numbers(app) == [1, 2, 3, 21, 31] and app.filters["Assignee"] == {}

            await pick(pilot, app, "20")
            assert headers(app) == [20] and shown_numbers(app) == [21, 31] and "map: #20" in status_of(app)
            await pick(pilot, app, "20")

            await pick(pilot, app, "bob")
            await pick(pilot, app, "dana")
            assert shown_numbers(app) == [2, 3, 31] and "author: bob, dana" in status_of(app)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, FilterPicker)
            assert "Blocked (0)" in str(app.query_one("#head-BLOCKED").render())
            assert "Frontier (0)" in str(app.query_one("#head-FRONTIER").render())  # #30, the spec, is no card
            assert "Claimed (3)" in str(app.query_one("#head-CLAIMED").render())

    run(scenario())


def test_lane_headers_keep_the_maps_own_counts_under_a_filter(tmp_path):
    async def scenario():
        app = filters_app(tmp_path)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            before = card_text(app, 9)
            await pilot.press("f")
            await pilot.pause()
            await pick(pilot, app, "alice")
            assert shown_numbers(app) == [1] and card_text(app, 9) == before
            assert "0 decided · 3 open · 0 blocked" in before

    run(scenario())


def test_text_filter_combines_with_the_picker_and_esc_clears_every_filter(tmp_path):
    async def scenario():
        app = filters_app(tmp_path)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            await pick(pilot, app, "HITL")
            await pick(pilot, app, "9")
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("slash", *"sign", "enter")
            await pilot.pause()
            assert shown_numbers(app) == [3] and "filter: sign" in status_of(app)
            await pilot.press("escape")
            await pilot.pause()
            assert shown_numbers(app) == [1, 2, 3, 21, 31] and headers(app) == [9, 20]
            assert not any(app.filters.values()) and app.needle == ""
            for gone in ("filter:", "hitl/afk:", "map:"):
                assert gone not in status_of(app)

    run(scenario())


def test_a_filter_that_matches_nothing_says_so(tmp_path):
    async def scenario():
        app = filters_app(tmp_path)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("slash", *"nothing here", "enter")
            await pilot.pause()
            assert headers(app) == [] and "No cards match the filters" in str(app.query_one("#empty").render())

    run(scenario())


def test_filters_are_saved_per_repo_and_restored_on_the_next_open(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"auto_remove": True, "filters": {"other:x/y": {"authors": ["alice"]}}}))

    async def choose():
        app = BoardApp(FakeProvider(FILTERED), Query(), interval=60, settings_path=settings)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            for value in ("grilling", "task", "HITL", "unassigned", "9", "alice"):
                await pick(pilot, app, value)

    async def reopen():
        app = BoardApp(FakeProvider(FILTERED), Query(), interval=60, settings_path=settings)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            assert shown_numbers(app) == [1] and headers(app) == [9]
            assert "type: grilling, task" in status_of(app) and "assignee: unassigned" in status_of(app)

    run(choose())
    assert json.loads(settings.read_text()) == {
        "auto_remove": True,
        "filters": {
            "other:x/y": {"authors": ["alice"]},
            "fake:o/r": {"types": ["grilling", "task"], "modes": ["HITL"], "assignee": ["unassigned"], "maps": [9],
                         "authors": ["alice"]},
        },
    }
    run(reopen())


def test_clicking_a_picker_option_selects_that_option(tmp_path):
    async def scenario():
        app = filters_app(tmp_path)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            option = next(o for o in app.screen.query(PickerOption) if o.value == "carol")
            await pilot.click(option)
            await pilot.pause()
            assert app.filters["Author"] == {"carol": "include"} and shown_numbers(app) == [21]

    run(scenario())


CLOSED_MAP = [*FILTERED, a_map(40, "Old effort", is_open=False), on(40, 41, is_open=False)]


def test_a_named_map_presets_the_map_filter_and_clearing_it_shows_every_open_map(tmp_path):
    async def scenario(named, expect):
        app = filters_app(tmp_path, Query(named_map=named), CLOSED_MAP)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            assert headers(app) == [named] and shown_numbers(app) == expect
            assert f"map: #{named}" in status_of(app)
            await pilot.press("escape")
            await pilot.pause()
            assert headers(app) == ([9, 20, 40] if named == 40 else [9, 20])  # a named closed map stays
            assert "map:" not in status_of(app)

    run(scenario(20, [21, 31]))
    run(scenario(40, []))  # closed: its header card alone


def test_a_named_closed_map_stays_under_a_card_filter(tmp_path):
    async def scenario():
        app = filters_app(tmp_path, Query(named_map=40), CLOSED_MAP)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("slash", *"sign", "enter")
            await pilot.pause()
            assert headers(app) == [40]

    run(scenario())


def test_saved_values_that_cannot_be_filters_are_dropped(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"filters": {"fake:o/r": {
        "types": 3, "assignee": "me", "maps": ["abc", 9], "authors": ["bob"]}}}))

    async def scenario():
        app = BoardApp(FakeProvider(FILTERED), Query(), interval=60, settings_path=settings)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            assert app.filters["Type"] == {} and app.filters["Assignee"] == {} and app.filters["Map"] == {"9": "include"}
            assert shown_numbers(app) == [2, 3]
            await pilot.press("f")
            await pilot.pause()
            await pick(pilot, app, "me")  # saving no longer trips over the bad values

    run(scenario())
    assert json.loads(settings.read_text())["filters"]["fake:o/r"]["maps"] == [9]


KEYED = [*FILTERED[:4], a_map(20, "Ship billing", key="TM-7"), *FILTERED[5:]]  # map #20 has the tracker key TM-7


def keyed_app(tmp_path, value, tickets=KEYED, settings=None):
    """The board as `ticketmaster <value>` opens it."""
    from ticketmaster.cli import parse_args

    named = parse_args([value]).map
    query = Query(named_map=named if isinstance(named, int) else None)
    return BoardApp(FakeProvider(tickets), query, interval=60, settings_path=settings or tmp_path / "settings.json",
                    map_key=named if isinstance(named, str) else None)


def test_each_accepted_map_form_presets_the_map_filter(tmp_path):
    async def scenario(value):
        app = keyed_app(tmp_path, value)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            assert headers(app) == [20], value
            await pilot.press("escape")
            await pilot.pause()
            assert headers(app) == [9, 20], value

    for value in ("#20", "20", "tm-7", "https://linear.app/t/issue/TM-7/ship", "https://github.com/o/r/issues/20"):
        run(scenario(value))


def test_a_key_matches_the_maps_tracker_key_never_its_number(tmp_path):
    async def scenario():
        app = keyed_app(tmp_path, "TM-20")  # #20's key is TM-7: TM-20 names no map here
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            assert headers(app) == [] and "map: TM-20" in status_of(app)
            assert "No cards match the filters" in str(app.query_one("#empty").render())

    run(scenario())


def test_a_named_map_is_never_saved(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"filters": {"fake:o/r": {"maps": [9]}}}))

    async def named():
        app = keyed_app(tmp_path, "20", settings=settings)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            assert headers(app) == [20]
            await pilot.press("f")
            await pilot.pause()
            await pick(pilot, app, "carol")

    async def plain():
        app = BoardApp(FakeProvider(KEYED), Query(), interval=60, settings_path=settings)
        async with app.run_test(size=(200, 50)) as pilot:
            await loaded(app, pilot)
            assert headers(app) == [] and app.filters["Map"] == {"9": "include"}  # carol has no card on #9
            assert app.filters["Author"] == {"carol": "include"}

    run(named())
    assert json.loads(settings.read_text())["filters"]["fake:o/r"]["maps"] == [9]
    run(plain())


def settings_app(settings, interval=None, clock=None):
    return BoardApp(FakeProvider(TICKETS), Query(), interval=interval, settings_path=settings, clock=clock or Clock())


async def open_settings(pilot, app):
    await pilot.press("c")
    await pilot.pause()
    assert isinstance(app.screen, SettingsMenu)


async def enter_interval(pilot, app, text):
    """Opens the settings menu, replaces the interval with `text` and presses Enter."""
    await open_settings(pilot, app)
    field = app.screen.query_one("#interval", Input)
    assert app.screen.focused is field
    field.value = text
    await pilot.press("enter")
    await pilot.pause()


def test_a_new_interval_applies_at_once_and_is_saved_for_the_repo(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"filters": {"fake:o/r": {"authors": ["alice"]}}, "repos": {"other:x/y": {"interval": 9}}}))

    async def scenario():
        clock = Clock()
        app = settings_app(settings, clock=clock)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert app.provider.calls == 1 and "every 5s" in status_of(app)
            await open_settings(pilot, app)
            assert app.screen.query_one("#interval", Input).value == "5"
            await pilot.press("escape")
            await enter_interval(pilot, app, "30")
            assert not isinstance(app.screen, SettingsMenu) and "every 30s" in status_of(app) and app.needle == ""
            clock.now += 29
            app.tick()
            await loaded(app, pilot)
            assert app.provider.calls == 1
            clock.now += 1
            app.tick()
            await loaded(app, pilot)
            assert app.provider.calls == 2
            await enter_interval(pilot, app, "0.5")  # no floor
            assert "every 0.5s" in status_of(app)
            clock.now += 0.5
            app.tick()
            await loaded(app, pilot)
            assert app.provider.calls == 3

    run(scenario())
    assert json.loads(settings.read_text()) == {
        "filters": {"fake:o/r": {"authors": ["alice"]}},
        "repos": {"other:x/y": {"interval": 9}, "fake:o/r": {"interval": 0.5}},
    }


def test_escape_closes_the_settings_menu_without_changing_anything(tmp_path):
    settings = tmp_path / "settings.json"

    async def scenario():
        app = settings_app(settings)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            await open_settings(pilot, app)
            app.screen.query_one("#interval", Input).value = "99"
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SettingsMenu) and app.is_running
            assert app.interval == 5 and app.pacer.interval == 5 and "every 5s" in status_of(app)

    run(scenario())
    assert not settings.exists()


def test_the_settings_menu_rejects_an_interval_that_is_no_positive_number(tmp_path):
    settings = tmp_path / "settings.json"

    async def scenario():
        app = settings_app(settings)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            await open_settings(pilot, app)
            field = app.screen.query_one("#interval", Input)
            for text, says in (("abc", "not a number"), ("0", "positive"), ("-2", "positive")):
                field.value = text
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SettingsMenu)
                assert says in str(app.screen.query_one("#settings-error").render())
            assert app.interval == 5 and "every 5s" in status_of(app)

    run(scenario())
    assert not settings.exists()


def test_the_saved_interval_is_restored_and_interval_overrides_it_without_overwriting(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"repos": {"fake:o/r": {"interval": 42}}}))

    async def scenario(flag, shown):
        app = settings_app(settings, interval=flag)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert app.pacer.interval == shown and f"every {shown}s" in status_of(app)
            await open_settings(pilot, app)
            assert app.screen.query_one("#interval", Input).value == str(shown)

    run(scenario(None, 42))
    run(scenario(7, 7))
    assert json.loads(settings.read_text()) == {"repos": {"fake:o/r": {"interval": 42}}}


def test_an_interval_edited_under_the_flag_is_saved(tmp_path):
    settings = tmp_path / "settings.json"

    async def scenario():
        app = settings_app(settings, interval=7)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            await enter_interval(pilot, app, "12")
            assert "every 12s" in status_of(app)

    run(scenario())
    assert json.loads(settings.read_text()) == {"repos": {"fake:o/r": {"interval": 12}}}


def test_the_flags_interval_confirmed_in_the_menu_replaces_the_saved_one(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"repos": {"fake:o/r": {"interval": 30}}}))

    async def scenario():
        app = settings_app(settings, interval=10)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            await enter_interval(pilot, app, "10")

    run(scenario())
    assert json.loads(settings.read_text()) == {"repos": {"fake:o/r": {"interval": 10}}}


def test_an_interval_shows_exactly_and_survives_the_menu_unedited(tmp_path):
    settings = tmp_path / "settings.json"

    async def scenario():
        app = settings_app(settings)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            await enter_interval(pilot, app, "1234567")
            assert "every 1234567s" in status_of(app)
            await open_settings(pilot, app)
            assert app.screen.query_one("#interval", Input).value == "1234567"
            await pilot.press("enter")
            await pilot.pause()
            assert app.interval == 1234567

    run(scenario())
    assert json.loads(settings.read_text()) == {"repos": {"fake:o/r": {"interval": 1234567}}}


def test_a_failed_settings_save_warns_and_still_applies(tmp_path):
    (tmp_path / "blocker").write_text("")  # a file where the settings directory should be

    async def scenario():
        app = settings_app(tmp_path / "blocker" / "settings.json")
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            await enter_interval(pilot, app, "9")
            assert "every 9s" in status_of(app)
            assert any(m.startswith("settings not saved") and s == "warning" for m, s in notifications(app))

    run(scenario())


def test_help_describes_the_settings_menu_and_the_interval_precedence():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            text = " ".join(app.screen.text.plain.split())
            assert "Settings" in text and "saved per repo" in text
            assert "--interval for this run, then the repo's saved interval, then the default" in text
            assert "Fetch limit" in text and "cut off at" in text

    run(scenario())


async def enter_fetch_limit(pilot, app, text):
    """Opens the settings menu, replaces the fetch limit with `text` and presses Enter."""
    await open_settings(pilot, app)
    app.screen.query_one("#fetch_limit", Input).value = text
    await pilot.press("enter")
    await pilot.pause()


def test_a_new_fetch_limit_applies_from_the_next_fetch_and_is_saved_for_the_repo(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"repos": {"fake:o/r": {"interval": 5}}}))

    async def scenario():
        app = settings_app(settings)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert app.provider.queries[-1].limit == 300
            await open_settings(pilot, app)
            assert app.screen.query_one("#fetch_limit", Input).value == "300"
            await pilot.press("escape")
            await enter_fetch_limit(pilot, app, " 50 ")
            assert not isinstance(app.screen, SettingsMenu) and app.provider.calls == 1  # no fetch of its own
            await pilot.press("r")
            await loaded(app, pilot)
            assert app.provider.queries[-1].limit == 50

    run(scenario())
    assert json.loads(settings.read_text()) == {"repos": {"fake:o/r": {"interval": 5, "fetch_limit": 50}}}


def test_the_saved_fetch_limit_is_used_and_an_unchanged_default_is_not_saved(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"repos": {"fake:o/r": {"fetch_limit": 40}}}))

    async def restored():
        app = settings_app(settings)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert app.provider.queries[-1].limit == 40

    run(restored())
    fresh = tmp_path / "fresh.json"

    async def interval_only():
        app = settings_app(fresh)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            await enter_interval(pilot, app, "9")

    run(interval_only())
    assert json.loads(fresh.read_text()) == {"repos": {"fake:o/r": {"interval": 9}}}


def test_the_settings_menu_rejects_a_fetch_limit_that_is_no_positive_whole_number(tmp_path):
    settings = tmp_path / "settings.json"

    async def scenario():
        app = settings_app(settings)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            await open_settings(pilot, app)
            field = app.screen.query_one("#fetch_limit", Input)
            for text in ("abc", "2.5", "0", "-1"):
                field.value = text
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, SettingsMenu)
                assert "Fetch limit" in str(app.screen.query_one("#settings-error").render())
            await pilot.press("escape")
            await pilot.press("r")
            await loaded(app, pilot)
            assert app.provider.queries[-1].limit == 300

    run(scenario())
    assert not settings.exists()


def test_the_status_line_says_when_a_fetch_was_cut_off_at_the_limit():
    async def scenario():
        provider = FakeProvider(TICKETS)
        provider.cut_off = ["open maps", "#9's tickets"]
        app = BoardApp(provider, Query(), interval=60)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert "cut off at 300: open maps, #9's tickets" in status_of(app)
            provider.cut_off = []
            await pilot.press("r")
            await loaded(app, pilot)
            assert "cut off" not in status_of(app)

    run(scenario())


# --- standalone spec lanes -------------------------------------------------------------------------

SPEC_BODY = "## Problem Statement\n\nCards vanish.\n\n## Solution\n\nKeep them.\n"


def standalone(number, title="", **kw):
    """A standalone spec: the spec signature, no map link and no `wayfinder:` label."""
    base = dict(number=number, title=title or f"Standalone {number}", url=f"u{number}", is_open=True,
                body=SPEC_BODY, labels=("ready-for-agent",))
    return Ticket(**{**base, **kw})


def ticket_of(spec, number, title="", what="Build the thing.", **kw):
    """A `/to-tickets` ticket of the standalone `spec`, linked by its `## Parent` section."""
    body = (f"## Parent\n\n#{spec.number}\n\n## What to build\n\n{what}\n\n## Acceptance criteria\n\n"
            "- [ ] Tests pass\n")
    base = dict(number=number, title=title or f"Ticket {number}", url=f"u{number}", is_open=True, body=body,
                parent_number=spec.number, parent_url=spec.url, lane_spec=spec.number)
    return Ticket(**{**base, **kw})


CLOSED_NOW = dict(is_open=False, closed_at=datetime.now(timezone.utc))
ALONE = standalone(70, "Board shows standalone work", assignees=("alice",), has_tickets=True)
CLOSED_ALONE = standalone(40, "Old spec", has_tickets=True, **CLOSED_NOW)
SPEC_LANES = [
    *TICKETS, a_map(20, "Ship billing"), ALONE, CLOSED_ALONE, standalone(85, "Fresh spec"),
    ticket_of(ALONE, 71, what="Show the lane."), ticket_of(ALONE, 72, open_blockers=1, blocked_by=("#71",)),
    ticket_of(ALONE, 73, **CLOSED_NOW),
    ticket_of(ALONE, 74, not_planned=True, is_open=False, closed_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
    ticket_of(ALONE, 75, assignees=("bob",)),
    ticket_of(CLOSED_ALONE, 41), ticket_of(CLOSED_ALONE, 42, **CLOSED_NOW),
    standalone(80, "Done spec", has_tickets=True, **CLOSED_NOW), ticket_of(standalone(80), 81, **CLOSED_NOW),
]


def test_spec_lanes_follow_the_map_lanes_by_number_headed_by_the_spec():
    async def scenario():
        app = BoardApp(FakeProvider(SPEC_LANES), Query(), interval=60)
        async with app.run_test(size=(200, 80)) as pilot:
            await loaded(app, pilot)
            assert headers(app) == [9, 20, 40, 70, 85]  # #80 and all its tickets are closed: no lane
            lanes = shown_lanes(app)
            assert {n: lanes[n] for n in (40, 70, 85)} == {
                40: {"FRONTIER": [41], "DECIDED": [42]},
                70: {"BLOCKED": [72], "FRONTIER": [71], "CLAIMED": [75], "DECIDED": [73, 74]},  # #74 however old
                85: {}}
            header = card_text(app, 70).split("\n")
            assert header[0] == "▣ #70 Board shows standalone work  @alice"
            assert header[1] == "spec · 1 done · 3 open · 1 blocked"  # #74 was canceled
            assert card_text(app, 40).split("\n")[:2] == ["▣ #40 Old spec  spec closed",
                                                         "spec · 1 done · 1 open · 0 blocked"]
            assert card_text(app, 85).split("\n")[1] == "spec · 0 done · 0 open · 0 blocked"
            assert chips_of(app, 71).startswith("impl ") and "▣ Board shows standalone work" in chips_of(app, 71)
            assert "Show the lane." in card_text(app, 71) and "Tests pass" not in card_text(app, 71)
            assert "2 maps · 3 specs · 11 tickets" in status_of(app)

    run(scenario())


def test_the_type_filter_keeps_standalone_cards_and_the_map_filter_hides_spec_lanes(tmp_path):
    async def scenario():
        app = filters_app(tmp_path, tickets=SPEC_LANES)
        async with app.run_test(size=(200, 80)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            await pick(pilot, app, "impl")
            assert headers(app) == [40, 70] and shown_numbers(app) == [41, 42, 71, 72, 73, 74, 75]
            await pick(pilot, app, "impl")  # off again
            await pick(pilot, app, "spec")
            assert headers(app) == [40, 70, 85] and shown_numbers(app) == []
            await pick(pilot, app, "spec")
            assert picker_values(app, "Map") == ["9", "20"]  # spec lanes are no maps
            await pick(pilot, app, "9", "Map")
            assert headers(app) == [9]

    run(scenario())


def test_the_map_named_on_the_command_line_hides_the_spec_lanes(tmp_path):
    async def scenario():
        app = filters_app(tmp_path, Query(named_map=9), SPEC_LANES)
        async with app.run_test(size=(200, 80)) as pilot:
            await loaded(app, pilot)
            assert headers(app) == [9]
            await pilot.press("escape")
            await pilot.pause()
            assert headers(app) == [9, 20, 40, 70, 85]

    run(scenario())
