"""Opens a new window of the user's terminal running a command in a directory, on macOS and Linux.

The terminal is the override (`TICKETMASTER_TERMINAL`, else `terminal` in `.ticketmaster.toml`: an argv template with
`{dir}` and `{cmd}`), else none without a display or over SSH, else the one a terminal's own env marker or a known
`TERM_PROGRAM` names, else on macOS the one `__CFBundleIdentifier` or the frontmost app names (Claude's Bash tool hides
every marker of Terminal.app and iTerm2), else on Linux the first of `xdg-terminal-exec`, `$TERMINAL`,
`x-terminal-emulator` and the known binaries on PATH, else Terminal.app on macOS. It always opens a window, never a
tab. Every failure says why and gives the line to paste instead.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..base import BackendError

OVERRIDE_ENV = "TICKETMASTER_TERMINAL"
OVERRIDE_HINT = (f"to pick the terminal, set {OVERRIDE_ENV} (or `terminal` in .ticketmaster.toml) to its command "
                 f"with {{dir}} and {{cmd}}, e.g. {OVERRIDE_ENV}='kitty --directory {{dir}} {{cmd}}'")

NAMES = {
    "terminal": "Terminal.app", "iterm": "iTerm2", "ghostty": "Ghostty", "kitty": "kitty", "wezterm": "WezTerm",
    "alacritty": "Alacritty", "gnome-terminal": "GNOME Terminal", "konsole": "Konsole",
    "xdg-terminal-exec": "xdg-terminal-exec", "x-terminal-emulator": "x-terminal-emulator",
}
MACOS = ("terminal", "iterm", "ghostty", "kitty", "wezterm", "alacritty")
LINUX_BINARIES = ("ghostty", "wezterm", "kitty", "alacritty", "gnome-terminal", "konsole")
# A terminal's own markers, which survive multiplexers that overwrite TERM_PROGRAM.
MARKERS = (
    ("KITTY_WINDOW_ID", "kitty"), ("ALACRITTY_SOCKET", "alacritty"), ("ALACRITTY_WINDOW_ID", "alacritty"),
    ("WEZTERM_PANE", "wezterm"), ("GHOSTTY_RESOURCES_DIR", "ghostty"), ("KONSOLE_DBUS_SESSION", "konsole"),
    ("GNOME_TERMINAL_SCREEN", "gnome-terminal"), ("ITERM_SESSION_ID", "iterm"),
)
TERM_PROGRAMS = {"Apple_Terminal": "terminal", "iTerm.app": "iterm", "ghostty": "ghostty", "WezTerm": "wezterm"}
BUNDLE_IDS = {
    "com.apple.Terminal": "terminal", "com.googlecode.iterm2": "iterm", "com.mitchellh.ghostty": "ghostty",
    "net.kovidgoyal.kitty": "kitty", "com.github.wez.wezterm": "wezterm", "org.alacritty": "alacritty",
}
# Dropped from a window's environment, so Claude there does not think it is nested and the board knows it is not
# run by Claude.
SCRUBBED = re.compile(r"^(?:CLAUDECODE$|CLAUDE_CODE_|HERDR_)")
# `$TERMINAL` and `x-terminal-emulator` have no directory flag: a shell changes there first.
CD_THEN = ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh"]
OSASCRIPT_TIMEOUT = 15.0
APPLE_ERROR = re.compile(r"\((-\d+)\)\s*$")


class Runner(Protocol):
    """Runs argv and waits for it, or with `detach` starts it in its own session with no stdio and returns at once."""

    def __call__(self, argv: list[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None,
                 timeout: float = 30.0, detach: bool = False) -> subprocess.CompletedProcess: ...


class LaunchFailed(BackendError):
    """No window opened: `reason` says why, `paste` is the line that does it by hand. `final`: the terminal app
    refused (consent denied, sandboxed) or may still open one late (timed out), so no other recipe is tried."""

    source = "plain"

    def __init__(self, reason: str, paste: str = "", final: bool = False):
        super().__init__(f"couldn't open a terminal window ({reason})")
        self.reason = reason
        self.paste = paste
        self.final = final


@dataclass(frozen=True)
class Terminal:
    key: str  # a recipe: "ghostty", or "override", "env-terminal" for a template or `$TERMINAL`
    name: str  # for the report: "Ghostty"
    program: tuple[str, ...] = ()  # Linux: the binary by absolute path (and `$TERMINAL`'s words); an override's template


def child_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not SCRUBBED.match(k)}


def paste_line(directory: str, command: list[str]) -> str:
    return f"cd {shlex.quote(directory)} && {shlex.join(command)}"


def shell_line(directory: str, command: list[str]) -> str:
    """What a window's shell runs: the command takes the shell's place, so the window ends with it."""
    return f"cd {shlex.quote(directory)} && exec {shlex.join(command)}"


def osascript(lines: list[str], *args: str) -> list[str]:
    """An `on run argv` script: the directory and command reach AppleScript as arguments, so nothing in them is ever
    parsed as AppleScript."""
    return ["osascript", *(part for line in ["on run argv", *lines, "end run"] for part in ("-e", line)), *args]


@dataclass
class Launcher:
    runner: Runner
    env: Mapping[str, str]
    system: str  # platform.system(): "Darwin", "Linux"
    which: Callable[[str], str | None]
    configured: str | None = None  # `terminal` in .ticketmaster.toml

    def open(self, directory: str, command: list[str], front: bool) -> Terminal:
        """Opens a new window running `command` in `directory`, brought forward only when `front`, and returns
        the terminal; LaunchFailed with the paste-able line otherwise."""
        try:
            terminal = self.detect()
            self.launch(terminal, directory, command, front)
        except LaunchFailed as exc:
            raise LaunchFailed(exc.reason, paste_line(directory, command), exc.final) from None
        return terminal

    # --- which terminal ----------------------------------------------------------------------------------------

    def detect(self) -> Terminal:
        if override := self.env.get(OVERRIDE_ENV) or self.configured:
            words = tuple(shlex.split(override))
            if not any("{cmd}" in w for w in words):
                where = OVERRIDE_ENV if self.env.get(OVERRIDE_ENV) else "terminal in .ticketmaster.toml"
                raise LaunchFailed(f"{where} has no {{cmd}}")
            return Terminal("override", Path(words[0]).name, words)
        if self.env.get("SSH_CONNECTION") or self.env.get("SSH_TTY"):
            raise LaunchFailed("over SSH")
        if self.system == "Darwin":
            return self.macos()
        if self.system == "Linux":
            if not (self.env.get("DISPLAY") or self.env.get("WAYLAND_DISPLAY")):
                raise LaunchFailed("no display: DISPLAY and WAYLAND_DISPLAY are unset")
            return self.linux()
        raise LaunchFailed(f"no terminal recipe for {self.system}")

    def marked(self) -> list[str]:
        """The terminals the env names, most precise first."""
        found = [key for var, key in MARKERS if self.env.get(var)]
        if key := TERM_PROGRAMS.get(self.env.get("TERM_PROGRAM", "")):
            found.append(key)
        return found

    def macos(self) -> Terminal:
        candidates = [*self.marked(), BUNDLE_IDS.get(self.env.get("__CFBundleIdentifier", ""))]
        key = next((k for k in candidates if k in MACOS), None) or BUNDLE_IDS.get(self.frontmost() or "", "terminal")
        return Terminal(key, NAMES[key])

    def frontmost(self) -> str | None:
        """The frontmost app's bundle id, from `lsappinfo` (no consent dialog); None when it cannot tell."""
        front = self.quiet(["lsappinfo", "front"])
        if not front or not front.startswith("ASN:"):
            return None
        match = re.search(r'bundleID="([^"]+)"', self.quiet(["lsappinfo", "info", "-only", "bundleID", front]) or "")
        return match[1] if match else None

    def quiet(self, argv: list[str]) -> str | None:
        try:
            done = self.runner(argv, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    def linux(self) -> Terminal:
        for key in self.marked():
            if key in LINUX_BINARIES and (path := self.which(key)):
                return Terminal(key, NAMES[key], (path,))
        if path := self.which("xdg-terminal-exec"):
            return Terminal("xdg-terminal-exec", NAMES["xdg-terminal-exec"], (path,))
        if (words := shlex.split(self.env.get("TERMINAL", ""))) and (path := self.which(words[0])):
            return Terminal("env-terminal", Path(words[0]).name, (path, *words[1:]))
        for key in ("x-terminal-emulator", *LINUX_BINARIES):
            if path := self.which(key):
                return Terminal(key, NAMES[key], (path,))
        tried = ", ".join(("xdg-terminal-exec", "$TERMINAL", "x-terminal-emulator", *LINUX_BINARIES))
        raise LaunchFailed(f"no terminal found (tried {tried})")

    # --- the recipes -------------------------------------------------------------------------------------------

    def launch(self, terminal: Terminal, directory: str, command: list[str], front: bool) -> None:
        key, line = terminal.key, shell_line(directory, command)
        activate = ["activate"] if front else []
        if key == "override":
            self.detached(self.template(terminal.program, directory, command))
        elif terminal.program:  # Linux
            self.detached(linux_argv(terminal, directory, command))
        elif key == "terminal":
            self.apple(terminal, osascript(['tell application "Terminal"', *activate, "do script (item 1 of argv)",
                                            "end tell"], line))
        elif key == "iterm":
            self.apple(terminal, osascript(['tell application "iTerm"', *activate,
                                            "create window with default profile command (item 1 of argv)", "end tell"],
                                           shlex.join(["/bin/sh", "-c", line])))
        elif key == "ghostty":
            try:
                self.apple(terminal, osascript([
                    'tell application "Ghostty"', "set cfg to new surface configuration",
                    "set initial working directory of cfg to item 1 of argv", "set command of cfg to item 2 of argv",
                    "new window with configuration cfg", *activate, "end tell",
                ], directory, shlex.join(["/bin/sh", "-c", line])))  # Ghostty runs it as `exec -l <command>`
            except LaunchFailed as exc:
                if exc.final:
                    raise
                self.open_app("Ghostty", front, f"--working-directory={directory}", "-e", *command)
        elif key == "kitty":
            self.open_app("kitty", front, "--directory", directory, *command)
        elif key == "wezterm":
            self.open_app("WezTerm", front, "start", "--cwd", directory, "--", *command)
        elif key == "alacritty":
            self.open_app("Alacritty", front, "--working-directory", directory, "-e", *command)

    def template(self, words: tuple[str, ...], directory: str, command: list[str]) -> list[str]:
        argv: list[str] = []
        for word in words:
            if word == "{cmd}":
                argv += command
            else:
                argv.append(word.replace("{dir}", directory).replace("{cmd}", shlex.join(command)))
        return argv

    def detached(self, argv: list[str]) -> None:
        try:
            self.runner(argv, env=child_env(self.env), detach=True)
        except OSError as exc:
            raise LaunchFailed(f"{argv[0]}: {exc.strerror or exc}") from None

    def apple(self, terminal: Terminal, argv: list[str]) -> None:
        done = self.waited(argv, "osascript")
        if done.returncode == 0:
            return
        error = last_line(done.stderr)
        code = (match[1] if (match := APPLE_ERROR.search(error)) else None)
        if code == "-1743":
            raise LaunchFailed(f"macOS Automation permission for {terminal.name} denied (-1743); allow it in System "
                               "Settings > Privacy & Security > Automation", final=True)
        if code == "-600":
            raise LaunchFailed(f"{terminal.name} could not be reached (-600); under the Bash sandbox, set "
                               'sandbox.allowAppleEvents or add "ticketmaster *" to sandbox.excludedCommands',
                               final=True)
        raise LaunchFailed(exited("osascript", done))

    def open_app(self, app: str, front: bool, *args: str) -> None:
        """A new instance of the app with one window: `open -n`, in the background (`-g`) unless `front`."""
        done = self.waited(["open", *([] if front else ["-g"]), "-n", "-a", app, "--args", *args], "open")
        if done.returncode != 0:
            raise LaunchFailed(exited("open", done))

    def waited(self, argv: list[str], name: str) -> subprocess.CompletedProcess:
        try:
            return self.runner(argv, env=child_env(self.env), timeout=OSASCRIPT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise LaunchFailed(f"{name} timed out", final=True) from None
        except OSError as exc:
            raise LaunchFailed(f"{name}: {exc.strerror or exc}") from None


def last_line(stderr: str) -> str:
    return (stderr.strip().splitlines() or [""])[-1]


def exited(name: str, done: subprocess.CompletedProcess) -> str:
    """`<name> exited <code>`, and its last line of stderr when it said something."""
    error = last_line(done.stderr)
    return f"{name} exited {done.returncode}" + (f": {error}" if error else "")


def linux_argv(terminal: Terminal, directory: str, command: list[str]) -> list[str]:
    program = list(terminal.program)
    match terminal.key:
        case "gnome-terminal":
            return [*program, "--window", f"--working-directory={directory}", "--", *command]
        case "konsole":
            return [*program, "--workdir", directory, "-e", *command]
        case "kitty":
            return [*program, "--single-instance", "-d", directory, *command]
        case "wezterm":
            return [*program, "start", "--cwd", directory, "--", *command]
        case "alacritty":
            return [*program, "--working-directory", directory, "-e", *command]
        case "ghostty":
            return [*program, "+new-window", f"--working-directory={directory}", "-e", *command]
        case "xdg-terminal-exec":
            return [*program, f"--dir={directory}", "--", *command]
    return [*program, "-e", *CD_THEN, directory, *command]  # $TERMINAL, x-terminal-emulator
