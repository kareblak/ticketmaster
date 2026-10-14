---
name: ticketmaster
description: Open a live, read-only kanban board (Blocked, Ready, In progress, Review, Done) of the current repo's issues in a herdr pane beside the Claude session, from which a wayfinder ticket starts /wayfinder in its own git worktree workspace with Claude as a herdr agent. Works with GitHub, GitLab and Gitea/Forgejo. Use when the user wants to see or track the work being done, or start a wayfinder map's tickets.
argument-hint: "[parent-issue] [--repo owner/name] [--provider github|gitlab|gitea] [--host <host>]"
---

Open the board for the repo in the current working directory, in a herdr pane next to the
calling pane, the one this session runs in (labelled `ticketmaster`; running it again restarts
the board there). If the inherited `HERDR_PANE_ID` is gone or belongs to another session or
project, the board opens next to the focused pane instead; the result line names the pane:

```bash
ticketmaster open $ARGUMENTS
```

- No arguments: every open issue, and every issue closed in the last 7 days.
- A number (`42`, `#42`, or an issue URL): the sub-issues of that parent, e.g. a wayfinder map; for a
  map, also the spec linked to it (`Part of #<map>`) and that spec's implementation tickets.

Then reply with the script's one-line result.

If it says it runs inside herdr, this session is not in a herdr pane: tell the user to start
herdr and run `/ticketmaster` from a Claude session inside it.
If `ticketmaster` is not found, tell the user to install it:
`pipx install git+https://github.com/kareblak/ticketmaster.git`.
If it fails with a provider error (CLI missing or not logged in, unknown host), report the error
as printed and the fix: `gh auth login`, `glab auth login`, `tea login add`, or a
`.ticketmaster.toml` with `provider`/`host`/`repo` for self-hosted trackers.

On the board, `Enter` goes to a ticket's herdr workspace (resuming Claude with `--continue` if it
exited); with none it only says what `s` would do. `s` starts `/mattpocock-skills:wayfinder <map>
<ticket>` in a new worktree (`issue-<n>`, `issue-<n>-2`…) as herdr agent `issue-<n>`: for a
wayfinder ticket, for the spec a map led to and its implementation tickets, and, on a map's card,
for the map's first frontier ticket. It refuses a blocked ticket, one assigned to someone else, and
any issue that is not wayfinder work, and says why. Cards show each session's agent state (working,
needs you, idle, done, no agent).

The board never writes to the tracker or the repo, and never removes a worktree. Column rules
(first match wins):

| Column      | Rule                                                                     |
| ----------- | ------------------------------------------------------------------------ |
| Done        | closed within the last 7 days; older closed issues hidden                |
| Review      | has a `review:<type>` / `review::<type>` label, or an open linked PR/MR  |
| Blocked     | has an open blocking issue (native, or a `Blocked by: #n` body line or `## Blocked by` section), or a `blocked` label |
| In progress | has an assignee                                                          |
| Ready       | everything else                                                          |

Each `review:<type>` label (security, ux, ui, …) shows as a colored tag on the card inside the
single Review column.
