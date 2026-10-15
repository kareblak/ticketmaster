"""GitHub via `gh api graphql`: native blockedBy plus `Blocked by:` body lines, sub-issues and closing PRs.

The board is every open `wayfinder:map`, and every closed one an open spec still names, and, per map, its sub-issues in order, the typed tickets that link to
it only by a `Part of #<map>` line, the spec that links to it that way and the spec's implementation tickets:
its sub-issues and the issues whose `## Parent` section names it. Body links show up as cross-references in
the linked issue's timeline.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

from ..model import Ticket, parse_time, part_of
from . import (LIMIT, OPEN_MAPS, IssueStates, NotFound, ProviderError, Query, Quota, RateLimited, as_spec,
               blocker_ref, capped, closed_map_refs, is_spec, lane_cut, names_parent, resolve_maps, run_cli, run_json,
               tracker_error, under_spec, with_body_blockers)

MAP_LABEL = "wayfinder:map"

ISSUE_FIELDS = """
  number title url body state stateReason closedAt
  author { login }
  assignees(first: 10) { nodes { login } }
  labels(first: 30) { nodes { name } }
  blockedBy(first: 20) { nodes { number state repository { nameWithOwner } } }
  closedByPullRequestsReferences(first: 10, includeClosedPrs: false) { nodes { state } }
  parent { number url labels(first: 20) { nodes { name } } }
"""

# Every query asks what it cost and what is left; the field costs no points itself.
RATE_LIMIT = "rateLimit { cost remaining resetAt }"

# A search hit carries only what picks the hits worth a full fetch: about 1 point a page.
LEAN_FIELDS = "number body labels(first: 30) { nodes { name } }"

SEARCH = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on Issue { %s } }
  }
  %s
}
""" % (LEAN_FIELDS, RATE_LIMIT)

# An issue's sub-issues and the issues of any repo cross-referencing it, a page each, with the paging arguments.
CONNECTIONS = """
  subIssues(first: 100%s)%s {
    pageInfo { hasNextPage endCursor }
    nodes { %s }
  }
  timelineItems(first: 100%s, itemTypes: [CROSS_REFERENCED_EVENT])%s {
    pageInfo { hasNextPage endCursor }
    nodes { ... on CrossReferencedEvent { source { ... on Issue { repository { nameWithOwner } %s } } } }
  }
"""

CHILDREN = """
query($owner: String!, $name: String!, $number: Int!, $subs: String, $refs: String,
      $withSubs: Boolean!, $withRefs: Boolean!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { %s } }
  %s
}
""" % (CONNECTIONS % (", after: $subs", " @include(if: $withSubs)", ISSUE_FIELDS,
                      ", after: $refs", " @include(if: $withRefs)", ISSUE_FIELDS), RATE_LIMIT)

# The first page of each, for a batch of issues.
FIRST_PAGES = CONNECTIONS % ("", "", ISSUE_FIELDS, "", "", ISSUE_FIELDS)

# Issues whose children one batched query reads: each costs about 2 points, and GitHub refuses a query that could
# return more than 500,000 nodes, about 27 issues' worth of children.
BATCH = 25

ISSUE = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { url body labels(first: 30) { nodes { name } } } }
  %s
}
""" % RATE_LIMIT

SUB_ISSUES = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { subIssues(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { number } } }
  }
  %s
}
""" % RATE_LIMIT

VIEWER = "query { viewer { login } %s }" % RATE_LIMIT


# An issue's sub-issues, the issues of this repo referring to it, and whether either went past the fetch limit.
Children = tuple[list[dict], list[dict], bool]


def aliased(selections: list[str]) -> str:
    """One query asking for each of `selections` (`alias: field { … }`) in the repo."""
    fields = " ".join(selections)
    return (f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} "
            f"{RATE_LIMIT} }}")


