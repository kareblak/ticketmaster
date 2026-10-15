"""Command line: `open` (the board in a herdr pane beside the caller, or right here when the caller is that
pane). It runs inside herdr (HERDR_ENV=1).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import __version__, herdr
from .providers import ProviderError, Query


COMMANDS = ("open",)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """No command means `open`, so `ticketmaster 42` is `ticketmaster open 42`; any other word is an invalid command."""
    parser = argparse.ArgumentParser(
        prog="ticketmaster",
        usage="ticketmaster [open] [map] [options]",
        description="Live swimlane board of wayfinder maps, in herdr.",
    )
    parser.add_argument("--version", action="store_true", help="print the version")
    parser.add_argument("command", choices=COMMANDS)
    # Read as text and converted below, so an unknown flag's value never fails as a map before the flag is named.
    parser.add_argument("map", nargs="?", help="a map issue to show, also when it is closed (42, #42, TM-1 or a URL)")
    parser.add_argument("--repo", help="owner/name (default: from the git remote)")
    parser.add_argument("--provider", choices=["github", "gitlab", "gitea", "forgejo", "linear"])
    parser.add_argument("--host", help="tracker host, for self-hosted instances")
    parser.add_argument("--interval", type=float, help="refresh seconds (default 5, 30 on Linear)")
    parser.add_argument("--columns", choices=["auto", "one", "all"], default="auto",
                        help="one column at a time, all side by side, or auto by width (default)")
    if not argv or argv[0].startswith("-") or is_issue(argv[0]):
        argv = ["open", *argv]
    args = parser.parse_args(argv)
    if args.map is not None:
        if not is_issue(args.map):
            parser.error(f"argument map: not an issue number or URL: {args.map!r}")
        args.map = parse_number(args.map)
    return args


# A Linear key, `TM-1`, or the end of its issue URL, `…/issue/TM-1/<slug>`.
_KEY = re.compile(r"(?:^|/issue/)[A-Za-z][A-Za-z0-9]*-(?P<number>\d+)(?:/[^/]*)?$")


def parse_number(value: str) -> int:
    if match := _KEY.search(value):
        return int(match.group("number"))
    return int(value.lstrip("#").rsplit("/", 1)[-1])


def is_issue(value: str) -> bool:
    try:
        parse_number(value)
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.version:
        print(f"ticketmaster {__version__}")
        return 0
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
        Query(named_map=args.map),
        interval=args.interval if args.interval is not None else default_interval(provider.name),
        workbench=workbench,
        describe_start=lambda ticket: workbench.prompt(ticket)
        + f"\nworktree: {worktree_name(ticket)}, in its own herdr workspace",
        columns=args.columns,
        settings_path=settings_path(),
    ).run()
    return 0
