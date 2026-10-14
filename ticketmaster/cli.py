"""Command line: `open` (the board in a herdr pane beside the caller, or right here when the caller is that
pane). It runs inside herdr (HERDR_ENV=1).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, herdr
from .providers import ProviderError, Query


COMMANDS = ("open",)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """No command means `open`, so `ticketmaster 42` is `ticketmaster open 42`; any other word is an invalid command."""
    parser = argparse.ArgumentParser(
        prog="ticketmaster",
        usage="ticketmaster [open] [parent] [options]",
        description="Live kanban board of issue-tracker work, in herdr.",
    )
    parser.add_argument("--version", action="store_true", help="print the version")
    parser.add_argument("command", choices=COMMANDS)
    # Read as text and converted below, so an unknown flag's value never fails as a parent before the flag is named.
    parser.add_argument("parent", nargs="?", help="parent issue: show its sub-issues")
    parser.add_argument("--repo", help="owner/name (default: from the git remote)")
    parser.add_argument("--provider", choices=["github", "gitlab", "gitea", "forgejo"])
    parser.add_argument("--host", help="tracker host, for self-hosted instances")
    parser.add_argument("--interval", type=float, default=5.0, help="refresh seconds (default 5)")
    parser.add_argument("--columns", choices=["auto", "one", "all"], default="auto",
                        help="one column at a time, all side by side, or auto by width (default)")
    if not argv or argv[0].startswith("-") or is_parent(argv[0]):
        argv = ["open", *argv]
    args = parser.parse_args(argv)
    if args.parent is not None:
        if not is_parent(args.parent):
            parser.error(f"argument parent: not an issue number or URL: {args.parent!r}")
        args.parent = parse_number(args.parent)
    return args


def parse_number(value: str) -> int:
    return int(value.lstrip("#").rsplit("/", 1)[-1])


def is_parent(value: str) -> bool:
    try:
        parse_number(value)
    except ValueError:
        return False
    return True


def passthrough(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    if args.parent is not None:
        out.append(str(args.parent))
    for flag in ("repo", "provider", "host"):
        if getattr(args, flag):
            out += [f"--{flag}", getattr(args, flag)]
    if args.columns != "auto":
        out += ["--columns", args.columns]
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
    from .config import resolve_provider

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
    from .start import build_prompt, workbench_for

    workbench = workbench_for(cwd, load_config(cwd))
    BoardApp(
        provider,
        Query(parent=args.parent),
        interval=args.interval,
        workbench=workbench,
        describe_start=lambda ticket: build_prompt(ticket, workbench.wayfinder_command)
        + f"\nworktree: issue-{ticket.number}, in its own herdr workspace",
        columns=args.columns,
        settings_path=settings_path(),
    ).run()
    return 0
