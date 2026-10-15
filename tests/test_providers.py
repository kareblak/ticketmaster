import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import pytest

from ticketmaster import providers
from ticketmaster.providers import ProviderError, Query, RateLimited, github, gitlab, gitea
from ticketmaster.model import Ticket


def test_github_node_to_ticket():
    node = {
        "number": 7, "title": "T", "url": "https://x/7", "body": "b", "state": "OPEN", "closedAt": None,
        "author": {"login": "opener"},
        "assignees": {"nodes": [{"login": "kareblak"}]},
        "labels": {"nodes": [{"name": "review:security"}]},
        "blockedBy": {"nodes": [{"number": 5, "state": "OPEN", "repository": {"nameWithOwner": "o/r"}}]},
        "closedByPullRequestsReferences": {"nodes": [{"state": "MERGED"}, {"state": "OPEN"}]},
        "parent": {"number": 2, "url": "https://x/2", "labels": {"nodes": [{"name": "wayfinder:map"}]}},
    }
    t = github.to_ticket(node, order=3, repo="o/r")
    assert t.blocked_by == ("#5",)
    assert (t.parent_number, t.parent_url, t.parent_labels) == (2, "https://x/2", ("wayfinder:map",))
    assert (t.number, t.is_open, t.open_blockers, t.has_open_change_request, t.order) == (7, True, 1, True, 3)
    assert t.assignees == ("kareblak",) and t.labels == ("review:security",)
    assert t.author == "opener"


def test_github_ghost_author_is_empty():
    node = {"number": 1, "title": "T", "url": "u", "state": "OPEN", "author": None,
            "assignees": {"nodes": []}, "labels": {"nodes": []}}
    assert github.to_ticket(node, 0).author == ""


def test_github_merged_pr_alone_is_not_review():
    node = {
        "number": 1, "title": "T", "url": "u", "state": "OPEN",
        "assignees": {"nodes": []}, "labels": {"nodes": []},
        "closedByPullRequestsReferences": {"nodes": [{"state": "MERGED"}]},
    }
    assert github.to_ticket(node, 0).has_open_change_request is False


def test_github_merged_closing_pr_is_marked_merged():
    node = {
        "number": 1, "title": "T", "url": "u", "state": "OPEN",
        "assignees": {"nodes": []}, "labels": {"nodes": []},
        "closedByPullRequestsReferences": {"nodes": [{"state": "MERGED"}]},
    }
    assert github.to_ticket(node, 0).has_merged_change_request is True
    assert github.to_ticket({**node, "closedByPullRequestsReferences": {"nodes": [{"state": "OPEN"}]}},
                            0).has_merged_change_request is False


def gh_issue(number, labels=(), body="", parent=None, state="OPEN", closed_at=None, parent_labels=("wayfinder:map",)):
    node = {"number": number, "title": f"t{number}", "url": f"https://gh/o/r/issues/{number}", "body": body,
            "state": state, "assignees": {"nodes": []}, "labels": {"nodes": [{"name": n} for n in labels]},
            "repository": {"nameWithOwner": "o/r"}}
    if closed_at is not None:
        node["closedAt"] = closed_at.isoformat().replace("+00:00", "Z")
    if parent is not None:
        node["parent"] = {"number": parent, "url": f"https://gh/o/r/issues/{parent}",
                          "labels": {"nodes": [{"name": n} for n in parent_labels]}}
    return node


def page(nodes):
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


def gh_answers(monkeypatch, answer):
    """Fakes `gh api graphql`: `answer(args, stdin)` is the JSON answer, as a dict."""
    monkeypatch.setattr(github, "run_cli", lambda args, stdin=None, check=True: json.dumps(answer(args, stdin)))


RESET = "2026-10-15T13:00:00Z"


class FakeGitHub:
    """Fakes `gh api graphql` for a repo of `issues` (full nodes by number): the map search answers `maps`, the
    `Part of` search `specs` and the signature search `signed` (full nodes, also issues of the repo), lean; `subs`
    and `refs` are each issue's sub-issues and cross-references. `asked` is what was asked, and every answer costs
    `cost` points and leaves `remaining`, one fewer each call."""

    def __init__(self, monkeypatch, issues=(), maps=(), specs=(), subs=None, refs=None, cost=1, remaining=4000,
                 signed=()):
        self.issues = {n["number"]: n for n in [*maps, *signed, *issues]}
        self.maps, self.specs, self.subs, self.refs = list(maps), list(specs), subs or {}, refs or {}
        self.signed = list(signed)
        self.cost, self.remaining = cost, remaining
        self.asked = []
        gh_answers(monkeypatch, self.answer)

    def answer(self, args, stdin):
        gql, variables = json.loads(stdin)["query"], json.loads(stdin)["variables"]
        aliases = [int(n) for n in re.findall(r"\w+: (?:issue|issueOrPullRequest)\(number: (\d+)\)", gql)]
        errors = []
        if "q" in variables:
            self.asked.append(("search", variables["q"]))
            if '"What to build"' in variables["q"]:
                assert "ISSUE_ADVANCED" in gql
                lean = ("number", "body", "labels", "state", "closedAt", "parent")
                data = {"search": page([{k: n.get(k) for k in lean} for n in self.signed])}
            else:
                hits = self.specs if '"Part of"' in variables["q"] else self.maps
                data = {"search": page([{k: n[k] for k in ("number", "body", "labels")} for n in hits])}
        elif "issueOrPullRequest" in gql:
            self.asked.append(("states", aliases))
            data = {"repository": {f"i{n}": {"state": self.issues[n]["state"]} if n in self.issues else None
                                   for n in aliases}}
        elif aliases and "subIssues" in gql:
            self.asked.append(("children", aliases))
            data = {"repository": {f"c{n}": self.children(n) for n in aliases}}
        elif aliases:
            self.asked.append(("issues", aliases))
            data = {"repository": {f"i{n}": self.issues.get(n) for n in aliases}}
            errors = [{"type": "NOT_FOUND", "message": f"Could not resolve #{n}"} for n in aliases if n not in self.issues]
        elif "viewer" in gql:
            self.asked.append(("viewer",))
            data = {"viewer": {"login": "me"}}
        elif "withSubs" in variables:
            self.asked.append(("more children", variables["number"]))
            data = {"repository": {"issue": self.children(variables["number"])}}
        else:
            self.asked.append(("issue", variables["number"]))
            found = self.issues.get(variables["number"])
            data = {"repository": {"issue": found}}
            errors = [] if found else [{"type": "NOT_FOUND", "message": "Could not resolve"}]
        data["rateLimit"] = {"cost": self.cost, "remaining": self.remaining, "resetAt": RESET}
        self.remaining -= self.cost
        return {"data": data, **({"errors": errors} if errors else {})}

    def children(self, number):
        return {"subIssues": page(self.subs.get(number, [])),
                "timelineItems": page([{"source": s} for s in self.refs.get(number, [])])}


def github_board(monkeypatch, maps, subs=None, refs=None, named=None, specs=()):
    """The map search answers `maps` and the open `Part of` search `specs`; `named` is one more issue, or a dict of
    them by number."""
    named = {} if named is None else named if "number" not in named else {named["number"]: named}
    return FakeGitHub(monkeypatch, named.values(), maps, specs, subs, refs).asked


def lanes(tickets, named_map=None):
    from ticketmaster.model import build_board

    return {lane.header.number: {**({"SPECS": [t.number for t in lane.specs]} if lane.specs else {}),
                              **{c.name: [t.number for t in ts] for c, ts in lane.columns.items() if ts}}
            for lane in build_board(tickets, named_map).lanes}


def test_github_fetches_every_open_map_with_its_children_in_map_order(monkeypatch):
    maps = [gh_issue(1, MAP_LABELS), gh_issue(20, MAP_LABELS)]
    subs = {1: [gh_issue(5, ["wayfinder:grilling"], parent=1),
                gh_issue(3, ["wayfinder:task"], parent=1, state="CLOSED")],
            20: [gh_issue(21, ["wayfinder:research"], parent=20)]}
    # #4 links to the map by its `Part of #1` line alone; #6 only mentions it, and #5 is a sub-issue already.
    refs = {1: [gh_issue(4, ["wayfinder:task"], "Part of #1"), gh_issue(6, [], "see #1"),
                gh_issue(5, ["wayfinder:grilling"], parent=1)]}
    asked = github_board(monkeypatch, maps, subs, refs)
    tickets = github.GitHubProvider("o/r").fetch(Query())
    assert asked == [("search", 'repo:o/r is:issue is:open label:"wayfinder:map"'),
                     ("search", 'repo:o/r is:issue is:open in:body "Part of"'), signature_search(),
                     ("issues", [1, 20]), ("children", [1, 20])]
    assert [(t.number, t.order) for t in tickets] == [(1, 0), (20, 0), (5, 0), (3, 1), (4, 2), (21, 0)]
    assert lanes(tickets) == {1: {"FRONTIER": [5, 4], "DECIDED": [3]}, 20: {"FRONTIER": [21]}}


def test_github_adds_a_named_closed_map_with_its_children(monkeypatch):
    closed_map = gh_issue(7, MAP_LABELS, state="CLOSED")
    asked = github_board(monkeypatch, [], {7: [gh_issue(8, ["wayfinder:task"], parent=7)]}, named=closed_map)
    tickets = github.GitHubProvider("o/r").fetch(Query(named_map=7))
    assert [(t.number, t.is_open) for t in tickets] == [(7, False), (8, True)]
    assert ("issues", [7]) in asked


def test_github_asks_for_a_named_map_only_when_it_is_not_open_and_drops_one_that_is_no_map(monkeypatch):
    asked = github_board(monkeypatch, [gh_issue(1, MAP_LABELS)])
    assert [t.number for t in github.GitHubProvider("o/r").fetch(Query(named_map=1))] == [1]
    assert [a for a in asked if a[0] == "issues"] == [("issues", [1])]
    github_board(monkeypatch, [], named=gh_issue(7, ["bug"], state="CLOSED"))
    assert github.GitHubProvider("o/r").fetch(Query(named_map=7)) == []
    github_board(monkeypatch, [])
    assert github.GitHubProvider("o/r").fetch(Query(named_map=404)) == []


