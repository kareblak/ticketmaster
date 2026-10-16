# Web board: opening a browser, and protecting the localhost server

Research for #151 (map #149, "Ticketmaster web board view"). Investigated 2026-10-16 on macOS (Darwin 25.6),
Python 3.12.8 and 3.14.7, from a Claude Code Bash tool inside a herdr pane with the sandbox **off**. No browser window
was opened. Sandboxed behaviour comes from the Claude Code and sandbox-runtime docs, not from a run.

## Question

How does `ticketmaster --web` open the board URL in the user's browser when it runs from Claude Code's Bash tool
(sandbox on and off), from a herdr pane, and over SSH or without a display, and what should it print when it can't?
What does prior art (Jupyter's token URL, Host-header and Origin checks, DNS-rebinding defences) say about
protecting a localhost server whose POSTs start agent sessions?

## Recommendation

1. **Don't use `webbrowser.open`. Call the platform opener directly** (detached, argv list, stdio to DEVNULL):
   - an override first: `$BROWSER` (the convention Python and `xdg-open` both honour), read as an argv template with
     `%s`, or as a bare command that gets the URL appended;
   - macOS: `/usr/bin/open <url>` (LaunchServices; no osascript, no Automation consent prompt);
   - Linux: `xdg-open <url>`, **only** when `DISPLAY` or `WAYLAND_DISPLAY` is set.
   Never fall through to a text-mode browser.
2. **Skip the open, but keep serving, when there's no local GUI**: `SSH_CONNECTION`/`SSH_TTY` set (on either OS), or
   Linux without `DISPLAY`/`WAYLAND_DISPLAY`. The server is up, so this isn't a failure. Print the URL and the reason,
   and exit 0.
3. **The server opens the browser itself, once it's listening** (as Jupyter does). The launcher reads the result back
   and prints one line for Claude to relay, e.g.
   `Web board at http://127.0.0.1:53817/?token=… in herdr pane w3:p2 (ticketmaster-web), opened in the browser, for github:kareblak/ticketmaster.`
   When the open is skipped or fails, the line ends with `couldn't open a browser (<reason>); open the URL yourself`.
   That puts the open in the herdr pane's process, which the Bash sandbox doesn't cover, and it means the browser
   never loads a URL before anything is listening.
4. **Plain backend with the Bash sandbox on: refuse rather than half-start.** A detached server started from a
   sandboxed Bash command stays inside the sandbox. On macOS it can't listen without `allowLocalBinding`. On Linux its
   `localhost` is private, so the user's browser can't reach it. Exit 1 with the reason, the paste-able
   `cd '<dir>' && ticketmaster --web <args>` line (to run in a terminal, where the server runs in the foreground), and
   the settings that fix it: `"excludedCommands": ["ticketmaster *"]` on any OS, or `allowLocalBinding` +
   `allowAppleEvents` on macOS. This matches the terminal launcher's style in #148.
5. **Security: Jupyter's model, tightened for POSTs that start agents.**
   - Bind `127.0.0.1` only, and use a random 32-byte `secrets.token_urlsafe` token compared with
     `hmac.compare_digest`.
   - The token in the URL is only for the first load. The server swaps it for an `HttpOnly; SameSite=Strict` cookie
     named with the port, then redirects to the bare path, so the token leaves the address bar, history and
     `Referer`.
   - Every request needs a loopback `Host` (`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`). That is the
     DNS-rebinding defence.
   - Every state-changing request (POST: start a card) also needs the cookie (or `Authorization` header), an `Origin`
     equal to the server's own origin (missing or `null` is rejected), and a JSON body with
     `Content-Type: application/json`. A cross-site form or `fetch` can't send that without a CORS preflight, and the
     server never answers preflights.
   - No CORS headers at all. The SSE stream is a GET under the same Host and cookie checks. It has no side effects,
     but it would leak the board.

## 1. Opening a browser: what the tools actually do

### Python `webbrowser` (stdlib)

Read from source: CPython 3.12.8 at `/Library/Frameworks/Python.framework/Versions/3.12/lib/python3.12/webbrowser.py`
and Homebrew 3.14.7.

- **macOS.** `register_standard_browsers()` registers `MacOSXOSAScript('default')` first. Its `open()` pipes
  `open location "<url>"` to `osascript` (3.12) or `/usr/bin/osascript` (3.14). This probe printed
  `['MacOSX', 'chrome', 'firefox', 'safari']`:
  `python3 -c 'import webbrowser; webbrowser.register_standard_browsers(); print(webbrowser._tryorder)'`.
  - The docs add a `MacOS` class in **3.15** that "open[s] browsers via /usr/bin/open instead of osascript". They
    deprecate `MacOSXOSAScript` because "osascript may be blocked on managed systems due to its abuse potential"
    ([webbrowser docs][py-wb]).
  - ticketmaster supports Python ≥ 3.11 (`pyproject.toml`), so every supported version takes the osascript path.
  - Calling `/usr/bin/open` ourselves gets the 3.15 behaviour today.
- **Linux/BSD.** Graphical openers (`xdg-open` first) are registered only when `DISPLAY` or `WAYLAND_DISPLAY` is
  set. When `TERM` is set, it also registers `www-browser`, `links`, `elinks`, `lynx` and `w3m` as `GenericBrowser`,
  which **waits** for the process. The docs say: "If text-mode browsers are used, the calling process will block
  until the user exits the browser" ([webbrowser docs][py-wb]).
  - Over SSH with `w3m` installed, `webbrowser.open` would run a TUI browser inside the server process, or in
    Claude's TTY-less Bash tool.
- **Silent false success.** `BackgroundBrowser.open` (used for `xdg-open`) returns `p.poll() is None`, which is True
  as soon as the child is still running. A later failure of `xdg-open` never reaches the caller (source,
  `BackgroundBrowser.open`).
- **`$BROWSER`** is an `os.pathsep`-separated list tried ahead of the platform defaults. A part containing `%s` is a
  command line with the URL substituted ([webbrowser docs][py-wb]).
- Jupyter uses `webbrowser.get(...).open(...)` in a thread (jupyter_server `serverapp.py`, `launch_browser`), so it
  inherits all of the above.

### macOS `open`

- `open` opens a URL with the default handler through LaunchServices, and "Opened applications inherit environment
  variables just as if you had launched the application directly" (`man 1 open`). `-g` keeps the app in the
  background.
- No Apple Events to another app means no Automation (TCC) consent dialog. That is the advantage over osascript.
- **Over SSH** (expected, not run): `open` asks LaunchServices on the *remote* Mac. It either opens a browser on that
  Mac's console session, which the user can't see, or fails. So check `SSH_CONNECTION`/`SSH_TTY` on macOS too.
  sshd sets those variables for the session.

### `xdg-open` (xdg-utils, `scripts/xdg-open.in`, master)

- `has_display()` is `[ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]` (`xdg-utils-common.in`).
- In `open_generic()`, without a display it skips the X scheme handler. When `$BROWSER` is empty it then tries
  `BROWSER=www-browser:links2:elinks:links:lynx:w3m`, the same text-mode trap as Python.
- Exit codes: `exit_failure_operation_impossible` → 3 ("no method available"), `exit_failure_operation_failed` → 4
  ([xdg-utils source][xdg-open]).
- `xdg-open` honours `$BROWSER` too, and strips itself out of it to avoid recursion.
- So only call `xdg-open` when there is a display. Run it detached, without waiting for the browser. Desktop
  handlers (`gio open`, `kde-open`) return once the browser has the URL. Treat a quick non-zero exit as a failure.

### Claude Code's Bash tool

- **Sandbox off (the default)**: commands run with the user's full access ([sandboxing][cc-sandbox]). `open` and
  `xdg-open` work as from any shell. Here, a probe bound `127.0.0.1:0` fine.
- **Sandbox on**, quoting the docs:
  - Restrictions apply to "shell commands … and the processes they start" and to "the entire process tree"
    ([sandboxing][cc-sandbox], [sandbox-runtime][srt]). A server detached from a sandboxed command stays sandboxed.
  - "`open`, `osascript`, or browser-based auth flows fail with error `-600` on macOS". The sandbox blocks Apple
    Events and LaunchServices open requests unless `allowAppleEvents` is set, and "Claude Code ignores this key in
    project settings" ([sandboxing][cc-sandbox], [sandbox-runtime][srt]). `webbrowser.open` and `/usr/bin/open` both
    fail.
  - Listening on a port, on macOS, needs `network.allowLocalBinding` (default false). The docs warn that it also
    lets the command reach every other localhost service ([sandboxing][cc-sandbox], [settings][cc-settings]).
  - On Linux and WSL2, "a sandboxed command's `localhost` is private to that command … `allowLocalBinding` has no
    effect" ([sandboxing][cc-sandbox]). A sandboxed web server is unreachable from the user's browser, whatever the
    settings. On top of that, the seccomp filter blocks Unix sockets, so `xdg-open` can't reach D-Bus or X11 (see
    `research/terminal-launch-134`).
  - Ways out: `excludedCommands` (`"ticketmaster *"` matches how SKILL.md calls it, as `ticketmaster $ARGUMENTS`),
    or Claude's retry with `dangerouslyDisableSandbox`, which `allowUnsandboxedCommands: false` turns off
    ([sandboxing][cc-sandbox]).
  - No documented environment variable says "you are sandboxed" ([sandbox-runtime][srt]: only proxy and CA
    variables, which users also set themselves). Detect it from the failure instead:
    - macOS: `bind`/`listen` fails with `EPERM`, or `open` exits non-zero with `-600`;
    - Linux: the server binds fine but the browser can't connect. That can't be seen from inside. A heuristic is
      `/sys/class/net` holding only `lo` while the `HTTP_PROXY`/`HTTPS_PROXY` variables point at `localhost`.
      **Unverified.**
