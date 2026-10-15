# Opening a new terminal window from a process

Question (#134, part of map #132): how can a Python process reliably open a new terminal window (or tab) that runs a
given command in a given directory, on macOS and Linux? The answer covers how to detect the user's terminal, the
launch recipe per terminal, what changes when the process runs inside Claude Code's Bash tool, and a fallback.

Researched 2026-10-15 against each terminal's official docs or source, the freedesktop xdg-terminal-exec spec,
Debian Policy, and the local macOS 27.0.1 man pages and scripting dictionaries (`sdef`), with Ghostty 1.3.1 and
iTerm2 installed. Claims marked **(local)** were checked on this machine. Claims marked **(unverified)** have no
primary source yet.

## Summary

1. **Detect the terminal from the environment first, then fall back per platform.** On macOS, read
   `$TERM_PROGRAM` (`Apple_Terminal`, `iTerm.app`, `ghostty`, `WezTerm`), plus `KITTY_WINDOW_ID` and
   `ALACRITTY_WINDOW_ID`. If none of these match, use Terminal.app, which every Mac has. On Linux, check the same
   variables, then `xdg-terminal-exec`, `$TERMINAL`, `x-terminal-emulator`, and finally a `which` scan of known
   terminals. Give the user an override (env var or config) that wins over all of this.
2. **macOS: drive the app with AppleScript through `osascript`, not by spawning its binary.** This applies to
   Terminal.app, iTerm2, and Ghostty 1.3 or later. Ghostty refuses to run as a terminal from the CLI on macOS.
   `open -na Ghostty.app --args …` works, but it starts a second app instance. Pass the directory and command as
   `argv` to an `on run argv` handler, so that nothing has to be escaped inside an AppleScript string.
3. **Linux: every listed terminal takes a working directory and a command as flags.** Spawn the terminal
   detached (`start_new_session=True`, stdio set to `DEVNULL`) and don't wait for it. Prefer `xdg-terminal-exec
   --dir=DIR -- cmd…` when it is installed, because it handles the per-terminal flag differences for you.
4. **Inside Claude Code's Bash tool** there is no TTY (stdin is `/dev/null` **(local)**), so the board can't run in
   place and a new window is the only option. The child inherits `CLAUDECODE`, `CLAUDE_CODE_*` and `HERDR_*`;
   scrub them. If the Bash sandbox is on, it blocks Apple Events on macOS (error `-600` from `open` and
   `osascript`), and Linux GUI and D-Bus sockets may be blocked too. The user's fix is `excludedCommands` (or
   `allowAppleEvents`); the program's job is to detect the failure and fall back.
5. **Fallback**: when no terminal is found, there is no display (`DISPLAY` and `WAYLAND_DISPLAY` both unset on
   Linux), or the launch fails, print a ready-to-paste `cd <dir> && <cmd>` line and exit non-zero with a clear
   message. Don't try to run the TUI in place.

## 1. Detecting the user's terminal

| Signal | Set by | Source |
|---|---|---|
| `TERM_PROGRAM=ghostty` (and `TERM_PROGRAM_VERSION`) | Ghostty | `src/termio/Exec.zig:750` in ghostty-org/ghostty |
| `TERM_PROGRAM=WezTerm` | WezTerm ("an emerging de-facto standard for identifying the terminal") | `config/src/config.rs:1619-1622` in wezterm/wezterm |
| `TERM_PROGRAM=Apple_Terminal`, `TERM_PROGRAM=iTerm.app` | Terminal.app, iTerm2 | Widely observed; no Apple or iTerm2 doc found **(unverified)** |
| `KITTY_WINDOW_ID`, `KITTY_LISTEN_ON` | kitty | [kitty remote control](https://sw.kovidgoyal.net/kitty/remote-control/) |
| `ALACRITTY_WINDOW_ID` (`ALACRITTY_SOCKET` for IPC) | Alacritty | [alacritty-msg(1)](https://alacritty.org/cmd-alacritty-msg.html) |
| `$TERMINAL` | User convention only | No spec defines it **(unverified)**. Treat it as a binary name that accepts `-e` |
| `xdg-terminal-exec` on PATH | freedesktop terminal-wg proposal, reference implementation | [Vladimir-csp/xdg-terminal-exec](https://github.com/Vladimir-csp/xdg-terminal-exec) |
| `x-terminal-emulator` on PATH | Debian alternatives | [Debian Policy 11.8.3](https://www.debian.org/doc/debian-policy/ch-customized-programs.html) |

Caveats:

- A multiplexer between the user's terminal and the process rewrites or drops `TERM_PROGRAM`. In this session,
  which runs in a herdr pane inside Ghostty, `TERM_PROGRAM` is **empty** **(local)**. tmux sets its own value
  **(unverified)**. For ticketmaster this matters little, because inside herdr the herdr backend is chosen anyway.
  It does mean the plain backend can't assume `TERM_PROGRAM` is always present, so on macOS the Terminal.app
  fallback has to stay.
- `x-terminal-emulator` only guarantees `-e cmd args…`, which is "as though the arguments were passed directly to
  `execvp`", and `-T title` (Debian Policy 11.8.3). It has **no working-directory option**, so wrap the command:
  `x-terminal-emulator -e sh -c 'cd "$1" && shift && exec "$@"' sh DIR cmd…`. The same wrapper fits a bare
  `$TERMINAL`.
- `xdg-terminal-exec` takes `[--dir=DIR] [--title=T] [--app-id=ID] [--hold] [-e|--] cmd…`. It picks the terminal
  from `xdg-terminals.list` in the XDG config dirs (desktop-specific variants such as `gnome-xdg-terminals.list`),
  and otherwise from the first desktop entry with the `TerminalEmulator` category. Each terminal's `.desktop` file
  declares its own flags through `X-TerminalArgExec`, `X-TerminalArgDir`, `X-TerminalArgTitle`,
  `X-TerminalArgAppId` and `X-TerminalArgHold`. Its README lists xterm, alacritty, kitty, foot and qterminal as
  compliant ([source](https://github.com/Vladimir-csp/xdg-terminal-exec)). The freedesktop merge request was not
  reachable: gitlab.freedesktop.org returned an Anubis bot block.

Recommended order:

1. Override.
2. Environment sniffing: the terminal the user is already in.
3. macOS: Terminal.app. Linux: `xdg-terminal-exec`, then `$TERMINAL`, then `x-terminal-emulator`, then a `which`
   scan of `ghostty kitty wezterm alacritty gnome-terminal konsole`.
4. The printed fallback.

## 2. Launch recipes

Each recipe opens a new window in directory `DIR` that runs `CMD…`. In the Python sketches, `cmd` is an argv list.

### macOS

**Terminal.app.** The scripting dictionary has `do script`: "Runs a UNIX shell script or command." Without an
`in` target, it opens a new window and returns "the tab the command was executed in" **(local, `sdef
/System/Applications/Utilities/Terminal.app`)**. The command is typed into the user's login shell, so build it
with `shlex.quote`:

```
osascript -e 'on run argv' -e 'tell application "Terminal"' -e 'activate' \
  -e 'do script (item 1 of argv)' -e 'end tell' -e 'end run' "cd 'DIR' && CMD"
```

`osascript` passes the arguments after the script to the `run` handler's `argv` (`man osascript` **(local)**), so
the shell string never goes through AppleScript string escaping.

**iTerm2.** Use `create window with default profile`, with an optional `command` parameter ("Shell command to
run") **(local, `sdef /Applications/iTerm.app`)**. A `command` replaces the profile's login shell, so the window
closes when the command exits. To keep a shell, create the window without `command`, then
`tell current session of w to write text "cd 'DIR' && CMD"`
([iTerm2 scripting docs](https://iterm2.com/documentation-scripting.html)). iTerm2 marks AppleScript
**deprecated** in favour of its Python API, but the dictionary still ships. The Python API needs the `iterm2`
package and an enabled API server, which is too heavy for a fallback.

**Ghostty 1.3+.** Ghostty 1.3.0 added AppleScript ([docs](https://ghostty.org/docs/features/applescript)). Its
`surface configuration` record has `initial working directory`, `command`, `initial input`, `wait after command`
and `environment variables`. `new window with configuration cfg` and `new tab` take it **(local, Ghostty 1.3.1
sdef)**. A user can disable this with `macos-applescript = false`
([config reference](https://ghostty.org/docs/config/reference)).

```
osascript -e 'on run argv' -e 'tell application "Ghostty"' \
  -e 'set cfg to new surface configuration' \
  -e 'set initial working directory of cfg to item 1 of argv' \
  -e 'set command of cfg to item 2 of argv' \
  -e 'new window with configuration cfg' -e 'activate' -e 'end tell' -e 'end run' DIR "CMD"
```

The `ghostty` binary on macOS says: "launching the terminal emulator from the CLI is not supported … Use
`open -na Ghostty.app` … `--args --foo=bar`" (`ghostty +help` **(local)**). `+new-window` "is not supported on this
platform" **(local)**. So `open -na Ghostty.app --args --working-directory=DIR -e CMD…` is the fallback for Ghostty
older than 1.3. The `-n` flag means "Open a new instance of the application(s) even if one is already running"
(`man open` **(local)**), which gives a second Ghostty process with its own Dock icon.

**WezTerm, kitty, Alacritty on macOS.** Their CLIs work the same as on Linux (below), as long as the binary is
reachable: on PATH, or `/Applications/<App>.app/Contents/MacOS/<bin>`. kitty's docs suggest `open -a kitty.app -n`
for a separate instance on macOS ([kitty invocation](https://sw.kovidgoyal.net/kitty/invocation/)).

**macOS permissions.** The first `osascript` call that controls another app triggers the Automation (TCC) consent
prompt ([Ghostty AppleScript docs](https://ghostty.org/docs/features/applescript)). The user must allow it once.
Until they do, or if they deny it, `osascript` fails, and that failure must lead to the fallback.

### Linux, and the cross-platform CLIs

| Terminal | New window in DIR running CMD | Notes | Source |
|---|---|---|---|
| xdg-terminal-exec | `xdg-terminal-exec --dir=DIR -- CMD…` | Chooses and adapts to the user's terminal | [README](https://github.com/Vladimir-csp/xdg-terminal-exec) |
| GNOME Terminal | `gnome-terminal --window --working-directory=DIR -- CMD…` | `-e`/`-x` are deprecated in favour of `--`. Returns at once because gnome-terminal-server owns the window. `--wait` blocks until the child exits | [gnome-terminal(1)](https://manpages.debian.org/testing/gnome-terminal/gnome-terminal.1.en.html) |
| Konsole | `konsole --workdir DIR -e CMD…` | `-e` "captures all following arguments", so put it last. `--new-tab` for a tab. `--hold` keeps it open | [Konsole command-line options](https://docs.kde.org/stable_kf6/en/konsole/konsole/command-line-options.html) |
| kitty | `kitty --single-instance --directory DIR CMD…` | Inside kitty with remote control on: `kitten @ launch --type=os-window --cwd DIR CMD…` | [invocation](https://sw.kovidgoyal.net/kitty/invocation/), [remote control](https://sw.kovidgoyal.net/kitty/remote-control/) |
| WezTerm | `wezterm start --cwd DIR -- CMD…` | Reuses a running GUI by default. `--new-tab` for a tab. `--always-new-process` forces a separate one | [wezterm start](https://wezterm.org/cli/start.html) |
| Alacritty | `alacritty msg create-window --working-directory DIR -e CMD…`, else `alacritty --working-directory DIR -e CMD…` | `msg` needs a running instance's socket. `-e` must be last | [alacritty(1)](https://alacritty.org/cmd-alacritty.html), [alacritty-msg(1)](https://alacritty.org/cmd-alacritty-msg.html) |
| Ghostty (GTK) | 1.3+: `ghostty +new-window --working-directory=DIR -e CMD…`. 1.2: `+new-window -e CMD…`, with no cwd. Any version: `ghostty --working-directory=DIR -e CMD…` | `+new-window` goes through D-Bus to the running instance, or D-Bus-activates one ([systemd docs](https://ghostty.org/docs/linux/systemd)). Plain `-e` forces `gtk-single-instance=false`, a new process ([config reference](https://ghostty.org/docs/config/reference)) | `src/cli/new_window.zig` at `v1.2.0` and `v1.3.0` in ghostty-org/ghostty |
| x-terminal-emulator / `$TERMINAL` | `x-terminal-emulator -e sh -c 'cd "$1" && shift && exec "$@"' sh DIR CMD…` | No cwd option. Args are execvp'd | Debian Policy 11.8.3 |

Python rules that apply to all of them:

- Launch with `subprocess.Popen(argv, cwd=DIR, env=clean_env, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
  start_new_session=True)`, and don't wait. kitty, Alacritty, Konsole and plain `ghostty -e` start a new process
  that lives as long as the window, so a non-detached child would tie the window to the caller and to Claude Code's
  Bash timeout. Also pass `cwd=DIR`, because a terminal that inherits its directory (`x-terminal-emulator`, Ghostty
  `working-directory=inherit`) then lands right.
- A clean exit from the launcher only proves the request was accepted. For `osascript`, check the return code and
  stderr, which carry TCC and `-600` errors. For gnome-terminal and `+new-window`, a zero exit means the server
  took the request.
- Use an absolute path to the command, such as `sys.executable -m ticketmaster` or `shutil.which("ticketmaster")`.
  The window's shell might not have the venv on PATH. A window opened through AppleScript runs a login shell in
  the terminal app's environment, not the caller's.

## 3. Launching from inside Claude Code's Bash tool

- **No TTY.** The Bash tool runs `zsh -c '…' < /dev/null`, and `tty` prints "not a tty" **(local)**. A TUI can't
  run there, which is why the new window is needed.
- **Inherited environment.** The process sees `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_ENTRYPOINT`,
  `CLAUDE_PID`, `CLAUDE_CODE_MESSAGING_SOCKET`/`_TOKEN` and, inside herdr, `HERDR_ENV`/`HERDR_PANE_ID`/… **(local)**.
  A terminal spawned directly (the Linux CLIs, `wezterm start`, kitty, Alacritty) passes these on to the board
  and on to any `claude` it starts. Scrub `CLAUDECODE`, `CLAUDE_CODE_*`, `CLAUDE_PID` and `HERDR_*` from the
  child env, so the new window doesn't look like a nested Claude session or a herdr pane. AppleScript launches
  don't inherit the env at all, so this matters only for direct spawns.
- **Sandbox, when the user enabled it**
  ([Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing)):
  - macOS uses Seatbelt. "The sandbox blocks Apple Events by default", and `open` and `osascript` fail with error
    `-600`. The user's fixes are `sandbox.allowAppleEvents: true` (user, managed or CLI settings only; project
    settings are ignored) or an `excludedCommands` pattern. Those docs warn that `allowAppleEvents` removes
    code-execution isolation.
  - Linux uses bubblewrap, plus an optional seccomp filter that can block Unix sockets (`allowUnixSockets`,
    `allowAllUnixSockets`). X11, Wayland and D-Bus all use Unix sockets, so gnome-terminal, `ghostty +new-window`
    and opening any GUI window may fail when sandboxed. This is inferred from the socket rules; it wasn't tested
    **(unverified)**.
  - In either case Claude may retry with `dangerouslyDisableSandbox`, which goes through the permission prompt.
    The robust design is: ticketmaster prints a clear error that names the cause ("could not open a terminal
    window: Apple Events blocked (-600); add `ticketmaster *` to sandbox.excludedCommands"), then prints the
    fallback line. The skill text can tell Claude to retry unsandboxed.
  - On this machine `~/.claude/settings.json` only has `sandbox.excludedCommands: ["op"]` **(local)**, and
    `osascript` queries ran fine from the Bash tool.

## 4. Fallback when no terminal can be found

Use the fallback when any of these hold:

- Detection finds nothing.
- On Linux, `DISPLAY` and `WAYLAND_DISPLAY` are both unset, for example over SSH or on a headless box.
- The launch command fails: non-zero exit, `FileNotFoundError`, a TCC denial, or `-600`.

Then ticketmaster prints, on stdout, a single line the user can paste into a terminal: `cd '<dir>' && <absolute
cmd>`. It also prints the reason on stderr and exits non-zero. The skill relays both, so the user can open a
terminal and paste the line. Out of scope here: Claude desktop and web hosts, where there is no terminal to open
(per #132).

## Open points for the spec

- Whether the override is an env var (`TICKETMASTER_TERMINAL=kitty`), a `.ticketmaster.toml` key, or both, and
  whether it may hold a full command template (`"foot -D {dir} -- {cmd}"`) for terminals not listed here.
- New window or new tab. Every macOS recipe and most Linux ones can do either. The #132 decision says "window".
- `TERM_PROGRAM` values for Terminal.app and iTerm2, and whether tmux and herdr overwrite or drop it, are taken
  from observation only. They should be checked in a plain (non-herdr) Claude Code session before the detection
  table is final.
