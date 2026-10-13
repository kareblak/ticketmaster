import json
import asyncio
from datetime import datetime, timezone

from ticketmaster.board import (BoardApp, Card, FilterPicker, HelpScreen, Pacer, PickerOption, passes_authors,
                               passes_labels, render_card, tag_style)
from ticketmaster.herdr import Session
from ticketmaster.model import Column, Ticket
from ticketmaster.providers import ProviderError, Query, RateLimited


class FakeProvider:
    name = "fake"
    repo = "o/r"

    def __init__(self, tickets, login="me", delete_error=None):
        self.tickets = tickets
        self.calls = 0
        self.login = login
        self.deleted = []
        self.delete_error = delete_error

    def viewer(self):
        if isinstance(self.login, Exception):
            raise self.login
        return self.login

    def fetch(self, query):
        self.calls += 1
        if isinstance(self.tickets, Exception):
            raise self.tickets
        return self.tickets

    def delete(self, number):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(number)
        self.tickets = [t for t in self.tickets if t.number != number]


TICKETS = [
    Ticket(number=1, title="Ready one", url="u1", is_open=True, body="## Question\nWhat now?"),
    Ticket(number=2, title="Doing", url="u2", is_open=True, author="alice", assignees=("me",)),
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


def test_card_shows_author_and_filter_matches_it():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            doing = app.query_one("#col-IN_PROGRESS").query(Card).first()
            assert "by alice" in str(doing.render())
            await pilot.press("slash", *"alice", "enter")
            await pilot.pause()
            assert [c.ticket.number for c in app.query(Card)] == [2]

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

    def auto_remove(self, ticket, sessions):
        self.calls.append(("auto_remove", ticket.number, [x.path for x in sessions]))
        return "ok"


DONE = Ticket(number=4, title="Shipped", url="u4", is_open=False, closed_at=datetime.now(timezone.utc))


def bench_app(bench, tickets=TICKETS, login="me"):
    return BoardApp(FakeProvider(tickets, login), Query(), interval=60, workbench=bench)


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


BLOCKED = Ticket(number=5, title="Waits", url="u5", is_open=True, blocked_by=("#1", "#9"), open_blockers=2)
THEIRS = Ticket(number=6, title="Bob's", url="u6", is_open=True, assignees=("bob",))


def test_s_and_enter_refuse_a_blocked_ticket_naming_its_blockers():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, [BLOCKED])
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down")
            assert app.focused.ticket.number == 5
            for key in ("s", "enter"):
                await pilot.press(key)
                await pilot.pause()
                assert not is_confirm(app)
            await loaded(app, pilot)
            assert bench.calls == []
            messages = [n.message for n in app._notifications]
            assert messages.count("#5 is blocked by #1, #9") == 2

    run(scenario())


def test_s_and_enter_refuse_a_ticket_claimed_by_someone_else():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, [THEIRS])
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down")
            assert app.focused.ticket.number == 6
            await pilot.press("s")
            await pilot.pause()
            assert not is_confirm(app)
            await pilot.press("enter")
            await pilot.pause()
            assert not is_confirm(app) and bench.calls == []
            assert [n.message for n in app._notifications].count("#6 is claimed by @bob") == 2

    run(scenario())


def test_own_claimed_ticket_still_starts():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, [THEIRS], login="Bob")
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s")
            await pilot.pause()
            assert is_confirm(app)
            await pilot.press("y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 6)]

    run(scenario())


def test_unknown_viewer_does_not_refuse_a_claimed_ticket():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, [THEIRS], login=ProviderError("gh: not logged in"))
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s")
            await pilot.pause()
            assert is_confirm(app)

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