- **Environment.** The Bash tool's env carries `CLAUDECODE=1`, `CLAUDE_CODE_*` and possibly secrets. Scrub them from
  the opener's child as #148's terminal launcher does. A browser that is already running ignores it anyway, but a
  freshly launched one inherits it (`man 1 open`).

### herdr pane

- The web server's pane is started by the herdr server, not by the Bash tool, so the Claude sandbox doesn't apply to
  the server or its `open` call. That is the reason for recommendation 3.
  - The launching `herdr` CLI call from a sandboxed Bash still needs its Unix socket allowed. That issue already
    exists for the TUI board, and nothing here changes it.
- herdr can run on another machine: `herdr --remote <ssh-target>` attaches to a remote server, and `--machine` runs
  API commands on a saved SSH machine (`herdr --help`). A pane there runs on the remote host.
  - Whether `SSH_CONNECTION` reaches such a pane is **unverified**: the persistent server may have been started
    outside the SSH session.
  - The only safe universal behaviour is to **always print the URL** in the report line, whether or not the open
    succeeded.

### What to print

Mirror #148's terminal launcher:

| Situation | Server | Browser | Exit | Output |
| --- | --- | --- | --- | --- |
| Opened | running | opened | 0 | one report line with the URL |
| SSH / no display / opener missing / opener failed / `-600` | running | not opened | 0 | report line + `couldn't open a browser (<reason>); open the URL yourself` |
| Plain backend, sandbox blocks listening (macOS `EPERM`) or Linux sandbox detected | not started | – | 1 | reason, `cd '<dir>' && ticketmaster --web <args>`, and the `excludedCommands` / `allowLocalBinding` hint |
| Typed at a shell (foreground) | runs in place | opened or skipped | – | URL printed, as `jupyter` prints its token URL |

