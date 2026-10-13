from ticketmaster.model import Ticket
from ticketmaster.start import build_prompt, claude_args, review_prompt

MAP = ("wayfinder:map",)


def ticket(**kw):
    base = dict(number=686, title="Attach a  deviation reason", url="https://gh/686", is_open=True)
    return Ticket(**{**base, **kw})


def test_wayfinder_ticket_runs_wayfinder_on_its_map():
    t = ticket(labels=("wayfinder:grilling",), parent_url="https://gh/680", parent_labels=MAP)
    assert build_prompt(t) == "/mattpocock-skills:wayfinder https://gh/680 https://gh/686"


def test_map_itself_runs_wayfinder_on_the_map():
    assert build_prompt(ticket(labels=MAP)) == "/mattpocock-skills:wayfinder https://gh/686"


def test_wayfinder_label_without_map_parent_gets_plain_prompt():
    t = ticket(labels=("wayfinder:task",), parent_url="https://gh/680", parent_labels=("epic",))
    prompt = build_prompt(t)
    assert prompt.startswith("Work on issue #686: Attach a  deviation reason\nhttps://gh/686")
    assert "assign it to yourself" in prompt


def test_custom_wayfinder_command():
    assert build_prompt(ticket(labels=MAP), "/wayfinder") == "/wayfinder https://gh/686"


def test_claude_args_name_the_session_and_never_use_claude_w():
    args = claude_args(ticket(), build_prompt(ticket()))
    assert args[:2] == ["-n", "#686 Attach a deviation reason"] and "-w" not in args
    assert args[2].startswith("Work on issue #686")


def test_claude_args_are_single_line_because_herdr_rejects_newlines():
    args = claude_args(ticket(), build_prompt(ticket())) + claude_args(ticket(), review_prompt(ticket()))
    assert not any("\n" in arg for arg in args)
    assert claude_args(ticket(), build_prompt(ticket()))[2] == (
        "Work on issue #686: Attach a  deviation reason https://gh/686 "
        "Read the issue and its comments first, then assign it to yourself before doing any work."
    )


def test_review_prompt_points_at_the_issue_and_forbids_pushing():
    prompt = review_prompt(ticket())
    assert "https://gh/686" in prompt and "do not push" in prompt