def test_auto_remove_cleans_done_tickets_whose_agent_is_not_busy_and_only_once(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"auto_remove": true}')

    async def scenario():
        bench = FakeBench({
            4: [Session(4, "issue", "/w/issue-4", status="idle"), Session(4, "review", "/w/review-4", status="working")],
            2: [Session(2, "issue", "/w/issue-2")],
        })
        app = BoardApp(FakeProvider(TICKETS + [DONE]), Query(), interval=60, workbench=bench, settings_path=settings)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert bench.calls == [("auto_remove", 4, ["/w/issue-4"])]
            bench.current = {4: [Session(4, "issue", "/w/issue-4", status="idle"), Session(4, "review", "/w/review-4", status="done")]}
            await pilot.press("r")
            await loaded(app, pilot)
            assert bench.calls == [("auto_remove", 4, ["/w/issue-4"]), ("auto_remove", 4, ["/w/review-4"])]

    run(scenario())


def test_auto_remove_is_off_by_default():
    async def scenario():
        bench = FakeBench({4: [Session(4, "issue", "/w/issue-4")]})
        app = bench_app(bench, TICKETS + [DONE])
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert bench.calls == []

    run(scenario())


def test_a_toggles_auto_remove_saves_it_and_cleans_up_right_away(tmp_path):
    settings = tmp_path / "ticketmaster" / "settings.json"

    async def scenario():
        bench = FakeBench({4: [Session(4, "issue", "/w/issue-4")]})
        app = BoardApp(FakeProvider(TICKETS + [DONE]), Query(), interval=60, workbench=bench, settings_path=settings)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert bench.calls == [] and "auto-remove" not in str(app.query_one("#status").render())
            await pilot.press("a")
            await loaded(app, pilot)
            assert bench.calls == [("auto_remove", 4, ["/w/issue-4"])]
            assert "auto-remove on" in str(app.query_one("#status").render())
            assert json.loads(settings.read_text()) == {"auto_remove": True}
            await pilot.press("a")
            await pilot.pause()
            assert json.loads(settings.read_text()) == {"auto_remove": False}

    run(scenario())


def test_sec_is_an_alias_for_the_security_colour():
    assert tag_style("sec") == tag_style("security") == "white on red"


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
                assert "herdr agent issue-<n>" in text and "select authors" in text
                await pilot.press(close)
                await pilot.pause()
                assert not isinstance(app.screen, HelpScreen) and app.is_running

    run(scenario())


LABELLED = [
    Ticket(number=1, title="Login bug", url="u1", is_open=True, labels=("bug",)),
    Ticket(number=2, title="Dark mode", url="u2", is_open=True, labels=("ux", "wontfix")),
    Ticket(number=3, title="Crash on save", url="u3", is_open=True, labels=("bug", "wontfix")),
    Ticket(number=4, title="Docs", url="u4", is_open=True),
]


def test_passes_labels_includes_with_or_and_excludes_any():
    known = {"bug", "ux", "wontfix"}
    shown = lambda modes: [t.number for t in LABELLED if passes_labels(t, modes, known)]
    assert shown({}) == [1, 2, 3, 4]
    assert shown({"bug": "include", "ux": "include"}) == [1, 2, 3]
    assert shown({"wontfix": "exclude"}) == [1, 4]
    assert shown({"bug": "include", "wontfix": "exclude"}) == [1]
    assert shown({"gone": "include", "wontfix": "exclude"}) == [1, 4]  # a saved label no longer on the board


def shown_numbers(app):
    return sorted(c.ticket.number for c in app.query(Card))


async def pick(pilot, app, label, times=1):
    """Focuses `label` in the open picker and cycles it."""
    option = next(o for o in app.screen.query(PickerOption) if o.value == label)
    option.focus()
    await pilot.pause()
    for _ in range(times):
        await pilot.press("enter")
    await pilot.pause()


def test_f_picker_lists_board_labels_and_narrows_the_board_live(tmp_path):
    async def scenario():
        app = BoardApp(FakeProvider(LABELLED), Query(), interval=60, settings_path=tmp_path / "settings.json")
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            assert isinstance(app.screen, FilterPicker)
            assert [o.value for o in app.screen.query(PickerOption)] == ["bug", "ux", "wontfix"]
            await pick(pilot, app, "bug")  # include
            assert shown_numbers(app) == [1, 3]
            await pick(pilot, app, "wontfix", times=2)  # exclude
            assert shown_numbers(app) == [1]
            assert "labels: +bug -wontfix" in str(app.query_one("#status").render())
            await pick(pilot, app, "bug", times=2)  # exclude, then off
            assert shown_numbers(app) == [1, 4]
            await pilot.press("c")
            await pilot.pause()
            assert shown_numbers(app) == [1, 2, 3, 4] and app.filters["Labels"] == {}
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, FilterPicker)

    run(scenario())


