from datetime import datetime, timedelta, timezone

from ticketmaster.model import Column, Ticket, build_board, classify, part_of, summarize

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
