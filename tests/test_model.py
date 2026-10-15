from datetime import datetime, timezone

from ticketmaster.model import (Column, Ticket, build_board, classify, frontier, is_out_of_scope, map_progress,
                                out_of_scope_refs, parse_blockers, part_of, summarize)

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def ticket(**kw):
    base = dict(number=1, title="t", url="u", is_open=True)
    return Ticket(**{**base, **kw})


def test_unassigned_unblocked_is_frontier():
    assert classify(ticket()) is Column.FRONTIER


def test_assigned_is_claimed():
    assert classify(ticket(assignees=("me",))) is Column.CLAIMED


def test_open_blocker_wins_over_assignee():
    assert classify(ticket(assignees=("me",), open_blockers=2)) is Column.BLOCKED


def test_a_blocked_label_no_longer_blocks():
    assert classify(ticket(labels=("Blocked",), open_blockers=None)) is Column.FRONTIER


def test_every_closed_ticket_is_decided_whatever_its_labels_or_age():
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for closed in (ticket(is_open=False, closed_at=old), ticket(is_open=False, closed_at=None),
                   ticket(is_open=False, labels=("wontfix",)), ticket(is_open=False, not_planned=True),
                   ticket(is_open=False, open_blockers=1, assignees=("bob",))):
        assert classify(closed) is Column.DECIDED


def test_review_labels_and_open_change_requests_change_nothing():
    assert classify(ticket(labels=("review:security",))) is Column.FRONTIER
    assert classify(ticket(has_open_change_request=True, assignees=("me",))) is Column.CLAIMED


def test_summary_prefers_wayfinder_question_section():
    body = "## Context\nignore me\n\n## Question\n\nShould we cache?\n- yes or no\n\n## Notes\nnope"
    assert summarize(body) == "Should we cache? yes or no"


def test_summary_strips_task_checkboxes():
    assert summarize("- [ ] first\n- [x] second") == "first second"


def test_summary_skips_headings_comments_and_truncates():
    body = "# Title\n<!-- hidden -->\n" + "word " * 100
    out = summarize(body, max_chars=20)
    assert len(out) == 20 and out.endswith("…") and "hidden" not in out


def test_parse_blockers_reads_the_body_line_and_the_section():
    assert parse_blockers("Some text\nBlocked by: #3, #12\n") == (3, 12)
    assert parse_blockers("**Blocked by:** #4") == (4,)
    assert parse_blockers("## What\n\nx\n\n## Blocked by\n\n- #30\n- #31\n\n## Notes\n- #99") == (30, 31)
    assert parse_blockers("Blocked by: #3\n\n## Blocked by\n\n- #3\n- #5") == (3, 5)
    assert parse_blockers("## Blocked by:\n\n- #6\n\n### Notes\n\n- #7") == (6,)


def test_parse_blockers_accepts_the_norwegian_line():
    assert parse_blockers("Blokkert av: #7 og #8") == (7, 8)


def test_parse_blockers_ignores_none_mentions_and_other_repos():
    assert parse_blockers("## Blocked by\n\nNone - can start immediately") == ()
    assert parse_blockers("Part of #1\nsee #2 and other/repo#3") == ()
    assert parse_blockers("Blocked by: other/repo#3, &#12;") == ()
    assert parse_blockers("") == ()


def test_start_refusal_names_blockers_and_other_claimant():
    assert ticket(blocked_by=("#3", "o/x#4"), open_blockers=2).start_refusal("me") == "#1 is blocked by #3, o/x#4"
    assert ticket(open_blockers=1).start_refusal("me") == "#1 is blocked by 1 open issue"
    assert ticket(labels=("Blocked",), map_number=9, map_url="u9").start_refusal("me") is None
    assert ticket(assignees=("bob", "eve")).start_refusal("me") == "#1 is claimed by @bob, @eve"


def test_start_refusal_lets_the_claimant_and_unknown_viewers_start():
    on_map = dict(map_number=9, map_url="u9")
    assert ticket(**on_map).start_refusal("me") is None
    assert ticket(assignees=("Me", "bob"), **on_map).start_refusal("me") is None
    assert ticket(assignees=("bob",), **on_map).start_refusal(None) is None


