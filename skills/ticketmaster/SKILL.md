---
name: ticketmaster
description: Open a live terminal kanban board (Blocked, Ready, In progress, Review, Done) of the current repo's issues in a new terminal window. Works with GitHub, GitLab and Gitea/Forgejo. Use when the user wants to see or track the work being done, e.g. a wayfinder map's tickets.
argument-hint: "[parent-issue] [--label <name>] [--repo owner/name] [--provider github|gitlab|gitea] [--host <host>]"
---

Open the board for the repo in the current working directory:

```bash
~/.claude/skills/ticketmaster/scripts/ticketmaster open $ARGUMENTS
```

- No arguments: the user's open issues, unassigned open issues, and their issues closed in the last 7 days.
- A number (`42`, `#42`, or an issue URL): the sub-issues of that parent, e.g. a wayfinder map.
- `--label <name>`: issues with that label.

Then reply with the script's one-line result. If it printed a command because no terminal
launcher worked, give the user that command to paste into another window.

If the script exits with "not installed", run the `install.sh` it names, then retry once.
If it fails with a provider error (CLI missing or not logged in, unknown host), report the error
as printed and the fix: `gh auth login`, `glab auth login`, `tea login add`, or a
`.ticketmaster.toml` with `provider`/`host`/`repo` for self-hosted trackers.

To answer a question about the board's contents without opening a window, run the same script
with `snapshot` instead of `open`; it prints every column once as plain text.

The board is read-only: it never changes issues. Column rules (first match wins):

| Column      | Rule                                                                     |
| ----------- | ------------------------------------------------------------------------ |
| Done        | closed within the last 7 days (`--done-days`); older closed issues hidden |
| Review      | has a `review:<type>` / `review::<type>` label, or an open linked PR/MR  |
| Blocked     | has an open blocking issue, or a `blocked` label                         |
| In progress | has an assignee                                                          |
| Ready       | everything else                                                          |

Each `review:<type>` label (security, ux, ui, …) shows as a colored tag on the card inside the
single Review column.
