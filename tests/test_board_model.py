"""The board model, driven synchronously: cards, filters, settings, the start flow and the starting marker."""

import json
from datetime import datetime, timedelta, timezone

from fakes import FakeBench, FakeProvider, a_map, impl_of, on, spec_of
from ticketmaster.board_model import (BoardModel, Chip, GoTo, Hint, Pacer, Refusal, StartPlan, budget_pace, card_view,
                                      passes_filters)
from ticketmaster.backends import BackendError
from ticketmaster.session import Session
from ticketmaster.model import Column, LaneKind, Ticket
from ticketmaster.providers import Query, Quota

MAP = a_map(9, "Pick a stack")
TICKETS = [
    MAP,
    on(9, 1, "Ready one", "task", body="## Question\nWhat now?"),
    on(9, 2, "Doing", author="alice", assignees=("me",)),
    on(9, 3, "Waits", open_blockers=1, blocked_by=("#1",)),
    on(9, 4, "Shipped", is_open=False, closed_at=datetime.now(timezone.utc)),
]
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
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class Wall:
    """The time of day, moved by hand."""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def loaded(model):
    """The model after its first fetch, run on this thread."""
    model.apply(model.fetch(model.due()))
    return model


def a_model(tickets=TICKETS, bench=None, **kw):
    return loaded(BoardModel(FakeProvider(tickets), Query(), interval=60, workbench=bench, **kw))


def cells(model):
    """{lane header number: {column: [ticket numbers]}} for the lanes the snapshot shows; empty columns left out."""
    return {lane.header.ticket.number: {column.name: [view.ticket.number for view in views]
                                        for column, views in lane.cells if views}
            for lane in model.snapshot().lanes}


# --- cards ------------------------------------------------------------------------------------------


def test_ticket_cards_carry_type_mode_blockers_assignees_and_the_lane_title():
    view = card_view(on(9, 3, kind="research", assignees=("bob",), open_blockers=2, blocked_by=("#1", "o/x#4")),
                     Column.BLOCKED, lane_title="Pick a stack")
    assert view.chips == (Chip("type", "research"), Chip("mode", "AFK"), Chip("blockers", "#1 o/x#4"),
                          Chip("assignees", "bob"), Chip("lane", "Pick a stack"))
    assert view.lane_kind is LaneKind.MAP and view.ref == "#3"


def test_a_decided_card_is_only_its_ref_and_title():
    done = on(9, 4, "Shipped", kind="research", assignees=("bob",), is_open=False, closed_at=datetime.now(timezone.utc))
    view = card_view(done, Column.DECIDED, [Session(number=4, path="/w/issue-4", status="done")],
                     lane_title="Pick a stack")
    assert (view.ref, view.title, view.badges, view.chips) == ("#4", "Shipped", (), ())


def blockers(**fields):
    view = card_view(Ticket(number=5, title="Waits", url="u5", is_open=True, **fields), Column.BLOCKED)
    return next((chip.text for chip in view.chips if chip.kind == "blockers"), None)


def test_a_blocked_card_lists_each_open_blocker_in_order_also_in_another_repo():
    assert blockers(blocked_by=("#12",), open_blockers=1) == "#12"
    many = tuple(f"#{n}" for n in range(20, 0, -1))
    assert blockers(blocked_by=many, open_blockers=len(many)) == " ".join(many)
    assert blockers(blocked_by=("#3", "other/lib#12"), open_blockers=2) == "#3 other/lib#12"


def test_a_blocked_card_without_refs_keeps_the_count_and_a_label_only_card_has_no_blockers():
    assert blockers(open_blockers=2) == "2"
    assert blockers(labels=("blocked",)) is None
    assert blockers(labels=("blocked",), open_blockers=0) is None


def test_the_snapshot_groups_lanes_into_columns_with_badges_counts_and_the_status_line():
    bench = FakeBench({2: [Session(2, "/w/issue-2", status="working")]})
    model = a_model(bench=bench)
    snapshot = model.snapshot()
    assert cells(model) == {9: {"BLOCKED": [3], "FRONTIER": [1], "CLAIMED": [2], "DECIDED": [4]}}
    claimed = snapshot.lanes[0].cells[2][1][0]
    assert [(b.kind, b.status, b.worktree) for b in claimed.badges] == [("session", "working", "issue-2")]
    assert snapshot.counts == {Column.BLOCKED: 1, Column.FRONTIER: 1, Column.CLAIMED: 1, Column.DECIDED: 1}
    assert snapshot.lanes[0].header.lane.progress.open == 3
    segments = model.status()
    assert segments[0] == ("fake:o/r · open maps", "title") and "1 map · 4 tickets" in segments[1].text


