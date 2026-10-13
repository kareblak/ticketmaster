"""GitHub via `gh api graphql`: native blockedBy, sub-issues and closing PRs.

A parent board is the parent's sub-issues, plus a spec that links to it with a `Part of #<parent>`
line and the spec's implementation tickets: its sub-issues and the issues whose `## Parent` section
names it. Body links show up as cross-references in the linked issue's timeline.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ..model import Ticket, parse_time
from . import ProviderError, Query, is_spec, names_parent, part_of, run_json, tracker_error, under

ISSUE_FIELDS = """
  number title url body state closedAt
  author { login }
  assignees(first: 10) { nodes { login } }
  labels(first: 30) { nodes { name } }
  issueDependenciesSummary { blockedBy }
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
      url labels(first: 20) { nodes { name } }
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


class GitHubProvider:
    name = "github"

    def __init__(self, repo: str, host: str = "github.com"):
        self.repo = repo
        self.host = host

    def fetch(self, query: Query) -> list[Ticket]:
        if query.parent is not None:
            return self._sub_issues(query)
        since = (datetime.now(timezone.utc) - timedelta(days=query.done_days)).date().isoformat()
        base = f"repo:{self.repo} is:issue"
        if query.label:
            base += f' label:"{query.label}"'
        searches = [f"{base} is:open", f"{base} is:closed closed:>={since}"]
        seen: dict[int, Ticket] = {}
        for q in searches:
            for node in self._paginate(SEARCH, {"q": q}, ["search"], query.limit):
                if node and node["number"] not in seen:
                    seen[node["number"]] = to_ticket(node, order=0)
        return list(seen.values())

    def _sub_issues(self, query: Query) -> list[Ticket]:
        """The parent's sub-issues in order, then each spec's implementation tickets after the spec."""
        tickets: list[Ticket] = []

        def new(nodes: list[dict], links=lambda body: True) -> list[Ticket]:
            taken = {query.parent} | {t.number for t in tickets}
            return [to_ticket(node, order=0) for node in nodes if node["number"] not in taken and links(node.get("body"))]

        parent, subs, refs = self._children(query.parent, query.limit)
        tickets += new(subs)
        tickets += under([t for t in new(refs, lambda body: part_of(body, query.parent)) if is_spec(t)], parent)
        for spec in [t for t in tickets if is_spec(t)]:
            _, subs, refs = self._children(spec.number, query.limit)
            tickets += new(subs)
            tickets += under(new(refs, lambda body: names_parent(body, spec.number)), spec)
        return [replace(t, order=index) for index, t in enumerate(tickets[: query.limit])]

    def _children(self, number: int, limit: int) -> tuple[Ticket, list[dict], list[dict]]:
        """An issue (as the parent of what links to it), its sub-issues, and the issues of this repo referring to it."""
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

    def _graphql(self, gql: str, variables: dict) -> dict:
        body = {"query": gql, "variables": variables}
        args = ["gh", "api", "graphql", "--hostname", self.host, "-H", "Content-Type: application/json", "--input", "-"]
        data = run_json(args, stdin=json.dumps(body))
        if data.get("errors"):
            raise tracker_error("; ".join(e.get("message", "") for e in data["errors"]))
        return data["data"]


def to_ticket(node: dict, order: int) -> Ticket:
    prs = node.get("closedByPullRequestsReferences", {}).get("nodes", [])
    deps = node.get("issueDependenciesSummary") or {}
    parent = node.get("parent") or {}
    return Ticket(
        number=node["number"],
        title=node["title"],
        url=node["url"],
        body=node.get("body") or "",
        is_open=node["state"] == "OPEN",
        closed_at=parse_time(node.get("closedAt")),
        author=(node.get("author") or {}).get("login", ""),
        assignees=tuple(a["login"] for a in node["assignees"]["nodes"]),
        labels=tuple(label["name"] for label in node["labels"]["nodes"]),
        open_blockers=deps.get("blockedBy"),
        has_open_change_request=any(pr.get("state") == "OPEN" for pr in prs),
        order=order,
        parent_number=parent.get("number"),
        parent_url=parent.get("url"),
        parent_labels=tuple(label["name"] for label in (parent.get("labels") or {}).get("nodes", [])),
    )
