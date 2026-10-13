import asyncio

from ticketmaster.board import HERDR_PLACEMENTS, BoardApp, Card, HelpScreen
from ticketmaster.launch import Placement
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


def start_app(starter_calls):
    def starter(ticket, placement):
        starter_calls.append((ticket.number, placement))
        return f"Started #{ticket.number}"

    return BoardApp(FakeProvider(TICKETS), Query(), interval=60, starter=starter)


async def loaded(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_start_ready_ticket_after_confirm():
    async def scenario():
        calls = []
        app = start_app(calls)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s")
            await pilot.pause()
            assert type(app.screen).__name__ == "ConfirmStart"
            await pilot.press("t")
            await loaded(app, pilot)
            assert calls == [(1, Placement.TAB)]
            card = app.query_one("#col-READY").query(Card).first()
            assert "session started" in str(card.render())
            await pilot.press("down", "s")
            await pilot.pause()
            assert type(app.screen).__name__ != "ConfirmStart" and len(calls) == 1

    run(scenario())


def test_cancel_does_not_start():
    async def scenario():
        calls = []
        app = start_app(calls)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s", "n")
            await loaded(app, pilot)
            assert calls == []

    run(scenario())


def test_only_ready_tickets_can_start():
    async def scenario():
        calls = []
        app = start_app(calls)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "right", "s")
            await pilot.pause()
            assert app.focused.ticket.number == 2
            assert type(app.screen).__name__ != "ConfirmStart" and calls == []

    run(scenario())


def test_double_click_opens_confirm():
    async def scenario():
        calls = []
        app = start_app(calls)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            card = app.query_one("#col-READY").query(Card).first()
            await pilot.click(card, times=2)
            await pilot.pause()
            assert type(app.screen).__name__ == "ConfirmStart"
            await pilot.click("#split-right")
            await loaded(app, pilot)
            assert calls == [(1, Placement.SPLIT_RIGHT)]

    run(scenario())


def test_pane_down_key():
    async def scenario():
        calls = []
        app = start_app(calls)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s", "d")
            await loaded(app, pilot)
            assert calls == [(1, Placement.SPLIT_DOWN)]

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


def test_auto_key():
    async def scenario():
        calls = []
        app = start_app(calls)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s", "a")
            await loaded(app, pilot)
            assert calls == [(1, Placement.AUTO)]

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


def test_herdr_placements_offer_a_workspace():
    async def scenario():
        calls = []
        app = BoardApp(
            FakeProvider(TICKETS), Query(), interval=60,
            starter=lambda ticket, placement: calls.append((ticket.number, placement)) or "ok",
            placements=HERDR_PLACEMENTS,
        )
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s", "a")  # auto is not offered in herdr
            await pilot.pause()
            assert type(app.screen).__name__ == "ConfirmStart"
            await pilot.press("w")
            await loaded(app, pilot)
            assert calls == [(1, Placement.WORKSPACE)]

    run(scenario())


def test_help_lists_every_visible_binding_and_closes_without_quitting():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60, placements=HERDR_PLACEMENTS)
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
                assert "Worktree workspace" in text and "herdr agent" in text
                await pilot.press(close)
                await pilot.pause()
                assert not isinstance(app.screen, HelpScreen) and app.is_running

    run(scenario())