def test_gitlab_node_to_ticket():
    node = {
        "iid": "12", "title": "T", "webUrl": "https://gl/12", "description": "d", "state": "opened",
        "closedAt": None, "author": {"username": "gl-user"}, "assignees": {"nodes": []},
        "labels": {"nodes": [{"title": "review::ux"}]}, "blockedByIssues": {"nodes": []}, "mergeRequestsCount": 1,
    }
    t = gitlab.to_ticket(node)
    assert (t.number, t.is_open, t.open_blockers, t.has_open_change_request) == (12, True, 0, True)
    assert t.labels == ("review::ux",) and t.author == "gl-user"


def gl_issue(iid, labels, body="", state="opened", closed_at=None):
    return {"iid": str(iid), "title": f"t{iid}", "webUrl": f"https://gl/g/p/-/issues/{iid}", "description": body,
            "state": state, "closedAt": closed_at and closed_at.isoformat(), "assignees": {"nodes": []},
            "labels": {"nodes": [{"title": n} for n in labels]}, "blockedByIssues": {"nodes": []},
            "mergeRequestsCount": 0}


def searched(words, *texts):
    """A tracker's word search, loosely: every word of three letters or more is in the title or body."""
    haystack = " ".join(texts).lower()
    return all(word in haystack for word in words.lower().split() if len(word) >= 3)


GITLAB_SIGNATURES = [("search", words, state) for words in ("problem statement solution", "build acceptance criteria")
                     for state in ("opened", "closed")]
GITEA_SIGNATURES = [("search", words, state) for words in ("Problem Statement", "What to build")
                    for state in ("open", "closed")]


def gitlab_board(monkeypatch, labels, issues, named=None):
    """Fakes `glab api graphql`: the project's labels, issues by label (and state), a named issue (by default the
    one with that iid)."""
    asked = []

    def fake_run_json(args, stdin=None):
        body = json.loads(stdin)
        variables = body["variables"]
        if "labels(first" in body["query"]:
            return {"data": {"project": {"labels": page([{"title": title} for title in labels])}}}
        if "named" in variables:
            asked.append(("named", variables["named"]))
            found = named or next((n for n in issues if n["iid"] == variables["named"]), None)
            return {"data": {"project": {"issue": found}}}
        if "search" in variables:
            state, after = variables.get("state"), variables.get("closedAfter")
            asked.append(("search", variables["search"], state) if state else ("search", variables["search"]))
            nodes = [n for n in issues if searched(variables["search"], n["title"], n["description"])
                     and state in (None, n["state"]) and (after is None or (n["closedAt"] or "") >= after)]
            return {"data": {"project": {"issues": page(nodes)}}}
        asked.append((variables.get("state"), tuple(variables["labels"])))
        nodes = [n for n in issues if variables["labels"][0] in [label["title"] for label in n["labels"]["nodes"]]
                 and variables.get("state", n["state"]) == n["state"]]
        return {"data": {"project": {"issues": page(nodes)}}}

    monkeypatch.setattr(gitlab, "run_json", fake_run_json)
    return asked


GITLAB_LABELS = ["wayfinder::map", "wayfinder::grilling", "wayfinder::task", "bug", "wayfinder::epic"]
GITLAB_ISSUES = [
    gl_issue(1, ["wayfinder::map"]), gl_issue(20, ["wayfinder::map"]), gl_issue(30, ["wayfinder::map"], state="closed"),
    gl_issue(5, ["wayfinder::grilling"], "Part of #1\n\nWhy"), gl_issue(3, ["wayfinder::task"], "Part of #1", "closed"),
    gl_issue(21, ["wayfinder::task"], "Part of #20"), gl_issue(31, ["wayfinder::task"], "Part of #30"),
    gl_issue(9, ["wayfinder::grilling"], "see #1"), gl_issue(40, ["bug"], "Part of #1"),
]


def test_gitlab_fetches_every_open_map_with_its_children_by_their_part_of_line(monkeypatch):
    asked = gitlab_board(monkeypatch, GITLAB_LABELS, GITLAB_ISSUES)
    tickets = gitlab.GitLabProvider("g/p").fetch(Query())
    assert asked == [("opened", ("wayfinder::map",)), (None, ("wayfinder::grilling",)), (None, ("wayfinder::task",)),
                     ("search", "Part of"), ("search", "Parent"), *GITLAB_SIGNATURES]
    assert [t.number for t in tickets] == [1, 20, 5, 3, 21, 40]  # #40: untyped with `Part of #1`, so the spec
    assert (tickets[2].parent_number, tickets[2].parent_url) == (1, "https://gl/g/p/-/issues/1")
    assert lanes(tickets) == {1: {"SPECS": [40], "FRONTIER": [5], "DECIDED": [3]}, 20: {"FRONTIER": [21]}}


def test_gitlab_keeps_an_open_maps_tickets_behind_many_of_another_maps(monkeypatch):
    # One type label lists the whole project's tickets, newest first: other maps' closed ones must not crowd out
    # an open map's.
    crowd = [gl_issue(1000 + n, ["wayfinder::task"], "Part of #30", "closed") for n in range(350)]
    gitlab_board(monkeypatch, GITLAB_LABELS, [gl_issue(1, ["wayfinder::map"]), *crowd,
                                              gl_issue(5, ["wayfinder::task"], "Part of #1")])
    assert [t.number for t in gitlab.GitLabProvider("g/p").fetch(Query())] == [1, 5]


def test_gitea_keeps_an_open_maps_tickets_behind_many_of_another_maps(monkeypatch):
    crowd = [gt_issue(1000 + n, ["wayfinder:task"], "Part of #30", "closed") for n in range(350)]
    gitea_board(monkeypatch, ["wayfinder:map", "wayfinder:task"], None,
                [gt_issue(1, ["wayfinder:map"]), *crowd, gt_issue(5, ["wayfinder:task"], "Part of #1")])
    assert [t.number for t in gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query())] == [1, 5]


def test_gitlab_adds_a_named_closed_map_with_its_children(monkeypatch):
    asked = gitlab_board(monkeypatch, GITLAB_LABELS, GITLAB_ISSUES, named=GITLAB_ISSUES[2])
    tickets = gitlab.GitLabProvider("g/p").fetch(Query(named_map=30))
    assert ("named", "30") in asked
    assert {30, 31} <= {t.number for t in tickets}
    assert lanes(tickets, named_map=30)[30] == {}


def test_gitea_issue_to_ticket_detects_closing_pr():
    issue = {"number": 4, "title": "T", "html_url": "https://gt/4", "body": "", "state": "open",
             "closed_at": None, "user": {"login": "gt-user"}, "assignees": None, "labels": [{"name": "review:perf"}]}
    t = gitea.to_ticket(issue, (), ["Refactor\nFixes #4"])
    assert t.has_open_change_request and t.labels == ("review:perf",) and t.assignees == ()
    assert t.author == "gt-user"
    assert gitea.to_ticket(issue, (), ["Fixes #40"]).has_open_change_request is False


def gt_issue(number, labels, body="", state="open", assignees=(), closed_at=None, comments=""):
    return {"number": number, "title": f"t{number}", "html_url": f"https://gt/o/r/issues/{number}", "body": body,
            "state": state, "closed_at": closed_at and closed_at.isoformat(), "comments_text": comments,
            "assignees": [{"login": a} for a in assignees], "labels": [{"name": n} for n in labels]}


def gitea_board(monkeypatch, repo_labels, org_labels, issues, dependencies=None):
    """Fakes Gitea's REST API: repo and org labels, issues by label and state, dependencies and single issues."""
    asked = []

    def fake_get(self, path, params=None):
        if path == "/repos/o/r/labels":
            return [{"name": name} for name in repo_labels]
        if path == "/orgs/o/labels":
            if org_labels is None:
                raise providers.NotFound("codeberg.org/orgs/o/labels: HTTP 404 Not Found")
            return [{"name": name} for name in org_labels]
        if path.endswith("/pulls"):
            return []
        if path == "/repos/o/r/issues" and "q" in params:
            state, since = params["state"], params.get("since")
            asked.append(("search", params["q"], state) if state != "all" else ("search", params["q"]))
            return [i for i in issues if searched(params["q"], i["title"], i["body"], i.get("comments_text", ""))
                    and state in ("all", i["state"]) and (since is None or (i["closed_at"] or "") >= since)]
        if path == "/repos/o/r/issues":
            asked.append((params["state"], params["labels"]))
            return [i for i in issues if params["labels"] in [label["name"] for label in i["labels"]]
                    and params["state"] in ("all", i["state"])]
        if path.endswith("/dependencies"):
            return (dependencies or {}).get(int(path.split("/")[-2]), [])
        number = int(path.rsplit("/", 1)[-1])
        asked.append(("issue", number))
        found = next((i for i in issues if i["number"] == number), None)
        if found is None:
            raise providers.NotFound(f"codeberg.org{path}: HTTP 404 Not Found")
        return found

    monkeypatch.setattr(gitea.GiteaProvider, "_get", fake_get)
    return asked


GITEA_ISSUES = [
    gt_issue(1, ["wayfinder:map"]), gt_issue(20, ["wayfinder:map"]), gt_issue(30, ["wayfinder:map"], state="closed"),
    gt_issue(5, ["wayfinder:research"], "Part of #1"), gt_issue(3, ["wayfinder:task"], "Part of #1", "closed"),
    gt_issue(2, ["wayfinder:task"], "Part of #1", assignees=("me",)), gt_issue(21, ["wayfinder:task"], "Part of #20"),
    gt_issue(31, ["wayfinder:task"], "Part of #30"), gt_issue(9, ["wayfinder:research"], "see #1"),
    gt_issue(40, ["bug"], "Part of #1"),
]


def test_github_shows_a_closed_map_whose_spec_is_still_open(monkeypatch):
    closed_map = gh_issue(1, MAP_LABELS, state="CLOSED")
    spec = gh_issue(14, [], "Part of #1")
    subs = {1: [gh_issue(2, ["wayfinder:grilling"], parent=1, state="CLOSED")], 14: [gh_issue(15, [], parent=14)]}
    # #7 is an open spec too, but #9 is no map, so it brings nothing in.
    asked = github_board(monkeypatch, [], subs, {1: [spec]}, named={1: closed_map, 9: gh_issue(9, ["bug"])},
                         specs=[spec, gh_issue(7, [], "Part of #9")])
    tickets = github.GitHubProvider("o/r").fetch(Query())
    assert ("issues", [1, 9]) in asked
    assert lanes(tickets) == {1: {"SPECS": [14], "FRONTIER": [15], "DECIDED": [2]}}


