"""GitLab via `glab api graphql`: `blocks` links plus `Blocked by:` body lines, and closing merge requests.

GitLab issues have no sub-issue list in the Issue API, so a parent board falls back to
issues whose description mentions `#<parent>`, plus, for a map, the implementation tickets of its
spec (`Part of #<map>`), which name the spec in their `## Parent` section.
"""

from __future__ import annotations

import functools
import json
from datetime import datetime, timedelta, timezone

from ..model import Ticket, parse_time
from . import (IssueStates, ProviderError, Query, blocker_ref, map_family, resolve_maps, run_json,
               tracker_error, with_body_blockers)

ISSUES = """
query($path: ID!, $state: IssuableState, $after: String, $closedAfter: Time,
      $assignees: [String!], $wildcard: AssigneeWildcardId, $labels: [String]) {
  project(fullPath: $path) {
    issues(state: $state, after: $after, first: 100, closedAfter: $closedAfter,
           assigneeUsernames: $assignees, assigneeWildcardId: $wildcard, labelName: $labels) {
      pageInfo { hasNextPage endCursor }
      nodes {
        iid title webUrl description state closedAt
        author { username }
        assignees { nodes { username } }
        labels { nodes { title } }
        blockedByIssues(first: 20) { nodes { iid state reference(full: true) } }
        mergeRequestsCount
      }
    }
  }
}
"""


PARENT = """
query($path: ID!, $iid: String!) {
  project(fullPath: $path) { issue(iid: $iid) { webUrl description labels { nodes { title } } } }
}
"""


STATES = """
query($path: ID!, $iids: [String!]) {
  project(fullPath: $path) { issues(iids: $iids, first: 100) { nodes { iid state } } }
}
"""

VIEWER = "query { currentUser { username } }"


class GitLabProvider:
    name = "gitlab"

    def __init__(self, repo: str, host: str = "gitlab.com"):
        self.repo = repo
        self.host = host
        self.states = IssueStates(self._issue_states)

    def viewer(self) -> str | None:
        return (self._graphql(VIEWER, {}).get("currentUser") or {}).get("username")

    def _issue_states(self, numbers: list[int]) -> dict[int, bool]:
        states: dict[int, bool] = {}
        for start in range(0, len(numbers), 100):
            iids = [str(n) for n in numbers[start:start + 100]]
            project = self._graphql(STATES, {"path": self.repo, "iids": iids}).get("project") or {}
            nodes = ((project.get("issues") or {}).get("nodes")) or []
            states |= {int(node["iid"]): node["state"] == "opened" for node in nodes}
        return states

    def fetch(self, query: Query) -> list[Ticket]:
        closed_after = (datetime.now(timezone.utc) - timedelta(days=query.done_days)).isoformat()
        labels = [query.label] if query.label else None
        filters = [
            {"state": "opened", "labels": labels},
            {"state": "closed", "closedAfter": closed_after, "labels": labels},
        ]
        seen: dict[int, Ticket] = {}
        for variables in filters:
            for node in self._issues(variables, query.limit):
                ticket = to_ticket(node, self.repo)
                seen.setdefault(ticket.number, ticket)
        tickets = list(seen.values())
        issue = functools.cache(self._issue)  # each chain issue off the board once per refresh
        if query.parent is not None:
            tickets = map_family(tickets, query.parent, issue(query.parent))
        return with_body_blockers(resolve_maps(tickets, issue), self.states)

    def _issue(self, iid: int) -> Ticket | None:
        issue = (self._graphql(PARENT, {"path": self.repo, "iid": str(iid)}).get("project") or {}).get("issue")
        if not issue:
            return None
        return Ticket(number=iid, title="", url=issue["webUrl"], is_open=True, body=issue.get("description") or "",
                      labels=tuple(label["title"] for label in issue["labels"]["nodes"]))

    def _issues(self, variables: dict, limit: int) -> list[dict]:
        nodes: list[dict] = []
        after = None
        while len(nodes) < limit:
            data = self._graphql(ISSUES, {"path": self.repo, "after": after, **variables})
            if data.get("project") is None:
                raise ProviderError(f"GitLab project {self.repo} not found")
            page = data["project"]["issues"]
            nodes += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return nodes[:limit]

    def _graphql(self, gql: str, variables: dict) -> dict:
        body = {"query": gql, "variables": {k: v for k, v in variables.items() if v is not None}}
        args = ["glab", "api", "graphql", "--hostname", self.host, "-X", "POST",
                "-H", "Content-Type: application/json", "--input", "-"]
        data = run_json(args, stdin=json.dumps(body))
        if data.get("errors"):
            raise tracker_error("; ".join(e.get("message", "") for e in data["errors"]))
        return data["data"]


def to_ticket(node: dict, repo: str = "") -> Ticket:
    deps = node.get("blockedByIssues")
    blocked_by = tuple(blocker_ref(int(b["iid"]), (b.get("reference") or "").rpartition("#")[0], repo)
                       for b in (deps or {}).get("nodes") or [] if b and b.get("state") == "opened")
    return Ticket(
        number=int(node["iid"]),
        title=node["title"],
        url=node["webUrl"],
        body=node.get("description") or "",
        is_open=node["state"] == "opened",
        closed_at=parse_time(node.get("closedAt")),
        author=(node.get("author") or {}).get("username", ""),
        assignees=tuple(a["username"] for a in node["assignees"]["nodes"]),
        labels=tuple(label["title"] for label in node["labels"]["nodes"]),
        open_blockers=None if deps is None else len(blocked_by),
        blocked_by=blocked_by,
        has_open_change_request=bool(node.get("mergeRequestsCount")) and node["state"] == "opened",
        order=0,
    )
