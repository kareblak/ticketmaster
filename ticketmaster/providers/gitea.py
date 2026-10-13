"""Gitea and Forgejo via REST, reusing the login stored by the `tea` CLI.

Token lookup order: GITEA_TOKEN / FORGEJO_TOKEN env vars, then the `tea` config login whose
URL matches the repo host. Dependencies come from `/issues/{n}/dependencies`; there are no
sub-issues, so a parent board falls back to issues whose body mentions `#<parent>`. Open
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
from . import ProviderError, Query, mentions_parent

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

        if query.parent is not None:
            raw = [i for i in raw if i["number"] != query.parent and mentions_parent(i.get("body", ""), query.parent)]
        elif not query.label:
            me = self._get("/user")["login"]
            raw = [i for i in raw if is_mine_or_unassigned(i, me)]

        pulls = self._list(f"/repos/{self.repo}/pulls", {"state": "open"}, 200)
        pr_bodies = [f"{p.get('title', '')}\n{p.get('body') or ''}" for p in pulls]
        open_numbers = [i["number"] for i in raw if i["state"] == "open"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            blockers = dict(zip(open_numbers, pool.map(self._open_blockers, open_numbers)))
        tickets = [to_ticket(i, blockers.get(i["number"]), pr_bodies) for i in raw]
        if query.parent is not None:
            parent = self._get(f"/repos/{self.repo}/issues/{query.parent}")
            labels = tuple(label["name"] for label in parent.get("labels") or [])
            tickets = [
                replace(t, parent_number=query.parent, parent_url=parent["html_url"], parent_labels=labels)
                for t in tickets
            ]
        return tickets

    def _open_blockers(self, number: int) -> int | None:
        try:
            deps = self._get(f"/repos/{self.repo}/issues/{number}/dependencies")
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
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.token:
            request.add_header("Authorization", f"token {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"{self.host}{path}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"{self.host}: {exc.reason}") from exc


def is_mine_or_unassigned(issue: dict, me: str) -> bool:
    assignees = [a["login"] for a in issue.get("assignees") or []]
    if issue["state"] == "open":
        return not assignees or me in assignees
    return me in assignees


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
