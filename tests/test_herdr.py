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


def test_the_board_pane_runs_the_plugin_wrapper_by_its_absolute_path():
    """Plugin `bin/` is on PATH only for Claude's Bash tool, so the pane gets the wrapper's path and data dir."""
    env = {"TICKETMASTER_WRAPPER": "/plugins/tm 0.3/bin/ticketmaster", "CLAUDE_PLUGIN_DATA": "/data/ticketmaster-kareblak"}
    fake = FakeHerdr({("pane", "list"): {"panes": [pane("w1:p1")]}, ("pane", "split"): {"pane": {"pane_id": "w1:p2"}}})
    Herdr(fake).open_board(REPO, herdr.open_command(["680", "--interval", "5"], env), caller="w1:p1")
    assert fake.first("pane", "run") == [
        "pane", "run", "w1:p2",
        "TICKETMASTER_BOARD=1 CLAUDE_PLUGIN_DATA=/data/ticketmaster-kareblak '/plugins/tm 0.3/bin/ticketmaster' open 680 --interval 5",
    ]


def test_outside_the_plugin_the_board_pane_reruns_this_interpreter(monkeypatch):
    monkeypatch.setattr(herdr.sys, "executable", "/venv/bin/python")
    assert herdr.open_command(["680"], {}) == "/venv/bin/python -m ticketmaster open 680"


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


def decision(kind="grilling", **kw):
    """A decision ticket of map #680."""
    base = dict(number=686, title="Attach a  deviation reason", url="https://gh/686", is_open=True,
                labels=(f"wayfinder:{kind}",), parent_number=680, parent_url="https://gh/680",
                parent_labels=("wayfinder:map",))
    return Ticket(**{**base, **kw})


def starting(agents=(), trees=(), **answers):
    return FakeHerdr({
        ("worktree", "list"): worktrees(*trees),
        ("worktree", "create"): {"root_pane": {"pane_id": "w9:p1"}},
        ("agent", "list"): {"agents": list(agents)},
        **{tuple(verb.split()): answer for verb, answer in answers.items()},
    })


def claude_argv(fake):
    start = fake.first("agent", "start")
    return start[start.index("--", 7) + 1:]


def test_start_creates_a_worktree_workspace_named_by_the_issue_key_with_a_named_agent():
    fake = starting()
    message = bench(fake).start(ticket())
    assert fake.first("worktree", "create") == ["worktree", "create", "--cwd", "/code/shiplog", "--branch", "issue-686",
                                                "--label", "#686 Attach a deviation reason", "--focus"]
    start = fake.first("agent", "start")
    assert start[2:7] == ["issue-686", "--kind", "claude", "--pane", "w9:p1"]
    assert claude_argv(fake) == ["-n", "#686 Attach a deviation reason", "/mattpocock-skills:wayfinder https://gh/686"]
    assert message == "Started #686 in new worktree issue-686 as agent issue-686"


def test_a_linear_issue_is_keyed_tm_n_and_labelled_with_its_team_key():
    fake = starting()
    message = bench(fake).start(decision(number=21, team_key="TM"))
    create = fake.first("worktree", "create")
    assert create[create.index("--branch") + 1] == "tm-21"
    assert create[create.index("--label") + 1] == "TM-21 Attach a deviation reason"
    assert claude_argv(fake)[:2] == ["-n", "TM-21 Attach a deviation reason"]
    assert message.startswith("Started TM-21 in new worktree tm-21")


def test_the_prompt_is_exactly_the_command_the_map_url_and_the_ticket_url():
    fake = starting()
    bench(fake, wayfinder_command="/wayfinder").start(decision())
    assert claude_argv(fake)[-1] == "/wayfinder https://gh/680 https://gh/686"
    fake = starting()
    bench(fake, wayfinder_command="/wayfinder").start(ticket())
    assert claude_argv(fake)[-1] == "/wayfinder https://gh/686"


@pytest.mark.parametrize("kind", ["grilling", "prototype", "task"])
def test_hitl_tickets_take_focus(kind):
    fake = starting()
    message = bench(fake).start(decision(kind))
    assert fake.first("worktree", "create")[-1] == "--focus" and "unfocused" not in message


def test_research_starts_unfocused_and_the_message_names_it():
    fake = starting()
    message = bench(fake).start(decision("research"))
    assert fake.first("worktree", "create")[-1] == "--no-focus"
    assert message == "Started #686 in new worktree issue-686 as agent issue-686, unfocused: research runs AFK"