Over SSH, the honest output is the URL. The board binds `127.0.0.1` on the remote host. `ssh -L <port>:127.0.0.1:<port>`
would make it reachable from the laptop, and it would pass the Host check, because the browser sends
`localhost:<port>` or `127.0.0.1:<port>`. That counts as access from another device, which map #149 puts out of
scope, so the output shouldn't suggest it.

## 2. Protecting the localhost server: prior art

### Jupyter Server (source: `jupyter_server` main, `base/handlers.py`, `auth/identity.py`, `serverapp.py`)

- **Token.** It is generated with `binascii.hexlify(os.urandom(24))` (`identity.py`, `_token_default`). The token is
  accepted "in the `Authorization` header", "in a URL parameter, e.g. `?token=abcdef...`", or in the login form.
  "Once you have visited this URL, a cookie will be set." "This token is logged to the terminal, so that you can
  copy/paste the URL" ([Jupyter security][jp-sec]).
- **Cookie.** `set_login_cookie` defaults to `httponly=True` and a signed cookie. Its name takes "host into account
  to avoid collisions for multiple servers on one hostname with different ports" (`identity.py`). Cookies are not
  port-isolated, so two localhost servers share a cookie jar.
- **Host check (DNS rebinding).** `check_host()` strips the port and accepts loopback IPs or names in
  `local_hostnames` (default `["localhost"]`), unless `allow_remote_access` is set. Otherwise it logs "Blocking
  request with non-local 'Host'" (`handlers.py`; config: "Allow requests where the Host header doesn't point to a
  local server" ([config][jp-cfg])).
