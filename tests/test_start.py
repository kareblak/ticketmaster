import shlex
from pathlib import Path

from ticketmaster.model import Ticket
from ticketmaster.start import StartOptions, build_prompt, session_command

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


def test_session_command_uses_worktree_and_name():
    args = shlex.split(session_command(ticket(), StartOptions(cwd=Path("."))))
    assert args[:5] == ["claude", "-w", "issue-686", "-n", "#686 Attach a deviation reason"]
    assert args[5].startswith("Work on issue #686")


def test_session_command_without_worktree():
    args = shlex.split(session_command(ticket(), StartOptions(cwd=Path("."), worktree=False)))
    assert "-w" not in args
