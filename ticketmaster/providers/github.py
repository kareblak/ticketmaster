"""GitHub via `gh api graphql`: native blockedBy plus `Blocked by:` body lines, sub-issues and closing PRs.

The board is every open `wayfinder:map`, and every closed one an open spec still names, and, per map, its sub-issues in order, the typed tickets that link to
it only by a `Part of #<map>` line, the spec that links to it that way and the spec's implementation tickets:
its sub-issues and the issues whose `## Parent` section names it. Body links show up as cross-references in
the linked issue's timeline. Then every standalone spec with its tickets, found by their signatures: an open spec's
tickets are its children the same way, a closed one's the open and recently closed tickets naming it. Last, the
loose tickets: the open and recently closed issues with the ticket signature that no lane took, whose parent is no spec.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import NamedTuple

from ..model import Ticket, has_spec_signature, has_ticket_signature, parse_time, part_of, wayfinder_kinds
from . import (LIMIT, LOOSE_TICKETS, OPEN_MAPS, RECENTLY_CLOSED, SIGNATURE_HITS, IssueStates, NotFound, ProviderError, Query, Quota,
               RateLimited, as_spec, blocker_ref, capped, closed_map_refs, in_spec_lane, is_signature_hit, is_spec,
               is_spec_ticket, is_standalone_candidate, lane_cut, names_parent, resolve_maps, run_cli, run_json,
               tracker_error, under_spec, with_body_blockers, with_spec_tickets)

MAP_LABEL = "wayfinder:map"

ISSUE_FIELDS = """
  number title url body state stateReason closedAt
  author { login }
  assignees(first: 10) { nodes { login } }
  labels(first: 30) { nodes { name } }
  blockedBy(first: 20) { nodes { number state repository { nameWithOwner } } }
  closedByPullRequestsReferences(first: 10, includeClosedPrs: false) { nodes { state } }
  parent { number url labels(first: 20) { nodes { name } } }
"""

# Every query asks what it cost and what is left; the field costs no points itself.
RATE_LIMIT = "rateLimit { cost remaining resetAt }"

# A search hit carries only what picks the hits worth a full fetch: about 1 point a page.
LEAN_FIELDS = "number body labels(first: 30) { nodes { name } }"

# A signature search hit also says whether it is open, when it closed, and its sub-issue parent.
SIGNATURE_FIELDS = LEAN_FIELDS + " state closedAt parent { number }"

SEARCHES = """
query($q: String!, $after: String) {
  search(query: $q, type: %s, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on Issue { %s } }
  }
  %s
}
"""
SEARCH = SEARCHES % ("ISSUE", LEAN_FIELDS, RATE_LIMIT)
# Advanced search takes OR, so one search finds both signatures, open or recently closed.
SIGNATURE_SEARCH = SEARCHES % ("ISSUE_ADVANCED", SIGNATURE_FIELDS, RATE_LIMIT)

# An issue's sub-issues and the issues of any repo cross-referencing it, a page each, with the paging arguments.
CONNECTIONS = """
  subIssues(first: 100%s)%s {
    pageInfo { hasNextPage endCursor }
    nodes { %s }
  }
  timelineItems(first: 100%s, itemTypes: [CROSS_REFERENCED_EVENT])%s {
    pageInfo { hasNextPage endCursor }
    nodes { ... on CrossReferencedEvent { source { ... on Issue { repository { nameWithOwner } %s } } } }
  }
"""

CHILDREN = """
query($owner: String!, $name: String!, $number: Int!, $subs: String, $refs: String,
      $withSubs: Boolean!, $withRefs: Boolean!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { %s } }
  %s
}
""" % (CONNECTIONS % (", after: $subs", " @include(if: $withSubs)", ISSUE_FIELDS,
                      ", after: $refs", " @include(if: $withRefs)", ISSUE_FIELDS), RATE_LIMIT)

# The first page of each, for a batch of issues.
FIRST_PAGES = CONNECTIONS % ("", "", ISSUE_FIELDS, "", "", ISSUE_FIELDS)

# Issues whose children one batched query reads: each costs about 2 points, and GitHub refuses a query that could
# return more than 500,000 nodes, about 27 issues' worth of children.
BATCH = 25

ISSUE = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { url body labels(first: 30) { nodes { name } } } }
  %s
}
""" % RATE_LIMIT

