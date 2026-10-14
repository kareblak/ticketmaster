"""Gitea and Forgejo via REST, reusing the login stored by the `tea` CLI.

Token lookup order: GITEA_TOKEN / FORGEJO_TOKEN env vars, then the `tea` config login whose
URL matches the repo host. Dependencies come from `/issues/{n}/dependencies` plus `Blocked by:`
body lines; there are no sub-issues, so a parent board falls back to issues whose body mentions
`#<parent>`, plus, for a map, the implementation tickets of its spec (`Part of #<map>`), which name
the spec in their `## Parent` section. Open pull requests count as review when their body says `closes/fixes/resolves #<n>`.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from ..model import WONTFIX_LABEL, Ticket, parse_time
from . import (IssueStates, NotFound, ProviderError, Query, RateLimited, blocker_ref, map_family, resolve_maps,
               tracker_error, with_body_blockers)

PAGE = 50
CLOSING = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#{n}\b"


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


def error_message(exc: urllib.error.HTTPError) -> str:
    """The `message` of the tracker's JSON error body, when it sent one."""
    try:
        return str(json.loads(exc.read() or b"null").get("message") or "")
    except (ValueError, AttributeError, OSError):
        return ""


class GiteaProvider:
    name = "gitea"

    def __init__(self, repo: str, host: str, token: str | None = None, scheme: str = "https"):
        self.repo = repo
        self.host = host
        self.base = f"{scheme}://{host}/api/v1"
        self.token = token or find_token(host)
        self.states = IssueStates(self._issue_states)

    def viewer(self) -> str | None:
        return self._get("/user").get("login")

    def fetch(self, query: Query) -> list[Ticket]:
        since = datetime.now(timezone.utc) - timedelta(days=query.done_days)
        params = {"type": "issues"}
        if query.label:
            params["labels"] = query.label
        open_issues = self._list(f"/repos/{self.repo}/issues", {**params, "state": "open"}, query.limit)
        closed_params = {**params, "state": "closed", "since": since.isoformat()}
        closed_issues = self._list(f"/repos/{self.repo}/issues", closed_params, query.limit)
        raw = open_issues + closed_issues

        pulls = self._list(f"/repos/{self.repo}/pulls", {"state": "open"}, 200)
        pr_bodies = [f"{p.get('title', '')}\n{p.get('body') or ''}" for p in pulls]
        tickets = [to_ticket(i, None, pr_bodies) for i in raw]
        if query.parent is not None:
            tickets = map_family(tickets, query.parent, self._map(query.parent))
        open_numbers = [t.number for t in tickets if t.is_open]
        with ThreadPoolExecutor(max_workers=8) as pool:
            blockers = dict(zip(open_numbers, pool.map(self._open_blockers, open_numbers)))
        tickets = [with_native_blockers(t, blockers.get(t.number)) for t in tickets]
        return with_body_blockers(resolve_maps(tickets, self._map), self.states)

    def _map(self, number: int) -> tuple[str, tuple[str, ...]] | None:
        try:
            issue = self._get(f"/repos/{self.repo}/issues/{number}")
        except NotFound:
            return None
        return issue["html_url"], tuple(label["name"] for label in issue.get("labels") or [])

    def close(self, number: int) -> None:
        """Gitea has no close reason, so the `wontfix` label marks it not planned; labelled first, so a
        refused label leaves the issue open."""
        path = f"/repos/{self.repo}/issues/{number}"
        self._request("POST", f"{path}/labels", body={"labels": [WONTFIX_LABEL]})
        self._request("PATCH", path, body={"state": "closed"})

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
        return self._request("GET", path, params)

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, headers={"Accept": "application/json"}, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"token {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                answer = response.read()
                return json.loads(answer) if answer else None
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimited(f"{self.host}{path}: HTTP 429 rate limit exceeded", 429,
                                  resets_at=lifts_at(exc.headers or {})) from exc
            if exc.code == 404:
                raise NotFound(f"{self.host}{path}: HTTP 404 {exc.reason}") from exc
            said = error_message(exc)
            raise tracker_error(f"{self.host}{path}: HTTP {exc.code} {exc.reason}" + (f": {said}" if said else "")) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"{self.host}: {exc.reason}") from exc



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


def find_token(host: str) -> str | None:
    for var in ("GITEA_TOKEN", "FORGEJO_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    for config in tea_config_paths():
        if config.exists():
            token = token_from_tea_config(config.read_text(), host)
            if token:
                return token
    return None


def tea_config_paths() -> list[Path]:
    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return [xdg / "tea" / "config.yml", home / "Library" / "Application Support" / "tea" / "config.yml"]


def token_from_tea_config(text: str, host: str) -> str | None:
    import yaml

    data = yaml.safe_load(text) or {}
    for login in data.get("logins") or []:
        if urllib.parse.urlparse(login.get("url", "")).hostname == host and login.get("token"):
            return login["token"]
    return None
