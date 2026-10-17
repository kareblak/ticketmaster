import shlex
import signal
from pathlib import Path

import pytest

from fake_plain import FakeRunner, agents, fail, ok
from fakes import FakeProvider, a_map, impl_of, spec_of
from ticketmaster import backends, cli, config
from ticketmaster.board_model import BoardModel
from ticketmaster.backends import BackendError, SessionsUnreadable, plain
from ticketmaster.backends.plain import PlainBackend
from ticketmaster.providers import Query
from ticketmaster.session import Session
from ticketmaster.start import Workbench

CLAUDE = "/usr/local/bin/claude"
WRAPPER = "/plugins/tm/bin/ticketmaster"
FRONT = ("lsappinfo", "front")


class World:
    """The processes a plain backend sees: `live` maps a PID to its command line, `killed` records signals sent."""

    def __init__(self, tmp_path, live=None):
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.live = dict(live or {})
        self.killed = []
        self.exits = []

    def kill(self, pid, sig):
        if pid not in self.live:
            raise ProcessLookupError(pid)
        if sig:
            self.killed.append((pid, sig))

    def ps(self, argv):
        command = self.live.get(int(argv[2]))
        return ok(command + "\n") if command else fail(1)

    def backend(self, env, answers=None, pid=4242):
        runner = FakeRunner({
            ("git", "rev-parse", "--git-common-dir"): ok(".git\n"),
            ("ps",): self.ps,
            FRONT: ok("[ NULL ]\n"),
            **(answers or {}),
        })
        backend = PlainBackend(runner=runner, env=env, clock=lambda: 0.0, system="Darwin", which=lambda n: None,
                               kill=self.kill, at_exit=self.exits.append, pid=pid)
        return backend, runner

    def pidfile(self, slot):
        return self.repo / ".git" / "ticketmaster" / f"board-{slot}.pid"

    def board_at(self, slot, pid, command="/venv/bin/python -P -m ticketmaster open"):
        self.pidfile(slot).parent.mkdir(parents=True, exist_ok=True)
        self.pidfile(slot).write_text(f"{pid}\n")
        self.live[pid] = command


FROM_CLAUDE = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s-1", "TICKETMASTER_WRAPPER": WRAPPER,
               "CLAUDE_PLUGIN_DATA": "/data/tm", "GHOSTTY_RESOURCES_DIR": "/x", "PATH": "/opt/bin:/bin"}


def test_plain_is_always_available_and_auto_detected_outside_herdr(monkeypatch):
    assert [b.NAME for b in backends.BACKENDS] == ["herdr", "plain"]
    assert plain.unavailable({}) is None
    monkeypatch.setattr(backends.herdr.shutil, "which", lambda name: None)
    assert backends.choose(None, {}, {}).NAME == "plain"
    assert backends.choose(None, {"HERDR_ENV": "1"}, {}).NAME == "plain"  # herdr not on PATH


def test_make_takes_the_terminal_override_from_the_toml():
    backend = plain.make(Path("/r"), {"terminal": "foot -D {dir} {cmd}"}, {})
    assert (backend.name, backend.configured, backend.where) == ("plain", "foot -D {dir} {cmd}", "in a new terminal window")


# --- open_board -----------------------------------------------------------------------------------------------

def test_typed_at_a_shell_the_board_runs_in_place_with_no_pidfile(tmp_path):
    world = World(tmp_path)
    backend, runner = world.backend({"GHOSTTY_RESOURCES_DIR": "/x"})
    assert backend.open_board(world.repo, ["42"]) is None
    assert runner.calls == [] and not (world.repo / ".git").exists()