MAP = ("wayfinder:map",)


def test_only_the_four_known_wayfinder_types_count():
    for kind in ("research", "prototype", "grilling", "task"):
        assert ticket(labels=(f"wayfinder:{kind}",)).wayfinder_type == kind
    assert ticket(labels=("wayfinder:epic",)).wayfinder_type is None
    assert ticket(labels=("wayfinder:",)).wayfinder_type is None
    assert ticket(labels=MAP).wayfinder_type is None


def test_gitlab_scoped_wayfinder_labels_are_accepted():
    assert ticket(labels=("wayfinder::grilling",)).wayfinder_type == "grilling"
    assert ticket(labels=("wayfinder::map",)).is_wayfinder_map
    t = ticket(labels=("wayfinder::task",), parent_url="https://gl/1", parent_labels=("wayfinder::map",))
    assert t.is_wayfinder_ticket


def test_typed_ticket_without_a_map_parent_has_its_map_missing():
    assert ticket(labels=("wayfinder:research",)).map_missing
    assert ticket(labels=("wayfinder:task",), parent_url="https://gh/9", parent_labels=("epic",)).map_missing
    resolved = ticket(labels=("wayfinder:task",), parent_url="https://gh/9", parent_labels=MAP)
    assert resolved.is_wayfinder_ticket and not resolved.map_missing
    assert not ticket(labels=("wayfinder:epic",)).map_missing and not ticket().map_missing


def test_part_of_reads_the_first_line_of_the_body():
    assert part_of("Part of #12\n\nMore") == 12
    assert part_of("\n  Part of #7 \r\nrest") == 7
    assert part_of("Intro\nPart of #12") is None
    assert part_of("Part of #x") is None and part_of("") is None
    assert part_of("Part of #12, and of #13") is None


def test_wayfinder_label_prefix_is_case_insensitive():
    assert ticket(labels=("Wayfinder:Task",)).wayfinder_type == "task"


def test_grilling_and_prototype_are_hitl_research_afk():
    assert ticket(labels=("wayfinder:grilling",)).mode == "HITL"
    assert ticket(labels=("wayfinder::prototype",)).mode == "HITL"
    assert ticket(labels=("wayfinder:research",)).mode == "AFK"
    assert ticket(labels=MAP).mode is None and ticket().mode is None


def test_a_task_is_hitl_or_afk_by_the_first_of_those_words_in_its_body():
    task = ("wayfinder:task",)
    assert ticket(labels=task, body="## Question\nProvision access. AFK: the agent can do it.").mode == "AFK"
    assert ticket(labels=task, body="HITL, then AFK").mode == "HITL"
    assert ticket(labels=task, body="I'll be afk; hitl later").mode is None
    assert ticket(labels=task, body="Sign up for the service").mode is None
    assert ticket(labels=task, body="the HITLER and AFKs").mode is None


def test_start_refusal_names_a_ticket_that_is_not_wayfinder_work_last():
    assert ticket().start_refusal("me") == "#1 is not wayfinder work: the board only starts work on a map"
    assert ticket(open_blockers=1).start_refusal("me") == "#1 is blocked by 1 open issue"
    assert ticket(labels=("wayfinder:map",)).start_refusal("me") is None
    assert ticket(map_number=9, map_url="u9").start_refusal("me") is None
    assert ticket(labels=("wayfinder:task",)).start_refusal("me") is None  # map not found: warned elsewhere


def test_triage_labels_never_hold_a_start():
    for label in ("needs-info", "ready-for-human"):
        assert ticket(labels=(label,), map_number=9, map_url="u9").start_refusal("me") is None


def child(number, **kw):
    """A typed ticket of map #1."""
    base = dict(number=number, labels=("wayfinder:grilling",), parent_number=1, parent_url="u1", parent_labels=MAP)
    return ticket(**{**base, **kw})


MAP_TICKET = ticket(number=1, labels=MAP)


