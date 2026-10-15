import subprocess
from pathlib import Path

import pytest

from fake_herdr import FakeHerdr
from ticketmaster import cleanup, cli, config, herdr
from ticketmaster.cleanup import Leftover
from ticketmaster.herdr import Herdr
from ticketmaster.model import Ticket
from ticketmaster.providers import Query

REAL_LOCAL_BRANCHES = herdr.local_branches

REPO = Path("/code/shiplog")
HERDR_HOME = "/home/me/.herdr/worktrees/shiplog"
CLAUDE_HOME = "/code/shiplog/.claude/worktrees"
MAP_LABELS = ("wayfinder:map",)


def issue(number, **kw):
    return Ticket(**{"number": number, "title": f"T{number}", "url": f"https://gh/o/r/issues/{number}",
                     "is_open": True, **kw})


def on_map(number, kind="grilling", **kw):
    return issue(number, labels=(f"wayfinder:{kind}",), parent_number=40, parent_url="https://gh/o/r/issues/40",
                 parent_labels=MAP_LABELS, **kw)


MAP = issue(40, title="Ship deviations", is_open=False, labels=MAP_LABELS)
DECIDED = on_map(41, is_open=False)
RESEARCH = on_map(42, kind="research")
SPEC = issue(50, body="Part of #40", is_open=False, parent_number=40, map_number=40,
             map_url="https://gh/o/r/issues/40")
MERGED = issue(51, body="## Parent\n#50", parent_number=50, map_number=40, has_merged_change_request=True)
IN_REVIEW = issue(52, body="## Parent\n#50", parent_number=50, map_number=40, has_open_change_request=True)
OTHER_MAP = issue(60, labels=MAP_LABELS)
OTHER = issue(61, labels=("wayfinder:task",), parent_number=60, parent_labels=MAP_LABELS)
TICKETS = [MAP, DECIDED, RESEARCH, SPEC, MERGED, IN_REVIEW, OTHER_MAP, OTHER]


def wt(path, branch, workspace=None):
    return {"path": path, "branch": branch, "is_linked_worktree": True, "open_workspace_id": workspace}


WORKTREES = {"worktrees": [
    {"path": str(REPO), "branch": "main", "is_linked_worktree": False, "open_workspace_id": "w1"},
    wt(f"{HERDR_HOME}/issue-40", "issue-40", "w2"),
    wt(f"{CLAUDE_HOME}/worktree-issue-41", "worktree-issue-41"),
    wt(f"{HERDR_HOME}/issue-42", "issue-42", "w5"),
    wt(f"{HERDR_HOME}/issue-51", "issue-51"),
    wt(f"{CLAUDE_HOME}/issue-52", "issue-52"),
    wt(f"{HERDR_HOME}/issue-61", "issue-61"),
]}
AGENTS = {"agents": [
    {"pane_id": "w5:p1", "workspace_id": "w5", "agent_status": "working", "foreground_cwd": f"{HERDR_HOME}/issue-42"},
    {"pane_id": "w1:p2", "workspace_id": "w1", "agent_status": "idle", "cwd": str(REPO),
     "terminal_title": "#42 Research the carrier API"},
    {"pane_id": "w9:p1", "workspace_id": "w9", "agent_status": "idle", "foreground_cwd": f"{HERDR_HOME}/issue-61"},
]}
DIRTY = {f"{CLAUDE_HOME}/worktree-issue-41"}


@pytest.fixture(autouse=True)
def branches(monkeypatch):
    """issue-50 is left without a worktree; issue-62 is not this map's."""
    monkeypatch.setattr(herdr, "local_branches", lambda cwd: ["main", "issue-40", "issue-50", "issue-62"])


def fake():
    return FakeHerdr({("worktree", "list"): WORKTREES, ("agent", "list"): AGENTS})


def leftovers(fake_herdr=None, tickets=TICKETS):
    return cleanup.leftovers(REPO, MAP, tickets, Herdr(fake_herdr or fake()), uncommitted=lambda path: path in DIRTY)


def by_place(found: list[Leftover]) -> dict[str, Leftover]:
    return {entry.path or f"branch {entry.branch}": entry for entry in found}


def test_lists_the_maps_and_its_lane_tickets_worktrees_in_both_homes_and_leftover_branches():
    assert list(by_place(leftovers())) == [
        f"{HERDR_HOME}/issue-40", f"{CLAUDE_HOME}/worktree-issue-41", f"{HERDR_HOME}/issue-42",
        f"{HERDR_HOME}/issue-51", f"{CLAUDE_HOME}/issue-52", "branch issue-50", str(REPO),
    ]


