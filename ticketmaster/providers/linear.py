"""Linear via its GraphQL API, with a personal API key (only the Read permission is enough) or the token
`ticketmaster login` stored (OAuth, scope `read`; see linear_oauth). The key wins when its env var is set.

The board is every open `wayfinder:map` issue of the configured team, and every closed one an open spec still names,
and, per map, its sub-issues in sub-issue order, plus the team's issues whose first line is `Part of <map>`: a typed ticket linked only that way, or the map's
spec. Linear stores a typed `Part of TM-1` as a mention, `[TM-1](<issue URL>)`, so the line may hold the key, the
mention or the URL. A spec's implementation tickets are its sub-issues and the issues whose `## Parent` section
names it. Then every standalone spec with its tickets, found by their signatures on the same linked-issues query:
an open spec's tickets are its sub-issues and the issues whose `## Parent` section names it, of any age; a closed
one's the open and recently closed tickets naming it. Blockers are the `blocks` relations pointing at an issue, then
the `TM-<n>` its `Blocked by:` line or `## Blocked by` section lists.

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
from typing import TYPE_CHECKING

from ..model import Ticket, parse_time, part_of, wayfinder_kinds
from . import (LIMIT, OPEN_MAPS, RECENTLY_CLOSED, SIGNATURE_HITS, IssueStates, ProviderError, Query, RateLimited,
               as_spec, cap_lanes, capped, closed_map_refs, is_map, is_signature_hit, is_spec, is_spec_ticket,
               is_standalone_candidate, parent_ref, spec_lane, under_spec, with_body_blockers, with_map,
               with_spec_tickets)

if TYPE_CHECKING:
    from .linear_oauth import OAuthSession

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


def _recently_signed(*headings: str) -> str:
    """Issues carrying every heading, open or closed within RECENTLY_CLOSED. Linear matches the `##` heading as an
    exact, case-sensitive substring, anywhere in the description: the provider still checks each body."""
    recent = ('{or: [{state: {type: {nin: %s}}}, {completedAt: {gt: "%s"}}, {canceledAt: {gt: "%s"}}]}'
              % (json.dumps(list(CLOSED)), *[f"-P{RECENTLY_CLOSED.days}D"] * 2))
    return "{and: [%s, %s]}" % (", ".join('{description: {contains: "## %s"}}' % h for h in headings), recent)


GQL = {
    "TicketmasterMaps": _issues(
        "TicketmasterMaps",
        "labels: {name: {eq: $label}}, state: {type: {nin: %s}}" % json.dumps(list(CLOSED)),
        ", $label: String!"),
    "TicketmasterIssue": _issues("TicketmasterIssue", "number: {eq: $number}", ", $number: Float!"),
    "TicketmasterNumbers": _issues("TicketmasterNumbers", "number: {in: $numbers}", ", $numbers: [Float!]!"),
    "TicketmasterSubIssues": _issues(
        "TicketmasterSubIssues", "parent: {number: {in: $numbers}}", ", $numbers: [Float!]!"),
    # What a spec or an implementation ticket may say, and the spec and ticket signatures: the search only narrows;
    # the provider checks each body.
    "TicketmasterLinked": _issues(
        "TicketmasterLinked",
        'or: [{description: {contains: "Part of"}}, {description: {contains: "Parent"}},'
        ' {parent: {description: {contains: "Part of"}}}, %s, %s]'
        % (_recently_signed("Problem Statement", "Solution"),
           _recently_signed("What to build", "Acceptance criteria"))),
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
    """POSTs GraphQL to Linear with a personal API key (sent bare in Authorization, as Linear expects) or an OAuth
    session's token (sent as `Bearer`). A token Linear rejects is refreshed once and the request sent again.

    Only https://api.linear.app is accepted, and redirects are refused rather than followed, so the key is never
    sent anywhere else."""

    def __init__(self, api_key: str | None = None, url: str = LINEAR_URL, timeout: float = 30,
                 session: OAuthSession | None = None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != LINEAR_HOST or parts.port not in (None, 443):
            raise ProviderError(f"linear: refusing to send the API key to {url} (only https://{LINEAR_HOST})")
        if (api_key is None) == (session is None):
            raise ValueError("LinearHTTP takes an API key or an OAuth session")
        self.api_key, self.session, self.url, self.timeout = api_key, session, url, timeout

    @staticmethod
    def opener() -> urllib.request.OpenerDirector:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ProviderError(f"linear: refused a redirect (HTTP {code}) to {newurl}")

        return urllib.request.build_opener(NoRedirect)

    def __call__(self, query: str, variables: dict) -> dict:
        if self.session is None:
            return self._post(query, variables, self.api_key)
        authorization = self.session.authorization()
        try:
            return self._post(query, variables, authorization)
        except Unauthenticated:
            self.session.refresh(authorization.removeprefix("Bearer "))
        return self._post(query, variables, self.session.authorization())

    def _post(self, query: str, variables: dict, authorization: str) -> dict:
        request = urllib.request.Request(
            self.url, data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Content-Type": "application/json", "Authorization": authorization})
        status, headers = None, {}
        try:
            with self.opener().open(request, timeout=self.timeout) as response:
                reply = json.load(response)
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers or {}
            try:
                reply = json.load(exc)
            except ValueError:
                error = Unauthenticated if exc.code == 401 else ProviderError
                raise error(f"linear: HTTP {exc.code} {exc.reason}", exc.code) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProviderError(f"linear: {exc}") from exc
        if not isinstance(reply, dict):
            raise ProviderError("linear: the reply is not a GraphQL response", status)
        if errors := reply.get("errors"):
            message = "linear: " + "; ".join(str(error.get("message", "?")) for error in errors)
            codes = {(error.get("extensions") or {}).get("code") for error in errors}
            if "RATELIMITED" in codes:
                raise rate_limited(message, status, headers)
            if status == 401 or "AUTHENTICATION_ERROR" in codes:
                raise Unauthenticated(message, status)
            raise ProviderError(message, status)
        return reply.get("data") or {}


class Unauthenticated(ProviderError):
    """Linear refused the credentials: HTTP 401, or an `AUTHENTICATION_ERROR` (which it sends with HTTP 400)."""


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


def linear_http(var: str, client_id: str | None) -> LinearHTTP:
    """The key in env var `var` when it is set, else the token `ticketmaster login` stored for the OAuth app. The
    refusal names both options, never a value."""
    if api_key := os.environ.get(var):
        return LinearHTTP(api_key)
    from .linear_oauth import OAuthSession, TokenStore  # http.server and the rest only for an OAuth board

    store = TokenStore.for_client(client_id) if client_id else None
    if store is not None and store.load() is not None:
        return LinearHTTP(session=OAuthSession(client_id, store))
    login = "run `ticketmaster login`" if client_id else (
        "set oauth_client_id in .ticketmaster.toml to a Linear OAuth app's client id and run `ticketmaster login`")
    raise ProviderError(f"{var} is not set: put a Linear personal API key in it (Linear: Settings, Security & "
                        f"access; the Read permission is enough), or name another variable with key_env in "
                        f".ticketmaster.toml; or, to read with your own OAuth token instead (oauth_client_id), "
                        f"{login}")


class LinearProvider:
    name = "linear"

    def __init__(self, team: str, key_env: str | None = None, transport: Transport | None = None,
                 oauth_client_id: str | None = None):
        self.team = team
        self.repo = team  # what the board shows as `linear:<team>` and keys saved filters by
        var = check_key_env(key_env)
        self.transport = transport or linear_http(var, oauth_client_id)
        self.states = IssueStates(self._issue_states)
        self.cut_off: list[str] = []

    def viewer(self) -> str | None:
        return (self._query("TicketmasterViewer", {}).get("viewer") or {}).get("displayName")

    def fetch(self, query: Query) -> list[Ticket]:
        """Every open map, every closed map an open spec's `Part of` names, and the named one when it is a closed map;
        per map its sub-issues in sub-issue order,
        then the team's tickets linked to it by `Part of` and the spec's implementation tickets. Then every standalone
        spec that is open or named by an open ticket, with its tickets. At most the query's limit of open maps, and of
        tickets per map or spec; paging stops one past it. The signature hits, which the linked-issues query brings,
        keep the default cap, the newest first."""
        limit, cut = query.limit, []
        open_maps = self._issues("TicketmasterMaps", {"label": MAP_LABEL}, limit + 1)
        found = {int(node["number"]): node for node in capped(open_maps, limit, OPEN_MAPS, cut)}
        linked = {int(node["number"]): node for node in self._issues("TicketmasterLinked", {})}
        candidates = sorted((to_ticket(node) for node in linked.values()),
                            key=lambda t: t.number)  # Linear lists newest first
        since = datetime.now(timezone.utc) - RECENTLY_CLOSED
        hits = capped([t for t in reversed(candidates) if is_signature_hit(t, since)], LIMIT, SIGNATURE_HITS, cut)
        wanted = closed_map_refs(candidates, set(found), self.team)
        if query.named_map is not None and query.named_map not in found.keys() | set(wanted):
            wanted.append(query.named_map)
        for number in wanted:
            found |= {int(node["number"]): node for node in self._issues("TicketmasterIssue", {"number": number})
                      if to_ticket(node).is_wayfinder_map}
        maps = {number: to_ticket(node) for number, node in found.items()}
        tickets = list(maps.values())
        for number, map_ticket in maps.items():
            tickets += [with_map(child, map_ticket) for child in self._children(found[number]["id"], limit + 1)]
        if maps:
            tickets += self._linked(maps, candidates, {t.number for t in tickets}, len(tickets))
        taken = {t.number for t in tickets}
        # A spec has tickets by its whole family, also when the lane is cut before them.
        tickets = cap_lanes(with_spec_tickets(tickets), limit, cut)
        tickets += self._spec_lanes(hits, candidates, linked, set(maps), taken, limit, cut)
        tickets = with_body_blockers(tickets, self.states, key=self.team)
        self.cut_off = cut
        return tickets

    def _spec_lanes(self, hits: list[Ticket], candidates: list[Ticket], nodes: dict[int, dict], maps: set[int],
                    taken: set[int], limit: int, cut: list[str]) -> list[Ticket]:
        """Each standalone spec no map lane has `taken`, then its tickets: the open specs among the signature `hits`,
        and the closed specs an open hit names. An open spec's tickets are its sub-issues, then the `candidates` whose
        `## Parent` section names it, of any age; a closed spec's the hits that name it. The sub-issues of all open
        specs come in one query, the closed specs that are no hit in another; the first `limit` tickets of each spec,
        and a spec with more goes on `cut`."""
        open_hits = [t for t in hits if t.is_open and t.number not in taken]
        specs = {t.number: t for t in open_hits if is_standalone_candidate(t, maps)}
        named = [n for t in open_hits if is_spec_ticket(t, n := t.spec_number) and n not in specs and n not in taken]
        specs |= self._closed_specs(list(dict.fromkeys(named)), nodes, maps)
        subs = self._sub_issues([n for n, spec in specs.items() if spec.is_open])
        shown, lanes = set(taken), []
        for number, spec in sorted(specs.items()):
            if spec.is_open:
                family = subs.get(number, [])
                family += [t for t in candidates if t.number not in {f.number for f in family}]
            else:
                family = sorted(hits, key=lambda t: t.number)
            family = [t for t in family if t.number not in shown | {number} and is_spec_ticket(t, number)]
            lane = spec_lane(spec, family, limit, cut)
            shown |= {t.number for t in lane}
            lanes += lane
        return lanes

    def _closed_specs(self, numbers: list[int], nodes: dict[int, dict], maps: set[int]) -> dict[int, Ticket]:
        """The issues of `numbers` that are closed standalone specs: the spec signature, no `wayfinder:` label and no
        `Part of` line naming a map. Each is read from `nodes` when there, else looked up, and so is the issue its
        `Part of` names; each lookup is one query for all of them."""
        found = self._by_number(numbers, nodes)
        specs = {n: t for n, t in found.items() if not t.is_open and is_standalone_candidate(t, maps)}
        named = {n: ref for n, t in specs.items() if (ref := part_of(t.body, t.team_key)) not in (None, n)}
        targets = self._by_number(list(dict.fromkeys(named.values())), nodes)
        return {n: t for n, t in specs.items() if not (n in named and is_map(targets.get(named[n])))}

    def _by_number(self, numbers: list[int], nodes: dict[int, dict]) -> dict[int, Ticket]:
        """The team's issues of `numbers`, from `nodes` when there, the rest in one query; a number that is no issue
        of the team is left out."""
        ask = [float(n) for n in numbers if n not in nodes]
        fetched = self._issues("TicketmasterNumbers", {"numbers": ask}) if ask else []
        found = {**{n: nodes[n] for n in numbers if n in nodes}, **{int(node["number"]): node for node in fetched}}
        return {n: to_ticket(node) for n, node in found.items()}

    def _sub_issues(self, parents: list[int]) -> dict[int, list[Ticket]]:
        """Every sub-issue of the issues of `parents`, of any state, by parent, in sub-issue order, then by number;
        one paged query for all of them."""
        if not parents:
            return {}
        by_parent: dict[int, list[Ticket]] = {}
        for node in sorted(self._issues("TicketmasterSubIssues", {"numbers": [float(n) for n in parents]}),
                           key=sub_issue_order):
            ticket = to_ticket(node)
            by_parent.setdefault(ticket.parent_number, []).append(ticket)
        return by_parent

    def _children(self, issue_id: str, limit: int) -> list[Ticket]:
        """The issue's first `limit` sub-issues as Linear pages them, in sub-issue order, then by number; `order` is
        that position."""

        def page(after: str | None) -> dict:
            issue = self._query("TicketmasterChildren", {"id": issue_id, "after": after}).get("issue")
            if issue is None:
                raise ProviderError(f"linear: issue {issue_id} not found")
            return issue["children"]

        nodes = sorted((self._complete(node) for node in self._pages(page, "a map's sub-issues", limit=limit)),
                       key=sub_issue_order)
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

    def _issues(self, op: str, variables: dict, limit: int | None = None) -> list[dict]:
        """Every issue of the team a query's filter matches, or its first `limit`, each with all its labels and
        relations."""

        def page(after: str | None) -> dict:
            return self._query(op, {"team": self.team, **variables, "after": after})["issues"]

        return [self._complete(node) for node in self._pages(page, op, limit=limit)]

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
    def _pages(fetch: Callable[[str | None], dict], what: str, connection: dict | None = None,
               limit: int | None = None) -> list[dict]:
        """Every node of a connection, or its first `limit`: its first page (fetched when not given), then
        fetch(endCursor) until hasNextPage is false or `limit` nodes are in. Stops with an error on a repeated cursor
        or past MAX_PAGES, so a misbehaving server can't loop it forever."""
        connection = fetch(None) if connection is None else connection
        nodes, seen = list(connection["nodes"]), set()
        while connection["pageInfo"]["hasNextPage"] and (limit is None or len(nodes) < limit):
            cursor = connection["pageInfo"].get("endCursor")
            if not cursor or cursor in seen:
                raise ProviderError(f"linear: paging {what} repeated a cursor; stopped")
            if len(seen) + 1 >= MAX_PAGES:
                raise ProviderError(f"linear: {what} has more than {MAX_PAGES} pages; stopped")
            seen.add(cursor)
            connection = fetch(cursor)
            nodes += connection["nodes"]
        return nodes if limit is None else nodes[:limit]

    def _query(self, op: str, variables: dict) -> dict:
        return self.transport(GQL[op], variables)


def sub_issue_order(node: dict) -> tuple:
    """Sub-issue order, unordered last, then by number."""
    return node.get("subIssueSortOrder") is None, node.get("subIssueSortOrder") or 0, node["number"]


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
