"""Gitea and Forgejo via REST, reusing the login stored by the `tea` CLI.

Token lookup order: GITEA_TOKEN / FORGEJO_TOKEN env vars, then the `tea` config login whose
URL matches the repo host. Dependencies come from `/issues/{n}/dependencies`; there are no
sub-issues, so a parent board falls back to issues whose body mentions `#<parent>`, plus, for a
map, the implementation tickets of its spec (`Part of #<map>`), which name the spec in their
`## Parent` section. Open
pull requests count as review when their body says `closes/fixes/resolves #<n>`.
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
from pathlib import Path

from ..model import Ticket, parse_time
from . import NotFound, ProviderError, Query, RateLimited, map_family, resolve_maps, tracker_error

PAGE = 50
CLOSING = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#{n}\b"


class GiteaProvider:
    name = "gitea"

    def __init__(self, repo: str, host: str, token: str | None = None, scheme: str = "https"):
        self.repo = repo
        self.host = host
        self.base = f"{scheme}://{host}/api/v1"
        self.token = token or find_token(host)

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
        tickets = [replace(t, open_blockers=blockers.get(t.number)) for t in tickets]
        return resolve_maps(tickets, self._map)

    def _map(self, number: int) -> tuple[str, tuple[str, ...]] | None:
        try:
            issue = self._get(f"/repos/{self.repo}/issues/{number}")
        except NotFound:
            return None
        return issue["html_url"], tuple(label["name"] for label in issue.get("labels") or [])

    def delete(self, number: int) -> None:
        self._request("DELETE", f"/repos/{self.repo}/issues/{number}")

    def _open_blockers(self, number: int) -> int | None:
        try:
            deps = self._get(f"/repos/{self.repo}/issues/{number}/dependencies")
        except RateLimited:
            raise
        except ProviderError:
            return None  # dependencies disabled for this repo
        return sum(1 for d in deps if d.get("state") == "open")

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

    def _request(self, method: str, path: str, params: dict | None = None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method=method)
        if self.token:
            request.add_header("Authorization", f"token {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None  # DELETE answers 204 No Content
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimited(f"{self.host}{path}: HTTP 429 rate limit exceeded") from exc
            if exc.code == 404:
                raise NotFound(f"{self.host}{path}: HTTP 404 {exc.reason}") from exc
            raise tracker_error(f"{self.host}{path}: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"{self.host}: {exc.reason}") from exc



def to_ticket(issue: dict, open_blockers: int | None, pr_bodies: list[str]) -> Ticket:
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
        open_blockers=open_blockers,
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
