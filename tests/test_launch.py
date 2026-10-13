from pathlib import Path

import pytest

from ticketmaster import launch
from ticketmaster.launch import Placement, candidates

CWD = Path("/repo")


@pytest.fixture(autouse=True)
def tools(monkeypatch):
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/usr/bin/{name}")



def test_ghostty_split_falls_back_to_tab_then_window(monkeypatch):
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    found = candidates(CWD, "tm board", Placement.SPLIT_RIGHT, {"TERM_PROGRAM": "ghostty", "SHELL": "/bin/zsh"})
    assert [(c.terminal, c.placement) for c in found][:3] == [
        ("Ghostty", Placement.SPLIT_RIGHT), ("Ghostty", Placement.TAB), ("Ghostty", Placement.WINDOW),
    ]
    script = found[0].argv[2]
    assert "split (focused terminal of selected tab of front window) direction right" in script
    assert "command:\"/bin/zsh -lc 'tm board'\"" in script
    assert 'initial working directory:"/repo"' in script


def test_ghostty_window_uses_applescript_not_open(monkeypatch):
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    found = candidates(CWD, "tm", Placement.WINDOW, {"TERM_PROGRAM": "ghostty"})
    assert all(c.argv[0] != "open" for c in found)
    assert "new window with configuration" in found[0].argv[2]


def test_tmux_wins_inside_any_terminal(monkeypatch):
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    env = {"TMUX": "/tmp/tmux-1/default,1,0", "TERM_PROGRAM": "ghostty", "SHELL": "/bin/zsh"}
    split, tab = candidates(CWD, "tm board", Placement.SPLIT_RIGHT, env)[:2]
    assert split.argv[:5] == ["tmux", "split-window", "-h", "-c", "/repo"]
    assert tab.argv[:2] == ["tmux", "new-window"]


def test_iterm_tab(monkeypatch):
    found = candidates(CWD, "claude -n x", Placement.TAB, {"TERM_PROGRAM": "iTerm.app", "SHELL": "/bin/zsh"})
    assert found[0].terminal == "iTerm" and "create tab with default profile command" in found[0].argv[2]


def test_wezterm_targets_current_pane(monkeypatch):
    env = {"TERM_PROGRAM": "WezTerm", "WEZTERM_PANE": "7", "SHELL": "/bin/bash"}
    found = candidates(CWD, "tm", Placement.SPLIT_RIGHT, env)
    assert found[0].argv[:6] == ["wezterm", "cli", "split-pane", "--right", "--pane-id", "7"]
    assert found[1].argv[:3] == ["wezterm", "cli", "spawn"]


def test_kitty_uses_remote_control(monkeypatch):
    found = candidates(CWD, "tm", Placement.TAB, {"KITTY_WINDOW_ID": "3", "SHELL": "/bin/sh"})
    assert found[0].argv[:4] == ["kitty", "@", "launch", "--type=tab"]


def test_apple_terminal_has_no_tabs_so_opens_window(monkeypatch):
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    monkeypatch.setattr(launch.Path, "exists", lambda self: False)
    found = candidates(CWD, "tm", Placement.TAB, {"TERM_PROGRAM": "Apple_Terminal"})
    assert [(c.terminal, c.placement) for c in found] == [("Terminal", Placement.WINDOW)]


def test_linux_gnome_terminal_tab(monkeypatch):
    monkeypatch.setattr(launch.sys, "platform", "linux")
    found = candidates(CWD, "tm", Placement.TAB, {"SHELL": "/bin/bash"})
    assert found[0].terminal == "gnome-terminal" and "--tab" in found[0].argv


def test_split_down_per_terminal(monkeypatch):
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    down = Placement.SPLIT_DOWN
    ghostty = candidates(CWD, "c", down, {"TERM_PROGRAM": "ghostty"})[0]
    assert "direction down" in ghostty.argv[2]
    assert candidates(CWD, "c", down, {"TMUX": "x"})[0].argv[:3] == ["tmux", "split-window", "-v"]
    assert "split horizontally" in candidates(CWD, "c", down, {"TERM_PROGRAM": "iTerm.app"})[0].argv[2]
    assert "--bottom" in candidates(CWD, "c", down, {"TERM_PROGRAM": "WezTerm"})[0].argv
    assert "--location=hsplit" in candidates(CWD, "c", down, {"KITTY_WINDOW_ID": "1"})[0].argv
