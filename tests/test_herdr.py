import json
import subprocess
from pathlib import Path

import pytest

from ticketmaster import herdr
from ticketmaster.herdr import Herdr, HerdrError, Session
from ticketmaster.model import Ticket
from ticketmaster.start import Workbench

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

    def first(self, group, verb):
        return next(c for c in self.calls if c[:2] == [group, verb])


@pytest.fixture(autouse=True)
def no_git(monkeypatch):
    monkeypatch.setattr(herdr, "local_branches", lambda cwd: [])


def worktrees(*entries):
    return {"worktrees": [{"path": str(REPO), "branch": "main", "is_linked_worktree": False, "open_workspace_id": "w1"}, *entries]}


def ticket():
    return Ticket(number=686, title="Attach a  deviation reason", url="https://gh/686", is_open=True)


def bench(fake, **kw):
    return Workbench(REPO, herdr=Herdr(fake), **kw)


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
    assert Herdr(fake).open_board(REPO, "tm open 680") == "w1:p2"
    split, rename, run = fake.calls[1:]
    assert split[:6] == ["pane", "split", "--current", "--direction", "right", "--ratio"]
    assert rename == ["pane", "rename", "w1:p2", "ticketmaster"]
    assert run == ["pane", "run", "w1:p2", "tm open 680"]


def test_open_board_restarts_in_the_existing_pane():
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [{"pane_id": "w1:p1"}, {"pane_id": "w1:p5", "label": "ticketmaster"}]},
        ("pane", "process-info"): {"process_info": {"shell_pid": 7, "foreground_process_group_id": 7}},
    })
    assert Herdr(fake).open_board(REPO, "tm open") == "w1:p5"
    assert ["pane", "send-keys", "w1:p5", "q"] not in fake.calls  # already at the prompt
    assert fake.calls[-1] == ["pane", "run", "w1:p5", "cd /code/shiplog && tm open"]
    assert ("pane", "split") not in fake.verbs()


def test_open_board_quits_a_running_board_before_restarting_it():
    states = iter([{"shell_pid": 7, "foreground_process_group_id": 9}, {"shell_pid": 7, "foreground_process_group_id": 7}])
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [{"pane_id": "w1:p5", "label": "ticketmaster"}]},
        ("pane", "process-info"): lambda argv: {"process_info": next(states)},
    })
    assert Herdr(fake).open_board(REPO, "tm open") == "w1:p5"
    assert fake.calls[2] == ["pane", "send-keys", "w1:p5", "q"]
    assert fake.calls[-1] == ["pane", "run", "w1:p5", "cd /code/shiplog && tm open"]


def test_open_board_from_the_board_pane_itself_leaves_the_pane_alone():
    fake = FakeHerdr({("pane", "list"): {"panes": [{"pane_id": "w1:p5", "label": "ticketmaster"}]}})
    assert Herdr(fake).open_board(REPO, "tm open", caller="w1:p5") is None
    assert fake.verbs() == [("pane", "list")]


def test_start_creates_a_worktree_workspace_with_a_named_agent():
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(),
        ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}},
        ("agent", "list"): {"agents": []},
    })
    message = bench(fake).start(ticket())
    assert fake.first("worktree", "create") == ["worktree", "create", "--cwd", "/code/shiplog", "--branch", "issue-686",
                                                "--label", "#686 Attach a deviation reason", "--no-focus"]
    start = fake.first("agent", "start")
    assert start[2:7] == ["issue-686", "--kind", "claude", "--pane", "w9:p1"]
    args = start[start.index("--", 7) + 1:]
    assert "-w" not in args and args[:2] == ["-n", "#686 Attach a deviation reason"]
    assert message == "Started #686 in new worktree issue-686 as agent issue-686"


def test_start_always_makes_a_new_worktree_even_when_one_exists(monkeypatch):
    monkeypatch.setattr(herdr, "local_branches", lambda cwd: ["main", "issue-686-2"])
    old = {"path": "/code/shiplog/.claude/worktrees/issue-686", "branch": "worktree-issue-686", "is_linked_worktree": True}
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(old),
        ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}},
        ("agent", "list"): {"agents": []},
    })
    bench(fake).start(ticket())
    assert ("worktree", "open") not in fake.verbs()
    assert fake.first("worktree", "create")[5] == "issue-686-3"  # issue-686 and issue-686-2 are taken


def test_review_gets_its_own_worktree():
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(),
        ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}},
        ("agent", "list"): {"agents": [{"name": "review-686"}]},
    })
    message = bench(fake).review(ticket())
    assert fake.first("worktree", "create")[5] == "review-686"
    assert fake.first("agent", "start")[2] == "review-686-2"  # agent name taken
    assert message.startswith("Reviewing #686 in new worktree review-686")


def test_startup_prompt_is_reported_not_failed():
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(),
        ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}},
        ("agent", "list"): {"agents": []},
        ("agent", "start"): HerdrError("agent_not_ready"),
    })
    assert bench(fake).start(ticket()).endswith("(waiting at a startup prompt)")


def test_readiness_timeout_is_reported_not_failed():
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(),
        ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}},
        ("agent", "list"): {"agents": []},
        ("agent", "start"): HerdrError("timeout"),
    })
    assert bench(fake).start(ticket()).endswith("(still starting)")


def test_custom_claude_binary_runs_as_a_plain_command():
    fake = FakeHerdr({("worktree", "list"): worktrees(), ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}}})
    bench(fake, claude="/opt/my-claude").start(ticket())
    assert fake.calls[-1][:3] == ["pane", "run", "w9:p1"] and fake.calls[-1][3].startswith("/opt/my-claude -n")


