---
name: ticketmaster
description: Open a live, read-only swimlane board of the current repo's wayfinder maps and, on GitHub, GitLab, Gitea/Forgejo and Linear, standalone /to-spec specs (a lane per open map or spec, its tickets in Blocked, Frontier, Claimed, Decided) in a herdr pane beside this Claude session, from which a wayfinder ticket starts /wayfinder, a spec without tickets /to-tickets and a spec's ticket /implement, each in its own worktree. Works with GitHub, GitLab, Gitea/Forgejo and Linear. Use when the user wants to see or track wayfinder work, start a map's tickets, or log in to the tracker for the board.
argument-hint: "[map] | login | cleanup <map> [--dry-run]"
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
- `cleanup <map>`: removes what the dry run marks removable (worktree, herdr workspace and agents, merged
  branch), never forcing anything; it refuses an open map. Run it when the user asks for the cleanup itself, not
  just the list, or as the final step of a session that finished a map's last work (see below), and relay the
  summary as printed. Run from inside one of the map's worktrees, it removes that worktree last, prints the
  summary, and then closes its own herdr workspace, which ends this session: make it the session's last step, as
  there is no turn after it to relay anything.

The first run, and the first after a plugin update, builds the plugin's venv, which takes a minute.

If it fails:

- **No session backend available** or **backend herdr unavailable** (exit 2): this session is not in a herdr
  pane (`HERDR_ENV is not 1`) or `herdr` is not on PATH. Tell the user to start herdr and run `/ticketmaster`
  from a Claude session inside it. **Unknown backend** (exit 2): report the known backends it lists; the name
  came from `--backend`, `TICKETMASTER_BACKEND` or `backend` in `.ticketmaster.toml`.
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

## Cleanup as a map's final step

A session that finishes a map's last work (its last ticket merged or closed) ends with cleanup, once a human has
closed the map in the tracker:

1. Run `ticketmaster cleanup <map> --dry-run`. If its first line says `(open)`, say that cleanup waits for the
   map to be closed, and stop; the user asks for the cleanup once they have closed it. Never close the map
   yourself.
2. Otherwise relay the list, since the real run may end this session before you can say anything, then run
   `ticketmaster cleanup <map>`.

Reading the list: **removable** entries go; **kept** entries stay with their reasons, and nothing is ever
forced. `open ticket` or `unmerged PR` means the ticket's work is not in yet; `uncommitted changes` (or
`git status unreadable`) means a worktree may hold work nobody committed; `agent outside a ticket worktree` is a
session working the ticket from another checkout. Name each kept entry and its reason for the user; do not remove
it by hand. `removed last: cleanup runs in it` marks this session's own worktree, whose workspace closes after the
summary. In the real run's summary, `branch … kept: not merged in git` is normal after a squash merge, and
`worktree kept: git worktree remove refused` means the worktree changed after the look.
