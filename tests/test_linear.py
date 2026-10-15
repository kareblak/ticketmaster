import io
import json
import urllib.error
from datetime import datetime, timezone
from email.message import Message

import pytest

from fake_linear import FakeLinear
from ticketmaster.model import Column, build_board
from ticketmaster.providers import ProviderError, Query, RateLimited
from ticketmaster.providers import linear
from ticketmaster.providers.linear import LinearHTTP, LinearProvider


def board(fake, named_map=None):
    tickets = LinearProvider("TM", transport=fake).fetch(Query(named_map=named_map))
    return {t.number: t for t in tickets}


def lanes(tickets, named_map=None):
    """{map: {SPECS: [spec numbers], column: [ticket numbers]}}, an empty one left out."""
    return {lane.header.number: {**({"SPECS": [t.number for t in lane.specs]} if lane.specs else {}),
                              **{column.name: [t.number for t in cards]
                                 for column, cards in lane.columns.items() if cards}}
            for lane in build_board(list(tickets), named_map).lanes}


def test_maps_and_their_sub_issues_in_sub_issue_order_with_labels_assignee_and_state():
    fake = FakeLinear()
    the_map = fake.add_issue("Pick a stack", "Notes", labels=["wayfinder:map"], state="In Progress")
    first = fake.add_issue("Which DB?", parent=the_map, labels=["wayfinder:grilling"], sort_order=-5.5)
    claimed = fake.add_issue("Survey", parent=the_map, labels=["wayfinder:research", "ux", "web"], sort_order=20,
                             assignee="kaare.blakstad", state="In Progress")
    delegated = fake.add_issue("Delegated", parent=the_map, labels=["wayfinder:task"], sort_order=10,
                               delegate="claude")
    done = fake.add_issue("Done", parent=the_map, labels=["wayfinder:task"], state="Done", sort_order=10)
    duplicate = fake.add_issue("Dup", parent=the_map, labels=["wayfinder:task"], state="Duplicate", sort_order=30)
    canceled = fake.add_issue("No", parent=the_map, labels=["wayfinder:task"], state="Canceled", sort_order=40)
    fake.add_issue("Plain bug", labels=["bug"])
    fake.add_issue("Old map", labels=["wayfinder:map"], state="Done")

    tickets = board(fake)

    assert set(tickets) == {n["number"] for n in (the_map, first, claimed, delegated, done, duplicate, canceled)}
    child = tickets[claimed["number"]]
    assert child.labels == ("wayfinder:research", "ux", "web")  # past the first page of labels
    assert (child.assignees, child.is_open, child.is_wayfinder_ticket) == (("kaare.blakstad",), True, True)
    assert (child.key, child.branch_name) == (f"TM-{claimed['number']}", f"kaare/tm-{claimed['number']}-slug")
    assert child.url == f"https://linear.app/fabricskill/issue/TM-{claimed['number']}/slug"
    assert (child.parent_number, child.parent_labels, child.author) == (the_map["number"], ("wayfinder:map",), "kaare")
    assert tickets[delegated["number"]].assignees == ()  # the delegate is no claim
    assert (tickets[done["number"]].is_open, tickets[done["number"]].not_planned) == (False, False)
    assert tickets[done["number"]].closed_at == datetime(2026, 10, 15, 9, tzinfo=timezone.utc)
    for closed in (duplicate, canceled):
        assert (tickets[closed["number"]].is_open, tickets[closed["number"]].not_planned) == (False, True)
    # Sub-issue order, then number: -5.5, 10 (#delegated before #done by number), 20, 30, 40.
    assert lanes(tickets.values()) == {the_map["number"]: {
        "FRONTIER": [first["number"], delegated["number"]], "CLAIMED": [claimed["number"]],
        "DECIDED": [done["number"], duplicate["number"], canceled["number"]]}}


def test_blockers_are_the_open_issues_blocking_it_by_key():
    fake = FakeLinear()
    the_map = fake.add_issue("Map", labels=["wayfinder:map"])
    blocker = fake.add_issue("Blocker", parent=the_map, labels=["wayfinder:task"])
    finished = fake.add_issue("Finished", parent=the_map, labels=["wayfinder:task"], state="Done")
    other_team = fake.add_issue("Elsewhere", team=fake.add_team("ENG")["key"])
    waiting = fake.add_issue("Waits", parent=the_map, labels=["wayfinder:grilling"])
    for issue in (blocker, finished, other_team):
        fake.block(issue, waiting)
    fake.relate(fake.add_issue("Related", parent=the_map, labels=["wayfinder:task"]), waiting)
    fake.block(waiting, blocker)  # the other direction: waiting blocks blocker

    tickets = board(fake)

    assert tickets[waiting["number"]].blocked_by == (f"TM-{blocker['number']}", "ENG-1")
    assert tickets[waiting["number"]].open_blockers == 2
    assert tickets[blocker["number"]].blocked_by == (f"TM-{waiting['number']}",)