def test_another_maps_worktree_agent_and_branch_are_not_listed():
    found = leftovers()
    assert all(entry.ticket.number in (40, 41, 42, 50, 51, 52) for entry in found)
    assert "w9" not in {w for entry in found for w in entry.workspaces}


def test_a_closed_ticket_with_a_clean_worktree_is_removable():
    entry = by_place(leftovers())[f"{HERDR_HOME}/issue-40"]
    assert (entry.removable, entry.reason) == (True, "ticket closed")
    assert (entry.branch, entry.workspaces) == ("issue-40", ("w2",))


def test_uncommitted_changes_keep_a_closed_tickets_worktree():
    entry = by_place(leftovers())[f"{CLAUDE_HOME}/worktree-issue-41"]
    assert (entry.removable, entry.reason) == (False, "uncommitted changes")


def test_an_open_ticket_is_kept_with_its_workspace_and_agent():
    entry = by_place(leftovers())[f"{HERDR_HOME}/issue-42"]
    assert (entry.removable, entry.reason) == (False, "open ticket")
    assert (entry.workspaces, entry.agents) == (("w5",), ("working",))


def test_a_merged_pr_makes_an_open_tickets_worktree_removable():
    """Merged comes from the tracker's PR state, never from unpushed commits: a squash merge whose remote branch
    was deleted leaves every local commit unpushed."""
    entry = by_place(leftovers())[f"{HERDR_HOME}/issue-51"]
    assert (entry.removable, entry.reason) == (True, "PR merged")


def test_an_unmerged_pr_keeps_the_worktree():
    entry = by_place(leftovers())[f"{CLAUDE_HOME}/issue-52"]
    assert (entry.removable, entry.reason) == (False, "unmerged PR")


def test_reasons_add_up():
    found = cleanup.leftovers(REPO, MAP, TICKETS, Herdr(fake()), uncommitted=lambda path: True)
    assert by_place(found)[f"{HERDR_HOME}/issue-42"].reason == "open ticket, uncommitted changes"


def test_unreadable_git_status_keeps_the_worktree():
    found = cleanup.leftovers(REPO, MAP, TICKETS, Herdr(fake()), uncommitted=lambda path: None)
    entry = by_place(found)[f"{HERDR_HOME}/issue-40"]
    assert (entry.removable, entry.reason) == (False, "git status unreadable")


def test_a_leftover_branch_without_a_worktree_is_listed_by_its_ticket():
    entry = by_place(leftovers())["branch issue-50"]
    assert (entry.ticket, entry.path, entry.removable, entry.reason) == (SPEC, None, True, "ticket closed")


def test_an_agent_titled_with_a_lane_ticket_outside_its_worktree_is_kept():
    entry = by_place(leftovers())[str(REPO)]
    assert entry.ticket == RESEARCH
    assert (entry.removable, entry.reason) == (False, "open ticket, agent outside a ticket worktree")
    assert (entry.workspaces, entry.agents) == (("w1",), ("idle",))


def test_a_claude_worktree_with_a_descriptive_suffix_is_its_tickets():
    suffixed = wt(f"{CLAUDE_HOME}/issue-42-carrier-api", "worktree-issue-42-carrier-api")
    spy = FakeHerdr({("worktree", "list"): {"worktrees": [*WORKTREES["worktrees"], suffixed]},
                     ("agent", "list"): {"agents": []}})
    entry = by_place(leftovers(spy))[suffixed["path"]]
    assert (entry.ticket, entry.removable, entry.reason) == (RESEARCH, False, "open ticket")


def test_every_agent_in_a_listed_worktree_is_listed_with_it_whatever_its_title():
    """A worktree found by the tracker's branch alone, with an agent whose title names no ticket."""
    linear = on_map(43, kind="task", is_open=False, key="TM-43", branch_name="me/tm-43-pick-a-carrier")
    checkout = wt("/code/elsewhere", "me/tm-43-pick-a-carrier")
    spy = FakeHerdr({
        ("worktree", "list"): {"worktrees": [*WORKTREES["worktrees"], checkout]},
        ("agent", "list"): {"agents": [{"pane_id": "w7:p1", "workspace_id": "w7", "agent_status": "idle",
                                        "foreground_cwd": "/code/elsewhere", "terminal_title": "Pick a carrier"}]},
    })
    entry = by_place(leftovers(spy, [*TICKETS, linear]))["/code/elsewhere"]
    assert (entry.ticket, entry.workspaces, entry.agents) == (linear, ("w7",), ("idle",))