def test_frontier_is_the_maps_open_unblocked_unclaimed_tickets_in_map_order():
    tickets = [
        MAP_TICKET,
        child(6, order=2),
        child(2, is_open=False),
        child(3, open_blockers=1, blocked_by=("#2",)),
        child(4, assignees=("bob",)),
        child(5, order=1),
        child(7, labels=("wayfinder:grilling", "blocked")),  # a label does not block
        ticket(number=8, parent_number=1, parent_url="u1", parent_labels=MAP),  # the spec: no wayfinder type
        child(9, parent_number=10, parent_url="u10"),  # another map's
        child(11, order=1),
    ]
    assert [t.number for t in frontier(MAP_TICKET, tickets)] == [7, 5, 11, 6]


def test_frontier_skips_tickets_already_being_started():
    tickets = [MAP_TICKET, child(5, order=1), child(6, order=2)]
    assert [t.number for t in frontier(MAP_TICKET, tickets, taken={5})] == [6]
    assert frontier(MAP_TICKET, tickets, taken={5, 6}) == []


def test_frontier_is_empty_without_open_children():
    assert frontier(MAP_TICKET, [MAP_TICKET, child(2, is_open=False)]) == []


def test_map_progress_counts_decided_open_and_blocked_tickets():
    map_ticket = ticket(number=1, labels=MAP, body="## Out of scope\n\n- #7 ruled out\n")
    tickets = [map_ticket, child(2), child(3, open_blockers=1), child(4, assignees=("bob",)), child(5, is_open=False),
               child(6, is_open=False, not_planned=True), child(7, is_open=False), child(8, is_open=False)]
    assert map_progress(map_ticket, tickets) == (2, 3, 1)  # #6 canceled and #7 ruled out are no decisions


def test_out_of_scope_refs_are_the_issues_the_maps_out_of_scope_section_links():
    body = ("## Decisions so far\n- #2\n\n## Out of scope\n\n- Billing (#7)\n- [SSO](https://gh/o/r/issues/8)\n"
            "- other/repo#9 and https://gh/x/y/issues/10\n\n## Fog\n- #11\n")
    assert out_of_scope_refs(ticket(number=1, url="https://gh/o/r/issues/1", body=body)) == {7, 8}
    gitlab = ticket(url="https://gl/g/p/-/issues/1", body="### Out-of-scope:\n* https://gl/g/p/-/issues/4\n")
    assert out_of_scope_refs(gitlab) == {4}
    assert out_of_scope_refs(ticket(body="")) == set()


def test_a_closed_ticket_is_out_of_scope_when_canceled_or_linked_from_the_maps_section():
    map_ticket = ticket(number=1, url="https://gh/o/r/issues/1", labels=MAP, body="## Out of scope\n- #3\n")
    assert is_out_of_scope(child(2, is_open=False, not_planned=True), map_ticket)
    assert is_out_of_scope(child(3, is_open=False), map_ticket)
    assert not is_out_of_scope(child(4, is_open=False), map_ticket)
    assert not is_out_of_scope(child(3), map_ticket)  # still open: not ruled out yet
    assert not is_out_of_scope(child(5, is_open=False, labels=("wayfinder:grilling", "out-of-scope")), map_ticket)


def lanes(board):
    return {lane.map.number: {c: [t.number for t in ts] for c, ts in lane.columns.items() if ts} for lane in board.lanes}


def test_each_open_map_is_a_lane_of_its_tickets_in_the_four_columns():
    other = ticket(number=20, labels=MAP)
    tickets = [
        other, MAP_TICKET,
        child(2), child(3, open_blockers=1, assignees=("bob",)), child(4, assignees=("bob",)),
        child(5, is_open=False), child(21, parent_number=20, parent_url="u20"),
        ticket(number=30),  # not wayfinder work
        ticket(number=31, map_number=1, map_url="u1"),  # an implementation ticket of the map's spec
        ticket(number=32, labels=("wayfinder:task",)),  # its map not found
        ticket(number=33, body="Part of #20", map_number=20, map_url="u20", assignees=("bob",)),  # map #20's spec
    ]
    board = build_board(tickets)
    assert [lane.map.number for lane in board.lanes] == [1, 20]
    assert lanes(board) == {
        1: {Column.BLOCKED: [3], Column.FRONTIER: [2, 31], Column.CLAIMED: [4], Column.DECIDED: [5]},
        20: {Column.FRONTIER: [21], Column.CLAIMED: [33]},
    }
    assert board.ticket_count() == 7
    assert map_progress(MAP_TICKET, tickets) == (1, 3, 1)  # the map's decisions only


