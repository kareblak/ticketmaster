---
name: ticketmaster
description: Open a live, read-only swimlane board of the current repo's wayfinder maps and, on GitHub, standalone /to-spec specs (a lane per open map or spec, its tickets in Blocked, Frontier, Claimed, Decided) in a herdr pane beside this Claude session, from which a wayfinder ticket starts /wayfinder, a spec without tickets /to-tickets and a spec's ticket /implement, each in its own worktree. Works with GitHub, GitLab, Gitea/Forgejo and Linear. Use when the user wants to see or track wayfinder work, start a map's tickets, or log in to the tracker for the board.
argument-hint: "[map] | login | cleanup <map> --dry-run"
---

Run the plugin's `ticketmaster` command with the user's arguments, then reply with its one-line result:

```bash
ticketmaster $ARGUMENTS
```

- No arguments, or a map (`42`, `#42`, `TM-1` or an issue URL): opens the board in a herdr pane next to this
  session, labelled `ticketmaster`; running it again restarts the board there. A named map shows even when it
  is closed, as its header card alone.
- `login`: logs in to Linear with OAuth in the browser, for a board without a personal API key; it needs
  `oauth_client_id` in `.ticketmaster.toml` and waits up to 5 minutes for the browser, so run it with a Bash
  timeout of at least 330000 ms. Relay what it prints: the token file it wrote, or the config line to add. On the
  other trackers it names their own login (`gh auth login`, `glab auth login`, `tea login add`).
- `cleanup <map> --dry-run`: lists the map's and its tickets' worktrees, branches and herdr workspaces, each
  marked removable or kept with the reason; it changes nothing. Relay the list as printed.

The first run, and the first after a plugin update, builds the plugin's venv, which takes a minute.

If it fails:

- **Runs inside herdr**: this session is not in a herdr pane. Tell the user to start herdr and run
  `/ticketmaster` from a Claude session inside it.
- **Building the plugin venv failed**: report the printed reason. The fix is Python 3.11 or newer as `python3` on
  PATH (`brew install python@3.12`) and network access for pip, then `/ticketmaster` again.
- **Provider or login error** (CLI missing or not logged in, unknown host): report it as printed, with the fix:
  `gh auth login`, `glab auth login`, `tea login add`, `/ticketmaster login`, or a `.ticketmaster.toml` with
  `provider`/`host`/`repo` for a self-hosted tracker. On Linear: `provider = "linear"` and `team = "<key>"` in
  `.ticketmaster.toml`, and a personal API key in `LINEAR_API_KEY` (or the `LINEAR_` variable `key_env` names),
  or `oauth_client_id` there and `/ticketmaster login`; when a stored login has expired, `/ticketmaster login`.

On the board, `Enter` goes to the session working a ticket (or its worktree, with a shell), or says it has
none and what `s` would do; it never starts Claude. `s` starts (or continues) `/wayfinder` for the ticket,
or a map's next frontier ticket, `/to-tickets` on a spec without tickets, or `/implement` on a spec's ticket,
in its own git worktree and herdr workspace, or says why it refuses.
