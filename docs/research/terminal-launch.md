# Opening a new terminal window from a process (issue #134)

Question: how can a Python process reliably open a new terminal window (or tab) that runs a given command in a
given directory, on macOS and Linux? The case that matters for ticketmaster is `/ticketmaster`, run through Claude
Code's Bash tool, opening the board in a new window of the user's terminal without herdr.

Researched 2026-10-15 against official terminal docs and man pages, the scripting dictionaries (`.sdef`) shipped
inside the installed apps, the Debian policy, the xdg-terminal-exec reference implementation, and the Claude Code
docs. The Claude Code findings were also checked empirically on this machine: macOS, Claude Code 2.1.292, Ghostty
1.3.1, iTerm2 3.7.3, and Terminal.app, with Claude running inside herdr. No terminal windows were opened during the
research, so **none of the launch recipes below has been run end to end**. Every claim says how it was checked.

## Answer in brief

1. **You cannot run the TUI inline.** A Bash-tool command has no TTY: `tty` prints `not a tty`, and stdin and
   stdout are not terminals. The board must go to a new window.
2. **Detect the host terminal in this order:**
   - a user override;
   - `$TERM_PROGRAM` plus terminal-specific variables;
   - on macOS, `$__CFBundleIdentifier` (it survives multiplexers);
   - on Linux, `xdg-terminal-exec`, then `$TERMINAL`, then `x-terminal-emulator`, then known binaries on `PATH`;
   - a platform default: Terminal.app on macOS.
3. **Launch through the terminal's own "new window" mechanism:**
   - macOS: AppleScript for Terminal.app, iTerm2 and Ghostty 1.3+, or `open -na <App> --args …` for the
     rest.
   - Linux: the terminal's CLI with its working-directory flag. Use `xdg-terminal-exec --dir=` where available.
4. **Detach the child.** On Linux, use `start_new_session=True` with stdio set to `DEVNULL` and do not wait.
   On macOS, `open` and `osascript` hand the launch to LaunchServices or to the app itself, so the window does not
   belong to the Bash tool's process group.
5. **Fallback:** if nothing is found, or there is no display (SSH, headless, Bash sandbox blocks the launch), exit
   non-zero. Print a single copy-pasteable command (`cd <dir> && <cmd>`) and the reason.

## 1. Detection signals

| Terminal | Signal in the child environment | How verified |
| --- | --- | --- |
| Terminal.app | `TERM_PROGRAM=Apple_Terminal`; `__CFBundleIdentifier=com.apple.Terminal` | Widely documented, not re-verified here |
| iTerm2 | `TERM_PROGRAM=iTerm.app`, `LC_TERMINAL=iTerm2`, `ITERM_SESSION_ID` | The strings `iTerm.app`, `LC_TERMINAL` and `ITERM_SESSION_ID` are in the iTerm2 3.7.3 binary. The iTerm2 docs only mention `$TERM_SESSION_ID` ([variables][iterm-vars]) |
| Ghostty | `TERM_PROGRAM=ghostty`, `GHOSTTY_RESOURCES_DIR`, `GHOSTTY_BIN_DIR`, `TERM=xterm-ghostty`; `__CFBundleIdentifier=com.mitchellh.ghostty` on macOS | `GHOSTTY_RESOURCES_DIR` is documented as the way to detect Ghostty ([README][ghostty-readme]). `GHOSTTY_*` and `__CFBundleIdentifier` were observed in this session. The strings `TERM_PROGRAM` and `ghostty` are in the binary |
| WezTerm | `TERM_PROGRAM=WezTerm`, `WEZTERM_PANE`, `WEZTERM_EXECUTABLE` | **Not verified.** The docs only say WezTerm propagates `TERM_PROGRAM` across WSL ([term][wezterm-term]) |
| kitty | `KITTY_WINDOW_ID`, `KITTY_PID`, `TERM=xterm-kitty`, `KITTY_LISTEN_ON` (if remote control is on) | [kitty glossary][kitty-glossary] |
| Alacritty | `ALACRITTY_SOCKET`, `ALACRITTY_WINDOW_ID` | [alacritty-msg(1)][alacritty-msg] |
| GNOME Terminal | `GNOME_TERMINAL_SCREEN`, `VTE_VERSION` (any VTE terminal) | **Not verified** against a primary source |
| Konsole | `KONSOLE_DBUS_SESSION`, `KONSOLE_DBUS_WINDOW`, `KONSOLE_DBUS_SERVICE` (`KONSOLE_VERSION` is commonly seen but **not verified**) | [Konsole scripting][konsole-scripting] |

