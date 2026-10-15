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

from .providers import LIMIT, Provider, ProviderError

KNOWN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "codeberg.org": "gitea", "gitea.com": "gitea"}
CONFIG_FILE = ".ticketmaster.toml"
# Seconds between refreshes. Linear's key allows 2,500 requests an hour, shared by everything using the key's user.
DEFAULT_INTERVAL = {"linear": 30.0}
# GraphQL points an hour a GitHub board aims to spend of the token's 5,000, leaving the rest to agents' `gh` calls.
DEFAULT_BUDGET = 1000.0


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
    return parse_positive(text, "seconds")


def parse_budget(text: str) -> float:
    """GitHub points an hour typed in the settings menu: any positive number; ValueError says what is wrong."""
    return parse_positive(text, "points")


def parse_positive(text: str, unit: str) -> float:
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"not a number: {text.strip()!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"must be a positive number of {unit}: {text.strip()!r}")
    return number


def seconds_text(seconds: float) -> str:
    """An interval as typed and shown, exactly: `10`, `0.5`, `1234567` (`:g` would round it)."""
    return str(int(seconds)) if seconds.is_integer() else repr(seconds)


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


def board_budget(saved: dict) -> float:
    """The repo's saved GitHub budget in points an hour (`saved` is its `repos` entry), else 1,000. A saved value
    that is no positive number is ignored."""
    value = saved.get("budget")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return parse_budget(str(value))
        except ValueError:
            pass
    return DEFAULT_BUDGET


def parse_fetch_limit(text: str) -> int:
    """The fetch limit typed in the settings menu: a whole number, 1 or more; ValueError says what is wrong."""
    if not (text.strip().isascii() and text.strip().isdigit()):
        raise ValueError(f"not a whole number: {text.strip()!r}")
    limit = int(text)
    if limit <= 0:
        raise ValueError(f"must be 1 or more: {text.strip()!r}")
    return limit


def board_fetch_limit(saved: dict) -> int:
    """The repo's saved fetch limit (`saved` is its `repos` entry in settings.json), else the default. A saved value
    that is no positive whole number is ignored."""
    value = saved.get("fetch_limit")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return LIMIT


def parse_decided_age(text: str) -> int | None:
    """The Decided age typed in the settings menu: a whole number of days, 0 or more, or empty for off (None);
    ValueError says what is wrong."""
    text = text.strip()
    if not text:
        return None
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"not a whole number of days, 0 or more: {text!r}")
    return int(text)


def board_decided_age(saved: dict) -> int | None:
    """The repo's saved Decided age in days (`saved` is its `repos` entry in settings.json), else None: every
    Decided ticket shown. A saved value that is no whole number, 0 or more, is ignored."""
    value = saved.get("decided_age")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


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
