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
from . import (OPEN_MAPS, IssueStates, NotFound, ProviderError, Query, RateLimited, as_spec, blocker_ref, capped,
               closed_map_refs, family_cut, is_spec, names_parent, resolve_maps, run_cli, run_json, tracker_error,
               under_spec, with_body_blockers)

MAP_LABEL = "wayfinder:map"
OPEN_SPECS = "open specs"  # the `Part of` search that finds closed maps with an open spec

ISSUE_FIELDS = """
  number title url body state stateReason closedAt
  author { login }
  assignees(first: 10) { nodes { login } }
  labels(first: 30) { nodes { name } }
  blockedBy(first: 20) { nodes { number state repository { nameWithOwner } } }
  closedByPullRequestsReferences(first: 10, includeClosedPrs: false) { nodes { state } }
  parent { number url labels(first: 20) { nodes { name } } }
"""

SEARCH = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on Issue { %s } }
  }
}
""" % ISSUE_FIELDS

CHILDREN = """
query($owner: String!, $name: String!, $number: Int!, $subs: String, $refs: String,
      $withSubs: Boolean!, $withRefs: Boolean!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      url labels(first: 30) { nodes { name } }
      subIssues(first: 100, after: $subs) @include(if: $withSubs) {
        pageInfo { hasNextPage endCursor }
        nodes { %s }
      }
      timelineItems(first: 100, after: $refs, itemTypes: [CROSS_REFERENCED_EVENT]) @include(if: $withRefs) {
        pageInfo { hasNextPage endCursor }
        nodes { ... on CrossReferencedEvent { source { ... on Issue { repository { nameWithOwner } %s } } } }
      }
    }
  }
}
""" % (ISSUE_FIELDS, ISSUE_FIELDS)

NAMED = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { %s } }
}
""" % ISSUE_FIELDS

ISSUE = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { url body labels(first: 30) { nodes { name } } } }
}
"""

SUB_ISSUES = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { subIssues(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { number } } }
  }
}
"""

VIEWER = "query { viewer { login } }"


