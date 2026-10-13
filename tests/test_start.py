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
    assert build_prompt(t).startswith("/mattpocock-skills:implement https://gh/686\n")


def test_plain_ticket_runs_implement_with_claim_implement_and_pr_brief():
    prompt = build_prompt(ticket())
    assert prompt.startswith("/mattpocock-skills:implement https://gh/686\n")
    assert "assign it to yourself before doing any work" in prompt
    assert "/tdd" in prompt and "/code-review" in prompt
    assert "ready PR that closes the issue" in prompt and "pr skill" in prompt


def test_custom_implement_command():
    assert build_prompt(ticket(), implement_command="/implement").startswith("/implement https://gh/686\n")


def test_custom_implement_command_leaves_wayfinder_prompts_alone():
    assert build_prompt(ticket(labels=MAP), implement_command="/implement") == (
        "/mattpocock-skills:wayfinder https://gh/686"
    )


def test_custom_wayfinder_command():
    assert build_prompt(ticket(labels=MAP), "/wayfinder") == "/wayfinder https://gh/686"


def test_claude_args_name_the_session_and_never_use_claude_w():
    args = claude_args(ticket(), build_prompt(ticket()))
    assert args[:2] == ["-n", "#686 Attach a deviation reason"] and "-w" not in args
    assert args[2].startswith("/mattpocock-skills:implement https://gh/686 ")


def test_claude_args_are_single_line_because_herdr_rejects_newlines():
    args = claude_args(ticket(), build_prompt(ticket())) + claude_args(ticket(), review_prompt(ticket()))
    assert not any("\n" in arg for arg in args)
    assert claude_args(ticket(), build_prompt(ticket()))[2] == (
        "/mattpocock-skills:implement https://gh/686 "
        "1. Read the issue and its comments, then assign it to yourself before doing any work. "
        "2. Implement it via the skill (it drives /tdd and /code-review and commits). "
        "3. Push the branch and open a ready PR that closes the issue, with the body written via the pr skill."
    )


def test_review_prompt_points_at_the_issue_and_forbids_pushing():
    prompt = review_prompt(ticket())
    assert "https://gh/686" in prompt and "do not push" in prompt