def test_label_filter_combines_with_text_filter_and_esc_clears_both(tmp_path):
    async def scenario():
        app = BoardApp(FakeProvider(LABELLED), Query(), interval=60, settings_path=tmp_path / "settings.json")
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            await pick(pilot, app, "bug")
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("slash", *"crash", "enter")
            await pilot.pause()
            assert shown_numbers(app) == [3]
            await pilot.press("escape")
            await pilot.pause()
            assert shown_numbers(app) == [1, 2, 3, 4]
            assert "labels:" not in str(app.query_one("#status").render())

    run(scenario())


def test_label_filter_is_saved_per_repo_and_restored(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"auto_remove": True, "filters": {"other:x/y": {"labels": {"ux": "include"}}}}))

    async def choose():
        app = BoardApp(FakeProvider(LABELLED), Query(), interval=60, settings_path=settings)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            await pick(pilot, app, "wontfix", times=2)

    async def reopen():
        app = BoardApp(FakeProvider(LABELLED), Query(), interval=60, settings_path=settings)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert shown_numbers(app) == [1, 4]
            assert "labels: -wontfix" in str(app.query_one("#status").render())

    run(choose())
    assert json.loads(settings.read_text()) == {
        "auto_remove": True,
        "filters": {"other:x/y": {"labels": {"ux": "include"}}, "fake:o/r": {"labels": {"wontfix": "exclude"}, "authors": []}},
    }
    run(reopen())


def test_clicking_a_picker_option_cycles_that_option(tmp_path):
    async def scenario():
        app = BoardApp(FakeProvider(LABELLED), Query(), interval=60, settings_path=tmp_path / "settings.json")
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            option = next(o for o in app.screen.query(PickerOption) if o.value == "ux")
            await pilot.click(option)
            await pilot.pause()
            assert app.filters["Labels"] == {"ux": "include"} and shown_numbers(app) == [2]

    run(scenario())


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


def test_status_warns_until_the_skills_setup_exists_and_rechecks_on_refresh(tmp_path):
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60, repo_root=tmp_path)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert "/setup-matt-pocock-skills" in status_of(app)
            (tmp_path / "docs" / "agents").mkdir(parents=True)
            (tmp_path / "docs" / "agents" / "issue-tracker.md").write_text("# Issue tracker\n")
            await pilot.press("r")
            await loaded(app, pilot)
            assert "/setup-matt-pocock-skills" not in status_of(app)

    run(scenario())


def test_missing_doc_warning_also_names_ticketmaster_setup(tmp_path):
    provider = FakeProvider(TICKETS)
    provider.name = "github"

    async def scenario():
        app = BoardApp(provider, Query(), interval=60, repo_root=tmp_path)
        async with app.run_test(size=(240, 40)) as pilot:
            await loaded(app, pilot)
            assert "/setup-matt-pocock-skills, then ticketmaster setup" in status_of(app)

    run(scenario())


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


def test_no_warning_when_the_skills_setup_exists(tmp_path):
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    (tmp_path / "docs" / "agents" / "issue-tracker.md").write_text("# Issue tracker\n")

    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60, repo_root=tmp_path)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert "setup-matt-pocock-skills" not in status_of(app)

    run(scenario())


def test_status_warns_when_the_wayfinding_rules_are_missing_or_out_of_date(tmp_path):
    from ticketmaster.wayfinding import setup

    doc = tmp_path / "docs" / "agents" / "issue-tracker.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Issue tracker\n")
    provider = FakeProvider(TICKETS)
    provider.name = "github"

    async def scenario():
        app = BoardApp(provider, Query(), interval=60, repo_root=tmp_path)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert "run ticketmaster setup" in status_of(app) and "missing" in status_of(app)
            setup(tmp_path, "github")
            await pilot.press("r")
            await loaded(app, pilot)
            assert "ticketmaster setup" not in status_of(app)
            doc.write_text(doc.read_text().replace("sub-issue", "task list"))
            await pilot.press("r")
            await loaded(app, pilot)
            assert "run ticketmaster setup" in status_of(app) and "out of date" in status_of(app)

    run(scenario())


