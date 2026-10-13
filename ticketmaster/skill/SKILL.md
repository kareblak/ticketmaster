---
name: ticketmaster
description: Open a live kanban board (Blocked, Ready, In progress, Review, Done) of the current repo's issues in a herdr pane beside the Claude session, where each ticket starts in its own git worktree workspace with Claude as a herdr agent. Works with GitHub, GitLab and Gitea/Forgejo. Use when the user wants to see, track or start the work being done, e.g. a wayfinder map's tickets.
argument-hint: "[parent-issue] [--label <name>] [--repo owner/name] [--provider github|gitlab|gitea] [--host <host>]"
---

Open the board for the repo in the current working directory, in a herdr pane next to the
calling pane, the one this session runs in (labelled `ticketmaster`; running it again restarts
the board there). If the inherited `HERDR_PANE_ID` is gone or belongs to another session or
project, the board opens next to the focused pane instead; the result line names the pane:

```bash
ticketmaster open $ARGUMENTS
```

- No arguments: every open issue, and every issue closed in the last 7 days.
- A number (`42`, `#42`, or an issue URL): the sub-issues of that parent, e.g. a wayfinder map.
- `--label <name>`: issues with that label.

Then reply with the script's one-line result.

If it says it runs inside herdr, this session is not in a herdr pane: tell the user to start
herdr and run `/ticketmaster` from a Claude session inside it.
If `ticketmaster` is not found, tell the user to install it:
`pipx install git+https://github.com/kareblak/ticketmaster.git && ticketmaster install-skill`.
If it fails with a provider error (CLI missing or not logged in, unknown host), report the error
as printed and the fix: `gh auth login`, `glab auth login`, `tea login add`, or a
`.ticketmaster.toml` with `provider`/`host`/`repo` for self-hosted trackers.

To answer a question about the board's contents without opening a pane, run the same script
with `snapshot` instead of `open`; it prints every column once as plain text.

On the board, `Enter` goes to a ticket's herdr workspace (resuming Claude with `--continue` if it
exited), or starts one when there is none. `s` always starts the ticket in a new worktree
(`issue-<n>`, `issue-<n>-2`…) as herdr agent `issue-<n>`, with `/mattpocock-skills:wayfinder
<map> <ticket>` for wayfinder tickets or a plain "work on issue" prompt otherwise. `c` starts a
review session for a Review ticket in a `review-<n>` worktree; `x` removes a Done ticket's
worktrees; `a` toggles auto-remove (saved per user), where the board does that itself once the
agent is idle, keeping worktrees with uncommitted or unpushed work. Cards
show each session's agent state (working, needs you, idle, done, no agent).
`ticketmaster adopt` opens herdr workspaces for older `claude -w` worktrees.

The board never changes issues. Column rules (first match wins):

| Column      | Rule                                                                     |
| ----------- | ------------------------------------------------------------------------ |
| Done        | closed within the last 7 days (`--done-days`); older closed issues hidden |
| Review      | has a `review:<type>` / `review::<type>` label, or an open linked PR/MR  |
| Blocked     | has an open blocking issue, or a `blocked` label                         |
| In progress | has an assignee                                                          |
| Ready       | everything else                                                          |

Each `review:<type>` label (security, ux, ui, …) shows as a colored tag on the card inside the
single Review column.