Generic signals:

- **`$TERM_PROGRAM`** is a convention, not a standard. Multiplexers overwrite it. In this session
  `TERM_PROGRAM=herdr`, and tmux sets `tmux`. A VS Code integrated terminal sets `vscode`. So `TERM_PROGRAM`
  alone often names something that cannot open a window.
- **`$__CFBundleIdentifier`** (macOS) is inherited from the GUI app that started the process tree. Here it was
  `com.mitchellh.ghostty` even though `TERM_PROGRAM=herdr`. This makes it the best macOS signal for the host app
  under a multiplexer. This is empirical; Apple does not document it as an API.
- **`$TERMINAL`** has no standard definition. It is a user convention, used for example by i3-sensible-terminal.
  Treat it as a binary name that probably accepts `-e`.
- **`x-terminal-emulator`** (Debian alternatives) must support `-e command [args…]`. The arguments are the argv
  for `execvp`, with no shell. It must also support `-T title`. Policy defines **no working-directory flag**
  ([Debian Policy §11.8.3][debian-policy]). Debian's `sensible-terminal` also honours `$TERMINAL_EMULATOR`
  ([sensible-terminal(1)][sensible-terminal]).
- **`xdg-terminal-exec`** is the proposed freedesktop default-terminal launcher:
  - Syntax: `xdg-terminal-exec [--dir=PATH] [--title=…] [--app-id=…] [--hold] [command [args…]]`.
  - It chooses the terminal from `${desktop}-xdg-terminals.list` or `xdg-terminals.list` in the XDG config dirs.
    It falls back to desktop entries in the `TerminalEmulator` category, which declare `X-TerminalArgExec`,
    `X-TerminalArgDir`, `X-TerminalArgTitle` and `X-TerminalArgAppId` ([reference implementation][xte]).
  - The spec itself is a merge request in the freedesktop terminal-wg repo. Its text could not be fetched (the
    GitLab instance is behind a bot wall), so its exact status is **not verified**. Older xdg-terminal-exec
    releases may lack `--dir`; check with `--help`, or `cd` through a shell wrapper as a fallback.
- **Display presence (Linux):** require `$WAYLAND_DISPLAY` or `$DISPLAY`. If `$SSH_CONNECTION` is set and
  neither is present, there is no local GUI.

## 2. Launch recipes

`DIR` is an absolute path and `CMD…` is an argv list. Where a recipe takes a single shell string, build it as
`cd <q(DIR)> && exec <q(CMD…)>`, where `q` is `shlex.quote`. For AppleScript, escape `\` and `"` in that string
before embedding it in a `"…"` literal. Better, pass it as an `on run argv` argument to `osascript` so that no
escaping is needed (see the sketch in §5).

