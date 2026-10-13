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

Upgrade with `pipx install --force git+https://github.com/kareblak/ticketmaster.git && ticketmaster install-skill`
(append `@<branch>` or `@<tag>` to the URL to pin a version). Uninstall with
`pipx uninstall ticketmaster && rm -r ~/.claude/skills/ticketmaster`.

For development, clone the repo and run `./install.sh`. It installs the clone as an editable
pipx app (code changes apply without reinstalling) and installs the skill.

## Usage

```
ticketmaster [open|snapshot|adopt|install-skill] [parent] [--label NAME] [--repo OWNER/NAME]
             [--provider github|gitlab|gitea|forgejo] [--host HOST] [--columns auto|one|all]
             [--interval SECONDS] [--done-days DAYS] [--limit N]
```

| Command         | Effect                                                                         |
| --------------- | ------------------------------------------------------------------------------ |
| `open` (default)| opens the board in a herdr pane right of the calling pane (the focused pane when `HERDR_PANE_ID` is stale or another session's), labelled `ticketmaster`; run again, it restarts there; run in that pane, it runs the board right there |
| `adopt`         | opens a herdr workspace for every `.claude/worktrees/*` checkout without one   |
| `snapshot`      | prints every column once as plain text (works outside herdr)                   |
| `install-skill` | writes the `/ticketmaster` skill into `~/.claude/skills`                       |

`open` and `adopt` only run inside herdr (`HERDR_ENV=1`).

Scope:

- no filter: every open issue, and every issue closed within `--done-days`
- `parent` (`42`, `#42` or an issue URL): the parent's sub-issues, e.g. a wayfinder map
- `--label NAME`: issues with that label

The board pane takes about a third of the width, so it shows one column at a time: `←`/`→`
switch columns, `1`–`5` jump to one, `v` shows all side by side (`--columns` sets the start).

## Keys

| Key            | Column            | Effect                                                              |
| -------------- | ----------------- | ------------------------------------------------------------------- |
| `Enter` / double-click | any       | go to the ticket's herdr workspace; with none: start (Ready, In progress) or review (Review) |
| `s`            | all but Done      | start the ticket in a **new** worktree (always new; asks first)     |
| `c`            | Review            | start a review session in a new `review-<n>` worktree               |
| `x`            | Done              | remove the ticket's worktrees (herdr refuses if one has uncommitted changes) |
| `a`            | any               | toggle auto-remove of Done tickets' worktrees (see below; saved)    |
| `o`            | any               | open the issue in the browser                                       |
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
| a wayfinder map (`wayfinder:map`)                             | `/mattpocock-skills:wayfinder <map-url>`        |
| has a `wayfinder:<type>` label and its parent is a map        | `/mattpocock-skills:wayfinder <map-url> <ticket-url>` |
| anything else                                                 | "Work on issue #n … assign it to yourself before doing any work" |

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

The board never writes to the tracker: wayfinder claims its tickets by assigning them, and the
plain prompt tells the session to do the same, so the card moves to In progress on the next
refresh.

Settings in `.ticketmaster.toml`:

```toml
wayfinder_command = "/wayfinder"          # if wayfinder is installed under another name
review_command = "/code-review"           # default /mattpocock-skills:code-review
```

## Columns

Each issue lands in the first column whose rule matches:

| Column      | Rule                                                                       |
| ----------- | -------------------------------------------------------------------------- |
| Done        | closed within `--done-days` (default 7); older closed issues are hidden    |
| Review      | has a `review:<type>` (or GitLab scoped `review::<type>`) label, or an open PR/MR that closes it |
| Blocked     | has an open blocking issue, or a `blocked` label                           |
| In progress | has an assignee                                                            |
| Ready       | everything else                                                            |

Review types are tags, not columns: an issue labelled `review:security` and `review:ux` shows
one card in Review with a red `security` chip and a magenta `ux` chip. Cards show the issue
number and title, the first ~90 characters (about two card lines) of the body (the `## Question` section when the
issue has one, as wayfinder tickets do), review tags, up to three other labels, the author (`by <login>`) and assignees.

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

The parent's labels and URL (needed for the wayfinder prompt) come from GitHub's `parent` field;
GitLab and Gitea boards only know a ticket's parent when the board is scoped to one.

Flags override the file, and the file overrides the remote.

| Provider        | Access                                   | Blocking                  | Parent filter                       | Review via         |
| --------------- | ---------------------------------------- | ------------------------- | ----------------------------------- | ------------------ |
| GitHub          | `gh api graphql` (your `gh` login)       | native `blockedBy`        | native sub-issues, in order         | open closing PRs   |
| GitLab          | `glab api graphql` (your `glab` login)   | `blocks` links            | issues mentioning `#<parent>`       | closing MRs        |
| Gitea / Forgejo | REST with the `tea` login token, or `GITEA_TOKEN` / `FORGEJO_TOKEN` | `/dependencies` API | issues mentioning `#<parent>` | open PRs whose body says `closes/fixes/resolves #<n>` |

Where a provider lacks a feature, the board falls back: no dependency support means only a
`blocked` label marks an issue Blocked, and no sub-issues means a parent board shows issues
whose body mentions `#<parent>`.

GitHub's GraphQL budget is 5,000 points an hour. Every board makes 2 searches per refresh
(about 1,500 an hour at 5s), so lengthen `--interval` if you run several boards at once.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

`tests/test_board.py` drives the Textual app headlessly with a fake provider.
