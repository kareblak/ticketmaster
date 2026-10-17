import signal
from pathlib import Path

import pytest

from fake_plain import FakeRunner, agents, fail, ok
from ticketmaster import backends, cli, config
from ticketmaster.backends import BackendError, SessionsUnreadable, plain
from ticketmaster.backends.plain import PlainBackend
from ticketmaster.session import Session

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
               "CLAUDE_PLUGIN_DATA": "/data/tm", "GHOSTTY_RESOURCES_DIR": "/x", "PATH": "/bin"}


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
    assert launch.argv[-1] == (f"cd {world.repo} && exec /usr/bin/env TICKETMASTER_SLOT=s-1 CLAUDE_PLUGIN_DATA=/data/tm "
                               f"{WRAPPER} open 42 --backend plain")
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


@pytest.mark.parametrize("call", [
    lambda b: b.start_in_worktree(Path(REPO), "issue-1", "issue-1", "l", [], []),
    lambda b: b.go_to(Path(REPO), Session(1, "/p"), "l"),
    lambda b: b.cleanup_view(Path(REPO)),
    lambda b: b.release(Path(REPO), "/p"),
])
def test_start_go_to_and_cleanup_are_refused_with_a_reason_until_they_land(call):
    with pytest.raises(BackendError, match="lands in a later ticket") as caught:
        call(PlainBackend(runner=FakeRunner(), env={}))
    assert caught.value.source == "plain"