@pytest.mark.parametrize("part_of", ["Part of [TM-1](https://linear.app/fabricskill/issue/TM-1/pick-a-stack)",
                                     "Part of https://linear.app/fabricskill/issue/TM-1/pick-a-stack",
                                     "Part of TM-1"])
def test_the_spec_whose_part_of_names_the_map_and_its_implementation_tickets_are_in_its_lane(part_of):
    fake = FakeLinear()
    the_map = fake.add_issue("Pick a stack", labels=["wayfinder:map"])  # TM-1
    decision = fake.add_issue("Decide", parent=the_map, labels=["wayfinder:grilling"], state="Done")
    spec = fake.add_issue("Spec", f"{part_of}\n\n## Problem Statement", labels=["ready-for-agent"])
    native = fake.add_issue("Slice A", "## What to build", parent=spec)
    mention = f"[TM-{spec['number']}](https://linear.app/fabricskill/issue/TM-{spec['number']}/spec)"
    by_parent = fake.add_issue("Slice B", f"## Parent\n\n{mention}\n\nBlocked by: TM-{native['number']}")
    typed = fake.add_issue("Linked task", "Part of TM-1\n\nAFK", labels=["wayfinder:task"])
    fake.add_issue("Other spec", "Part of TM-99")
    fake.add_issue("Aside", "Mentions [TM-1](https://linear.app/fabricskill/issue/TM-1/pick-a-stack)")

    tickets = board(fake)

    assert set(tickets) == {n["number"] for n in (the_map, decision, spec, native, by_parent, typed)}
    for issue in (spec, native, by_parent):
        ticket = tickets[issue["number"]]
        assert (ticket.map_number, ticket.map_url) == (1, "https://linear.app/fabricskill/issue/TM-1/slug")
    assert [tickets[n["number"]].spec_kind for n in (spec, native, by_parent)] == ["spec", "impl", "impl"]
    assert tickets[native["number"]].parent_number == spec["number"]
    assert tickets[spec["number"]].has_tickets and not tickets[the_map["number"]].has_tickets
    assert tickets[typed["number"]].is_wayfinder_ticket and tickets[typed["number"]].mode == "AFK"
    assert tickets[by_parent["number"]].blocked_by == (f"TM-{native['number']}",)  # a body blocker
    assert lanes(tickets.values()) == {1: {"SPECS": [spec["number"]], "BLOCKED": [by_parent["number"]],
                                           "FRONTIER": [typed["number"], native["number"]],
                                           "DECIDED": [decision["number"]]}}


SPEC_SIG = "## Problem Statement\n\nIt hurts.\n\n## Solution\n\nFix it.\n"
TICKET_SIG = "## What to build\n\nA slice.\n\n## Acceptance criteria\n\n- [ ] done\n"


def test_a_spec_signature_sub_issue_of_the_map_is_its_spec_without_a_part_of_line():
    fake = FakeLinear()
    the_map = fake.add_issue("Pick a stack", labels=["wayfinder:map"])  # TM-1
    spec = fake.add_issue("Spec", SPEC_SIG, parent=the_map)
    plain = fake.add_issue("Plain", TICKET_SIG, parent=the_map)  # no spec signature: no spec
    decision = fake.add_issue("Decide", SPEC_SIG, parent=the_map, labels=["wayfinder:grilling"])
    native = fake.add_issue("Slice A", TICKET_SIG, parent=spec)
    by_parent = fake.add_issue("Slice B", f"## Parent\n\nTM-{spec['number']}\n\n" + TICKET_SIG)

    tickets = board(fake)

    kinds = [tickets[n["number"]].spec_kind for n in (spec, plain, decision, native, by_parent)]
    assert kinds == ["spec", None, None, "impl", "impl"]
    for issue in (spec, native, by_parent):
        assert tickets[issue["number"]].map_number == the_map["number"]
    assert tickets[spec["number"]].has_tickets and tickets[decision["number"]].is_wayfinder_ticket
    assert lanes(tickets.values()) == {1: {"SPECS": [spec["number"]],
                                           "FRONTIER": [decision["number"], native["number"], by_parent["number"]]}}


