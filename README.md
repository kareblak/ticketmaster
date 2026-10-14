# ticketmaster

A live, read-only kanban board of issue-tracker work for [herdr](https://herdr.dev), packaged as a
Claude Code skill. Run `/ticketmaster` in a Claude session inside herdr and the board opens in a
pane beside it. `s` starts `/wayfinder` for a ticket in its own git worktree, opened as a herdr
workspace with Claude running in it as a herdr agent, and each card shows what that agent is
doing. It refreshes every 5 seconds. The board never writes to the tracker or the repo, and never
removes a worktree.

```
‹ 1 Blocked 0 ·  2 Ready 3  · 3 In progress 2 · 4 Review 1 · 5 Done 2 ›
#14 Pick a queue                     ● working issue-11
  Should we use…                     #11 Auth flow  @kare
```

## Install

Needs [herdr](https://herdr.dev), Python 3.11+, [pipx](https://pipx.pypa.io)
(`brew install pipx`), Claude Code, and the CLI for your tracker (`gh`, `glab`, or a `tea` login
for Gitea/Forgejo). On each machine:

```bash
pipx install git+https://github.com/kareblak/ticketmaster.git
mkdir -p ~/.claude/skills/ticketmaster
curl -fsSL https://raw.githubusercontent.com/kareblak/ticketmaster/main/ticketmaster/skill/SKILL.md \
  -o ~/.claude/skills/ticketmaster/SKILL.md
```

The first command puts `ticketmaster` on your PATH (`~/.local/bin`). The others put the skill in
`~/.claude/skills/ticketmaster/SKILL.md`, so `/ticketmaster` works in every Claude Code session
started afterwards, in any repo. Nothing is installed per project: the board reads the repo it is
started in. `ticketmaster --version` prints the version.

To upgrade, run the same commands again with `pipx install --force`. Uninstall with
`pipx uninstall ticketmaster && rm -r ~/.claude/skills/ticketmaster`.

For development, clone the repo and run `./install.sh`. It installs the clone as an editable
pipx app (code changes apply without reinstalling) and links the skill to the clone.

## Usage

```
ticketmaster [open] [parent] [--repo OWNER/NAME] [--provider github|gitlab|gitea|forgejo]
             [--host HOST] [--columns auto|one|all] [--interval SECONDS]
```

`open` is the only command, and the default: it opens the board in a herdr pane right of the
calling pane (the focused pane when `HERDR_PANE_ID` is stale or another session's), labelled
`ticketmaster`; run again, it restarts there; run in that pane, it runs the board right there. It
only runs inside herdr (`HERDR_ENV=1`).

Scope:

- no parent: every open issue, and every issue closed within the last 7 days
- `parent` (`42`, `#42` or an issue URL): the parent's sub-issues, e.g. a wayfinder map; for a map,
  also the spec linked to it and that spec's implementation tickets (see [Wayfinding links](#wayfinding-links))

The board pane takes about a third of the width, so it shows one column at a time: `←`/`→`
switch columns, `1`–`5` jump to one, `v` shows all side by side (`--columns` sets the start).

## Keys

| Key            | Column            | Effect                                                              |
| -------------- | ----------------- | ------------------------------------------------------------------- |
| `Enter` / double-click | any       | go to the ticket's herdr workspace; with none, the status line says what `s` would do. It never starts a session |
| `s`            | all but Done      | start `/wayfinder` for the ticket in a **new** worktree (always new; asks first); refused while it is blocked or assigned to someone else, and for an issue that is not wayfinder work |
| `o`            | any               | open the issue in the browser                                       |
| `/` `Esc` `r` `q` |                | filter, clear both filters, refresh now, quit                       |
| `f`            | any               | filter picker: labels cycle include → exclude → off, authors on/off (see below; saved per repo) |
| `?`            | any               | help: keys, columns, scope and how sessions start                   |

Arrows or `hjkl` move between cards.

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
| the spec a map led to, or one of its implementation tickets (see [Wayfinding links](#wayfinding-links)) | `/mattpocock-skills:wayfinder <map-url> <ticket-url>`; wayfinder runs the flow, and the ticket still lands through a PR |
| anything else                                                 | never started: it is not wayfinder work        |

The board finds a typed wayfinder ticket's map on every board (whole repo or map): the native link
(a GitHub sub-issue's parent), else `Part of #<map>` as the first line of the body. When neither
leads to a `wayfinder:map` issue, the card says "map not found", and `s` only warns.

A map's card sits in In progress and shows the map's progress: decisions so far (the top-level
bullets of the body's Decisions-so-far section), and its open and blocked tickets on the board. `s`
on the map starts its first **frontier** ticket: open, unblocked, unassigned, not already started
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

The board only reads. Wayfinder claims its tickets by assigning them, so the card moves to In
progress on the next refresh.

Done hides issues closed as not planned: a `wontfix` label, or on GitHub the close reason "not
planned". An issue closed as not planned with the `out-of-scope` label (wayfinder's out-of-scope
rule) is the exception: it stays on Done, marked `out of scope`.

## Wayfinding links

The board reads wayfinder's links between tickets; it never writes them, and never reads or writes
`docs/agents/issue-tracker.md`. It understands these encodings:

| Tracker         | Child → map                               | Blocking                                   | Out of scope                    |
| --------------- | ----------------------------------------- | ------------------------------------------ | ------------------------------- |
| GitHub          | native sub-issue of the map               | native issue dependency (`blocked_by`)     | `out-of-scope` label, closed    |
| GitLab          | `Part of #<map>` as the description's first line | native `blocks` issue link (GitLab Premium) | `out-of-scope` label, closed |
| Gitea / Forgejo | `Part of #<map>` as the body's first line | native issue dependency (`/dependencies`)  | `out-of-scope` label, closed    |

Once the map is resolved, its destination becomes a spec (`/to-spec`) and then implementation tickets
(`/to-tickets`). They link back like this, so `ticketmaster <map>` shows the decision tickets, the
spec and the spec's implementation tickets on one board:

| Tracker         | Spec → map                       | Implementation ticket → spec                                          |
| --------------- | -------------------------------- | --------------------------------------------------------------------- |
| GitHub          | `Part of #<map>` as the first line | native sub-issue of the spec, and `## Parent` naming `#<spec>`      |
| GitLab          | `Part of #<map>` as the first line | `## Parent` section naming `#<spec>`                                |
| Gitea / Forgejo | `Part of #<map>` as the first line | `## Parent` section naming `#<spec>`                                |

The spec and its tickets carry no `wayfinder:` label, so they are no decision tickets: they show in
Review as usual. They have a map all the same, so `s` starts them like a decision ticket, with
`/mattpocock-skills:wayfinder <map-url> <ticket-url>` (`wayfinder_command`). The spec stays a
ticket's parent on the board; the map only drives the prompt. On a map board the spec and its
tickets come with their map. On a whole-repo board the board follows ticket → spec (the GitHub
sub-issue parent, else the `## Parent` section) → the spec's `Part of #<map>` line → a
`wayfinder:map` issue, fetching each issue of that chain that is not on the board once per refresh.
When the chain ends anywhere but a map, the ticket is not wayfinder work and `s` refuses it. On
GitHub a spec's ticket linked by only one of its two links still counts.

Settings in `.ticketmaster.toml`:

```toml
wayfinder_command = "/wayfinder"          # if wayfinder is installed under another name
```

## Columns

Each issue lands in the first column whose rule matches:

| Column      | Rule                                                                       |
| ----------- | -------------------------------------------------------------------------- |
| Done        | closed within the last 7 days; older closed issues, and those closed as not planned (see above), are hidden; an `out-of-scope` label marks the card `out of scope` |
| Review      | has a `review:<type>` (or GitLab scoped `review::<type>`) label, or an open PR/MR that closes it; never a `wayfinder:<type>` ticket |
| Blocked     | has an open blocking issue (native, or listed in the body), or a `blocked` label |
| In progress | has an assignee                                                            |
| Ready       | everything else                                                            |

Review types are tags, not columns: an issue labelled `review:security` and `review:ux` shows
one card in Review with a red `security` chip and a magenta `ux` chip. Cards show the issue
number and title, the first ~90 characters (about two card lines) of the body (the `## Question` section when the
issue has one, as wayfinder tickets do), review tags, up to three other labels, the author (`by <login>`) and assignees.

Cards speak wayfinder's vocabulary:

- A wayfinder ticket shows who it needs: `HITL` (you at the keyboard) for `grilling` and `prototype`, `AFK`
  (the agent alone) for `research`, and for a `task` whichever of the words `HITL` / `AFK` its body names
  first (no badge when it names neither).
- A ticket closed with the `out-of-scope` label shows `out of scope` in Done, apart from the decisions
  that were resolved.
- Wayfinder tickets (`wayfinder:<type>`) resolve with a comment and close, never a PR, so they never show in Review.

Ready and In progress sort by parent order (sub-issue position), then issue number. Done sorts
newest first.

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

The map's labels and URL (needed for the wayfinder prompt) come from GitHub's `parent` field, else
from the issue a typed ticket's `Part of #<map>` line names (fetched once per refresh when it is
not on the board), on GitHub, GitLab and Gitea alike.

Flags override the file, and the file overrides the remote.

| Provider        | Access                                   | Blocking                  | Parent filter                       | Review via         |
| --------------- | ---------------------------------------- | ------------------------- | ----------------------------------- | ------------------ |
| GitHub          | `gh api graphql` (your `gh` login)       | native `blockedBy`        | native sub-issues, in order, plus the spec and its tickets | open closing PRs   |
| GitLab          | `glab api graphql` (your `glab` login)   | `blocks` links            | issues mentioning `#<parent>`, plus the spec's tickets | closing MRs        |
| Gitea / Forgejo | REST with the `tea` login token, or `GITEA_TOKEN` / `FORGEJO_TOKEN` | `/dependencies` API | issues mentioning `#<parent>`, plus the spec's tickets | open PRs whose body says `closes/fixes/resolves #<n>` |

Every provider only reads: the board has no way to close, label, assign or comment.

Besides native links, every provider counts the open issues a body lists on a `Blocked by: #3, #4`
line (or the older Norwegian `Blokkert av: #3`) or under a `## Blocked by` heading as `- #3` bullets.
A ticket leaves Blocked once every listed blocker is closed. `s` refuses to start a blocked ticket,
naming its blockers, and a ticket assigned to someone other than the tracker login the board runs
as; your own claimed tickets still start.

Where a provider lacks a feature, the board falls back: no dependency support means only body-listed
blockers or a `blocked` label mark an issue Blocked, and no sub-issues means a parent board shows issues
whose body mentions `#<parent>`.

GitHub's GraphQL budget is 5,000 points an hour. Every board makes 2 searches per refresh
(about 1,500 an hour at 5s); a map board makes one query for the map and one per spec instead.
Lengthen `--interval` if you run several boards at once.

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
```

`tests/test_board.py` drives the Textual app headlessly with a fake provider.
