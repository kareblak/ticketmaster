# ticketmaster

A live kanban board of issue-tracker work for [herdr](https://herdr.dev), packaged as a Claude
Code skill. Run `/ticketmaster` in a Claude session inside herdr and the board opens in a pane
beside it. Every ticket you start gets its own git worktree, opened as a herdr workspace with
Claude running in it as a herdr agent, and each card shows what that agent is doing. It
refreshes every 5 seconds.

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
ticketmaster install-skill
```

The first command puts `ticketmaster` on your PATH (`~/.local/bin`). The second writes the
skill to `~/.claude/skills/ticketmaster/SKILL.md`, so `/ticketmaster` works in every Claude Code
session started afterwards, in any repo (`CLAUDE_SKILLS_DIR` overrides the location). Nothing is
installed per project: the board reads the repo it is started in.

Upgrade with `ticketmaster upgrade`: it reinstalls from the GitHub source with pipx, refreshes the
skill, and prints the commit it now runs (`ticketmaster --version` prints it any time). To pin a
version instead, run `pipx install --force git+https://github.com/kareblak/ticketmaster.git@<tag> && ticketmaster install-skill`.
Uninstall with `pipx uninstall ticketmaster && rm -r ~/.claude/skills/ticketmaster`.

For development, clone the repo and run `./install.sh`. It installs the clone as an editable
pipx app (code changes apply without reinstalling) and installs the skill.
`ticketmaster upgrade` leaves such an install alone and tells you to `git pull` in the clone.

When the board opens it asks GitHub (`git ls-remote`, in the background, once per start) for the
head of `main`. If that differs from the commit your install was built from, a toast and the status
line say `a newer ticketmaster is available: run ticketmaster upgrade`. It stays silent when offline,
when `git` is missing, on editable installs, and when the install does not know its commit.

## Usage

```
ticketmaster [open|snapshot|adopt|setup|install-skill|upgrade] [parent] [--label NAME] [--repo OWNER/NAME]
             [--provider github|gitlab|gitea|forgejo] [--host HOST] [--columns auto|one|all]
             [--interval SECONDS] [--done-days DAYS] [--limit N]
```

| Command         | Effect                                                                         |
| --------------- | ------------------------------------------------------------------------------ |
| `open` (default)| opens the board in a herdr pane right of the calling pane (the focused pane when `HERDR_PANE_ID` is stale or another session's), labelled `ticketmaster`; run again, it restarts there; run in that pane, it runs the board right there |
| `adopt`         | opens a herdr workspace for every `.claude/worktrees/*` checkout without one   |
| `snapshot`      | prints every column once as plain text (works outside herdr)                   |
| `setup`         | writes the wayfinding linking rules for the repo's tracker into `docs/agents/issue-tracker.md` (see [Wayfinding rules](#wayfinding-rules)) |
| `install-skill` | writes the `/ticketmaster` skill into `~/.claude/skills`                       |
| `upgrade`       | reinstalls from the GitHub source with pipx, then refreshes the skill; on an editable install it says to `git pull` in the clone instead |

`open` and `adopt` only run inside herdr (`HERDR_ENV=1`).

Scope:

- no filter: every open issue, and every issue closed within `--done-days`
- `parent` (`42`, `#42` or an issue URL): the parent's sub-issues, e.g. a wayfinder map; for a map,
  also the spec linked to it and that spec's implementation tickets (see [Wayfinding rules](#wayfinding-rules))
- `--label NAME`: issues with that label

The board pane takes about a third of the width, so it shows one column at a time: `←`/`→`
switch columns, `1`–`5` jump to one, `v` shows all side by side (`--columns` sets the start).

## Keys

| Key            | Column            | Effect                                                              |
| -------------- | ----------------- | ------------------------------------------------------------------- |
| `Enter` / double-click | any       | go to the ticket's herdr workspace; with none: start (Ready, In progress) or review (Review) |
| `s`            | all but Done      | start the ticket in a **new** worktree (always new; asks first); refused while it is blocked, assigned to someone else, or held by a triage label (see below) |
| `c`            | Review            | start a review session in a new `review-<n>` worktree; never for wayfinder tickets |
| `x`            | Done              | remove the ticket's worktrees (herdr refuses if one has uncommitted changes) |
| `a`            | any               | toggle auto-remove of Done tickets' worktrees (see below; saved)    |
| `o`            | any               | open the issue in the browser                                       |
| `Backspace` `Delete` | Blocked, Ready | close the issue as not planned with the `wontfix` label (asks first; its worktrees are left behind) |
| `/` `Esc` `r` `q` |                | filter, clear both filters, refresh now, quit                       |
| `f`            | any               | filter picker: labels cycle include → exclude → off, authors on/off (see below; saved per repo) |
| `?`            | any               | help: keys, columns, scope and how sessions start                   |

Arrows or `hjkl` move between cards.

## Sessions in herdr

Starting a ticket runs `herdr worktree create --branch issue-<n>` (or `issue-<n>-2`, `-3`… when
that branch already exists, so a start never reuses an old checkout), then starts Claude in the
new workspace as herdr agent `issue-<n>`:

```
claude -n "#<n> <title>" "<prompt>"
```

| Ticket                                                        | Prompt                                         |
| ------------------------------------------------------------- | ---------------------------------------------- |
| has a `wayfinder:<type>` label (`research`, `prototype`, `grilling`, `task`; GitLab's `wayfinder::<type>` too) | `/mattpocock-skills:wayfinder <map-url> <ticket-url>` |
| anything else                                                 | `/mattpocock-skills:implement <ticket-url>` plus a brief: assign it to yourself first, implement via the skill, then push and open a ready PR that closes it (body via the `pr` skill) |

The mattpocock skills a session runs stop to ask for `/setup-matt-pocock-skills` when the repo
has no `docs/agents/issue-tracker.md`, which would leave the session waiting. Until that file
exists, the status line warns and names the command, so it can be run once before starting
sessions. The board checks again on every refresh, and `s` and `c` still work while the warning shows.

A typed wayfinder ticket never starts implement. The board finds its map on every board (whole
repo, `--label` or map) by the rule `ticketmaster setup` writes: the native link (a GitHub
sub-issue's parent), else `Part of #<map>` as the first line of the body. When neither leads to a
`wayfinder:map` issue, the card (and `snapshot`) says "map not found", and starting it (`s`, or
`Enter` on a card without a session) only warns.

A map (`wayfinder:map`) is never started itself. Its card sits in In progress and shows the map's
progress: decisions so far (the top-level bullets of the body's Decisions-so-far section), and its
open and blocked tickets on the board. `s` (or `Enter` with no session) on the map starts its first
**frontier** ticket: open, unblocked, unassigned, not already started from this board or holding a
session, in map order (GitHub's sub-issue order, asked for when you press `s`; else issue number).
That ticket gets its own `issue-<n>` worktree, card and
`/mattpocock-skills:wayfinder <map-url> <ticket-url>` session, so a second press starts the next
frontier ticket. With an empty frontier the board says so and starts nothing.

A review session (`c`) gets a `review-<n>` worktree and starts `/mattpocock-skills:code-review`
the same way: the prompt tells it to find the PR that closes the issue, check out its branch, and
review it with the PR's base branch (e.g. `origin/main`) as the fixed point and the issue URL as
the spec. Findings stay in the session; it does not push or comment on the PR without asking.

The board finds a ticket's sessions through its worktrees: any checkout named `issue-<n>`,
`issue-<n>-<k>` or `review-<n>`, including older `claude -w issue-<n>` ones under
`.claude/worktrees`. Each card lists them with the state of the agent in their workspace:
`● working`, `⚠ needs you`, `◌ idle`, `✓ done`, or `○ no agent` when the workspace is closed or
Claude has exited. `Enter` on such a card focuses the workspace, reopening it and resuming
Claude with `claude --continue` when needed.

With auto-remove on (`a` toggles it; the status line shows `auto-remove on`), the board cleans
up by itself: when a ticket is in Done, each of its worktrees is removed as `x` would, once its
agent is no longer `working` or `needs you`. A worktree is kept, with a notification saying why, when it has uncommitted changes
or commits that are on no remote (`git rev-list HEAD --not --remotes`). A squash-merged branch
whose remote branch was deleted counts as unpushed, so remove that one with `x`. The board tries
each worktree once per run; a kept one is retried after the board restarts. The setting is
saved per user in `$XDG_CONFIG_HOME/ticketmaster/settings.json` (`~/.config/…`), so it applies to
every board until toggled off.

`f` opens a picker listing every label and every author on the board's tickets; `Enter` (or space,
or a click) cycles a label through include, exclude and off, and selects or deselects an author. The
board narrows as you go. With any label included, a ticket shows only if it has at least one of
them; a ticket with any excluded label is hidden. With any author selected, a ticket shows only if
one of them opened it. A ticket must pass the labels, the authors and the `/` text filter. The
status line shows the selection (`labels: +bug -wontfix · authors: alice, bob`); `c` in the picker
or `Esc` on the board clears it. The selection is saved per repo in the same settings file and
restored when the board reopens; a saved label or author that is no longer on the board is ignored
until it comes back. This filters what the board shows, unlike
`--label`, which scopes the query.

The board writes to the tracker only to close an issue as not planned: wayfinder claims its tickets by assigning
them, and the implement prompt tells the session to do the same, so the card moves to In progress
on the next refresh.

`Backspace` or `Delete` on a Blocked or Ready card closes the issue as not planned after a confirm
dialog, and labels it `wontfix`: `gh issue close --reason "not planned"` on GitHub, and on GitLab and
Gitea, which have no close reason, the label is the marker. Plain write access is enough on all
three trackers. On a Done card the keys do nothing; In progress and Review cards are refused. The
ticket's worktrees are not removed; the dialog lists them so you can remove them in herdr yourself.
A tracker refusal shows as an error notification and the board stays as it was. In the `/` filter
input, `Backspace` edits the text as usual.

## Wayfinding rules

Wayfinder links tickets the way the "Wayfinding operations" section of the repo's
`docs/agents/issue-tracker.md` says, and the board reads the same links, so ticketmaster owns that
section. Run `ticketmaster setup` in the repo once (and again after upgrading ticketmaster): it
writes the section for the repo's tracker, replacing an existing one in place and leaving the rest
of the file alone, or creates the doc when there is none. It pins these encodings:

| Tracker         | Child → map                               | Blocking                                   | Out of scope                    |
| --------------- | ----------------------------------------- | ------------------------------------------ | ------------------------------- |
| GitHub          | native sub-issue of the map               | native issue dependency (`blocked_by`)     | `out-of-scope` label, closed    |
| GitLab          | `Part of #<map>` as the description's first line | native `blocks` issue link (GitLab Premium) | `out-of-scope` label, closed |
| Gitea / Forgejo | `Part of #<map>` as the body's first line | native issue dependency (`/dependencies`)  | `out-of-scope` label, closed    |

Once the map is resolved, its destination becomes a spec (`/to-spec`) and then implementation tickets
(`/to-tickets`). The section pins how they link back, so `ticketmaster <map>` shows the decision
tickets, the spec and the spec's implementation tickets on one board:

| Tracker         | Spec → map                       | Implementation ticket → spec                                          |
| --------------- | -------------------------------- | --------------------------------------------------------------------- |
| GitHub          | `Part of #<map>` as the first line | native sub-issue of the spec, and `## Parent` naming `#<spec>`      |
| GitLab          | `Part of #<map>` as the first line | `## Parent` section naming `#<spec>`                                |
| Gitea / Forgejo | `Part of #<map>` as the first line | `## Parent` section naming `#<spec>`                                |

The spec and its tickets carry no `wayfinder:` label, so `s` starts them with the implement skill;
decision tickets keep wayfinder. Only a `wayfinder:map` parent gets this; on GitHub a spec's ticket
linked by only one of its two links still counts.

Blocking is always the native link, never a `Blocked by:` body line. The keywords `Part of` and
`## Parent` and the `out-of-scope` label are always English, even when the rest of the issue is in another language. While the repo's section is missing or differs
from what this version writes, the status line warns and names `ticketmaster setup`; when the doc
itself is missing, it names `/setup-matt-pocock-skills` first, then `ticketmaster setup`.

Settings in `.ticketmaster.toml`:

```toml
wayfinder_command = "/wayfinder"          # if wayfinder is installed under another name
implement_command = "/implement"          # default /mattpocock-skills:implement (plain tickets)
review_command = "/code-review"           # default /mattpocock-skills:code-review
```

## Columns

Each issue lands in the first column whose rule matches:

| Column      | Rule                                                                       |
| ----------- | -------------------------------------------------------------------------- |
| Done        | closed within `--done-days` (default 7); older closed issues, and those closed as not planned (see below), are hidden; an `out-of-scope` label marks the card `out of scope` |
| Review      | has a `review:<type>` (or GitLab scoped `review::<type>`) label, or an open PR/MR that closes it; never a `wayfinder:<type>` ticket |
| Blocked     | has an open blocking issue (native, or listed in the body), or a `blocked` label |
| In progress | has an assignee                                                            |
| Ready       | everything else                                                            |

Review types are tags, not columns: an issue labelled `review:security` and `review:ux` shows
one card in Review with a red `security` chip and a magenta `ux` chip. Cards show the issue
number and title, the first ~90 characters (about two card lines) of the body (the `## Question` section when the
issue has one, as wayfinder tickets do), review tags, up to three other labels, the author (`by <login>`) and assignees.

Cards speak wayfinder's and the triage labels' vocabulary:

- A wayfinder ticket shows who it needs: `HITL` (you at the keyboard) for `grilling` and `prototype`, `AFK`
  (the agent alone) for `research`, and for a `task` whichever of the words `HITL` / `AFK` its body names
  first (no badge when it names neither).
- A ticket closed with the `out-of-scope` label (as `ticketmaster setup` pins it) shows `out of scope` in Done,
  apart from the decisions that were resolved.
- Wayfinder tickets (`wayfinder:<type>`) resolve with a comment and close, never a PR, so they never show in Review and
  `c` refuses them.
- A ticket labelled `needs-info` or `ready-for-human` shows the label (`⏸ needs-info`) and `s` / `Enter`
  refuse to start it, with the reason. The names are the repo's own, from the right-hand column of
  `docs/agents/triage-labels.md`; without that doc the canonical names apply.

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

Besides native links, every provider counts the open issues a body lists on a `Blocked by: #3, #4`
line (or the older Norwegian `Blokkert av: #3`) or under a `## Blocked by` heading as `- #3` bullets.
A ticket leaves Blocked once every listed blocker is closed. `s` and `Enter` refuse to start a
blocked ticket, naming its blockers, and a ticket assigned to someone other than the tracker login
the board runs as; your own claimed tickets still start.

Where a provider lacks a feature, the board falls back: no dependency support means only body-listed
blockers or a `blocked` label mark an issue Blocked, and no sub-issues means a parent board shows issues
whose body mentions `#<parent>`.

GitHub's GraphQL budget is 5,000 points an hour. Every board makes 2 searches per refresh
(about 1,500 an hour at 5s); a map board makes one query for the map and one per spec instead.
Lengthen `--interval` if you run several boards at once.

When the tracker rate-limits the board (GitHub's primary or secondary limit, or an HTTP 429 from
GitLab or Gitea/Forgejo), ticket fetches drop to once a minute and herdr session states to every
10s; the status line shows `rate-limited · next fetch in 42s`, and `r` refreshes only the sessions.
The first good fetch restores `--interval`, once: a second rate limit keeps the board at once a
minute until you restart it.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

`tests/test_board.py` drives the Textual app headlessly with a fake provider.
