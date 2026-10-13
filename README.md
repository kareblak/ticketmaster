# ticketmaster

A live terminal kanban board of issue-tracker work, packaged as a Claude Code skill. Run
`/ticketmaster` in Claude Code (or `ticketmaster` in a shell) and the board opens in a new
terminal window, refreshing every 5 seconds.

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
| `open`     | default; opens the board in a new terminal window         |
| `board`    | runs the board in the current terminal                    |
| `snapshot` | prints every column once as plain text                    |

Scope:

- no filter: your open issues, unassigned open issues, and your recently closed issues
- `parent` (`42`, `#42` or an issue URL): the parent's sub-issues, e.g. a wayfinder map
- `--label NAME`: issues with that label

Keys: arrows or `hjkl` move between cards, `Enter` opens the issue in the browser, `/` filters
(every word must match title, body, labels or assignees), `Esc` clears the filter, `r`
refreshes now, `q` quits.

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
