import json
import subprocess
from pathlib import Path

import pytest

from ticketmaster import herdr
from ticketmaster.herdr import BoardOpened, Herdr, HerdrError, Session
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
    monkeypatch.setattr(herdr, "repo_root", lambda cwd: cwd)


def worktrees(*entries):
    return {"worktrees": [{"path": str(REPO), "branch": "main", "is_linked_worktree": False, "open_workspace_id": "w1"}, *entries]}


def ticket():
    return Ticket(number=686, title="Attach a  deviation reason", url="https://gh/686", is_open=True,
                  labels=("wayfinder:map",))


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
        Herdr(fake).open_board(REPO, "tm open", caller="w1:p1")


def pane(pane_id, label=None, cwd=REPO, session=None, focused=False):
    entry = {"pane_id": pane_id, "workspace_id": pane_id.split(":")[0], "cwd": str(cwd), "foreground_cwd": str(cwd), "focused": focused}
    if label:
        entry["label"] = label
    if session:
        entry["agent_session"] = {"agent": "claude", "kind": "id", "value": session}
    return entry


AT_PROMPT = {"process_info": {"shell_pid": 7, "foreground_process_group_id": 7}}


def test_open_board_splits_and_labels_a_new_pane():
    fake = FakeHerdr({("pane", "list"): {"panes": [pane("w1:p1")]}, ("pane", "split"): {"pane": {"pane_id": "w1:p2"}}})
    assert Herdr(fake).open_board(REPO, "tm open 680", caller="w1:p1") == BoardOpened("w1:p2", "w1:p1", restarted=False)
    split, rename, run = fake.calls[1:]
    assert split[:7] == ["pane", "split", "--pane", "w1:p1", "--direction", "right", "--ratio"]
    assert rename == ["pane", "rename", "w1:p2", "ticketmaster"]
    assert run == ["pane", "run", "w1:p2", "TICKETMASTER_BOARD=1 tm open 680"]


def test_open_board_splits_off_the_env_pane_of_this_claude_session():
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [pane("w1:p1", focused=True), pane("wN:p1", cwd="/elsewhere", session="me")]},
        ("pane", "split"): {"pane": {"pane_id": "wN:p2"}},
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="wN:p1", session="me").beside == "wN:p1"
    assert fake.first("pane", "split")[2:4] == ["--pane", "wN:p1"]


def test_open_board_from_a_stale_env_pane_splits_off_the_focused_pane():
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [pane("w4:p1"), pane("wJ:p1", focused=True)]},
        ("pane", "split"): {"pane": {"pane_id": "wJ:p2"}},
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="w9:p7", session="me") == BoardOpened("wJ:p2", "wJ:p1", restarted=False)
    assert fake.first("pane", "split")[2:4] == ["--pane", "wJ:p1"]


def test_open_board_from_another_sessions_env_pane_splits_off_the_focused_pane():
    """The observed bug: a session inherited HERDR_PANE_ID of a Claude pane in another project."""
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [pane("w4:pY", cwd="/code/tickets", session="other"), pane("wJ:p1", focused=True)]},
        ("pane", "split"): {"pane": {"pane_id": "wJ:p2"}},
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="w4:pY", session="me").beside == "wJ:p1"
    assert not [c for c in fake.calls if "w4:pY" in c]


def test_open_board_from_an_env_pane_in_another_project_splits_off_the_focused_pane():
    """Without a herdr session id to compare, the pane's working directory must be in the repo."""
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [pane("w4:p0", cwd="/code/tickets"), pane("wJ:p1", focused=True)]},
        ("pane", "split"): {"pane": {"pane_id": "wJ:p2"}},
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="w4:p0", session="me").beside == "wJ:p1"


def test_open_board_accepts_an_env_pane_in_a_subdirectory_of_the_repo():
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [pane("w1:p1", cwd=REPO / "src"), pane("w2:p1", focused=True)]},
        ("pane", "split"): {"pane": {"pane_id": "w1:p2"}},
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="w1:p1").beside == "w1:p1"


def test_open_board_without_a_valid_or_focused_pane_splits_nothing():
    fake = FakeHerdr({("pane", "list"): {"panes": [pane("w4:p0", cwd="/code/tickets")]}})
    with pytest.raises(HerdrError, match="no pane is focused"):
        Herdr(fake).open_board(REPO, "tm open", caller="w4:p0")
    assert fake.verbs() == [("pane", "list")]


def test_open_board_restarts_in_the_existing_pane():
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [pane("w1:p1"), pane("w1:p5", label="ticketmaster")]},
        ("pane", "process-info"): AT_PROMPT,
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="w1:p1") == BoardOpened("w1:p5", "w1:p1", restarted=True)
    assert ["pane", "send-keys", "w1:p5", "q"] not in fake.calls  # already at the prompt
    assert fake.calls[-1] == ["pane", "run", "w1:p5", "cd /code/shiplog && TICKETMASTER_BOARD=1 tm open"]
    assert ("pane", "split") not in fake.verbs()


def test_open_board_from_a_stale_env_pane_restarts_the_board_in_the_focused_panes_workspace():
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [
            pane("w4:p1", cwd="/code/tickets", session="other"), pane("w4:p2", label="ticketmaster"),
            pane("wJ:p1", focused=True), pane("wJ:p2", label="ticketmaster"),
        ]},
        ("pane", "process-info"): AT_PROMPT,
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="w4:p1", session="me") == BoardOpened("wJ:p2", "wJ:p1", restarted=True)
    assert not [c for c in fake.calls if "w4:p2" in c]