def test_sessions_still_start_while_the_setup_warning_shows(tmp_path):
    async def scenario():
        bench = FakeBench()
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60, workbench=bench, repo_root=tmp_path)
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert "/setup-matt-pocock-skills" in status_of(app)
            await pilot.press("down", "s", "y")
            await loaded(app, pilot)
            await pilot.press("right", "right", "c", "y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 1), ("review", 3)]

    run(scenario())


def test_help_mentions_the_skills_setup():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            text = app.screen.text.plain
            assert "/setup-matt-pocock-skills" in text and "docs/agents/issue-tracker.md" in text
            assert "ticketmaster setup" in text

    run(scenario())


AUTHORED = [
    Ticket(number=1, title="Login bug", url="u1", is_open=True, labels=("bug",), author="alice"),
    Ticket(number=2, title="Dark mode", url="u2", is_open=True, labels=("ux",), author="bob"),
    Ticket(number=3, title="Crash on save", url="u3", is_open=True, labels=("bug",), author="bob"),
    Ticket(number=4, title="Docs", url="u4", is_open=True, author="carol"),
    Ticket(number=5, title="Imported", url="u5", is_open=True),
]


def test_passes_authors_matches_any_selected_author():
    known = {"alice", "bob", "carol"}
    shown = lambda selected: [t.number for t in AUTHORED if passes_authors(t, selected, known)]
    assert shown({}) == [1, 2, 3, 4, 5]
    assert shown({"bob": "include"}) == [2, 3]
    assert shown({"alice": "include", "carol": "include"}) == [1, 4]
    assert shown({"gone": "include"}) == [1, 2, 3, 4, 5]  # a saved author no longer on the board


def test_picker_selects_authors_on_and_off_and_shows_them_in_the_status_line(tmp_path):
    async def scenario():
        app = BoardApp(FakeProvider(AUTHORED), Query(), interval=60, settings_path=tmp_path / "settings.json")
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            authors = [o.value for o in app.screen.query(PickerOption) if o.section == "Authors"]
            assert authors == ["alice", "bob", "carol"]
            await pick(pilot, app, "alice")
            await pick(pilot, app, "carol")
            assert shown_numbers(app) == [1, 4]
            assert "authors: alice, carol" in str(app.query_one("#status").render())
            await pick(pilot, app, "carol")  # second press deselects; authors have no exclude
            assert shown_numbers(app) == [1] and app.filters["Authors"] == {"alice": "include"}
            await pilot.press("c")
            await pilot.pause()
            assert shown_numbers(app) == [1, 2, 3, 4, 5] and app.filters["Authors"] == {}
            assert "authors:" not in str(app.query_one("#status").render())

    run(scenario())


def test_author_filter_combines_with_label_and_text_filters_and_esc_clears_all(tmp_path):
    async def scenario():
        app = BoardApp(FakeProvider(AUTHORED), Query(), interval=60, settings_path=tmp_path / "settings.json")
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            await pick(pilot, app, "bob")
            assert shown_numbers(app) == [2, 3]
            await pick(pilot, app, "bug")
            assert shown_numbers(app) == [3]
            await pick(pilot, app, "bug")  # exclude
            assert shown_numbers(app) == [2]
            await pick(pilot, app, "bug")  # off
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("slash", *"dark", "enter")
            await pilot.pause()
            assert shown_numbers(app) == [2]
            await pilot.press("escape")
            await pilot.pause()
            assert shown_numbers(app) == [1, 2, 3, 4, 5] and app.filters["Authors"] == {}

    run(scenario())


def test_author_filter_is_saved_per_repo_and_restored(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"filters": {"other:x/y": {"labels": {}, "authors": ["alice"]}}}))

    async def choose():
        app = BoardApp(FakeProvider(AUTHORED), Query(), interval=60, settings_path=settings)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            await pick(pilot, app, "carol")
            await pick(pilot, app, "bob")
            await pick(pilot, app, "ux")

    async def reopen():
        app = BoardApp(FakeProvider(AUTHORED), Query(), interval=60, settings_path=settings)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert shown_numbers(app) == [2]
            assert "authors: bob, carol" in str(app.query_one("#status").render())

    run(choose())
    assert json.loads(settings.read_text()) == {
        "filters": {
            "other:x/y": {"labels": {}, "authors": ["alice"]},
            "fake:o/r": {"labels": {"ux": "include"}, "authors": ["bob", "carol"]},
        },
    }
    run(reopen())