def test_gitea_fetches_every_open_map_with_its_children_by_their_part_of_line(monkeypatch):
    # Gitea drops an unknown label filter and lists every issue, so only labels that exist are asked for.
    asked = gitea_board(monkeypatch, ["wayfinder:map", "wayfinder:research", "bug"], ["wayfinder:task"], GITEA_ISSUES,
                        dependencies={5: [{"number": 2, "state": "open", "repository": {"full_name": "o/r"}}]})
    tickets = gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query())
    assert asked == [("open", "wayfinder:map"), ("all", "wayfinder:research"), ("all", "wayfinder:task"),
                     ("search", "Part of"), ("search", "Parent"), *GITEA_SIGNATURES]
    assert [t.number for t in tickets] == [1, 20, 5, 3, 2, 21, 40]
    assert lanes(tickets) == {1: {"SPECS": [40], "BLOCKED": [5], "CLAIMED": [2], "DECIDED": [3]},
                              20: {"FRONTIER": [21]}}


def test_gitea_adds_a_named_closed_map_without_org_labels(monkeypatch):
    asked = gitea_board(monkeypatch, ["wayfinder:map", "wayfinder:task"], None, GITEA_ISSUES)
    tickets = gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query(named_map=30))
    assert ("issue", 30) in asked and {30, 31} <= {t.number for t in tickets}
    asked = gitea_board(monkeypatch, ["bug"], None, GITEA_ISSUES)
    assert gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query()) == []
    # No wayfinder labels: no issue listed by label, only the signature searches.
    assert asked == GITEA_SIGNATURES


def fake_tea(monkeypatch, answer, logins=()):
    """Fakes the `tea` CLI: `tea logins list -o json` prints `logins`, and `tea api -i <endpoint>` prints the
    (status, body[, headers]) that `answer(endpoint)` gives, with the status line and headers on stderr."""
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["tea", "logins", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(list(logins)), stderr="")
        status, body, *headers = answer(args[-1])
        head = "".join(f"{k}: {v}\n" for k, v in (headers[0] if headers else {}).items())
        stderr = f"NOTE: a tea notice\nHTTP/2.0 {status} {HTTPStatus(status).phrase}\n{head}\n"
        return subprocess.CompletedProcess(args, 0, stdout=body, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_gitea_uses_the_tea_login_for_the_repo_host(monkeypatch):
    logins = [{"name": "other", "url": "https://other.io"}, {"name": "cb", "url": "https://codeberg.org"}]
    calls = fake_tea(monkeypatch, lambda endpoint: (200, '{"login": "me"}'), logins)
    assert gitea.GiteaProvider("o/r", "codeberg.org").viewer() == "me"
    assert calls[-1] == ["tea", "api", "--login", "cb", "-i", "/user"]


def test_gitea_without_a_tea_login_for_the_host_says_how_to_add_one(monkeypatch):
    fake_tea(monkeypatch, lambda endpoint: (200, "{}"), [{"name": "other", "url": "https://other.io"}])
    with pytest.raises(ProviderError, match="tea logins add --url https://codeberg.org"):
        gitea.GiteaProvider("o/r", "codeberg.org").viewer()


def test_gitea_tea_failing_before_any_http_call_is_a_provider_error(monkeypatch):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Error: login 'x' not found")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ProviderError, match="login 'x' not found"):
        gitea.GiteaProvider("o/r", "codeberg.org", login="x").viewer()


def test_a_parent_section_names_whole_issue_numbers_only():
    assert providers.names_parent("## Parent\n\n#12.", 12)
    assert not providers.names_parent("## Parent\n\nsee #123", 12)
    assert not providers.names_parent("## Parent\n\n&#12;", 12)


class FakeCompleted:
    def __init__(self, stderr, returncode=1, stdout=""):
        self.stderr, self.returncode, self.stdout = stderr, returncode, stdout


@pytest.mark.parametrize("stderr", [
    "gh: API rate limit exceeded for user ID 842012.",
    "gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. (HTTP 403)",
    "glab: 429 Too Many Requests",
    "ERROR: Too Many Requests (HTTP 429)",
])
def test_cli_rate_limit_errors_are_rate_limited(monkeypatch, stderr):
    monkeypatch.setattr(providers.subprocess, "run", lambda *a, **k: FakeCompleted(stderr))
    with pytest.raises(RateLimited):
        providers.run_cli(["gh", "api", "graphql"])


def test_other_cli_errors_are_not_rate_limited(monkeypatch):
    monkeypatch.setattr(providers.subprocess, "run", lambda *a, **k: FakeCompleted("gh: Could not resolve issue 429"))
    with pytest.raises(ProviderError) as raised:
        providers.run_cli(["gh", "api", "graphql"])
    assert not isinstance(raised.value, RateLimited)


@pytest.mark.parametrize("module, path", [(github, ["search"]), (gitlab, ["project"])])
def test_graphql_rate_limit_errors_are_rate_limited(monkeypatch, module, path):
    errors = [{"type": "RATE_LIMITED", "message": "API rate limit exceeded for user ID 1."}]
    if module is github:
        gh_answers(monkeypatch, lambda args, stdin=None: {"errors": errors})
    else:
        monkeypatch.setattr(module, "run_json", lambda args, stdin=None: {"errors": errors})
    provider = github.GitHubProvider("o/r") if module is github else gitlab.GitLabProvider("g/p")
    with pytest.raises(RateLimited):
        provider.fetch(Query())


def test_gitea_http_429_is_rate_limited(monkeypatch):
    fake_tea(monkeypatch, lambda endpoint: (429, ""))
    with pytest.raises(RateLimited):
        gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query())


def test_gitea_rate_limit_on_dependencies_is_not_swallowed(monkeypatch):
    gitea_board(monkeypatch, ["wayfinder:map"], None, [gt_issue(1, ["wayfinder:map"])])
    fake_get = gitea.GiteaProvider._get

    def limited(self, path, params=None):
        if path.endswith("/dependencies"):
            raise RateLimited("codeberg.org: HTTP 429 rate limit exceeded")
        return fake_get(self, path, params)

    monkeypatch.setattr(gitea.GiteaProvider, "_get", limited)
    with pytest.raises(RateLimited):
        gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query())


def test_gitea_other_http_errors_are_not_rate_limited(monkeypatch):
    fake_tea(monkeypatch, lambda endpoint: (404, '{"message": "not found"}'))
    with pytest.raises(ProviderError) as raised:
        gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query())
    assert not isinstance(raised.value, RateLimited)


def test_body_blockers_join_native_ones_and_lookup_skips_known_issues():
    tickets = [
        Ticket(number=1, title="t", url="u", is_open=True, body="Blocked by: #2, #3, #4, #9", blocked_by=("#2",),
               open_blockers=1),
        Ticket(number=2, title="t", url="u", is_open=True),
        Ticket(number=3, title="t", url="u", is_open=False),
    ]
    asked = []

    def lookup(numbers):
        asked.append(numbers)
        return {4: True, 9: False}

    out = providers.with_body_blockers(tickets, providers.IssueStates(lookup))
    assert asked == [[4, 9]]
    assert (out[0].blocked_by, out[0].open_blockers) == (("#2", "#4"), 2)
    assert (out[1].blocked_by, out[1].open_blockers) == ((), None)


def test_ticket_unblocks_once_every_listed_blocker_is_closed():
    tickets = [Ticket(number=1, title="t", url="u", is_open=True, body="## Blocked by\n\n- #2\n- #3"),
               Ticket(number=2, title="t", url="u", is_open=False),
               Ticket(number=3, title="t", url="u", is_open=False)]
    out = providers.with_body_blockers(tickets, providers.IssueStates(lambda numbers: {}))
    assert (out[0].blocked_by, out[0].open_blockers, out[0].is_blocked) == ((), 0, False)


def test_issue_states_remembers_closed_and_missing_issues():
    asked = []

    def lookup(numbers):
        asked.append(numbers)
        return {4: True, 5: False}

    states = providers.IssueStates(lookup)
    assert states([4, 5, 6]) == {4: True, 5: False, 6: False}
    assert states([4, 5, 6]) == {4: True, 5: False, 6: False}
    assert asked == [[4, 5, 6], [4]]


def test_github_native_blockers_are_named_and_body_blockers_looked_up(monkeypatch):
    def node(number, body="", blocked_by=()):
        return {"number": number, "title": "t", "url": "u", "body": body, "state": "OPEN",
                "assignees": {"nodes": []}, "labels": {"nodes": [{"name": "wayfinder:map"}]},
                "blockedBy": {"nodes": [{"number": n, "state": s, "repository": {"nameWithOwner": repo}}
                                        for n, s, repo in blocked_by]}}

    calls = []

    def answer(args, stdin):
        body = json.loads(stdin)
        calls.append(body)
        if "q" in body["variables"]:
            return {"data": {"search": page([{"number": 7, "body": "", "labels": {"nodes": [{"name": "wayfinder:map"}]}}])}}
        if "subIssues" in body["query"]:
            return {"data": {"repository": {"c7": {"subIssues": page([]), "timelineItems": page([])}}}}
        if "i7: issue(" in body["query"]:
            nodes = node(7, "Blocked by: #8, #50, #51", [(8, "OPEN", "o/r"), (3, "OPEN", "x/y"), (9, "CLOSED", "o/r")])
            return {"data": {"repository": {"i7": nodes}}}
        errors = [{"type": "NOT_FOUND", "message": "Could not resolve"}]
        return {"data": {"repository": {"i50": {"state": "OPEN"}, "i51": None}}, "errors": errors}

    gh_answers(monkeypatch, answer)
    [t] = github.GitHubProvider("o/r").fetch(Query())
    assert (t.blocked_by, t.open_blockers) == (("#8", "x/y#3", "#50"), 3)
    assert "i50: issueOrPullRequest(number: 50)" in calls[-1]["query"]


def test_github_lookup_raises_on_errors_other_than_not_found(monkeypatch):
    errors = [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]
    monkeypatch.setattr(github, "run_cli", lambda args, stdin=None, check=True: json.dumps({"data": None, "errors": errors}))
    with pytest.raises(RateLimited):
        github.GitHubProvider("o/r")._issue_states([5])