def test_from_claude_the_board_opens_in_a_new_window_running_the_wrapper_with_its_slot(tmp_path):
    world = World(tmp_path)
    backend, runner = world.backend(FROM_CLAUDE)
    report = backend.open_board(world.repo, ["42", "--backend", "plain"])
    assert report == "Board opened in a new Ghostty window"
    launch = runner.last("osascript")
    assert "activate" in launch.argv  # the board's own window comes forward
    assert launch.argv[-1] == shlex.join(["/bin/sh", "-c", f"cd {world.repo} && exec /usr/bin/env PATH=/opt/bin:/bin "
                                          f"TICKETMASTER_SLOT=s-1 CLAUDE_PLUGIN_DATA=/data/tm {WRAPPER} open 42 "
                                          f"--backend plain"])
    # iTerm2 and Ghostty run it without a login shell: without Claude's PATH the board finds neither gh nor claude
    assert "CLAUDECODE" not in launch.env and "CLAUDE_CODE_SESSION_ID" not in launch.env


def test_the_board_a_window_runs_records_itself_and_runs_in_place(tmp_path):
    world = World(tmp_path)
    backend, _ = world.backend({"TICKETMASTER_SLOT": "s-1"}, pid=777)
    assert backend.open_board(world.repo, ["42"]) is None
    assert world.pidfile("s-1").read_text() == "777\n"
    (forget,) = world.exits
    forget()
    assert not world.pidfile("s-1").exists()


def test_rerunning_from_the_same_claude_session_replaces_its_board(tmp_path):
    world = World(tmp_path)
    world.board_at("s-1", 900)
    backend, _ = world.backend(FROM_CLAUDE)
    assert backend.open_board(world.repo, []) == "Board opened in a new Ghostty window (replaced the previous one)"
    assert world.killed == [(900, signal.SIGTERM)]


def test_a_second_claude_session_keeps_the_first_ones_board(tmp_path):
    world = World(tmp_path)
    world.board_at("s-1", 900)
    backend, _ = world.backend({**FROM_CLAUDE, "CLAUDE_CODE_SESSION_ID": "s-2"})
    assert backend.open_board(world.repo, []) == "Board opened in a new Ghostty window"
    assert world.killed == []


def test_without_a_session_id_there_is_one_board_per_repo(tmp_path):
    world = World(tmp_path)
    world.board_at("repo", 900)
    env = {k: v for k, v in FROM_CLAUDE.items() if k != "CLAUDE_CODE_SESSION_ID"}
    backend, runner = world.backend(env)
    assert backend.open_board(world.repo, []).endswith("(replaced the previous one)")
    assert "TICKETMASTER_SLOT=repo" in runner.last("osascript").argv[-1]


@pytest.mark.parametrize("live", [{}, {900: "/usr/bin/vim notes.txt"}])
def test_a_pidfile_of_a_gone_or_reused_pid_replaces_nothing(tmp_path, live):
    world = World(tmp_path)
    world.board_at("s-1", 900)
    world.live = live
    backend, _ = world.backend(FROM_CLAUDE)
    assert backend.open_board(world.repo, []) == "Board opened in a new Ghostty window"
    assert world.killed == []


def test_without_a_terminal_it_fails_with_the_reason_the_paste_able_line_and_the_override(tmp_path):
    world = World(tmp_path)
    world.board_at("s-1", 900)
    backend, _ = world.backend({**FROM_CLAUDE, "SSH_CONNECTION": "1 2 3 4"})
    with pytest.raises(BackendError) as caught:
        backend.open_board(world.repo, ["42"])
    assert caught.value.source == "plain"
    assert str(caught.value) == (
        "couldn't open a terminal window (over SSH); run it yourself:\n"
        f"  cd {world.repo} && /usr/bin/env TICKETMASTER_SLOT=s-1 CLAUDE_PLUGIN_DATA=/data/tm {WRAPPER} open 42\n"
        "to pick the terminal, set TICKETMASTER_TERMINAL (or `terminal` in .ticketmaster.toml) to its command with "
        "{dir} and {cmd}, e.g. TICKETMASTER_TERMINAL='kitty --directory {dir} {cmd}'")
    assert world.killed == []  # the old board stays when no new one can open


def test_a_denied_automation_consent_fails_the_same_way(tmp_path):
    world = World(tmp_path)
    backend, _ = world.backend(FROM_CLAUDE, {("osascript",): fail(1, "execution error: Not authorized (-1743)")})
    with pytest.raises(BackendError, match=r"\(-1743\).*run it yourself:\n  cd "):
        backend.open_board(world.repo, [])


