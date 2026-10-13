import json
import subprocess
from pathlib import Path

import pytest

from ticketmaster import herdr
from ticketmaster.herdr import Herdr, HerdrError
from ticketmaster.launch import Placement
from ticketmaster.model import Ticket
from ticketmaster.start import StartOptions, start_in_herdr

REPO = Path("/code/shiplog")


class FakeHerdr:
    """Answers `herdr` calls from a {(group, verb): result | callable} table and records argv."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(argv[1:])
        answer = self.answers.get(tuple(argv[1:3]), {})
        if callable(answer):
            answer = answer(argv[1:])
        if isinstance(answer, HerdrError):
            err = json.dumps({"error": {"code": str(answer), "message": "x"}})
            return subprocess.CompletedProcess(argv, 1, "", err)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": answer}), "")

    def verbs(self):
        return [tuple(c[:2]) for c in self.calls]


def worktrees(*entries):
    return {"worktrees": [{"path": str(REPO), "branch": "main", "is_linked_worktree": False, "open_workspace_id": "w1"}, *entries]}


def ticket():
    return Ticket(number=686, title="Attach a  deviation reason", url="https://gh/686", is_open=True)


def test_available_needs_env_and_binary(monkeypatch):
    monkeypatch.setattr(herdr.shutil, "which", lambda name: "/opt/homebrew/bin/herdr")
    assert herdr.available({"HERDR_ENV": "1"})
    assert not herdr.available({})
    monkeypatch.setattr(herdr.shutil, "which", lambda name: None)
    assert not herdr.available({"HERDR_ENV": "1"})


def test_error_code_from_stderr_json():
    fake = FakeHerdr({("pane", "list"): HerdrError("server_unavailable")})
    with pytest.raises(HerdrError, match="server_unavailable"):
        Herdr(fake).board_pane()


def test_open_board_splits_and_labels_a_new_pane():
    fake = FakeHerdr({("pane", "list"): {"panes": []}, ("pane", "split"): {"pane": {"pane_id": "w1:p2"}}})
    assert Herdr(fake).open_board(REPO, "tm board 680") == "w1:p2"
    split, rename, run = fake.calls[1:]
    assert split[:6] == ["pane", "split", "--current", "--direction", "right", "--ratio"]
    assert rename == ["pane", "rename", "w1:p2", "ticketmaster"]
    assert run == ["pane", "run", "w1:p2", "tm board 680"]


def test_open_board_restarts_in_the_existing_pane():
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [{"pane_id": "w1:p1"}, {"pane_id": "w1:p5", "label": "ticketmaster"}]},
        ("pane", "process-info"): {"process_info": {"shell_pid": 7, "foreground_process_group_id": 7}},
    })
    assert Herdr(fake).open_board(REPO, "tm board") == "w1:p5"
    assert fake.calls[1] == ["pane", "send-keys", "w1:p5", "q"]
    assert fake.calls[-1] == ["pane", "run", "w1:p5", "cd /code/shiplog && tm board"]
    assert ("pane", "split") not in fake.verbs()


def test_new_ticket_gets_a_worktree_workspace_and_a_named_agent():
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(),
        ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}},
        ("agent", "list"): {"agents": []},
    })
    message = start_in_herdr(ticket(), StartOptions(cwd=REPO), Placement.WORKSPACE, Herdr(fake))
    create = next(c for c in fake.calls if c[:2] == ["worktree", "create"])
    assert create == ["worktree", "create", "--cwd", "/code/shiplog", "--branch", "issue-686",
                      "--label", "#686 Attach a deviation reason", "--no-focus"]
    start = next(c for c in fake.calls if c[:2] == ["agent", "start"])
    assert start[2:7] == ["issue-686", "--kind", "claude", "--pane", "w9:p1"]
    claude_args = start[start.index("--", 7) + 1:]
    assert "-w" not in claude_args and claude_args[:2] == ["-n", "#686 Attach a deviation reason"]
    assert message == "Started #686 in new worktree workspace #686 Attach a deviation reason as agent issue-686"


def test_claude_w_worktree_is_reopened_not_recreated():
    old = {"path": "/code/shiplog/.claude/worktrees/issue-686", "branch": "worktree-issue-686", "is_linked_worktree": True}
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(old),
        ("worktree", "open"): {"root_pane": {"pane_id": "w4:p1"}},
        ("agent", "list"): {"agents": [{"name": "issue-686"}]},
    })
    start_in_herdr(ticket(), StartOptions(cwd=REPO), Placement.WORKSPACE, Herdr(fake))
    assert ("worktree", "create") not in fake.verbs()
    assert next(c for c in fake.calls if c[:2] == ["worktree", "open"])[4:6] == ["--path", old["path"]]
    assert next(c for c in fake.calls if c[:2] == ["agent", "start"])[2] == "issue-686-2"  # name taken


def test_open_worktree_workspace_gets_a_new_tab():
    wt = {"path": "/h/worktrees/shiplog/issue-686", "branch": "issue-686", "is_linked_worktree": True, "open_workspace_id": "w4"}
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(wt),
        ("tab", "create"): {"root_pane": {"pane_id": "w4:p3"}},
        ("agent", "list"): {"agents": []},
    })
    start_in_herdr(ticket(), StartOptions(cwd=REPO), Placement.WORKSPACE, Herdr(fake))
    assert next(c for c in fake.calls if c[:2] == ["tab", "create"])[2:4] == ["--workspace", "w4"]


def test_startup_prompt_is_reported_not_failed():
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(),
        ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}},
        ("agent", "list"): {"agents": []},
        ("agent", "start"): HerdrError("agent_not_ready"),
    })
    message = start_in_herdr(ticket(), StartOptions(cwd=REPO), Placement.WORKSPACE, Herdr(fake))
    assert message.endswith("(waiting at a startup prompt)")


def test_split_keeps_claude_w():
    fake = FakeHerdr({("pane", "split"): {"pane": {"pane_id": "w1:p3"}}, ("agent", "list"): {"agents": []}})
    message = start_in_herdr(ticket(), StartOptions(cwd=REPO), Placement.SPLIT_DOWN, Herdr(fake))
    assert fake.calls[0][:5] == ["pane", "split", "--current", "--direction", "down"]
    start = fake.calls[-1]
    assert start[start.index("--", 7) + 1:][:2] == ["-w", "issue-686"]
    assert "a herdr pane below" in message


def test_custom_claude_binary_runs_as_a_plain_command():
    fake = FakeHerdr({("tab", "create"): {"root_pane": {"pane_id": "w1:p4"}}})
    start_in_herdr(ticket(), StartOptions(cwd=REPO, claude="/opt/my-claude"), Placement.TAB, Herdr(fake))
    assert fake.calls[-1][:3] == ["pane", "run", "w1:p4"] and fake.calls[-1][3].startswith("/opt/my-claude -w issue-686")


def test_adopt_opens_only_unopened_claude_worktrees():
    fake = FakeHerdr({("worktree", "list"): worktrees(
        {"path": "/code/shiplog/.claude/worktrees/issue-692", "branch": "worktree-issue-692", "is_linked_worktree": True},
        {"path": "/code/shiplog/.claude/worktrees/issue-693", "branch": "worktree-issue-693", "is_linked_worktree": True, "open_workspace_id": "w5"},
        {"path": "/code/shiplog-lease-647", "branch": "fix/lease", "is_linked_worktree": True},
    )})
    assert Herdr(fake).adopt(REPO, {"issue-692": "#692 Fuel"}) == ["#692 Fuel"]
    opens = [c for c in fake.calls if c[:2] == ["worktree", "open"]]
    assert len(opens) == 1 and opens[0][5] == "/code/shiplog/.claude/worktrees/issue-692"
