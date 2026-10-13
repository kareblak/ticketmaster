"""GitHub via `gh api graphql`: native blockedBy, sub-issues and closing PRs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..model import Ticket, parse_time
from . import ProviderError, Query, run_cli, run_json, tracker_error

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

SUB_ISSUES = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { %s }
      }
    }
  }
}
""" % ISSUE_FIELDS


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

    def delete(self, number: int) -> None:
        run_cli(["gh", "issue", "delete", str(number), "--repo", f"{self.host}/{self.repo}", "--yes"])

    def _sub_issues(self, query: Query) -> list[Ticket]:
        owner, name = self.repo.split("/", 1)
        variables = {"owner": owner, "name": name, "number": query.parent}
        path = ["repository", "issue", "subIssues"]
        nodes = self._paginate(SUB_ISSUES, variables, path, query.limit)
        return [to_ticket(node, order=index) for index, node in enumerate(nodes)]

    def _paginate(self, gql: str, variables: dict, path: list[str], limit: int) -> list[dict]:
        nodes: list[dict] = []
        after = None
        while len(nodes) < limit:
            body = {"query": gql, "variables": {**variables, "after": after}}
            args = ["gh", "api", "graphql", "--hostname", self.host, "-H", "Content-Type: application/json", "--input", "-"]
            data = run_json(args, stdin=json.dumps(body))
            if data.get("errors"):
                raise tracker_error("; ".join(e.get("message", "") for e in data["errors"]))
            page = data["data"]
            for key in path:
                page = (page or {}).get(key)
            if page is None:
                raise ProviderError(f"{'/'.join(path)} not found in {self.repo}")
            nodes += [n for n in page["nodes"] if n]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return nodes[:limit]


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
