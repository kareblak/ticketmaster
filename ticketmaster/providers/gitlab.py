"""GitLab via `glab api graphql`: `blocks` links and closing merge requests.

GitLab issues have no sub-issue list in the Issue API, so a parent board falls back to
issues whose description mentions `#<parent>`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..model import Ticket, parse_time
from . import ProviderError, Query, mentions_parent, on_map_board, resolve_maps, run_json, tracker_error

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
        blockedByCount
        mergeRequestsCount
      }
    }
  }
}
"""


PARENT = """
query($path: ID!, $iid: String!) {
  project(fullPath: $path) { issue(iid: $iid) { webUrl labels { nodes { title } } } }
}
"""


class GitLabProvider:
    name = "gitlab"

    def __init__(self, repo: str, host: str = "gitlab.com"):
        self.repo = repo
        self.host = host

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
                ticket = to_ticket(node)
                seen.setdefault(ticket.number, ticket)
        tickets = list(seen.values())
        if query.parent is not None:
            tickets = [t for t in tickets if t.number != query.parent and mentions_parent(t.body, query.parent)]
            tickets = on_map_board(tickets, query.parent, self._map(query.parent))
        return resolve_maps(tickets, self._map)

    def _map(self, iid: int) -> tuple[str, tuple[str, ...]] | None:
        issue = (self._graphql(PARENT, {"path": self.repo, "iid": str(iid)}).get("project") or {}).get("issue")
        return (issue["webUrl"], tuple(label["title"] for label in issue["labels"]["nodes"])) if issue else None

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


def to_ticket(node: dict) -> Ticket:
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
        open_blockers=node.get("blockedByCount"),
        has_open_change_request=bool(node.get("mergeRequestsCount")) and node["state"] == "opened",
        order=0,
    )