def test_a_spec_linked_both_as_a_sub_issue_and_by_part_of_is_listed_once():
    fake = FakeLinear()
    the_map = fake.add_issue("Pick a stack", labels=["wayfinder:map"])
    spec = fake.add_issue("Spec", "Part of TM-1\n\n" + SPEC_SIG, parent=the_map)

    tickets = LinearProvider("TM", transport=fake).fetch(Query())

    assert [t.number for t in tickets] == [the_map["number"], spec["number"]]
    assert lanes(tickets) == {1: {"SPECS": [spec["number"]]}}


def test_body_blockers_are_read_by_key_and_closed_ones_do_not_block():
    fake = FakeLinear()
    the_map = fake.add_issue("Map", labels=["wayfinder:map"])
    outside = fake.add_issue("Not on the board")
    closed = fake.add_issue("Closed", state="Canceled")
    body = (f"## Blocked by\n\n* [TM-{outside['number']}](https://linear.app/w/issue/TM-{outside['number']}/x)\n"
            f"* TM-{closed['number']}\n* TM-404\n")
    waiting = fake.add_issue("Waits", body, parent=the_map, labels=["wayfinder:task"])

    tickets = board(fake)

    assert tickets[waiting["number"]].blocked_by == (f"TM-{outside['number']}",)
    asked = [variables["numbers"] for op, variables in fake.calls if op == "TicketmasterStates"]
    assert asked == [[float(outside["number"]), float(closed["number"]), 404.0]]


def test_a_named_closed_map_is_added_and_a_named_issue_that_is_no_map_is_not():
    fake = FakeLinear()
    closed = fake.add_issue("Old map", labels=["wayfinder:map"], state="Done")
    child = fake.add_issue("Asked", parent=closed, labels=["wayfinder:research"], state="Done")
    plain = fake.add_issue("Plain")

    assert board(fake) == {}
    assert set(board(fake, named_map=closed["number"])) == {closed["number"], child["number"]}
    assert board(fake, named_map=plain["number"]) == {}


def test_a_closed_map_whose_spec_is_still_open_is_shown_in_full():
    fake = FakeLinear()
    closed = fake.add_issue("Map", labels=["wayfinder:map"], state="Done")
    decided = fake.add_issue("Asked", parent=closed, labels=["wayfinder:grilling"], state="Done")
    spec = fake.add_issue("Spec", description=f"Part of [TM-{closed['number']}](https://linear.app/w/issue/TM-"
                                              f"{closed['number']}/map)\n\n## Problem")
    impl = fake.add_issue("Build it", parent=spec)
    fake.add_issue("Stray", description="Part of TM-404")  # names no issue: brings no map in

    tickets = LinearProvider("TM", transport=fake).fetch(Query())
    lane = build_board(tickets).lanes[0]
    assert lane.header.number == closed["number"] and not lane.collapsed
    assert [t.number for t in lane.columns[Column.DECIDED]] == [decided["number"]]
    assert [t.number for t in lane.specs] == [spec["number"]]
    assert [t.number for t in lane.columns[Column.FRONTIER]] == [impl["number"]]


def test_the_viewer_is_the_keys_display_name():
    assert LinearProvider("TM", transport=FakeLinear()).viewer() == "kaare"


def test_every_document_reads_and_stays_under_the_complexity_budget():
    fake = FakeLinear(page_size=50)
    for name, document in linear.GQL.items():
        assert "mutation" not in document, name
        assert fake.estimate(document) <= linear.BUDGET, name
    assert not any(hasattr(LinearProvider, write) for write in ("close", "delete"))


