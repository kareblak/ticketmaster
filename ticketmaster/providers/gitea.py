"""Gitea and Forgejo via `tea api`, with the `tea` login whose URL matches the repo host (a token or an OAuth login,
which tea keeps fresh). `tea api` exits 0 on HTTP errors, so the status and headers come from its `-i` output on
stderr. Dependencies come from `/issues/{n}/dependencies` plus `Blocked by:` body lines. There are no sub-issues, so the board reads the repo's and its org's `wayfinder:` labels: the
open maps, and each typed ticket whose body's first line is `Part of #<map>` for one of them. A map's spec is an
untyped issue with that first line, and the spec's implementation tickets name it in a `## Parent` section; both
are found by an issue search (`q`). An open pull request closes an issue when its body says
`closes/fixes/resolves #<n>`.
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.parser import HeaderParser
from email.utils import parsedate_to_datetime

from ..model import Ticket, parse_time
from . import (LABEL_LIMIT, LIMIT, SPEC_SEARCHES, IssueStates, NotFound, ProviderError, Query, RateLimited,
               blocker_ref, closed_map_refs, map_board, resolve_maps, run_json, tracker_error, wayfinder_labels,
               with_body_blockers, with_spec_tickets, with_specs)

PAGE = 50
CLOSING = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#{n}\b"
STATUS = re.compile(r"^HTTP/\S+ (\d{3})\b ?(.*)$", re.MULTILINE)


def now() -> datetime:
    """The clock a `Retry-After` in seconds counts from."""
    return datetime.now(timezone.utc)


def lifts_at(headers) -> datetime | None:
    """When a 429 lifts: `Retry-After` (seconds or an HTTP date), else a reset header (epoch seconds)."""
    if retry := headers.get("Retry-After"):
        if retry.strip().isdigit():
            return now() + timedelta(seconds=int(retry))
        try:
            lifts = parsedate_to_datetime(retry)
            return lifts if lifts.tzinfo else lifts.replace(tzinfo=timezone.utc)  # `-0000`: UTC, no zone given
        except (TypeError, ValueError):
            pass
    reset = headers.get("X-RateLimit-Reset") or headers.get("RateLimit-Reset")
    return datetime.fromtimestamp(int(reset), timezone.utc) if reset and reset.strip().isdigit() else None


def error_message(body: str) -> str:
    """The `message` of the tracker's JSON error body, when it sent one."""
    try:
        return str(json.loads(body or "null").get("message") or "")
    except (ValueError, AttributeError):
        return ""


