# How can the board find a herdr session it did not start?

Research for [#58](https://github.com/kareblak/ticketmaster/issues/58) (map [#56](https://github.com/kareblak/ticketmaster/issues/56)).
Checked 2026-10-14 against herdr 0.9.3 (installed CLI, live `herdr api schema --json`) and the herdr
v0.9.5 docs (`https://herdr.dev/llms-full.txt`), Claude Code 2.1.292, and this repo at `41787f4`.

## Answer

herdr never knows about tickets. What it does expose per agent is enough to find any Claude
session that is *running in a herdr pane*: its pane, workspace, `cwd`/`foreground_cwd`, state,
optional agent name, terminal title, and (with the Claude integration installed) the Claude
session id. The ticket number has to come from something the session's launcher chose:

1. **A worktree path or branch named after the ticket** (`issue-<n>`, `worktree-issue-<n>`, `review-<n>`).
   This is what the board already uses. It covers board-started sessions and `claude -w issue-<n>`.
2. **The Claude session name / terminal title** (`claude -n "#<n> …"`), visible in herdr's
   `terminal_title` and in the transcript's `custom-title` entry.
3. **The Claude transcript** (via herdr's `agent_session.value` → `~/.claude/projects/<cwd>/<id>.jsonl`),
   whose first user message holds the ticket URL for any session started with
   `/wayfinder <map-url> <ticket-url>` or `/implement <ticket-url>`. Undocumented format.
4. **Nothing at all**, for wayfinder research tickets run as subagents inside the map's session
   (worktree `agent-<hash>`, no pane of their own), and for a plain `claude` in the main checkout
   whose prompt does not name the ticket. Only the tracker assignment (the claim) shows the ticket is
   being worked on.

The cheapest gain over today's code: match agents to worktrees by **`foreground_cwd` lying inside
the worktree path**, not only by `open_workspace_id`. Today a `claude -w issue-<n>` session running
in some other workspace's pane shows as "no agent" (see "Gap in the current board" below).

## What herdr exposes

All via the CLI (`herdr <noun> list|get`, JSON on stdout, `.result.*`) or the same socket API.

| Record | Fields that matter for discovery | Source |
| --- | --- | --- |
| `workspace list` → `WorkspaceInfo` | `workspace_id`, `label`, `agent_status` (rollup), `tokens`, optional `worktree` {`checkout_path`, `is_linked_worktree`, `repo_root`, `repo_name`, `repo_key`}. **No branch.** | live schema `WorkspaceInfo`, `WorkspaceWorktreeInfo` |
| `worktree list --cwd <repo>` → `WorktreeInfo` | every git worktree of the repo: `path`, `branch`, `is_linked_worktree`, `is_detached`, `label`, `open_workspace_id` (only when a herdr workspace has that checkout open) | live schema `WorktreeInfo`; docs "Worktrees"; observed |
| `pane list` / `pane get` | `pane_id`, `workspace_id`, `tab_id`, `label`, `cwd`, `foreground_cwd`, `agent`, `agent_status`, `agent_session`, `terminal_title`, `title`, `tokens` | live schema; docs "Panes" |
| `agent list` / `agent get` | as pane, plus `name` (the `agent start`/`agent rename` alias), `display_agent`, `interactive_ready`, `launch_pending` | live schema; docs "Agents" |

Notes, each from the herdr docs unless marked observed:

- **Agent state**: `idle`, `working`, `blocked`, `done` (idle, not yet seen), `unknown`. For Claude
  Code it comes from screen-manifest detection, not from Claude itself ("Integrations → Claude Code").
- **Agent name** is only set by `herdr agent start <name>` or `agent rename`; "manually launched agents
  remain unnamed and use their pane ID". The name "follows the current pane occupant and is cleared when
  that agent exits". So an unnamed agent is the norm for sessions the board did not start (observed:
  every agent on this machine has `name: null`).
- **`agent_session`** = `{agent, kind, source, value}`; for Claude, `source: "herdr:claude"`, `value`
  = Claude session id. Present only when the official integration is installed
  (`herdr integration install claude`, a `SessionStart` hook) and the session started after that
  (observed: one of five Claude panes had it). "If no native session reference is stored, the field is omitted."
- **`cwd` vs `foreground_cwd`**: `cwd` is the pane/workspace cwd; `foreground_cwd` is the cwd of the
  foreground process "when Herdr can resolve" it. A Claude started with `claude -w` runs with its
  worktree as cwd, so `foreground_cwd` should name the worktree even when the pane's `cwd` is the main
  checkout (inferred, not observed live).
- **Branch** is exposed only by `worktree list` (per checkout), not on panes, agents or workspaces.
  The sidebar can render a branch token, but it is not in the API records.
- **`terminal_title`**: what the program set via OSC. `claude -n <name>` sets the session name
  "shown in the prompt box, /resume picker, and terminal title" (`claude --help`); without `-n`, Claude
  writes its own topic there (observed: `5 awaiting input · claude agents`, `◑ ticketmaster read-only view`).
  Claude may prefix a spinner glyph, so parse with a search, not an anchored match.
- **`tokens` / pane metadata**: anyone can tag a pane with `herdr pane report-metadata <pane> --source …
  --token ticket=58` (visual-only, optional TTL); tokens show up on pane/agent records and can be used in
  socket-API agent filters (`{"token": "name"}`). A hook outside the board could tag sessions this way.
- **Events**: `events.subscribe` streams lifecycle and agent-status events; `session.snapshot` gives a
  one-shot snapshot of workspaces, panes and agents. Polling `agent list` + `worktree list`, as the
  board does, is enough; events would only cut latency.
- herdr's own worktrees go to `<worktrees.directory>/<repo>/<branch-slug>` (here
  `~/.herdr-worktrees/ticketmaster/issue-<n>`), grouped under the repo's workspace.