def test_github_viewer(monkeypatch):
    gh_answers(monkeypatch, lambda args, stdin=None: {"data": {"viewer": {"login": "kareblak"}}})
    assert github.GitHubProvider("o/r").viewer() == "kareblak"


def test_gitlab_native_and_body_blockers(monkeypatch):
    issue = {"iid": "12", "title": "T", "webUrl": "u", "description": "Blokkert av: #20", "state": "opened",
             "closedAt": None, "assignees": {"nodes": []}, "labels": {"nodes": [{"title": "wayfinder::map"}]},
             "mergeRequestsCount": 0,
             "blockedByIssues": {"nodes": [{"iid": "13", "state": "opened", "reference": "g/p#13"},
                                           {"iid": "14", "state": "closed", "reference": "g/p#14"},
                                           {"iid": "3", "state": "opened", "reference": "g/other#3"}]}}

    def fake_run_json(args, stdin=None):
        body = json.loads(stdin)
        variables = body["variables"]
        if "labels(first" in body["query"]:
            return {"data": {"project": {"labels": page([{"title": "wayfinder::map"}])}}}
        if "iids" in variables:
            assert variables["iids"] == ["20"]
            return {"data": {"project": {"issues": {"nodes": [{"iid": "20", "state": "opened"}]}}}}
        return {"data": {"project": {"issues": page([issue])}}}

    monkeypatch.setattr(gitlab, "run_json", fake_run_json)
    [t] = gitlab.GitLabProvider("g/p").fetch(Query())
    assert (t.blocked_by, t.open_blockers) == (("#13", "g/other#3", "#20"), 3)


def test_gitlab_viewer(monkeypatch):
    monkeypatch.setattr(gitlab, "run_json", lambda args, stdin=None: {"data": {"currentUser": {"username": "gl"}}})
    assert gitlab.GitLabProvider("g/p").viewer() == "gl"


def test_gitea_native_and_body_blockers(monkeypatch):
    def fake_get(self, path, params=None):
        if path.endswith("/labels"):
            return [{"name": "wayfinder:map"}]
        if path.endswith("/issues"):
            return [{"number": 1, "title": "t", "html_url": "u", "state": "open", "labels": [{"name": "wayfinder:map"}],
                     "body": "Blocked by: #5, #6"}] if params["state"] == "open" else []
        if path.endswith("/1/dependencies"):
            return [{"number": 2, "state": "open", "repository": {"full_name": "o/r"}},
                    {"number": 3, "state": "open", "repository": {"full_name": "x/y"}},
                    {"number": 4, "state": "closed", "repository": {"full_name": "o/r"}}]
        if path.endswith("/issues/5"):
            return {"number": 5, "state": "open"}
        if path.endswith("/issues/6"):
            raise providers.NotFound("codeberg.org/x: HTTP 404 Not Found")
        if path == "/user":
            return {"login": "gt"}
        return []

    monkeypatch.setattr(gitea.GiteaProvider, "_get", fake_get)
    provider = gitea.GiteaProvider("o/r", "codeberg.org", login="x")
    [t] = provider.fetch(Query())
    assert (t.blocked_by, t.open_blockers) == (("#2", "x/y#3", "#5"), 3)
    assert provider.viewer() == "gt"


def test_gitea_blocker_lookup_failure_is_not_taken_for_closed(monkeypatch):
    def fake_get(self, path, params=None):
        if path.endswith("/issues/5"):
            raise ProviderError("codeberg.org/x: HTTP 502 Bad Gateway")
        return {}

    monkeypatch.setattr(gitea.GiteaProvider, "_get", fake_get)
    with pytest.raises(ProviderError):
        gitea.GiteaProvider("o/r", "codeberg.org", login="x")._issue_states([5])


MAP_LABELS = ("wayfinder:map",)


def test_resolve_maps_follows_part_of_to_a_map_in_the_board_or_fetched_once():
    from ticketmaster.model import Ticket

    fetched = []

    def fetch_map(number):
        fetched.append(number)
        labels = {9: MAP_LABELS, 8: ("epic",)}.get(number)
        return Ticket(number=number, title="", url=f"https://x/{number}", is_open=True, labels=labels) if labels else None

    tickets = [
        Ticket(number=1, title="map", url="https://x/1", is_open=True, labels=MAP_LABELS),
        Ticket(number=2, title="in board", url="u", is_open=True, labels=("wayfinder:task",), body="Part of #1"),
        Ticket(number=3, title="closed map", url="u", is_open=True, labels=("wayfinder::research",), body="Part of #9"),
        Ticket(number=4, title="same map", url="u", is_open=True, labels=("wayfinder:grilling",), body="Part of #9"),
        Ticket(number=5, title="not a map", url="u", is_open=True, labels=("wayfinder:task",), body="Part of #8"),
        Ticket(number=6, title="untyped", url="u", is_open=True, labels=("bug",), body="Part of #9"),
        Ticket(number=7, title="native", url="u", is_open=True, labels=("wayfinder:task",), body="Part of #9",
               parent_number=1, parent_url="https://x/1", parent_labels=MAP_LABELS),
        Ticket(number=10, title="gone", url="u", is_open=True, labels=("wayfinder:task",), body="Part of #404"),
        Ticket(number=11, title="loose", url="u", is_open=True, labels=("wayfinder:task",), body="Part of #9, kind of"),
    ]
    resolved = {t.number: t for t in providers.resolve_maps(tickets, fetch_map)}
    assert (resolved[2].parent_number, resolved[2].parent_url) == (1, "https://x/1")
    assert resolved[3].is_wayfinder_ticket and resolved[4].parent_url == "https://x/9"
    assert resolved[5].map_missing and resolved[10].map_missing and resolved[11].map_missing
    assert resolved[6].parent_url is None
    assert resolved[7].parent_number == 1
    assert sorted(fetched) == [8, 9, 404]  # each missing map fetched once, never one already on the board


def test_resolve_maps_lets_tracker_errors_surface_instead_of_calling_the_map_missing():
    from ticketmaster.model import Ticket

    typed = [Ticket(number=2, title="t", url="u", is_open=True, labels=("wayfinder:task",), body="Part of #1")]

    def down(number):
        raise ProviderError("HTTP 401 Unauthorized")

    with pytest.raises(ProviderError):
        providers.resolve_maps(typed, down)


def test_github_missing_map_issue_is_no_map_but_other_errors_surface(monkeypatch):
    answer = {}
    gh_answers(monkeypatch, lambda args, stdin=None: answer)
    answer.update(errors=[{"type": "NOT_FOUND", "message": "Could not resolve to an Issue with the number of 9."}])
    assert github.GitHubProvider("o/r")._issue(9) is None
    answer.update(errors=[{"type": "FORBIDDEN", "message": "Resource not accessible"}])
    with pytest.raises(ProviderError):
        github.GitHubProvider("o/r")._issue(9)


def test_gitea_missing_map_issue_is_no_map_but_other_errors_surface(monkeypatch):
    fake_tea(monkeypatch, lambda endpoint: (404 if endpoint.endswith("/9") else 500, ""))
    provider = gitea.GiteaProvider("o/r", "codeberg.org", login="x")
    assert provider._issue(9) is None
    with pytest.raises(ProviderError):
        provider._issue(8)


def test_github_not_planned_close_reason_is_read():
    node = {"number": 1, "title": "T", "url": "u", "state": "CLOSED", "stateReason": "NOT_PLANNED",
            "assignees": {"nodes": []}, "labels": {"nodes": []}}
    assert github.to_ticket(node, 0).not_planned is True
    assert github.to_ticket({**node, "stateReason": "COMPLETED"}, 0).not_planned is False
    assert "stateReason" in github.ISSUE_FIELDS


def test_gitea_only_sends_gets_through_tea(monkeypatch):
    calls = fake_tea(monkeypatch, lambda endpoint: (200, "[]"))
    gitea.GiteaProvider("o/r", "codeberg.org", login="x")._get("/repos/o/r/issues", {"state": "open", "page": 2})
    # No -X, -f, -F or -d: `tea api` sends a GET.
    assert calls == [["tea", "api", "--login", "x", "-i", "/repos/o/r/issues?state=open&page=2"]]


@pytest.mark.parametrize("provider", [github.GitHubProvider("o/r"), gitlab.GitLabProvider("g/p"),
                                      gitea.GiteaProvider("o/r", "codeberg.org", login="x")])
def test_no_provider_can_write(provider):
    assert not any(hasattr(provider, write) for write in ("close", "delete"))


def test_the_provider_protocol_only_reads():
    from ticketmaster.providers import Provider

    assert {name for name in vars(Provider) if not name.startswith("_")} == {"fetch", "viewer"}


def test_spec_is_part_of_the_map_without_a_wayfinder_label():
    from ticketmaster.model import Ticket

    def spec(body, labels=()):
        return Ticket(number=3, title="Spec", url="u", is_open=True, body=body, labels=labels)

    assert providers.is_spec(spec("Part of #5\n\n## Problem Statement", ("ready-for-agent",)), 5)
    assert not providers.is_spec(spec("Part of #50"), 5)
    assert not providers.is_spec(spec("Part of #5", ("wayfinder:task",)), 5)  # a decision ticket
    assert not providers.is_spec(spec("Part of #5", ("wayfinder::grilling",)), 5)


def test_implementation_ticket_names_its_spec_in_the_parent_section():
    body = "## Parent\n\n#8\n\n## What to build\n\nSee #9."
    assert providers.names_parent(body, 8)
    assert not providers.names_parent(body, 9)  # mentioned, but outside ## Parent
    assert providers.names_parent("## Parent\n\nhttps://gl/g/p/-/issues/8\n", 8)
    assert not providers.names_parent("## Parent\n\n#80\n", 8)
    assert not providers.names_parent("Part of #8", 8)


def test_a_linear_parent_section_names_its_spec_by_key_mention_or_url():
    mention = "## Parent\n\n[TM-14](https://linear.app/w/issue/TM-14/read-only-ticketmaster)\n\n## What\n\nTM-9"
    assert providers.parent_ref(mention, "TM") == 14 and providers.names_parent(mention, 14, "TM")
    assert not providers.names_parent(mention, 9, "TM")
    assert providers.parent_ref("## Parent\n\nTM-14\n", "TM") == 14
    assert providers.parent_ref("## Parent\n\nhttps://linear.app/w/issue/TM-14\n", "TM") == 14
    assert providers.parent_ref("## Parent\n\nENG-14\n", "TM") is None


