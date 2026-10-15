# Finding standalone specs and tickets by body signature

Research for [#89](https://github.com/kareblak/ticketmaster/issues/89), on map
[#88](https://github.com/kareblak/ticketmaster/issues/88). Date: 2026-10-15.

**Question.** For each provider, how can the board find open and recently closed issues whose body carries the
spec signature (`## Problem Statement` + `## Solution`) or the ticket signature (`## What to build` +
`## Acceptance criteria`), with no label and no map link, and at what cost against the 5 s poll (30 s on Linear)?

## Answer

All four providers can do it in **one or two extra calls per refresh**. Only Linear matches the heading text
exactly; the other three only narrow the results (words, not `##` or line starts), so the exact body check that
`is_spec` and `names_parent` already do follows on every provider. No provider has a gap that rules the signature
approach out. The real constraint is GitHub's hourly GraphQL quota, which the board's current field set already
strains at a 5 s poll (see "Cost surprise" below).

| Provider | API | Heading match | Extra calls per refresh | Limit vs poll | Index lag | Spec's children |
|---|---|---|---|---|---|---|
| GitHub | GraphQL `search(type: ISSUE_ADVANCED)` with `in:body "…" OR in:body "…"` | Narrows (phrase in order, `##` ignored) | 1 for both signatures (plus pages past 100) | 5,000 pts/h; lean query 1 pt, board fields 5 pts | Search index, not documented | Sub-issue `parent` on each hit, then `## Parent` in its body |
| Linear | `issues(filter: {description: {contains: "## What to build"}, …})` | **Exact** substring, case-sensitive | 0 if folded into `TicketmasterLinked`, else 1 | 2,500 req/h (API key), 3M complexity/h | None (database filter) | `parent { number }` on each hit, then `## Parent` |
| GitLab | GraphQL `project.issues(search:, in: [DESCRIPTION])` | Narrows (PostgreSQL full text, English stemming and stop words) | 1 per signature (2) | 2,000 req/min today; proposed Free 5,000 req/h | None (written after commit) | `## Parent` in each hit's body; no issue sub-issues |
| Gitea / Forgejo | `GET /repos/{o}/{r}/issues?type=issues&q=…` | Narrows on bleve (phrase, also matches comments); case-insensitive substring on the `db` indexer | 1 per signature (2), 50 per page | None built in | bleve/ES/Meilisearch: async indexer queue; `db`: none | `## Parent` in each hit's body; no sub-issues |

**Recommendation.** Use one signature search per provider for both kinds where the provider allows OR (GitHub,
Linear), two where it does not (GitLab, Gitea), with a lean field set (number, state, body, labels, parent) and the
existing exact check on the body. Derive a spec's children from the **ticket** search results (their sub-issue
parent or `## Parent` line) instead of a per-spec children call, so cost does not grow with the number of specs.
Add a per-spec children call only for a spec's closed tickets if "recently closed" turns out too narrow for the lane
rule "leaves only once the spec and all of its tickets are closed". On GitHub, the refresh-cost item on the map is
real: either trim `ISSUE_FIELDS` on the searches or poll standalone work more slowly.

## GitHub

**API.** The board already uses GraphQL `search(type: ISSUE)` (`ticketmaster/providers/github.py:90-106`). Since
March 2025 GraphQL also has `type: ISSUE_ADVANCED`, which takes `AND`, `OR` and parentheses
([changelog](https://github.blog/changelog/2025-03-06-github-issues-projects-api-support-for-issues-advanced-search-and-more/)).

**Matching (probed against this repo, 2026-10-15).**

- With `type: ISSUE`, a quoted phrase is not order-sensitive: `in:body "build what"` returned the same 29 issues
  as `in:body "What to build"`, and `"## What to build"` the same again, so `##` is dropped.
- With `type: ISSUE_ADVANCED`, phrase order holds: `in:body "build what"` returned 0, `in:body "What to build"` 29.
- `repo:… is:issue (in:body "Problem Statement" OR in:body "What to build")` returned the union in one call.
- False positives exist: #88 and #89 matched because they quote the headings in prose. The exact body check
  (heading at a line start) is required.
- `in:body` searches the body only, not comments
  ([docs](https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests)).

**Open and recently closed.** `is:open`, or `is:closed closed:>=YYYY-MM-DD` (date qualifiers, same docs page).

**Cost.** GraphQL is 5,000 points per hour per user, a call costs at least 1 point, and the cost is the requested
connection sizes divided by 100 ([docs](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)).
Measured with `rateLimit { cost }`:

- a search with only `number state body`: **1 point**;
- the board's `SEARCH` (100 nodes with `ISSUE_FIELDS`' nested `labels`, `assignees`, `blockedBy`,
  `closedByPullRequestsReferences` and `parent.labels`): **5 points**, however few hits come back;
- the board's `CHILDREN` query for one issue: **5 points**.

GraphQL search did not move the REST `search` bucket (30 requests per minute,
[docs](https://docs.github.com/en/rest/search/search)); it is charged to the GraphQL quota only. A search returns
at most 1,000 results (same page).

**Cost surprise.** At a 5 s poll a refresh runs 720 times an hour. One extra 5-point query is 3,600 points an hour,
72 % of the quota. Today's `fetch` already issues two 5-point searches plus one 5-point `CHILDREN` call per map and
per map spec, so with one map and one spec it would need about 20 points a refresh, 14,400 an hour, about three
times the quota (arithmetic from the measured costs, not an observed board run); on a rate limit the `Pacer`
(`board.py:185`) slows ticket fetches to once a minute. Adding the signature search at
1 point (lean fields) costs 720 points an hour; adding it with `ISSUE_FIELDS` costs 3,600.

**Index lag.** GitHub does not document how fast new or edited issues reach search. The lag is a search-index
property, so a just-created spec may miss a refresh or two. `repository.issues(states: OPEN)` reads the database
instead (1 point per 100, no lag, exact body check client-side); it is the fallback if lag proves visible.

**Children.** Each ticket hit carries `parent { number }` (sub-issue parent) and its body; a ticket belongs to a
spec when the parent is the spec or its `## Parent` section names it (`names_parent`). The current per-spec route,
`_children` (sub-issues plus `CROSS_REFERENCED_EVENT`s), still works for a standalone spec at 5 points a spec.

**Gaps.** None that rule it out. The 1,000-result cap only matters for closed tickets without a date bound.

## Linear

**API.** `issues(filter: …)` with the `description` string comparator. Comparators include `contains`,
`containsIgnoreCase`, `startsWith` and the negations; `and` is the default and `or` is available
([filtering docs](https://linear.app/developers/filtering)). The board already does this for `Part of` and
`Parent` (`TicketmasterLinked`, `ticketmaster/providers/linear.py:99-103`).

**Matching (probed against team TM, 2026-10-15).** `description` holds Markdown with the headings verbatim
(`…\n\n## What to build\n\nThe developer…`). `and: [{description: {contains: "## What to build"}},
{description: {contains: "## Acceptance criteria"}}]` returned 11 issues, and the spec pair returned TM-14. The
match is an exact, case-sensitive substring, `##` included. It does not anchor to a line start, so the body check
still follows, but false positives need a literal `## What to build` in prose.

**Open and recently closed.** `state: {type: {nin: [completed, canceled, duplicate]}}` for open, and date filters
take relative ISO 8601 durations, for example `completedAt: {gt: "-P2W"}` (same docs page).

**Cost.** API key: 2,500 requests and 3,000,000 complexity points an hour; OAuth: 5,000 requests and 2,000,000
points per user an hour; at most 10,000 points per query
([rate-limiting docs](https://linear.app/developers/rate-limiting)). Complexity counts the requested `first`, so a
page of 50 with `TicketFields` is about 4,400 points (`linear.py:44-52`). At 30 s, a refresh runs 120 times an
hour: one extra page query is 120 requests (5 % of an API key's) and about 528,000 points (18 % of 3M, 26 % of an
OAuth token's 2M). Folding both signatures into `TicketmasterLinked`'s `or` list adds no request, only more matches
and possibly more pages.

**Index lag.** None: the filter runs on Linear's data, not a search index.

**Children.** `parent { number }` is already in `TicketFields`; `_linked` matches a candidate to a spec by parent or
`## Parent` (`linear.py` `_linked`). The same works for standalone specs.

**Gaps.** None.

## GitLab

**API.** `project.issues(search:, in: [DESCRIPTION], state:, closedAfter:, updatedAfter:)`; `search` is "Search
query for title or description", `in` defaults to `[TITLE, DESCRIPTION]`
([GraphQL reference](https://docs.gitlab.com/api/graphql/reference/)). The board already runs
`SPEC_SEARCHES` (`"Part of"`, `"Parent"`) through the same query (`ticketmaster/providers/gitlab.py:94-108`).

**Matching.** Issue search uses PostgreSQL full text on `issue_search_data`, with the `english` dictionary and
prefix terms (`to_tsquery … :*`) (`app/models/concerns/pg_full_text_searchable.rb` in
[gitlabhq](https://github.com/gitlabhq/gitlabhq/blob/master/app/models/concerns/pg_full_text_searchable.rb)). So
`##` is dropped, `what` and `to` are stop words, `statement` is stemmed and word order is ignored: the search only
narrows. The current code does not pass `in`, so titles match too; passing `in: [DESCRIPTION]` trims that.

**Open and recently closed.** `state: opened`, and `state: closed, closedAfter: <time>`.

**Cost.** GitLab.com allows 2,000 authenticated API requests per user per minute today; the proposed tier-aware
limits are 5,000 an hour sustained (100 a minute burst) on Free
([rate limits](https://docs.gitlab.com/user/gitlab_com/rate_limits/)). Two signature searches at a 5 s poll are
1,440 requests an hour. Today's fetch already makes at least eight requests a refresh (labels, map label pages,
one per ticket label, two spec searches), about 5,800 an hour, so under the proposed Free limit the board would
need a slower poll whether or not standalone work is added. The 100-per-minute search limit applies to the
`/search` API, not to project issue lists (same page).

**Index lag.** None in practice: the search vector is written in an `after_save_commit` hook in the same request
(`pg_full_text_searchable.rb`, `pg_full_text_searchable`).

**Children.** No sub-issues between issues in the Issue API (`gitlab.py` module docstring); children are the ticket
hits whose `## Parent` section names the spec, as `with_specs` does for map specs.

**Gaps.** None. A signature with no matching words left after stop-word removal would be a gap, but `build`,
`acceptance`, `criteria`, `problem`, `statement` and `solution` all survive.

## Gitea and Forgejo

**API.** `GET /repos/{owner}/{repo}/issues?type=issues&state=open|closed|all&q=…&since=…`, 50 per page by default.
The board already runs `SPEC_SEARCHES` through it (`ticketmaster/providers/gitea.py:83-106`).

**Matching.** `q` goes to the issue indexer, which the admin picks: `bleve` (default), `db`, `elasticsearch` or
`meilisearch` ([config cheat sheet](https://docs.gitea.com/administration/config-cheat-sheet), `[indexer]`). The
API passes no search mode, so the indexer's first one applies:

- bleve: exact mode, a `MatchPhraseQuery` over `title`, `content` **and `comments`**, case-insensitive, `##` dropped
  by the analyzer (`modules/indexer/issues/bleve/bleve.go` in
  [go-gitea/gitea](https://github.com/go-gitea/gitea/blob/main/modules/indexer/issues/bleve/bleve.go)). A comment
  quoting a heading is a false positive.
- db: a case-insensitive `LIKE` over `issue.name`, `issue.content` and comment content
  (`modules/indexer/issues/db/db.go`), so `## What to build` matches as a substring.
- Forgejo forked the indexer and tokenizes `q`, building a `MatchPhraseQuery` per token with a per-token fuzzy flag
  ([forgejo bleve.go](https://codeberg.org/forgejo/forgejo/src/branch/forgejo/modules/indexer/issues/bleve/bleve.go));
  quote each heading so it stays one phrase.

Either way the result only narrows, and one `q` holds one phrase, so a signature needs one heading per search
(`What to build`, `Problem Statement`) and the second heading is checked in the body.

**Open and recently closed.** `state=open`, and `state=closed&since=<time>` (`since` is the update time, which a
close sets).

**Cost.** Gitea has no built-in API rate limit; a reverse proxy or a hosted instance (Codeberg) may add one. Two
searches at 5 s are 1,440 requests an hour.

**Index lag.** For bleve, Elasticsearch and Meilisearch, issue updates go through the `issue_indexer` queue
(`modules/indexer/issues/indexer.go`, `CreateUniqueQueue`), so a new issue can miss the next refresh. The `db`
indexer reads the tables directly and has none.

**Children.** No sub-issues (`gitea.py` module docstring); children are the ticket hits whose `## Parent` section
names the spec.

**Gaps.** None that rule it out. An instance with the issue indexer disabled or broken returns no `q` results; the
board's map specs already depend on the same search.

## How the children question resolves everywhere

The ticket-signature search returns every ticket, standalone or not. Each hit's sub-issue parent (GitHub, Linear)
or `## Parent` line (all four) points at its spec, so the spec lanes can be built from the two signature searches
alone. What that misses:

- a spec's child that lacks the ticket signature (a hand-made sub-issue);
- a spec's tickets closed before the "recently closed" window, which the lane rule needs to know are closed. An
  open spec whose tickets are all old and closed would look ticketless. A per-open-spec children call (GitHub
  `_children`, Linear `children`) or a `## Parent` search with `state=all` covers it at one call per open spec.

## Sources

- GitHub: [searching issues](https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests),
  [REST search limits](https://docs.github.com/en/rest/search/search),
  [GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api),
  [advanced search API changelog](https://github.blog/changelog/2025-03-06-github-issues-projects-api-support-for-issues-advanced-search-and-more/);
  probes against `kareblak/ticketmaster` with `gh api graphql`.
- Linear: [filtering](https://linear.app/developers/filtering), [rate limiting](https://linear.app/developers/rate-limiting);
  probes against team TM.
- GitLab: [GraphQL reference](https://docs.gitlab.com/api/graphql/reference/),
  [GitLab.com rate limits](https://docs.gitlab.com/user/gitlab_com/rate_limits/),
  [`pg_full_text_searchable.rb`](https://github.com/gitlabhq/gitlabhq/blob/master/app/models/concerns/pg_full_text_searchable.rb),
  [full-text search MR !71913](https://gitlab.com/gitlab-org/gitlab/-/merge_requests/71913).
- Gitea/Forgejo: [config cheat sheet](https://docs.gitea.com/administration/config-cheat-sheet), gitea
  `modules/indexer/issues/{bleve/bleve.go,db/db.go,indexer.go}`, `routers/api/v1/repo/issue.go`; forgejo
  `modules/indexer/issues/bleve/bleve.go`.
