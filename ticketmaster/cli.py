"""Command line: `open` (the board in a herdr pane beside the caller, or right here when the caller is that
pane), which runs inside herdr (HERDR_ENV=1), and `login` (Linear OAuth, in the browser), which runs anywhere.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import __version__, herdr
from .providers import ProviderError, Query


COMMANDS = ("open", "login")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """No command means `open`, so `ticketmaster 42` is `ticketmaster open 42`; any other word is an invalid command."""
    parser = argparse.ArgumentParser(
        prog="ticketmaster",
        usage="ticketmaster [open] [map] [options] | ticketmaster login",
        description="Live swimlane board of wayfinder maps, in herdr.",
    )
    parser.add_argument("--version", action="store_true", help="print the version")
    parser.add_argument("command", choices=COMMANDS)
    # Read as text and converted below, so an unknown flag's value never fails as a map before the flag is named.
    parser.add_argument("map", nargs="?", help="a map to filter the board to (42, #42, TM-1 or its URL), shown also when closed")
    parser.add_argument("--repo", help="owner/name (default: from the git remote)")
    parser.add_argument("--provider", choices=["github", "gitlab", "gitea", "forgejo", "linear"])
    parser.add_argument("--host", help="tracker host, for self-hosted instances")
    parser.add_argument("--interval", type=float, help="refresh seconds (default 5, 30 on Linear)")
    parser.add_argument("--columns", choices=["auto", "one", "all"], default="auto",
                        help="one column at a time, all side by side, or auto by width (default)")
    if not argv or argv[0].startswith("-") or is_issue(argv[0]):
        argv = ["open", *argv]
    args = parser.parse_args(argv)
    if args.command == "login" and args.map is not None:
        parser.error("login takes no map")
    if args.map is not None:
        if not is_issue(args.map):
            parser.error(f"argument map: not an issue number, key or URL: {args.map!r}")
        args.map = parse_map(args.map)
    return args


_KEY = r"[A-Za-z][A-Za-z0-9]*-\d+"  # a tracker key, e.g. Linear's `TM-1`
_KEY_OR_NUMBER = re.compile(rf"^(?P<key>{_KEY})$|^#?(?P<number>\d+)$")
# An issue URL: Linear `/issue/TM-1/<slug>`, GitHub/Gitea `/issues/42`, GitLab `/-/issues/42`, else a URL ending
# in the number (GitLab `/-/work_items/42`).
_ISSUE_URL = re.compile(rf"/issue/(?P<key>{_KEY})(?:[/?#]|$)|/issues/(?P<number>\d+)(?:[/?#]|$)|/(?P<last>\d+)/?$")


def parse_map(value: str) -> int | str:
    """The map's number from `42`, `#42` or an issue URL, or its key (`TM-1`, upper-cased) from the key or a Linear
    URL; ValueError for anything else. A key matches the map card with that tracker key, never issue number 1."""
    match = _ISSUE_URL.search(value) if "://" in value else _KEY_OR_NUMBER.match(value)
    if match is None:
        raise ValueError(f"not an issue: {value!r}")
    if match.group("key"):
        return match.group("key").upper()
    return int(match.group("number") or match.group("last"))


def named_number(named: int | str | None, provider) -> int | None:
    """The map number the provider is asked for, also when closed: a number, or a key of the Linear team the board
    reads (`TM-1` is the team's issue 1). Another key is matched on the board alone."""
    if isinstance(named, int):
        return named
    if named is not None and provider.name == "linear":
        prefix, _, number = named.rpartition("-")
        return int(number) if prefix == provider.team.upper() else None
    return None


def is_issue(value: str) -> bool:
    try:
        parse_map(value)
    except ValueError:
        return False
    return True


def passthrough(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    if args.map is not None:
        out.append(str(args.map))
    for flag in ("repo", "provider", "host"):
        if getattr(args, flag):
            out += [f"--{flag}", getattr(args, flag)]
    if args.columns != "auto":
        out += ["--columns", args.columns]
    if args.interval is not None:
        out += ["--interval", f"{args.interval:g}"]
    return out


def login(cwd: Path, provider: str | None) -> int:
    """Linear's OAuth login for `oauth_client_id`; without one, prints the line to add. The other trackers log in
    with their own CLIs."""
    from .config import CONFIG_FILE, load_config
    from .providers import linear_oauth

    config = load_config(cwd)
    if (provider or config.get("provider")) != "linear":
        print("ticketmaster: login is for Linear (provider = \"linear\" in .ticketmaster.toml); the other trackers "
              "are read with their own CLI's login: `gh auth login`, `glab auth login` or `tea login add`",
              file=sys.stderr)
        return 2
    client_id = config.get("oauth_client_id")
    if not client_id:
        print(f"ticketmaster: login needs a Linear OAuth app of your workspace (Linear: Settings, API, OAuth "
              f"applications; callback URL {linear_oauth.CALLBACK_URL}); add its client id to {CONFIG_FILE}:\n\n"
              f'oauth_client_id = "<client id>"\n', file=sys.stderr)
        return 2
    try:
        store = linear_oauth.login(str(client_id))
    except ProviderError as exc:
        print(f"ticketmaster: {exc}", file=sys.stderr)
        return 1
    print(f"Logged in to Linear; the board reads with your OAuth token, stored in {store.path}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.version:
        print(f"ticketmaster {__version__}")
        return 0
    if args.command == "login":
        return login(Path.cwd(), args.provider)
    if not herdr.available():
        print(f"ticketmaster: `{args.command}` runs inside herdr; start herdr and run it from a herdr pane", file=sys.stderr)
        return 2
    cwd = Path.cwd()
    from .config import default_interval, resolve_provider

    try:
        provider = resolve_provider(cwd, args.provider, args.repo, args.host)
    except ProviderError as exc:
        print(f"ticketmaster: {exc}", file=sys.stderr)
        return 2
    try:
        board = herdr.Herdr().open_board(
            cwd, herdr.open_command(passthrough(args)), os.environ.get("HERDR_PANE_ID"), os.environ.get("CLAUDE_CODE_SESSION_ID"),
        )
    except herdr.HerdrError as exc:
        print(f"ticketmaster: herdr: {exc}", file=sys.stderr)
        return 1
    if board is not None:
        how = f"restarted in {board.beside}'s workspace" if board.restarted else f"split off {board.beside}"
        print(f"Board opened in herdr pane {board.pane} ({herdr.BOARD_LABEL}), {how}, for {provider.name}:{provider.repo}.")
        return 0
    # None: this is the board pane (a fresh one, or the user's), so the board runs here

    from .board import BoardApp
    from .config import load_config, settings_path
    from .start import workbench_for, worktree_name

    workbench = workbench_for(cwd, load_config(cwd))
    BoardApp(
        provider,
        Query(named_map=named_number(args.map, provider)),
        interval=args.interval if args.interval is not None else default_interval(provider.name),
        workbench=workbench,
        describe_start=lambda ticket: workbench.prompt(ticket)
        + f"\nworktree: {worktree_name(ticket)}, in its own herdr workspace",
        columns=args.columns,
        settings_path=settings_path(),
        map_key=args.map if isinstance(args.map, str) else None,
    ).run()
    return 0
