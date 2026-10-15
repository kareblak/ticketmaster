# ticketmaster

A live, read-only swimlane board of [wayfinder](https://github.com/mattpocock/skills) maps for
[herdr](https://herdr.dev), packaged as a Claude Code plugin. Run `/ticketmaster` in a Claude session
inside herdr and the board opens in a pane beside it. Every open map is a lane, and its decision
tickets sit in Blocked · Frontier · Claimed · Decided. `s` starts `/wayfinder` for a ticket in its
own git worktree, opened as a herdr workspace with Claude running in it as a herdr agent, and each
card shows what that agent is doing. It refreshes every 5 seconds. The board never writes to the
tracker or the repo, and never removes a worktree.

```
‹ 1 Blocked 1 ·  2 Frontier 2  · 3 Claimed 1 · 4 Decided 3 ›
◆ #9 Pick a stack
3 decided · 3 open · 1 blocked
#14 Pick a queue
  Should we use…
grilling ·  HITL  ◆ Pick a stack
```

## Install

Needs [herdr](https://herdr.dev), Python 3.11+ as `python3`, Claude Code, and the CLI for your
tracker (`gh`, `glab`, or a `tea` login for Gitea/Forgejo). In Claude Code:

```
/plugin marketplace add kareblak/ticketmaster
/plugin install ticketmaster@kareblak
```

This repo is the `kareblak` marketplace, and `ticketmaster` its plugin. The plugin brings the
`/ticketmaster` skill (`/ticketmaster:ticketmaster` if another skill has the same name) and the
`ticketmaster` command, which plugin `bin/` puts on PATH for Claude's Bash tool only. Nothing is
installed per project: the board reads the repo it is started in.

The command is a wrapper, `bin/ticketmaster`. On its first run it builds a venv from the plugin's own
source in the plugin's data dir (`~/.claude/plugins/data/ticketmaster-kareblak/venv`) with
`python3 -m venv` and pip, which takes a minute; later runs reuse it, and a new version rebuilds it.
The herdr board pane runs the wrapper by its absolute path, since plugin `bin/` is not on the pane's
PATH. If the build fails, the wrapper says why and exits 3; fix it (usually an older `python3`) and run
`/ticketmaster` again.

Update with `/plugin update ticketmaster`; there is no other update path. Uninstall with
`/plugin uninstall ticketmaster`.

For development, load your clone instead of the installed plugin:

```bash
claude --plugin-dir ~/code/ticketmaster
```

A clone (it has `.git`) is installed editable, in `~/.claude/plugins/data/ticketmaster-inline/venv`,
so code changes apply on the next `/ticketmaster` without rebuilding. Changing `__version__` (and
`version` in `.claude-plugin/plugin.json`, which must match) rebuilds it, e.g. after a dependency change.

## Usage

```
ticketmaster [open] [map] [--repo OWNER/NAME] [--provider github|gitlab|gitea|forgejo]
             [--host HOST] [--columns auto|one|all] [--interval SECONDS]
```

`open` is the only command, and the default: it opens the board in a herdr pane right of the
calling pane (the focused pane when `HERDR_PANE_ID` is stale or another session's), labelled
`ticketmaster`; run again, it restarts there; run in that pane, it runs the board right there. It
only runs inside herdr (`HERDR_ENV=1`).

The board shows every open `wayfinder:map` in the repo as a lane, with the map's decision tickets,
its spec and the spec's implementation tickets (see [Lanes and columns](#lanes-and-columns)). Issues that are not wayfinder work are not shown. A
closed map shows only when you name it: `map` (`42`, `#42` or an issue URL) adds that map, as its
header card alone.

The board pane takes about a third of the width, so it shows one column at a time: `←`/`→`
switch columns, `1`–`4` jump to one, `v` shows all side by side (`--columns` sets the start).

## Keys

| Key            | Column            | Effect                                                              |
| -------------- | ----------------- | ------------------------------------------------------------------- |
| `Enter` / double-click | any       | go to the ticket's herdr workspace; with none, the status line says what `s` would do. It never starts a session |
| `s`            | all but Decided   | start `/wayfinder` for the ticket in a **new** worktree (always new; asks first); refused while it is blocked or assigned to someone else, and on a closed map |
| `o`            | any               | open the issue in the browser                                       |
| `/` `Esc` `r` `q` |                | filter, clear both filters, refresh now, quit                       |
| `f`            | any               | filter picker: labels cycle include → exclude → off, authors on/off (see below; saved per repo) |
| `?`            | any               | help: keys, columns, scope and how sessions start                   |

Arrows or `hjkl` move between cards: up and down walk each lane's map card and then its cards in the
current column, on into the next lane; left and right change column within the lane.

## Sessions in herdr

`s` runs `herdr worktree create --branch issue-<n>` (or `issue-<n>-2`, `-3`… when that branch
already exists, so a start never reuses an old checkout), then starts Claude in the new workspace
as herdr agent `issue-<n>`:

```
claude -n "#<n> <title>" "<prompt>"
```

| Ticket                                                        | Prompt                                         |
| ------------------------------------------------------------- | ---------------------------------------------- |
| a map (`wayfinder:map`)                                       | never started itself; see below                |
| has a `wayfinder:<type>` label (`research`, `prototype`, `grilling`, `task`; GitLab's `wayfinder::<type>` too) | `/mattpocock-skills:wayfinder <map-url> <ticket-url>` |
| closed (Decided, or a closed map)                             | never started                                  |

`s` on a lane's map card starts the map's first **frontier** ticket: open, unblocked, unassigned, not already started
from this board or holding a session, in map order (GitHub's sub-issue order, asked for when you
press `s`; else issue number). That ticket gets its own `issue-<n>` worktree, card and
`/mattpocock-skills:wayfinder <map-url> <ticket-url>` session, so a second press starts the next
frontier ticket. With an empty frontier the board says so and starts nothing.

The board finds a ticket's sessions through its worktrees: any checkout named `issue-<n>` or
`issue-<n>-<k>`, including `claude -w issue-<n>` ones under `.claude/worktrees`. Each card lists
them with the state of the agent in their workspace: `● working`, `⚠ needs you`, `◌ idle`,
`✓ done`, or `○ no agent` when the workspace is closed or Claude has exited. `Enter` on such a card
focuses the workspace, reopening it and resuming Claude with `claude --continue` when needed.

`f` opens a picker listing every label and every author on the board's tickets; `Enter` (or space,
or a click) cycles a label through include, exclude and off, and selects or deselects an author. The
board narrows as you go. With any label included, a ticket shows only if it has at least one of
them; a ticket with any excluded label is hidden. With any author selected, a ticket shows only if
one of them opened it. A ticket must pass the labels, the authors and the `/` text filter. The
status line shows the selection (`labels: +bug -wontfix · authors: alice, bob`); `c` in the picker
or `Esc` on the board clears it. The selection is saved per user and repo in
`$XDG_CONFIG_HOME/ticketmaster/settings.json` (`~/.config/…`) and restored when the board reopens;
a saved label or author that is no longer on the board is ignored until it comes back.

The board only reads. Wayfinder claims its tickets by assigning them, so the card moves to Claimed
on the next refresh.

## Wayfinding links

The board reads wayfinder's links between tickets; it never writes them, and never reads or writes
`docs/agents/issue-tracker.md`. It understands these encodings:

| Tracker         | Child → map                               | Blocking                                   | Out of scope                    |
| --------------- | ----------------------------------------- | ------------------------------------------ | ------------------------------- |
| GitHub          | native sub-issue of the map, else `Part of #<map>` as the body's first line | native issue dependency (`blocked_by`) | closed as not planned, or linked from the map's `## Out of scope` section |
| GitLab          | `Part of #<map>` as the description's first line | native `blocks` issue link (GitLab Premium) | linked from the map's `## Out of scope` section, closed |
| Gitea / Forgejo | `Part of #<map>` as the body's first line | native issue dependency (`/dependencies`)  | linked from the map's `## Out of scope` section, closed |

The `## Out of scope` section links a ticket as `#<n>` or by its issue URL in the map's repo.

Once the map is resolved, its destination becomes a spec (`/to-spec`) and then implementation tickets
(`/to-tickets`). The spec's first line is `Part of #<map>`; its implementation tickets are its
sub-issues (GitHub) or name it in a `## Parent` section (every store). They carry no `wayfinder:` label,
so they are no decision tickets: they sit in the map's lane tagged `spec` and `impl`. The board reads
these links, and never writes them.

Settings in `.ticketmaster.toml`:

```toml
wayfinder_command = "/wayfinder"          # if wayfinder is installed under another name
```

## Lanes and columns

Each open `wayfinder:map` is a lane, ordered by issue number. Its header is the map card: the map's
title and its counts, **decided** (closed tickets, less those out of scope), **open** and
**blocked**. A closed map shows only when named (`ticketmaster <map>`), shrunk to its header card.
The lane holds the map's decision tickets (`wayfinder:research`, `prototype`, `grilling` or `task`),
then its spec and the spec's implementation tickets, each in the first column whose rule matches.
The map card's counts are of its decision tickets only.

| Column   | Rule                                                                                   |
| -------- | -------------------------------------------------------------------------------------- |
| Decided  | closed, of any age. A ticket closed as not planned (canceled), or linked from the map's `## Out of scope` section, is marked `out of scope` |
| Blocked  | open, with an open blocking issue (native, or listed in the body)                      |
| Claimed  | open, with an assignee                                                                 |
| Frontier | everything else                                                                        |

Labels play no part: there is no `blocked`, `review:` or `wontfix` rule, and no Review or Done
column. Within a column, cards sort by the map's sub-issue order, then issue number, so a map's first
Frontier card is the one `/wayfinder <map>` would pick.

A ticket card shows its number and title, the first ~90 characters of the body (the `## Question`
section when it has one), and then:

- its type and who it needs: `HITL` (you at the keyboard) for `grilling` and `prototype`, `AFK` (the
  agent alone) for `research`, and for a `task` whichever of the words `HITL` / `AFK` its body names
  first (no badge when it names neither); `spec` or `impl` for a spec and its implementation tickets
- `PR open` when an open pull or merge request closes it
- `out of scope`, in Decided
- its open blockers (`⛔ #3 other/lib#12`)
- its assignees (`@kare`)
- its map's title (`◆ Pick a stack`)

## Providers

The provider is detected from `git remote get-url origin`: `github.com` → GitHub, `gitlab.com`
or a host containing `gitlab` → GitLab, `codeberg.org`, `gitea.com` or a host containing
`gitea`/`forgejo` → Gitea. For other hosts, add `.ticketmaster.toml` at the repo root:

```toml
provider = "gitlab"         # github | gitlab | gitea | forgejo
host = "git.example.com"
repo = "group/project"      # optional; defaults to the remote's path
remote = "upstream"         # optional; defaults to origin
```

Flags override the file, and the file overrides the remote.

| Provider        | Access                                   | Blocking                  | Maps and their tickets              |
| --------------- | ---------------------------------------- | ------------------------- | ----------------------------------- |
| GitHub          | `gh api graphql` (your `gh` login)       | native `blockedBy`        | a search for open `wayfinder:map` issues, then per map its sub-issues in order and the typed tickets and spec whose `Part of #<map>` line names it, then the spec's sub-issues and the issues whose `## Parent` names it |
| GitLab          | `glab api graphql` (your `glab` login)   | `blocks` links            | the project's `wayfinder:` labels (`wayfinder:map` or `wayfinder::map`, and the four types), one query per label; a typed ticket joins the map its `Part of #<map>` line names; a description search for `Part of` and `Parent` finds the specs and their implementation tickets |
| Gitea / Forgejo | REST with the `tea` login token, or `GITEA_TOKEN` / `FORGEJO_TOKEN` | `/dependencies` API | the repo's and its org's `wayfinder:` labels, one list per label; a typed ticket joins the map its `Part of #<map>` line names; an issue search (`q`) for `Part of` and `Parent` finds the specs and their implementation tickets |

Every provider only reads: the board has no way to close, label, assign or comment.

Besides native links, every provider counts the open issues a body lists on a `Blocked by: #3, #4`
line (or the older Norwegian `Blokkert av: #3`) or under a `## Blocked by` heading as `- #3` bullets.
A ticket leaves Blocked once every listed blocker is closed. `s` refuses to start a blocked ticket,
naming its blockers, and a ticket assigned to someone other than the tracker login the board runs
as; your own claimed tickets still start.

Where a tracker has no dependency support, only body-listed blockers mark a ticket Blocked.

GitHub's GraphQL budget is 5,000 points an hour. Every refresh makes one search for the maps and one
query per open map (and per spec a map led to). Lengthen `--interval` if you run several boards at
once or have many open maps.

When the tracker rate-limits the board (GitHub's primary or secondary limit, or an HTTP 429 from
GitLab or Gitea/Forgejo), ticket fetches drop to once a minute and herdr session states to every
10s; the status line shows `rate-limited · next fetch in 42s · secondary limit`, and `r` refreshes
only the sessions. The first good fetch restores `--interval`, once: a second rate limit keeps the
board at once a minute until you restart it.

Any tracker error while fetching (a rate limit, a 4xx such as `401 unauthorized`, a 5xx such as
`502 bad gateway`) shows a notification: the tracker, the reason, and the tracker's own message. A
rate limit or a 5xx is a warning, since the board retries by itself; any other error needs you (e.g.
`gh auth login`). The board notifies once per error: again only when it changes, or when it comes back
after a good fetch; `r` repeats it. On GitHub the board asks the `rate_limit` endpoint (it costs no
quota) which limit it hit: an hourly quota at 0 is named with the time it resets, otherwise it is the
secondary limit (too many calls per minute, shared by every board and agent on the account). Gitea and
Forgejo say when a 429 lifts when the response's `Retry-After`, `X-RateLimit-Reset` or `RateLimit-Reset` does
(GitLab's `glab` does not pass the headers on, so a GitLab 429 gives the status and message). `gh` and
`glab` do not always print the HTTP status; then the notification gives the message alone.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
claude plugin validate .
```

`tests/test_board.py` drives the Textual app headlessly with a fake provider, and
`tests/test_wrapper.py` runs `bin/ticketmaster` against a fake `python3`. Try the plugin itself with
`claude --plugin-dir .` (see [Install](#install)).
