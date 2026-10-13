"""Command line: `open` (new window), `board` (run here), `snapshot` (print once)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .model import COLUMN_ORDER, build_board
from .providers import ProviderError, Query


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ticketmaster", description="Live kanban board of issue-tracker work.")
    parser.add_argument("command", nargs="?", default="open", choices=["open", "board", "snapshot"])
    parser.add_argument("parent", nargs="?", type=parse_number, help="parent issue: show its sub-issues")
    parser.add_argument("--label", help="show issues with this label")
    parser.add_argument("--repo", help="owner/name (default: from the git remote)")
    parser.add_argument("--provider", choices=["github", "gitlab", "gitea", "forgejo"])
    parser.add_argument("--host", help="tracker host, for self-hosted instances")
    parser.add_argument("--interval", type=float, default=5.0, help="refresh seconds (default 5)")
    parser.add_argument("--done-days", type=int, default=7, help="days closed issues stay in Done (default 7)")
    parser.add_argument("--limit", type=int, default=300, help="max issues per query (default 300)")
    return parser.parse_args(argv)


def parse_number(value: str) -> int:
    return int(value.lstrip("#").rsplit("/", 1)[-1])


def passthrough(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    if args.parent is not None:
        out.append(str(args.parent))
    for flag in ("label", "repo", "provider", "host"):
        if getattr(args, flag):
            out += [f"--{flag}", getattr(args, flag)]
    out += ["--interval", f"{args.interval:g}", "--done-days", str(args.done_days), "--limit", str(args.limit)]
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cwd = Path.cwd()
    from .config import resolve_provider

    try:
        provider = resolve_provider(cwd, args.provider, args.repo, args.host)
    except ProviderError as exc:
        print(f"ticketmaster: {exc}", file=sys.stderr)
        return 2
    query = Query(parent=args.parent, label=args.label, done_days=args.done_days, limit=args.limit)

    if args.command == "open":
        from .launch import board_command, open_window

        entrypoint = Path(os.environ.get("TICKETMASTER_BIN", sys.argv[0])).resolve()
        command = board_command(entrypoint, passthrough(args))
        terminal = open_window(cwd, command)
        if terminal:
            print(f"Board opened in a new {terminal} window for {provider.name}:{provider.repo}.")
        else:
            print(f"No terminal launcher worked. Run this in another window:\n  {command}")
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

    from .board import BoardApp

    BoardApp(provider, query, interval=args.interval).run()
    return 0