def test_sessions_map_worktrees_to_tickets_with_agent_state():
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(
            {"path": "/code/shiplog/.claude/worktrees/issue-713", "branch": "feat/713-reasons", "is_linked_worktree": True, "open_workspace_id": "wA"},
            {"path": "/h/worktrees/shiplog/issue-713-2", "branch": "issue-713-2", "is_linked_worktree": True},
            {"path": "/h/worktrees/shiplog/review-741", "branch": "review-741", "is_linked_worktree": True, "open_workspace_id": "wC"},
            {"path": "/code/shiplog-lease-647", "branch": "fix/lease", "is_linked_worktree": True},
        ),
        ("agent", "list"): {"agents": [
            {"workspace_id": "wA", "agent_status": "idle"},
            {"workspace_id": "wA", "agent_status": "blocked"},
        ]},
    })
    found = Herdr(fake).sessions(REPO)
    assert set(found) == {713, 741}
    first, second = found[713]
    assert (first.workspace, first.status) == ("wA", "blocked")  # the agent needing you wins
    assert (second.kind, second.workspace, second.status) == ("issue", None, None)
    assert (found[741][0].kind, found[741][0].status) == ("review", None)  # open, but no agent


def test_go_to_focuses_a_live_session():
    fake = FakeHerdr({})
    session = Session(713, "issue", "/w/issue-713", workspace="wA", status="working")
    assert bench(fake).go_to(ticket(), session) == "#686: focused wA"
    assert fake.calls == [["workspace", "focus", "wA"]]


def test_go_to_reopens_a_closed_worktree_and_resumes_claude():
    fake = FakeHerdr({("worktree", "open"): {"root_pane": {"pane_id": "wD:p1"}}, ("agent", "list"): {"agents": []}})
    bench(fake).go_to(ticket(), Session(686, "issue", "/w/issue-686"))
    assert fake.first("worktree", "open")[4:6] == ["--path", "/w/issue-686"]
    start = fake.first("agent", "start")
    assert start[2] == "issue-686" and start[-2:] == ["--", "--continue"]


def test_go_to_an_open_workspace_without_claude_resumes_in_a_new_tab():
    fake = FakeHerdr({("tab", "create"): {"root_pane": {"pane_id": "wA:p2"}}, ("agent", "list"): {"agents": []}})
    bench(fake).go_to(ticket(), Session(686, "issue", "/w/issue-686", workspace="wA"))
    assert fake.first("tab", "create")[2:4] == ["--workspace", "wA"]
    assert ["workspace", "focus", "wA"] in fake.calls and fake.first("agent", "start")[-1] == "--continue"


def test_remove_opens_a_closed_worktree_then_removes_it_without_force():
    fake = FakeHerdr({("worktree", "open"): {"root_pane": {"pane_id": "wD:p1"}}})
    message = bench(fake).remove(ticket(), [Session(686, "issue", "/w/issue-686"), Session(686, "review", "/w/review-686", workspace="wE")])
    removes = [c for c in fake.calls if c[:2] == ["worktree", "remove"]]
    assert removes == [["worktree", "remove", "--workspace", "wD"], ["worktree", "remove", "--workspace", "wE"]]
    assert message == "Removed 2 worktree(s) of #686"


def test_adopt_opens_only_unopened_claude_worktrees():
    fake = FakeHerdr({("worktree", "list"): worktrees(
        {"path": "/code/shiplog/.claude/worktrees/issue-692", "branch": "worktree-issue-692", "is_linked_worktree": True},
        {"path": "/code/shiplog/.claude/worktrees/issue-693", "branch": "worktree-issue-693", "is_linked_worktree": True, "open_workspace_id": "w5"},
        {"path": "/code/shiplog-lease-647", "branch": "fix/lease", "is_linked_worktree": True},
    )})
    assert Herdr(fake).adopt(REPO, {"issue-692": "#692 Fuel"}) == ["#692 Fuel"]
    opens = [c for c in fake.calls if c[:2] == ["worktree", "open"]]
    assert len(opens) == 1 and opens[0][5] == "/code/shiplog/.claude/worktrees/issue-692"


def test_auto_remove_keeps_worktrees_with_unsaved_work():
    fake = FakeHerdr({})
    unsaved = {"/w/issue-686-2": "unpushed commits"}
    sessions = [Session(686, "issue", "/w/issue-686", workspace="wA"), Session(686, "issue", "/w/issue-686-2", workspace="wB")]
    message = bench(fake, check_unsaved=unsaved.get).auto_remove(ticket(), sessions)
    assert [c for c in fake.calls if c[:2] == ["worktree", "remove"]] == [["worktree", "remove", "--workspace", "wA"]]
    assert message == "#686 done: auto-removed issue-686; kept issue-686-2 (unpushed commits)"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_unsaved_work_sees_uncommitted_and_unpushed_changes(tmp_path):
    remote, clone = tmp_path / "remote.git", tmp_path / "clone"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(tmp_path, "clone", "-q", str(remote), str(clone))
    for key, value in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
        git(clone, "config", key, value)
    (clone / "a").write_text("1")
    git(clone, "add", "a")
    git(clone, "commit", "-qm", "a")
    assert herdr.unsaved_work(str(clone)) == "unpushed commits"
    git(clone, "push", "-q", "origin", "HEAD")
    assert herdr.unsaved_work(str(clone)) is None
    (clone / "a").write_text("2")
    assert herdr.unsaved_work(str(clone)) == "uncommitted changes"
    assert herdr.unsaved_work(str(tmp_path)) == "not a git checkout"
