"""Fakes the board tests share: a tracker, a session workbench and ticket builders."""

from ticketmaster.model import Ticket


class FakeProvider:
    name = "fake"
    repo = "o/r"

    def __init__(self, tickets, login="me"):
        self.tickets = tickets
        self.calls = 0
        self.queries = []
        self.login = login

    def viewer(self):
        if isinstance(self.login, Exception):
            raise self.login
        return self.login

    def fetch(self, query):
        self.calls += 1
        self.queries.append(query)
        if isinstance(self.tickets, Exception):
            raise self.tickets
        return self.tickets


class FakeBench:
    """The board's Workbench, faked: records its session actions; `sessions` is what the backend reports."""

    def __init__(self, sessions=None):
        self.calls = []
        self.current = sessions or {}

    def sessions(self):
        return self.current

    def start(self, ticket):
        self.calls.append(("start", ticket.number))
        return f"Started #{ticket.number}"

    def go_to(self, ticket, session):
        self.calls.append(("go_to", ticket.number, session.path))
        return "ok"


def a_map(number, title="", **kw):
    base = dict(number=number, title=title or f"Map {number}", url=f"https://gh/o/r/issues/{number}", is_open=True,
                labels=("wayfinder:map",))
    return Ticket(**{**base, **kw})


def on(map_number, number, title="", kind="grilling", **kw):
    """A decision ticket of map #<map_number>."""
    base = dict(number=number, title=title or f"Question {number}", url=f"u{number}", is_open=True,
                labels=(f"wayfinder:{kind}",), parent_number=map_number,
                parent_url=f"https://gh/o/r/issues/{map_number}", parent_labels=("wayfinder:map",))
    return Ticket(**{**base, **kw})


def spec_of(map_number, number, title="", **kw):
    """A spec of map #<map_number>: `Part of #<map>`, no `wayfinder:` label, and the map as its parent."""
    map_url = f"https://gh/o/r/issues/{map_number}"
    base = dict(number=number, title=title or f"Spec {number}", url=f"u{number}", is_open=True,
                body=f"Part of #{map_number}\n\n## Problem Statement", labels=("ready-for-agent",),
                parent_number=map_number, parent_url=map_url, parent_labels=("wayfinder:map",),
                map_number=map_number, map_url=map_url)
    return Ticket(**{**base, **kw})


def impl_of(spec, number, title="", **kw):
    """An implementation ticket of `spec`, linked by its `## Parent` section."""
    base = dict(number=number, title=title or f"Slice {number}", url=f"u{number}", is_open=True,
                body=f"## Parent\n\n#{spec.number}\n\n## What to build", map_number=spec.map_number,
                map_url=spec.map_url)
    return Ticket(**{**base, **kw})
