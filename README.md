# ticketmaster

A live, read-only swimlane board of [wayfinder](https://github.com/mattpocock/skills) maps and
standalone specs for [herdr](https://herdr.dev), packaged as a Claude Code plugin. Run `/ticketmaster` in a
Claude session inside herdr and the board opens in a pane beside it. Every open map is a lane, and its
decision tickets sit in Blocked · Frontier · Claimed · Decided; on GitHub, so is every standalone spec
written by `/to-spec`, with its `/to-tickets` tickets. `s` starts `/wayfinder` for a ticket in its
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
tracker (`gh`, `glab`, a `tea` login for Gitea/Forgejo, or a Linear API key or `ticketmaster login`, see
[Providers](#providers)). In Claude Code:

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
ticketmaster [open] [map] [--repo OWNER/NAME] [--provider github|gitlab|gitea|forgejo|linear]
             [--host HOST] [--columns auto|one|all] [--interval SECONDS]
ticketmaster login
ticketmaster cleanup MAP [--dry-run]
```

`open` is the default command: it opens the board in a herdr pane right of the
calling pane (the focused pane when `HERDR_PANE_ID` is stale or another session's), labelled
`ticketmaster`; run again, it restarts there; run in that pane, it runs the board right there.
Typed at a shell rather than run by Claude (no `CLAUDECODE` in the env), it runs the board right in
the current pane, with no split. It
only runs inside herdr (`HERDR_ENV=1`). `login` logs in to Linear with OAuth instead of an API key
(see [Providers](#providers)); it runs anywhere.

`cleanup MAP --dry-run` lists what a map, open or closed, leaves behind: the worktrees of the map and its
decision, spec and implementation tickets (herdr's `~/.herdr/worktrees/<repo>/` and Claude's
`.claude/worktrees/` alike) with their branches, herdr workspaces and agents, a ticket branch left without a
worktree, and an agent titled with one of the tickets that runs elsewhere. Each is **removable** when its
ticket is closed, or a PR that closes it is merged (GitHub), and it has no uncommitted changes; otherwise it is
**kept**, with the reasons: open ticket, unmerged PR, uncommitted changes. Merged comes from the tracker, never
from unpushed commits, which a squash merge with its remote branch deleted leaves behind. Whatever the command
runs in (its directory, or its herdr pane's agent) is kept. With `--dry-run` it changes nothing. It runs inside
herdr.

`cleanup MAP` without `--dry-run` removes what the dry run marks removable, on a closed map only: an open map
is refused with a non-zero exit and nothing changed, since a human closes the map in the tracker. For each
removable entry it removes the worktree (`git worktree remove`, which refuses one changed since the look, and
then leaves all of that entry alone), closes the herdr workspace open on it and any agent of it in another
workspace, and deletes the branch with `git branch -d`, which keeps a branch git calls unmerged, such as one
whose PR was squash-merged. Nothing is forced. Kept entries are left alone, and the summary lists what was
removed and what was kept, with the reasons. It exits non-zero when git or herdr refused a step.

The board shows every open `wayfinder:map` in the repo as a lane, with the map's decision tickets,
its spec and the spec's implementation tickets, and on GitHub every standalone spec written by `/to-spec`
as a lane of its own, with its `/to-tickets` tickets (see [Lanes and columns](#lanes-and-columns)).
Other issues are not shown. `map` (`42`, `#42`, the issue's URL, or a tracker
key like `TM-1` or a Linear issue URL) opens the full board with the [map filter](#filters) preset to
that map, for this run only, which hides the spec lanes; `Esc` clears it and shows every lane. A key matches the map card
whose tracker key it is, never issue number 1; a key no map carries shows an empty board. A closed map
whose spec is still open stays on the board in full. Any other closed map shows only when you name it by
number or URL, or on Linear by its team's key, as its header card alone.

The board refreshes every 5 s, every 30 s on Linear; `--interval` sets another for one run, and the
[settings menu](#settings) (`c`) saves one per repo. On GitHub, ticket fetches slow down further to stay
within a [GraphQL budget](#providers).

The board pane takes about a third of the width, so it shows one column at a time: `←`/`→`
switch columns, `1`–`4` jump to one, `v` shows all side by side (`--columns` sets the start).

## Keys

| Key            | Column            | Effect                                                              |
| -------------- | ----------------- | ------------------------------------------------------------------- |
| `Enter` / double-click | any       | go to the card's most urgent session, else its worktree with a shell; with neither, the status line says what `s` would do there or why it refuses. It never starts or resumes Claude |
| `s`            | all but Decided   | start `/wayfinder` for the ticket, `/implement` on an `impl` card, or `/to-tickets` on a map card whose spec has no tickets yet, in its own `issue-<n>` worktree, or continue Claude in it when it has no agent (asks first); refused when closed, blocked, assigned to someone else or live (press `Enter`) |
| `o`            | any               | open the issue in the browser                                       |
| `/` `Esc` `r` `q` |                | free-text filter, clear every filter, refresh now, quit             |
| `f`            | any               | filter picker: type, HITL/AFK, assignee, map, author ([Filters](#filters); saved per repo) |
| `c`            | any               | settings menu: refresh interval, fetch limit, GitHub budget ([Settings](#settings); saved per repo) |
| `?`            | any               | help: keys, columns, scope and how sessions start                   |

Arrows or `hjkl` move between cards: up and down walk each lane's map card and then its cards in the
current column, on into the next lane; left and right change column within the lane.

## Sessions in herdr

`s` runs `herdr worktree create --branch issue-<n>` (`tm-<n>` on Linear), labelled `#<n> <title>`
(`TM-<n> <title>`), then starts Claude in the new workspace as herdr agent `issue-<n>`:

```
claude -n "#<n> <title>" "<prompt>"
```

| Card                                                          | Prompt                                         |
| ------------------------------------------------------------- | ---------------------------------------------- |
| a map (`wayfinder:map`) whose spec has no tickets yet         | `/mattpocock-skills:to-tickets <spec-url>` in the spec's worktree; see below |
| any other map                                                 | its first frontier ticket's; with an empty frontier `/mattpocock-skills:wayfinder <map-url>`; see below |
| has a `wayfinder:<type>` label (`research`, `prototype`, `grilling`, `task`; GitLab's `wayfinder::<type>` too) | `/mattpocock-skills:wayfinder <map-url> <ticket-url>` |
| an implementation ticket (`impl`)                             | `/mattpocock-skills:implement <ticket-url>`, see below |
| closed, blocked, assigned to someone else, a live session     | never started; the status line says why        |

The skills are `wayfinder_command`, `implement_command` and `to_tickets_command` in `.ticketmaster.toml`. Each ticket gets
one worktree, never a `-2` copy: when it exists without an agent, `s` opens it and runs
`claude -n "#<n> <title>" --continue` there. Maps and HITL tickets (grilling, prototype, task) take
focus; research and `impl` cards start unfocused, and the status line says so.

`s` on an `impl` card starts `/implement` AFK. On Linear its branch is the issue's branch name (`branchName`),
in a `tm-<n>` worktree. `s` also refuses an `impl` card with an open PR that closes it. A card you
claimed starts, even while its spec is open. The session owns review, push and PR; the board starts
nothing else.

`s` on a lane's map card whose open spec has no implementation ticket yet, open or closed, breaks the
spec into tickets: it starts `/mattpocock-skills:to-tickets <spec-url>`, focused, in the spec's own
`issue-<spec>` worktree (`tm-<spec>` on Linear, on the issue's branch name), also when the map is
closed. It refuses a blocked spec, one assigned to someone else, and one with a live session (`Enter`
on the map card goes to the map's session, else the spec's); a spec closed without tickets is passed
over. The board never removes the spec's
worktree. Otherwise `s` on a lane's map card starts the map's first **frontier** ticket: open, unblocked, unassigned, not
being started from this board and with no live session, in map order (GitHub's sub-issue order,
asked for when you press `s`; else issue number). That ticket gets its own `issue-<n>` worktree,
card and `/mattpocock-skills:wayfinder <map-url> <ticket-url>` session, so a second press starts the
next frontier ticket. With an empty frontier it starts `/mattpocock-skills:wayfinder <map-url>` in
the map's own worktree, so wayfinder can graduate fog or finish the map.

The board finds the sessions working a ticket, including ones it did not start. A herdr agent works
ticket `<n>` when its worktree path or branch is `issue-<n>` or `tm-<n>` (optionally `worktree-`
first, as `claude -w` names it, and `-<k>` after), or when its terminal title starts with the key
(`TM-<n> …` or `#<n> …`, as `claude -n` sets it). Only agents whose working directory lies in one of
the repo's checkouts count, and each is matched to the checkout its foreground process runs in, so a
`claude -w tm-<n>` session in another workspace's pane shows its real state. Transcripts are not read.

Each card lists one badge per session: `⚠ needs you`, `● working`, `◌ idle` (or `✓ done`), and
`○ no agent` for a ticket worktree with no Claude running. A claimed ticket with no session found
says `claimed, no session here`. `Enter` on any card, closed, blocked or claimed by someone else
included, focuses its most urgent session (needs you, then working, then idle). For a worktree with
no Claude running it focuses, or opens, that worktree's workspace with a shell; Claude is not
resumed. A map card goes only to the map's own session or worktree, never to one of its tickets'.

The board only reads; `cleanup MAP` without `--dry-run` is the one command that changes anything. Wayfinder claims its tickets by assigning them, so the card moves to Claimed
on the next refresh.

## Filters

`/` filters by free text: every word must appear in a card's number, title, body, author, labels or
assignees. `f` opens the filter picker; `Enter` (or space, or a click) selects or deselects a value,
and the board narrows as you go. Every value comes from the tracker data on the board:

| Section   | Values                                                    | A card shows when            |
| --------- | --------------------------------------------------------- | ---------------------------- |
| Type      | `research`, `prototype`, `grilling`, `task`, `spec`, `impl` (those on the board) | its type is selected; `spec` keeps a lane whose map card carries a spec, and a spec lane |
| HITL/AFK  | `HITL`, `AFK`                                             | its wayfinder mode is selected |
| Assignee  | `me` (your tracker login), `unassigned`, `anyone` (the default); one at a time | it is assigned to you, or to nobody |
| Map       | each map lane's map                                       | its lane's map is selected; spec lanes hide while a map is selected |
| Author    | each card's author                                        | one of them opened it        |

Values in a section are alternatives; a card must pass every section with a selection, and the `/`
filter. The map filter keeps or hides whole lanes; while cards are filtered, a lane with no matching
card is hidden. A lane's map card keeps the map's own counts. The status line shows the selection
(`type: grilling, task · assignee: me · map: #9`); `c` in the picker clears its selection, and `Esc`
on the board clears every filter, the text and the map preset included.

The selection is saved per user and repo in `$XDG_CONFIG_HOME/ticketmaster/settings.json`
(`~/.config/…`) with every change and restored when the board reopens. A map named on the command
line is never saved: while it stands, the map section keeps the selection saved before. A saved value
that is no longer on the board is ignored until it comes back. There are no label or state filters.

## Settings

`c` opens the settings menu. `Enter` checks every field, applies the values at once and saves them
per user and repo, under `repos` in the same `settings.json`:

```json
{"repos": {"github:owner/name": {"interval": 10, "fetch_limit": 100, "budget": 1000}}}
```

`Esc` closes the menu without changing anything. A field that is not valid stays open with a message
saying why, and a failed save shows a warning while the new value still applies to this run.

| Setting          | Values                       | Effect                                                    |
| ---------------- | ---------------------------- | --------------------------------------------------------- |
| Refresh interval | any positive number of seconds, no floor | ticket and session fetches reschedule at once; the status line's `every Ns` shows it |
| Fetch limit      | a whole number, 1 or more; default 300 | the open maps the board reads, and the tickets it reads per map, from the next ticket fetch |
| GitHub budget    | any positive number of GraphQL points an hour, default 1,000; GitHub only | ticket fetches re-pace at once to the [budget](#providers) |

The refresh interval is `--interval` for this run, then the repo's saved interval, then the
default: 5 s, 30 s on Linear. `--interval` never overwrites the saved value; an interval changed in
the menu is saved, also under `--interval`. A [rate limit](#providers) still slows the board on top
of it.

The fetch limit caps what the board reads from the tracker: at most that many open maps, and that
many tickets per map, on every provider. A lower limit means fewer tracker requests on a large repo,
so the board stays further from rate limits. The default fetch limit is not saved until it is
changed. A fetch that reaches the limit does not hide tickets silently: the status line says what it
cut off, e.g. `cut off at 300: open maps, #12's tickets`, until a fetch no longer reaches it. A
closed map that an open spec names, or that the board was started on, comes on top of the open maps.
On GitLab and Gitea/Forgejo, which have no sub-issues, a map's tickets are still found among every
issue with a `wayfinder:` type label, so there the per-map limit trims the board, not the requests. A Linear
map keeps its first sub-issues as Linear pages them.

## Wayfinding links

The board reads wayfinder's links between tickets; it never writes them, and never reads or writes
`docs/agents/issue-tracker.md`. It understands these encodings:

| Tracker         | Child → map                               | Blocking                                   | Out of scope                    |
| --------------- | ----------------------------------------- | ------------------------------------------ | ------------------------------- |
| GitHub          | native sub-issue of the map, else `Part of #<map>` as the body's first line | native issue dependency (`blocked_by`) | closed as not planned, or linked from the map's `## Out of scope` section |
| GitLab          | `Part of #<map>` as the description's first line | native `blocks` issue link (GitLab Premium) | linked from the map's `## Out of scope` section, closed |
| Gitea / Forgejo | `Part of #<map>` as the body's first line | native issue dependency (`/dependencies`)  | linked from the map's `## Out of scope` section, closed |
| Linear          | native sub-issue of the map, else `Part of TM-<map>` as the description's first line | native `blocks` relation | canceled or duplicate |

The `## Out of scope` section links a ticket as `#<n>` or by its issue URL in the map's repo.

On Linear a typed `TM-1` is stored as a mention, a link to the issue, so `Part of`, `## Parent` and
`Blocked by` accept the key, the mention or the issue URL.

Once the map is resolved, its destination becomes a spec (`/to-spec`) and then implementation tickets
(`/to-tickets`). The spec's first line is `Part of #<map>` (`Part of TM-<map>` on Linear); its
implementation tickets are its sub-issues (GitHub and Linear) or name it in a `## Parent` section (every
store). They carry no `wayfinder:` label,
so they are no decision tickets. The spec is a line of the map card, and its implementation tickets sit
in the map's lane tagged `impl`. The board reads these links, and never writes them.

Settings in `.ticketmaster.toml`:

```toml
wayfinder_command = "/wayfinder"          # if wayfinder is installed under another name
implement_command = "/implement"          # default /mattpocock-skills:implement
to_tickets_command = "/to-tickets"        # default /mattpocock-skills:to-tickets
```

## Lanes and columns

Map lanes come first, then spec lanes, each ordered by issue number.

Each open `wayfinder:map` is a map lane. Its header is the map card (`◆`, cyan): the map's
title and its counts, **decided** (closed tickets, less those out of scope), **open** and
**blocked**. A closed map keeps its full lane while any of its spec or implementation tickets is
open, since the decisions are made but the work goes on. Otherwise it shows only when named
(`ticketmaster <map>`), shrunk to its header card.
The lane holds the map's decision tickets (`wayfinder:research`, `prototype`, `grilling` or `task`),
then the spec's implementation tickets, each in the first column whose rule matches. The spec itself
is no card: the map card shows it on a line of its own, `▣ #<spec> open` (or `closed`) with its
tickets' counts, `N done · N open · N blocked` (done is closed, less canceled), or `no tickets yet`,
and the badges of the spec's sessions. The map card's own counts are of its decision tickets only.

On GitHub, each standalone spec is a spec lane. A standalone spec carries the spec **signature**, the
headings `/to-spec` writes, `## Problem Statement` and `## Solution`, has no `Part of #<map>` line naming a
map and no `wayfinder:` label. Both headings must be there, at `##` level, in any case; an issue that
only mentions them in prose does not count, and one that also carries the ticket signature is a spec.
Its header is the spec card (`▣`, magenta): `▣ #<n> <title>`, `spec closed` once it is closed, its
assignees, and `spec · N done · N open · N blocked` (done is closed, less canceled). Its tickets are the
issues with the ticket signature `/to-tickets` writes, `## What to build` and `## Acceptance criteria`,
whose sub-issue parent, else first `## Parent` reference (`#<n>` or the issue's URL), is the spec. They
fill the same four columns, every closed one in Decided however old. The lane stays while the spec or any
of its tickets is open, so a closed spec with an open ticket keeps its lane; it leaves once all are
closed. A ticket whose parent is a map spec stays in the map's lane.

| Column   | Rule                                                                                   |
| -------- | -------------------------------------------------------------------------------------- |
| Decided  | closed, of any age. A ticket closed as not planned (canceled), or linked from the map's `## Out of scope` section, is marked `out of scope` |
| Blocked  | open, with an open blocking issue (native, or listed in the body)                      |
| Claimed  | open, with an assignee                                                                 |
| Frontier | everything else                                                                        |

Labels play no part: there is no `blocked`, `review:` or `wontfix` rule, and no Review or Done
column. Within a column, cards sort by the map's sub-issue order, then issue number, so a map's first
Frontier card is the one `/wayfinder <map>` would pick.

A ticket card shows its number (its key, `TM-<n>`, on Linear) and title, the first ~90 characters of the body (the `## Question`
section when it has one, else the `## What to build` section), and then:

- its type and who it needs: `HITL` (you at the keyboard) for `grilling` and `prototype`, `AFK` (the
  agent alone) for `research`, and for a `task` whichever of the words `HITL` / `AFK` its body names
  first (no badge when it names neither); `impl` for a spec's implementation ticket, a map spec's or a
  standalone spec's
- `PR open` on an open card when an open pull or merge request will close its issue
- `out of scope`, in Decided
- its open blockers (`⛔ #3 other/lib#12`, `⛔ TM-3` on Linear)
- its assignees (`@kare`; Linear's display name, `@kaare.blakstad`)
- its lane's title (`◆ Pick a stack` for a map, `▣ Board shows standalone work` for a spec)

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

Linear has no remote to detect, so name it and its team:

```toml
provider = "linear"
team = "TM"                 # the team's issue key, as in TM-12
key_env = "LINEAR_API_KEY"  # optional; the env var holding the API key
oauth_client_id = "…"       # optional; your OAuth app's client id, for `ticketmaster login`
```

The board reads a Linear personal API key (Linear: Settings, Security & access; the Read permission
is enough) from `LINEAR_API_KEY`, or from the variable `key_env` names. That name must start with
`LINEAR_`, so no other secret is sent to Linear. `--provider linear` without a `team` fails and
prints the lines to add.

Without a personal key, read with your own OAuth token: register an OAuth application in your
workspace (Linear: Settings, API, OAuth applications) with the callback URL
`http://localhost:8719/callback`, put its client id in `oauth_client_id`, and run
`ticketmaster login` (or `/ticketmaster login`) once. It opens Linear in the browser, takes the
code on that localhost callback (OAuth 2.0 with PKCE, scope `read`; ticketmaster ships no client
id and needs no client secret), and writes the token to `~/.config/ticketmaster/linear-<client id>.json`
with mode 0600. The board refreshes an expired token itself and writes the rotated one back
there, a local write; it never opens a browser, so when the refresh is refused it says to run
`ticketmaster login` again. `login` without `oauth_client_id` fails and prints the line to add.

When the `key_env` variable is set, the key wins over a stored token. With neither, the board
refuses and names both options. The key goes bare in `Authorization`, the token as `Bearer <token>`.

| Provider        | Access                                   | Blocking                  | Maps and their tickets              |
| --------------- | ---------------------------------------- | ------------------------- | ----------------------------------- |
| GitHub          | `gh api graphql` (your `gh` login)       | native `blockedBy`        | a search for open `wayfinder:map` issues, then per map its sub-issues in order and the typed tickets and spec whose `Part of #<map>` line names it, then the spec's sub-issues and the issues whose `## Parent` names it |
| GitLab          | `glab api graphql` (your `glab` login)   | `blocks` links            | the project's `wayfinder:` labels (`wayfinder:map` or `wayfinder::map`, and the four types), one query per label; a typed ticket joins the map its `Part of #<map>` line names; a description search for `Part of` and `Parent` finds the specs and their implementation tickets |
| Gitea / Forgejo | REST through `tea api`, with the `tea` login (token or OAuth) whose URL matches the repo host | `/dependencies` API | the repo's and its org's `wayfinder:` labels, one list per label; a typed ticket joins the map its `Part of #<map>` line names; an issue search (`q`) for `Part of` and `Parent` finds the specs and their implementation tickets |
| Linear          | GraphQL with the API key (`Authorization: <key>`) or the `login` token (`Bearer`) | `blocks` relations (`inverseRelations`) | the team's open `wayfinder:map` issues, then per map its sub-issues in sub-issue order; one description search for `Part of` and `Parent` finds the typed tickets and spec linked by `Part of`, and the spec's sub-issues and `## Parent` tickets |

On Linear only the workflow state's type counts, never its team-renamable name: `completed` is
closed, `canceled` and `duplicate` closed as not planned. Only the assignee claims a ticket; the
delegate does not. Users are their display name, and the board's "me" is the key's or the token's user.

Every provider only reads: the board has no way to close, label, assign or comment, and neither has `cleanup`.

Besides native links, every provider counts the open issues a body lists on a `Blocked by: #3, #4`
line (or the older Norwegian `Blokkert av: #3`) or under a `## Blocked by` heading as `- #3` bullets
(`TM-3` on Linear).
A ticket leaves Blocked once every listed blocker is closed. `s` refuses to start a blocked ticket,
naming its blockers, and a ticket assigned to someone other than the tracker login the board runs
as; your own claimed tickets still start.

Where a tracker has no dependency support, only body-listed blockers mark a ticket Blocked.

GitHub's GraphQL quota is 5,000 points an hour, shared by every board and every agent's `gh` call on the
token, so a GitHub board paces itself to a budget: 1,000 points an hour by default, set per repo in the
[settings menu](#settings). Ticket fetches come every `--interval` while that is cheap enough, or every
`last fetch's cost × 3600 / budget` seconds when that is longer, and longer still when the points left
would run out before the quota resets at that pace. The status line shows the pace and the points left,
e.g. `every 22s · 3,412 pts left`. Herdr session refreshes keep the refresh interval, and `r` fetches at
once, with the next scheduled fetch one paced interval after it. The rate-limit back-off below overrides
the pace while it lasts.

A Linear API key allows 2,500 requests an hour, shared by everything using its user's key, which is why
the board polls Linear every 30 s. Every refresh lists the maps, then makes one query per open map and
one search for specs and implementation tickets; each stays under half of Linear's 10,000-point
complexity cap per query.

When the tracker rate-limits the board (GitHub's primary or secondary limit, an HTTP 429 from
GitLab or Gitea/Forgejo, or Linear's HTTP 400 `RATELIMITED`), ticket fetches drop to once a minute and herdr session states to every
10s; the status line shows `rate-limited · next fetch in 42s · secondary limit`, and `r` refreshes
only the sessions. The first good fetch restores the refresh interval, once: a second rate limit keeps the
board at once a minute until you restart it.

Any tracker error while fetching (a rate limit, a 4xx such as `401 unauthorized`, a 5xx such as
`502 bad gateway`) shows a notification: the tracker, the reason, and the tracker's own message. A
rate limit or a 5xx is a warning, since the board retries by itself; any other error needs you (e.g.
`gh auth login`). The board notifies once per error: again only when it changes, or when it comes back
after a good fetch; `r` repeats it. On GitHub the board asks the `rate_limit` endpoint (it costs no
quota) which limit it hit: an hourly quota at 0 is named with the time it resets, otherwise it is the
secondary limit (too many calls per minute, shared by every board and agent on the account). Gitea and
Forgejo say when a 429 lifts when the response's `Retry-After`, `X-RateLimit-Reset` or `RateLimit-Reset` does
Linear names the hourly request or complexity limit that ran out and when it resets
(`X-RateLimit-*-Reset`). (GitLab's `glab` does not pass the headers on, so a GitLab 429 gives the status and message). `gh` and
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
