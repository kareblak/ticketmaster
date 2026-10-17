from pathlib import Path

import pytest

from fake_backend import FakeBackend
from ticketmaster import start
from ticketmaster.config import load_config
from ticketmaster.model import Ticket
from ticketmaster.start import MapNotFound, NotBoardWork, build_prompt, claude_args, workbench_for

MAP = ("wayfinder:map",)


def ticket(**kw):
    base = dict(number=686, title="Attach a  deviation reason", url="https://gh/686", is_open=True)
    return Ticket(**{**base, **kw})


def test_wayfinder_ticket_runs_wayfinder_on_its_map():
    t = ticket(labels=("wayfinder:grilling",), parent_url="https://gh/680", parent_labels=MAP)
    assert build_prompt(t) == "/mattpocock-skills:wayfinder https://gh/680 https://gh/686"


def test_map_itself_runs_wayfinder_on_the_map():
    assert build_prompt(ticket(labels=MAP)) == "/mattpocock-skills:wayfinder https://gh/686"


def test_typed_ticket_without_a_map_is_never_started():
    for t in (ticket(labels=("wayfinder:task",), parent_url="https://gh/680", parent_labels=("epic",)),
              ticket(labels=("wayfinder::research",))):
        with pytest.raises(MapNotFound, match="#686"):
            build_prompt(t)


SPEC = ticket(parent_number=680, parent_url="https://gh/680", parent_labels=MAP, body="Part of #680",
              map_number=680, map_url="https://gh/680")
SLICE = ticket(parent_number=690, parent_url="https://gh/690", map_number=680, map_url="https://gh/680")


def test_a_spec_runs_to_tickets_on_the_spec():
    assert build_prompt(SPEC) == "/mattpocock-skills:to-tickets https://gh/686"
    assert build_prompt(SPEC, "/wayfinder", "/implement", "/to-tickets") == "/to-tickets https://gh/686"


def test_an_implementation_ticket_runs_implement_on_the_ticket_alone():
    assert build_prompt(SLICE) == "/mattpocock-skills:implement https://gh/686"
    assert build_prompt(SLICE, "/wayfinder", "/implement") == "/implement https://gh/686"


STANDALONE = ticket(body="## Problem Statement\n\nx\n\n## Solution\n\ny")
STANDALONE_TICKET = ticket(body="## Parent\n\n#680\n\n## What to build\n\nx\n\n## Acceptance criteria\n\n- [ ] y",
                           parent_number=680, lane_spec=680)


def test_a_standalone_spec_runs_to_tickets_and_its_ticket_implement():
    assert build_prompt(STANDALONE) == "/mattpocock-skills:to-tickets https://gh/686"
    assert build_prompt(STANDALONE_TICKET) == "/mattpocock-skills:implement https://gh/686"


def test_linear_tickets_are_named_by_their_key_and_branch_on_gitbranchname():
    linear = ticket(key="TM-26", branch_name="kb/tm-26-s-starts-implement")
    assert (start.worktree_name(linear), start.branch_name(linear)) == ("tm-26", "kb/tm-26-s-starts-implement")
    assert start.session_label(linear) == "TM-26 Attach a deviation reason"
    assert (start.worktree_name(ticket()), start.branch_name(ticket())) == ("issue-686", "issue-686")
    assert start.session_label(ticket()) == "#686 Attach a deviation reason"


@pytest.mark.parametrize("t", [ticket(), ticket(parent_number=690, parent_url="https://gh/690"),
                               ticket(labels=("wayfinder:epic",))])
def test_a_ticket_without_a_map_or_standalone_spec_is_not_board_work_and_never_starts(t):
    with pytest.raises(NotBoardWork, match="#686"):
        build_prompt(t)


def test_scoped_gitlab_ticket_runs_wayfinder_on_its_map():
    t = ticket(labels=("wayfinder::grilling",), parent_url="https://gl/680", parent_labels=("wayfinder::map",))
    assert build_prompt(t) == "/mattpocock-skills:wayfinder https://gl/680 https://gh/686"


def test_custom_wayfinder_command():
    assert build_prompt(ticket(labels=MAP), "/wayfinder") == "/wayfinder https://gh/686"


def test_claude_args_name_the_session_and_never_use_claude_w():
    args = claude_args(ticket(), build_prompt(ticket(labels=MAP)))
    assert args == ["-n", "#686 Attach a deviation reason", "/mattpocock-skills:wayfinder https://gh/686"]


def test_claude_args_are_single_line_because_herdr_rejects_newlines():
    assert claude_args(ticket(), "one\n\n  two\n")[2] == "one two"


def test_workbench_reads_the_implement_command_from_ticketmaster_toml(tmp_path):
    (tmp_path / ".git").mkdir()
    assert workbench_for(tmp_path, load_config(tmp_path), FakeBackend()).implement_command == "/mattpocock-skills:implement"
    (tmp_path / ".ticketmaster.toml").write_text('implement_command = "/implement"\n')
    assert workbench_for(tmp_path, load_config(tmp_path), FakeBackend()).implement_command == "/implement"


def test_workbench_reads_the_wayfinder_command_from_config():
    assert workbench_for(Path("/r"), {}, FakeBackend()).wayfinder_command == "/mattpocock-skills:wayfinder"
    assert workbench_for(Path("/r"), {"wayfinder_command": "/wayfinder"}, FakeBackend()).wayfinder_command == "/wayfinder"


def test_only_wayfinder_starts_remain():
    assert not hasattr(start, "review_prompt")
    assert not any(hasattr(start.Workbench, action) for action in ("review", "remove", "auto_remove"))


def test_workbench_reads_the_to_tickets_command_from_ticketmaster_toml(tmp_path):
    (tmp_path / ".git").mkdir()
    assert workbench_for(tmp_path, load_config(tmp_path), FakeBackend()).to_tickets_command == "/mattpocock-skills:to-tickets"
    (tmp_path / ".ticketmaster.toml").write_text('to_tickets_command = "/to-tickets"\n')
    assert workbench_for(tmp_path, load_config(tmp_path), FakeBackend()).to_tickets_command == "/to-tickets"