def test_start_never_creates_a_suffixed_worktree_or_branch():
    fake = starting(trees=[tree("/code/shiplog/.claude/worktrees/issue-686-2")])
    bench(fake).start(ticket())
    create = fake.first("worktree", "create")
    assert create[create.index("--branch") + 1] == "issue-686"


def test_an_agentless_worktree_is_reopened_and_claude_continues_there():
    fake = starting(**{"worktree open": {"root_pane": {"pane_id": "wD:p1"}}})
    message = bench(fake).start(decision(), Session(686, "/w/issue-686", "issue-686"))
    assert ("worktree", "create") not in fake.verbs()
    assert fake.first("worktree", "open") == ["worktree", "open", "--cwd", "/code/shiplog", "--path", "/w/issue-686",
                                              "--label", "#686 Attach a deviation reason", "--focus"]
    assert fake.first("agent", "start")[2:7] == ["issue-686", "--kind", "claude", "--pane", "wD:p1"]
    assert claude_argv(fake) == ["-n", "#686 Attach a deviation reason", "--continue"]
    assert message == "Continued #686 in worktree issue-686 as agent issue-686"


def test_an_agentless_worktree_open_in_a_workspace_continues_in_its_pane():
    fake = starting(**{"pane list": {"panes": [pane("wX:p1"), pane("wA:p2"), pane("wA:p3")]}})
    message = bench(fake).start(decision("research"), Session(686, "/w/issue-686", workspace="wA"))
    assert ("worktree", "open") not in fake.verbs() and ("workspace", "focus") not in fake.verbs()
    assert fake.first("agent", "start")[6] == "wA:p2"
    assert claude_argv(fake)[-1] == "--continue"
    assert message.endswith(", unfocused: research runs AFK")


def test_continuing_a_hitl_ticket_in_an_open_workspace_focuses_it():
    fake = starting(**{"pane list": {"panes": [pane("wA:p2")]}})
    bench(fake).start(decision(), Session(686, "/w/issue-686", workspace="wA"))
    assert ["workspace", "focus", "wA"] in fake.calls


def test_a_taken_agent_name_gets_a_suffix():
    fake = starting(agents=[{"name": "issue-686"}])
    assert bench(fake).start(ticket()) == "Started #686 in new worktree issue-686 as agent issue-686-2"


def test_startup_prompt_is_reported_not_failed():
    fake = starting(**{"agent start": HerdrError("agent_not_ready")})
    assert bench(fake).start(ticket()).endswith("(waiting at a startup prompt)")


def test_readiness_timeout_is_reported_not_failed():
    fake = starting(**{"agent start": HerdrError("timeout")})
    assert bench(fake).start(ticket()).endswith("(still starting)")


def test_custom_claude_binary_runs_as_a_plain_command():
    fake = starting()
    bench(fake, claude="/opt/my-claude").start(ticket())
    assert fake.calls[-1][:3] == ["pane", "run", "w9:p1"] and fake.calls[-1][3].startswith("/opt/my-claude -n")


def tree(path, branch=None, workspace=None):
    entry = {"path": path, "branch": branch or Path(path).name, "is_linked_worktree": True}
    if workspace:
        entry["open_workspace_id"] = workspace
    return entry


def agent(pane_id, status="idle", cwd=REPO, foreground=None, title=""):
    return {"pane_id": pane_id, "workspace_id": pane_id.split(":")[0], "agent_status": status, "cwd": str(cwd),
            "foreground_cwd": str(foreground or cwd), "terminal_title": title, "terminal_title_stripped": title}


def found_by(trees=(), agents=()):
    fake = FakeHerdr({("worktree", "list"): worktrees(*trees), ("agent", "list"): {"agents": list(agents)}})
    return Herdr(fake).sessions(REPO)


@pytest.mark.parametrize("name", ["issue-5", "tm-5", "worktree-tm-5", "worktree-issue-5", "tm-5-2", "TM-5"])
def test_worktree_names_tie_to_their_ticket(name):
    assert set(found_by([tree(f"/h/wt/shiplog/{name}")])) == {5}


def test_a_worktree_ties_by_its_branch_too():
    assert set(found_by([tree("/h/wt/shiplog/lease", branch="worktree-tm-5")])) == {5}


@pytest.mark.parametrize("name", ["review-5", "tm-5x", "agent-a5", "fix-5", "issue-"])
def test_other_worktree_names_tie_to_no_ticket(name):
    assert found_by([tree(f"/h/wt/shiplog/{name}")]) == {}