SUB_ISSUES = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { subIssues(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { number } } }
  }
  %s
}
""" % RATE_LIMIT

VIEWER = "query { viewer { login } %s }" % RATE_LIMIT


# An issue's sub-issues, the issues of this repo referring to it, and whether either went past the fetch limit.
Children = tuple[list[dict], list[dict], bool]


def aliased(selections: list[str]) -> str:
    """One query asking for each of `selections` (`alias: field { … }`) in the repo."""
    fields = " ".join(selections)
    return (f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} "
            f"{RATE_LIMIT} }}")


class GitHubProvider:
    name = "github"

    def __init__(self, repo: str, host: str = "github.com"):
        self.repo = repo
        self.host = host
        self.states = IssueStates(self._issue_states)
        self.quota: Quota | None = None  # the last fetch's cost and the points left after it
        self._spent: int | None = None  # the points spent so far by the fetch under way
        self._seen: tuple[int | None, datetime | None] = (None, None)  # the latest points left and reset time
        self.cut_off: list[str] = []

    def fetch(self, query: Query) -> list[Ticket]:
        """Every open map, every closed map an open spec's `Part of #<map>` names, and their children; the
        sub-issue parent is the native child→map link, `Part of #<map>` the fallback. Then every standalone spec
        that is open or named by an open ticket, with its tickets.

        The searches read lean fields, the maps and the standalone specs get one full fetch together, and the
        children of every map, of every map spec and of every open standalone spec one batched query; `quota` then
        holds what the calls cost. The map search and each lane's tickets stop at the query's limit. The `Part of`
        search, which only finds closed maps, and the signature search keep the default cap: nearly every ticket
        says `Part of`, so a low limit would cut it on every fetch.
        """
        self._spent = 0
        try:
            return self._fetch(query)
        finally:
            self.quota = Quota(self._spent, *self._seen)
            self._spent = None

    def _fetch(self, query: Query) -> list[Ticket]:
        limit, cut = query.limit, []
        q = f'repo:{self.repo} is:issue is:open label:"{MAP_LABEL}"'
        hits = capped(self._paginate(SEARCH, {"q": q}, ["search"], limit + 1), limit, OPEN_MAPS, cut)
        found = [lean_ticket(node) for node in hits]
        q = f'repo:{self.repo} is:issue is:open in:body "Part of"'
        specs = [lean_ticket(node) for node in self._paginate(SEARCH, {"q": q}, ["search"], LIMIT)]
        open_maps = [t.number for t in found]
        wanted = closed_map_refs(specs, set(open_maps))
        if query.named_map is not None and query.named_map not in set(open_maps) | set(wanted):
            wanted.append(query.named_map)
        hits = {t.number: t for t in self._signature_hits(cut)}
        wanted_specs = standalone_wanted(hits, set(open_maps), {s.number for s in specs})
        known = {**{s.number: s for s in specs}, **hits}
        loose, parents = loose_wanted(hits, known, set(open_maps), wanted_specs)
        full = {n: to_ticket(node, order=0, repo=self.repo) for n, node in
                self._issues(open_maps + wanted + wanted_specs.all() + loose + parents).items()}
        open_specs, closed_specs, their_tickets = wanted_specs.open, wanted_specs.closed, wanted_specs.tickets
        maps = [full[n] for n in open_maps if n in full]
        maps += [full[n] for n in wanted if n in full and full[n].is_wayfinder_map]
        numbers = {m.number for m in maps}
        candidates = [s.number for s in specs
                      if s.number not in numbers and part_of(s.body) in numbers and is_spec(s, part_of(s.body))]
        any_map = numbers | {n for n, t in full.items() if t.is_wayfinder_map}
        standalone = [full[n] for n in open_specs + closed_specs if n in full and is_standalone_candidate(full[n], any_map)]
        children = self._children([m.number for m in maps] + candidates
                                  + [s.number for s in standalone if s.is_open], limit)
        # A spec the open `Part of` search missed (a closed one) has its children read in one more batch.
        specs_of = [t.number for m in maps for t in self._map_family(m, children, {m.number}) if is_spec(t, m.number)]
        children |= self._children([n for n in specs_of if n not in children], limit)
        tickets = list(maps)
        for map_ticket in maps:
            tickets += self._map_with_specs(map_ticket, children, {t.number for t in tickets}, limit, cut)
        for spec in standalone:
            if spec.number not in {t.number for t in tickets}:
                named = [full[n] for n in their_tickets if n in full]
                tickets += self._spec_lane(spec, children, named, {t.number for t in tickets}, limit, cut)
        tickets = resolve_maps(tickets, self._issue, tuple(full.values()))
        taken = {t.number for t in tickets}
        found = [replace(full[n], loose=True) for n in loose
                 if n in full and n not in taken and not has_spec_parent(full[n], {**known, **full})]
        tickets += capped(found, limit, LOOSE_TICKETS, cut)
        tickets = with_body_blockers(tickets, self.states)
        self.cut_off = cut
        return tickets

    def _signature_hits(self, cut: list[str]) -> list[Ticket]:
        """The issues with the spec or ticket signature, open or closed within `RECENTLY_CLOSED`, from one lean
        search with the default cap, each body checked exactly."""
        since = datetime.now(timezone.utc) - RECENTLY_CLOSED
        q = (f'repo:{self.repo} is:issue (in:body "Problem Statement" OR in:body "What to build") '
             f'(is:open OR closed:>={since:%Y-%m-%d})')
        nodes = capped(self._paginate(SIGNATURE_SEARCH, {"q": q}, ["search"], LIMIT + 1), LIMIT, SIGNATURE_HITS, cut)
        return [hit for node in nodes if is_signature_hit(hit := signature_hit(node), since)]

    def _spec_lane(self, spec: Ticket, children: dict[int, Children], named: list[Ticket], taken: set[int],
                   limit: int, cut: list[str]) -> list[Ticket]:
        """The standalone spec, then its tickets: an open spec's sub-issues and the issues whose `## Parent` section
        names it, a closed spec's the `named` hits that name it; `order` is that position. The first `limit` of them;
        a spec with more goes on `cut`."""
        if spec.is_open:
            subs, refs, more = children[spec.number]
            family = new_tickets(subs, taken | {spec.number}, self.repo)
            family += new_tickets(refs, taken | {spec.number} | {t.number for t in family}, self.repo)
        else:
            family, more = [t for t in named if t.number not in taken], False
        family = in_spec_lane([t for t in family if is_spec_ticket(t, spec.number)], spec)
        if more or len(family) > limit:
            cut.append(lane_cut(spec))
        family = [replace(t, order=index) for index, t in enumerate(family[:limit])]
        return with_spec_tickets([spec, *family])

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
        state = "... on Issue { state } ... on PullRequest { state }"
        found = self._aliased([f"i{n}: issueOrPullRequest(number: {n}) {{ {state} }}" for n in numbers])
        return {n: found[f"i{n}"]["state"] == "OPEN" for n in numbers if found.get(f"i{n}")}

    def _issues(self, numbers: list[int]) -> dict[int, dict]:
        """The issues by number, all fields, in one query; a number that is no issue is left out."""
        if not numbers:
            return {}
        found = self._aliased([f"i{n}: issue(number: {n}) {{ {ISSUE_FIELDS} }}" for n in dict.fromkeys(numbers)])
        return {n: found[f"i{n}"] for n in numbers if found.get(f"i{n}")}

    def _aliased(self, selections: list[str]) -> dict:
        """The repository's answer to `selections`, where an alias that names no issue is null."""
        owner, name = self.repo.split("/", 1)
        found = self._graphql(aliased(selections), {"owner": owner, "name": name}, partial=True).get("repository")
        if not found:
            raise ProviderError(f"{self.repo} not found")
        return found

    def _args(self) -> list[str]:
        return ["gh", "api", "graphql", "--hostname", self.host, "-H", "Content-Type: application/json", "--input", "-"]

    def _map_family(self, map_ticket: Ticket, children: dict[int, Children], taken: set[int]) -> list[Ticket]:
        """The map's sub-issues in order, then the typed tickets and the spec linked by `Part of #<map>`."""
        number = map_ticket.number
        subs, refs, _ = children[number]
        tickets = new_tickets(subs, taken, self.repo)
        tickets += new_tickets(refs, taken | {t.number for t in tickets}, self.repo,
                               lambda node: part_of(node.get("body") or "") == number)
        return [as_spec(t, map_ticket) if is_spec(t, number) else t for t in tickets]

    def _map_with_specs(self, map_ticket: Ticket, children: dict[int, Children], taken: set[int], limit: int,
                        cut: list[str]) -> list[Ticket]:
        """The map's family, then each spec's implementation tickets: its sub-issues and the issues whose
        `## Parent` section names it; `order` is that position. The first `limit` of them; a map with more, or with
        sub-issues or references (its own or its specs') past `limit`, goes on `cut`."""
        tickets = self._map_family(map_ticket, children, taken)
        more = children[map_ticket.number][2]
        def seen() -> set[int]:
            return taken | {t.number for t in tickets}

        for spec in [t for t in tickets if is_spec(t, map_ticket.number)]:
            subs, refs, spec_more = children[spec.number]
            more = more or spec_more
            tickets += under_spec(new_tickets(subs, seen(), self.repo), spec)
            tickets += under_spec(new_tickets(refs, seen(), self.repo,
                                              lambda node: names_parent(node.get("body"), spec.number)), spec)
        if more or len(tickets) > limit:
            cut.append(lane_cut(map_ticket))
        # A spec has tickets by its whole family, also when the lane is cut before them.
        return [replace(t, order=index) for index, t in enumerate(with_spec_tickets(tickets)[:limit])]

    def _children(self, numbers: list[int], limit: int) -> dict[int, Children]:
        """Each issue's first `limit` sub-issues and the issues of this repo among its first `limit` references, by
        number: the first pages of `BATCH` issues per query, then any further page issue by issue."""
        children: dict[int, Children] = {}
        numbers = list(dict.fromkeys(numbers))
        for start in range(0, len(numbers), BATCH):
            chunk = numbers[start:start + BATCH]
            found = self._aliased([f"c{n}: issue(number: {n}) {{ {FIRST_PAGES} }}" for n in chunk])
            for n in chunk:
                if not found.get(f"c{n}"):
                    raise ProviderError(f"issue #{n} not found in {self.repo}")
                children[n] = self._rest_of(n, found[f"c{n}"], limit)
        return children

    def _rest_of(self, number: int, issue: dict, limit: int) -> Children:
        """The issue's sub-issues and this repo's issues referring to it, reading the pages after `issue`'s first,
        up to `limit` of each, and whether either went past `limit`."""
        owner, name = self.repo.split("/", 1)
        found: dict[str, list[dict]] = {"subIssues": [], "timelineItems": []}
        after: dict[str, str | None] = {"subIssues": None, "timelineItems": None}
        todo = set(found)
        more = False
        while True:
            for connection in list(todo):
                page = issue[connection]
                found[connection] += [n for n in page["nodes"] if n]
                after[connection] = page["pageInfo"].get("endCursor")
                has_next, count = page["pageInfo"]["hasNextPage"], len(found[connection])
                more = more or count > limit or (has_next and count >= limit)
                if not has_next or count >= limit:
                    todo.discard(connection)
            if not todo:
                break
            variables = {"owner": owner, "name": name, "number": number,
                         "subs": after["subIssues"], "refs": after["timelineItems"],
                         "withSubs": "subIssues" in todo, "withRefs": "timelineItems" in todo}
            issue = (self._graphql(CHILDREN, variables).get("repository") or {}).get("issue")
            if issue is None:
                raise ProviderError(f"issue #{number} not found in {self.repo}")
        sources = [event.get("source") or {} for event in found["timelineItems"]]
        refs = {s["number"]: s for s in sources if s.get("number")
                and (s.get("repository") or {}).get("nameWithOwner", "").lower() == self.repo.lower()}
        return found["subIssues"][:limit], list(refs.values())[:limit], more

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

    def _graphql(self, gql: str, variables: dict, partial: bool = False) -> dict:
        """The query's data, noting what it cost. With `partial`, NOT_FOUND errors (an alias naming no issue) are
        no failure: the alias is null, and `gh`'s failing exit for them is read past."""
        try:
            data = json.loads(run_cli(self._args(), stdin=json.dumps({"query": gql, "variables": variables}),
                                      check=not partial))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"`gh` returned non-JSON output: {exc}") from exc
        if "data" not in data and not data.get("errors"):  # a refusal before GraphQL ran: 401, 403, a rate limit
            raise tracker_error(data.get("message") or f"`gh` answered no data: {json.dumps(data)[:200]}")
        self._note((data.get("data") or {}).get("rateLimit"))
        errors = [e for e in data.get("errors") or [] if not (partial and e.get("type") == "NOT_FOUND")]
        if errors:
            message = "; ".join(e.get("message", "") for e in errors)
            if all(e.get("type") == "NOT_FOUND" for e in errors):
                raise NotFound(message)
            raise tracker_error(message)
        return data.get("data") or {}

    def _note(self, rate: dict | None) -> None:
        """Adds a call's cost to the fetch under way, and keeps the points left and the reset time it reports."""
        if not rate:
            return
        if self._spent is not None:
            self._spent += rate.get("cost") or 0
        self._seen = (rate.get("remaining"), parse_time(rate.get("resetAt")))

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


