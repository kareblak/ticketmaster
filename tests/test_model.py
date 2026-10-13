from datetime import datetime, timedelta, timezone

from ticketmaster.model import (Column, Ticket, build_board, classify, decisions_so_far, frontier, map_progress,
                                parse_blockers, part_of, summarize)

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def ticket(**kw):
    base = dict(number=1, title="t", url="u", is_open=True)
    return Ticket(**{**base, **kw})


def test_unassigned_unblocked_is_ready():
    assert classify(ticket(), NOW, 7) is Column.READY


def test_assigned_is_in_progress():
    assert classify(ticket(assignees=("me",)), NOW, 7) is Column.IN_PROGRESS


def test_open_blocker_wins_over_assignee():
    assert classify(ticket(assignees=("me",), open_blockers=2), NOW, 7) is Column.BLOCKED


def test_blocked_label_counts_when_provider_has_no_dependencies():
    assert classify(ticket(labels=("Blocked",), open_blockers=None), NOW, 7) is Column.BLOCKED


def test_review_labels_become_tags_in_one_review_column():
    t = ticket(labels=("review:security", "review::ux", "bug"), open_blockers=1)
    assert classify(t, NOW, 7) is Column.REVIEW
    assert t.review_tags == ["security", "ux"]
    assert t.other_labels == ["bug"]


def test_open_change_request_means_review():
    assert classify(ticket(has_open_change_request=True), NOW, 7) is Column.REVIEW


def test_recently_closed_is_done_and_old_closed_hidden():
    assert classify(ticket(is_open=False, closed_at=NOW - timedelta(days=2)), NOW, 7) is Column.DONE
    assert classify(ticket(is_open=False, closed_at=NOW - timedelta(days=8)), NOW, 7) is None
    assert classify(ticket(is_open=False, closed_at=None), NOW, 7) is None


def test_board_sorts_by_order_then_number_and_done_newest_first():
    tickets = [
        ticket(number=5, order=1),
        ticket(number=9, order=0),
        ticket(number=2, order=1),
        ticket(number=3, is_open=False, closed_at=NOW - timedelta(days=3)),
        ticket(number=4, is_open=False, closed_at=NOW - timedelta(days=1)),
    ]
    board = build_board(tickets, now=NOW)
    assert [t.number for t in board.columns[Column.READY]] == [9, 2, 5]
    assert [t.number for t in board.columns[Column.DONE]] == [4, 3]


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


def test_start_refusal_names_blockers_label_and_other_claimant():
    assert ticket(blocked_by=("#3", "o/x#4"), open_blockers=2).start_refusal("me") == "#1 is blocked by #3, o/x#4"
    assert ticket(open_blockers=1).start_refusal("me") == "#1 is blocked by 1 open issue"
    assert ticket(labels=("Blocked",)).start_refusal("me") == "#1 has the 'blocked' label"
    assert ticket(assignees=("bob", "eve")).start_refusal("me") == "#1 is claimed by @bob, @eve"


def test_start_refusal_lets_the_claimant_and_unknown_viewers_start():
    assert ticket().start_refusal("me") is None
    assert ticket(assignees=("Me", "bob")).start_refusal("me") is None
    assert ticket(assignees=("bob",)).start_refusal(None) is None


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


def child(number, **kw):
    """A typed ticket of map #1."""
    base = dict(number=number, labels=("wayfinder:grilling",), parent_number=1, parent_url="u1", parent_labels=MAP)
    return ticket(**{**base, **kw})


MAP_TICKET = ticket(number=1, labels=MAP)


def test_an_open_map_shows_as_in_progress_not_as_a_ready_ticket():
    assert classify(MAP_TICKET, NOW, 7) is Column.IN_PROGRESS
    assert classify(ticket(labels=("wayfinder::map",)), NOW, 7) is Column.IN_PROGRESS
    assert classify(ticket(labels=MAP + ("review:ux",)), NOW, 7) is Column.REVIEW


def test_frontier_is_the_maps_open_unblocked_unclaimed_tickets_in_map_order():
    tickets = [
        MAP_TICKET,
        child(6, order=2),
        child(2, is_open=False),
        child(3, open_blockers=1, blocked_by=("#2",)),
        child(4, assignees=("bob",)),
        child(5, order=1),
        child(7, labels=("blocked",)),
        ticket(number=8, parent_number=1, parent_url="u1", parent_labels=MAP),  # the spec: no wayfinder type
        child(9, parent_number=10, parent_url="u10"),  # another map's
        child(11, order=1),
    ]
    assert [t.number for t in frontier(MAP_TICKET, tickets)] == [5, 11, 6]


def test_frontier_skips_tickets_already_being_started():
    tickets = [MAP_TICKET, child(5, order=1), child(6, order=2)]
    assert [t.number for t in frontier(MAP_TICKET, tickets, taken={5})] == [6]
    assert frontier(MAP_TICKET, tickets, taken={5, 6}) == []


def test_frontier_is_empty_without_open_children():
    assert frontier(MAP_TICKET, [MAP_TICKET, child(2, is_open=False)]) == []


def test_decisions_so_far_counts_the_maps_decision_bullets():
    body = ("## Notes\n- a note\n\n## Decisions so far\n\n- Store: Postgres ([#3](u3))\n* Auth: magic links #4\n"
            "1. Cache: none\n  - nested detail\n\n## Fog\n- unknown\n")
    assert decisions_so_far(body) == 3
    assert decisions_so_far("## Decisions-so-far\n_none yet_\n") == 0
    assert decisions_so_far("") == 0


def test_map_progress_counts_decisions_open_and_blocked_tickets():
    map_ticket = ticket(number=1, labels=MAP, body="## Decisions so far\n- one\n- two\n")
    tickets = [map_ticket, child(2), child(3, open_blockers=1), child(4, assignees=("bob",)), child(5, is_open=False)]
    assert map_progress(map_ticket, tickets) == (2, 3, 1)


def test_frontier_follows_the_trackers_map_order_when_given():
    tickets = [MAP_TICKET, child(5, order=1), child(6, order=2), child(7, order=3)]
    assert [t.number for t in frontier(MAP_TICKET, tickets, order=[7, 5])] == [7, 5, 6]
