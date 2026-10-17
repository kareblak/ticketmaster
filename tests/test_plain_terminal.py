import shlex
import subprocess

import pytest

from fake_plain import FakeRunner, fail, ok
from ticketmaster.backends.plain.terminal import LaunchFailed, Launcher, child_env, paste_line

DIR = "/code/my repo"
CMD = ["/opt/tm/bin/ticketmaster", "open", "42"]
LINE = "cd '/code/my repo' && exec /opt/tm/bin/ticketmaster open 42"
FRONT = ("lsappinfo", "front")
INFO = ("lsappinfo", "info", "-only", "bundleID")


def launcher(env=None, system="Darwin", answers=None, on_path=(), configured=None):
    runner = FakeRunner({FRONT: ok("ASN:0x0-0x1:\n"), **(answers or {})})
    which = lambda name: f"/usr/bin/{name}" if name in on_path else None  # noqa: E731
    return Launcher(runner, env or {}, system, which, configured), runner


def frontmost(bundle_id):
    return {INFO: ok(f'[ NULL ]  ASN:0x0-0x1: (in front)\n    bundleID="{bundle_id}"\n')}


# --- detection ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env, key", [
    ({"KITTY_WINDOW_ID": "1", "TERM_PROGRAM": "ghostty"}, "kitty"),
    ({"ALACRITTY_SOCKET": "/tmp/s"}, "alacritty"),
    ({"ALACRITTY_WINDOW_ID": "7"}, "alacritty"),
    ({"WEZTERM_PANE": "0"}, "wezterm"),
    ({"GHOSTTY_RESOURCES_DIR": "/x", "TERM_PROGRAM": "herdr"}, "ghostty"),
    ({"ITERM_SESSION_ID": "w0t0p0"}, "iterm"),
    ({"TERM_PROGRAM": "Apple_Terminal"}, "terminal"),
    ({"TERM_PROGRAM": "iTerm.app"}, "iterm"),
    ({"TERM_PROGRAM": "WezTerm", "__CFBundleIdentifier": "com.mitchellh.ghostty"}, "wezterm"),
])
def test_macos_env_markers_win_before_any_bundle_id(env, key):
    found, runner = launcher(env, answers=frontmost("com.googlecode.iterm2"))
    assert found.detect().key == key
    assert not runner.calls


def test_macos_bundle_identifier_comes_before_the_frontmost_app():
    found, runner = launcher({"TERM_PROGRAM": "herdr", "__CFBundleIdentifier": "com.mitchellh.ghostty"},
                             answers=frontmost("com.googlecode.iterm2"))
    assert found.detect().key == "ghostty"
    assert not runner.calls


def test_macos_unknown_bundle_identifier_falls_through_to_the_frontmost_app():
    found, runner = launcher({"__CFBundleIdentifier": "com.microsoft.VSCode"}, answers=frontmost("com.googlecode.iterm2"))
    terminal = found.detect()
    assert (terminal.key, terminal.name) == ("iterm", "iTerm2")
    assert runner.argvs() == [["lsappinfo", "front"], ["lsappinfo", "info", "-only", "bundleID", "ASN:0x0-0x1:"]]


@pytest.mark.parametrize("bundle_id, key", [
    ("com.apple.Terminal", "terminal"), ("com.googlecode.iterm2", "iterm"), ("com.mitchellh.ghostty", "ghostty"),
    ("net.kovidgoyal.kitty", "kitty"), ("com.github.wez.wezterm", "wezterm"), ("org.alacritty", "alacritty"),
])
def test_macos_frontmost_app_is_matched_by_bundle_id(bundle_id, key):
    found, _ = launcher(answers=frontmost(bundle_id))
    assert found.detect().key == key


@pytest.mark.parametrize("answers", [
    frontmost("com.google.Chrome"),
    {INFO: ok('[ NULL ]  ASN:0x0-0x1: (in front)\n    bundleID=[ NULL ]\n')},
    {FRONT: ok("[ NULL ]\n")},
    {FRONT: fail(1, "lsappinfo: error")},
    {FRONT: FileNotFoundError(2, "No such file or directory")},
    {INFO: fail(1)},
])
def test_macos_frontmost_app_that_is_not_a_known_terminal_falls_through_to_terminal_app(answers):
    found, _ = launcher(answers=answers)
    terminal = found.detect()
    assert (terminal.key, terminal.name) == ("terminal", "Terminal.app")


def test_macos_ignores_markers_of_terminals_it_has_no_recipe_for():
    found, _ = launcher({"KONSOLE_DBUS_SESSION": "/x", "GNOME_TERMINAL_SCREEN": "/y"},
                        answers=frontmost("com.mitchellh.ghostty"))
    assert found.detect().key == "ghostty"