NO_MAP = Ticket(number=5, title="Which store?", url="u5", is_open=True, labels=("wayfinder:grilling",))


def test_typed_ticket_without_a_map_warns_on_its_card_and_never_starts():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, [NO_MAP])
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            card = app.query_one("#col-READY").query(Card).first()
            assert "map not found" in str(card.render())
            await pilot.press("down", "s")
            await pilot.pause()
            assert not is_confirm(app)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert bench.calls == []
            assert any("map" in n.message and "#5" in n.message for n in app._notifications)

    run(scenario())


def notifications(app):
    return [(n.message, n.severity) for n in app._notifications]


def test_backspace_on_a_ready_card_asks_and_cancel_keeps_the_issue():
    async def scenario():
        provider = FakeProvider(TICKETS)
        app = BoardApp(provider, Query(), interval=60)  # deleting needs no herdr
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "backspace")
            await pilot.pause()
            assert is_confirm(app)
            assert app.screen.question == "Delete #1 Ready one?"
            assert "cannot be undone" in app.screen.detail and "left behind" not in app.screen.detail
            await pilot.press("n")
            await loaded(app, pilot)
            assert provider.deleted == [] and not is_confirm(app)
            await pilot.press("delete")
            await pilot.pause()
            assert is_confirm(app)
            await pilot.press("escape")
            await loaded(app, pilot)
            assert provider.deleted == [] and len(app.query(Card)) == 3

    run(scenario())


def test_delete_on_a_done_card_with_worktrees_lists_them_then_deletes_and_refreshes():
    async def scenario():
        provider = FakeProvider(TICKETS + [DONE])
        bench = FakeBench({4: [Session(4, "issue", "/w/issue-4"), Session(4, "review", "/w/review-4")]})
        app = BoardApp(provider, Query(), interval=60, workbench=bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "right", "right", "right")
            assert app.focused.ticket.number == 4
            await pilot.press("delete")
            await pilot.pause()
            assert is_confirm(app)
            detail = app.screen.detail
            assert "cannot be undone" in detail and "/w/issue-4" in detail and "/w/review-4" in detail
            assert "not removed" in detail
            await pilot.press("y")
            await loaded(app, pilot)
            await loaded(app, pilot)
            assert provider.deleted == [4] and bench.calls == []
            assert 4 not in [c.ticket.number for c in app.query(Card)]
            assert ("Deleted #4 Shipped", "information") in notifications(app)

    run(scenario())


def test_delete_is_refused_in_progress_and_review():
    async def scenario():
        provider = FakeProvider(TICKETS)
        app = BoardApp(provider, Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "right", "delete")  # In progress
            await pilot.pause()
            assert not is_confirm(app)
            await pilot.press("right", "backspace")  # Review
            await pilot.pause()
            assert not is_confirm(app) and provider.deleted == []
            warnings = [m for m, severity in notifications(app) if severity == "warning"]
            assert len(warnings) == 2 and "#2" in warnings[0] and "#3" in warnings[1]

    run(scenario())


