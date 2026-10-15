"""Pick a provider for the current repo from the git remote, `.ticketmaster.toml`, or flags.

Linear has no remote to detect it by: `.ticketmaster.toml` names it with `provider = "linear"` and `team = "<key>"`,
plus `key_env` when the API key is in another `LINEAR_` variable than LINEAR_API_KEY, or `oauth_client_id` for
`ticketmaster login`.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .providers import Provider, ProviderError

KNOWN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "codeberg.org": "gitea", "gitea.com": "gitea"}
CONFIG_FILE = ".ticketmaster.toml"
# Seconds between refreshes. Linear's key allows 2,500 requests an hour, shared by everything using the key's user.
DEFAULT_INTERVAL = {"linear": 30.0}


@dataclass(frozen=True)
class Remote:
    host: str
    repo: str


def parse_remote(url: str) -> Remote:
    """Accepts https, ssh (`git@host:o/r.git`) and `ssh://` remote URLs, including GitLab subgroups."""
    url = url.strip()
    match = re.match(r"^(?:[\w+.-]+://)?(?:[^@/]+@)?(?P<host>[^:/]+)(?::\d+)?[:/](?P<path>.+?)(?:\.git)?/?$", url)
    if not match:
        raise ProviderError(f"cannot parse git remote URL: {url}")
    return Remote(host=match["host"].lower(), repo=match["path"])


def guess_kind(host: str) -> str | None:
    if host in KNOWN_HOSTS:
        return KNOWN_HOSTS[host]
    for kind in ("github", "gitlab", "gitea", "forgejo"):
        if kind in host:
            return "gitea" if kind == "forgejo" else kind
    return None


def load_config(cwd: Path) -> dict:
    for directory in [cwd, *cwd.parents]:
        path = directory / CONFIG_FILE
        if path.exists():
            return tomllib.loads(path.read_text())
        if (directory / ".git").exists():
            break
    return {}


def settings_path() -> Path:
    """Per-user board settings changed from inside the board, e.g. the saved filters."""
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ticketmaster" / "settings.json"


def load_settings(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


def default_interval(kind: str) -> float:
    """The refresh interval when `--interval` is not given: 30 s on Linear, 5 s elsewhere."""
    return DEFAULT_INTERVAL.get(kind, 5.0)


def parse_interval(text: str) -> float:
    """Refresh seconds typed in the settings menu: any positive number, no floor; ValueError says what is wrong."""
    try:
        seconds = float(text)
    except ValueError:
        raise ValueError(f"not a number: {text.strip()!r}") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"must be a positive number of seconds: {text.strip()!r}")
    return seconds


def board_interval(flag: float | None, saved: dict, kind: str) -> float:
    """`--interval` for this run, else the repo's saved interval (`saved` is its `repos` entry in settings.json),
    else the provider's default. A saved value that is no positive number is ignored."""
    if flag is not None:
        return flag
    value = saved.get("interval")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return parse_interval(str(value))
        except ValueError:
            pass
    return default_interval(kind)


def git_remote(cwd: Path, name: str) -> str:
    result = subprocess.run(["git", "remote", "get-url", name], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ProviderError(f"no git remote '{name}' here; pass --repo and --provider")
    return result.stdout


def resolve_provider(
    cwd: Path,
    provider: str | None = None,
    repo: str | None = None,
    host: str | None = None,
) -> Provider:
    """Flags win over `.ticketmaster.toml`, which wins over the detected `origin` remote."""
    config = load_config(cwd)
    if (provider or config.get("provider")) == "linear":
        return linear_provider(config)
    remote = None
    if not repo and not config.get("repo"):
        remote = parse_remote(git_remote(cwd, config.get("remote", "origin")))
    repo = repo or config.get("repo") or remote.repo
    host = host or config.get("host") or (remote.host if remote else None)
    kind = provider or config.get("provider") or (guess_kind(host) if host else None)
    if kind is None:
        raise ProviderError(f"cannot tell which tracker {host} is; set provider in {CONFIG_FILE} or pass --provider")
    return make_provider(kind, repo, host)


def linear_provider(config: dict) -> Provider:
    """The team's Linear provider; without a team, the error holds the lines to add."""
    if not config.get("team"):
        raise ProviderError(f'Linear has no repo to detect its team from; add to {CONFIG_FILE}:\n\n'
                            f'provider = "linear"\nteam = "<key>"  # the team\'s issue key: TM for TM-12\n')
    from .providers.linear import LinearProvider

    client_id = config.get("oauth_client_id")
    return LinearProvider(str(config["team"]), config.get("key_env"),
                          oauth_client_id=str(client_id) if client_id else None)


def make_provider(kind: str, repo: str, host: str | None) -> Provider:
    if kind == "github":
        from .providers.github import GitHubProvider

        return GitHubProvider(repo, host or "github.com")
    if kind == "gitlab":
        from .providers.gitlab import GitLabProvider

        return GitLabProvider(repo, host or "gitlab.com")
    if kind in ("gitea", "forgejo"):
        from .providers.gitea import GiteaProvider

        if not host:
            raise ProviderError("gitea/forgejo needs a host")
        return GiteaProvider(repo, host)
    raise ProviderError(f"unknown provider '{kind}' (github, gitlab, gitea, forgejo, linear)")
