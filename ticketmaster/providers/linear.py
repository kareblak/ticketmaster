"""Linear via its GraphQL API, with a personal API key; a key with only the Read permission is enough.

The board is every open `wayfinder:map` issue of the configured team, and every closed one an open spec still names,
and, per map, its sub-issues in sub-issue order, plus the team's issues whose first line is `Part of <map>`: a typed ticket linked only that way, or the map's
spec. Linear stores a typed `Part of TM-1` as a mention, `[TM-1](<issue URL>)`, so the line may hold the key, the
mention or the URL. A spec's implementation tickets are its sub-issues and the issues whose `## Parent` section
names it. Blockers are the `blocks` relations pointing at an issue, then the `TM-<n>` its `Blocked by:` line or
`## Blocked by` section lists.

Only `state.type` is read, never the team's renamable state names: `completed` is done, `canceled` and `duplicate`
are closed as not planned. Only the assignee claims; the delegate is ignored. Users are their `displayName`.

The transport, the key env var check, the paging guard and the complexity budget are copied from fabric's Linear
backend, not imported: fabric-store is a script loaded by path, and its backend also writes.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from ..model import Ticket, parse_time, part_of, wayfinder_kinds
from . import (IssueStates, ProviderError, Query, RateLimited, as_spec, closed_map_refs, is_spec, parent_ref,
               under_spec, with_body_blockers, with_map)

LINEAR_URL = "https://api.linear.app/graphql"
LINEAR_HOST = "api.linear.app"
KEY_ENV = "LINEAR_API_KEY"  # the env var holding the key; `key_env` in .ticketmaster.toml names another
KEY_ENV_NAME = re.compile(r"LINEAR_[A-Z0-9_]{1,57}", re.A)  # LINEAR_ only: no other secret goes to Linear
MAP_LABEL = "wayfinder:map"
CLOSED = ("completed", "canceled", "duplicate")  # workflow state types of a closed issue
NOT_PLANNED = ("canceled", "duplicate")
RATE_LIMIT_HINT = "every board, agent and script using this key's user shares it"

# Query complexity. Linear charges each scalar 0.1 and each object 1, multiplies a connection's nodes by its
# `first` (50 when absent) and refuses a single query over 10,000. Every document below stays under BUDGET, half
# the cap, so Linear can count a little differently without breaking reads (tests/test_linear.py estimates each
# one). One issue with the fragment below costs 10.8 + 1.1*LABELS + 3.3*RELATIONS = 87.8 points, so a page of
# PAGE issues (the maps, a map's sub-issues, the issues linked by `Part of` or `## Parent`) is about 4,400, and
# the follow-ups for an issue's labels or relations past their first page about 110-330.
# A connection that holds more than its first page is paged with `after`; none is silently cut short.
MAX_COMPLEXITY = 10_000
BUDGET = MAX_COMPLEXITY // 2
PAGE = 50  # issues per page
LABELS = 10
RELATIONS = 20
MORE = 100  # page size of the follow-up queries
MAX_PAGES = 200  # any paged read stops with an error past this many pages

# Every GraphQL document the provider sends, by operation name (tests/fake_linear.py answers the same names).
_PAGE_INFO = "pageInfo { hasNextPage endCursor }"
_LABEL = "name"
_RELATION = "type issue { identifier state { type } }"
_ISSUE_FIELDS = """
fragment TicketFields on Issue {
  id identifier number title url description branchName subIssueSortOrder completedAt canceledAt
  state { type }
  creator { displayName }
  assignee { displayName }
  parent { number }
  labels(first: %(labels)d) { nodes { %(label)s } %(page)s }
  inverseRelations(first: %(relations)d) { nodes { %(relation)s } %(page)s }
}""" % {"labels": LABELS, "relations": RELATIONS, "label": _LABEL, "relation": _RELATION, "page": _PAGE_INFO}


def _issues(op: str, where: str, variables: str = "") -> str:
    """The team's issues matching `where`, more IssueFilter fields; `variables` declares the ones it uses."""
    return """
query %s($team: String!, $after: String%s) {
  issues(first: %d, after: $after, filter: {team: {key: {eq: $team}}, %s}) { nodes { ...TicketFields } %s }
}""" % (op, variables, PAGE, where, _PAGE_INFO) + _ISSUE_FIELDS


