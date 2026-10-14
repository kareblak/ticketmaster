from pathlib import Path

import pytest

from ticketmaster.model import Ticket
from ticketmaster.start import MapNotFound, build_prompt, claude_args, review_prompt, workbench_for

MAP = ("wayfinder:map",)


def ticket(**kw):
    base = dict(number=686, title="Attach a  deviation reason", url="https://gh/686", is_open=True)
    return Ticket(**{**base, **kw})


def test_wayfinder_ticket_runs_wayfinder_on_its_map():
    t = ticket(labels=("wayfinder:grilling",), parent_url="https://gh/680", parent_labels=MAP)
    assert build_prompt(t) == "/mattpocock-skills:wayfinder https://gh/680 https://gh/686"


def test_map_itself_runs_wayfinder_on_the_map():
    assert build_prompt(ticket(labels=MAP)) == "/mattpocock-skills:wayfinder https://gh/686"


def test_typed_ticket_without_a_map_is_never_started_with_implement():
    for t in (ticket(labels=("wayfinder:task",), parent_url="https://gh/680", parent_labels=("epic",)),
              ticket(labels=("wayfinder::research",))):
        with pytest.raises(MapNotFound, match="#686"):
            build_prompt(t)


def test_ticket_with_a_map_runs_wayfinder_on_that_map_not_implement():
    # The spec (its parent is the map) and a spec's implementation ticket (its parent is the spec).
    spec = ticket(parent_number=680, parent_url="https://gh/680", parent_labels=MAP,
                  map_number=680, map_url="https://gh/680")
    slice_ = ticket(parent_number=690, parent_url="https://gh/690", map_number=680, map_url="https://gh/680")
    for t in (spec, slice_):
        assert build_prompt(t) == "/mattpocock-skills:wayfinder https://gh/680 https://gh/686"
    assert build_prompt(slice_, "/wayfinder", "/implement") == "/wayfinder https://gh/680 https://gh/686"


def test_ticket_whose_parent_is_no_map_still_runs_implement():
    t = ticket(parent_number=690, parent_url="https://gh/690")
    assert build_prompt(t).startswith("/mattpocock-skills:implement https://gh/686\n")


def test_unknown_wayfinder_label_gets_plain_prompt():
    t = ticket(labels=("wayfinder:epic",))
    assert build_prompt(t).startswith("/mattpocock-skills:implement https://gh/686\n")


def test_scoped_gitlab_ticket_runs_wayfinder_on_its_map():
    t = ticket(labels=("wayfinder::grilling",), parent_url="https://gl/680", parent_labels=("wayfinder::map",))
    assert build_prompt(t) == "/mattpocock-skills:wayfinder https://gl/680 https://gh/686"


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
    assert default.implement_command == "/mattpocock-skills:implement"
    assert (default.wayfinder_command, default.review_command) == (
        "/mattpocock-skills:wayfinder", "/mattpocock-skills:code-review")
    configured = workbench_for(Path("/r"), {"review_command": "/code-review", "wayfinder_command": "/wayfinder",
                                            "implement_command": "/implement"})
    assert configured.implement_command == "/implement"
    assert (configured.wayfinder_command, configured.review_command) == ("/wayfinder", "/code-review")