def test_a_ticket_branch_checked_out_in_the_main_checkout_is_not_left_over():
    main_on_50 = {"worktrees": [{**WORKTREES["worktrees"][0], "branch": "issue-50"}, *WORKTREES["worktrees"][1:]]}
    spy = FakeHerdr({("worktree", "list"): main_on_50, ("agent", "list"): AGENTS})
    assert "branch issue-50" not in by_place(leftovers(spy))


def test_an_open_pr_keeps_the_worktree_beside_a_merged_one():
    both = [t if t.number != 51 else issue(51, body="## Parent\n#50", parent_number=50, map_number=40,
                                           has_merged_change_request=True, has_open_change_request=True)
            for t in TICKETS]
    entry = by_place(leftovers(tickets=both))[f"{HERDR_HOME}/issue-51"]
    assert (entry.removable, entry.reason) == (False, "unmerged PR")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_has_uncommitted_reads_git_status_and_says_none_when_git_cannot(tmp_path):
    git("init", "-q", cwd=tmp_path)
    assert cleanup.has_uncommitted(str(tmp_path)) is False
    (tmp_path / "new.txt").write_text("x")
    assert cleanup.has_uncommitted(str(tmp_path)) is True
    assert cleanup.has_uncommitted(str(tmp_path / "gone")) is None


def test_the_dry_run_only_lists():
    spy = fake()
    leftovers(spy)
    assert set(spy.verbs()) <= {("worktree", "list"), ("agent", "list")}


def test_render_marks_each_entry_and_says_nothing_changed():
    text = cleanup.render(MAP, leftovers(), home=Path("/home/me"))
    lines = text.splitlines()
    assert lines[0] == "#40 Ship deviations (closed): dry run, nothing changed"
    assert "removable  #40  worktree ~/.herdr/worktrees/shiplog/issue-40, branch issue-40, workspace w2 · ticket closed" in lines
    assert "kept       #42  worktree ~/.herdr/worktrees/shiplog/issue-42, branch issue-42, workspace w5, agent working · open ticket" in lines
    assert "removable  #50  branch issue-50 · ticket closed" in lines
    assert lines[-1] == "3 removable, 4 kept"


def test_render_with_nothing_left_behind():
    assert cleanup.render(MAP, []).splitlines()[1] == "Nothing left behind."


class FakeProvider:
    name = "github"
    repo = "o/r"

    def __init__(self, tickets):
        self.tickets = tickets
        self.queries = []

    def fetch(self, query):
        self.queries.append(query)
        return self.tickets


def test_dry_run_fetches_the_named_map_even_when_closed():
    provider = FakeProvider(TICKETS)
    text = cleanup.dry_run(REPO, provider, 40, Herdr(fake()), uncommitted=lambda path: False)
    assert provider.queries == [Query(named_map=40)]
    assert text.startswith("#40 Ship deviations (closed)")


def test_dry_run_on_a_linear_key_matches_the_maps_key():
    linear_map = issue(7, title="Linear map", labels=MAP_LABELS, key="TM-7")
    text = cleanup.dry_run(REPO, FakeProvider([linear_map]), "TM-7", Herdr(fake()), uncommitted=lambda path: False)
    assert text.startswith("TM-7 Linear map (open)")


@pytest.mark.parametrize("named", [41, 99, "ENG-4"])
def test_dry_run_on_something_other_than_a_map_says_so(named):
    with pytest.raises(cleanup.CleanupError, match="not a wayfinder map"):
        cleanup.dry_run(REPO, FakeProvider(TICKETS), named, Herdr(fake()))


def test_cli_parses_cleanup_with_its_map_and_dry_run():
    args = cli.parse_args(["cleanup", "#40", "--dry-run"])
    assert (args.command, args.map, args.dry_run) == ("cleanup", 40, True)
    assert cli.parse_args(["cleanup", "TM-7", "--dry-run"]).map == "TM-7"


def test_cli_parses_cleanup_without_dry_run():
    args = cli.parse_args(["cleanup", "40"])
    assert (args.command, args.map, args.dry_run) == ("cleanup", 40, False)