def _more(op: str, field: str, fields: str) -> str:
    return """
query %s($id: String!, $after: String) {
  issue(id: $id) { %s(first: %d, after: $after) { nodes { %s } %s } }
}""" % (op, field, MORE, fields, _PAGE_INFO)


GQL = {
    "TicketmasterMaps": _issues(
        "TicketmasterMaps",
        "labels: {name: {eq: $label}}, state: {type: {nin: %s}}" % json.dumps(list(CLOSED)),
        ", $label: String!"),
    "TicketmasterIssue": _issues("TicketmasterIssue", "number: {eq: $number}", ", $number: Float!"),
    # What a spec or an implementation ticket may say: the search only narrows; the provider checks each body.
    "TicketmasterLinked": _issues(
        "TicketmasterLinked",
        'or: [{description: {contains: "Part of"}}, {description: {contains: "Parent"}},'
        ' {parent: {description: {contains: "Part of"}}}]'),
    "TicketmasterChildren": """
query TicketmasterChildren($id: String!, $after: String) {
  issue(id: $id) { children(first: %d, after: $after) { nodes { ...TicketFields } %s } }
}""" % (PAGE, _PAGE_INFO) + _ISSUE_FIELDS,
    "TicketmasterLabels": _more("TicketmasterLabels", "labels", _LABEL),
    "TicketmasterRelations": _more("TicketmasterRelations", "inverseRelations", _RELATION),
    "TicketmasterStates": """
query TicketmasterStates($team: String!, $numbers: [Float!]!, $after: String) {
  issues(first: %d, after: $after, filter: {team: {key: {eq: $team}}, number: {in: $numbers}}) {
    nodes { number state { type } } %s
  }
}""" % (MORE, _PAGE_INFO),
    "TicketmasterViewer": """
query TicketmasterViewer {
  viewer { displayName }
}""",
}

# query, variables -> the reply's `data`
Transport = Callable[[str, dict], dict]


