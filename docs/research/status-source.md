# Session status source without herdr

Research for #133 (map #132). Question: can ticketmaster learn a session's status (idle / working / blocked on a
prompt / done) for a worktree without herdr, cheaply and reliably?

Investigated 2026-10-15 against Claude Code 2.1.292 on macOS, the official docs at code.claude.com, and a
read-only look at `~/.claude` on a machine with 28 live sessions.

## Answer

Yes. **`claude agents --json` is a cheap, documented and supported source**, and it needs nothing installed.
It lists every live Claude Code session, interactive ones included, with its `cwd`, `pid` and a live `status`.
A board can poll it on its normal refresh and match sessions to worktrees by `cwd`.

- It distinguishes **working** (`busy`), **blocked** (`waiting`, with `waitingFor` naming the prompt) and
  **idle** (`idle`) for every session.
- It reports **done** only for *background* sessions (`state: done`). For a plain interactive `claude`, a finished
  turn and an idle prompt both read `idle`.
- One call takes about 0.27 to 0.30 s wall time and about 0.3 s CPU. That is fine at the board's default 5 s
  interval.

Recommendation: the plain backend reads status from `claude agents --json`. It maps `busy` to `working`, `waiting`
to `blocked` and `idle` to `idle`. A worktree with no matching live entry has no agent (`None`). `done` comes from
`state` when the session is a background one. Hooks, transcripts, the statusline and process inspection are all
worse; see the comparison below.

## The source: `claude agents --json`

