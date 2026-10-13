# ticketmaster

A live terminal kanban board of issue-tracker work, packaged as a Claude Code skill. Run
`/ticketmaster` in Claude Code and the board opens in a split pane beside the session; run
`ticketmaster` in a shell and it takes over that pane. It refreshes every 5 seconds.

```
Blocked (2)        Ready (3)          In progress (1)    Review (1)               Done (2)
#12 Cache layer    #14 Pick a queue   #11 Auth flow      #9 Login page            #8 Schema
  ⛔ 1               Should we use…     @kare              [security] [ux] @kare
```

## Install

```bash
git clone git@github.com:kareblak/ticketmaster.git ~/code/ticketmaster
~/code/ticketmaster/install.sh
```

`install.sh` needs Python 3.11+. It creates `.venv/` with `textual` and `pyyaml`, symlinks
`skills/ticketmaster` into `~/.claude/skills/` (so `/ticketmaster` works in every repo), and
links `~/.local/bin/ticketmaster` when that directory exists.

## Usage

```
ticketmaster [open|board|snapshot] [parent] [--label NAME] [--repo OWNER/NAME]
             [--provider github|gitlab|gitea|forgejo] [--host HOST]
             [--interval SECONDS] [--done-days DAYS] [--limit N]
```

| Command    | Effect                                                    |
| ---------- | --------------------------------------------------------- |
| `open`     | opens the board beside the caller: split pane, else tab, else window |
| `board`    | runs the board in the current terminal                    |

With no command, `ticketmaster` runs `board` when started from a terminal and `open` otherwise
(Claude Code's shell has no TTY, so `/ticketmaster` gets a split).
| `snapshot` | prints every column once as plain text                    |

Scope:

- no filter: your open issues, unassigned open issues, and your recently closed issues
- `parent` (`42`, `#42` or an issue URL): the parent's sub-issues, e.g. a wayfinder map
- `--label NAME`: issues with that label

Keys: arrows or `hjkl` move between cards (or click one), `Enter` opens the issue in the
browser, `s` or a double-click starts a session on it (see below), `/` filters (every word must
match title, body, labels or assignees), `Esc` clears the filter, `r` refreshes now, `q` quits.

## Starting work from the board

Select a card in **Ready** and press `s` or double-click it. The board asks where to open the
session: `t` new tab, `r` pane to the right, `d` pane below, `a` auto layout (`n` or `Esc`
cancels). It runs:

```
claude -w issue-<n> -n "#<n> <title>" "<prompt>"
```

so the session gets its own git worktree (`.claude/worktrees/issue-<n>`) and a recognizable
name. The prompt depends on the ticket:

| Ticket                                                        | Prompt                                         |
| ------------------------------------------------------------- | ---------------------------------------------- |
| a wayfinder map (`wayfinder:map`)                             | `/mattpocock-skills:wayfinder <map-url>`        |
| has a `wayfinder:<type>` label and its parent is a map        | `/mattpocock-skills:wayfinder <map-url> <ticket-url>` |
| anything else                                                 | "Work on issue #n … assign it to yourself before doing any work" |

The board never writes to the tracker: wayfinder claims its tickets by assigning them, and the
plain prompt tells the session to do the same, so the card moves to In progress on the next
refresh. Until then it shows "▶ session started", and the board refuses to start the same
ticket twice. Only Ready tickets can be started.

## Terminal support

| Terminal        | Split (board, `r`/`d`)         | Tab (`t`)                |
| --------------- | ------------------------------ | ------------------------ |
| tmux (any host) | `split-window -h`              | `new-window`             |
| Ghostty 1.3+ (macOS) | AppleScript split of the focused pane | AppleScript new tab |
| iTerm2          | split current session          | new tab                  |
| WezTerm         | `wezterm cli split-pane`       | `wezterm cli spawn`      |
| kitty           | `kitty @ launch` vsplit (needs `allow_remote_control`) | `--type=tab` |
| Terminal.app    | new window                     | new window (no tab API)  |
| gnome-terminal  | new tab                        | `--tab`                  |
| others (Linux)  | new window (ghostty, konsole, x-terminal-emulator) | new window |

tmux is checked first, so inside tmux everything opens as tmux panes and windows regardless of
the host terminal. Ghostty splits the focused pane of its front window: the pane you typed `/ticketmaster` in,
or the board itself when starting a session. Where a terminal can't split, `r`/`d` fall back to
a tab, then a window.

### Auto layout (`a`, Ghostty on macOS)

Sessions are tiled in the board's tab as a two-row grid, with the board fixed top-left:

```
1 session    2            3            4                5
B | 1        B | 1        B | 1        B | 1 | 4        B | 1 | 4
             2 |          2 | 3        2 | 3            2 | 3 | 5
```

Ghostty can only split one pane at a time, so the order is fixed: session 1 splits the board
right, 2 splits the board down, 3 splits session 1 down, then new sessions alternate between
extending the top and bottom rows to the right. Each split is followed by `equalize_splits`.
The board remembers which panes it placed; if you close one, the next session starts the
layout again from the board. Outside Ghostty (or inside tmux), `a` behaves like `r`.

Settings in `.ticketmaster.toml`:

```toml
worktree = false                          # start sessions in the repo root, no `-w`
wayfinder_command = "/wayfinder"          # if wayfinder is installed under another name
```

`--no-worktree` does the same as `worktree = false` for one run.

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
issue has one, as wayfinder tickets do), review tags, up to three other labels and assignees.

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

GitHub's GraphQL budget is 5,000 points an hour. The default board makes 3 searches per refresh
(about 2,200 an hour at 5s), so lengthen `--interval` if you run several boards at once.

## Development

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

`tests/test_board.py` drives the Textual app headlessly with a fake provider.
