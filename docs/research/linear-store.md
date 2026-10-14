# How the read-only board reads a Linear store

Research for Linear TM-9 (map TM-1). Question: ticketmaster has GitHub, GitLab and Gitea providers but none
for Linear. Which API does a read-only board use to read a wayfinder map in Linear, how does it find the map,
its sub-issues, `blockedBy`, assignees and Done/Canceled, how is it configured, can fabric's Linear backend be
reused, and what do rate limits mean for polling?

Researched 2026-10-14. Sources are listed at the end; each claim names the source it rests on.

## Answer in short

Use Linear's GraphQL API (`https://api.linear.app/graphql`) from Python's standard library with a personal API
key read from an environment variable (`LINEAR_API_KEY`), exactly as fabric's Linear backend already does. One
query per refresh returns the map plus every child with its state type, assignee, labels, sub-issue order and
incoming `blocks` relations, at roughly 2,000-2,500 complexity points, well under the 10,000-point per-query cap.
Reusing the `linear-server` MCP's OAuth session from a standalone process is not a supported path. Configure it
explicitly in `.ticketmaster.toml` (`provider = "linear"` plus the team key); the git remote cannot reveal it.
Poll at about 30 s, not the board's 5 s default, because the API-key budget is 2,500 requests per hour per user,
shared with every other board, agent and script using that user's key.

## 1. Which API

### Linear GraphQL with a personal API key (recommended)

- Endpoint `https://api.linear.app/graphql`. A personal API key goes in `Authorization: <API_KEY>` (no `Bearer`);
  OAuth access tokens go in `Authorization: Bearer <ACCESS_TOKEN>`. Keys are created under Settings, Security &
  access. [Linear GraphQL docs]
- A key can be created with only the Read permission, which suits a read-only board. [Linear MCP docs, which
  describe restricted read-only API keys]
- No SDK is needed: fabric's `LinearHTTP` is about 40 lines of `urllib` (POST JSON, refuse redirects, only send
  the key to `api.linear.app`, surface `errors[].message`). [fabric `bin/fabric-store` 838-882]
- This mirrors how ticketmaster's GitHub provider works today (GraphQL documents plus a small transport), except
  that the transport is HTTP instead of the `gh` CLI. [`ticketmaster/providers/github.py`]

### Reusing the `linear-server` MCP's OAuth (not recommended)

- The Linear MCP server (`https://mcp.linear.app/mcp`, read-only variant `https://mcp.linear.app/mcp/readonly`)
  authenticates with OAuth 2.1 and dynamic client registration; it also accepts an OAuth token or an API key
  directly in `Authorization: Bearer`. [Linear MCP docs]
- The token Claude Code obtained through `/mcp` belongs to Claude Code's own dynamically registered client and is
  kept in Claude Code's private credential store. Nothing documents reading it from another process, and access
  tokens expire after 24 h with rotating refresh tokens [Linear OAuth docs], so a second reader would race Claude
  Code's refresh. Treat it as not possible in a supported way.
- What is possible: ticketmaster could be its own MCP client of `mcp.linear.app` (its own OAuth 2.1 + DCR + PKCE
  login with a localhost callback), or its own Linear OAuth app against the GraphQL API (Linear supports PKCE
  without a client secret, 24 h access tokens, refresh tokens, scope `read`) [Linear OAuth docs]. Both need a
  browser login flow and token storage in ticketmaster. The MCP route also swaps one GraphQL query for many tool
  calls (`list_issues` then `get_issue` with relations per child), whose output shape is not a versioned API.
- The only thing the API-key route costs the user is creating a key once. That matches fabric's choice.

## 2. Finding the map, children, blocking, assignee and state

All field names below are from Linear's published schema (`packages/sdk/src/schema.graphql` in `linear/linear`).

| Board needs | Linear field | Notes |
|---|---|---|
| The map | `issues(filter: {team: {key: {eq: "TM"}}, number: {eq: 1}})`, or labels filter `{labels: {name: {eq: "wayfinder:map"}}}` to list maps | Fabric looks issues up by team key + number this way rather than `issue(id: "TM-1")`. `number` is a `Float` scoped to the team; `identifier` is `TM-1`. |
| Child decision tickets | `Issue.children(first, after, filter: IssueFilter, orderBy)` | Native sub-issues; the wayfinder convention in `docs/agents/issue-tracker.md` makes every child a sub-issue of the map. |
| Map order | `Issue.subIssueSortOrder: Float` ("order of the item in the sub-issue list; only set if the issue has a parent") | Sort children by it, then `number`, matching the frontier rule in `docs/agents/issue-tracker.md`. |
| Type | `labels { nodes { name } }` = `wayfinder:research` / `prototype` / `grilling` / `task` | Labels are workspace labels in this setup. |
| Blocked by | `Issue.inverseRelations { nodes { type issue { identifier number state { type } } } }`, keep `type == "blocks"` | `IssueRelation` is directional: `issue` blocks `relatedIssue`. The issues blocking X are X's inverse relations of type `blocks`; their `issue` is the blocker. `IssueRelationType` is `blocks`, `duplicate`, `related`, `similar`. Neither `relations` nor `inverseRelations` takes a filter, so filter client-side. |
| Open blocker | blocker's `state.type` not in `completed`, `canceled` (and `duplicate`) | Same rule as `docs/agents/issue-tracker.md`: unblocked when every blocker is completed or canceled. |
| Claim | `Issue.assignee: User` (null when unassigned) | One assignee only. `Issue.delegate` is the AI agent delegated to the issue, separate from the assignee. |
| Done / Canceled | `state { name type }`; `WorkflowState.type` is one of `triage`, `backlog`, `unstarted`, `started`, `completed`, `canceled`, `duplicate` | Use `type`, never the team-renamable `name`. `completedAt` / `canceledAt` give the close time for `done_days`. |
| Link | `Issue.url` | |