@pytest.mark.parametrize("env, on_path, key", [
    ({"KONSOLE_DBUS_SESSION": "/x", "GNOME_TERMINAL_SCREEN": "/y"}, ["konsole", "gnome-terminal"], "konsole"),
    ({"GNOME_TERMINAL_SCREEN": "/y"}, ["gnome-terminal", "xdg-terminal-exec"], "gnome-terminal"),
    ({"TERM_PROGRAM": "ghostty"}, ["ghostty", "xdg-terminal-exec"], "ghostty"),
    ({"KITTY_WINDOW_ID": "1"}, ["xdg-terminal-exec"], "xdg-terminal-exec"),  # kitty not on PATH: falls through
    ({}, ["xdg-terminal-exec", "x-terminal-emulator", "kitty"], "xdg-terminal-exec"),
    ({"TERMINAL": "foot"}, ["foot", "x-terminal-emulator", "kitty"], "env-terminal"),
    ({"TERMINAL": "missing"}, ["x-terminal-emulator", "kitty"], "x-terminal-emulator"),
    ({}, ["kitty", "konsole"], "kitty"),
    ({}, ["konsole", "gnome-terminal"], "gnome-terminal"),
])
def test_linux_detection_order(env, on_path, key):
    found, runner = launcher({"DISPLAY": ":0", **env}, system="Linux", on_path=on_path)
    assert found.detect().key == key
    assert not runner.calls  # no lsappinfo on Linux


def test_linux_without_any_terminal_has_none():
    found, _ = launcher({"WAYLAND_DISPLAY": "wayland-0"}, system="Linux")
    with pytest.raises(LaunchFailed, match="no terminal found"):
        found.detect()


@pytest.mark.parametrize("env, system, reason", [
    ({}, "Linux", "no display: DISPLAY and WAYLAND_DISPLAY are unset"),
    ({"DISPLAY": ":0", "SSH_CONNECTION": "1 2 3 4", "KITTY_WINDOW_ID": "1"}, "Linux", "over SSH"),
    ({"SSH_TTY": "/dev/ttys001", "TERM_PROGRAM": "iTerm.app"}, "Darwin", "over SSH"),
])
def test_no_display_goes_straight_to_the_fallback(env, system, reason):
    found, runner = launcher(env, system=system, on_path=["kitty"])
    with pytest.raises(LaunchFailed) as caught:
        found.detect()
    assert caught.value.reason == reason
    assert not runner.calls


def test_the_env_override_beats_the_toml_one_and_every_other_signal():
    found, _ = launcher({"TICKETMASTER_TERMINAL": "foot -D {dir} {cmd}", "SSH_TTY": "x", "KITTY_WINDOW_ID": "1"},
                        configured="wezterm start --cwd {dir} -- {cmd}")
    terminal = found.detect()
    assert (terminal.key, terminal.name) == ("override", "foot")
    found, _ = launcher({"SSH_TTY": "x"}, configured="wezterm start --cwd {dir} -- {cmd}")
    assert found.detect().name == "wezterm"


# --- launch recipes -------------------------------------------------------------------------------------------

def opened(env, system="Darwin", front=True, answers=None, on_path=(), configured=None):
    found, runner = launcher(env, system, answers, on_path, configured)
    terminal = found.open(DIR, CMD, front=front)
    return terminal, runner.calls[-1]


def script(argv):
    """An osascript argv's script lines (each after -e) and its run arguments."""
    lines = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    return lines, argv[2 * len(lines) + 1:]


def test_terminal_app_runs_the_command_line_as_an_argument_never_inside_the_script():
    terminal, call = opened({"TERM_PROGRAM": "Apple_Terminal"})
    assert terminal.name == "Terminal.app" and call.argv[0] == "osascript" and not call.detach
    lines, run_args = script(call.argv)
    assert lines == ["on run argv", 'tell application "Terminal"', "activate", "do script (item 1 of argv)",
                     "end tell", "end run"]
    assert run_args == [LINE]


def test_a_background_window_leaves_out_activate():
    _, call = opened({"TERM_PROGRAM": "Apple_Terminal"}, front=False)
    assert "activate" not in script(call.argv)[0]


def test_quotes_in_the_directory_and_arguments_reach_the_shell_quoted_once():
    found, runner = launcher({"TERM_PROGRAM": "Apple_Terminal"})
    found.open("/code/it's", ["/bin/tm", 'say "hi"', "a\\b"], front=True)
    (line,) = script(runner.calls[-1].argv)[1]
    assert line == """cd '/code/it'"'"'s' && exec /bin/tm 'say "hi"' 'a\\b'"""