| Terminal | Exact argv to open a new window running `CMD` in `DIR` | Window or tab | Caveats |
| --- | --- | --- | --- |
| Terminal.app | `osascript -e 'on run argv' -e 'tell application "Terminal"' -e 'activate' -e 'do script (item 1 of argv)' -e 'end tell' -e 'end run' "cd 'DIR' && exec CMD"` | New window: `do script` without `in` opens a new window. Pass `in front window` to run in an existing tab instead | `do script` "Runs a UNIX shell script or command" in a new interactive shell (Terminal.sdef). The window stays open after `CMD` exits unless the profile closes on exit. Sends Apple Events, so the first use triggers a macOS Automation (TCC) prompt |
| iTerm2 | `osascript … 'tell application "iTerm" to create window with default profile command (item 1 of argv)' … "/bin/sh -c 'cd DIR && exec CMD'"` | Window. `tell current window to create tab with default profile command …` gives a tab | AppleScript is marked **deprecated** in favour of the Python API, but still ships in 3.7.3 (iTerm2.sdef: `create window with default profile`, `command` = "Shell command to run") ([iTerm2 AppleScript][iterm-as]). The docs do not say whether `command` goes through a shell, so wrap in an explicit `/bin/sh -c` (**not verified**). Automation (TCC) prompt |
| Ghostty (macOS, 1.3+) | `osascript -e 'on run argv' -e 'tell application "Ghostty"' -e 'set cfg to new surface configuration' -e 'set initial working directory of cfg to item 1 of argv' -e 'set command of cfg to item 2 of argv' -e 'new window with configuration cfg' -e 'activate' -e 'end tell' -e 'end run' DIR "CMD"` | Window. Use `new tab with configuration cfg` for a tab | AppleScript arrived in 1.3.0 and can be disabled with `macos-applescript = false` ([Ghostty AppleScript][ghostty-as]). The surface configuration also has `wait after command` and `environment variables` (Ghostty.sdef). Automation (TCC) prompt. **Not run** |
| Ghostty (macOS, any version) | `open -na Ghostty.app --args --working-directory=DIR -e CMD…` | New **app instance** with one window (a second Dock icon) | The docs prescribe `open -na Ghostty.app --args` on macOS ([config reference][ghostty-config]). `-e` implies `quit-after-last-window-closed`. Since 1.2.0, `-e` runs the command directly, not through `/bin/sh` ([1.2.0 notes][ghostty-120]) |
| Ghostty (Linux) | `ghostty --working-directory=DIR -e CMD…` | Window | `-e` forces `gtk-single-instance=false`, so a new process always starts ([config reference][ghostty-config]) |
| WezTerm | `wezterm start --cwd DIR -- CMD…` (macOS binary: `/Applications/WezTerm.app/Contents/MacOS/wezterm` if it is not on `PATH`) | Window in the running GUI. `--new-tab` gives a tab. `--always-new-process` starts a separate GUI | By default it connects to a running instance ([wezterm start][wezterm-start]) |
| kitty | macOS: `open -na kitty.app --args --directory DIR CMD…`. Linux: `kitty --detach --directory DIR CMD…`. Add `-1` (`--single-instance`) to reuse a running kitty | Window. `-1` opens a new top-level window in the existing instance | The docs recommend `open -na kitty --args` on macOS in place of `--detach`. `--hold` keeps the window open after exit ([kitty invocation][kitty-cli]) |
| Alacritty | `alacritty --working-directory DIR -e CMD…`. If `ALACRITTY_SOCKET` is set: `alacritty msg create-window --working-directory DIR -e CMD…`. macOS: `open -na Alacritty.app --args --working-directory DIR -e CMD…` | Window (Alacritty has no tabs) | `-e` must come last. `--hold` keeps the window open ([alacritty(1)][alacritty-man], [alacritty-msg(1)][alacritty-msg]). The macOS `open -na` form is by analogy with kitty and Ghostty (**not verified** in the Alacritty docs) |
| GNOME Terminal | `gnome-terminal --window --working-directory=DIR -- CMD…` | `--window` gives a window. `--tab` adds a tab to the last-opened window | `-e` and `-x` are deprecated; use `--`. The client asks `gnome-terminal-server` over D-Bus and returns at once unless `--wait` is given ([gnome-terminal(1)][gnome-terminal]) |
| Konsole | `konsole --workdir DIR -e CMD…` | Window. `--new-tab` gives a tab in an existing window | `-e` takes all remaining arguments, so it must come last. Use `--hold`/`--noclose` to keep the window open, and `--separate` to force a separate process ([Konsole options][konsole-cli]) |
| xdg-terminal-exec | `xdg-terminal-exec --dir=DIR CMD…` | Whatever the chosen terminal does (normally a window) | See §1 for `--dir` availability ([xdg-terminal-exec][xte]) |
| `$TERMINAL`, `x-terminal-emulator` | `<term> -e /bin/sh -c 'cd "$1" && shift && exec "$@"' sh DIR CMD…` | Window | There is no portable directory flag, so `cd` in a wrapper. `-e` argv semantics are guaranteed only for `x-terminal-emulator` ([Debian Policy][debian-policy]) |

