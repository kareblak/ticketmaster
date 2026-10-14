---
name: ticketmaster
description: Open a live, read-only swimlane board of the current repo's wayfinder maps (a lane per open map, its tickets in Blocked, Frontier, Claimed, Decided) in a herdr pane beside this Claude session, from which a wayfinder ticket starts /wayfinder in its own worktree. Works with GitHub, GitLab and Gitea/Forgejo. Use when the user wants to see or track wayfinder work, start a map's tickets, or log in to the tracker for the board.
argument-hint: "[map] | login"
---

Run the plugin's `ticketmaster` command with the user's arguments, then reply with its one-line result:

```bash
ticketmaster $ARGUMENTS
```

- No arguments, or a map (`42`, `#42` or an issue URL): opens the board in a herdr pane next to this
  session, labelled `ticketmaster`; running it again restarts the board there. A named map shows even when it
  is closed, as its header card alone.
- `login`: logs in to the tracker for the board; relay what it prints.

The first run, and the first after a plugin update, builds the plugin's venv, which takes a minute.

If it fails:

- **Runs inside herdr**: this session is not in a herdr pane. Tell the user to start herdr and run
  `/ticketmaster` from a Claude session inside it.
- **Building the plugin venv failed**: report the printed reason. The fix is Python 3.11 or newer as `python3` on
  PATH (`brew install python@3.12`) and network access for pip, then `/ticketmaster` again.
- **Provider or login error** (CLI missing or not logged in, unknown host): report it as printed, with the fix:
  `gh auth login`, `glab auth login`, `tea login add`, `/ticketmaster login`, or a `.ticketmaster.toml` with
  `provider`/`host`/`repo` for a self-hosted tracker.

On the board, `Enter` goes to a ticket's herdr session, or says what `s` would do. `s` starts
`/wayfinder` for the ticket in its own git worktree and herdr workspace, or says why it refuses.