class GitHubProvider:
    name = "github"

    def __init__(self, repo: str, host: str = "github.com"):
        self.repo = repo
        self.host = host
        self.states = IssueStates(self._issue_states)
        self.quota: Quota | None = None  # the last fetch's cost and the points left after it
        self._spent: int | None = None  # the points spent so far by the fetch under way
        self._seen: tuple[int | None, datetime | None] = (None, None)  # the latest points left and reset time
        self.cut_off: list[str] = []

    def fetch(self, query: Query) -> list[Ticket]:
        """Every open map, every closed map an open spec's `Part of #<map>` names, and their children; the
        sub-issue parent is the native child→map link, `Part of #<map>` the fallback.

        The searches read lean fields, the maps get one full fetch together, and the children of every map and of
        every spec one batched query; `quota` then holds what the calls cost. The map search and each map's
        tickets stop at the query's limit. The `Part of` search, which only finds closed maps, keeps the default
        cap: nearly every ticket says `Part of`, so a low limit would cut it on every fetch.
        """
        self._spent = 0
        try:
            return self._fetch(query)
        finally:
            self.quota = Quota(self._spent, *self._seen)
            self._spent = None

    def _fetch(self, query: Query) -> list[Ticket]:
        limit, cut = query.limit, []
        q = f'repo:{self.repo} is:issue is:open label:"{MAP_LABEL}"'
        hits = capped(self._paginate(SEARCH, {"q": q}, ["search"], limit + 1), limit, OPEN_MAPS, cut)
        found = [lean_ticket(node) for node in hits]
        q = f'repo:{self.repo} is:issue is:open in:body "Part of"'
        specs = [lean_ticket(node) for node in self._paginate(SEARCH, {"q": q}, ["search"], LIMIT)]
        open_maps = [t.number for t in found]
        wanted = closed_map_refs(specs, set(open_maps))
        if query.named_map is not None and query.named_map not in set(open_maps) | set(wanted):
            wanted.append(query.named_map)
        full = self._issues(open_maps + wanted)
        maps = [to_ticket(full[n], order=0, repo=self.repo) for n in open_maps if n in full]
        maps += [t for n in wanted if n in full and (t := to_ticket(full[n], order=0, repo=self.repo)).is_wayfinder_map]
        numbers = {m.number for m in maps}
        candidates = [s.number for s in specs
                      if s.number not in numbers and part_of(s.body) in numbers and is_spec(s, part_of(s.body))]
        children = self._children([m.number for m in maps] + candidates, limit)
        # A spec the open `Part of` search missed (a closed one) has its children read in one more batch.
        specs_of = [t.number for m in maps for t in self._map_family(m, children, {m.number}) if is_spec(t, m.number)]
        children |= self._children([n for n in specs_of if n not in children], limit)
        tickets = list(maps)
        for map_ticket in maps:
            tickets += self._map_with_specs(map_ticket, children, {t.number for t in tickets}, limit, cut)
        tickets = with_body_blockers(resolve_maps(tickets, self._issue), self.states)
        self.cut_off = cut
        return tickets

    def map_order(self, number: int) -> list[int]:
        """The map's sub-issue numbers in the order set on GitHub; search results carry no such order."""
        owner, name = self.repo.split("/", 1)
        nodes = self._paginate(SUB_ISSUES, {"owner": owner, "name": name, "number": number},
                               ["repository", "issue", "subIssues"], limit=1000)
        return [node["number"] for node in nodes]

    def explain(self, error: RateLimited) -> RateLimited:
        """Which limit GitHub hit: an hourly quota at 0, with its reset, else the per-minute secondary limit.

        The `rate_limit` endpoint costs no quota; when it fails too, the tracker's message is all there is.
        """
        try:
            resources = run_json(["gh", "api", "rate_limit", "--hostname", self.host])["resources"]
        except (ProviderError, KeyError, TypeError):
            return error
        for key, name in (("graphql", "GraphQL"), ("core", "REST")):
            quota = resources.get(key) or {}
            if quota.get("remaining") == 0 and quota.get("reset"):
                return error.hit(f"hourly {name} quota", datetime.fromtimestamp(quota["reset"], timezone.utc))
        return error.hit("secondary limit", hint="too many calls per minute; every board and agent on the account shares it")

    def viewer(self) -> str | None:
        return (self._graphql(VIEWER, {}).get("viewer") or {}).get("login")

    def _issue_states(self, numbers: list[int]) -> dict[int, bool]:
        """One query for every number; a number that is no issue or PR answers NOT_FOUND, and is left out."""
        state = "... on Issue { state } ... on PullRequest { state }"
        found = self._aliased([f"i{n}: issueOrPullRequest(number: {n}) {{ {state} }}" for n in numbers])
        return {n: found[f"i{n}"]["state"] == "OPEN" for n in numbers if found.get(f"i{n}")}

    def _issues(self, numbers: list[int]) -> dict[int, dict]:
        """The issues by number, all fields, in one query; a number that is no issue is left out."""
        if not numbers:
            return {}
        found = self._aliased([f"i{n}: issue(number: {n}) {{ {ISSUE_FIELDS} }}" for n in dict.fromkeys(numbers)])
        return {n: found[f"i{n}"] for n in numbers if found.get(f"i{n}")}

    def _aliased(self, selections: list[str]) -> dict:
        """The repository's answer to `selections`, where an alias that names no issue is null."""
        owner, name = self.repo.split("/", 1)
        found = self._graphql(aliased(selections), {"owner": owner, "name": name}, partial=True).get("repository")
        if not found:
            raise ProviderError(f"{self.repo} not found")
        return found

    def _args(self) -> list[str]:
        return ["gh", "api", "graphql", "--hostname", self.host, "-H", "Content-Type: application/json", "--input", "-"]

    def _map_family(self, map_ticket: Ticket, children: dict[int, Children], taken: set[int]) -> list[Ticket]:
        """The map's sub-issues in order, then the typed tickets and the spec linked by `Part of #<map>`."""
        number = map_ticket.number
        subs, refs, _ = children[number]
        tickets = new_tickets(subs, taken, self.repo)
        tickets += new_tickets(refs, taken | {t.number for t in tickets}, self.repo,
                               lambda node: part_of(node.get("body") or "") == number)
        return [as_spec(t, map_ticket) if is_spec(t, number) else t for t in tickets]

    def _map_with_specs(self, map_ticket: Ticket, children: dict[int, Children], taken: set[int], limit: int,
                        cut: list[str]) -> list[Ticket]:
        """The map's family, then each spec's implementation tickets: its sub-issues and the issues whose
        `## Parent` section names it; `order` is that position. The first `limit` of them; a map with more, or with
        sub-issues or references (its own or its specs') past `limit`, goes on `cut`."""
        tickets = self._map_family(map_ticket, children, taken)
        more = children[map_ticket.number][2]
        def seen() -> set[int]:
            return taken | {t.number for t in tickets}

        for spec in [t for t in tickets if is_spec(t, map_ticket.number)]:
            subs, refs, spec_more = children[spec.number]
            more = more or spec_more
            tickets += under_spec(new_tickets(subs, seen(), self.repo), spec)
            tickets += under_spec(new_tickets(refs, seen(), self.repo,
                                              lambda node: names_parent(node.get("body"), spec.number)), spec)
        if more or len(tickets) > limit:
            cut.append(lane_cut(map_ticket))
        return [replace(t, order=index) for index, t in enumerate(tickets[:limit])]

    def _children(self, numbers: list[int], limit: int) -> dict[int, Children]:
        """Each issue's first `limit` sub-issues and the issues of this repo among its first `limit` references, by
        number: the first pages of `BATCH` issues per query, then any further page issue by issue."""
        children: dict[int, Children] = {}
        numbers = list(dict.fromkeys(numbers))
        for start in range(0, len(numbers), BATCH):
            chunk = numbers[start:start + BATCH]
            found = self._aliased([f"c{n}: issue(number: {n}) {{ {FIRST_PAGES} }}" for n in chunk])
            for n in chunk:
                if not found.get(f"c{n}"):
                    raise ProviderError(f"issue #{n} not found in {self.repo}")
                children[n] = self._rest_of(n, found[f"c{n}"], limit)
        return children

    def _rest_of(self, number: int, issue: dict, limit: int) -> Children:
        """The issue's sub-issues and this repo's issues referring to it, reading the pages after `issue`'s first,
        up to `limit` of each, and whether either went past `limit`."""
        owner, name = self.repo.split("/", 1)
        found: dict[str, list[dict]] = {"subIssues": [], "timelineItems": []}
        after: dict[str, str | None] = {"subIssues": None, "timelineItems": None}
        todo = set(found)
        more = False
        while True:
            for connection in list(todo):
                page = issue[connection]
                found[connection] += [n for n in page["nodes"] if n]
                after[connection] = page["pageInfo"].get("endCursor")
                has_next, count = page["pageInfo"]["hasNextPage"], len(found[connection])
                more = more or count > limit or (has_next and count >= limit)
                if not has_next or count >= limit:
                    todo.discard(connection)
            if not todo:
                break
            variables = {"owner": owner, "name": name, "number": number,
                         "subs": after["subIssues"], "refs": after["timelineItems"],
                         "withSubs": "subIssues" in todo, "withRefs": "timelineItems" in todo}
            issue = (self._graphql(CHILDREN, variables).get("repository") or {}).get("issue")
            if issue is None:
                raise ProviderError(f"issue #{number} not found in {self.repo}")
        sources = [event.get("source") or {} for event in found["timelineItems"]]
        refs = {s["number"]: s for s in sources if s.get("number")
                and (s.get("repository") or {}).get("nameWithOwner", "").lower() == self.repo.lower()}
        return found["subIssues"][:limit], list(refs.values())[:limit], more

    def _issue(self, number: int) -> Ticket | None:
        owner, name = self.repo.split("/", 1)
        try:
            data = self._graphql(ISSUE, {"owner": owner, "name": name, "number": number})
        except NotFound:
            return None
        issue = (data.get("repository") or {}).get("issue")
        if not issue:
            return None
        return Ticket(number=number, title="", url=issue["url"], is_open=True, body=issue.get("body") or "",
                      labels=tuple(label["name"] for label in issue["labels"]["nodes"]))

    def _graphql(self, gql: str, variables: dict, partial: bool = False) -> dict:
        """The query's data, noting what it cost. With `partial`, NOT_FOUND errors (an alias naming no issue) are
        no failure: the alias is null, and `gh`'s failing exit for them is read past."""
        try:
            data = json.loads(run_cli(self._args(), stdin=json.dumps({"query": gql, "variables": variables}),
                                      check=not partial))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"`gh` returned non-JSON output: {exc}") from exc
        if "data" not in data and not data.get("errors"):  # a refusal before GraphQL ran: 401, 403, a rate limit
            raise tracker_error(data.get("message") or f"`gh` answered no data: {json.dumps(data)[:200]}")
        self._note((data.get("data") or {}).get("rateLimit"))
        errors = [e for e in data.get("errors") or [] if not (partial and e.get("type") == "NOT_FOUND")]
        if errors:
            message = "; ".join(e.get("message", "") for e in errors)
            if all(e.get("type") == "NOT_FOUND" for e in errors):
                raise NotFound(message)
            raise tracker_error(message)
        return data.get("data") or {}

    def _note(self, rate: dict | None) -> None:
        """Adds a call's cost to the fetch under way, and keeps the points left and the reset time it reports."""
        if not rate:
            return
        if self._spent is not None:
            self._spent += rate.get("cost") or 0
        self._seen = (rate.get("remaining"), parse_time(rate.get("resetAt")))

    def _paginate(self, gql: str, variables: dict, path: list[str], limit: int) -> list[dict]:
        nodes: list[dict] = []
        after = None
        while len(nodes) < limit:
            page = self._graphql(gql, {**variables, "after": after})
            for key in path:
                page = (page or {}).get(key)
            if page is None:
                raise ProviderError(f"{'/'.join(path)} not found in {self.repo}")
            nodes += [n for n in page["nodes"] if n]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return nodes[:limit]


