"""Open the board in a new terminal window, preferring the terminal the caller runs in."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def board_command(entrypoint: Path, args: list[str]) -> str:
    return shlex.join([str(entrypoint), "board", *args])


def applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def launchers(cwd: Path, command: str) -> list[tuple[str, list[str]]]:
    """Candidate launch commands, best first."""
    shell_command = f"cd {shlex.quote(str(cwd))} && {command}"
    candidates: list[tuple[str, list[str]]] = []
    if sys.platform == "darwin":
        ghostty = ("Ghostty", ["open", "-na", "Ghostty.app", "--args", f"--working-directory={cwd}", "-e", "/bin/zsh", "-lc", command])
        iterm = ("iTerm", ["osascript", "-e", f'tell application "iTerm" to create window with default profile command {applescript_string("/bin/zsh -lc " + shlex.quote(shell_command))}'])
        terminal = ("Terminal", ["osascript", "-e", f'tell application "Terminal" to do script {applescript_string(shell_command)}', "-e", 'tell application "Terminal" to activate'])
        by_program = {"ghostty": ghostty, "iTerm.app": iterm, "Apple_Terminal": terminal}
        preferred = by_program.get(os.environ.get("TERM_PROGRAM", ""))
        installed = [
            entry
            for entry, app in [(ghostty, "Ghostty.app"), (iterm, "iTerm.app")]
            if Path("/Applications", app).exists()
        ]
        candidates = [c for c in [preferred, *installed, terminal] if c]
    else:
        for name, argv in [
            ("ghostty", ["ghostty", f"--working-directory={cwd}", "-e", "sh", "-lc", command]),
            ("gnome-terminal", ["gnome-terminal", f"--working-directory={cwd}", "--", "sh", "-lc", command]),
            ("konsole", ["konsole", "--workdir", str(cwd), "-e", "sh", "-lc", command]),
            ("x-terminal-emulator", ["x-terminal-emulator", "-e", "sh", "-lc", shell_command]),
        ]:
            if shutil.which(argv[0]):
                candidates.append((name, argv))
    unique: list[tuple[str, list[str]]] = []
    for candidate in candidates:
        if candidate[0] not in [u[0] for u in unique]:
            unique.append(candidate)
    return unique


def open_window(cwd: Path, command: str) -> str | None:
    """Returns the terminal used, or None when no launcher worked (caller prints the command)."""
    for name, argv in launchers(cwd, command):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return name
    return None