def test_key_like_words_in_a_parent_section_are_no_refs_without_the_trackers_key():
    body = "## Parent\n\nUTF-8 notes for #14 (RFC-2119)\n"
    assert providers.parent_ref(body) == 14 and providers.names_parent(body, 14)
    assert not providers.names_parent(body, 8) and not providers.names_parent("## Parent\n\nTM-14\n", 14)


def test_linear_body_blockers_render_as_keys():
    tickets = [Ticket(number=4, title="A", url="u4", is_open=True, body="Blocked by: TM-2, TM-3", key="TM-4",
                      blocked_by=("TM-9",), open_blockers=1)]
    states = providers.IssueStates(lambda numbers: {2: True, 3: False})
    [ticket] = providers.with_body_blockers(tickets, states, key="TM")
    assert (ticket.blocked_by, ticket.open_blockers) == (("TM-9", "TM-2"), 2)


def test_linear_errors_name_linear():
    notice = providers.error_notice("linear", RateLimited("Rate limit exceeded", 400, "hourly request limit"))
    assert notice.text.startswith("Linear rate-limited the board: hourly request limit")


# A map (#1) with a decision ticket (#2), the spec (#3) and its implementation tickets (#4, #5);
# #6 mentions the map and #7 names another spec, so neither belongs to the spec.
FAMILY = {
    1: ("Map", "Notes", ["wayfinder:map"]),
    2: ("Decide", "Part of #1", ["wayfinder:grilling"]),
    3: ("Spec", "Part of #1\n\n## Problem Statement", ["ready-for-agent"]),
    4: ("Slice A", "## Parent\n\n#3\n\n## What to build\n\n## Blocked by\n\n- #2\n", ["ready-for-agent"]),
    5: ("Slice B", "## Parent\n\n#3\n\nBlocked by: #4", []),
    6: ("Aside", "see #1", []),
    7: ("Elsewhere", "## Parent\n\n#30\n", []),
}


def assert_lane_family(tickets, map_url):
    """On every store: the spec and its tickets are on the map, tagged, blocked by their bodies, in its lane."""
    by_number = {t.number: t for t in tickets}
    assert {1, 2, 3, 4, 5} <= set(by_number) and not {6, 7} & set(by_number)
    for n in (3, 4, 5):
        assert (by_number[n].map_number, by_number[n].map_url) == (1, map_url), n
    assert [by_number[n].spec_kind for n in (2, 3, 4, 5)] == [None, "spec", "impl", "impl"]
    assert [by_number[n].has_tickets for n in (1, 2, 3, 4, 5)] == [False, False, True, False, False]
    # The body's blockers, after any native ones.
    assert (by_number[4].blocked_by[-1:], by_number[5].blocked_by[-1:]) == (("#2",), ("#4",))
    assert lanes(tickets) == {1: {"SPECS": [3], "BLOCKED": [4, 5], "FRONTIER": [2]}}


def assert_map_family(tickets, map_url, spec_url):
    assert_lane_family(tickets, map_url)
    by_number = {t.number: t for t in tickets}
    assert (by_number[3].parent_number, by_number[3].parent_url, by_number[3].parent_labels) == (1, map_url, ("wayfinder:map",))
    for n in (4, 5):
        assert (by_number[n].parent_number, by_number[n].parent_url) == (3, spec_url)
        assert by_number[n].parent_labels == ("ready-for-agent",)
    assert by_number[2].is_wayfinder_ticket and not by_number[3].is_wayfinder_ticket
    assert not by_number[4].is_wayfinder_ticket
    # The spec and its tickets start wayfinder on the map; the decision ticket finds it through its parent.
    for n in (3, 4, 5):
        assert (by_number[n].map_number, by_number[n].map_url) == (1, map_url)
    assert by_number[2].map_url is None


def family_node(n, parent=None, repo="o/r"):
    title, body, labels = FAMILY[n]
    node = {"number": n, "title": title, "url": f"https://gh/{n}", "body": body, "state": "OPEN",
            "assignees": {"nodes": []}, "labels": {"nodes": [{"name": label} for label in labels]},
            "repository": {"nameWithOwner": repo}}
    if parent is not None:
        node["parent"] = {"number": parent, "url": f"https://gh/{parent}",
                          "labels": {"nodes": [{"name": label} for label in FAMILY[parent][2]]}}
    return node


def github_children(monkeypatch, sub_issues, references):
    """The map #1 alone is open; the children of the map and its spec come from `sub_issues` and `references`."""
    fake = FakeGitHub(monkeypatch, maps=[family_node(1)], subs=sub_issues, refs=references)
    return fake.asked


def test_github_reads_each_maps_spec_and_its_implementation_tickets(monkeypatch):
    # #2 is a native sub-issue of the map and #4 one of the spec; #3 and #5 link by body only,
    # so GitHub reports them as cross-references; #6 and #8 (another repo) mention without linking.
    sub_issues = {1: [family_node(2, parent=1)], 3: [family_node(4, parent=3)]}
    references = {1: [family_node(3), family_node(6), family_node(2, parent=1)],
                  3: [family_node(5), family_node(4, parent=3), {**family_node(5, repo="x/y"), "number": 8}, {}]}
    asked = github_children(monkeypatch, sub_issues, references)
    tickets = github.GitHubProvider("o/r").fetch(Query())
    assert_map_family(tickets, "https://gh/1", "https://gh/3")
    assert [t.number for t in tickets] == [1, 2, 3, 4, 5]  # the map, its order, then the spec, then its tickets
    # The open `Part of` search missed the spec, so its children come in a batch of their own; the decision ticket
    # is not a spec, so its children are not asked for.
    assert [a for a in asked if a[0] == "children"] == [("children", [1]), ("children", [3])]


def family_labels(n, scoped=False):
    return [label.replace("wayfinder:", "wayfinder::") if scoped else label for label in FAMILY[n][2]]


def test_gitlab_reads_each_maps_spec_and_its_implementation_tickets_by_their_bodies(monkeypatch):
    issues = [gl_issue(n, family_labels(n, scoped=True), FAMILY[n][1]) for n in FAMILY]
    asked = gitlab_board(monkeypatch, ["wayfinder::map", "wayfinder::grilling"], issues)
    tickets = gitlab.GitLabProvider("g/p").fetch(Query())
    assert_lane_family(tickets, "https://gl/g/p/-/issues/1")
    assert asked[-6:-4] == [("search", "Part of"), ("search", "Parent")]


def test_gitea_reads_each_maps_spec_and_its_implementation_tickets_with_no_sub_issues(monkeypatch):
    # Gitea has no sub-issues: the decision ticket and the spec name the map by `Part of #<map>` alone, the
    # implementation tickets their spec by `## Parent`, and #5 also has a native dependency on the decision ticket.
    issues = [gt_issue(n, family_labels(n), FAMILY[n][1]) for n in FAMILY]
    gitea_board(monkeypatch, ["wayfinder:map", "wayfinder:grilling"], None, issues,
                dependencies={5: [{"number": 2, "state": "open", "repository": {"full_name": "o/r"}}]})
    tickets = gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query())
    by_number = {t.number: t for t in tickets}
    assert by_number[2].parent_number == 1 and by_number[2].is_wayfinder_ticket  # child → map by `Part of #1`
    assert by_number[5].blocked_by == ("#2", "#4")  # native first, then the body's
    assert_lane_family(tickets, "https://gt/o/r/issues/1")


def test_trackers_without_sub_issues_search_for_specs_only_with_a_map_label(monkeypatch):
    asked = gitlab_board(monkeypatch, ["bug"], [gl_issue(3, [], "Part of #1")])
    assert gitlab.GitLabProvider("g/p").fetch(Query()) == []
    assert not any(call[1] in providers.SPEC_SEARCHES for call in asked if call[0] == "search")


def test_gitlab_shows_a_closed_map_whose_spec_is_still_open(monkeypatch):
    # No map is open, so the board finds map #1 only through its open spec #14's `Part of #1` line.
    issues = [gl_issue(1, ["wayfinder::map"], state="closed"), gl_issue(14, [], "Part of #1"),
              gl_issue(2, ["wayfinder::grilling"], "Part of #1", "closed"), gl_issue(15, [], "## Parent\n\n#14")]
    asked = gitlab_board(monkeypatch, ["wayfinder::map", "wayfinder::grilling"], issues, named=issues[0])
    tickets = gitlab.GitLabProvider("g/p").fetch(Query())
    assert ("named", "1") in asked
    assert lanes(tickets) == {1: {"SPECS": [14], "FRONTIER": [15], "DECIDED": [2]}}


def test_github_map_order_is_the_maps_sub_issue_order(monkeypatch):
    calls = []

    def answer(args, stdin=None):
        body = json.loads(stdin)
        calls.append(body["variables"])
        after = body["variables"]["after"]
        nodes, more = ([{"number": 9}, {"number": 4}], True) if after is None else ([{"number": 6}], False)
        page = {"pageInfo": {"hasNextPage": more, "endCursor": "c1"}, "nodes": nodes}
        return {"data": {"repository": {"issue": {"subIssues": page}}}}

    gh_answers(monkeypatch, answer)
    assert github.GitHubProvider("o/r").map_order(1) == [9, 4, 6]
    assert [c["number"] for c in calls] == [1, 1] and calls[1]["after"] == "c1"


@pytest.mark.parametrize("stderr, status", [
    ("gh: Bad credentials (HTTP 401)", 401),
    ("gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. (HTTP 403)", 403),
    ("glab: 502 Bad Gateway", 502),
    ("ERROR: Too Many Requests (HTTP 429)", 429),
    ("gh: API rate limit already exceeded for user ID 842012.", None),
    ("gh: Could not resolve issue 429", None),
])
def test_cli_errors_carry_the_http_status_the_cli_names(monkeypatch, stderr, status):
    monkeypatch.setattr(providers.subprocess, "run", lambda *a, **k: FakeCompleted(stderr))
    with pytest.raises(ProviderError) as raised:
        providers.run_cli(["gh", "api", "graphql"])
    assert raised.value.status == status