def new_tickets(nodes: list[dict], taken: set[int], repo: str, links=lambda node: True) -> list[Ticket]:
    """The nodes not `taken` that `links` accepts, as tickets."""
    return [to_ticket(node, order=0, repo=repo) for node in nodes if node["number"] not in taken and links(node)]


def lean_ticket(node: dict) -> Ticket:
    """An open search hit, from its lean fields."""
    return Ticket(number=node["number"], title="", url="", is_open=True, body=node.get("body") or "",
                  labels=tuple(label["name"] for label in (node.get("labels") or {}).get("nodes", [])))


def to_ticket(node: dict, order: int, repo: str = "") -> Ticket:
    prs = node.get("closedByPullRequestsReferences", {}).get("nodes", [])
    deps = node.get("blockedBy")
    blocked_by = tuple(blocker_ref(b["number"], (b.get("repository") or {}).get("nameWithOwner", ""), repo)
                       for b in (deps or {}).get("nodes") or [] if b and b.get("state") == "OPEN")
    parent = node.get("parent") or {}
    return Ticket(
        number=node["number"],
        title=node["title"],
        url=node["url"],
        body=node.get("body") or "",
        is_open=node["state"] == "OPEN",
        closed_at=parse_time(node.get("closedAt")),
        not_planned=node.get("stateReason") == "NOT_PLANNED",
        author=(node.get("author") or {}).get("login", ""),
        assignees=tuple(a["login"] for a in node["assignees"]["nodes"]),
        labels=tuple(label["name"] for label in node["labels"]["nodes"]),
        open_blockers=None if deps is None else len(blocked_by),
        blocked_by=blocked_by,
        has_open_change_request=any(pr.get("state") == "OPEN" for pr in prs),
        has_merged_change_request=any(pr.get("state") == "MERGED" for pr in prs),
        order=order,
        parent_number=parent.get("number"),
        parent_url=parent.get("url"),
        parent_labels=tuple(label["name"] for label in (parent.get("labels") or {}).get("nodes", [])),
    )
