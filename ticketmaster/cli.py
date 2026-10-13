"""Command line: `open` (split beside the caller), `board` (run here), `snapshot` (print once),
`adopt` (open herdr workspaces for existing `claude -w` worktrees).

With no command, a terminal runs the board in place; anything else (e.g. Claude Code's shell,
which has no TTY) opens it beside the caller.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .model import COLUMN_ORDER, build_board
from .providers import ProviderError, Query


COMMANDS = ("open", "board", "snapshot", "adopt", "install-skill")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ticketmaster",
        usage="ticketmaster [open|board|snapshot|adopt|install-skill] [parent] [options]",
        description="Live kanban board of issue-tracker work.",
    )
    parser.add_argument("parent", nargs="?", type=parse_number, help="parent issue: show its sub-issues")
    parser.add_argument("--label", help="show issues with this label")
    parser.add_argument("--repo", help="owner/name (default: from the git remote)")
    parser.add_argument("--provider", choices=["github", "gitlab", "gitea", "forgejo"])
    parser.add_argument("--host", help="tracker host, for self-hosted instances")
    parser.add_argument("--interval", type=float, default=5.0, help="refresh seconds (default 5)")
    parser.add_argument("--done-days", type=int, default=7, help="days closed issues stay in Done (default 7)")
    parser.add_argument("--limit", type=int, default=300, help="max issues per query (default 300)")
    parser.add_argument("--no-worktree", action="store_true", help="start sessions without `claude -w`")
    parser.add_argument("--columns", choices=["auto", "one", "all"], default="auto",
                        help="one column at a time, all side by side, or auto by width (default)")
    command = argv[0] if argv and argv[0] in COMMANDS else None
    args = parser.parse_args(argv[1:] if command else argv)
    args.command = command
    return args


def parse_number(value: str) -> int:
    return int(value.lstrip("#").rsplit("/", 1)[-1])


def passthrough(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    if args.parent is not None:
        out.append(str(args.parent))
    for flag in ("label", "repo", "provider", "host"):
        if getattr(args, flag):
            out += [f"--{flag}", getattr(args, flag)]
    if args.no_worktree:
        out.append("--no-worktree")
    if args.columns != "auto":
        out += ["--columns", args.columns]
    out += ["--interval", f"{args.interval:g}", "--done-days", str(args.done_days), "--limit", str(args.limit)]
    return out


def default_command() -> str:
    return "board" if sys.stdin.isatty() and sys.stdout.isatty() else "open"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    args.command = args.command or default_command()
    if args.command == "install-skill":
        from .install_skill import install_skill

        path = install_skill()
        print(f"Installed /ticketmaster skill at {path}. Restart Claude Code sessions to pick it up.")
        return 0
    cwd = Path.cwd()
    from .config import resolve_provider

    try:
        provider = resolve_provider(cwd, args.provider, args.repo, args.host)
    except ProviderError as exc:
        print(f"ticketmaster: {exc}", file=sys.stderr)
        return 2
    query = Query(parent=args.parent, label=args.label, done_days=args.done_days, limit=args.limit)

    if args.command == "open":
        from . import herdr
        from .launch import Placement, board_command, describe, open_beside

        command = board_command(passthrough(args))
        if herdr.available():
            try:
                pane = herdr.Herdr().open_board(cwd, command)
                print(f"Board opened in herdr pane {pane} ({herdr.BOARD_LABEL}) for {provider.name}:{provider.repo}.")
                return 0
            except herdr.HerdrError as exc:
                print(f"ticketmaster: herdr failed ({exc}); falling back to the host terminal", file=sys.stderr)
        launch = open_beside(cwd, command, Placement.SPLIT_RIGHT)
        if launch:
            print(f"Board opened in {describe(launch)} for {provider.name}:{provider.repo}.")
        else:
            print(f"No terminal launcher worked. Run this in another window:\n  {command}")
        return 0

    if args.command == "adopt":
        from . import herdr
        from .start import session_label, worktree_name

        if not herdr.available():
            print("ticketmaster: adopt needs herdr (run it from a herdr pane)", file=sys.stderr)
            return 2
        try:
            labels = {worktree_name(t): session_label(t) for t in provider.fetch(query)}
        except ProviderError:
            labels = {}  # titles are a nicety; open the workspaces with bare issue-<n> labels
        try:
            opened = herdr.Herdr().adopt(cwd, labels)
        except herdr.HerdrError as exc:
            print(f"ticketmaster: {exc}", file=sys.stderr)
            return 1
        print(f"Opened {len(opened)} herdr workspace(s): {', '.join(opened)}" if opened else "No unopened .claude/worktrees checkouts.")
        return 0

    if args.command == "snapshot":
        try:
            board = build_board(provider.fetch(query), done_days=query.done_days)
        except ProviderError as exc:
            print(f"ticketmaster: {exc}", file=sys.stderr)
            return 1
        print(f"{provider.name}:{provider.repo}")
        for column in COLUMN_ORDER:
            tickets = board.columns[column]
            print(f"\n{column.value} ({len(tickets)})")
            for t in tickets:
                tags = "".join(f" [{k}]" for k in t.review_tags)
                who = " " + ",".join("@" + a for a in t.assignees) if t.assignees else ""
                print(f"  #{t.number} {t.title}{tags}{who}")
        return 0

    from . import herdr
    from .board import DEFAULT_PLACEMENTS, HERDR_PLACEMENTS, BoardApp
    from .config import load_config
    from .start import DEFAULT_WAYFINDER_COMMAND, StartOptions, build_prompt, start_session

    config = load_config(cwd)
    options = StartOptions(
        cwd=cwd,
        worktree=not args.no_worktree and config.get("worktree", True),
        wayfinder_command=config.get("wayfinder_command", DEFAULT_WAYFINDER_COMMAND),
    )
    in_herdr = herdr.available()
    worktree_note = "worktree: issue-{n} (w: its own herdr workspace)" if in_herdr else "worktree: issue-{n}"
    BoardApp(
        provider,
        query,
        interval=args.interval,
        starter=lambda ticket, placement: start_session(ticket, options, placement),
        describe_start=lambda ticket: build_prompt(ticket, options.wayfinder_command).splitlines()[0]
        + ("\n" + worktree_note.format(n=ticket.number) if options.worktree else ""),
        placements=HERDR_PLACEMENTS if in_herdr else DEFAULT_PLACEMENTS,
        columns=args.columns,
    ).run()
    return 0