## How sessions are started, and what ties each to a ticket

| How it was started | Where it runs | Ticket tie visible to the board | Reliability |
| --- | --- | --- | --- |
| **Board `s`/`c`** (`herdr worktree create --branch issue-<n>` + `herdr agent start issue-<n> --kind claude -- -n "#<n> <title>" "<prompt>"`, [start.py](../../ticketmaster/start.py), [herdr.py](../../ticketmaster/herdr.py)) | own workspace, `~/.herdr-worktrees/<repo>/issue-<n>` | worktree path + branch `issue-<n>[-k]` / `review-<n>`; `open_workspace_id`; agent `name` `issue-<n>`; `terminal_title` `#<n> …`; prompt has ticket URL | strong (already handled) |
| **`claude -w issue-<n>`** in any herdr pane | `.claude/worktrees/issue-<n>`, branch `worktree-issue-<n>` (Claude Code worktree docs) | worktree path/branch (matched by `WORKTREE_NAME`); agent found only via `foreground_cwd`, since there is no `open_workspace_id` unless `ticketmaster adopt` opened one | strong for the number; agent state missed today |
| **`claude -w <other-name>`** or `claude -w` (auto name like `bright-running-fox`) | `.claude/worktrees/<name>`, branch `worktree-<name>` | none in the name; `-n "#<n>"` title or the transcript's first prompt if it names the ticket | weak |
| **herdr UI "New worktree" / `herdr worktree create` by hand**, then `claude` | own workspace, `~/.herdr-worktrees/<repo>/<branch-slug>` | the branch name, if the user chose `issue-<n>`; otherwise title/transcript | depends on naming |
| **Plain `claude` in a pane of the main checkout**, then `/wayfinder <map-url> <ticket-url>` | main checkout, no worktree | no path/branch tie; `terminal_title` only if `-n`; transcript first prompt (needs `agent_session`) | weak; several sessions can share the checkout |
| **Session that moved into a worktree mid-run** (`EnterWorktree` tool, e.g. per a CLAUDE.md worktree rule) | `.claude/worktrees/<name>`, branch `worktree-<name>`, name chosen by the agent | only if the agent named it `issue-<n>`; pane `cwd` stays the launch dir, `foreground_cwd` follows only if Claude chdirs (unverified) | weak |
| **Wayfinder research ticket run as a subagent** of the map session (wayfinder SKILL.md step 5; this ticket is one) | the map session's pane; worktree `.claude/worktrees/agent-<hash>`, branch `worktree-agent-<hash>` (observed) | none: no pane, no agent record, no ticket in path/branch; only the tracker assignee | none |