def test_paging_stops_on_a_repeated_cursor_and_past_the_page_guard(monkeypatch):
    def endless(cursors):
        def transport(query, variables):
            return {"issues": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": next(cursors)}}}
        return transport

    same = iter(lambda: "c", None)
    with pytest.raises(ProviderError, match="repeated a cursor"):
        LinearProvider("TM", transport=endless(same)).fetch(Query())
    monkeypatch.setattr(linear, "MAX_PAGES", 5)
    calls = iter(range(1, 100))
    with pytest.raises(ProviderError, match="more than 5 pages"):
        LinearProvider("TM", transport=endless(map(str, calls))).fetch(Query())
    assert next(calls) == 6  # five pages asked for, then it stopped


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return FakeResponse(json.dumps(self.answer).encode())


def http(monkeypatch, answer) -> FakeOpener:
    opener = FakeOpener(answer)
    monkeypatch.setattr(LinearHTTP, "opener", staticmethod(lambda: opener))
    return opener


def http_error(code, body, headers=None):
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError(linear.LINEAR_URL, code, "Bad Request", message, io.BytesIO(json.dumps(body).encode()))


def test_the_key_goes_bare_in_authorization_to_linear_only(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_secret")
    opener = http(monkeypatch, {"data": {"viewer": {"displayName": "kaare.blakstad"}}})

    assert LinearProvider("TM").viewer() == "kaare.blakstad"

    [request] = opener.requests
    assert (request.get_method(), request.full_url) == ("POST", "https://api.linear.app/graphql")
    assert request.get_header("Authorization") == "lin_api_secret"  # no `Bearer`
    assert json.loads(request.data)["query"] == linear.GQL["TicketmasterViewer"]
    with pytest.raises(ProviderError, match="refusing to send the API key"):
        LinearHTTP("lin_api_secret", url="https://evil.example/graphql")


def test_a_400_with_ratelimited_is_a_rate_limit_naming_the_limit_and_when_it_lifts(monkeypatch):
    body = {"errors": [{"message": "Rate limit exceeded", "extensions": {"code": "RATELIMITED"}}]}
    headers = {"X-RateLimit-Requests-Remaining": "0", "X-RateLimit-Requests-Reset": "1792047106174"}
    http(monkeypatch, http_error(400, body, headers))

    with pytest.raises(RateLimited) as limited:
        LinearHTTP("key")("query Q { viewer { displayName } }", {})

    assert (limited.value.status, limited.value.limit) == (400, "hourly request limit")
    assert limited.value.resets_at == datetime.fromtimestamp(1792047106.174, timezone.utc)
    assert "shares it" in limited.value.hint and "Rate limit exceeded" in str(limited.value)


def test_other_graphql_errors_are_not_rate_limits(monkeypatch):
    body = {"errors": [{"message": "Authentication required", "extensions": {"code": "AUTHENTICATION_ERROR"}}]}
    http(monkeypatch, http_error(400, body))

    with pytest.raises(ProviderError) as error:
        LinearHTTP("key")("query Q { viewer { displayName } }", {})

    assert not isinstance(error.value, RateLimited)
    assert (error.value.status, str(error.value)) == (400, "linear: Authentication required")


def test_a_key_env_outside_linear_is_refused_without_showing_it():
    with pytest.raises(ProviderError, match="starting LINEAR_") as error:
        LinearProvider("TM", key_env="GITHUB_TOKEN", transport=FakeLinear())
    assert "GITHUB_TOKEN" not in str(error.value)


def test_a_missing_key_names_the_variable_and_the_key_env_option(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY_ACME", raising=False)
    with pytest.raises(ProviderError, match="LINEAR_API_KEY_ACME is not set.*key_env"):
        LinearProvider("TM", key_env="LINEAR_API_KEY_ACME")


def test_the_fetch_limit_caps_the_open_maps_and_says_so():
    fake = FakeLinear()
    for n in range(3):
        fake.add_issue(f"Map {n}", labels=["wayfinder:map"])
    provider = LinearProvider("TM", transport=fake)
    assert len(lanes(provider.fetch(Query(limit=2)))) == 2 and provider.cut_off == ["open maps"]
    assert len(lanes(provider.fetch(Query(limit=3)))) == 3 and provider.cut_off == []


def test_the_fetch_limit_caps_a_maps_tickets_and_reads_no_page_past_it():
    fake = FakeLinear()  # two issues a page
    the_map = fake.add_issue("Map", labels=["wayfinder:map"])
    for n in range(8):
        fake.add_issue(f"Ticket {n}", parent=the_map, labels=["wayfinder:task"])
    provider = LinearProvider("TM", transport=fake)
    shown = lanes(provider.fetch(Query(limit=2)))  # the first sub-issues as Linear pages them
    assert list(shown) == [1] and len(shown[1]["FRONTIER"]) == 2
    assert provider.cut_off == ["TM-1's tickets"]
    assert fake.ops().count("TicketmasterChildren") == 2  # three of the eight sub-issues tell it was cut
    assert len(provider.fetch(Query(limit=8))) == 9 and provider.cut_off == []


def test_linear_reports_no_quota():
    fake = FakeLinear()
    fake.add_issue("Pick a stack", "Notes", labels=["wayfinder:map"])
    provider = LinearProvider("TM", transport=fake)
    provider.fetch(Query())
    assert getattr(provider, "quota", None) is None