def test_iterm_creates_a_window_running_the_line_through_sh():
    terminal, call = opened({"ITERM_SESSION_ID": "x"}, front=False)
    lines, run_args = script(call.argv)
    assert terminal.name == "iTerm2"
    assert lines == ["on run argv", 'tell application "iTerm"', "create window with default profile command (item 1 of argv)",
                     "end tell", "end run"]
    assert run_args == [shlex.join(["/bin/sh", "-c", LINE])]


def test_ghostty_opens_a_window_with_a_surface_configuration_running_the_line_through_sh():
    """Ghostty runs `command` as `exec -l <command>`, so a bare `cd … && …` line would exec `cd`."""
    _, call = opened({"GHOSTTY_RESOURCES_DIR": "/x"})
    lines, run_args = script(call.argv)
    assert lines == ["on run argv", 'tell application "Ghostty"', "set cfg to new surface configuration",
                     "set initial working directory of cfg to item 1 of argv", "set command of cfg to item 2 of argv",
                     "new window with configuration cfg", "activate", "end tell", "end run"]
    assert run_args == [DIR, shlex.join(["/bin/sh", "-c", LINE])]


def test_ghostty_before_1_3_falls_back_to_a_new_instance():
    old = {("osascript",): fail(1, "execution error: Expected end of line but found class name. (-2741)")}
    _, call = opened({"GHOSTTY_RESOURCES_DIR": "/x"}, front=False, answers=old)
    assert call.argv == ["open", "-g", "-n", "-a", "Ghostty", "--args", f"--working-directory={DIR}", "-e", *CMD]


@pytest.mark.parametrize("env, argv", [
    ({"KITTY_WINDOW_ID": "1"}, ["open", "-n", "-a", "kitty", "--args", "--directory", DIR, *CMD]),
    ({"WEZTERM_PANE": "0"}, ["open", "-n", "-a", "WezTerm", "--args", "start", "--cwd", DIR, "--", *CMD]),
    ({"ALACRITTY_SOCKET": "/s"}, ["open", "-n", "-a", "Alacritty", "--args", "--working-directory", DIR, "-e", *CMD]),
])
def test_macos_open_recipes(env, argv):
    _, call = opened(env)
    assert call.argv == argv


@pytest.mark.parametrize("env, on_path, argv", [
    ({"GNOME_TERMINAL_SCREEN": "/y"}, ["gnome-terminal"],
     ["/usr/bin/gnome-terminal", "--window", f"--working-directory={DIR}", "--", *CMD]),
    ({"KONSOLE_DBUS_SESSION": "/x"}, ["konsole"], ["/usr/bin/konsole", "--workdir", DIR, "-e", *CMD]),
    ({"KITTY_WINDOW_ID": "1"}, ["kitty"], ["/usr/bin/kitty", "--single-instance", "-d", DIR, *CMD]),
    ({"WEZTERM_PANE": "0"}, ["wezterm"], ["/usr/bin/wezterm", "start", "--cwd", DIR, "--", *CMD]),
    ({"ALACRITTY_SOCKET": "/s"}, ["alacritty"], ["/usr/bin/alacritty", "--working-directory", DIR, "-e", *CMD]),
    ({"GHOSTTY_RESOURCES_DIR": "/x"}, ["ghostty"],
     ["/usr/bin/ghostty", "+new-window", f"--working-directory={DIR}", "-e", *CMD]),
    ({}, ["xdg-terminal-exec"], ["/usr/bin/xdg-terminal-exec", f"--dir={DIR}", "--", *CMD]),
    ({"TERMINAL": "foot"}, ["foot"], ["/usr/bin/foot", "-e", "sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", DIR, *CMD]),
    ({}, ["x-terminal-emulator"],
     ["/usr/bin/x-terminal-emulator", "-e", "sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", DIR, *CMD]),
])
def test_linux_recipes_run_detached(env, on_path, argv):
    _, call = opened({"DISPLAY": ":0", **env}, system="Linux", on_path=on_path)
    assert call.argv == argv and call.detach


def test_the_override_template_takes_the_directory_and_the_command_as_arguments():
    _, call = opened({"TICKETMASTER_TERMINAL": "foot -D {dir} {cmd}"})
    assert call.argv == ["foot", "-D", DIR, *CMD] and call.detach
    _, call = opened({}, configured="myterm --run 'cd {dir}; {cmd}'")
    assert call.argv == ["myterm", "--run", f"cd {DIR}; /opt/tm/bin/ticketmaster open 42"]


# --- the child environment ------------------------------------------------------------------------------------