Common to every wayfinder session: it **claims** its ticket by assigning it before any work
(wayfinder SKILL.md, "claims a ticket by assigning it"), so the tracker always shows the ticket as in
progress, but the assignment carries no pointer to the session.

## Gap in the current board

`Herdr.sessions()` ([herdr.py](../../ticketmaster/herdr.py)) keys agent state by `workspace_id` and looks it
up through the worktree's `open_workspace_id`. A `.claude/worktrees/issue-<n>` checkout with Claude
running in a pane of another workspace (the usual `claude -w` case) has no `open_workspace_id`, so the
card shows "no agent" even while it works. Matching `agent.foreground_cwd` (falling back to `cwd`)
against `worktree.path` (`Path.is_relative_to`) closes this without new herdr calls; both lists are
already fetched.

## Options for finding the rest (not decided here)

1. **cwd match** (above): cheap, robust, no new conventions.
2. **Title match**: parse `#<n>` from `terminal_title` for agents in the repo (by cwd). Works for any
   session started with `claude -n "#<n> …"`; costs nothing, but titles are free text.
3. **Transcript lookup**: for agents with `agent_session.value`, read the first user message of
   `~/.claude/projects/<encoded cwd>/<id>.jsonl` and match the repo's issue URLs. Catches plain-pane
   wayfinder sessions, but relies on an undocumented Claude Code file format and the herdr integration.
4. **Self-tagging**: have sessions announce their ticket, e.g. a Claude hook or the skill running
   `herdr pane report-metadata "$HERDR_PANE_ID" --source ticketmaster --token ticket=<n>`. Precise, but
   needs every launcher to cooperate.
5. Subagent-run research tickets stay invisible to herdr whatever the board does; the board could at
   most show "claimed, session unknown" from the assignee.

## Sources

- `herdr --help`, `herdr {workspace,pane,agent,worktree,tab} --help`, `herdr agent start --help`,
  `herdr pane report-agent-session --help`, `herdr workspace report-metadata --help` (0.9.3).
- `herdr api schema --json` (0.9.3): `WorkspaceInfo`, `WorkspaceWorktreeInfo`, `WorktreeInfo`,
  `AgentSessionInfo`, pane and agent records.
- Live read-only `herdr workspace list`, `pane list`, `agent list`, `worktree list --cwd .` on this machine.
- herdr docs v0.9.5 via https://herdr.dev/llms-full.txt: "Add Herdr support to your agent",
  "Agent automation → Agent identity and launch", "CLI reference → Workspaces / Worktrees / Panes / Agents",
  "Concepts → Agent", "Integrations → Claude Code", "Custom status labels", "Socket API → Event subscriptions".
- `~/.claude/skills/herdr/SKILL.md` (agent names, states).
- Claude Code: `claude --help` (2.1.292: `-w/--worktree`, `-n/--name`, `--session-id`);
  https://code.claude.com/docs/en/worktrees.md ("created under `.claude/worktrees/<name>/` … on a new
  branch named `worktree-<name>`"; auto names like `bright-running-fox`).
- A board-started transcript (`~/.claude/projects/-Users-kareblak--herdr-worktrees-ticketmaster-issue-31/*.jsonl`):
  `custom-title`/`agent-name` entries hold `#31 …`; the first user message holds the issue URL.
- This repo: `ticketmaster/herdr.py` (`WORKTREE_NAME`, `ticket_number`, `sessions`, `adopt`),
  `ticketmaster/start.py`, README "Sessions in herdr".
- mattpocock-skills `wayfinder/SKILL.md` (claim by assignment; research tickets run as subagents).
