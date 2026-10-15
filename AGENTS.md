# AGENTS.md

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `kareblak/ticketmaster`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

#### Issue lifecycle

Every issue a session works on follows the work, whichever skill drives it (`/implement`, `/wayfinder`, `/pr`, or
none):

1. **Start**, the session's first write: `gh issue edit <n> --add-assignee @me`.
2. **PR opened**: the PR body carries `Closes #<n>`.
3. **Merged**: GitHub closes the issue through `Closes #<n>`. A session that sees the merge with the issue still open
   closes it with `gh issue close <n>`.

An issue resolved without a PR (a wayfinder decision ticket) is closed with `gh issue close <n> --comment "..."`.

### Triage labels

The default five: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
