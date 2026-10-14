# Issue tracker: Linear

Issues and specs for this repo live in Linear, team **Ticketmaster** (key `TM`, so issues read `TM-12`).
Use the `linear-server` MCP tools (`https://mcp.linear.app/mcp`, user scope; authenticate once with `/mcp`).
Code, branches and pull requests stay on GitHub (`kareblak/ticketmaster`); only tracking moved.

Issues created before the move are GitHub issues in `kareblak/ticketmaster`; each one that moved was closed with a link
to its Linear copy.

## Conventions

- **Create an issue**: `save_issue` with `team: "Ticketmaster"`, `title`, `description` (Markdown), and `labels` by name.
- **Read an issue**: `get_issue` with `id: "TM-12"` and `includeRelations: true`; comments with `list_comments` and `issueId`.
- **List issues**: `list_issues` with `team: "Ticketmaster"`, filtered by `state`, `label`, `assignee` or `parentId`.
- **Comment on an issue**: `save_comment` with `issueId` and `body`.
- **Apply / remove labels**: `save_issue` with `id` and `addLabels` / `removeLabels`. Never pass `labels` on an update:
  it replaces the whole set.
- **Start, PR opened, merged**: the states follow the work, see "Issue lifecycle" in `AGENTS.md`.
- **Close**: `save_issue` with `id` and a `state` of type completed (`Done`); not planned is a `state` of type canceled
  (`Canceled`), after a comment saying why.

Labels are workspace labels, so they mean the same in every team.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

A pull request links to its Linear issue when its title starts with the issue key (`TM-12`) and its body carries
`Closes TM-12`; merging it closes the issue. The branch is the issue's `gitBranchName` (from `get_issue`, e.g.
`kaareblakstad/tm-12-short-title`), so the branch links it too. A worktree made with `EnterWorktree` starts on
`worktree-<name>`, which Linear does not match: rename it before the first push with `git branch -m <gitBranchName>`.
The link shows on the issue in Linear's UI, but not in the MCP's `attachments`, so do not judge it from there. Once the
PR merges, `get_issue` must show the issue Done; if it does not, set `state: "Done"` with `save_issue`.

## When a skill says "publish to the issue tracker"

Create a Linear issue in team Ticketmaster with `save_issue`.

## When a skill says "fetch the relevant ticket"

Call `get_issue` with `includeRelations: true`, then `list_comments` for its comments.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `save_issue` with
  `team: "Ticketmaster"`, `labels: ["wayfinder:map"]`.
- **Child ticket**: a Linear **sub-issue** of the map (`save_issue` with `parentId: "<map id>"`), nothing in the body.
  Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the
  driving dev.
- **Blocking**: Linear's **native blocking relation**, the canonical, UI-visible representation: `save_issue` on the
  child with `blockedBy: ["<blocker id>"]` (it appends; `removeBlockedBy` removes). A ticket is unblocked when every
  blocker is in a completed or canceled state.
- **Frontier query**: `list_issues` with `parentId: "<map id>"`, keep the open ones (state type not completed or
  canceled) without an assignee, then drop any whose `get_issue` (`includeRelations: true`) shows an open blocker.
  First in map order (sub-issue sort order, then issue number) wins.
- **Claim**: `save_issue` with `id` and `assignee: "me"`, the session's first write.
- **Resolve**: `save_comment` with the answer, then `save_issue` with `state: "Done"`, then append a context pointer
  (gist + link) to the map's Decisions-so-far with `save_issue` and a `patch` on the map's description.
- **Out of scope**: comment why, set `state: "Canceled"`, and add one line to the map's Out of scope section.
