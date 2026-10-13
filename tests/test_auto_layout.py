from pathlib import Path

from ticketmaster import auto_layout
from ticketmaster.auto_layout import GhosttyAutoLayout


class FakeGhostty:
    """Stands in for osascript: tracks splits and which terminals exist."""

    def __init__(self, focused="B"):
        self.focused = focused
        self.alive = {focused}
        self.splits = []
        self.counter = 0

    def __call__(self, script):
        if "focused terminal" in script:
            return self.focused
        if "exists terminal id" in script:
            return "true" if script.split('"')[-2] in self.alive else "false"
        anchor = script.split('terminal id "')[1].split('"')[0]
        direction = script.split(") direction ")[1].split(" ")[0]
        assert "equalize_splits" in script
        self.counter += 1
        new = f"s{self.counter}"
        self.splits.append((anchor, direction, new))
        self.alive.add(new)
        return new


def test_grid_order_keeps_board_top_left(monkeypatch):
    fake = FakeGhostty()
    monkeypatch.setattr(auto_layout, "osascript", fake)
    layout = GhosttyAutoLayout()
    for _ in range(6):
        layout.open(Path("/repo"), "claude")
    assert fake.splits == [
        ("B", "right", "s1"),
        ("B", "down", "s2"),
        ("s1", "down", "s3"),
        ("s1", "right", "s4"),
        ("s3", "right", "s5"),
        ("s4", "right", "s6"),
    ]
    assert layout.top == ["s1", "s4", "s6"] and layout.bottom == ["s2", "s3", "s5"]


def test_closed_pane_restarts_layout_around_board(monkeypatch):
    fake = FakeGhostty()
    monkeypatch.setattr(auto_layout, "osascript", fake)
    layout = GhosttyAutoLayout()
    layout.open(Path("/repo"), "claude")
    layout.open(Path("/repo"), "claude")
    fake.alive.discard("s1")
    layout.open(Path("/repo"), "claude")
    assert fake.splits[-1] == ("B", "right", "s3")
    assert layout.top == ["s3"] and layout.bottom == []


def test_new_board_pane_resets_layout(monkeypatch):
    fake = FakeGhostty()
    monkeypatch.setattr(auto_layout, "osascript", fake)
    layout = GhosttyAutoLayout()
    layout.open(Path("/repo"), "claude")
    fake.focused = "B2"
    fake.alive.add("B2")
    layout.open(Path("/repo"), "claude")
    assert fake.splits[-1] == ("B2", "right", "s2")
