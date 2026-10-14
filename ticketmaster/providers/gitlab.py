"""GitLab via `glab api graphql`: `blocks` links plus `Blocked by:` body lines, and closing merge requests.

GitLab issues have no sub-issue list in the Issue API, so the board reads the project's `wayfinder:` labels
(`wayfinder:map` or the scoped `wayfinder::map`, and the four ticket types): the open maps, and each typed
ticket whose description's first line is `Part of #<map>` for one of them.
"""

from __future__ import annotations

import functools
import json

from ..model import WAYFINDER_TYPES, Ticket, parse_time, part_of, wayfinder_kinds
from . import LIMIT, IssueStates, ProviderError, Query, blocker_ref, resolve_maps, run_json, tracker_error, with_body_blockers

ISSUE_FIELDS = """
  iid title webUrl description state closedAt
  author { username }
  assignees { nodes { username } }
  labels { nodes { title } }
  blockedByIssues(first: 20) { nodes { iid state reference(full: true) } }
  mergeRequestsCount
"""

ISSUES = """
query($path: ID!, $state: IssuableState, $after: String, $labels: [String]) {
  project(fullPath: $path) {
    issues(state: $state, after: $after, first: 100, labelName: $labels) {
      pageInfo { hasNextPage endCursor }
      nodes { %s }
    }
  }
}
""" % ISSUE_FIELDS

NAMED = """
query($path: ID!, $named: String!) {
  project(fullPath: $path) { issue(iid: $named) { %s } }
}
""" % ISSUE_FIELDS

LABELS = """
query($path: ID!, $after: String) {
  project(fullPath: $path) {
    labels(first: 100, after: $after, searchTerm: "wayfinder", includeAncestorGroups: true) {
      pageInfo { hasNextPage endCursor }
      nodes { title }
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
        """The open maps, the named map, and every typed ticket, open or closed, whose `Part of #<map>` names one."""
        labels = self._wayfinder_labels()
        maps = {t.number: t for label in labels["map"] for t in self._issues({"state": "opened", "labels": [label]})
                if t.is_wayfinder_map}
        if query.named_map is not None and query.named_map not in maps:
            named = self._named(query.named_map)
            maps |= {named.number: named} if named is not None and named.is_wayfinder_map else {}
        children = {t.number: t for label in labels["ticket"] for t in self._issues({"labels": [label]})
                    if t.wayfinder_type is not None and part_of(t.body) in maps}
        tickets = list(maps.values()) + [t for n, t in children.items() if n not in maps]
        return with_body_blockers(resolve_maps(tickets, functools.cache(self._issue)), self.states)

    def _wayfinder_labels(self) -> dict[str, list[str]]:
        """The project's (and its groups') label titles for maps and for the four ticket types."""
        found: dict[str, list[str]] = {"map": [], "ticket": []}
        for title in [node["title"] for node in self._pages(LABELS, {}, ["project", "labels"])]:
            kinds = wayfinder_kinds((title,))
            if "map" in kinds:
                found["map"].append(title)
            elif any(kind in WAYFINDER_TYPES for kind in kinds):
                found["ticket"].append(title)
        return found

    def _named(self, iid: int) -> Ticket | None:
        issue = (self._graphql(NAMED, {"path": self.repo, "named": str(iid)}).get("project") or {}).get("issue")
        return to_ticket(issue, self.repo) if issue else None

    def _issue(self, iid: int) -> Ticket | None:
        issue = (self._graphql(PARENT, {"path": self.repo, "iid": str(iid)}).get("project") or {}).get("issue")
        if not issue:
            return None
        return Ticket(number=iid, title="", url=issue["webUrl"], is_open=True, body=issue.get("description") or "",
                      labels=tuple(label["title"] for label in issue["labels"]["nodes"]))

    def _issues(self, variables: dict) -> list[Ticket]:
        return [to_ticket(node, self.repo) for node in self._pages(ISSUES, variables, ["project", "issues"])]

    def _pages(self, gql: str, variables: dict, path: list[str]) -> list[dict]:
        nodes: list[dict] = []
        after = None
        while len(nodes) < LIMIT:
            page = self._graphql(gql, {"path": self.repo, "after": after, **variables})
            for key in path:
                page = (page or {}).get(key)
            if page is None:
                raise ProviderError(f"GitLab project {self.repo} not found")
            nodes += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return nodes[:LIMIT]

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
