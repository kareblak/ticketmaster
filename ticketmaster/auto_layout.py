"""Auto layout: tile session panes around the board in one Ghostty tab, board top-left.

Ghostty splits subdivide a single pane, so the order is fixed to keep a two-row grid:

    1: B | 1       2: B | 1      3: B | 1      4: B | 1 | 4      5: B | 1 | 4
                      2 |           2 | 3         2 | 3            2 | 3 | 5

Session 1 splits the board right (full height), 2 splits the board down, 3 splits session 1
down, and from then on new sessions alternate: top row splits its last pane right, bottom row
splits its last pane right. Every split is followed by Ghostty's `equalize_splits`.

The board pane is the focused terminal when a session is started (the key press came from it).
If the user closes a tracked pane, the layout restarts from step 1 around the board.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .launch import applescript_string


class LayoutError(RuntimeError):
    pass


@dataclass
class GhosttyAutoLayout:
    board: str | None = None
    top: list[str] = field(default_factory=list)  # session panes in the top row, left to right
    bottom: list[str] = field(default_factory=list)  # bottom row, left to right (first is under the board)

    def next_split(self) -> tuple[str, str]:
        """(anchor terminal id, direction) for the next session pane."""
        count = len(self.top) + len(self.bottom)
        if count == 0:
            return self.board, "right"
        if count == 1:
            return self.board, "down"
        if count == 2:
            return self.top[0], "down"
        if len(self.top) <= len(self.bottom) - 1:
            return self.top[-1], "right"
        return self.bottom[-1], "right"

    def record(self, anchor: str, direction: str, new_id: str) -> None:
        count = len(self.top) + len(self.bottom)
        if count == 0 or (count >= 3 and anchor in self.top):
            self.top.append(new_id)
        else:
            self.bottom.append(new_id)

    def open(self, cwd: Path, command: str) -> str:
        board = osascript('tell application "Ghostty" to return id of focused terminal of selected tab of front window')
        tracked = self.top + self.bottom
        alive = all(exists(pane) for pane in tracked)
        if board != self.board and board not in tracked or not alive:
            self.board, self.top, self.bottom = board, [], []
        anchor, direction = self.next_split()
        config = f"{{command:{applescript_string(command)}, initial working directory:{applescript_string(str(cwd))}}}"
        new_id = osascript(
            'tell application "Ghostty"\n'
            f"set n to split (terminal id {applescript_string(anchor)}) direction {direction} with configuration {config}\n"
            'perform action "equalize_splits" on n\n'
            "return id of n\n"
            "end tell"
        )
        self.record(anchor, direction, new_id)
        return new_id


def exists(terminal_id: str) -> bool:
    script = f'tell application "Ghostty" to return exists terminal id {applescript_string(terminal_id)}'
    try:
        return osascript(script) == "true"
    except LayoutError:
        return False


def osascript(script: str) -> str:
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LayoutError(str(exc)) from exc
    if result.returncode != 0:
        raise LayoutError(result.stderr.strip())
    return result.stdout.strip()