def test_spec_kind_tells_a_maps_spec_from_its_implementation_tickets():
    assert ticket(body="Part of #1\n\n## Problem", map_number=1, map_url="u1").spec_kind == "spec"
    assert ticket(body="## Parent\n\n#7", map_number=1, map_url="u1").spec_kind == "impl"
    assert ticket(body="Part of #1", map_number=1, map_url="u1", labels=("wayfinder:task",)).spec_kind is None
    assert ticket(body="Part of #1").spec_kind is None  # no map resolved
    assert child(2).spec_kind is None and MAP_TICKET.spec_kind is None


def test_lane_columns_sort_by_sub_issue_order_then_number_and_decided_keeps_all():
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    tickets = [MAP_TICKET, child(5, order=1), child(9, order=0), child(2, order=1),
               child(3, is_open=False, order=2, closed_at=old), child(4, is_open=False, order=1, closed_at=NOW)]
    lane = build_board(tickets).lanes[0]
    assert [t.number for t in lane.columns[Column.FRONTIER]] == [9, 2, 5]
    assert [t.number for t in lane.columns[Column.DECIDED]] == [4, 3]
    assert lane.columns[Column.FRONTIER] == frontier(MAP_TICKET, tickets)


def test_a_closed_map_shows_only_when_named_and_then_as_its_header_alone():
    closed_map = ticket(number=1, labels=MAP, is_open=False)
    tickets = [closed_map, child(2), child(3, is_open=False)]
    assert build_board(tickets).lanes == []
    lane = build_board(tickets, named_map=1).lanes[0]
    assert lane.map is closed_map and lane.collapsed
    assert all(not cards for cards in lane.columns.values())
    assert lane.progress == (1, 1, 0)
    assert build_board(tickets, named_map=1).ticket_count() == 0


def test_lanes_mark_out_of_scope_tickets():
    map_ticket = ticket(number=1, labels=MAP, body="## Out of scope\n- #3\n")
    lane = build_board([map_ticket, child(2, is_open=False, not_planned=True), child(3, is_open=False),
                        child(4, is_open=False)]).lanes[0]
    assert lane.out_of_scope == {2, 3}


def test_frontier_follows_the_trackers_map_order_when_given():
    tickets = [MAP_TICKET, child(5, order=1), child(6, order=2), child(7, order=3)]
    assert [t.number for t in frontier(MAP_TICKET, tickets, order=[7, 5])] == [7, 5, 6]


def test_a_ticket_with_a_map_has_no_map_missing():
    t = Ticket(number=4, title="Slice", url="u", is_open=True, parent_number=3, map_number=1, map_url="https://gh/1")
    assert not t.map_missing


def spec_or_impl(kind, **kw):
    return ticket(map_number=9, map_url="u9", body="Part of #9" if kind == "spec" else "## Parent\n\n#8", **kw)


def test_start_refusal_refuses_a_closed_ticket_and_a_spec():
    assert ticket(is_open=False, labels=("wayfinder:map",)).start_refusal("me") == "#1 is closed"
    assert spec_or_impl("spec").start_refusal("me") == "#1 is a spec: s starts /implement on its implementation tickets"


def test_start_refusal_refuses_an_implementation_ticket_with_an_open_closing_pr():
    assert spec_or_impl("impl", has_open_change_request=True).start_refusal("me") == \
        "#1 has an open PR that closes it"
    assert spec_or_impl("impl").start_refusal("me") is None
    assert spec_or_impl("impl", assignees=("me",)).start_refusal("me") is None


def test_a_ticket_is_named_by_its_tracker_key_else_its_number():
    assert ticket().ref == "#1" and ticket(key="TM-1").ref == "TM-1"