def test_a_board_without_board_work_and_a_filtered_away_board_say_why_they_are_empty():
    assert a_model([]).snapshot().empty == ("No open wayfinder maps or specs in o/r.", False)
    model = a_model()
    model.needle = "nothing matches this"
    assert model.snapshot().lanes == () and model.snapshot().empty == ("No cards match the filters", True)
    assert ("filter: nothing matches this", "filter") in [(s.text.lstrip(" ·"), s.style) for s in model.status()]


# --- filters ----------------------------------------------------------------------------------------


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


def test_budget_pace_is_the_cost_spread_over_the_hourly_budget_in_whole_seconds():
    assert budget_pace(Quota(cost=1, remaining=4000, resets_at=None), 1000, NOW) == 4  # 3.6
    assert budget_pace(Quota(cost=12, remaining=4000, resets_at=None), 1000, NOW) == 44  # 43.2
    assert budget_pace(Quota(cost=12, remaining=None, resets_at=None), 2000, NOW) == 22  # 21.6
    assert budget_pace(Quota(cost=0, remaining=0, resets_at=NOW + timedelta(minutes=5)), 1000, NOW) == 0


def test_budget_pace_stretches_until_the_points_left_last_to_the_reset():
    reset = NOW + timedelta(minutes=50)
    assert budget_pace(Quota(cost=12, remaining=4000, resets_at=reset), 1000, NOW) == 44  # plenty left
    assert budget_pace(Quota(cost=12, remaining=100, resets_at=reset), 1000, NOW) == 12 * 3000 / 100
    assert budget_pace(Quota(cost=12, remaining=0, resets_at=reset), 1000, NOW) == 3000  # wait for the reset
    assert budget_pace(Quota(cost=12, remaining=0, resets_at=NOW - timedelta(seconds=1)), 1000, NOW) == 44


def test_a_pace_slows_tickets_but_never_below_the_interval_and_never_sessions():
    pacer = Pacer(5, now=0)
    pacer.started(0, tickets=True)
    pacer.pace(3.6)
    assert pacer.tickets_every == 5 and pacer.due(5) == (True, True)
    pacer.started(5, tickets=True)
    pacer.pace(44)
    assert pacer.tickets_every == 44
    assert pacer.due(10) == (False, True) and pacer.due(48.9) == (False, True) and pacer.due(49) == (True, True)
    pacer.started(10, tickets=False)
    assert pacer.due(15) == (False, True)  # sessions keep the interval


def test_a_rate_limit_overrides_the_pace_and_its_recovery_returns_to_it():
    pacer = Pacer(5, now=0)
    pacer.started(0, tickets=True)
    pacer.pace(100)
    pacer.started(100, tickets=True)
    pacer.fetched(101, ok=False, rate_limited=True)
    pacer.pace(100)  # a pace never cuts the back-off's minute short or stretches it
    assert pacer.limited and pacer.wait(101) == 60
    pacer.started(161, tickets=True)
    pacer.fetched(162, ok=True, rate_limited=False)
    pacer.pace(100)  # the board paces after every good fetch
    assert not pacer.limited and pacer.due(260) == (False, True) and pacer.due(261) == (True, True)


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


def test_picker_values_come_from_the_board_and_a_selection_narrows_the_snapshot(tmp_path):
    model = a_model(FILTERED, settings_path=tmp_path / "settings.json")
    sections = model.picker_sections()
    assert [value for value, _ in sections["Type"]] == ["research", "prototype", "grilling", "task", "spec", "impl"]
    assert [value for value, _ in sections["Map"]] == ["9", "20"]
    model.filters["Type"]["research"] = "include"
    assert cells(model) == {9: {"CLAIMED": [2]}}
    model.filters["Type"].clear()
    model.filters["Map"]["20"] = "include"
    assert list(cells(model)) == [20]


def test_filters_are_saved_per_repo_and_cleared_together_with_the_needle(tmp_path):
    path = tmp_path / "settings.json"
    model = a_model(FILTERED, settings_path=path)
    model.filters["Author"]["bob"] = "include"
    model.filters["Map"]["9"] = "include"
    assert model.save_filters() is None
    saved = json.loads(path.read_text())["filters"]["fake:o/r"]
    assert saved["authors"] == ["bob"] and saved["maps"] == [9]
    assert a_model(FILTERED, settings_path=path).filters["Author"] == {"bob": "include"}
    model.needle = "store"
    model.clear_filters()
    assert model.needle == "" and not any(model.filters.values())
    assert json.loads(path.read_text())["filters"]["fake:o/r"]["authors"] == []


# --- settings ---------------------------------------------------------------------------------------