def test_notice_for_a_used_up_quota_names_it_and_when_it_lifts():
    resets = datetime(2026, 10, 14, 8, 4, tzinfo=timezone.utc)
    error = providers.RateLimited("gh: API rate limit exceeded for user ID 1.", limit="hourly GraphQL quota",
                                  resets_at=resets)
    notice = providers.error_notice("github", error)
    assert notice.reason == "hourly GraphQL quota" and notice.severity == "warning"
    assert notice.text == (f"GitHub rate-limited the board: hourly GraphQL quota, lifts at "
                           f"{resets.astimezone():%H:%M}\ngh: API rate limit exceeded for user ID 1.")


def test_notice_for_a_secondary_limit_gives_the_hint():
    error = providers.RateLimited("gh: API rate limit already exceeded for user ID 1.", limit="secondary limit",
                                  hint="too many calls per minute; every board and agent on the account shares it")
    notice = providers.error_notice("github", error)
    assert notice.reason == "secondary limit"
    assert notice.text.splitlines()[0] == ("GitHub rate-limited the board: secondary limit (too many calls per minute; "
                                           "every board and agent on the account shares it)")


def test_notice_for_a_bare_429_names_the_status():
    notice = providers.error_notice("gitea", providers.RateLimited("codeberg.org/x: HTTP 429 Too Many Requests"))
    assert notice.reason == "429" and notice.severity == "warning"
    assert notice.text.splitlines()[0] == "Gitea/Forgejo rate-limited the board: 429"


@pytest.mark.parametrize("message, reason, severity, line", [
    ("gh: Bad credentials (HTTP 401)", "401 unauthorized", "error", "401 unauthorized · gh: Bad credentials (HTTP 401)"),
    ("glab: 502 Bad Gateway", "502 bad gateway", "warning", "502 bad gateway · glab: 502 Bad Gateway"),
    ("gh: not logged in", "error", "error", "gh: not logged in"),
])
def test_notice_for_other_errors_names_the_status_class(message, reason, severity, line):
    notice = providers.error_notice("gitlab", ProviderError(message))
    assert (notice.reason, notice.severity, notice.status_text) == (reason, severity, line)
    assert notice.text.splitlines()[-1] == message


def rate_limit_answer(graphql=4470, core=4972, reset=1791964800):
    return {"resources": {"graphql": {"remaining": graphql, "limit": 5000, "reset": reset},
                          "core": {"remaining": core, "limit": 5000, "reset": reset}}}


@pytest.mark.parametrize("answer, limit", [
    (rate_limit_answer(graphql=0), "hourly GraphQL quota"),
    (rate_limit_answer(core=0), "hourly REST quota"),
])
def test_github_names_the_used_up_hourly_quota(monkeypatch, answer, limit):
    asked = []
    monkeypatch.setattr(github, "run_json", lambda args, stdin=None: asked.append(args) or answer)
    error = github.GitHubProvider("o/r").explain(RateLimited("gh: API rate limit exceeded for user ID 1."))
    assert asked == [["gh", "api", "rate_limit", "--hostname", "github.com"]]
    assert error.limit == limit and error.resets_at == datetime.fromtimestamp(1791964800, timezone.utc)
    assert str(error) == "gh: API rate limit exceeded for user ID 1."


def test_github_with_quota_left_names_the_secondary_limit(monkeypatch):
    monkeypatch.setattr(github, "run_json", lambda args, stdin=None: rate_limit_answer())
    error = github.GitHubProvider("o/r").explain(RateLimited("gh: API rate limit already exceeded for user ID 1. (HTTP 403)"))
    assert error.limit == "secondary limit" and error.resets_at is None and error.status == 403
    assert "every board and agent on the account shares it" in error.hint


def test_github_keeps_the_trackers_message_when_the_rate_limit_lookup_fails(monkeypatch):
    def fail(args, stdin=None):
        raise ProviderError("gh: boom")

    monkeypatch.setattr(github, "run_json", fail)
    original = RateLimited("gh: API rate limit exceeded")
    assert github.GitHubProvider("o/r").explain(original) is original


@pytest.mark.parametrize("headers, resets_at", [
    ({"Retry-After": "120"}, datetime(2026, 10, 14, 8, 2, tzinfo=timezone.utc)),
    ({"Retry-After": "Wed, 14 Oct 2026 08:30:00 GMT"}, datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)),
    ({"Retry-After": "Wed, 14 Oct 2026 08:30:00 -0000"}, datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)),
    ({"X-RateLimit-Reset": "1791966600"}, datetime.fromtimestamp(1791966600, timezone.utc)),
    ({"RateLimit-Reset": "1791966600"}, datetime.fromtimestamp(1791966600, timezone.utc)),
    ({}, None),
])
def test_gitea_429_says_when_the_limit_lifts(monkeypatch, headers, resets_at):
    fake_tea(monkeypatch, lambda endpoint: (429, "", headers))
    monkeypatch.setattr(gitea, "now", lambda: datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc))
    with pytest.raises(RateLimited) as raised:
        gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query())
    assert raised.value.status == 429 and raised.value.resets_at == resets_at
    assert resets_at is None or raised.value.resets_at.tzinfo is not None


def test_gitea_errors_carry_the_trackers_own_message(monkeypatch):
    fake_tea(monkeypatch, lambda endpoint: (403, '{"message": "token does not have required scope"}'))
    with pytest.raises(ProviderError) as raised:
        gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query())
    assert raised.value.status == 403 and str(raised.value).endswith("HTTP 403 Forbidden: token does not have required scope")


def test_parent_ref_is_the_first_issue_the_parent_section_names():
    assert providers.parent_ref("## Parent\n\n#8, see also #9\n\n## What to build\n\n#7") == 8
    assert providers.parent_ref("## Parent\n\nhttps://gl/g/p/-/issues/12\n") == 12
    assert providers.parent_ref("Part of #8\n\n## Problem") is None
    assert providers.parent_ref("## Parent\n\nnone yet\n") is None


def issue(number, labels=(), body="", parent=None):
    from ticketmaster.model import Ticket

    return Ticket(number=number, title="", url=f"https://x/{number}", is_open=True, labels=tuple(labels), body=body,
                  parent_number=parent)


def test_resolve_maps_follows_ticket_to_spec_to_map_fetching_each_issue_once():
    # Off the board: the map (#1), a spec (#3), an epic (#20), a spec of an epic (#21) and a spec of a typed ticket (#22).
    off_board = {1: issue(1, MAP_LABELS), 3: issue(3, body="Part of #1\n\n## Problem"),
                 20: issue(20, ("epic",)), 21: issue(21, body="Part of #20"),
                 22: issue(22, ("wayfinder:task",), "Part of #1")}
    fetched = []

    def fetch_issue(number):
        fetched.append(number)
        return off_board.get(number)

    tickets = [
        issue(4, body="## Parent\n\n#3\n"),  # implementation ticket, spec off the board
        issue(5, parent=3),  # GitHub sub-issue of the spec
        issue(6, body="Part of #1\n\n## Problem"),  # a spec on the board
        issue(7, body="## Parent\n\n#6\n"),  # its implementation ticket
        issue(8, body="## Parent\n\n#21\n"),  # spec of an epic: no map
        issue(9, body="## Parent\n\n#404\n"),  # spec gone
        issue(10, body="## Parent\n\n#22\n"),  # parent is a typed ticket, not a spec
        issue(11),  # plain
        issue(12, body="## Parent\n\n#3\n", parent=6),  # the sub-issue parent wins over ## Parent
    ]
    resolved = {t.number: t for t in providers.resolve_maps(tickets, fetch_issue)}
    for n in (4, 5, 6, 7, 12):
        assert (resolved[n].map_number, resolved[n].map_url) == (1, "https://x/1"), n
    for n in (8, 9, 10, 11):
        assert resolved[n].map_url is None and not resolved[n].map_missing, n
    assert resolved[4].parent_number is None and resolved[5].parent_number == 3  # the parent stays as it was
    assert sorted(fetched) == [1, 3, 20, 21, 22, 404]  # each chain issue off the board fetched once


def test_a_spec_has_tickets_when_an_open_or_a_closed_implementation_ticket_names_it():
    def spec(number):
        return Ticket(number=number, title="Spec", url=f"u{number}", is_open=True, body="Part of #1",
                      map_number=1, map_url="u1")

    def impl(number, parent, **kw):
        base = dict(number=number, title="Slice", url=f"u{number}", is_open=True, body=f"## Parent\n\n#{parent}",
                    map_number=1, map_url="u1")
        return Ticket(**{**base, **kw})

    tickets = [spec(3), spec(4), spec(5), impl(6, 3, is_open=False), impl(7, 0, body="", parent_number=4)]
    marked = {t.number: t.has_tickets for t in providers.with_spec_tickets(tickets)}
    assert marked == {3: True, 4: True, 5: False, 6: False, 7: False}


def test_a_linear_spec_has_tickets_by_its_teams_parent_section():
    spec = Ticket(number=3, title="Spec", url="u3", is_open=True, key="TM-3", body="Part of TM-1",
                  map_number=1, map_url="u1")
    impl = Ticket(number=6, title="Slice", url="u6", is_open=False, key="TM-6", body="## Parent\n\nTM-3",
                  map_number=1, map_url="u1")
    assert [t.has_tickets for t in providers.with_spec_tickets([spec, impl])] == [True, False]


# ---------- the fetch limit: maps per board and tickets per map, and what a fetch cut off ----------

def test_github_reads_at_most_the_limit_of_maps_and_of_each_maps_tickets_and_says_what_it_cut(monkeypatch):
    maps = [gh_issue(1, MAP_LABELS), gh_issue(20, MAP_LABELS), gh_issue(30, MAP_LABELS)]
    subs = {1: [gh_issue(n, ["wayfinder:task"], parent=1) for n in (2, 3, 4)]}
    github_board(monkeypatch, maps, subs)
    provider = github.GitHubProvider("o/r")
    assert [t.number for t in provider.fetch(Query(limit=2))] == [1, 20, 2, 3]
    assert provider.cut_off == ["open maps", "#1's tickets"]
    assert len(provider.fetch(Query(limit=3))) == 6 and provider.cut_off == []


def test_gitlab_reads_at_most_the_limit_of_maps_and_of_each_maps_tickets_and_says_what_it_cut(monkeypatch):
    issues = [gl_issue(1, ["wayfinder::map"]), gl_issue(20, ["wayfinder::map"]), gl_issue(30, ["wayfinder::map"]),
              *(gl_issue(n, ["wayfinder::task"], "Part of #1") for n in (2, 3, 4))]
    gitlab_board(monkeypatch, GITLAB_LABELS, issues)
    provider = gitlab.GitLabProvider("g/p")
    assert lanes(provider.fetch(Query(limit=2))) == {1: {"FRONTIER": [2, 3]}, 20: {}}
    assert provider.cut_off == ["open maps", "#1's tickets"]
    assert len(provider.fetch(Query(limit=3))) == 6 and provider.cut_off == []