@pytest.mark.parametrize("argv, error", [
    (["cleanup", "--dry-run"], "cleanup needs a map"),
    (["cleanup"], "cleanup needs a map"),
    (["open", "40", "--dry-run"], "--dry-run is for cleanup"),
])
def test_cli_refuses_cleanup_without_a_map_or_dry_run(argv, error, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    with pytest.raises(SystemExit):
        cli.parse_args(argv)
    assert error in capsys.readouterr().err


def run_main(monkeypatch, tickets, argv):
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(config, "resolve_provider", lambda *args: FakeProvider(tickets))
    monkeypatch.setattr(herdr, "Herdr", lambda: Herdr(fake()))
    monkeypatch.setattr(cleanup, "has_uncommitted", lambda path: False)
    return cli.main(argv)


def test_main_prints_the_dry_run(monkeypatch, capsys):
    assert run_main(monkeypatch, TICKETS, ["cleanup", "40", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("#40 Ship deviations (closed): dry run, nothing changed\n")
    assert "removable  #40" in out


def test_main_on_no_map_fails_with_the_reason(monkeypatch, capsys):
    assert run_main(monkeypatch, TICKETS, ["cleanup", "41", "--dry-run"]) == 1
    assert "ticketmaster: #41 is not a wayfinder map in github:o/r" in capsys.readouterr().err


def test_an_agent_in_the_pane_cleanup_runs_in_keeps_its_worktree():
    found = cleanup.leftovers(REPO, MAP, TICKETS, Herdr(fake()), uncommitted=lambda path: False, own_pane="w5:p1")
    entry = by_place(found)[f"{HERDR_HOME}/issue-42"]
    assert "cleanup runs in it" in entry.reason


def test_the_worktree_cleanup_runs_in_is_kept():
    found = cleanup.leftovers(Path(f"{HERDR_HOME}/issue-40/sub"), MAP, TICKETS, Herdr(fake()),
                              uncommitted=lambda path: False)
    entry = by_place(found)[f"{HERDR_HOME}/issue-40"]
    assert (entry.removable, entry.reason) == (False, "cleanup runs in it")


# --- cleanup without --dry-run, on a real git repo -------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """main, with worktrees issue-40 (merged into main), issue-51 (its PR squash-merged, so unmerged in git) and
    issue-42 (open ticket), and branch issue-50 merged with no worktree."""
    monkeypatch.setattr(herdr, "local_branches", REAL_LOCAL_BRANCHES)
    root = tmp_path / "shiplog"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "start", cwd=root)
    for branch in ("issue-40", "issue-50"):
        git("branch", branch, cwd=root)
    for name in ("issue-40", "issue-51", "issue-42"):
        args = ["worktree", "add", "-q", str(tmp_path / name)]
        git(*args, *(["-b", name] if name != "issue-40" else [name]), cwd=root)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "work",
        cwd=tmp_path / "issue-51")
    return root


def real_fake(repo, agents=()):
    home = repo.parent
    return FakeHerdr({
        ("worktree", "list"): {"worktrees": [
            {"path": str(repo), "branch": "main", "is_linked_worktree": False, "open_workspace_id": "w1"},
            wt(str(home / "issue-40"), "issue-40", "w2"),
            wt(str(home / "issue-51"), "issue-51", "w3"),
            wt(str(home / "issue-42"), "issue-42", "w5"),
        ]},
        ("agent", "list"): {"agents": list(agents)},
    })


def run_cleanup(repo, spy, tickets=TICKETS, own_pane=None):
    return cleanup.clean(repo, FakeProvider(tickets), 40, Herdr(spy), own_pane=own_pane)


def test_cleanup_removes_a_removable_worktree_its_workspace_and_its_merged_branch(repo):
    spy = real_fake(repo)
    run_cleanup(repo, spy)
    assert not (repo.parent / "issue-40").exists()
    assert "issue-40" not in REAL_LOCAL_BRANCHES(repo)
    assert ["workspace", "close", "w2"] in spy.calls


def test_cleanup_deletes_a_merged_branch_left_without_a_worktree(repo):
    run_cleanup(repo, real_fake(repo))
    assert "issue-50" not in REAL_LOCAL_BRANCHES(repo)


def test_cleanup_never_deletes_a_branch_git_calls_unmerged(repo):
    """The tracker says merged (a squash merge), git does not: the worktree goes, the branch stays."""
    text, _ = run_cleanup(repo, real_fake(repo))
    assert not (repo.parent / "issue-51").exists()
    assert "issue-51" in REAL_LOCAL_BRANCHES(repo)
    line = next(line for line in text.splitlines() if "#51" in line)
    assert line.startswith("removed")
    assert "branch issue-51 kept: not merged in git" in line


def test_cleanup_leaves_kept_entries_alone_and_lists_them(repo):
    spy = real_fake(repo, [{"pane_id": "w5:p1", "workspace_id": "w5", "agent_status": "working",
                            "foreground_cwd": str(repo.parent / "issue-42")}])
    text, _ = run_cleanup(repo, spy)
    assert (repo.parent / "issue-42").exists()
    assert "issue-42" in REAL_LOCAL_BRANCHES(repo)
    assert not any("w5" in call for call in spy.calls if call[1] == "close")
    assert any(line.startswith("kept") and "#42" in line and "open ticket" in line for line in text.splitlines())


def test_cleanup_closes_an_agent_in_another_workspace_by_its_pane(repo):
    spy = real_fake(repo, [{"pane_id": "w8:p3", "workspace_id": "w8", "agent_status": "idle",
                            "foreground_cwd": str(repo.parent / "issue-40")}])
    run_cleanup(repo, spy)
    assert ["pane", "close", "w8:p3"] in spy.calls
    assert ["workspace", "close", "w8"] not in spy.calls


def test_cleanup_never_forces_a_worktree_with_new_changes_out(repo):
    """Changes made after the look: git refuses to remove the worktree, which keeps its branch too."""
    (repo.parent / "issue-40" / "new.txt").write_text("x")
    spy = real_fake(repo)
    found = cleanup.leftovers(repo, MAP, TICKETS, Herdr(spy), uncommitted=lambda path: False)
    outcomes = cleanup.remove(repo, found, Herdr(spy))
    assert (repo.parent / "issue-40" / "new.txt").exists()
    assert "issue-40" in REAL_LOCAL_BRANCHES(repo)
    entry = next(o for o in outcomes if o.entry.ticket.number == 40)
    assert entry.failed and "worktree kept: git worktree remove refused" in entry.note
    assert not any("--force" in call or "-D" in call for call in spy.calls)


def test_cleanup_summary_shows_what_was_removed_and_kept(repo):
    text, ok = run_cleanup(repo, real_fake(repo))
    lines = text.splitlines()
    assert lines[0] == "#40 Ship deviations (closed): cleaned up"
    assert any(line.startswith("removed") and "#40" in line and "branch issue-40" in line and "workspace w2" in line
               for line in lines)
    assert lines[-1] == "3 removed, 1 kept"
    assert ok


def test_cleanup_on_an_open_map_refuses_and_changes_nothing(repo):
    open_map = issue(40, title="Ship deviations", labels=MAP_LABELS)
    spy = real_fake(repo)
    with pytest.raises(cleanup.CleanupError, match="#40 is still open: close the map in the tracker first"):
        run_cleanup(repo, spy, tickets=[open_map, *TICKETS[1:]])
    assert spy.calls == []
    assert (repo.parent / "issue-40").exists()


def test_main_cleanup_on_an_open_map_exits_non_zero(monkeypatch, capsys):
    open_map = issue(40, title="Ship deviations", labels=MAP_LABELS)
    assert run_main(monkeypatch, [open_map, *TICKETS[1:]], ["cleanup", "40"]) == 1
    assert "still open" in capsys.readouterr().err


def test_cleanup_closes_herdr_before_removing_the_worktree(repo):
    """No live agent has its directory removed under it."""
    seen = {}
    spy = real_fake(repo)
    spy.answers[("workspace", "close")] = lambda argv: seen.setdefault(argv[2], (repo.parent / "issue-40").exists())
    run_cleanup(repo, spy)
    assert seen["w2"] is True


def test_a_refused_herdr_close_fails_the_cleanup_and_main_exits_non_zero(repo, monkeypatch, capsys):
    spy = real_fake(repo)
    spy.answers[("workspace", "close")] = herdr.HerdrError("workspace_not_found")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(config, "resolve_provider", lambda *args: FakeProvider(TICKETS))
    monkeypatch.setattr(herdr, "Herdr", lambda: Herdr(spy))
    assert cli.main(["cleanup", "40"]) == 1
    assert "workspace w2 not closed: workspace_not_found" in capsys.readouterr().out


def test_the_workspace_cleanup_runs_in_is_kept(repo):
    text, _ = run_cleanup(repo, real_fake(repo), own_pane="w2:p4")
    assert (repo.parent / "issue-40").exists()
    assert any(line.startswith("kept") and "#40" in line and "cleanup runs in it" in line for line in text.splitlines())