def test_typed_ticket_with_its_map_starts():
    async def scenario():
        bench = FakeBench()
        typed = Ticket(number=5, title="Which store?", url="u5", is_open=True, labels=("wayfinder::grilling",),
                       parent_number=1, parent_url="u1", parent_labels=("wayfinder:map",))
        app = bench_app(bench, [typed])
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert "map not found" not in str(app.query_one("#col-READY").query(Card).first().render())
            await pilot.press("down", "s", "y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 5)]

    run(scenario())


def test_a_tracker_error_on_delete_is_an_error_notification_and_the_card_stays():
    async def scenario():
        provider = FakeProvider(TICKETS, delete_error=ProviderError("must have admin rights to Repository"))
        app = BoardApp(provider, Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "delete", "y")
            await loaded(app, pilot)
            assert ("must have admin rights to Repository", "error") in [
                (m.split(": ", 1)[-1], s) for m, s in notifications(app)]
            assert 1 in [c.ticket.number for c in app.query(Card)]

    run(scenario())


def test_backspace_in_the_filter_input_edits_the_text_and_deletes_nothing():
    async def scenario():
        provider = FakeProvider(TICKETS)
        app = BoardApp(provider, Query(), interval=60)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "slash", *"ready", "backspace", "delete")
            await pilot.pause()
            assert not is_confirm(app) and provider.deleted == []
            assert app.query_one("#filter").value == "read"

    run(scenario())


def test_help_lists_the_delete_binding():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            text = app.screen.text.plain
            assert "Delete issue" in text and "Blocked, Ready" in text

    run(scenario())


MAP_CARD = Ticket(number=1, title="Pick a stack", url="u1", is_open=True, labels=("wayfinder:map",),
                  body="## Decisions so far\n- Store: Postgres\n")


def map_child(number, **kw):
    base = dict(number=number, title=f"Question {number}", url=f"u{number}", is_open=True,
                labels=("wayfinder:grilling",), parent_number=1, parent_url="u1", parent_labels=("wayfinder:map",))
    return Ticket(**{**base, **kw})


MAP_BOARD = [MAP_CARD, map_child(5, order=1), map_child(6, order=2), map_child(7, order=0, open_blockers=1)]


async def focus_map(app, pilot):
    await loaded(app, pilot)
    await pilot.press("down")
    while app.focused.ticket.number != 1:
        await pilot.press("right")


def test_map_card_shows_progress_in_progress_not_ready():
    async def scenario():
        app = bench_app(FakeBench(), MAP_BOARD)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            assert 1 not in [c.ticket.number for c in app.query_one("#col-READY").query(Card)]
            card = app.query_one("#col-IN_PROGRESS").query(Card).first()
            assert card.ticket.number == 1
            assert "1 decided · 3 open · 1 blocked" in str(card.render())

    run(scenario())


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


def test_frontier_skips_tickets_that_already_have_a_session():
    async def scenario():
        bench = FakeBench({5: [Session(5, "issue", "/w/issue-5")]})
        app = bench_app(bench, MAP_BOARD)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("s", "y")
            await loaded(app, pilot)
            assert bench.calls == [("start", 6)]

    run(scenario())


def test_empty_frontier_says_so_and_starts_nothing():
    async def scenario():
        bench = FakeBench()
        tickets = [MAP_CARD, map_child(5, assignees=("bob",)), map_child(7, open_blockers=1)]
        app = bench_app(bench, tickets)
        async with app.run_test(size=(160, 40)) as pilot:
            await focus_map(app, pilot)
            await pilot.press("s")
            await pilot.pause()
            assert not is_confirm(app)
            await pilot.press("enter")
            await loaded(app, pilot)
            assert bench.calls == []
            warnings = [m for m, severity in notifications(app) if severity == "warning"]
            assert len(warnings) == 2 and all("#1" in m and "frontier" in m for m in warnings)

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


def test_snapshot_lists_a_blocked_tickets_open_blockers(monkeypatch, capsys):
    from ticketmaster import cli, config

    tickets = [BLOCKED, Ticket(number=7, title="Free", url="u7", is_open=True),
               Ticket(number=8, title="Counted", url="u8", is_open=True, open_blockers=1)]
    monkeypatch.setattr(config, "resolve_provider", lambda *args: FakeProvider(tickets))
    assert cli.main(["snapshot"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "  #5 Waits blocked by #1, #9" in lines
    assert "  #7 Free" in lines and "  #8 Counted" in lines