def test_a_save_from_one_view_keeps_keys_another_view_saved(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"other": True, "repos": {"fake:x/y": {"interval": 7}}}))
    one, two = a_model(settings_path=path), a_model(settings_path=path)
    one.filters["Author"]["alice"] = "include"
    one.save_filters()
    applied = two.apply_settings({"interval": 30.0, "fetch_limit": 300, "decided_age": 3})
    assert applied.interval and applied.board and applied.notice is None
    two.filters["Type"]["task"] = "include"
    two.save_filters()  # its own repo's filters, written over one's
    one.apply_settings({"interval": 60.0, "fetch_limit": 300, "decided_age": 3})
    saved = json.loads(path.read_text())
    assert saved["other"] is True and saved["repos"]["fake:x/y"] == {"interval": 7}
    assert saved["repos"]["fake:o/r"] == {"interval": 60, "decided_age": 3}
    assert saved["filters"]["fake:o/r"]["types"] == ["task"]


def test_settings_apply_at_once_and_an_empty_decided_age_removes_the_saved_one(tmp_path):
    path = tmp_path / "settings.json"
    model = a_model(settings_path=path)
    model.apply_settings({"interval": 5.0, "fetch_limit": 50, "decided_age": 2})
    assert model.interval == 5 and model.pacer.interval == 5 and model.board_query.limit == 50
    assert json.loads(path.read_text())["repos"]["fake:o/r"] == {"interval": 5, "fetch_limit": 50, "decided_age": 2}
    applied = model.apply_settings({"interval": 5.0, "fetch_limit": 50, "decided_age": None})
    assert not applied.interval and applied.board
    assert json.loads(path.read_text())["repos"]["fake:o/r"] == {"interval": 5, "fetch_limit": 50}


