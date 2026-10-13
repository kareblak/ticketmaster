from pathlib import Path

from ticketmaster.model import Ticket
from ticketmaster.start import build_prompt, claude_args, review_prompt, workbench_for

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


def test_review_prompt_runs_code_review_from_the_pr_base_against_the_issue():
    prompt = review_prompt(ticket())
    assert prompt.startswith("/mattpocock-skills:code-review Review the open pull request for issue #686")
    assert "check out its branch" in prompt and "base branch (e.g. origin/main) as the fixed point" in prompt
    assert "https://gh/686 as the spec" in prompt
    assert "do not push or comment on the PR without asking" in prompt


def test_custom_review_command():
    assert review_prompt(ticket(), "/code-review").startswith("/code-review Review the open pull request")


def test_workbench_reads_commands_from_config():
    default = workbench_for(Path("/r"), {})
    assert (default.wayfinder_command, default.review_command) == (
        "/mattpocock-skills:wayfinder", "/mattpocock-skills:code-review")
    configured = workbench_for(Path("/r"), {"review_command": "/code-review", "wayfinder_command": "/wayfinder"})
    assert (configured.wayfinder_command, configured.review_command) == ("/wayfinder", "/code-review")
