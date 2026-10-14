from pathlib import Path

import pytest

from ticketmaster import start
from ticketmaster.model import Ticket
from ticketmaster.start import MapNotFound, NotWayfinderWork, build_prompt, claude_args, workbench_for

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


def test_ticket_with_a_map_runs_wayfinder_on_that_map():
    # The spec (its parent is the map) and a spec's implementation ticket (its parent is the spec).
    spec = ticket(parent_number=680, parent_url="https://gh/680", parent_labels=MAP,
                  map_number=680, map_url="https://gh/680")
    slice_ = ticket(parent_number=690, parent_url="https://gh/690", map_number=680, map_url="https://gh/680")
    for t in (spec, slice_):
        assert build_prompt(t) == "/mattpocock-skills:wayfinder https://gh/680 https://gh/686"
    assert build_prompt(slice_, "/wayfinder") == "/wayfinder https://gh/680 https://gh/686"


@pytest.mark.parametrize("t", [ticket(), ticket(parent_number=690, parent_url="https://gh/690"),
                               ticket(labels=("wayfinder:epic",))])
def test_a_ticket_without_a_map_is_not_wayfinder_work_and_never_starts(t):
    with pytest.raises(NotWayfinderWork, match="#686"):
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


def test_workbench_reads_the_wayfinder_command_from_config():
    assert workbench_for(Path("/r"), {}).wayfinder_command == "/mattpocock-skills:wayfinder"
    assert workbench_for(Path("/r"), {"wayfinder_command": "/wayfinder"}).wayfinder_command == "/wayfinder"


def test_only_wayfinder_starts_remain():
    assert not hasattr(start, "review_prompt")
    assert not any(hasattr(start.Workbench, action) for action in ("review", "remove", "auto_remove"))