def test_gitea_reads_at_most_the_limit_of_maps_and_of_each_maps_tickets_and_says_what_it_cut(monkeypatch):
    issues = [gt_issue(1, ["wayfinder:map"]), gt_issue(20, ["wayfinder:map"]), gt_issue(30, ["wayfinder:map"]),
              *(gt_issue(n, ["wayfinder:task"], "Part of #1") for n in (2, 3, 4))]
    gitea_board(monkeypatch, ["wayfinder:map", "wayfinder:task"], None, issues)
    provider = gitea.GiteaProvider("o/r", "codeberg.org", login="x")
    assert lanes(provider.fetch(Query(limit=2))) == {1: {"FRONTIER": [2, 3]}, 20: {}}
    assert provider.cut_off == ["open maps", "#1's tickets"]
    assert len(provider.fetch(Query(limit=3))) == 6 and provider.cut_off == []


def test_a_spec_whose_tickets_the_limit_cuts_still_has_tickets(monkeypatch):
    lean_board(monkeypatch)
    tickets = github.GitHubProvider("o/r").fetch(Query(limit=2))  # each lane keeps its decision ticket and spec
    assert {t.number: t.has_tickets for t in tickets if t.spec_kind} == {3: True, 12: True, 22: True}
    issues = [gl_issue(1, ["wayfinder::map"]), gl_issue(2, ["wayfinder::grilling"], "Part of #1"),
              gl_issue(3, [], "Part of #1"), gl_issue(4, [], "## Parent\n\n#3")]
    gitlab_board(monkeypatch, GITLAB_LABELS, issues)
    assert {t.number: t.has_tickets for t in gitlab.GitLabProvider("g/p").fetch(Query(limit=2))} == \
        {1: False, 2: False, 3: True}


def test_a_cut_map_lane_keeps_its_first_tickets_in_map_order():
    the_map = Ticket(number=1, title="", url="u1", is_open=True, labels=MAP_LABELS)
    kids = [Ticket(number=n, title="", url=f"u{n}", is_open=True, labels=("wayfinder:task",), parent_number=1,
                   parent_url="u1", parent_labels=MAP_LABELS, order=order) for n, order in ((7, 2), (8, 0), (9, 1))]
    cut: list[str] = []
    assert [t.number for t in providers.cap_lanes([the_map, *kids], 2, cut)] == [1, 8, 9]
    assert cut == ["#1's tickets"]


def lean_board(monkeypatch, cost=1):
    """Three open maps (#1, #10, #20), each with a decision ticket and a spec that names it by `Part of`; each spec has
    an implementation ticket as a sub-issue and one by its `## Parent` section. #30 is no map's spec."""
    maps, subs, refs, specs = [], {}, {}, []
    for m in (1, 10, 20):
        spec = gh_issue(m + 2, ["ready-for-agent"], f"Part of #{m}\n\n## Problem Statement")
        maps.append(gh_issue(m, MAP_LABELS))
        subs[m] = [gh_issue(m + 1, ["wayfinder:grilling"], parent=m)]
        refs[m] = [spec]
        subs[m + 2] = [gh_issue(m + 3, [], parent=m + 2)]
        refs[m + 2] = [gh_issue(m + 4, [], f"## Parent\n\n#{m + 2}")]
        specs.append(spec)
    specs.append(gh_issue(30, [], "Part of #404"))
    return FakeGitHub(monkeypatch, [gh_issue(404, ["bug"])], maps, specs, subs, refs, cost=cost)


def test_github_searches_lean_then_fetches_the_hits_and_every_maps_and_specs_children_once(monkeypatch):
    fake = lean_board(monkeypatch)
    queries = []
    answer = fake.answer
    gh_answers(monkeypatch, lambda args, stdin: queries.append(json.loads(stdin)["query"]) or answer(args, stdin))
    tickets = github.GitHubProvider("o/r").fetch(Query())
    # #30's `Part of #404` names a closed issue that is no map, so it is fetched with the maps and dropped.
    assert fake.asked == [("search", 'repo:o/r is:issue is:open label:"wayfinder:map"'),
                          ("search", 'repo:o/r is:issue is:open in:body "Part of"'), signature_search(),
                          ("issues", [1, 10, 20, 404]),
                          ("children", [1, 10, 20, 3, 12, 22])]
    for search in queries[:3]:
        assert "title" not in search and "assignees" not in search and "blockedBy" not in search
    assert lanes(tickets) == {m: {"SPECS": [m + 2], "FRONTIER": [m + 1, m + 3, m + 4]} for m in (1, 10, 20)}


def test_github_reports_what_its_last_fetch_cost_and_the_points_left(monkeypatch):
    lean_board(monkeypatch, cost=3)
    provider = github.GitHubProvider("o/r")
    assert provider.quota is None
    provider.fetch(Query())
    reset = datetime(2026, 10, 15, 13, tzinfo=timezone.utc)
    # Five calls at 3 points each: the remaining count the last call saw.
    assert provider.quota == providers.Quota(cost=15, remaining=4000 - 12, resets_at=reset)
    provider.viewer()  # outside a fetch: no fetch's cost
    provider.fetch(Query())
    assert provider.quota == providers.Quota(cost=15, remaining=4000 - 30, resets_at=reset)


def test_trackers_other_than_github_report_no_quota(monkeypatch):
    gitlab_board(monkeypatch, GITLAB_LABELS, GITLAB_ISSUES)
    provider = gitlab.GitLabProvider("g/p")
    provider.fetch(Query())
    assert getattr(provider, "quota", None) is None
    gitea_board(monkeypatch, ["wayfinder:map"], None, GITEA_ISSUES)
    provider = gitea.GiteaProvider("o/r", "codeberg.org", login="x")
    provider.fetch(Query())
    assert getattr(provider, "quota", None) is None


def test_github_reads_a_maps_further_sub_issue_pages_after_the_batch(monkeypatch):
    fake = FakeGitHub(monkeypatch, maps=[gh_issue(1, MAP_LABELS)])
    first = {"pageInfo": {"hasNextPage": True, "endCursor": "s1"},
             "nodes": [gh_issue(2, ["wayfinder:task"], parent=1)]}
    more = page([gh_issue(3, ["wayfinder:task"], parent=1)])
    fake.children = lambda number: {"subIssues": more if fake.asked[-1][0] == "more children" else first,
                                    "timelineItems": page([])}
    tickets = github.GitHubProvider("o/r").fetch(Query())
    assert fake.asked[-2:] == [("children", [1]), ("more children", 1)]
    assert [t.number for t in tickets] == [1, 2, 3]


def test_github_rate_limit_refusal_without_graphql_errors_is_rate_limited(monkeypatch):
    # GitHub's secondary limit answers a REST body; for an aliased query gh's failing exit is read past.
    gh_answers(monkeypatch, lambda args, stdin: {"message": "You have exceeded a secondary rate limit."})
    with pytest.raises(RateLimited):
        github.GitHubProvider("o/r")._issue_states([5])