Fabric reads blocking the same way: its fragment asks for `inverseRelations { nodes { id type issue { id number } } }`
and maps `blocks` relations between siblings to `blocked_by`. [fabric `bin/fabric-store` 689-766]

Mapping onto ticketmaster's provider-neutral `Ticket` [`ticketmaster/model.py`]: `number` = Linear `number`,
`is_open` = state type not completed/canceled/duplicate, `not_planned` = canceled or duplicate,
`closed_at` = `completedAt` or `canceledAt`, `assignees` = the one assignee, `blocked_by` = open blockers as
`TM-<n>` (the existing `#<n>` rendering does not fit Linear identifiers), `order` = rank by `subIssueSortOrder`,
`parent_number` = the map's number.

### One query per refresh

```graphql
query Map($team: String!, $number: Float!) {
  issues(first: 1, filter: {team: {key: {eq: $team}}, number: {eq: $number}}) {
    nodes {
      id identifier number title url updatedAt
      state { type }
      labels(first: 10) { nodes { name } }
      children(first: 50) {
        pageInfo { hasNextPage endCursor }
        nodes {
          identifier number title url subIssueSortOrder completedAt canceledAt
          state { name type }
          assignee { id displayName }
          labels(first: 10) { nodes { name } }
          inverseRelations(first: 10) { nodes { type issue { identifier number state { type } } } }
        }
      }
    }
  }
}
```

Complexity, using Linear's rule (scalar 0.1, object 1, a connection multiplies its children by `first`, default
50) [Linear rate-limiting docs]: one child is about 0.7 (scalars) + 2.2 (state) + 1.2 (assignee) + 11 (labels) +
33 (relations) = about 48, so 50 children are about 2,400 points plus a few for the map. That is under the
10,000-point single-query cap with room to spare. A map with more than 50 children, or a child with more than 10
inverse relations, needs a follow-up page (`after`); fabric pages every connection this way and stops on a repeated
cursor. [fabric `bin/fabric-store` 722-739]

Listing maps (for a picker or the no-parent board) is one more query:
`issues(filter: {team: {key: {eq: $team}}, labels: {name: {eq: "wayfinder:map"}}})`.

## 3. Rate limits and polling

[Linear rate-limiting docs]

| Auth | Requests / hour | Complexity points / hour |
|---|---|---|
| API key | 2,500 per user | 3,000,000 per user |
| OAuth app | 5,000 per user/app user | 2,000,000 per user/app user |
| Unauthenticated | 600 per IP | 100,000 per IP |

- Single query cap: 10,000 points.
- A refused call is HTTP 400 with `extensions.code == "RATELIMITED"`, not 429. Ticketmaster's `RATE_LIMIT`
  regex (`rate.?limit|too many requests|429`) would only catch it if the message text says so; the Linear
  provider must check the error code. [`ticketmaster/providers/__init__.py`]
- Headers tell how much is left: `X-RateLimit-Requests-Remaining/Reset`, `X-Complexity`,
  `X-RateLimit-Complexity-Remaining/Reset`, plus endpoint-specific `X-RateLimit-Endpoint-*`. These map onto
  `RateLimited.limit` / `resets_at` the way GitHub's `rate_limit` endpoint does today.
- Linear says to avoid polling and use webhooks. A local TUI has no public endpoint for webhooks, so polling is
  the realistic option.
- The board's default refresh is 5 s [`ticketmaster/cli.py`], which is 720 requests/hour for one board: under the
  key's 2,500, but the budget is per user and shared with other boards, the user's agents (fabric with an API key)
  and scripts. At about 2,400 points per poll, 5 s also costs about 1.7M of the 3M points/hour. Fabric polls Linear
  every 30 s for this reason [fabric `board/fabric-board` 7-8, 44]. At 30 s one board costs 120 requests and about
  300k points per hour.
