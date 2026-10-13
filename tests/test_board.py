import asyncio
from datetime import datetime, timezone

from ticketmaster.board import BoardApp, Card
from ticketmaster.herdr import Session
from ticketmaster.model import Ticket
from ticketmaster.providers import ProviderError, Query


class FakeProvider:
    name = "fake"
    repo = "o/r"

    def __init__(self, tickets):
        self.tickets = tickets
        self.calls = 0

    def fetch(self, query):
        self.calls += 1
        if isinstance(self.tickets, Exception):
            raise self.tickets
        return self.tickets


TICKETS = [
    Ticket(number=1, title="Ready one", url="u1", is_open=True, body="## Question\nWhat now?"),
    Ticket(number=2, title="Doing", url="u2", is_open=True, assignees=("me",)),
    Ticket(number=3, title="Reviewing", url="u3", is_open=True, labels=("review:security", "review:ux")),
]


def run(coro):
    return asyncio.run(coro)


def test_board_renders_columns_and_navigates():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            cards = list(app.query(Card))
            assert sorted(c.ticket.number for c in cards) == [1, 2, 3]
            review = app.query_one("#col-REVIEW").query(Card).first()
            assert "security" in str(review.render()) and "ux" in str(review.render())
            await pilot.press("down")
            assert app.focused.ticket.number == 1
            await pilot.press("right")
            assert app.focused.ticket.number == 2
            await pilot.press("right")
            assert app.focused.ticket.number == 3

    run(scenario())


def test_filter_and_refresh_keeps_focus():
    async def scenario():
        provider = FakeProvider(TICKETS)
        app = BoardApp(provider, Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("slash", *"doing", "enter")
            await pilot.pause()
            assert [c.ticket.number for c in app.query(Card)] == [2]
            assert app.focused.ticket.number == 2
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert provider.calls >= 2 and app.focused.ticket.number == 2
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.query(Card)) == 3

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

    def review(self, ticket):
        self.calls.append(("review", ticket.number))
        return "ok"

    def go_to(self, ticket, session):
        self.calls.append(("go_to", ticket.number, session.path))
        return "ok"

    def remove(self, ticket, sessions):
        self.calls.append(("remove", ticket.number, len(sessions)))
        return "ok"


DONE = Ticket(number=4, title="Shipped", url="u4", is_open=False, closed_at=datetime.now(timezone.utc))


def bench_app(bench, tickets=TICKETS):
    return BoardApp(FakeProvider(tickets), Query(), interval=60, workbench=bench)


async def loaded(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


def is_confirm(app):
    return type(app.screen).__name__ == "Confirm"


def test_start_ready_ticket_after_confirm():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s")
            await pilot.pause()
            assert is_confirm(app)
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 1)]
            card = app.query_one("#col-READY").query(Card).first()
            assert "starting" in str(card.render())

    run(scenario())


def test_cancel_does_not_start():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s", "n")
            await loaded(app, pilot)
            assert bench.calls == []

    run(scenario())


def test_enter_on_ready_card_without_session_asks_to_start_and_enter_confirms():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "enter")
            await pilot.pause()
            assert is_confirm(app)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert bench.calls == [("start", 1)]

    run(scenario())


def test_double_click_opens_confirm():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            card = app.query_one("#col-READY").query(Card).first()
            await pilot.click(card, times=2)
            await pilot.pause()
            assert is_confirm(app)
            await pilot.click("#yes")
            await loaded(app, pilot)
            assert bench.calls == [("start", 1)]

    run(scenario())


def test_cards_show_agent_state_and_enter_goes_to_the_session():
    async def scenario():
        bench = FakeBench({2: [Session(2, "issue", "/w/issue-2", workspace="w5", status="blocked")]})
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            card = app.query_one("#col-IN_PROGRESS").query(Card).first()
            assert "needs you" in str(card.render()) and "issue-2" in str(card.render())
            await pilot.press("down", "right", "enter")
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == [("go_to", 2, "/w/issue-2")]

    run(scenario())


def test_new_herdr_session_shows_up_on_refresh():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            bench.current = {1: [Session(1, "issue", "/w/issue-1", workspace="w9", status="working")]}
            await pilot.press("r")
            await loaded(app, pilot)
            assert "working" in str(app.query_one("#col-READY").query(Card).first().render())

    run(scenario())


def test_review_key_starts_a_review_worktree_only_in_review():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "c")  # Ready card
            await pilot.pause()
            assert not is_confirm(app)
            await pilot.press("right", "right", "c", "y")
            await loaded(app, pilot)
            assert bench.calls == [("review", 3)]

    run(scenario())


def test_enter_in_review_without_session_offers_a_review():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "right", "right", "enter", "y")
            await loaded(app, pilot)
            assert bench.calls == [("review", 3)]

    run(scenario())


def test_remove_only_for_done_tickets_with_worktrees():
    async def scenario():
        bench = FakeBench({4: [Session(4, "issue", "/w/issue-4")], 2: [Session(2, "issue", "/w/issue-2")]})
        app = bench_app(bench, TICKETS + [DONE])
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "right", "x")  # In progress
            await pilot.pause()
            assert not is_confirm(app)
            await pilot.press("right", "right")
            assert app.focused.ticket.number == 4
            await pilot.press("x")
            await pilot.pause()
            assert is_confirm(app)
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("remove", 4, 1)]

    run(scenario())


def test_without_herdr_nothing_starts():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s")
            await pilot.pause()
            assert not is_confirm(app)

    run(scenario())


def test_up_down_move_between_cards_in_a_column():
    async def scenario():
        tickets = TICKETS + [
            Ticket(number=4, title="Ready two", url="u4", is_open=True),
            Ticket(number=5, title="Ready three", url="u5", is_open=True),
        ]
        app = BoardApp(FakeProvider(tickets), Query(), interval=60)
        async with app.run_test(size=(160, 9)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down")
            assert app.focused.ticket.number == 1
            await pilot.press("down", "down")
            await pilot.pause()
            assert app.focused.ticket.number == 5
            column = app.query_one("#col-READY")
            assert column.max_scroll_y > 0 and column.scroll_y > 0  # focused card scrolled into view
            await pilot.press("up", "up")
            await pilot.pause()
            assert app.focused.ticket.number == 1 and column.scroll_y == 0

    run(scenario())


def test_narrow_board_shows_one_column_and_switches_with_arrows():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(60, 30)) as pilot:
            await loaded(app, pilot)
            visible = lambda: [c.id for c in app.query(".column") if c.display]
            assert app.single and visible() == ["column-READY"]
            assert "Ready 1" in str(app.query_one("#tabs").render())
            await pilot.press("down")
            assert app.focused.ticket.number == 1
            await pilot.press("right")
            assert visible() == ["column-IN_PROGRESS"] and app.focused.ticket.number == 2
            await pilot.press("left", "left")
            assert visible() == ["column-BLOCKED"] and app.focused is None  # empty columns still show
            await pilot.press("4")
            assert visible() == ["column-REVIEW"] and app.focused.ticket.number == 3
            await pilot.press("v")
            assert len(visible()) == 5 and app.focused.ticket.number == 3

    run(scenario())


def test_wide_board_can_be_forced_to_one_column():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60, columns="one")
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert [c.id for c in app.query(".column") if c.display] == ["column-READY"]

    run(scenario())