class StandaloneWanted(NamedTuple):
    """What the full fetch reads for the standalone specs, by number: the open specs, the closed specs an open ticket
    names, the maps those name by `Part of`, and the tickets naming them."""

    open: list[int]
    closed: list[int]
    maps: list[int]
    tickets: list[int]

    def all(self) -> list[int]:
        return [*self.open, *self.closed, *self.maps, *self.tickets]


def standalone_wanted(hits: dict[int, Ticket], open_maps: set[int], part_of_specs: set[int]) -> StandaloneWanted:
    """From the signature search hits: the open standalone spec candidates, and the closed specs that keep a lane
    because an open ticket names them, which come in full with their tickets. A parent that is an open hit or an
    open `Part of` issue (`part_of_specs`) is no closed spec."""
    open_specs = [n for n, t in hits.items() if t.is_open and is_standalone_candidate(t, open_maps)]
    named = [t.spec_number for t in hits.values() if t.is_open and is_spec_ticket(t, t.spec_number)]
    closed = list(dict.fromkeys(p for p in named if p not in part_of_specs and not (p in hits and hits[p].is_open)))
    maps = [n for p in closed if p in hits and (n := part_of(hits[p].body)) is not None]
    tickets = [n for n, t in hits.items() if t.spec_number in closed and is_spec_ticket(t, t.spec_number)]
    return StandaloneWanted(open_specs, closed, maps, tickets)