@pytest.mark.parametrize("title", ["TM-5 Pick a store", "#5 Pick a store", "tm-5", "✳ TM-5 Pick a store", "#5: store"])
def test_a_terminal_title_starting_with_the_key_ties_to_the_ticket(title):
    found = found_by(agents=[agent("w3:p1", "working", title=title)])
    assert set(found) == {5}
    assert (found[5][0].pane, found[5][0].status, found[5][0].path) == ("w3:p1", "working", str(REPO))


@pytest.mark.parametrize("title", ["TM-50 other", "#55", "Fix TM-5 later", "5 awaiting input · claude agents", ""])
def test_titles_not_starting_with_the_key_do_not_tie_to_the_ticket(title):
    assert 5 not in found_by(agents=[agent("w3:p1", title=title)])


def test_an_agent_outside_the_repo_is_ignored():
    found = found_by([tree("/h/wt/shiplog/tm-5")], [
        agent("w3:p1", "working", cwd="/code/tickets", title="TM-5 elsewhere"),
        agent("w4:p1", "blocked", cwd="/h/wt/other/tm-5"),
    ])
    assert [(s.pane, s.status) for s in found[5]] == [(None, None)]  # only the worktree, no agent


def test_a_claude_w_agent_in_another_workspace_gets_its_state_through_the_foreground_cwd():
    """`claude -w tm-5` in some other workspace's pane: the worktree has no open workspace, the pane's cwd is the
    main checkout, and only the foreground cwd says where Claude runs."""
    path = "/code/shiplog/.claude/worktrees/tm-5"
    found = found_by([tree(path, "worktree-tm-5")], [
        agent("wX:p2", "blocked", cwd=REPO, foreground=f"{path}/src"),
        agent("wX:p1", "working", cwd=REPO),  # in the main checkout: no ticket's
    ])
    assert set(found) == {5}
    (session,) = found[5]
    assert (session.path, session.pane, session.workspace, session.status) == (path, "wX:p2", "wX", "blocked")


def test_the_join_ignores_the_open_workspace_and_falls_back_to_the_pane_cwd():
    path = "/h/wt/shiplog/issue-713"
    found = found_by([tree(path, workspace="wA")], [
        agent("wA:p1", "working", cwd=REPO),  # the worktree's workspace, but not in its checkout
        {**agent("wB:p1", "idle", cwd=path), "foreground_cwd": None},
    ])
    assert [(s.pane, s.status) for s in found[713]] == [("wB:p1", "idle")]


def test_each_agent_is_a_session_most_urgent_first_and_a_dead_worktree_follows():
    live, dead = "/h/wt/shiplog/issue-7", "/h/wt/shiplog/issue-7-2"
    found = found_by([tree(live), tree(dead, workspace="wD")], [
        agent("w1:p1", "idle", cwd=live), agent("w1:p2", "blocked", cwd=live), agent("w1:p3", "working", cwd=live),
    ])
    assert [(s.status, s.path) for s in found[7]] == [
        ("blocked", live), ("working", live), ("idle", live), (None, dead),
    ]
    assert found[7][-1].workspace == "wD"


def test_a_titled_agent_in_a_ticket_worktree_counts_once():
    path = "/h/wt/shiplog/tm-5"
    found = found_by([tree(path)], [agent("w1:p1", "working", cwd=path, title="TM-5 store")])
    assert len(found[5]) == 1


def test_go_to_focuses_a_live_sessions_agent_pane():
    fake = FakeHerdr({})
    session = Session(713, "/w/issue-713", workspace="wA", status="working", pane="wA:p3")
    assert bench(fake).go_to(ticket(), session) == "#686: focused issue-713"
    assert fake.calls == [["agent", "focus", "wA:p3"]]


def test_go_to_opens_a_dead_worktree_with_a_shell_without_resuming_claude():
    fake = FakeHerdr({("worktree", "open"): {"root_pane": {"pane_id": "wD:p1"}}})
    assert bench(fake).go_to(ticket(), Session(686, "/w/issue-686")) == "#686: opened issue-686 with a shell"
    assert fake.calls == [["worktree", "open", "--cwd", "/code/shiplog", "--path", "/w/issue-686",
                           "--label", "#686 Attach a deviation reason", "--focus"]]


def test_go_to_an_open_workspace_without_claude_focuses_it_without_resuming():
    fake = FakeHerdr({})
    assert bench(fake).go_to(ticket(), Session(686, "/w/issue-686", workspace="wA")) == \
        "#686: focused issue-686 (no Claude running)"
    assert fake.calls == [["workspace", "focus", "wA"]]


def test_herdr_cannot_remove_or_adopt_worktrees():
    assert not any(hasattr(Herdr, action) for action in ("remove", "adopt"))
    assert not hasattr(herdr, "unsaved_work")