class LinearHTTP:
    """POSTs GraphQL to Linear with a personal API key (sent bare in Authorization, as Linear expects).

    Only https://api.linear.app is accepted, and redirects are refused rather than followed, so the key is never
    sent anywhere else."""

    def __init__(self, api_key: str, url: str = LINEAR_URL, timeout: float = 30):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != LINEAR_HOST or parts.port not in (None, 443):
            raise ProviderError(f"linear: refusing to send the API key to {url} (only https://{LINEAR_HOST})")
        self.api_key, self.url, self.timeout = api_key, url, timeout

    @staticmethod
    def opener() -> urllib.request.OpenerDirector:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ProviderError(f"linear: refused a redirect (HTTP {code}) to {newurl}")

        return urllib.request.build_opener(NoRedirect)

    def __call__(self, query: str, variables: dict) -> dict:
        request = urllib.request.Request(
            self.url, data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Content-Type": "application/json", "Authorization": self.api_key})
        status, headers = None, {}
        try:
            with self.opener().open(request, timeout=self.timeout) as response:
                reply = json.load(response)
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers or {}
            try:
                reply = json.load(exc)
            except ValueError:
                raise ProviderError(f"linear: HTTP {exc.code} {exc.reason}", exc.code) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProviderError(f"linear: {exc}") from exc
        if not isinstance(reply, dict):
            raise ProviderError("linear: the reply is not a GraphQL response", status)
        if errors := reply.get("errors"):
            message = "linear: " + "; ".join(str(error.get("message", "?")) for error in errors)
            if any((error.get("extensions") or {}).get("code") == "RATELIMITED" for error in errors):
                raise rate_limited(message, status, headers)
            raise ProviderError(message, status)
        return reply.get("data") or {}


def rate_limited(message: str, status: int | None, headers) -> RateLimited:
    """Linear's refusal (HTTP 400, `RATELIMITED`), naming the hourly limit that ran out and when it lifts; its
    reset headers are epoch milliseconds."""
    for kind in ("requests", "complexity"):
        name = kind.title()
        if headers.get(f"X-RateLimit-{name}-Remaining") == "0":
            reset = headers.get(f"X-RateLimit-{name}-Reset") or ""
            lifts = datetime.fromtimestamp(int(reset) / 1000, timezone.utc) if reset.isdigit() else None
            limit = "hourly request limit" if kind == "requests" else "hourly complexity limit"
            return RateLimited(message, status, limit, lifts, RATE_LIMIT_HINT)
    return RateLimited(message, status, hint=RATE_LIMIT_HINT)


def check_key_env(var: str | None) -> str:
    """An env var name for a Linear key: LINEAR_ then upper-case letters, digits and `_`, as in LINEAR_API_KEY_ACME.
    The error never repeats the value: it may be a pasted key."""
    var = (var or "").strip() or KEY_ENV
    if not KEY_ENV_NAME.fullmatch(var):
        raise ProviderError("bad key_env in .ticketmaster.toml: an env var name starting LINEAR_, then A-Z, 0-9 and "
                            "_, as in LINEAR_API_KEY_ACME (the value is not shown: it may be a key)")
    return var


def linear_key(var: str) -> str:
    """The API key in env var `var`; the error names the var and the key_env option, never a value."""
    api_key = os.environ.get(var)
    if not api_key:
        raise ProviderError(f"{var} is not set: put a Linear personal API key in it (Linear: Settings, Security & "
                            f"access; the Read permission is enough), or name another variable with key_env in "
                            f".ticketmaster.toml")
    return api_key


class LinearProvider:
    name = "linear"

    def __init__(self, team: str, key_env: str | None = None, transport: Transport | None = None):
        self.team = team
        self.repo = team  # what the board shows as `linear:<team>` and keys saved filters by
        var = check_key_env(key_env)
        self.transport = transport or LinearHTTP(linear_key(var))
        self.states = IssueStates(self._issue_states)

    def viewer(self) -> str | None:
        return (self._query("TicketmasterViewer", {}).get("viewer") or {}).get("displayName")

    def fetch(self, query: Query) -> list[Ticket]:
        """Every open map, every closed map an open spec's `Part of` names, and the named one when it is a closed map;
        per map its sub-issues in sub-issue order,
        then the team's tickets linked to it by `Part of` and the spec's implementation tickets."""
        found = {int(node["number"]): node for node in self._issues("TicketmasterMaps", {"label": MAP_LABEL})}
        candidates = sorted((to_ticket(node) for node in self._issues("TicketmasterLinked", {})),
                            key=lambda t: t.number)  # Linear lists newest first
        wanted = closed_map_refs(candidates, set(found), self.team)
        if query.named_map is not None and query.named_map not in found.keys() | set(wanted):
            wanted.append(query.named_map)
        for number in wanted:
            found |= {int(node["number"]): node for node in self._issues("TicketmasterIssue", {"number": number})
                      if to_ticket(node).is_wayfinder_map}
        maps = {number: to_ticket(node) for number, node in found.items()}
        tickets = list(maps.values())
        for number, map_ticket in maps.items():
            tickets += [with_map(child, map_ticket) for child in self._children(found[number]["id"])]
        if maps:
            tickets += self._linked(maps, candidates, {t.number for t in tickets}, len(tickets))
        return with_body_blockers(tickets, self.states, key=self.team)

    def _children(self, issue_id: str) -> list[Ticket]:
        """The issue's sub-issues in sub-issue order, then by number; `order` is that position."""

        def page(after: str | None) -> dict:
            issue = self._query("TicketmasterChildren", {"id": issue_id, "after": after}).get("issue")
            if issue is None:
                raise ProviderError(f"linear: issue {issue_id} not found")
            return issue["children"]

        nodes = sorted((self._complete(node) for node in self._pages(page, "a map's sub-issues")),
                       key=lambda node: (node.get("subIssueSortOrder") is None, node.get("subIssueSortOrder") or 0,
                                         node["number"]))
        return [to_ticket(node, order) for order, node in enumerate(nodes)]

    def _linked(self, maps: dict[int, Ticket], candidates: list[Ticket], seen: set[int], order: int) -> list[Ticket]:
        """The tickets whose `Part of` line names a map: a typed ticket goes on it, an untyped one is its spec; then
        each spec's implementation tickets, its sub-issues and those whose `## Parent` section names it. Their
        `order` follows the maps' sub-issues."""
        candidates = [t for t in candidates if t.number not in seen]
        linked, specs = [], {}
        for ticket in candidates:
            map_number = part_of(ticket.body, self.team)
            map_ticket = maps.get(map_number) if map_number is not None else None
            if map_ticket is None or map_ticket.number == ticket.number:
                continue
            if ticket.wayfinder_type is not None:
                linked.append(with_map(ticket, map_ticket))
            elif is_spec(ticket, map_ticket.number):
                specs[ticket.number] = as_spec(ticket, map_ticket)
                linked.append(specs[ticket.number])
        for ticket in candidates:
            if ticket.number in specs or wayfinder_kinds(ticket.labels):
                continue
            named = parent_ref(ticket.body, self.team)
            spec = specs.get(ticket.parent_number or 0) or (specs.get(named) if named is not None else None)
            if spec is not None:
                linked += under_spec([ticket], spec)
        return [replace(ticket, order=order + index) for index, ticket in enumerate(linked)]

    def _issue_states(self, numbers: list[int]) -> dict[int, bool]:
        """Open (True) or closed by number; a number that is no issue of the team is left out."""

        def page(after: str | None) -> dict:
            variables = {"team": self.team, "numbers": [float(n) for n in numbers], "after": after}
            return self._query("TicketmasterStates", variables)["issues"]

        nodes = self._pages(page, "the blockers' states")
        return {int(node["number"]): node["state"]["type"] not in CLOSED for node in nodes}

    def _issues(self, op: str, variables: dict) -> list[dict]:
        """Every issue of the team a query's filter matches, each with all its labels and relations."""

        def page(after: str | None) -> dict:
            return self._query(op, {"team": self.team, **variables, "after": after})["issues"]

        return [self._complete(node) for node in self._pages(page, op)]

    def _complete(self, node: dict) -> dict:
        """The issue with all its labels and relations, not just their first pages."""
        for field, op in (("labels", "TicketmasterLabels"), ("inverseRelations", "TicketmasterRelations")):
            connection = node[field]
            if connection["pageInfo"]["hasNextPage"]:
                def page(after: str | None, field=field, op=op) -> dict:
                    return self._query(op, {"id": node["id"], "after": after})["issue"][field]

                node[field] = {"nodes": self._pages(page, f"{node['identifier']}'s {field}", connection),
                               "pageInfo": {"hasNextPage": False}}
        return node

    @staticmethod
    def _pages(fetch: Callable[[str | None], dict], what: str, connection: dict | None = None) -> list[dict]:
        """Every node of a connection: its first page (fetched when not given), then fetch(endCursor) until
        hasNextPage is false. Stops with an error on a repeated cursor or past MAX_PAGES, so a misbehaving server
        can't loop it forever."""
        connection = fetch(None) if connection is None else connection
        nodes, seen = list(connection["nodes"]), set()
        while connection["pageInfo"]["hasNextPage"]:
            cursor = connection["pageInfo"].get("endCursor")
            if not cursor or cursor in seen:
                raise ProviderError(f"linear: paging {what} repeated a cursor; stopped")
            if len(seen) + 1 >= MAX_PAGES:
                raise ProviderError(f"linear: {what} has more than {MAX_PAGES} pages; stopped")
            seen.add(cursor)
            connection = fetch(cursor)
            nodes += connection["nodes"]
        return nodes

    def _query(self, op: str, variables: dict) -> dict:
        return self.transport(GQL[op], variables)


def to_ticket(node: dict, order: int = 0) -> Ticket:
    state = node["state"]["type"]
    blocked_by = tuple(dict.fromkeys(
        relation["issue"]["identifier"] for relation in node["inverseRelations"]["nodes"]
        if relation["type"] == "blocks" and relation["issue"]["state"]["type"] not in CLOSED))
    parent, assignee = node.get("parent"), node.get("assignee")
    return Ticket(
        number=int(node["number"]),
        title=node["title"],
        url=node["url"],
        body=node.get("description") or "",
        is_open=state not in CLOSED,
        closed_at=parse_time(node.get("completedAt") or node.get("canceledAt")),
        not_planned=state in NOT_PLANNED,
        author=(node.get("creator") or {}).get("displayName", ""),
        assignees=(assignee["displayName"],) if assignee else (),
        labels=tuple(label["name"] for label in node["labels"]["nodes"]),
        open_blockers=len(blocked_by),
        blocked_by=blocked_by,
        order=order,
        parent_number=int(parent["number"]) if parent else None,
        key=node["identifier"],
        branch_name=node.get("branchName"),
    )
