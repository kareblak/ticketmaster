# Session status without herdr (#133)

Question (ticket #133, map #132): can ticketmaster learn a session's status (idle / working / blocked / done) for
a worktree without herdr, cheaply and reliably?

Investigated 2026-10-15 against Claude Code 2.1.292 on macOS, the official docs at code.claude.com, and a read-only
look at `~/.claude` with 31 live sessions.

## Answer

Yes. **`claude agents --json`** is the documented, supported way to read every live session's state from outside
Claude Code. It covers plain interactive `claude` processes, not only background ones. It returns `cwd`, `pid`
and `status` (`busy` | `waiting` | `idle`), plus `waitingFor` when the session is waiting. One call lists all
sessions in about 0.3 s, and nothing needs installing.

It maps onto the board's vocabulary as `busy` to `working`, `waiting` to `blocked` and `idle` to `idle`. It
cannot tell `done` from `idle`, but herdr's own `done` is only "idle, and nobody has looked yet", which is
explained under the first caveat below.

## The vocabulary to match

`ticketmaster/herdr.py:36` ranks `blocked`, `working`, `idle`, `done` and `unknown`, with `None` for a worktree
that has no agent. `ticketmaster/board.py:46-53` renders them as `⚠ needs you`, `● working`, `◌ idle`, `✓ done`,
`? unknown` and `○ no agent`. The values come straight from herdr's `agent list` field `agent_status`
(`herdr.py:218`). Herdr's skill text defines them:

> "`idle` and `done` both mean the agent is ready for input. The CLI/API uses the server's seen state to
> distinguish them; explicit focus commands mark the target seen, while reads do not. ... `blocked` means Herdr
> recognized an approval or question UI. `unknown` means an agent is present but Herdr cannot classify it
> confidently."
> (`herdr --skill`)

## Sources compared

| Source | States it distinguishes | Latency | Install needs | How the board reads it |
|---|---|---|---|---|
| `claude agents --json` | working (`busy`), blocked (`waiting` + `waitingFor`), idle (`idle`); for interactive sessions, done is the same as idle | Live per poll, about 0.3 s per call for all sessions | None. `claude` must be on PATH (documented in agent-view, works on 2.1.292) | Subprocess each tick, then filter entries whose `cwd` lies in a ticket worktree |
| `~/.claude/sessions/<pid>.json` (the registry behind the above) | The same, plus a raw `shell` status | Live, read as a file | None | Glob and parse JSON. **Undocumented** |
| Plugin hooks writing a status file | working, blocked, idle, and done if the board tracks "seen" | Immediate on each event, except that `Notification` is gated about 6 s | `hooks/hooks.json` in the ticketmaster plugin (it has none today), and the plugin must be enabled | Read `<dir>/<session_id>.json` keyed by `cwd`; stale files need a PID or liveness check |
| Transcripts `~/.claude/projects/<slug>/<session>.jsonl` | working and idle, heuristically from the last entry; not blocked | Written as entries arrive | None | Tail the newest jsonl per project slug. Internal format, and expensive |
| Status line command | None: its input has no state field, and it stops running while the session is idle | Event-driven, 300 ms debounce | User `statusLine` setting (one per user; this user's is already taken) | Would need the script to write a side file |
| `ps` / `lsof -d cwd` | Only whether a `claude` process is running with its cwd in the worktree | Live | None | `pgrep claude` then `lsof -a -p <pid> -d cwd`. Gives presence, not state |

## Findings per source

### `claude agents --json` (recommended)

The agent-view docs (https://code.claude.com/docs/en/agent-view#list-sessions-as-json) say:

> "`claude agents --json` prints active sessions as a JSON array and exits: every live session, plus background
> sessions that are still working or blocked even when their process has exited. Add `--all` to also include
> completed background sessions, and `--cwd <path>` to limit the list to sessions started under that directory."

The documented fields:

- `cwd`, `kind` (`interactive` | `background`) and `startedAt` are always present.
- `state` (`working` | `blocked` | `done` | `failed` | `stopped`) is present for **background sessions only**.
- `pid` and `status` are present "while the process is alive". `status` is one of `busy`, `waiting` or `idle`.
- `waitingFor` is present when `status` is `waiting`. Its values are `permission prompt`, `input needed`,
  `sandbox request`, `worker request` and `dialog open`.
- `sessionId` and `name` are present when set.

The docs call this the supported interface:

> "`claude agents --json` is the supported way to read session state from outside Claude Code, for example from a
> status bar, a scheduler, or another Claude session that supervises background work."

The same page warns that `~/.claude/jobs/<id>/` "are not a stable interface".

Observed locally, the call took 0.297 s wall time and returned 34 entries. The 30 live entries matched every live
PID in `~/.claude/sessions/`. Every interactive entry had `state: null` and a `status` of `busy` or `idle`. Each
entry's `cwd` was the herdr worktree path, for example `.../ticketmaster/issue-135`, so the board can match
sessions to worktrees the same way it matches herdr agents today.

These sessions run inside herdr. A plain-backend session in a separate terminal window is the same `claude`
binary and registers the same way. The docs say "every live session", and nothing in the listing depends on
herdr. I did not test a session started outside herdr.

### `~/.claude/sessions/<pid>.json`

There is one file per live process, and it is removed on exit: all 31 files had a live PID. The fields are
`pid`, `sessionId`, `cwd`, `startedAt`, `procStart`, `version`, `kind`, `entrypoint`, `name`, `status`,
`statusUpdatedAt`, `waitingFor`, `messagingSocketPath`, and `jobId` and `agent` for background sessions.

`status` was `busy`, `idle`, `waiting` (with `waitingFor: "input needed"`), and once `shell`.
`claude agents --json` reported that `shell` session as `busy`, so the CLI normalises the raw value.

No docs page describes this directory. Reading it directly would save the subprocess, but it is the kind of
internal file the docs steer scripts away from.

### Hooks (plugin `hooks/hooks.json`)

Hooks reference: https://code.claude.com/docs/en/hooks

- **Plugin hooks apply automatically.** The docs say: "Define plugin hooks in `hooks/hooks.json` ... When a plugin
  is enabled, its hooks merge with your user and project hooks." The scope table lists them as active "When
  plugin is enabled". `disableAllHooks` turns them off.
- **The ticketmaster plugin ships no hooks today.** `.claude-plugin/plugin.json` has no `hooks` key, and there is
  no `hooks/` directory. It also has to be enabled in the session being watched, not only in the board's session.
- **Common input fields**, which every event receives: `session_id`, `transcript_path`, `cwd`, `hook_event_name`,
  and `permission_mode` on most events. `agent_id` and `agent_type` are added inside subagents.
  `CLAUDE_PLUGIN_DATA` is exported as a persistent per-plugin directory.
- **Events that map to the states:**
  - Working:
    - `UserPromptSubmit` (`prompt`) fires before Claude processes a prompt.
    - `PreToolUse` and `PostToolUse` (`tool_name`, `tool_input`, `tool_use_id`) also clear a blocked state after
      an approval.
  - Blocked:
    - `PermissionRequest` (`tool_name`, `tool_input`) fires immediately when the prompt appears.
    - `Notification` with `notification_type` `permission_prompt`, `elicitation_dialog` or
      `elicitation_url_dialog` fires only "once you haven't typed for about six seconds". The docs advise:
      "To run a hook immediately when Claude asks for permission to use a tool, use PermissionRequest instead."
  - Idle or done:
    - `Stop` (`last_assistant_message`). Stop "Does not run if the stoppage occurred due to a user interrupt.
      API errors fire StopFailure instead." `StopFailure` carries `error_type`.
    - `Notification` `idle_prompt` comes about 60 s after a response.
  - Gone: `SessionEnd`, with `reason` one of `clear`, `resume`, `logout`, `prompt_input_exit` or `other`. It has
    a 1.5 s shared budget. `SessionStart` has `source` (`startup`, `resume`, `clear`, `compact` or `fork`).
  - `SubagentStop` fires per subagent. Hooks should ignore events that carry `agent_id` for main-session status.
- **Gaps.** An Esc interrupt fires no `Stop`, so the file stays "working". A crash or kill fires no `SessionEnd`,
  so the board must check whether the PID is still alive. A `AskUserQuestion` prompt shows up only as the
  6-second-gated `Notification` or as a `PreToolUse` on that tool. Hooks are synchronous unless `"async": true`.
  In short, this approach re-implements what `claude agents --json` already exposes, with more failure modes.
- **Herdr does this today too, but only partly.** `~/.claude/hooks/herdr-agent-state.sh`, wired as a user
  `SessionStart` hook, reports only `session_id` and `transcript_path` to herdr's socket. Herdr gets its states
  by watching the screen, not from hooks.

### Transcripts

`~/.claude/projects/<cwd-slug>/<session_id>.jsonl` holds one JSON object per line with a `type` field. The types
seen were `user`, `assistant` (with `message.stop_reason`), `attachment`, `permission-mode`, `mode`,
`last-prompt`, `custom-title` and `file-history-snapshot`, and each line carries `cwd`, `sessionId`,
`gitBranch` and `timestamp`.

A pending permission prompt is not written, so this source cannot show blocked. Inferring working or idle from
the last line is a guess, and the format is internal. It is not a candidate.

### Status line

The docs (https://code.claude.com/docs/en/statusline) describe when it updates:

> "Your script runs once when a session starts ... After that, it runs again when: A new assistant message
> arrives ..."

They also say "The event-driven triggers can go quiet when the main session is idle". Its input has `session_id`,
`cwd`, `workspace.git_worktree` and `transcript_path`, but no state or permission field. It is also a single
user-level setting, already used here by `~/.claude/statusline.sh`. It is not a candidate.

### Process signals

`ps` and `lsof -a -p <pid> -d cwd` find a `claude` process and its cwd, for example
`n/Users/kareblak/.herdr/worktrees/ticketmaster/issue-135`. That tells the board whether an agent is present,
which is the difference between `None` and `unknown`, but not what the agent is doing. It is redundant, because
`claude agents --json` already returns `pid` and `cwd`.

## Caveats

- **`done` vs `idle`.** For interactive sessions, `claude agents --json` gives only `idle`. Herdr's `done` means
  idle and not yet seen since the turn finished. To keep `✓ done`, the board would have to track "seen" itself,
  for example from when it last focused the session and the registry's `statusUpdatedAt`. That field is
  undocumented.
- **Cost.** Each poll spawns the `claude` CLI, which took about 0.3 s for 34 sessions. That
  is fine at the board's refresh cadence, but it should run off the UI thread.
- **`disableAgentView`.** The setting and `CLAUDE_CODE_DISABLE_AGENT_VIEW` turn off "background agents and agent
  view entirely". I did not verify whether `claude agents --json` still lists interactive sessions when they are
  set.
- **Version floor.** The docs give no minimum version for the `--json` fields. The behaviour was confirmed on
  2.1.292.
- **`cwd` drift.** `--cwd` filters on the directory a session was *started* under. I did not verify whether `cwd`
  follows a session that changes directory (`CwdChanged`). Ticket sessions start in their worktree, so this
  rarely matters.
- **Unexpected `status` values.** The raw registry can hold `shell`, which the CLI reports as `busy`. The board
  should map any unexpected `status` to `unknown`.