def test_open_board_when_the_focused_fallback_is_the_board_restarts_it():
    """Only this process's own pane runs the board in place; a focused board pane is restarted."""
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [pane("wJ:p2", label="ticketmaster", focused=True)]},
        ("pane", "process-info"): AT_PROMPT,
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="w9:p9") == BoardOpened("wJ:p2", "wJ:p2", restarted=True)


def test_open_board_quits_a_running_board_before_restarting_it():
    states = iter([{"shell_pid": 7, "foreground_process_group_id": 9}, {"shell_pid": 7, "foreground_process_group_id": 7}])
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [pane("w1:p1"), pane("w1:p5", label="ticketmaster")]},
        ("pane", "process-info"): lambda argv: {"process_info": next(states)},
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="w1:p1").pane == "w1:p5"
    assert fake.calls[2] == ["pane", "send-keys", "w1:p5", "q"]
    assert fake.calls[-1] == ["pane", "run", "w1:p5", "cd /code/shiplog && TICKETMASTER_BOARD=1 tm open"]


def test_open_board_ignores_a_board_in_another_workspace_and_splits_beside_the_caller():
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [{"pane_id": "w1:p2", "tab_id": "w1:t1", "label": "ticketmaster"}, pane("wN:p1")]},
        ("pane", "split"): {"pane": {"pane_id": "wN:p2"}},
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="wN:p1").pane == "wN:p2"
    assert not [c for c in fake.calls if "w1:p2" in c]
    assert fake.first("pane", "split")[2:4] == ["--pane", "wN:p1"]


def test_open_board_restarts_the_callers_workspace_board_even_when_another_workspace_lists_first():
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [pane("w1:p2", label="ticketmaster"), pane("wN:p1"), pane("wN:p2", label="ticketmaster")]},
        ("pane", "process-info"): AT_PROMPT,
    })
    assert Herdr(fake).open_board(REPO, "tm open", caller="wN:p1").pane == "wN:p2"
    assert not [c for c in fake.calls if "w1:p2" in c]
    assert ("pane", "split") not in fake.verbs()


def test_open_board_launched_by_a_board_pane_runs_in_place_without_looking_it_up(monkeypatch):
    """A freshly split board pane may not be in `pane list` yet; the env var it was started with says it is the board."""
    monkeypatch.setenv(herdr.BOARD_ENV, "1")
    fake = FakeHerdr({("pane", "list"): {"panes": [pane("w1:p2", label="ticketmaster")]}})
    assert Herdr(fake).open_board(REPO, "tm open", caller="wN:p2") is None
    assert fake.calls == []


def test_open_board_from_the_board_pane_itself_leaves_the_pane_alone():
    fake = FakeHerdr({("pane", "list"): {"panes": [pane("w1:p5", label="ticketmaster")]}})
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


def test_start_passes_the_configured_wayfinder_command_to_claude():
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(),
        ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}},
        ("agent", "list"): {"agents": []},
    })
    bench(fake, wayfinder_command="/wayfinder").start(ticket())
    assert fake.first("agent", "start")[-1] == "/wayfinder https://gh/686"


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


def test_a_taken_agent_name_gets_a_suffix():
    fake = FakeHerdr({
        ("worktree", "list"): worktrees(),
        ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}},
        ("agent", "list"): {"agents": [{"name": "issue-686"}]},
    })
    assert bench(fake).start(ticket()) == "Started #686 in new worktree issue-686 as agent issue-686-2"


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
            {"path": "/h/worktrees/shiplog/issue-742", "branch": "issue-742", "is_linked_worktree": True, "open_workspace_id": "wE"},
            {"path": "/code/shiplog-lease-647", "branch": "fix/lease", "is_linked_worktree": True},
        ),
        ("agent", "list"): {"agents": [
            {"workspace_id": "wA", "agent_status": "idle"},
            {"workspace_id": "wA", "agent_status": "blocked"},
        ]},
    })
    found = Herdr(fake).sessions(REPO)
    assert set(found) == {713, 742}  # review-<n> worktrees are no ticket's
    first, second = found[713]
    assert (first.workspace, first.status) == ("wA", "blocked")  # the agent needing you wins
    assert (second.path, second.workspace, second.status) == ("/h/worktrees/shiplog/issue-713-2", None, None)
    assert (found[742][0].workspace, found[742][0].status) == ("wE", None)  # open, but no agent


def test_go_to_focuses_a_live_session():
    fake = FakeHerdr({})
    session = Session(713, "/w/issue-713", workspace="wA", status="working")
    assert bench(fake).go_to(ticket(), session) == "#686: focused wA"
    assert fake.calls == [["workspace", "focus", "wA"]]


def test_go_to_reopens_a_closed_worktree_and_resumes_claude():
    fake = FakeHerdr({("worktree", "open"): {"root_pane": {"pane_id": "wD:p1"}}, ("agent", "list"): {"agents": []}})
    bench(fake).go_to(ticket(), Session(686, "/w/issue-686"))
    assert fake.first("worktree", "open")[4:6] == ["--path", "/w/issue-686"]
    start = fake.first("agent", "start")
    assert start[2] == "issue-686" and start[-2:] == ["--", "--continue"]


def test_go_to_an_open_workspace_without_claude_resumes_in_a_new_tab():
    fake = FakeHerdr({("tab", "create"): {"root_pane": {"pane_id": "wA:p2"}}, ("agent", "list"): {"agents": []}})
    bench(fake).go_to(ticket(), Session(686, "/w/issue-686", workspace="wA"))
    assert fake.first("tab", "create")[2:4] == ["--workspace", "wA"]
    assert ["workspace", "focus", "wA"] in fake.calls and fake.first("agent", "start")[-1] == "--continue"


def test_herdr_cannot_remove_or_adopt_worktrees():
    assert not any(hasattr(Herdr, action) for action in ("remove", "adopt"))
    assert not hasattr(herdr, "unsaved_work")