class GitHubProvider:
    name = "github"

    def __init__(self, repo: str, host: str = "github.com"):
        self.repo = repo
        self.host = host
        self.states = IssueStates(self._issue_states)
        self.cut_off: list[str] = []

    def fetch(self, query: Query) -> list[Ticket]:
        """Every open map, every closed map an open spec's `Part of #<map>` names, and their children; the
        sub-issue parent is the native child→map link, `Part of #<map>` the fallback. Each search and each map's
        tickets stop at the query's limit."""
        limit, cut = query.limit, []

        def search(q: str, what: str) -> list[Ticket]:
            nodes = capped(self._paginate(SEARCH, {"q": q}, ["search"], limit + 1), limit, what, cut)
            return [to_ticket(node, order=0, repo=self.repo) for node in nodes]

        maps = search(f'repo:{self.repo} is:issue is:open label:"{MAP_LABEL}"', OPEN_MAPS)
        specs = search(f'repo:{self.repo} is:issue is:open in:body "Part of"', OPEN_SPECS)
        wanted = closed_map_refs(specs, {m.number for m in maps})
        if query.named_map is not None and query.named_map not in {m.number for m in maps} | set(wanted):
            wanted.append(query.named_map)
        for number in wanted:
            found = self._named(number)
            maps += [found] if found is not None and found.is_wayfinder_map else []
        tickets = list(maps)
        for map_ticket in maps:
            tickets += self._map_family(map_ticket, {t.number for t in tickets}, limit, cut)
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
        owner, name = self.repo.split("/", 1)
        state = "... on Issue { state } ... on PullRequest { state }"
        fields = " ".join(f"i{n}: issueOrPullRequest(number: {n}) {{ {state} }}" for n in numbers)
        gql = f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
        body = {"query": gql, "variables": {"owner": owner, "name": name}}
        try:
            data = json.loads(run_cli(self._args(), stdin=json.dumps(body), check=False))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"`gh` returned non-JSON output: {exc}") from exc
        errors = [e for e in data.get("errors") or [] if e.get("type") != "NOT_FOUND"]
        if errors or not (data.get("data") or {}).get("repository"):
            raise tracker_error("; ".join(e.get("message", "") for e in errors) or f"{self.repo} not found")
        found = data["data"]["repository"]
        return {n: found[f"i{n}"]["state"] == "OPEN" for n in numbers if found.get(f"i{n}")}

    def _args(self) -> list[str]:
        return ["gh", "api", "graphql", "--hostname", self.host, "-H", "Content-Type: application/json", "--input", "-"]

    def _map_family(self, map_ticket: Ticket, taken: set[int], limit: int, cut: list[str]) -> list[Ticket]:
        """The map's sub-issues in order, then the typed tickets and the spec linked by `Part of #<map>`, then the
        spec's implementation tickets; `order` is that position. The first `limit` of them; a map with more, or
        whose sub-issues or references go past `limit`, goes on `cut`."""
        tickets: list[Ticket] = []
        number = map_ticket.number

        def new(nodes: list[dict], links=lambda node: True) -> list[Ticket]:
            seen = taken | {t.number for t in tickets}
            return [to_ticket(node, order=0, repo=self.repo) for node in nodes if node["number"] not in seen and links(node)]

        _, subs, refs, more = self._children(number, limit)
        tickets += new(subs)
        tickets += new(refs, lambda node: part_of(node.get("body") or "") == number)
        tickets = [as_spec(t, map_ticket) if is_spec(t, number) else t for t in tickets]
        for spec in [t for t in tickets if is_spec(t, number)]:
            _, subs, refs, spec_more = self._children(spec.number, limit)
            more = more or spec_more
            tickets += under_spec(new(subs), spec)
            tickets += under_spec(new(refs, lambda node: names_parent(node.get("body"), spec.number)), spec)
        if more or len(tickets) > limit:
            cut.append(family_cut(map_ticket))
        return [replace(t, order=index) for index, t in enumerate(tickets[:limit])]

    def _children(self, number: int, limit: int) -> tuple[Ticket, list[dict], list[dict], bool]:
        """The issue (its URL and labels), its first `limit` sub-issues, the issues of this repo among its first
        `limit` references, and whether either list went past `limit`."""
        owner, name = self.repo.split("/", 1)
        found: dict[str, list[dict]] = {"subIssues": [], "timelineItems": []}
        after: dict[str, str | None] = {"subIssues": None, "timelineItems": None}
        todo = set(found)
        more = False
        while todo:
            variables = {"owner": owner, "name": name, "number": number,
                         "subs": after["subIssues"], "refs": after["timelineItems"],
                         "withSubs": "subIssues" in todo, "withRefs": "timelineItems" in todo}
            issue = (self._graphql(CHILDREN, variables).get("repository") or {}).get("issue")
            if issue is None:
                raise ProviderError(f"issue #{number} not found in {self.repo}")
            for connection in list(todo):
                page = issue[connection]
                found[connection] += [n for n in page["nodes"] if n]
                after[connection] = page["pageInfo"].get("endCursor")
                has_next, count = page["pageInfo"]["hasNextPage"], len(found[connection])
                more = more or count > limit or (has_next and count >= limit)
                if not has_next or count >= limit:
                    todo.discard(connection)
        sources = [event.get("source") or {} for event in found["timelineItems"]]
        refs = {s["number"]: s for s in sources if s.get("number")
                and (s.get("repository") or {}).get("nameWithOwner", "").lower() == self.repo.lower()}
        labels = tuple(label["name"] for label in issue.get("labels", {}).get("nodes", []))
        parent = Ticket(number=number, title="", url=issue.get("url", ""), is_open=True, labels=labels)
        return parent, found["subIssues"][:limit], list(refs.values())[:limit], more

    def _named(self, number: int) -> Ticket | None:
        """The issue the board was named with, or None when there is no such issue."""
        owner, name = self.repo.split("/", 1)
        try:
            data = self._graphql(NAMED, {"owner": owner, "name": name, "number": number})
        except NotFound:
            return None
        issue = (data.get("repository") or {}).get("issue")
        return to_ticket(issue, order=0, repo=self.repo) if issue else None

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

    def _graphql(self, gql: str, variables: dict) -> dict:
        data = run_json(self._args(), stdin=json.dumps({"query": gql, "variables": variables}))
        if errors := data.get("errors"):
            message = "; ".join(e.get("message", "") for e in errors)
            if all(e.get("type") == "NOT_FOUND" for e in errors):
                raise NotFound(message)
            raise tracker_error(message)
        return data["data"]

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