def test_github_failed_fetch_still_reports_the_points_it_saw(monkeypatch):
    fake = lean_board(monkeypatch, cost=2)
    answer = fake.answer
    gh_answers(monkeypatch, lambda args, stdin: answer(args, stdin) if len(fake.asked) < 2 else
               {"errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]})
    provider = github.GitHubProvider("o/r")
    with pytest.raises(ProviderError):
        provider.fetch(Query())
    assert (provider.quota.cost, provider.quota.remaining) == (4, 3998)


# --- standalone specs and their tickets on GitHub ------------------------------------------------------------------

SPEC_SIG = "## Problem Statement\n\nIt hurts.\n\n## Solution\n\nFix it.\n"
TICKET_SIG = "## What to build\n\nA slice.\n\n## Acceptance criteria\n\n- [ ] done\n"


def signature_search(days=7):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    return ("search", f'repo:o/r is:issue (in:body "Problem Statement" OR in:body "What to build") '
                      f'(is:open OR closed:>={since:%Y-%m-%d})')


def ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


def test_github_takes_an_issue_with_both_spec_headings_as_a_standalone_spec_and_drops_the_rest(monkeypatch):
    map_spec = gh_issue(34, [], "Part of #1\n\n" + SPEC_SIG)
    signed = [
        gh_issue(30, ["ready-for-agent"], SPEC_SIG),
        gh_issue(31, [], "## Problem Statement\n\nOnly half.\n"),  # half a signature
        gh_issue(32, [], SPEC_SIG + TICKET_SIG),  # both signatures: a spec
        gh_issue(33, ["wayfinder:task"], SPEC_SIG),  # a wayfinder ticket is never standalone
        map_spec,  # `Part of #1` keeps it the map's spec
        gh_issue(35, [], "### Problem Statement\n\n## Solution\n"),  # not at `##` level
        gh_issue(36, [], "## PROBLEM  statement\n\n## solution\n"),  # any case
        gh_issue(37, [], "We need a Problem Statement and a Solution."),  # prose only
    ]
    fake = FakeGitHub(monkeypatch, maps=[gh_issue(1, MAP_LABELS)], specs=[map_spec], refs={1: [map_spec]},
                      signed=signed)
    tickets = github.GitHubProvider("o/r").fetch(Query())
    assert lanes(tickets) == {1: {"SPECS": [34]}, 30: {}, 32: {}, 36: {}}
    assert {t.number: t.spec_kind for t in tickets if t.number >= 30} == {30: "spec", 32: "spec", 34: "spec", 36: "spec"}
    assert fake.asked == [("search", 'repo:o/r is:issue is:open label:"wayfinder:map"'),
                          ("search", 'repo:o/r is:issue is:open in:body "Part of"'), signature_search(),
                          ("issues", [1, 30, 32, 36]), ("children", [1, 34, 30, 32, 36])]


def test_github_fills_each_spec_lane_from_its_children_and_a_closed_specs_from_its_open_tickets(monkeypatch):
    def ticket(number, parent_line="", **kw):
        return gh_issue(number, [], (f"## Parent\n\n{parent_line}\n\n" if parent_line else "") + TICKET_SIG, **kw)

    closed_spec = gh_issue(50, [], SPEC_SIG, state="CLOSED", closed_at=ago(60))  # outside the window: no hit
    subs = {40: [ticket(41, parent=40, parent_labels=()), gh_issue(44, [], "Hand-made", parent=40, parent_labels=())]}
    refs = {40: [ticket(42, "#40"), ticket(43, "https://gh/o/r/issues/40", state="CLOSED", closed_at=ago(400)),
                 gh_issue(45, [], "See #40.\n\n" + TICKET_SIG)]}
    signed = [
        gh_issue(40, [], SPEC_SIG), ticket(42, "#40"),
        ticket(51, "#50"), ticket(52, "#50", state="CLOSED", closed_at=ago(2)),
        ticket(53, "#50", state="CLOSED", closed_at=ago(30)),  # closed before the window
        ticket(54, parent=50, parent_labels=()),  # a sub-issue of the closed spec
        ticket(55),  # no parent: a loose ticket, not in this lane or any
        gh_issue(60, [], SPEC_SIG, state="CLOSED", closed_at=ago(1)), ticket(61, "#60", state="CLOSED", closed_at=ago(1)),
    ]
    fake = FakeGitHub(monkeypatch, issues=[closed_spec], subs=subs, refs=refs, signed=signed)
    tickets = github.GitHubProvider("o/r").fetch(Query())
    assert lanes(tickets) == {40: {"FRONTIER": [41, 42], "DECIDED": [43]}, 50: {"FRONTIER": [51, 54], "DECIDED": [52]}}
    assert {t.number for t in tickets if t.has_tickets} == {40, 50}
    by_number = {t.number: t for t in tickets}
    assert (by_number[42].parent_number, by_number[42].lane_spec, by_number[42].spec_kind) == (40, 40, "impl")
    # One lean search more, the closed spec and its tickets in the one full fetch, the open spec's children in the
    # one batched query: no call per spec.
    assert fake.asked == [("search", 'repo:o/r is:issue is:open label:"wayfinder:map"'),
                          ("search", 'repo:o/r is:issue is:open in:body "Part of"'), signature_search(),
                          ("issues", [40, 50, 51, 52, 54]), ("children", [40])]


def test_github_signature_search_is_lean_and_capped(monkeypatch):
    signed = [gh_issue(n, [], SPEC_SIG) for n in range(1, providers.LIMIT + 2)]
    fake = FakeGitHub(monkeypatch, signed=signed)
    queries = []
    gh_answers(monkeypatch, lambda args, stdin: queries.append(json.loads(stdin)["query"]) or fake.answer(args, stdin))
    provider = github.GitHubProvider("o/r")
    tickets = provider.fetch(Query())
    assert len([t for t in tickets if t.is_standalone_spec]) == providers.LIMIT
    assert "specs and tickets" in provider.cut_off
    search = next(q for q in queries if "ISSUE_ADVANCED" in q)
    assert "title" not in search and "assignees" not in search and "blockedBy" not in search


# --- standalone specs and their tickets on GitLab and Gitea/Forgejo ------------------------------------------------

def standalone_family(issue, closed, url):
    """A board's standalone specs and tickets, as `issue(number, labels, body, state=…, closed_at=…)` makes them, with
    `closed` its closed state's name and `url` an issue's URL without the number."""
    def ticket(number, parent_line="", **kw):
        return issue(number, [], (f"## Parent\n\n{parent_line}\n\n" if parent_line else "") + TICKET_SIG, **kw)

    return [
        issue(40, [], SPEC_SIG),  # an open standalone spec
        ticket(41, "#40"),
        ticket(42, f"{url}40", state=closed, closed_at=ago(2)),  # names it by URL, closed within the window
        ticket(43, "#40", state=closed, closed_at=ago(400)),  # closed long ago: only the children search finds it
        issue(44, [], "## Parent\n\n#40\n\nHand-made"),  # no ticket signature
        issue(31, [], "## Problem Statement\n\nOnly half.\n"),  # half a signature
        issue(32, [], "## What to build\n\nOnly half.\n\n## Parent\n\n#40\n"),
        issue(33, ["wayfinder:task"], SPEC_SIG), issue(34, ["wayfinder:task"], "## Parent\n\n#40\n\n" + TICKET_SIG),
        issue(50, [], SPEC_SIG, state=closed, closed_at=ago(60)),  # closed before the window, an open ticket names it
        ticket(51, "#50"),
        issue(60, [], SPEC_SIG, state=closed, closed_at=ago(30)),  # closed before the window, so are its tickets
        ticket(61, "#60", state=closed, closed_at=ago(30)),
        ticket(55),  # no parent: a loose ticket
        issue(7, [], "Just a bug."), ticket(56, "#7"),  # its parent is no spec: looked up, no lane
    ]


STANDALONE_LANES = {40: {"FRONTIER": [41], "DECIDED": [42, 43]}, 50: {"FRONTIER": [51]}}


def test_gitlab_shows_standalone_specs_with_their_tickets_from_two_signature_searches(monkeypatch):
    def issue(number, labels, body, state="opened", closed_at=None):
        return gl_issue(number, [label.replace("wayfinder:", "wayfinder::") for label in labels], body, state, closed_at)

    asked = gitlab_board(monkeypatch, ["wayfinder::task"], standalone_family(issue, "closed", "https://gl/g/p/-/issues/"))
    tickets = gitlab.GitLabProvider("g/p").fetch(Query())
    assert lanes(tickets) == STANDALONE_LANES
    by_number = {t.number: t for t in tickets}
    assert (by_number[42].parent_number, by_number[42].lane_spec, by_number[42].spec_kind) == (40, 40, "impl")
    assert {t.number for t in tickets if t.has_tickets} == {40, 50}
    # Two signature searches, open and closed within the window, one children search per open spec, and the parents
    # an open ticket names that no search found: a closed spec, and #7, which is no spec.
    assert asked == [(None, ("wayfinder::task",)), *GITLAB_SIGNATURES, ("named", "7"), ("named", "50"),
                     ("search", "Parent 40")]


def test_gitea_shows_standalone_specs_with_their_tickets_from_two_signature_searches(monkeypatch):
    issues = standalone_family(gt_issue, "closed", "https://gt/o/r/issues/")
    issues.append(gt_issue(70, [], "Unrelated.", comments="## Problem Statement\n\nx\n\n## Solution\n\ny"))
    asked = gitea_board(monkeypatch, ["wayfinder:task"], None, issues)
    tickets = gitea.GiteaProvider("o/r", "codeberg.org", login="x").fetch(Query())
    assert lanes(tickets) == STANDALONE_LANES
    assert 70 not in {t.number for t in tickets}  # a hit by its comment only
    assert {t.number for t in tickets if t.has_tickets} == {40, 50}
    assert asked == [("all", "wayfinder:task"), *GITEA_SIGNATURES, ("issue", 7), ("issue", 50),
                     ("search", "Parent #40")]


def test_standalone_specs_sit_beside_map_lanes_and_leave_map_specs_to_their_map(monkeypatch):
    issues = [gl_issue(1, ["wayfinder::map"]), gl_issue(14, [], "Part of #1\n\n" + SPEC_SIG),
              gl_issue(15, [], "## Parent\n\n#14\n\n" + TICKET_SIG), gl_issue(40, [], SPEC_SIG),
              gl_issue(41, [], "## Parent\n\n#40\n\n" + TICKET_SIG)]
    asked = gitlab_board(monkeypatch, ["wayfinder::map"], issues)
    tickets = gitlab.GitLabProvider("g/p").fetch(Query())
    assert lanes(tickets) == {1: {"SPECS": [14], "FRONTIER": [15]}, 40: {"FRONTIER": [41]}}
    assert [call for call in asked if call[0] == "search"] == [
        ("search", "Part of"), ("search", "Parent"), *GITLAB_SIGNATURES, ("search", "Parent 40")]


def test_gitlab_signature_hits_are_capped(monkeypatch):
    gitlab_board(monkeypatch, [], [gl_issue(n, [], SPEC_SIG) for n in range(1, providers.LIMIT + 2)])
    provider = gitlab.GitLabProvider("g/p")
    tickets = provider.fetch(Query())
    assert len([t for t in tickets if t.is_standalone_spec]) == providers.LIMIT
    assert provider.cut_off == ["specs and tickets"]


def test_a_spec_lane_cut_after_the_body_check_says_so(monkeypatch):
    # The children search finds more issues than the limit, most of them no ticket of the spec: the lane is cut
    # only by its own tickets.
    noise = [gl_issue(n, [], "## Parent\n\n#400\n\n" + TICKET_SIG) for n in range(100, 110)]
    family = [gl_issue(40, [], SPEC_SIG), *noise,
              *(gl_issue(n, [], "## Parent\n\n#40\n\n" + TICKET_SIG, "closed", ago(30)) for n in (41, 42, 43))]
    gitlab_board(monkeypatch, [], family)
    provider = gitlab.GitLabProvider("g/p")
    assert lanes(provider.fetch(Query(limit=2)))[40] == {"DECIDED": [41, 42]}
    assert provider.cut_off == ["#40's tickets"]
    assert lanes(provider.fetch(Query(limit=3)))[40] == {"DECIDED": [41, 42, 43]} and provider.cut_off == []


def test_a_spec_closed_within_the_window_with_every_ticket_closed_has_no_lane(monkeypatch):
    gitlab_board(monkeypatch, [], [gl_issue(40, [], SPEC_SIG, "closed", ago(2)),
                                   gl_issue(41, [], "## Parent\n\n#40\n\n" + TICKET_SIG, "closed", ago(2))])
    assert lanes(gitlab.GitLabProvider("g/p").fetch(Query())) == {}


def test_a_parent_that_is_no_standalone_spec_is_looked_up_once_per_board_run(monkeypatch):
    issues = standalone_family(gt_issue, "closed", "https://gt/o/r/issues/")
    asked = gitea_board(monkeypatch, [], None, issues)
    provider = gitea.GiteaProvider("o/r", "codeberg.org", login="x")
    provider.fetch(Query())
    asked.clear()
    assert lanes(provider.fetch(Query())) == STANDALONE_LANES
    assert [call for call in asked if call[0] == "issue"] == [("issue", 50)]  # the closed spec, not #7 again