The CLI reference describes `claude agents` with "`--json` to print active sessions as a JSON array for scripting
(`--json --all` also includes completed background sessions)" and "`--cwd <path>` to show only sessions started
under that directory" ([CLI reference](https://code.claude.com/docs/en/cli-reference#cli-commands)). `claude
agents --help` on 2.1.292 says: "Print active sessions (interactive and background) as a JSON array and exit (for
scripting; does not require a TTY)".

The agent-view docs name it as the supported interface: "`claude agents --json` is the supported way to read
session state from outside Claude Code, for example from a status bar, a scheduler, or another Claude session that
supervises background work" ([Agent view: Read session state from a
script](https://code.claude.com/docs/en/agent-view#read-session-state-from-a-script)).

Fields ([Agent view: List sessions as JSON](https://code.claude.com/docs/en/agent-view#list-sessions-as-json)):

| Field | Present | Meaning |
| :- | :- | :- |
| `cwd`, `kind`, `startedAt` | Always | Working directory; `interactive` or `background`; start time in ms |
| `pid`, `status` | While the process is alive | `busy`, `waiting` or `idle` |
| `waitingFor` | When `status` is `waiting` | `permission prompt`, `input needed`, `sandbox request`, `worker request`, `dialog open` |
| `state` | Background sessions only | `working`, `blocked`, `done`, `failed`, `stopped` |
| `id` | Background sessions only | Short id for `claude attach` / `logs` / `stop` |
| `sessionId`, `name` | When set | Session UUID; display name |

The docs define `done` as "The last turn finished what you asked for and the session is ready for your next
prompt" and add: "A session that finished its turn and is waiting for your next instruction reads `done`, not
`blocked`. `blocked` always means the session needs something from you" (same section).

### Observed locally

`claude agents --json` returned 31 entries. Each interactive session had `pid` and `status` and no `state`.
Background sessions had both. Combinations seen:

- interactive: `idle` ×21, `busy` ×1
- background: `busy`/`working`, `idle`/`working`, `idle`/`done`, `waiting`/`blocked` with
  `waitingFor: "input needed"`, and four exited sessions with only `state: blocked` and no `pid`

The interactive sessions in herdr worktrees (`~/.herdr/worktrees/ticketmaster/issue-*`) showed up with their
worktree as `cwd`. That is the match a board needs.

Timing: `claude agents --json` took 0.29 s wall (0.20 s user, 0.12 s system), and `--cwd <dir>` runs took
0.26 to 0.30 s.

### Mapping to the board's statuses

| Board status (`herdr.py` `URGENCY`) | From `claude agents --json` |
| :- | :- |
| `blocked` | `status == "waiting"` (or `state == "blocked"`) |
| `working` | `status == "busy"` (or background `state == "working"` with `status` idle between steps) |
| `idle` | `status == "idle"` on an interactive session |
| `done` | background `state == "done"` only; an interactive session cannot say this |
| `None` (no agent) | no entry whose `cwd` is the worktree |

How a board reads it: on each refresh, run `claude agents --json --cwd <repo-or-worktree-root>` once (not once per
card). Parse the array and index the entries by `cwd`, keeping only those with a `pid`. If `claude` is missing or
the call fails, show status as unknown instead of failing the board.

### Caveats

- **`cwd` is the start directory.** The docs say `--cwd` filters on sessions "started under that directory". A
  session that the plain backend starts in the worktree matches. A session started elsewhere that later entered the
  worktree may not match.
- **No `done` for interactive sessions.** To get `done` without herdr, the plain backend would have to start each
  card's session as a background session (`claude --bg ...`) and open `claude attach <id>` in the new terminal
  window. That changes the lifecycle: the session outlives its window and is stopped with `claude stop` / `claude
  rm`. That choice belongs to the map (see Open questions).
- **One process spawn per refresh** (about 0.3 s CPU). That is negligible at 5 s and noticeable below 1 s.
- **Since when.** Interactive entries in `claude agents --json` are mentioned at least since v2.1.196 ("Interactive
  sessions you never named carry a default name ... in session listings and `claude agents --json`"; [agent-view
  changelog](https://code.claude.com/docs/en/agent-view)). Older Claude Code builds may lack the command. The
  backend should treat a non-zero exit as "status unavailable".

## Alternatives considered

| Source | States distinguished | Latency | Install | How a board reads it | Verdict |
| :- | :- | :- | :- | :- | :- |
| `claude agents --json` | working, blocked, idle; done for background only | live; read on each poll | nothing | spawn and parse JSON | **Recommended** |
| Plugin hooks writing a per-worktree status file | all four, by inference | per event | plugin hooks enabled in every session | read files and check pid liveness | Works, but more moving parts and more ways to go stale |
| Transcripts `~/.claude/projects/*/*.jsonl` | working vs idle, heuristically; not permission prompts | per message | nothing | tail and parse JSONL, guess | Unreliable |
| Statusline command | none directly | event-driven, 300 ms debounce | the user's single `statusLine` slot | the script writes a file | Unsuitable |
| Process inspection (`ps`, `lsof`) | alive or not | live | nothing | scan processes, map cwd | Too coarse |
| `~/.claude/sessions/<pid>.json` | same data as the CLI | live | nothing | read files | Undocumented; do not depend on it |

### Hooks

Claude Code has `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `Notification`, `Stop`, `StopFailure`,
`SessionStart` and `SessionEnd`, among others. Every input carries `session_id`, `cwd` and `transcript_path`.
`Notification` matchers include `permission_prompt`, `idle_prompt`, `elicitation_dialog` and `agent_needs_input`.
Plugins can ship hooks in `hooks/hooks.json`, and command hooks accept `"async": true`
([Hooks reference](https://code.claude.com/docs/en/hooks)).

A ticketmaster plugin could therefore write `<status-dir>/<session_id>.json` with `cwd` and a status:

- `UserPromptSubmit` or `PreToolUse` sets `working`.
- `Notification` with `permission_prompt` or `elicitation_dialog`, or `PermissionRequest`, sets `blocked`.
- `Stop` sets `done`.
- `Notification` with `idle_prompt` sets `idle`.
- `SessionEnd` deletes the file.

This is the only route that yields `done` for interactive sessions. Against it:

- The hooks must be installed and enabled in every session the board should see, including ones it did not start.
- A process spawns on each tool call unless the hook is async.
- Answering a permission prompt fires no event until the tool's `PostToolUse`.
- A killed session never fires `SessionEnd`, so the file goes stale and the board needs a pid liveness check that
  the hook input does not provide.

Claude Code already does all of this internally and exposes the result through `claude agents --json`.

### Transcripts

Transcripts are JSONL under `~/.claude/projects/<project>/` (path in every hook and statusline input as
`transcript_path`). Inferring state means tailing and guessing from the last record. Pending permission prompts are
not reliably visible: there is an open community request to "Surface pending-approval state in session JSONL" so
that external tools can detect blocked sessions
([claudeissues.com #56944](https://claudeissues.com/issue/56944-feature-surface-pending-approval-state-in-session-jsonl-or-via-api-so-external-t)).

### Statusline

The statusline script runs at session start and then on new assistant messages, `/compact`, permission-mode
changes and similar events, debounced at 300 ms. The docs warn that "the event-driven triggers can go quiet when
the main session is idle" ([Status line](https://code.claude.com/docs/en/statusline#when-the-status-line-updates)).
Its JSON input has `session_id`, `cwd`, `transcript_path` and `worktree.*` but no busy, waiting or idle field. The
setting is a single slot, and this user already has `~/.claude/statusline.sh` there. Ticketmaster would have to
take over or wrap the user's own statusline.

### Process-level signals

`ps` and `lsof` can find `claude` processes and their cwd. That says whether an agent is alive, but nothing about
its state.

### `~/.claude/sessions/<pid>.json`

Claude Code writes one file per live process with `pid`, `cwd`, `kind`, `status`, `statusUpdatedAt` and
`waitingFor`. A community monitor already reads these files
([cmon](https://github.com/knowald/cmon)). They are not documented. The raw values differ from the CLI's: one
interactive session's file said `status: "shell"` while `claude agents --json` reported it as `busy`. The docs give
the equivalent warning for the background-job files: "The files under `~/.claude/jobs/<id>/` are not a stable
interface ... use `claude agents --json` rather than the files"
([Agent view](https://code.claude.com/docs/en/agent-view#read-session-state-from-a-script)). Use the CLI.

## Open questions for the map

1. **`done` for plain sessions.** Should the plain backend accept that interactive sessions show `idle` instead of
   `done`, or start card sessions as background sessions (`claude --bg`) attached in the new terminal window to get
   `done`?
   - Accepting `idle` keeps a plain `claude` per window.
   - Background sessions add `claude stop` / `claude rm` to the session lifecycle and to plain cleanup.
2. **Sessions the board did not start.** Matching by `cwd` also finds sessions the user opened by hand in a
   worktree. This is the same as herdr's "finds every session working a ticket". It is noted here so the spec says
   it.
