import asyncio

from ticketmaster.board import BoardApp, Card
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
