import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from fake_backend import FakeBackend
from fake_herdr import FakeHerdr
from ticketmaster import backends, cli, config
from ticketmaster.backends import ChoiceError, herdr
from ticketmaster.backends.herdr import Herdr, HerdrBackend

REPO = Path("/code/shiplog")


def fake_module(name, reason=None):
    return SimpleNamespace(NAME=name, unavailable=lambda env: reason, make=lambda cwd, config, env: name)


@pytest.fixture
def two(monkeypatch):
    """`first` is unavailable here, so auto-detect takes `second`."""
    monkeypatch.setattr(backends, "BACKENDS", (fake_module("first", "not here"), fake_module("second"),
                                               fake_module("third")))


@pytest.mark.parametrize("flag, env, toml, chosen", [
    ("third", {"TICKETMASTER_BACKEND": "second"}, {"backend": "second"}, "third"),
    (None, {"TICKETMASTER_BACKEND": "third"}, {"backend": "second"}, "third"),
    (None, {}, {"backend": "third"}, "third"),
    (None, {}, {}, "second"),
])
def test_the_flag_beats_the_env_which_beats_the_toml_which_beats_auto_detect(two, flag, env, toml, chosen):
    assert backends.choose(flag, env, toml).NAME == chosen


def test_a_named_backend_that_is_unavailable_is_an_error_never_a_fallback(two):
    with pytest.raises(ChoiceError, match="^backend first unavailable: not here$"):
        backends.choose("first", {}, {})


def test_an_unknown_backend_lists_the_known_ones(two):
    with pytest.raises(ChoiceError, match="unknown backend 'tmux'; known backends: first, second, third"):
        backends.choose(None, {"TICKETMASTER_BACKEND": "tmux"}, {})


def test_herdr_is_the_only_backend_and_says_why_it_is_unavailable(monkeypatch):
    assert [b.NAME for b in backends.BACKENDS] == ["herdr"]
    monkeypatch.setattr(herdr.shutil, "which", lambda name: None)
    assert herdr.unavailable({}) == "HERDR_ENV is not 1"
    assert herdr.unavailable({"HERDR_ENV": "1"}) == "herdr not on PATH"


@pytest.fixture
def outside_herdr(monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("TICKETMASTER_BACKEND", raising=False)
    monkeypatch.setattr(config, "resolve_provider", lambda *args: pytest.fail("no provider before the backend"))


@pytest.mark.parametrize("argv", [["--backend", "herdr"], ["cleanup", "40", "--backend", "herdr"]])
def test_forcing_herdr_outside_herdr_exits_2_with_the_reason(outside_herdr, argv, capsys):
    assert cli.main(argv) == 2
    assert capsys.readouterr().err == "ticketmaster: backend herdr unavailable: HERDR_ENV is not 1\n"


def test_forcing_herdr_without_herdr_on_path_exits_2_with_the_reason(outside_herdr, monkeypatch, capsys):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(herdr.shutil, "which", lambda name: None)
    assert cli.main(["--backend", "herdr"]) == 2
    assert capsys.readouterr().err == "ticketmaster: backend herdr unavailable: herdr not on PATH\n"


def test_an_unknown_backend_exits_2_listing_the_known_ones(outside_herdr, monkeypatch, capsys):
    monkeypatch.setenv("TICKETMASTER_BACKEND", "tmux")
    assert cli.main(["open"]) == 2
    assert capsys.readouterr().err == "ticketmaster: unknown backend 'tmux'; known backends: herdr\n"


def test_a_forced_backend_reaches_the_open_rerun_in_the_board_pane(monkeypatch):
    monkeypatch.setenv(herdr.CLAUDE_ENV, "1")
    monkeypatch.delenv(herdr.BOARD_ENV, raising=False)
    monkeypatch.setattr(herdr, "repo_root", lambda cwd: cwd)
    args = cli.parse_args(["680", "--backend", "herdr"])
    assert cli.passthrough(args) == ["680", "--backend", "herdr"]
    fake = FakeHerdr({
        ("pane", "list"): {"panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "cwd": str(REPO), "focused": True}]},
        ("pane", "split"): {"pane": {"pane_id": "w1:p2"}},
    })
    backend = HerdrBackend(Herdr(fake), env={"HERDR_PANE_ID": "w1:p1"})
    report = backend.open_board(REPO, cli.passthrough(args))
    assert report == "Board opened in herdr pane w1:p2 (ticketmaster), split off w1:p1"
    run = fake.first("pane", "run")
    assert run[3].endswith("open 680 --backend herdr")


def test_main_reports_where_the_backend_opened_the_board(monkeypatch, capsys):
    class Opening(FakeBackend):
        def open_board(self, cwd, args):
            super().open_board(cwd, args)
            return "Board opened somewhere"

    opening = Opening()
    monkeypatch.setattr(cli, "resolve", lambda cwd, flag, config: opening)
    monkeypatch.setattr(config, "resolve_provider", lambda *args: SimpleNamespace(name="github", repo="o/r"))
    assert cli.main(["42", "--interval", "9"]) == 0
    assert capsys.readouterr().out == "Board opened somewhere, for github:o/r.\n"
    assert opening.boards == [["42", "--interval", "9"]]


def imported(module: Path) -> set[str]:
    """Every module a source file imports, relative ones resolved within the package."""
    names = set()
    for node in ast.walk(ast.parse(module.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = ("ticketmaster." if node.level else "") + (node.module or "")
            names.add(base)
            names |= {f"{base}.{alias.name}".replace("..", ".") for alias in node.names}
    return names


@pytest.mark.parametrize("name", ["board", "start", "cleanup", "cli"])
def test_the_board_start_cleanup_and_cli_import_nothing_from_herdr(name):
    source = Path(__file__).parent.parent / "ticketmaster" / f"{name}.py"
    assert not [m for m in imported(source) if "herdr" in m]
