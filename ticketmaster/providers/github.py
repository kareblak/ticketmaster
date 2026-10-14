"""GitHub via `gh api graphql`: native blockedBy plus `Blocked by:` body lines, sub-issues and closing PRs.

A parent board is the parent's sub-issues and, for a map, the spec that links to it with a
`Part of #<map>` line (a sub-issue or not) and the spec's implementation tickets: its sub-issues and
the issues whose `## Parent` section names it. Body links show up as cross-references in the linked
issue's timeline.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ..model import Ticket, parse_time
from . import (IssueStates, NotFound, ProviderError, Query, RateLimited, as_spec, blocker_ref, is_map, is_spec,
               names_parent, resolve_maps, run_cli, run_json, tracker_error, under_spec, with_body_blockers)

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

    def fetch(self, query: Query) -> list[Ticket]:
        """The sub-issue parent is the native child→map link; `Part of #<map>` is the fallback."""
        tickets, parent = self._sub_issues(query) if query.parent is not None else (self._search(query), None)
        return with_body_blockers(resolve_maps(tickets, self._issue, (parent,) if parent else ()), self.states)

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

    def _search(self, query: Query) -> list[Ticket]:
        since = (datetime.now(timezone.utc) - timedelta(days=query.done_days)).date().isoformat()
        base = f"repo:{self.repo} is:issue"
        if query.label:
            base += f' label:"{query.label}"'
        searches = [f"{base} is:open", f"{base} is:closed closed:>={since}"]
        seen: dict[int, Ticket] = {}
        for q in searches:
            for node in self._paginate(SEARCH, {"q": q}, ["search"], query.limit):
                if node and node["number"] not in seen:
                    seen[node["number"]] = to_ticket(node, order=0, repo=self.repo)
        return list(seen.values())

    def _sub_issues(self, query: Query) -> tuple[list[Ticket], Ticket]:
        """The parent's sub-issues in order; for a map, then its spec and the spec's implementation tickets.
        Also the parent itself, which is not on its own board."""
        tickets: list[Ticket] = []

        def new(nodes: list[dict], links=lambda body: True) -> list[Ticket]:
            taken = {query.parent} | {t.number for t in tickets}
            return [to_ticket(node, order=0, repo=self.repo) for node in nodes if node["number"] not in taken and links(node.get("body"))]

        found, subs, refs = self._children(query.parent, query.limit)
        tickets += new(subs)
        if is_map(found):
            tickets += [t for t in new(refs) if is_spec(t, query.parent)]
            tickets = [as_spec(t, found) if is_spec(t, query.parent) else t for t in tickets]
            for spec in [t for t in tickets if is_spec(t, query.parent)]:
                _, subs, refs = self._children(spec.number, query.limit)
                tickets += under_spec(new(subs), spec)
                tickets += under_spec(new(refs, lambda body: names_parent(body, spec.number)), spec)
        return [replace(t, order=index) for index, t in enumerate(tickets[: query.limit])], found

    def _children(self, number: int, limit: int) -> tuple[Ticket, list[dict], list[dict]]:
        """The issue (its URL and labels), its sub-issues, and the issues of this repo referring to it."""
        owner, name = self.repo.split("/", 1)
        found: dict[str, list[dict]] = {"subIssues": [], "timelineItems": []}
        after: dict[str, str | None] = {"subIssues": None, "timelineItems": None}
        todo = set(found)
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
                if not page["pageInfo"]["hasNextPage"] or len(found[connection]) >= limit:
                    todo.discard(connection)
        sources = [event.get("source") or {} for event in found["timelineItems"]]
        refs = {s["number"]: s for s in sources if s.get("number")
                and (s.get("repository") or {}).get("nameWithOwner", "").lower() == self.repo.lower()}
        labels = tuple(label["name"] for label in issue.get("labels", {}).get("nodes", []))
        parent = Ticket(number=number, title="", url=issue.get("url", ""), is_open=True, labels=labels)
        return parent, found["subIssues"][:limit], list(refs.values())[:limit]

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
        order=order,
        parent_number=parent.get("number"),
        parent_url=parent.get("url"),
        parent_labels=tuple(label["name"] for label in (parent.get("labels") or {}).get("nodes", [])),
    )