- A cheaper change probe (for example the newest `updatedAt` among the map's children) could make 5 s polls cheap,
  but whether adding or removing a blocking relation bumps an issue's `updatedAt` is not documented; this needs a
  test against a real workspace before relying on it.
- The MCP server's OAuth session (if it were reusable) would be on the OAuth row, but the MCP's own calls from
  Claude sessions would share it.

## 4. Configuration and detection

- Today `resolve_provider` picks the tracker from flags, then `.ticketmaster.toml` (`provider`, `repo`, `host`,
  `remote`), then the `origin` remote's host. [`ticketmaster/config.py`] For this repo the remote is GitHub while
  issues live in Linear, so host detection picks the wrong tracker; Linear has no host or repo path to detect.
- Explicit config fits the existing shape: `.ticketmaster.toml` with `provider = "linear"` and the team key
  (`TM`), plus optionally the name of the env var holding the key (fabric's `linear_key_env`, restricted to names
  starting `LINEAR_` so no other secret is sent to Linear). [fabric `bin/fabric-store` 715-716, 885-900]
- `docs/agents/issue-tracker.md` already says "Issue tracker: Linear" and "team **Ticketmaster** (key `TM`)".
  Ticketmaster owns that file's "Wayfinding operations" section (`ticketmaster setup` writes it per tracker and the
  board detects drift) [`ticketmaster/wayfinding.py`], but the heading and the team line are free prose written by
  the setup-matt-pocock-skills flow, not by ticketmaster. Parsing them works as a hint (for example to print "this
  repo tracks issues in Linear team TM; add provider = \"linear\" to .ticketmaster.toml") but is brittle as the
  sole source.
- `wayfinding.TRACKER_NAMES` and the setup templates have no Linear entry yet; a Linear provider would need one,
  and its Wayfinding operations text already exists in this repo's `docs/agents/issue-tracker.md`.

## 5. Can fabric's Linear backend be reused?

- `~/.fabric/src` no longer exists on this machine, so the `fabric-14-*` branches could not be listed with git.
  The Linear backend that came out of that work (`fabric-14-7-fabric-store-linear-team-and-mixed-store` and
  others, per Claude project directories) is present in the checkout at
  `~/.herdr/worktrees/src/fabric-35-reshape-fabric-around-the-wayfinder` (its `.git` points at the missing
  `~/.fabric/src`), which was read directly.
- Reusable as code to copy (not import): `LinearHTTP` transport (key-only-to-api.linear.app, no redirects),
  `check_key_env`, the paging loop with repeated-cursor guard, the complexity budgeting comment and constants, the
  `inverseRelations`-for-blockers mapping, and `tests/fake_linear.py` (a fake that answers fabric's operations by
  name, 685 lines) as a model for a test fake.
- Not reusable as is: `LinearBackend` models fabric stories and items (a `fabric` label, a fenced `fabric` block in
  the description, fabric statuses mapped onto workflow-state names, a local `.linear-cache.json`) and includes
  writes (claims, comments, relation create/delete). Fabric-store is a single script loaded by path
  (`SourceFileLoader`), not a package, so importing it into ticketmaster would couple the two installs.
- Fabric matches workflow states by name with per-repo overrides (`linear_state_<status>`); a read-only board only
  needs `state.type`, which avoids that configuration entirely.

## Sources

- Linear GraphQL docs (endpoint, auth headers, key creation): https://linear.app/developers/graphql
- Linear rate limiting (request and complexity limits, headers, `RATELIMITED`, complexity rule, polling advice):
  https://linear.app/developers/rate-limiting
- Linear pagination (default 50, `createdAt`/`updatedAt` ordering): https://linear.app/developers/pagination
- Linear OAuth 2.0 (PKCE, 24 h tokens, refresh, scopes): https://linear.app/developers/oauth-2-0-authentication
- Linear MCP server (OAuth 2.1 + DCR, Bearer API key/token, read-only endpoint): https://linear.app/docs/mcp
- Linear GraphQL schema: https://raw.githubusercontent.com/linear/linear/master/packages/sdk/src/schema.graphql
  (`type Issue`: `assignee`, `children`, `inverseRelations`, `subIssueSortOrder`, `state`, `completedAt`,
  `canceledAt`, `delegate`; `type IssueRelation`; `enum IssueRelationType`; `type WorkflowState.type`)
- Ticketmaster: `ticketmaster/config.py`, `ticketmaster/providers/__init__.py`, `ticketmaster/providers/github.py`,
  `ticketmaster/model.py`, `ticketmaster/wayfinding.py`, `ticketmaster/cli.py`, `docs/agents/issue-tracker.md`
- Fabric: `~/.herdr/worktrees/src/fabric-35-reshape-fabric-around-the-wayfinder/bin/fabric-store` (Linear backend,
  lines 687-1430), `board/fabric-board` (30 s Linear refresh), `tests/fake_linear.py`
