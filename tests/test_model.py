from datetime import datetime, timedelta, timezone

from ticketmaster.model import Column, Ticket, build_board, classify, parse_blockers, summarize

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