- **Origin / XSRF.** `check_origin()` lets a request through when `Origin` is missing ("Scripts"). Otherwise it
  allows only `Origin` == `Host` or the CORS allow-list. `check_xsrf_cookie()` **skips XSRF for token-authenticated
  requests**, and cookie-authenticated non-GETs need Tornado's `_xsrf` plus the Origin check (`handlers.py`).
- **Token not in the browser's argv.** Since notebook 5.7.2, Jupyter opens a local redirect HTML file instead of the
  token URL, "prevent[ing] the authentication token used to launch the browser from being visible … to other users
  on a multi-user system". It is disabled where the file path doesn't map, as in WSL and Chromebooks
  (`use_redirect_file` help text in `serverapp.py`).
  - Recommendation: not worth it for ticketmaster. `/usr/bin/open` and `xdg-open` live for milliseconds. The token
    is also printed in the report line that Claude relays anyway. A redirect file can also open in a non-browser
    `.html` handler. Keep it as optional hardening.

### MCP Streamable HTTP (closest analogue: a local server that makes an agent act)

From "Security Warning" in [MCP transports][mcp]:

1. "Servers **MUST** validate the `Origin` header on all incoming connections to prevent DNS rebinding attacks".
2. "When running locally, servers **SHOULD** bind only to localhost (127.0.0.1)".
3. "Servers **SHOULD** implement proper authentication for all connections".

This is stricter than Jupyter on Origin: it says validate, not "allow if missing". A browser always sends `Origin`
on a cross-origin POST, and on a same-origin `fetch` POST, so ticketmaster can require it on POSTs.

### Transmission CVE-2018-5702 (the canonical DNS-rebinding case)

- Transmission's daemon listened on localhost and relied on a session-id header. Tavis Ormandy showed that "any
  website can simply create a dns name that they are authorized to communicate with" and rebind it to 127.0.0.1.
