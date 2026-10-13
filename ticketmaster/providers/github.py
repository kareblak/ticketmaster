"""GitHub via `gh api graphql`: native blockedBy plus `Blocked by:` body lines, sub-issues and closing PRs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..model import Ticket, parse_time
from . import (IssueStates, NotFound, ProviderError, Query, blocker_ref, resolve_maps, run_cli, run_json,
               tracker_error, with_body_blockers)

ISSUE_FIELDS = """
  number title url body state closedAt
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

ISSUE = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { url labels(first: 30) { nodes { name } } } }
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
        return with_body_blockers(resolve_maps(self._fetch(query), self._map), self.states)

    def _fetch(self, query: Query) -> list[Ticket]:
        return self._sub_issues(query) if query.parent is not None else self._search(query)

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

    def delete(self, number: int) -> None:
        run_cli(["gh", "issue", "delete", str(number), "--repo", f"{self.host}/{self.repo}", "--yes"])

    def _sub_issues(self, query: Query) -> list[Ticket]:
        owner, name = self.repo.split("/", 1)
        variables = {"owner": owner, "name": name, "number": query.parent}
        path = ["repository", "issue", "subIssues"]
        nodes = self._paginate(SUB_ISSUES, variables, path, query.limit)
        return [to_ticket(node, order=index, repo=self.repo) for index, node in enumerate(nodes)]

    def _map(self, number: int) -> tuple[str, tuple[str, ...]] | None:
        owner, name = self.repo.split("/", 1)
        try:
            data = self._graphql(ISSUE, {"owner": owner, "name": name, "number": number})
        except NotFound:
            return None
        issue = (data.get("repository") or {}).get("issue")
        return (issue["url"], tuple(label["name"] for label in issue["labels"]["nodes"])) if issue else None

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