def test_a_failed_settings_save_is_a_warning_and_still_applies(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    model = a_model(settings_path=blocker / "settings.json")
    applied = model.apply_settings({"interval": 9.0, "fetch_limit": 300, "decided_age": None})
    assert model.interval == 9 and applied.notice.severity == "warning"
    assert applied.notice.text.startswith("settings not saved: ")


# --- fetching ---------------------------------------------------------------------------------------


def test_a_refresh_asked_for_mid_fetch_runs_once_the_fetch_lands():
    model = BoardModel(FakeProvider(TICKETS), Query(), interval=60, clock=lambda: 0.0)
    plan = model.due()
    assert plan.tickets and plan.viewer and model.due() is None
    assert model.refresh() is None
    applied = model.apply(model.fetch(plan))
    assert applied.changed and applied.next is not None and applied.next.tickets and not applied.next.viewer
    assert model.viewer == "me"
    assert not model.apply(model.fetch(applied.next)).changed


def test_a_tracker_error_queues_one_notice_per_episode():
    from ticketmaster.providers import ProviderError

    provider = FakeProvider(ProviderError("gh: not logged in"))
    model = BoardModel(provider, Query(), interval=60, clock=lambda: 0.0)
    model.apply(model.fetch(model.refresh()))
    assert [n.severity for n in model.drain_notices()] == ["error"] and model.drain_notices() == []
    model.apply(model.fetch(model.refresh()))
    assert model.drain_notices() == []
    assert any(s.style == "error" and "not logged in" in s.text for s in model.status())


# --- the start flow ---------------------------------------------------------------------------------

MAP_CARD = a_map(1, "Pick a stack")
MAP_BOARD = [MAP_CARD, on(1, 5, order=1), on(1, 6, order=2), on(1, 7, order=0, open_blockers=1),
             on(1, 8, is_open=False)]


def test_a_start_on_the_map_plans_its_first_frontier_ticket_and_the_confirm_names_it():
    model = a_model(MAP_BOARD, FakeBench(), describe_start=lambda t: f"prompt {t.ref}")
    plan = model.start_plan(MAP_CARD)
    assert plan == StartPlan(MAP_BOARD[1], " (next on map #1)")
    assert model.confirm(plan) == ("Start #5 Question 5 (next on map #1) in a new worktree?", "prompt #5", "Start")


def test_a_start_refuses_a_blocked_or_someone_elses_ticket_and_names_a_live_session():
    bench = FakeBench({1: [Session(1, "/w/issue-1", status="idle")]})
    tickets = [*TICKETS, on(9, 6, "Bob's", assignees=("bob",))]
    model = a_model(tickets, bench)
    assert model.start_plan(tickets[3]) == Refusal("#3 is blocked by #1")
    assert model.start_plan(tickets[5]) == Refusal("#6 is claimed by @bob")
    assert model.start_plan(tickets[1]) == Refusal("#1 has a live session", live=True)


def test_a_start_continues_an_agentless_worktree():
    model = a_model(bench=FakeBench({1: [Session(1, "/w/issue-1")]}))
    plan = model.start_plan(TICKETS[1])
    assert model.confirm(plan) == ("Continue #1 Ready one in its worktree issue-1?",
                                   "claude --continue in /w/issue-1", "Continue")


def test_primary_goes_to_the_most_urgent_session_else_hints_what_a_start_would_do():
    bench = FakeBench({2: [Session(2, "/w/issue-2", status="idle", handle="w"),
                           Session(2, "/w/issue-2-b", status="blocked", handle="w")]})
    model = a_model(bench=bench)
    found = model.primary(TICKETS[2])
    assert isinstance(found, GoTo) and found.session.status == "blocked"
    assert model.go_to(found) == ("ok", "information") and bench.calls == [("go_to", 2, "/w/issue-2-b")]
    hint = model.primary(TICKETS[1])
    assert isinstance(hint, Hint)
    assert model.hint_text(hint, "Start") == "#1 has no session here; Start starts /wayfinder on it"
    assert model.hint_text(model.primary(TICKETS[3]), "s") == "#3 has no session here; s refuses: #3 is blocked by #1"


class FailingBench(FakeBench):
    def start(self, ticket):
        super().start(ticket)
        raise BackendError("agent_start_failed", source="fake")


def test_a_begun_start_holds_the_ticket_until_it_fails():
    model = a_model(bench=FailingBench())
    model.begin_start(TICKETS[1])
    assert model.start_plan(TICKETS[1]) == Refusal("#1 has a live session", live=True)
    assert "starting" in [b.kind for b in model.snapshot().lanes[0].cells[1][1][0].badges]
    notice = model.start(TICKETS[1])
    assert notice == ("fake: agent_start_failed", "error")
    model.start_failed(TICKETS[1])
    assert isinstance(model.start_plan(TICKETS[1]), StartPlan)


# --- the starting marker ----------------------------------------------------------------------------


def test_the_starting_marker_is_seen_by_another_model_and_expires_after_30_seconds(tmp_path):
    wall, markers = Wall(), tmp_path / "ticketmaster" / "starting"
    one = a_model(bench=FakeBench(), wall=wall, starting_dir=markers)
    two = a_model(bench=FakeBench(), wall=wall, starting_dir=markers)
    one.begin_start(TICKETS[1])
    assert (markers / "1").exists() and 1 not in two.starting
    wall.now += timedelta(seconds=1)
    assert two.read_markers() and 1 in two.starting
    assert two.start_plan(TICKETS[1]) == Refusal("#1 is being started from another board")
    assert "starting" in [b.kind for b in two.snapshot().lanes[0].cells[1][1][0].badges]
    wall.now += timedelta(seconds=29)
    assert not two.read_markers() and 1 in two.starting  # 30 s old: still starting
    wall.now += timedelta(seconds=1)
    assert two.read_markers() and 1 not in two.starting
    assert isinstance(two.start_plan(TICKETS[1]), StartPlan)


def test_a_failed_start_or_a_live_agent_removes_the_marker(tmp_path):
    markers = tmp_path / "starting"
    bench = FakeBench()
    model = a_model(bench=bench, wall=Wall(), starting_dir=markers)
    model.begin_start(TICKETS[1])
    model.start_failed(TICKETS[1])
    assert not (markers / "1").exists()
    model.begin_start(TICKETS[1])
    bench.current = {1: [Session(1, "/w/issue-1", status="working")]}
    model.apply(model.fetch(model.refresh()))
    assert not (markers / "1").exists() and 1 not in model.starting


def test_a_start_confirmed_on_two_boards_at_once_begins_on_one(tmp_path):
    wall, markers = Wall(), tmp_path / "starting"
    one = a_model(bench=FakeBench(), wall=wall, starting_dir=markers)
    two = a_model(bench=FakeBench(), wall=wall, starting_dir=markers)
    assert isinstance(one.start_plan(TICKETS[1]), StartPlan) and isinstance(two.start_plan(TICKETS[1]), StartPlan)
    assert one.begin_start(TICKETS[1]) is None
    assert two.begin_start(TICKETS[1]) == Refusal("#1 is being started from another board")
    assert one.begin_start(TICKETS[1]) == Refusal("#1 has a live session", live=True)
    wall.now += timedelta(seconds=31)
    assert two.begin_start(TICKETS[1]) is None  # a stale marker is replaced


def test_a_settings_save_writes_only_what_it_changed(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"repos": {"fake:o/r": {"interval": 60, "decided_age": 7}}}))
    one, two = a_model(settings_path=path), a_model(settings_path=path)
    two.apply_settings({"interval": 60.0, "fetch_limit": 300, "decided_age": 3})
    one.apply_settings({"interval": 20.0, "fetch_limit": 300, "decided_age": 7})
    assert json.loads(path.read_text())["repos"]["fake:o/r"] == {"interval": 20, "decided_age": 3}