- The fix "requir[es] a host whitelist for requests that cannot be proven to be secure", that is, a `Host`-header
  check ([transmission PR #468][tr-468]).
- Lesson: a token stops a rebinding attacker only if every route needs it. The Host check is cheap and covers any
  route that doesn't, such as the bare HTML shell or static assets.

### OWASP CSRF cheat sheet

From the [OWASP CSRF cheat sheet][owasp-csrf]:

- "If the Origin header is present, verify that its value matches the target origin."
- Fetch Metadata (`Sec-Fetch-Site`) is "a lightweight and reliable method to block obvious cross-site requests",
  with Origin verification as a mandatory fallback.
- Requests with custom headers are preflighted. `<form>` posts are "simple" requests.
- "`SameSite` is useful as a defense-in-depth control but it does not replace a proper CSRF defense".
- "Do not put tokens in URLs or logs." That applies to CSRF tokens. Jupyter's launch token is a bearer token put in
  a URL on purpose, which is why it should be swapped for a cookie and redirected away on first use.

### Browsers

- Chrome 142 ships a **Local Network Access** permission prompt for "any request from the public network to a local
  network or loopback destination", replacing Private Network Access ([Chrome LNA][chrome-lna]).
- It's Chrome-only and the page doesn't discuss DNS rebinding, so it is defence in depth and not something to rely
  on.

### Tokens

- `secrets.token_urlsafe(nbytes)`. "As of 2015, it is believed that 32 bytes (256 bits) of randomness is sufficient".
- Use `secrets.compare_digest` (constant time) to compare ([secrets docs][py-secrets]).

### Threat model in one line

Who can reach `127.0.0.1:<port>`:

- any local process of any user. The token is the defence. Optionally keep the URL out of other users' `ps`.
- any web page, through CSRF or DNS rebinding. Host check, Origin check, JSON content type, `SameSite=Strict`, no
  CORS.
- other sandboxed commands, when the user set `allowLocalBinding`. The token covers this, and the sandbox docs warn
  exactly about unauthenticated localhost services ([sandboxing][cc-sandbox]).

## Open points and unverified assumptions

- Nothing was run with the sandbox **on**. The `-600` on `open`, `EPERM` on listen (macOS) and the private
  `localhost` (Linux) are from the docs. The Linux sandbox heuristic (`/sys/class/net` only `lo`) is a guess.
- `open <url>` over SSH on macOS (remote console vs failure) wasn't run.
- Whether `SSH_CONNECTION` reaches herdr panes on a `--remote` server is unknown.
- How quickly `xdg-open`'s desktop handlers (`gio open`, `kde-open5`, `exo-open`) return varies. Treat a non-zero
  exit within ~2 s as a failure, and anything else as success.
- Does `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` matter? It scrubs Claude's own credentials from subprocesses, so it
  doesn't change the opener.

## Sources

- [py-wb]: https://docs.python.org/3/library/webbrowser.html, plus local source of CPython 3.12.8 and 3.14.7
  `webbrowser.py`
- [py-secrets]: https://docs.python.org/3/library/secrets.html
- `man 1 open` (macOS 26)
- [xdg-open]: https://gitlab.freedesktop.org/xdg/xdg-utils/-/blob/master/scripts/xdg-open.in and
  `scripts/xdg-utils-common.in`
- [cc-sandbox]: https://code.claude.com/docs/en/sandboxing
- [cc-settings]: https://code.claude.com/docs/en/settings-reference
- [srt]: https://github.com/anthropics/sandbox-runtime
- [jp-sec]: https://jupyter-server.readthedocs.io/en/latest/operators/security.html
- [jp-cfg]: https://jupyter-server.readthedocs.io/en/latest/other/full-config.html
- Jupyter Server source: https://github.com/jupyter-server/jupyter_server (`jupyter_server/base/handlers.py`,
  `auth/identity.py`, `serverapp.py`, main branch)
- [mcp]: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- [tr-468]: https://github.com/transmission/transmission/pull/468 (CVE-2018-5702)
- [owasp-csrf]: https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
- [chrome-lna]: https://developer.chrome.com/blog/local-network-access
- Prior ticketmaster research: `research/terminal-launch-134` branch, `docs/research/terminal-launch.md`

[py-wb]: https://docs.python.org/3/library/webbrowser.html
[py-secrets]: https://docs.python.org/3/library/secrets.html
[xdg-open]: https://gitlab.freedesktop.org/xdg/xdg-utils/-/blob/master/scripts/xdg-open.in
[cc-sandbox]: https://code.claude.com/docs/en/sandboxing
[cc-settings]: https://code.claude.com/docs/en/settings-reference
[srt]: https://github.com/anthropics/sandbox-runtime
[jp-sec]: https://jupyter-server.readthedocs.io/en/latest/operators/security.html
[jp-cfg]: https://jupyter-server.readthedocs.io/en/latest/other/full-config.html
[mcp]: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
[tr-468]: https://github.com/transmission/transmission/pull/468
[owasp-csrf]: https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
[chrome-lna]: https://developer.chrome.com/blog/local-network-access
