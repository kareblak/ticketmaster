"""Command line: `open` (the board in a herdr pane beside the caller, or right here when the caller is that
pane), `snapshot` (print once), `adopt` (open herdr workspaces for existing `claude -w` worktrees), `setup`
(write the wayfinding linking rules into the repo's issue-tracker doc), `upgrade` (reinstall from GitHub).

Everything but `snapshot`, `setup`, `install-skill` and `upgrade` runs inside herdr (HERDR_ENV=1).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import herdr
from .model import COLUMN_ORDER, build_board
from .providers import ProviderError, Query


COMMANDS = ("open", "snapshot", "adopt", "setup", "install-skill", "upgrade")
NEEDS_HERDR = ("open", "adopt")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ticketmaster",
        usage="ticketmaster [open|snapshot|adopt|setup|install-skill|upgrade] [parent] [options]",
        description="Live kanban board of issue-tracker work, in herdr.",
    )
    parser.add_argument("--version", action="store_true", help="print the version and the commit it was built from")
    parser.add_argument("parent", nargs="?", type=parse_number, help="parent issue: show its sub-issues")
    parser.add_argument("--label", help="show issues with this label")
    parser.add_argument("--repo", help="owner/name (default: from the git remote)")
    parser.add_argument("--provider", choices=["github", "gitlab", "gitea", "forgejo"])
    parser.add_argument("--host", help="tracker host, for self-hosted instances")
    parser.add_argument("--interval", type=float, default=5.0, help="refresh seconds (default 5)")
    parser.add_argument("--done-days", type=int, default=7, help="days closed issues stay in Done (default 7)")
    parser.add_argument("--limit", type=int, default=300, help="max issues per query (default 300)")
    parser.add_argument("--columns", choices=["auto", "one", "all"], default="auto",
                        help="one column at a time, all side by side, or auto by width (default)")
    command = argv[0] if argv and argv[0] in COMMANDS else None
    args = parser.parse_args(argv[1:] if command else argv)
    args.command = command or "open"
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
    if args.columns != "auto":
        out += ["--columns", args.columns]
    out += ["--interval", f"{args.interval:g}", "--done-days", str(args.done_days), "--limit", str(args.limit)]
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.version:
        from .upgrade import version_line

        print(version_line())
        return 0
    if args.command == "install-skill":
        from .install_skill import install_skill

        path = install_skill()
        print(f"Installed /ticketmaster skill at {path}. Restart Claude Code sessions to pick it up.")
        return 0
    if args.command == "upgrade":
        from . import upgrade

        return upgrade.upgrade(upgrade.installed())
    if args.command in NEEDS_HERDR and not herdr.available():
        print(f"ticketmaster: `{args.command}` runs inside herdr; start herdr and run it from a herdr pane", file=sys.stderr)
        return 2
    cwd = Path.cwd()
    from .config import resolve_provider

    try:
        provider = resolve_provider(cwd, args.provider, args.repo, args.host)
    except ProviderError as exc:
        print(f"ticketmaster: {exc}", file=sys.stderr)
        return 2
    if args.command == "setup":
        from .wayfinding import TRACKER_NAMES, normalize, setup

        path = setup(herdr.repo_root(cwd), provider.name)
        print(f"Wrote the {TRACKER_NAMES[normalize(provider.name)]} wayfinding operations to {path}.")
        return 0
    query = Query(parent=args.parent, label=args.label, done_days=args.done_days, limit=args.limit)

    if args.command == "open":
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
        # None: this is the board pane (a fresh one, or the user's), so the board runs here, below

    if args.command == "adopt":
        from .start import session_label, worktree_name

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

    from .board import BoardApp
    from .config import load_config, settings_path
    from .start import build_prompt, workbench_for

    workbench = workbench_for(cwd, load_config(cwd))
    BoardApp(
        provider,
        query,
        interval=args.interval,
        workbench=workbench,
        describe_start=lambda ticket: build_prompt(
            ticket, workbench.wayfinder_command, workbench.implement_command
        ).splitlines()[0]
        + f"\nworktree: issue-{ticket.number}, in its own herdr workspace",
        columns=args.columns,
        settings_path=settings_path(),
        repo_root=herdr.repo_root(cwd),
    ).run()
    return 0