macOS `open` facts ([open(1)][open-man], local man page):

- `-n` opens a new instance even if one is running.
- `-a`/`-b` choose the app by name or bundle id.
- `--args` passes the remaining arguments to the app's `main()`.
- `--env VAR` adds variables to the app's environment.

`open` returns once LaunchServices has launched the app, so the new terminal is a launchd child, not a child of
the Bash tool.

## 3. Recommended detection order

1. **Explicit override.** For example a `TICKETMASTER_TERMINAL` environment variable or a config key holding an
   argv template with `{dir}` and `{cmd}`. Nothing in the environment can be fully trusted under multiplexers,
   VS Code or SSH, so the user needs a way out.
2. **No GUI, so go to the fallback.** On Linux this means neither `DISPLAY` nor `WAYLAND_DISPLAY` is set. On
   either platform it also covers `SSH_CONNECTION` set without a local display.
3. **Terminal-specific variables**, matched in the order of the §1 table:
   - `KITTY_WINDOW_ID`, then `ALACRITTY_SOCKET`, `WEZTERM_PANE`, `GHOSTTY_RESOURCES_DIR` or `GHOSTTY_BIN_DIR`,
     `KONSOLE_DBUS_SESSION`, `GNOME_TERMINAL_SCREEN`, and `ITERM_SESSION_ID` or `LC_TERMINAL=iTerm2`.
   - Next, `TERM_PROGRAM` when it is a known terminal (`Apple_Terminal`, `iTerm.app`, `ghostty`, `WezTerm`).
   - These variables leak through multiplexers: herdr kept `GHOSTTY_*` here. A terminal launched from a different
     terminal can inherit stale ones, which is acceptable.
4. **macOS: `__CFBundleIdentifier`** mapped to a known app: `com.apple.Terminal`, `com.googlecode.iterm2`,
   `com.mitchellh.ghostty`, `com.github.wez.wezterm`, `net.kovidgoyal.kitty`, `org.alacritty`. The bundle ids
   other than Terminal and Ghostty are **not verified** here.