def test_the_child_environment_drops_claude_and_herdr_markers():
    env = {"PATH": "/bin", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CODE_ENTRYPOINT": "cli",
           "HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "CLAUDE_PLUGIN_DATA": "/d", "HOME": "/h"}
    assert child_env(env) == {"PATH": "/bin", "CLAUDE_PLUGIN_DATA": "/d", "HOME": "/h"}
    _, call = opened({**env, "DISPLAY": ":0", "KITTY_WINDOW_ID": "1"}, system="Linux", on_path=["kitty"])
    assert call.env == {"PATH": "/bin", "CLAUDE_PLUGIN_DATA": "/d", "HOME": "/h", "DISPLAY": ":0", "KITTY_WINDOW_ID": "1"}
    _, call = opened({**env, "TERM_PROGRAM": "Apple_Terminal"})
    assert "CLAUDECODE" not in call.env and "HERDR_ENV" not in call.env


# --- every failure gives the paste-able line ------------------------------------------------------------------

PASTE = "cd '/code/my repo' && /opt/tm/bin/ticketmaster open 42"


@pytest.mark.parametrize("env, system, answers, on_path, reason", [
    ({}, "Linux", {}, ["kitty"], "no display: DISPLAY and WAYLAND_DISPLAY are unset"),
    ({"SSH_CONNECTION": "x"}, "Darwin", {}, [], "over SSH"),
    ({"DISPLAY": ":0"}, "Linux", {}, [], "no terminal found (tried xdg-terminal-exec, $TERMINAL, x-terminal-emulator, "
                                          "ghostty, wezterm, kitty, alacritty, gnome-terminal, konsole)"),
    ({}, "Windows", {}, [], "no terminal recipe for Windows"),
    ({"TERM_PROGRAM": "Apple_Terminal"}, "Darwin",
     {("osascript",): fail(1, "execution error: Not authorized to send Apple events to Terminal. (-1743)")}, [],
     "macOS Automation permission for Terminal.app denied (-1743); allow it in System Settings > Privacy & "
     "Security > Automation"),
    ({"ITERM_SESSION_ID": "x"}, "Darwin",
     {("osascript",): fail(1, "execution error: iTerm got an error: Application isn't running. (-600)")}, [],
     "iTerm2 could not be reached (-600); under the Bash sandbox, set sandbox.allowAppleEvents or add "
     "\"ticketmaster *\" to sandbox.excludedCommands"),
    ({"GHOSTTY_RESOURCES_DIR": "x"}, "Darwin",
     {("osascript",): fail(1, "(-600)")}, [],
     "Ghostty could not be reached (-600); under the Bash sandbox, set sandbox.allowAppleEvents or add "
     "\"ticketmaster *\" to sandbox.excludedCommands"),
    ({"GHOSTTY_RESOURCES_DIR": "x"}, "Darwin", {("osascript",): fail(1, "Not authorized (-1743)")}, [],
     "macOS Automation permission for Ghostty denied (-1743); allow it in System Settings > Privacy & Security > "
     "Automation"),
    ({"TERM_PROGRAM": "Apple_Terminal"}, "Darwin", {("osascript",): fail(1, "boom\nsome error (-10810)")}, [],
     "osascript exited 1: some error (-10810)"),
    ({"TERM_PROGRAM": "Apple_Terminal"}, "Darwin", {("osascript",): subprocess.TimeoutExpired("osascript", 15)}, [],
     "osascript timed out"),
    ({"KITTY_WINDOW_ID": "1"}, "Darwin", {("open",): fail(1, "Unable to find application named 'kitty'")}, [],
     "open exited 1: Unable to find application named 'kitty'"),
    ({"DISPLAY": ":0", "KITTY_WINDOW_ID": "1"}, "Linux",
     {("/usr/bin/kitty",): PermissionError(13, "Permission denied")}, ["kitty"], "/usr/bin/kitty: Permission denied"),
    ({"TICKETMASTER_TERMINAL": "foot {dir}"}, "Linux", {}, [], "TICKETMASTER_TERMINAL has no {cmd}"),
])
def test_every_failure_kind_gives_the_reason_and_the_paste_able_line(env, system, answers, on_path, reason):
    found, _ = launcher(env, system, answers, on_path)
    with pytest.raises(LaunchFailed) as caught:
        found.open(DIR, CMD, front=True)
    assert caught.value.reason == reason
    assert caught.value.paste == PASTE == paste_line(DIR, CMD)


@pytest.mark.parametrize("answer", [subprocess.TimeoutExpired("osascript", 15), fail(1, "Not authorized (-1743)")])
def test_ghostty_tries_no_second_recipe_after_a_timeout_or_a_refusal(answer):
    found, runner = launcher({"GHOSTTY_RESOURCES_DIR": "/x"}, answers={("osascript",): answer})
    with pytest.raises(LaunchFailed):
        found.open(DIR, CMD, front=True)
    assert [c.argv[0] for c in runner.calls] == ["osascript"]
