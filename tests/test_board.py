import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from ticketmaster.board import (BoardApp, Card, FilterPicker, HelpScreen, Pacer, PickerOption, passes_authors,
                               passes_labels, render_card, tag_style)
from ticketmaster.herdr import Session
from ticketmaster.model import Column, Ticket
from ticketmaster.providers import ProviderError, Query, RateLimited


class FakeProvider:
    name = "fake"
    repo = "o/r"

    def __init__(self, tickets, login="me"):
        self.tickets = tickets
        self.calls = 0
        self.login = login

    def viewer(self):
        if isinstance(self.login, Exception):
            raise self.login
        return self.login

    def fetch(self, query):
        self.calls += 1
        if isinstance(self.tickets, Exception):
            raise self.tickets
        return self.tickets


TICKETS = [
    # #1 is a spec on map #9, so s starts it with /wayfinder; plain issues are never started.
    Ticket(number=1, title="Ready one", url="u1", is_open=True, body="## Question\nWhat now?", map_number=9, map_url="u9"),
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

    def go_to(self, ticket, session):
        self.calls.append(("go_to", ticket.number, session.path))
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
THEIRS = Ticket(number=6, title="Bob's", url="u6", is_open=True, assignees=("bob",), map_number=9, map_url="u9")


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


def test_enter_on_a_card_without_session_never_starts_and_says_s_does():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "enter")
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == []
            assert notifications(app)[-1][0] == "#1 has no session here; s starts one"

    run(scenario())


def test_double_click_acts_like_enter_and_never_starts():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            card = app.query_one("#col-READY").query(Card).first()
            await pilot.click(card, times=2)
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == []
            assert notifications(app)[-1][0] == "#1 has no session here; s starts one"

    run(scenario())


def test_s_refuses_a_plain_issue_because_the_board_only_starts_wayfinder():
    async def scenario():
        bench = FakeBench()
        app = bench_app(bench, [Ticket(number=7, title="Plain", url="u7", is_open=True)])
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            await pilot.press("down", "s")
            await loaded(app, pilot)
            assert not is_confirm(app) and bench.calls == []
            assert notifications(app)[-1][0] == "#7 is not wayfinder work: the board only starts /wayfinder"

    run(scenario())


def test_cards_show_agent_state_and_enter_goes_to_the_session():
    async def scenario():
        bench = FakeBench({2: [Session(2, "/w/issue-2", workspace="w5", status="blocked")]})
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
            bench.current = {1: [Session(1, "/w/issue-1", workspace="w9", status="working")]}
            await pilot.press("r")
            await loaded(app, pilot)
            assert "working" in str(app.query_one("#col-READY").query(Card).first().render())

    run(scenario())


REMOVED_KEYS = ("c", "x", "a", "backspace", "delete")


def test_removed_keys_do_nothing_on_any_card(tmp_path):
    settings = tmp_path / "settings.json"

    async def scenario():
        sessions = {n: [Session(n, f"/w/issue-{n}")] for n in (1, 2, 3, 4)}
        bench = FakeBench(sessions)
        provider = FakeProvider(TICKETS + [DONE])
        app = BoardApp(provider, Query(), interval=60, workbench=bench, settings_path=settings)
        async with app.run_test(size=(160, 40)) as pilot:
            await loaded(app, pilot)
            before = notifications(app)
            for number in (1, 2, 3, 4):
                card = next(c for c in app.query(Card) if c.ticket.number == number)
                card.focus()
                await pilot.pause()
                for key in REMOVED_KEYS:
                    await pilot.press(key)
                    await loaded(app, pilot)
                    assert not is_confirm(app)
            assert bench.calls == [] and notifications(app) == before
            assert not settings.exists() and len(app.query(Card)) == 4

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
                assert "The board only reads" in text
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


def test_help_says_a_maps_spec_and_its_tickets_start_wayfinder():
    async def scenario():
        app = BoardApp(FakeProvider(TICKETS), Query(), interval=60)
        async with app.run_test(size=(160, 50)) as pilot:
            await loaded(app, pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            text = " ".join(app.screen.text.plain.split())
            assert "spec or its ticket the wayfinder skill with the map's and the ticket's URLs" in text

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


MAP_URL = "https://gh/o/r/issues/9"
ON_MAP = dict(parent_url=MAP_URL, parent_labels=("wayfinder:map",))
GRILL = Ticket(number=11, title="Which store?", url="u11", is_open=True, labels=("wayfinder:grilling",), **ON_MAP)
DIG = Ticket(number=12, title="Read the API", url="u12", is_open=True, labels=("wayfinder:research",), **ON_MAP)
CHORE = Ticket(number=13, title="Sign up", url="u13", is_open=True, labels=("wayfinder:task",),
               body="## Question\nAFK: the agent can sign up alone.", **ON_MAP)


def card_text(app, number):
    return str(next(c for c in app.query(Card) if c.ticket.number == number).render())


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


def test_done_marks_out_of_scope_tickets_apart_from_resolved_decisions():
    closed = dict(is_open=False, closed_at=datetime.now(timezone.utc))
    ruled_out = Ticket(number=21, title="Mobile app", url="u21", labels=("wayfinder:grilling", "out-of-scope"), **closed)
    resolved = Ticket(number=22, title="Which DB?", url="u22", labels=("wayfinder:grilling",), **closed)

    async def scenario():
        app = bench_app(FakeBench(), [ruled_out, resolved])
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert [c.ticket.number for c in app.query_one("#col-DONE").query(Card)] == [21, 22]
            assert "out of scope" in card_text(app, 21) and "[out-of-scope]" not in card_text(app, 21)
            assert "out of scope" not in card_text(app, 22)

    run(scenario())


def test_decision_tickets_never_show_in_review():
    with_pr = Ticket(number=11, title="Which store?", url="u11", is_open=True, has_open_change_request=True,
                     labels=("wayfinder:grilling", "review:ux"), **ON_MAP)

    async def scenario():
        app = bench_app(FakeBench(), [with_pr])
        async with app.run_test(size=(200, 40)) as pilot:
            await loaded(app, pilot)
            assert not list(app.query_one("#col-REVIEW").query(Card))

    run(scenario())


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


def test_frontier_skips_tickets_that_already_have_a_session():
    async def scenario():
        bench = FakeBench({5: [Session(5, "/w/issue-5")]})
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
            assert "#1" in warnings[0] and "frontier" in warnings[0]
            assert warnings[1:] == ["#1 has no session here; s starts one"]  # Enter never starts

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


