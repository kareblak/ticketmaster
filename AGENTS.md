# AGENTS.md

## Agent skills

### Issue tracker

Issues live in Linear, team Ticketmaster, via the `linear-server` MCP; code and PRs stay on GitHub. See `docs/agents/issue-tracker.md`.

#### Issue lifecycle

Every `TM-<n>` issue a session works on moves through Linear's states as the work moves, whichever skill drives it
(`/implement`, `/wayfinder`, `/pr`, or none). Set each state with `save_issue` the moment its trigger happens:

1. **Start**, the session's first write, before reading code or claiming anything else: `state: "In Progress"` and
   `assignee: "me"`.
2. **PR opened** (ready for review): the PR title starts with `TM-<n>` and the body carries `Closes TM-<n>`; then
   `state: "In Review"`. A draft PR leaves the issue In Progress until it is marked ready.
3. **Merged**: Linear's GitHub integration moves the issue to `Done` through `Closes TM-<n>`. A session that sees the
   merge with the issue still In Review sets `state: "Done"` itself.

An issue resolved without a PR (a wayfinder decision ticket) goes from In Progress straight to `Done`.

### Triage labels

The default five: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