def test_the_cli_exits_1_when_no_window_opens(tmp_path, monkeypatch, capsys):
    world = World(tmp_path)
    backend, _ = world.backend({**FROM_CLAUDE, "SSH_TTY": "/dev/ttys1"})
    monkeypatch.setattr(cli, "resolve", lambda cwd, flag, config: backend)
    monkeypatch.setattr(config, "resolve_provider", lambda *a: type("P", (), {"name": "github", "repo": "o/r"})())
    monkeypatch.chdir(world.repo)
    assert cli.main(["42"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("ticketmaster: plain: couldn't open a terminal window (over SSH); run it yourself:\n  cd ")
    assert "TICKETMASTER_TERMINAL" in err


# --- sessions -------------------------------------------------------------------------------------------------

REPO = "/code/shiplog"
PORCELAIN = (f"worktree {REPO}\nHEAD a\nbranch refs/heads/main\n\n"
             f"worktree {REPO}/.claude/worktrees/issue-12\nHEAD b\nbranch refs/heads/issue-12\n\n"
             "worktree /home/me/.herdr/worktrees/shiplog/tm-13\nHEAD c\nbranch refs/heads/kare/tm-13-fix\n\n"
             f"worktree {REPO}/.claude/worktrees/issue-14\nHEAD d\nbranch refs/heads/issue-14\n\n"
             f"worktree {REPO}/.claude/worktrees/spike\nHEAD e\nbranch refs/heads/spike\n\n")
GIT = ("git", "worktree", "list", "--porcelain")
LIST = (CLAUDE, "agents", "--json")


def reading(answer):
    runner = FakeRunner({GIT: ok(PORCELAIN), LIST: answer})
    return PlainBackend(runner=runner, env={}, which=lambda n: CLAUDE if n == "claude" else None), runner


def test_sessions_map_claude_agents_statuses_by_worktree_cwd():
    backend, _ = reading(agents(
        {"cwd": f"{REPO}/.claude/worktrees/issue-12", "pid": 1, "status": "idle", "kind": "interactive"},
        {"cwd": f"{REPO}/.claude/worktrees/issue-12/src", "pid": 2, "status": "waiting", "waitingFor": "permission prompt"},
        {"cwd": "/home/me/.herdr/worktrees/shiplog/tm-13", "pid": 3, "status": "busy"},
        {"cwd": f"{REPO}", "pid": 4, "status": "busy"},  # the main checkout works no ticket
        {"cwd": f"{REPO}/.claude/worktrees/spike", "pid": 5, "status": "busy"},  # nor a worktree named otherwise
        {"cwd": "/elsewhere/issue-12", "pid": 6, "status": "busy"},  # nor another repo
    ))
    wt = f"{REPO}/.claude/worktrees"
    assert backend.sessions(Path(REPO)) == {
        12: [Session(12, f"{wt}/issue-12", "issue-12", "blocked", 2, "permission prompt"),
             Session(12, f"{wt}/issue-12", "issue-12", "idle", 1)],
        13: [Session(13, "/home/me/.herdr/worktrees/shiplog/tm-13", "kare/tm-13-fix", "working", 3)],
        14: [Session(14, f"{wt}/issue-14", "issue-14")],
    }


@pytest.mark.parametrize("entry", [
    {"status": "shell"}, {"status": None}, {}, {"state": "done", "kind": "background"}, {"status": "done"},
])
def test_any_other_or_missing_status_is_unknown_never_done(entry):
    backend, _ = reading(agents({"cwd": f"{REPO}/.claude/worktrees/issue-14", "id": "ab12", **entry}))
    (session,) = backend.sessions(Path(REPO))[14]
    assert session.status == "unknown"


@pytest.mark.parametrize("answer, which, error", [
    (agents(), lambda n: None, "claude not on PATH"),
    (fail(1, "Error: unknown command agents"), None, "exited 1: Error: unknown command agents"),
    (fail(2), None, "exited 2"),
    (ok("not json"), None, "unparseable output"),
    (ok('{"agents": []}'), None, "unparseable output"),
    (FileNotFoundError(2, "No such file or directory"), None, "No such file or directory"),
])
def test_before_any_good_read_every_worktree_shows_status_unavailable(answer, which, error):
    backend, _ = reading(answer)
    if which:
        backend.which = which
    with pytest.raises(SessionsUnreadable) as caught:
        backend.sessions(Path(REPO))
    assert (caught.value.source, str(caught.value)) == ("claude agents", error)
    wt = f"{REPO}/.claude/worktrees"
    assert caught.value.sessions == {
        12: [Session(12, f"{wt}/issue-12", "issue-12", "unavailable")],
        13: [Session(13, "/home/me/.herdr/worktrees/shiplog/tm-13", "kare/tm-13-fix", "unavailable")],
        14: [Session(14, f"{wt}/issue-14", "issue-14", "unavailable")],
    }


def test_after_a_good_read_a_failure_keeps_the_last_good_list():
    backend, runner = reading(agents())
    assert backend.sessions(Path(REPO))[14] == [Session(14, f"{REPO}/.claude/worktrees/issue-14", "issue-14")]
    runner.answers[LIST] = fail(1)
    with pytest.raises(SessionsUnreadable) as caught:
        backend.sessions(Path(REPO))
    assert caught.value.sessions is None


# --- start_in_worktree ------------------------------------------------------------------------------------------

LABEL = "#12 Fix it"
NEW = ["-n", LABEL, "/mattpocock-skills:implement https://x/issues/12"]
CONTINUE = ["-n", LABEL, "--continue"]


class Repo:
    """A repo on disk for the plain backend's git: worktrees and branches answered from tables."""

    def __init__(self, tmp_path, worktrees=(), branches=(), ignored=False, agents_answer=None, env=None):
        self.root = tmp_path / "shiplog"
        (self.root / ".git" / "info").mkdir(parents=True)
        (self.root / ".git" / "info" / "exclude").write_text("# git ls-files --others --exclude-from=.git/info/exclude\n")
        porcelain = f"worktree {self.root}\nHEAD a\nbranch refs/heads/main\n\n" + "".join(
            f"worktree {path}\nHEAD b\nbranch refs/heads/{branch}\n\n" for path, branch in worktrees)
        self.runner = FakeRunner({
            GIT: ok(porcelain),
            LIST: agents_answer or agents(),
            ("git", "rev-parse", "--git-common-dir"): ok(".git\n"),
            ("git", "rev-parse", "--verify", "--quiet"): lambda argv: ok() if argv[-1].removeprefix("refs/heads/") in branches else fail(1),
            ("git", "check-ignore"): ok() if ignored else fail(1),
            FRONT: ok("[ NULL ]\n"),
        })
        self.now = 100.0
        self.backend = PlainBackend(runner=self.runner, env=env or {"TERM_PROGRAM": "ghostty"}, clock=lambda: self.now,
                                    system="Darwin", which=lambda n: CLAUDE if n == "claude" else None,
                                    kill=lambda pid, sig: pytest.fail("the board stops nothing on s"))

    def start(self, name="issue-12", branch="issue-12"):
        return self.backend.start_in_worktree(self.root, name, branch, LABEL, NEW, CONTINUE)

    def exclude(self):
        return (self.root / ".git" / "info" / "exclude").read_text()


def window_line(runner):
    """What the card's window runs, from Ghostty's AppleScript recipe."""
    return shlex.split(runner.last("osascript").argv[-1])[-1]


def test_a_fresh_card_gets_a_worktree_on_its_branch_from_head_and_claude_with_its_prompt(tmp_path):
    repo = Repo(tmp_path)
    assert repo.start(branch="kare/issue-12-fix") == "new worktree issue-12 in a new Ghostty window"
    path = f"{repo.root}/.claude/worktrees/issue-12"
    add = next(c for c in repo.runner.calls if c.argv[:3] == ["git", "worktree", "add"])
    assert (add.argv, add.cwd) == (["git", "worktree", "add", "-b", "kare/issue-12-fix", path, "HEAD"], str(repo.root))
    assert window_line(repo.runner) == f"cd {path} && exec {shlex.join([CLAUDE, *NEW])}"
    assert "activate" not in repo.runner.last("osascript").argv  # card windows open in the background


def test_a_board_in_a_linked_worktree_puts_new_ones_under_the_main_checkout(tmp_path):
    repo = Repo(tmp_path, worktrees=[(str(tmp_path / "shiplog" / ".claude" / "worktrees" / "issue-9"), "issue-9")])
    repo.runner.answers[("git", "rev-parse", "--git-common-dir")] = ok(f"{repo.root}/.git\n")
    repo.backend.start_in_worktree(repo.root / ".claude" / "worktrees" / "issue-9", "issue-12", "issue-12", LABEL,
                                   NEW, CONTINUE)
    add = next(c for c in repo.runner.calls if c.argv[:3] == ["git", "worktree", "add"])
    assert add.argv[5] == f"{repo.root}/.claude/worktrees/issue-12" and add.cwd == str(repo.root)
    assert repo.exclude().endswith(".claude/worktrees/\n")


def test_without_git_s_worktree_list_nothing_is_started(tmp_path):
    repo = Repo(tmp_path)
    repo.runner.answers[GIT] = fail(128, "fatal: not a git repository")
    with pytest.raises(BackendError, match="git can't list the worktrees of"):
        repo.start()
    assert "osascript" not in [argv[0] for argv in repo.runner.argvs()]


def test_worktrees_are_kept_out_of_git_status_through_info_exclude_once(tmp_path):
    repo = Repo(tmp_path)
    repo.runner.answers[("git", "check-ignore")] = lambda argv: ok() if ".claude/worktrees/" in repo.exclude() else fail(1)
    repo.start()
    repo.now += 60
    repo.start(name="issue-13", branch="issue-13")
    assert repo.exclude().splitlines() == ["# git ls-files --others --exclude-from=.git/info/exclude",
                                           ".claude/worktrees/"]


def test_a_repo_that_already_ignores_the_worktrees_is_left_alone(tmp_path):
    repo = Repo(tmp_path, ignored=True)
    before = repo.exclude()
    repo.start()
    assert repo.exclude() == before


@pytest.mark.parametrize("made_by, branch", [
    ("{root}/.claude/worktrees/issue-12", "issue-12"),  # plain
    ("{home}/.herdr/worktrees/shiplog/issue-12", "issue-12"),  # herdr
    ("{root}/.claude/worktrees/worktree-issue-12", "worktree-issue-12"),  # claude -w
    ("{home}/elsewhere", "kare/issue-12-fix"),  # by its branch
])
def test_an_existing_worktree_of_any_maker_continues_and_none_is_added(tmp_path, made_by, branch):
    path = made_by.format(root=tmp_path / "shiplog", home=tmp_path)
    repo = Repo(tmp_path, worktrees=[(path, branch)], branches=[branch])
    report = repo.start(branch="kare/issue-12-fix")
    assert report == f"worktree {Path(path).name} with claude --continue in a new Ghostty window"
    assert window_line(repo.runner) == f"cd {path} && exec {shlex.join([CLAUDE, *CONTINUE])}"
    assert not any(c.argv[:3] == ["git", "worktree", "add"] for c in repo.runner.calls)


def test_a_leftover_branch_without_a_worktree_is_refused_as_in_herdr(tmp_path):
    repo = Repo(tmp_path, branches=["issue-12"])
    with pytest.raises(BackendError) as caught:
        repo.start()
    assert (caught.value.source, str(caught.value)) == (
        "plain", "branch issue-12 exists without a worktree; check it out in one or delete it, then s again")
    assert not any(c.argv[:3] == ["git", "worktree", "add"] or c.argv[0] == "osascript" for c in repo.runner.calls)


@pytest.mark.parametrize("status", ["busy", "waiting", "idle", "shell", None])
def test_a_session_in_the_worktree_read_just_now_refuses_including_an_unknown_one(tmp_path, status):
    path = str(tmp_path / "shiplog" / ".claude" / "worktrees" / "issue-12")
    repo = Repo(tmp_path, worktrees=[(path, "issue-12")],
                agents_answer=agents({"cwd": f"{path}/src", "pid": 7, "status": status}))
    with pytest.raises(BackendError, match="^live session: press Enter$"):
        repo.start()
    assert "osascript" not in [argv[0] for argv in repo.runner.argvs()]


def test_a_session_elsewhere_does_not_refuse(tmp_path):
    path = str(tmp_path / "shiplog" / ".claude" / "worktrees" / "issue-12")
    repo = Repo(tmp_path, worktrees=[(path, "issue-12")],
                agents_answer=agents({"cwd": str(tmp_path / "shiplog"), "pid": 7, "status": "busy"}))
    assert repo.start().endswith("with claude --continue in a new Ghostty window")


def test_a_second_start_within_30_seconds_of_opening_a_window_is_refused(tmp_path):
    repo = Repo(tmp_path)
    repo.start()
    path = str(repo.root / ".claude" / "worktrees" / "issue-12")
    repo.runner.answers[GIT].stdout += f"worktree {path}\nHEAD b\nbranch refs/heads/issue-12\n\n"
    repo.now += 29
    with pytest.raises(BackendError, match="a session is starting in issue-12: wait for it, then press Enter"):
        repo.start()
    repo.now += 1  # Claude never showed up: the window was closed or failed
    assert repo.start() == "worktree issue-12 with claude --continue in a new Ghostty window"


@pytest.mark.parametrize("answer, error", [(fail(1), "exited 1"), (ok("{"), "unparseable output")])
def test_unreadable_sessions_refuse_with_why(tmp_path, answer, error):
    repo = Repo(tmp_path, agents_answer=answer)
    with pytest.raises(BackendError) as caught:
        repo.start()
    assert (caught.value.source, str(caught.value)) == ("claude agents", f"can't check for a live session: {error}")
    assert not any(c.argv[:3] == ["git", "worktree", "add"] for c in repo.runner.calls)


QUOTED = ["-n", "#12 Don't \"quote\" $HOME `me`", "/implement https://x/issues/12 'and' \\ \"more\""]


@pytest.mark.parametrize("env, recipe", [
    ({"TERM_PROGRAM": "ghostty"}, 'tell application "Ghostty"'),
    ({"TERM_PROGRAM": "iTerm.app"}, 'tell application "iTerm"'),
    ({"TERM_PROGRAM": "Apple_Terminal"}, 'tell application "Terminal"'),
])
def test_claude_args_reach_the_window_intact_through_shlex_and_applescript(tmp_path, env, recipe):
    repo = Repo(tmp_path, env=env)
    repo.backend.start_in_worktree(repo.root, "issue-12", "issue-12", LABEL, QUOTED, CONTINUE)
    launch = repo.runner.last("osascript")
    assert recipe in launch.argv and "on run argv" in launch.argv  # the line is an argument, never AppleScript source
    line = launch.argv[-1] if recipe.endswith('"Terminal"') else shlex.split(launch.argv[-1])[-1]  # sh -c's line
    words = shlex.split(line)
    assert words[:3] == ["cd", f"{repo.root}/.claude/worktrees/issue-12", "&&"]
    assert words[3:] == ["exec", CLAUDE, *QUOTED]


def test_the_board_window_detects_its_terminal_from_its_own_env(tmp_path):
    # The board a window runs has that terminal's env, not Claude's: no CLAUDECODE, its own TERM_PROGRAM.
    repo = Repo(tmp_path, env={"TICKETMASTER_SLOT": "s-1", "TERM_PROGRAM": "iTerm.app"})
    assert repo.start() == "new worktree issue-12 in a new iTerm2 window"
    assert 'tell application "iTerm"' in repo.runner.last("osascript").argv


def test_a_window_that_cannot_open_leaves_the_paste_able_line(tmp_path):
    repo = Repo(tmp_path, env={"TERM_PROGRAM": "ghostty", "SSH_TTY": "/dev/ttys2"})
    with pytest.raises(BackendError) as caught:
        repo.start()
    path = f"{repo.root}/.claude/worktrees/issue-12"
    assert (caught.value.source, str(caught.value)) == (
        "plain", f"couldn't open a terminal window (over SSH); run it yourself: cd {path} && {shlex.join([CLAUDE, *NEW])}")
    repo.runner.answers[GIT].stdout += f"worktree {path}\nHEAD b\nbranch refs/heads/issue-12\n\n"
    repo.backend.env = {"TERM_PROGRAM": "ghostty"}
    assert repo.start().startswith("worktree issue-12 with claude --continue")  # the failed launch is not starting


def test_a_failed_launch_shows_the_paste_able_line_on_the_status_line_and_no_starting_badge(tmp_path):
    repo = Repo(tmp_path, env={"TERM_PROGRAM": "ghostty", "SSH_CONNECTION": "1 2 3 4"})
    spec = spec_of(9, 20)
    ticket = impl_of(spec, 12, "Fix it")
    model = BoardModel(FakeProvider([a_map(9), spec, ticket]), Query(), interval=60,
                       workbench=Workbench(repo.root, repo.backend), starting_dir=tmp_path / "starting")
    model.apply(model.fetch(model.due()))
    assert model.begin_start(ticket) is None
    notice = model.start(ticket)
    assert notice.severity == "error"
    assert notice.text.startswith("plain: couldn't open a terminal window (over SSH); run it yourself: cd ")
    assert notice.text.endswith(shlex.join([CLAUDE, "-n", "#12 Fix it", "/mattpocock-skills:implement u12"]))
    model.start_failed(ticket)  # what a view does with an error notice
    badges = [b.kind for lane in model.snapshot().lanes for _, views in lane.cells for v in views
              if v.ticket.number == 12 for b in v.badges]
    assert "starting" not in badges and not (tmp_path / "starting" / "12").exists()


# --- go_to --------------------------------------------------------------------------------------------------------

def test_go_to_a_live_session_reports_where_it_runs_and_opens_nothing(tmp_path):
    repo = Repo(tmp_path)
    session = Session(12, "/w/issue-12", "issue-12", "blocked", 7, "permission prompt")
    assert repo.backend.go_to(repo.root, session, LABEL) == "running in /w/issue-12"
    assert repo.runner.calls == []


def test_go_to_a_worktree_without_a_session_opens_a_shell_window_there_never_claude(tmp_path):
    repo = Repo(tmp_path, env={"TERM_PROGRAM": "ghostty", "SHELL": "/bin/zsh"})
    assert repo.backend.go_to(repo.root, Session(12, "/w/issue-12", "issue-12"), LABEL) == (
        "opened issue-12 with a shell in a new Ghostty window")
    assert window_line(repo.runner) == "cd /w/issue-12 && exec /bin/zsh"
    assert CLAUDE not in [argv[0] for argv in repo.runner.argvs()]


def test_go_to_a_shell_window_that_cannot_open_leaves_the_paste_able_line(tmp_path):
    repo = Repo(tmp_path, env={"SSH_TTY": "/dev/ttys2"})
    with pytest.raises(BackendError, match=r"\(over SSH\); run it yourself: cd /w/issue-12 && /bin/sh$"):
        repo.backend.go_to(repo.root, Session(12, "/w/issue-12", "issue-12"), LABEL)


def test_go_to_while_sessions_were_never_read_says_so_and_opens_nothing(tmp_path):
    repo = Repo(tmp_path)
    session = Session(12, "/w/issue-12", "issue-12", "unavailable")
    assert repo.backend.go_to(repo.root, session, LABEL) == "can't tell whether a session runs in /w/issue-12"
    assert repo.runner.calls == []


# --- later tickets ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda b: b.cleanup_view(Path(REPO)),
    lambda b: b.release(Path(REPO), "/p"),
])
def test_cleanup_is_refused_with_a_reason_until_it_lands(call):
    with pytest.raises(BackendError, match="lands in a later ticket") as caught:
        call(PlainBackend(runner=FakeRunner(), env={}))
    assert caught.value.source == "plain"
