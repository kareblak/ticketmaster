"""Open a command beside the caller: a split pane, a new tab, or a new window.

The terminal is detected from the environment the caller runs in (tmux, Ghostty, iTerm2,
WezTerm, kitty, Apple Terminal, common Linux terminals). Each placement falls back to the next
one when the terminal can't do it: split → tab → window.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Placement(Enum):
    SPLIT_RIGHT = "split right"
    SPLIT_DOWN = "split down"
    TAB = "tab"
    WINDOW = "window"

    @property
    def is_split(self) -> bool:
        return self in (Placement.SPLIT_RIGHT, Placement.SPLIT_DOWN)


FALLBACKS = {
    Placement.SPLIT_RIGHT: [Placement.SPLIT_RIGHT, Placement.TAB, Placement.WINDOW],
    Placement.SPLIT_DOWN: [Placement.SPLIT_DOWN, Placement.TAB, Placement.WINDOW],
    Placement.TAB: [Placement.TAB, Placement.WINDOW],
    Placement.WINDOW: [Placement.WINDOW],
}


@dataclass(frozen=True)
class Launch:
    terminal: str
    placement: Placement
    argv: list[str]


def board_command(entrypoint: Path, args: list[str]) -> str:
    return shlex.join([str(entrypoint), "board", *args])


def applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def terminal_program(env: Mapping[str, str]) -> str:
    """TERM_PROGRAM, or a guess from terminal-specific variables when a host strips it
    (Claude Code's background session host does)."""
    if env.get("TERM_PROGRAM"):
        return env["TERM_PROGRAM"]
    markers = [
        ("GHOSTTY_RESOURCES_DIR", "ghostty"),
        ("ITERM_SESSION_ID", "iTerm.app"),
        ("WEZTERM_PANE", "WezTerm"),
        ("TERM_SESSION_ID", "Apple_Terminal"),
    ]
    return next((program for var, program in markers if env.get(var)), "")


def login_shell(env: Mapping[str, str]) -> str:
    return env.get("SHELL") or "/bin/sh"


def candidates(cwd: Path, command: str, placement: Placement, env: Mapping[str, str] | None = None) -> list[Launch]:
    """Launch commands for the caller's terminal, in fallback order."""
    env = os.environ if env is None else env
    shell = login_shell(env)
    in_shell = [shell, "-lc", command]
    shell_line = f"cd {shlex.quote(str(cwd))} && {command}"
    program = terminal_program(env)
    out: list[Launch] = []

    for step in FALLBACKS[placement]:
        if env.get("TMUX") and shutil.which("tmux"):
            tmux = {
                Placement.SPLIT_RIGHT: ["tmux", "split-window", "-h", "-c", str(cwd), shlex.join(in_shell)],
                Placement.SPLIT_DOWN: ["tmux", "split-window", "-v", "-c", str(cwd), shlex.join(in_shell)],
                Placement.TAB: ["tmux", "new-window", "-c", str(cwd), shlex.join(in_shell)],
            }
            if step in tmux:
                out.append(Launch("tmux", step, tmux[step]))
                continue

        if program == "ghostty" and sys.platform == "darwin":
            out.append(Launch("Ghostty", step, ["osascript", "-e", ghostty_script(step, cwd, shlex.join(in_shell))]))
            continue

        if program == "iTerm.app":
            out.append(Launch("iTerm", step, ["osascript", "-e", iterm_script(step, shlex.join([shell, "-lc", shell_line]))]))
            continue

        if program == "WezTerm" and shutil.which("wezterm"):
            pane = ["--pane-id", env["WEZTERM_PANE"]] if env.get("WEZTERM_PANE") else []
            wezterm = {
                Placement.SPLIT_RIGHT: ["wezterm", "cli", "split-pane", "--right", *pane, "--cwd", str(cwd), "--", *in_shell],
                Placement.SPLIT_DOWN: ["wezterm", "cli", "split-pane", "--bottom", *pane, "--cwd", str(cwd), "--", *in_shell],
                Placement.TAB: ["wezterm", "cli", "spawn", *pane, "--cwd", str(cwd), "--", *in_shell],
                Placement.WINDOW: ["wezterm", "cli", "spawn", "--new-window", "--cwd", str(cwd), "--", *in_shell],
            }
            out.append(Launch("WezTerm", step, wezterm[step]))
            continue

        if env.get("KITTY_WINDOW_ID") and shutil.which("kitty"):
            kind = {
                Placement.SPLIT_RIGHT: ["--type=window", "--location=vsplit"],
                Placement.SPLIT_DOWN: ["--type=window", "--location=hsplit"],
                Placement.TAB: ["--type=tab"],
                Placement.WINDOW: ["--type=os-window"],
            }[step]
            out.append(Launch("kitty", step, ["kitty", "@", "launch", *kind, f"--cwd={cwd}", *in_shell]))
            continue

        if step is Placement.TAB and shutil.which("gnome-terminal") and sys.platform != "darwin":
            out.append(Launch("gnome-terminal", step, ["gnome-terminal", "--tab", f"--working-directory={cwd}", "--", *in_shell]))
            continue

        if step is Placement.WINDOW:
            out += window_launchers(cwd, command, in_shell, shell_line, program)

    unique: list[Launch] = []
    for launch in out:
        if (launch.terminal, launch.placement) not in [(u.terminal, u.placement) for u in unique]:
            unique.append(launch)
    return unique


def window_launchers(cwd: Path, command: str, in_shell: list[str], shell_line: str, program: str) -> list[Launch]:
    found: list[Launch] = []
    if sys.platform == "darwin":
        if Path("/Applications/Ghostty.app").exists():
            found.append(Launch("Ghostty", Placement.WINDOW, ["osascript", "-e", ghostty_script(Placement.WINDOW, cwd, shlex.join(in_shell))]))
        if Path("/Applications/iTerm.app").exists():
            found.append(Launch("iTerm", Placement.WINDOW, ["osascript", "-e", iterm_script(Placement.WINDOW, shlex.join(["/bin/zsh", "-lc", shell_line]))]))
        terminal = Launch("Terminal", Placement.WINDOW, [
            "osascript", "-e", f'tell application "Terminal" to do script {applescript_string(shell_line)}',
            "-e", 'tell application "Terminal" to activate',
        ])
        preferred = {"ghostty": "Ghostty", "iTerm.app": "iTerm", "Apple_Terminal": "Terminal"}.get(program)
        found.append(terminal)
        found.sort(key=lambda launch: launch.terminal != preferred)
        return found
    for name, argv in [
        ("ghostty", ["ghostty", f"--working-directory={cwd}", "-e", *in_shell]),
        ("gnome-terminal", ["gnome-terminal", "--window", f"--working-directory={cwd}", "--", *in_shell]),
        ("konsole", ["konsole", "--workdir", str(cwd), "-e", *in_shell]),
        ("x-terminal-emulator", ["x-terminal-emulator", "-e", "sh", "-lc", shell_line]),
    ]:
        if shutil.which(argv[0]):
            found.append(Launch(name, Placement.WINDOW, argv))
    return found


def ghostty_script(placement: Placement, cwd: Path, command: str) -> str:
    """Ghostty 1.3+ AppleScript. Targets the focused pane of the front window, i.e. where the user is."""
    config = f"{{command:{applescript_string(command)}, initial working directory:{applescript_string(str(cwd))}}}"
    action = {
        Placement.SPLIT_RIGHT: f"split (focused terminal of selected tab of front window) direction right with configuration {config}",
        Placement.SPLIT_DOWN: f"split (focused terminal of selected tab of front window) direction down with configuration {config}",
        Placement.TAB: f"new tab in front window with configuration {config}",
        Placement.WINDOW: f"new window with configuration {config}",
    }[placement]
    if placement is not Placement.WINDOW:
        action = f"if (count of windows) is 0 then error \"no Ghostty window\"\n{action}"
    return f'tell application "Ghostty"\n{action}\nend tell'


def iterm_script(placement: Placement, command: str) -> str:
    quoted = applescript_string(command)
    action = {
        Placement.SPLIT_RIGHT: f"tell current session of current window to split vertically with default profile command {quoted}",
        Placement.SPLIT_DOWN: f"tell current session of current window to split horizontally with default profile command {quoted}",
        Placement.TAB: f"tell current window to create tab with default profile command {quoted}",
        Placement.WINDOW: f"create window with default profile command {quoted}",
    }[placement]
    return f'tell application "iTerm"\n{action}\nend tell'


def open_beside(cwd: Path, command: str, placement: Placement) -> Launch | None:
    """Runs the first launcher that succeeds. None when nothing worked (caller prints the command)."""
    for launch in candidates(cwd, command, placement):
        try:
            result = subprocess.run(launch.argv, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return launch
    return None


def describe(launch: Launch) -> str:
    return {
        Placement.SPLIT_RIGHT: f"a {launch.terminal} pane to the right",
        Placement.SPLIT_DOWN: f"a {launch.terminal} pane below",
        Placement.TAB: f"a new {launch.terminal} tab",
        Placement.WINDOW: f"a new {launch.terminal} window",
    }[launch.placement]