def loose_wanted(hits: dict[int, Ticket], known: dict[int, Ticket], open_maps: set[int],
                 specs: StandaloneWanted) -> tuple[list[int], list[int]]:
    """From the signature search hits: the loose ticket candidates, the ticket signature hits whose parent is no
    spec a search answered, and the parents of theirs that neither a search answered nor the full fetch reads already,
    which the full fetch reads to tell whether they are specs."""
    candidates = [n for n, t in hits.items() if has_ticket_signature(t.body) and not has_spec_signature(t.body)
                  and t.spec_number not in specs.open and not has_spec_parent(t, known)]
    read = set(known) | open_maps | set(specs.all())
    parents = [p for n in candidates if (p := hits[n].spec_number) is not None and p not in read]
    return candidates, list(dict.fromkeys(parents))


def has_spec_parent(ticket: Ticket, issues: dict[int, Ticket]) -> bool:
    """The ticket's parent, among `issues`, is a spec: a standalone spec (the spec signature) or a map's spec (a
    `Part of` line), with no `wayfinder:` label. A parent not found is none."""
    parent = issues.get(ticket.spec_number) if ticket.spec_number is not None else None
    return (parent is not None and not wayfinder_kinds(parent.labels)
            and (has_spec_signature(parent.body) or part_of(parent.body) is not None))


def new_tickets(nodes: list[dict], taken: set[int], repo: str, links=lambda node: True) -> list[Ticket]:
    """The nodes not `taken` that `links` accepts, as tickets."""
    return [to_ticket(node, order=0, repo=repo) for node in nodes if node["number"] not in taken and links(node)]


def signature_hit(node: dict) -> Ticket:
    """A signature search hit, from its lean fields."""
    return replace(lean_ticket(node), is_open=node.get("state") == "OPEN", closed_at=parse_time(node.get("closedAt")),
                   parent_number=(node.get("parent") or {}).get("number"))


def lean_ticket(node: dict) -> Ticket:
    """An open search hit, from its lean fields."""
    return Ticket(number=node["number"], title="", url="", is_open=True, body=node.get("body") or "",
                  labels=tuple(label["name"] for label in (node.get("labels") or {}).get("nodes", [])))


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