class GiteaProvider:
    name = "gitea"

    def __init__(self, repo: str, host: str, login: str | None = None):
        self.repo = repo
        self.host = host
        if login:
            self.login = login
        self.states = IssueStates(self._issue_states)

    @functools.cached_property
    def login(self) -> str:
        """The name of the `tea` login for this host, looked up on the first call."""
        for login in run_json(["tea", "logins", "list", "-o", "json"]) or []:
            if urllib.parse.urlparse(login.get("url", "")).hostname == self.host:
                return login["name"]
        raise ProviderError(f"no `tea` login for {self.host}: run `tea logins add --url https://{self.host}`")

    def viewer(self) -> str | None:
        return self._get("/user").get("login")

    def fetch(self, query: Query) -> list[Ticket]:
        """The open maps, the closed maps an open spec names, the named map, and every typed ticket, open or closed,
        whose `Part of #<map>` names one."""
        labels = self._wayfinder_labels()
        pulls = self._list(f"/repos/{self.repo}/pulls", {"state": "open"}, 200)
        pr_bodies = [f"{p.get('title', '')}\n{p.get('body') or ''}" for p in pulls]

        def issues(state: str, limit: int = LIMIT, **filters: str) -> list[Ticket]:
            params = {"type": "issues", "state": state, **filters}
            return [to_ticket(i, None, pr_bodies) for i in self._list(f"/repos/{self.repo}/issues", params, limit)]

        maps = {t.number: t for label in labels["map"] for t in issues("open", labels=label) if t.is_wayfinder_map}
        named = None
        if query.named_map is not None and query.named_map not in maps:
            found = self._issue_or_none(query.named_map)
            named = to_ticket(found, None, pr_bodies) if found else None
        children = [t for label in labels["ticket"] for t in issues("all", LABEL_LIMIT, labels=label)]
        candidates = [t for words in SPEC_SEARCHES for t in issues("all", LABEL_LIMIT, q=words)] if labels["map"] else []
        shown = maps.keys() | ({named.number} if named else set())
        for number in closed_map_refs(candidates, shown):
            if (found := self._issue_or_none(number)) and (closed := to_ticket(found, None, pr_bodies)).is_wayfinder_map:
                maps[number] = closed
        tickets = map_board(maps, named, children)
        if any(t.is_wayfinder_map for t in tickets):
            tickets = with_specs(tickets, candidates)
        open_numbers = [t.number for t in tickets if t.is_open]
        with ThreadPoolExecutor(max_workers=8) as pool:
            blockers = dict(zip(open_numbers, pool.map(self._open_blockers, open_numbers)))
        tickets = [with_native_blockers(t, blockers.get(t.number)) for t in tickets]
        return with_spec_tickets(with_body_blockers(resolve_maps(tickets, functools.cache(self._issue)), self.states))

    def _wayfinder_labels(self) -> dict[str, list[str]]:
        """The repo's and its org's label names for maps and for the four ticket types. Gitea drops a label filter
        it does not know and then lists every issue, so only names that exist are asked for."""
        names = [label["name"] for label in self._list(f"/repos/{self.repo}/labels", {}, LIMIT)]
        try:
            names += [label["name"] for label in self._list(f"/orgs/{self.repo.split('/')[0]}/labels", {}, LIMIT)]
        except NotFound:
            pass  # a user's repo has no org labels
        return wayfinder_labels(names)

    def _issue(self, number: int) -> Ticket | None:
        found = self._issue_or_none(number)
        return to_ticket(found, None, []) if found else None

    def _open_blockers(self, number: int) -> tuple[str, ...] | None:
        try:
            deps = self._get(f"/repos/{self.repo}/issues/{number}/dependencies")
        except RateLimited:
            raise
        except ProviderError:
            return None  # dependencies disabled for this repo
        return tuple(blocker_ref(d["number"], (d.get("repository") or {}).get("full_name", ""), self.repo)
                     for d in deps if d.get("state") == "open")

    def _issue_states(self, numbers: list[int]) -> dict[int, bool]:
        with ThreadPoolExecutor(max_workers=8) as pool:
            issues = pool.map(self._issue_or_none, numbers)
        return {n: issue["state"] == "open" for n, issue in zip(numbers, issues) if issue}

    def _issue_or_none(self, number: int) -> dict | None:
        try:
            return self._get(f"/repos/{self.repo}/issues/{number}")
        except NotFound:
            return None  # deleted, or not visible to this login

    def _list(self, path: str, params: dict, limit: int) -> list[dict]:
        items: list[dict] = []
        page = 1
        while len(items) < limit:
            batch = self._get(path, {**params, "limit": PAGE, "page": page})
            items += batch
            if len(batch) < PAGE:
                break
            page += 1
        return items[:limit]

    def _get(self, path: str, params: dict | None = None):
        endpoint = path + ("?" + urllib.parse.urlencode(params) if params else "")
        status, reason, headers, body = tea_api(["tea", "api", "--login", self.login, "-i", endpoint])
        if status == 429:
            raise RateLimited(f"{self.host}{path}: HTTP 429 rate limit exceeded", 429, resets_at=lifts_at(headers))
        if status == 404:
            raise NotFound(f"{self.host}{path}: HTTP 404 {reason}")
        if status >= 400:
            said = error_message(body)
            raise tracker_error(f"{self.host}{path}: HTTP {status} {reason}" + (f": {said}" if said else ""))
        return json.loads(body) if body.strip() else None


def tea_api(args: list[str]) -> tuple[int, str, Message, str]:
    """Status, reason, headers and body of a `tea api -i` call; a call that never reached the tracker raises."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise ProviderError("`tea` is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"`{' '.join(args[:3])}` timed out") from exc
    status = STATUS.search(result.stderr)
    if result.returncode != 0 or not status:
        raise tracker_error(result.stderr.strip() or result.stdout.strip() or f"`tea` exited {result.returncode}")
    headers = HeaderParser().parsestr(result.stderr[status.end():].lstrip("\r\n"), headersonly=True)
    return int(status[1]), status[2].strip(), headers, result.stdout


def with_native_blockers(ticket: Ticket, blocked_by: tuple[str, ...] | None) -> Ticket:
    """None when the repo has dependencies disabled, so the blocker count stays unknown."""
    return replace(ticket, open_blockers=None if blocked_by is None else len(blocked_by), blocked_by=blocked_by or ())


def to_ticket(issue: dict, blocked_by: tuple[str, ...] | None, pr_bodies: list[str]) -> Ticket:
    closing = re.compile(CLOSING.format(n=issue["number"]), re.IGNORECASE)
    is_open = issue["state"] == "open"
    return Ticket(
        number=issue["number"],
        title=issue["title"],
        url=issue["html_url"],
        body=issue.get("body") or "",
        is_open=is_open,
        closed_at=parse_time(issue.get("closed_at")),
        author=(issue.get("user") or {}).get("login", ""),
        assignees=tuple(a["login"] for a in issue.get("assignees") or []),
        labels=tuple(label["name"] for label in issue.get("labels") or []),
        open_blockers=None if blocked_by is None else len(blocked_by),
        blocked_by=blocked_by or (),
        has_open_change_request=is_open and any(closing.search(body) for body in pr_bodies),
        order=0,
    )