5. **Linux generic launchers:**
   - `xdg-terminal-exec` on `PATH` (it encodes the user's desktop preference);
   - then `$TERMINAL`;
   - then `x-terminal-emulator`;
   - then the first of `ghostty wezterm kitty alacritty gnome-terminal konsole` on `PATH`.
6. **Platform default.** On macOS that is Terminal.app, which is always installed. On Linux there is none, so go
   to the fallback.

Before using a detected app, check that it is installed: `shutil.which(...)` on Linux, or
`open -Ra <App>` / `mdfind kMDItemCFBundleIdentifier == <id>` on macOS. If it is missing, continue down the list.

## 4. Launched from Claude Code's Bash tool

- **No TTY.** This was verified in this session:
  - `tty` prints `not a tty`;
  - `test -t 0` and `test -t 1` are both false;
  - every command runs as its own process-group leader (`pgid == pid` of the shell), as a child of the `claude`
    process.
  - The Bash tool runs "each command in a separate process". Environment changes do not persist between
    commands ([tools reference][cc-tools]).
- **Environment.** Claude Code sets `CLAUDECODE=1` and `CLAUDE_CODE_CHILD_SESSION=1` in Bash-tool subprocesses
  ([env vars][cc-env]). Both were present here, along with `CLAUDE_CODE_ENTRYPOINT=cli`, `CLAUDE_PID` and
  `AI_AGENT=claude-code_…`. Use `CLAUDECODE=1` to decide "launch a window, don't try to draw here". The host
  terminal's variables (`TERM_PROGRAM`, `GHOSTTY_*`, `__CFBundleIdentifier`, `DISPLAY`) are inherited from the
  shell that started `claude`, so the detection above works unchanged.
- **Timeouts and process lifetime.** Foreground commands time out at 2 minutes by default
  ([tools reference][cc-tools]).
  - The launcher must return at once. Use `subprocess.Popen(argv, cwd=DIR, stdin=DEVNULL, stdout=DEVNULL,
    stderr=DEVNULL, start_new_session=True)` and do not wait.
  - On Linux, `start_new_session` takes the terminal out of the Bash tool's process group, so it is not killed with
    the command. Whether Claude Code kills the whole group on timeout is **not verified**. `setsid` is cheap
    insurance.
  - On macOS, `open` and `osascript` exit quickly, and the window belongs to launchd or the app.
- **Sandbox.** The Bash sandbox is **off by default** and is enabled with `/sandbox` or `sandbox.enabled`
  ([sandboxing][cc-sandbox]). It was off in this session.
  - When it is on, macOS uses Seatbelt. Apple Events (osascript) are blocked unless `sandbox.allowAppleEvents` is
    set, and a repository cannot turn that on. XPC/Mach lookups need `sandbox.network.allowMachLookup`, and Unix
    sockets need `sandbox.network.allowUnixSockets` ([settings reference][cc-settings]).
  - Linux uses bubblewrap, plus an optional seccomp filter that blocks Unix-domain sockets. That would block the
    X11/Wayland socket and the D-Bus session bus, which gnome-terminal and Konsole need.
  - Whether `open -na …` (LaunchServices) works under Seatbelt is **not verified**.
  - Options for users with the sandbox on:
    - add the launcher to `sandbox.excludedCommands`, for example `"ticketmaster *"`; the pattern must match the
      call text exactly;
    - set `sandbox.allowAppleEvents`;
    - or let Claude retry with `dangerouslyDisableSandbox`, which `allowUnsandboxedCommands: false` turns off.
  - Detect the failure (non-zero exit, `Operation not permitted`, or a launcher that returns success but no window)
    and fall back.
- **macOS Automation (TCC) prompt.** The AppleScript routes (Terminal, iTerm2, Ghostty 1.3+) trigger a one-time
  "… wants to control …" consent dialog ([Ghostty AppleScript][ghostty-as]). If the user denies it, osascript
  fails with error −1743. Which app the prompt names (the hosting terminal or `osascript`) is **not verified**.
  `open -na … --args` avoids Apple Events for Ghostty, kitty and Alacritty, but there is no `--args` equivalent
  for Terminal.app or iTerm2.
- **Shell environment of the new window.** The new terminal is started by LaunchServices (macOS) or by the terminal
  server or D-Bus (GNOME). It does **not** inherit the Bash tool's environment, so variables such as a venv `PATH`
  are lost.
  - Run `CMD` through a login shell (`$SHELL -lc '…'`) or by absolute path.
  - Pass the variables the board needs explicitly: `open --env`, Ghostty `environment variables`, or a `cd … &&
    VAR=… exec …` wrapper.

## 5. Fallback when no terminal can be found

1. Exit non-zero with one actionable message:
   `ticketmaster: could not open a terminal window (<reason>). Run it yourself: cd '<dir>' && ticketmaster`.
   From Claude Code the user can paste that after `!`, or into any terminal.
2. Mention the override (`TICKETMASTER_TERMINAL='kitty --directory {dir} {cmd}'` or similar) so the next run
   works.
3. Do not try to run the TUI in the Bash tool's own output. It has no TTY and would hang or print garbage until the
   2-minute timeout.

Minimal sketch of the launch step:

```python
import os, shlex, subprocess, sys

def popen_detached(argv, cwd):
    return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)

def macos_terminal_app(dir, cmd):  # cmd: list[str]
    script = f"cd {shlex.quote(dir)} && exec {shlex.join(cmd)}"
    return subprocess.run(["osascript", "-e", "on run argv", "-e", 'tell application "Terminal"',
                           "-e", "activate", "-e", "do script (item 1 of argv)", "-e", "end tell",
                           "-e", "end run", script], capture_output=True, timeout=15)
```

## Not verified

- No recipe was executed end to end, because opening windows was out of scope.
- These environment markers were not verified: WezTerm (`TERM_PROGRAM=WezTerm`, `WEZTERM_PANE`), GNOME Terminal
  (`GNOME_TERMINAL_SCREEN`, `VTE_VERSION`) and Konsole `KONSOLE_VERSION`.
- Bundle ids for iTerm2, WezTerm, kitty and Alacritty.
- Whether iTerm2's `command` is run through a shell.
- `open -na Alacritty.app --args` on macOS.
- The current status of the freedesktop xdg-terminal-exec spec (the merge-request page was unreachable), and which
  xdg-terminal-exec release added `--dir`.
- Behaviour under an enabled Claude Code sandbox: `open` under Seatbelt, and terminals on Linux under bubblewrap
  with the seccomp filter.
- Whether Claude Code kills the whole process group on timeout.
- Which app the TCC Automation prompt names.

## Sources

- Terminal.app scripting dictionary: `/System/Applications/Utilities/Terminal.app/Contents/Resources/Terminal.sdef`
  (local, `do script`)
- iTerm2 scripting dictionary: `/Applications/iTerm.app/Contents/Resources/iTerm2.sdef` (local, v3.7.3); [iTerm2
  AppleScript docs][iterm-as]; [iTerm2 variables][iterm-vars]
- Ghostty scripting dictionary: `/Applications/Ghostty.app/Contents/Resources/Ghostty.sdef` (local, v1.3.1);
  [Ghostty AppleScript][ghostty-as]; [Ghostty config reference][ghostty-config]; [Ghostty 1.2.0 release
  notes][ghostty-120]; [Ghostty README (GHOSTTY_RESOURCES_DIR)][ghostty-readme]
- [wezterm start][wezterm-start]; [WezTerm `term` config][wezterm-term]
- [kitty invocation][kitty-cli]; [kitty glossary (env vars)][kitty-glossary]
- [alacritty(1)][alacritty-man]; [alacritty-msg(1)][alacritty-msg]
- [gnome-terminal(1)][gnome-terminal]
- [Konsole command-line options][konsole-cli]; [Konsole scripting][konsole-scripting]
- [xdg-terminal-exec reference implementation and proposed spec][xte]
- [Debian Policy §11.8.3 x-terminal-emulator][debian-policy]; [sensible-terminal(1)][sensible-terminal]
- [open(1)][open-man] (also `man open` on macOS)
- Claude Code: [tools reference (Bash behaviour)][cc-tools], [environment variables][cc-env],
  [sandboxing][cc-sandbox], [settings reference (sandbox keys)][cc-settings]

[iterm-as]: https://iterm2.com/documentation-scripting.html
[iterm-vars]: https://iterm2.com/documentation-variables.html
[ghostty-as]: https://ghostty.org/docs/features/applescript
[ghostty-config]: https://ghostty.org/docs/config/reference
[ghostty-120]: https://ghostty.org/docs/install/release-notes/1-2-0
[ghostty-readme]: https://github.com/ghostty-org/ghostty/blob/main/README.md
[wezterm-start]: https://wezterm.org/cli/start.html
[wezterm-term]: https://wezterm.org/config/lua/config/term.html
[kitty-cli]: https://sw.kovidgoyal.net/kitty/invocation/
[kitty-glossary]: https://sw.kovidgoyal.net/kitty/glossary/
[alacritty-man]: https://man.archlinux.org/man/alacritty.1
[alacritty-msg]: https://man.archlinux.org/man/alacritty-msg.1.en
[gnome-terminal]: https://manpages.debian.org/testing/gnome-terminal/gnome-terminal.1.en.html
[konsole-cli]: https://docs.kde.org/stable_kf6/en/konsole/konsole/command-line-options.html
[konsole-scripting]: https://docs.kde.org/stable_kf6/en/konsole/konsole/scripting.html
[xte]: https://github.com/Vladimir-csp/xdg-terminal-exec
[debian-policy]: https://www.debian.org/doc/debian-policy/ch-customized-programs.html#packages-providing-a-terminal-emulator
[sensible-terminal]: https://manpages.debian.org/testing/sensible-utils/sensible-terminal.1.en.html
[open-man]: https://keith.github.io/xcode-man-pages/open.1.html
[cc-tools]: https://code.claude.com/docs/en/tools-reference#bash-tool-behavior
[cc-env]: https://code.claude.com/docs/en/env-vars
[cc-sandbox]: https://code.claude.com/docs/en/sandboxing
[cc-settings]: https://code.claude.com/docs/en/settings-reference
