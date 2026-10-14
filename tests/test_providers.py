import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from ticketmaster import providers
from ticketmaster.providers import ProviderError, Query, RateLimited, github, gitlab, gitea, mentions_parent
from ticketmaster.model import Ticket
from ticketmaster.providers.gitea import token_from_tea_config


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


def gh_issue(number, labels=(), body="", parent=None, state="OPEN"):
    node = {"number": number, "title": f"t{number}", "url": f"https://gh/o/r/issues/{number}", "body": body,
            "state": state, "assignees": {"nodes": []}, "labels": {"nodes": [{"name": n} for n in labels]},
            "repository": {"nameWithOwner": "o/r"}}
    if parent is not None:
        node["parent"] = {"number": parent, "url": f"https://gh/o/r/issues/{parent}",
                          "labels": {"nodes": [{"name": "wayfinder:map"}]}}
    return node


def page(nodes):
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


def github_board(monkeypatch, maps, subs=None, refs=None, named=None):
    """Fakes `gh api graphql`: the map search, each issue's sub-issues and cross-references, a named issue."""
    asked = []

    def fake_run_json(args, stdin=None):
        variables = json.loads(stdin)["variables"]
        if "q" in variables:
            asked.append(("search", variables["q"]))
            return {"data": {"search": page(maps)}}
        if "withSubs" in variables:
            number = variables["number"]
            asked.append(("children", number))
            issue = {"url": f"https://gh/o/r/issues/{number}", "labels": {"nodes": []},
                     "subIssues": page((subs or {}).get(number, [])),
                     "timelineItems": page([{"source": s} for s in (refs or {}).get(number, [])])}
            return {"data": {"repository": {"issue": issue}}}
        asked.append(("issue", variables["number"]))
        if named is None:
            return {"errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}
        return {"data": {"repository": {"issue": named}}}

    monkeypatch.setattr(github, "run_json", fake_run_json)
    return asked


def lanes(tickets, named_map=None):
    from ticketmaster.model import build_board

    return {lane.map.number: {c.name: [t.number for t in ts] for c, ts in lane.columns.items() if ts}
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
    assert asked == [("search", 'repo:o/r is:issue is:open label:"wayfinder:map"'), ("children", 1), ("children", 20)]
    assert [(t.number, t.order) for t in tickets] == [(1, 0), (20, 0), (5, 0), (3, 1), (4, 2), (21, 0)]
    assert lanes(tickets) == {1: {"FRONTIER": [5, 4], "DECIDED": [3]}, 20: {"FRONTIER": [21]}}


def test_github_adds_a_named_closed_map_with_its_children(monkeypatch):
    closed_map = gh_issue(7, MAP_LABELS, state="CLOSED")
    asked = github_board(monkeypatch, [], {7: [gh_issue(8, ["wayfinder:task"], parent=7)]}, named=closed_map)
    tickets = github.GitHubProvider("o/r").fetch(Query(named_map=7))
    assert [(t.number, t.is_open) for t in tickets] == [(7, False), (8, True)]
    assert ("issue", 7) in asked


def test_github_asks_for_a_named_map_only_when_it_is_not_open_and_drops_one_that_is_no_map(monkeypatch):
    asked = github_board(monkeypatch, [gh_issue(1, MAP_LABELS)])
    assert [t.number for t in github.GitHubProvider("o/r").fetch(Query(named_map=1))] == [1]
    assert ("issue", 1) not in asked
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


def gl_issue(iid, labels, body="", state="opened"):
    return {"iid": str(iid), "title": f"t{iid}", "webUrl": f"https://gl/g/p/-/issues/{iid}", "description": body,
            "state": state, "closedAt": None, "assignees": {"nodes": []},
            "labels": {"nodes": [{"title": n} for n in labels]}, "blockedByIssues": {"nodes": []},
            "mergeRequestsCount": 0}


def gitlab_board(monkeypatch, labels, issues, named=None):
    """Fakes `glab api graphql`: the project's labels, issues by label (and state), a named issue."""
    asked = []

    def fake_run_json(args, stdin=None):
        body = json.loads(stdin)
        variables = body["variables"]
        if "labels(first" in body["query"]:
            return {"data": {"project": {"labels": page([{"title": title} for title in labels])}}}
        if "named" in variables:
            asked.append(("named", variables["named"]))
            return {"data": {"project": {"issue": named}}}
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
    assert asked == [("opened", ("wayfinder::map",)), (None, ("wayfinder::grilling",)), (None, ("wayfinder::task",))]
    assert [t.number for t in tickets] == [1, 20, 5, 3, 21]
    assert (tickets[2].parent_number, tickets[2].parent_url) == (1, "https://gl/g/p/-/issues/1")
    assert lanes(tickets) == {1: {"FRONTIER": [5], "DECIDED": [3]}, 20: {"FRONTIER": [21]}}


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


def gt_issue(number, labels, body="", state="open", assignees=()):
    return {"number": number, "title": f"t{number}", "html_url": f"https://gt/o/r/issues/{number}", "body": body,
            "state": state, "assignees": [{"login": a} for a in assignees], "labels": [{"name": n} for n in labels]}


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


def test_gitea_fetches_every_open_map_with_its_children_by_their_part_of_line(monkeypatch):
    # Gitea drops an unknown label filter and lists every issue, so only labels that exist are asked for.
    asked = gitea_board(monkeypatch, ["wayfinder:map", "wayfinder:research", "bug"], ["wayfinder:task"], GITEA_ISSUES,
                        dependencies={5: [{"number": 2, "state": "open", "repository": {"full_name": "o/r"}}]})
    tickets = gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query())
    assert asked == [("open", "wayfinder:map"), ("all", "wayfinder:research"), ("all", "wayfinder:task")]
    assert [t.number for t in tickets] == [1, 20, 5, 3, 2, 21]
    assert lanes(tickets) == {1: {"BLOCKED": [5], "CLAIMED": [2], "DECIDED": [3]}, 20: {"FRONTIER": [21]}}


def test_gitea_adds_a_named_closed_map_without_org_labels(monkeypatch):
    asked = gitea_board(monkeypatch, ["wayfinder:map", "wayfinder:task"], None, GITEA_ISSUES)
    tickets = gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query(named_map=30))
    assert ("issue", 30) in asked and {30, 31} <= {t.number for t in tickets}
    asked = gitea_board(monkeypatch, ["bug"], None, GITEA_ISSUES)
    assert gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query()) == []
    assert asked == []  # no wayfinder labels: no issue listed


def test_tea_config_token_lookup():
    text = "logins:\n  - name: cb\n    url: https://codeberg.org\n    token: abc\n  - url: https://other.io\n    token: zzz\n"
    assert token_from_tea_config(text, "codeberg.org") == "abc"
    assert token_from_tea_config(text, "missing.io") is None


def test_mentions_parent():
    assert mentions_parent("Part of #12.", 12)
    assert not mentions_parent("see #123", 12)
    assert not mentions_parent("&#12;", 12)


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
    monkeypatch.setattr(module, "run_json", lambda args, stdin=None: {"errors": errors})
    provider = github.GitHubProvider("o/r") if module is github else gitlab.GitLabProvider("g/p")
    with pytest.raises(RateLimited):
        provider.fetch(Query())


def http_error(code, reason):
    return urllib.error.HTTPError("https://codeberg.org/api/v1/x", code, reason, {}, io.BytesIO(b""))


def test_gitea_http_429_is_rate_limited(monkeypatch):
    def urlopen(request, timeout=None):
        raise http_error(429, "Too Many Requests")

    monkeypatch.setattr(gitea.urllib.request, "urlopen", urlopen)
    with pytest.raises(RateLimited):
        gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query())


def test_gitea_rate_limit_on_dependencies_is_not_swallowed(monkeypatch):
    gitea_board(monkeypatch, ["wayfinder:map"], None, [gt_issue(1, ["wayfinder:map"])])
    fake_get = gitea.GiteaProvider._get

    def limited(self, path, params=None):
        if path.endswith("/dependencies"):
            raise RateLimited("codeberg.org: HTTP 429 rate limit exceeded")
        return fake_get(self, path, params)

    monkeypatch.setattr(gitea.GiteaProvider, "_get", limited)
    with pytest.raises(RateLimited):
        gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query())


def test_gitea_other_http_errors_are_not_rate_limited(monkeypatch):
    def urlopen(request, timeout=None):
        raise http_error(404, "Not Found")

    monkeypatch.setattr(gitea.urllib.request, "urlopen", urlopen)
    with pytest.raises(ProviderError) as raised:
        gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query())
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

    def fake_run_cli(args, stdin=None, check=True):
        body = json.loads(stdin)
        calls.append(body)
        if "q" in body["variables"]:
            nodes = [node(7, "Blocked by: #8, #50, #51", [(8, "OPEN", "o/r"), (3, "OPEN", "x/y"), (9, "CLOSED", "o/r")])]
            return json.dumps({"data": {"search": page(nodes)}})
        if "withSubs" in body["variables"]:
            issue = {"url": "u", "labels": {"nodes": []}, "subIssues": page([]), "timelineItems": page([])}
            return json.dumps({"data": {"repository": {"issue": issue}}})
        errors = [{"type": "NOT_FOUND", "message": "Could not resolve"}]
        return json.dumps({"data": {"repository": {"i50": {"state": "OPEN"}, "i51": None}}, "errors": errors})

    monkeypatch.setattr(github, "run_cli", fake_run_cli)
    monkeypatch.setattr(providers, "run_cli", fake_run_cli)
    [t] = github.GitHubProvider("o/r").fetch(Query())
    assert (t.blocked_by, t.open_blockers) == (("#8", "x/y#3", "#50"), 3)
    assert "i50: issueOrPullRequest(number: 50)" in calls[-1]["query"]


def test_github_lookup_raises_on_errors_other_than_not_found(monkeypatch):
    errors = [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]
    monkeypatch.setattr(github, "run_cli", lambda args, stdin=None, check=True: json.dumps({"data": None, "errors": errors}))
    with pytest.raises(RateLimited):
        github.GitHubProvider("o/r")._issue_states([5])


def test_github_viewer(monkeypatch):
    monkeypatch.setattr(github, "run_json", lambda args, stdin=None: {"data": {"viewer": {"login": "kareblak"}}})
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
    provider = gitea.GiteaProvider("o/r", "codeberg.org", token="x")
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
        gitea.GiteaProvider("o/r", "codeberg.org", token="x")._issue_states([5])


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
    monkeypatch.setattr(github, "run_json", lambda args, stdin=None: answer)
    answer.update(errors=[{"type": "NOT_FOUND", "message": "Could not resolve to an Issue with the number of 9."}])
    assert github.GitHubProvider("o/r")._issue(9) is None
    answer.update(errors=[{"type": "FORBIDDEN", "message": "Resource not accessible"}])
    with pytest.raises(ProviderError):
        github.GitHubProvider("o/r")._issue(9)


def test_gitea_missing_map_issue_is_no_map_but_other_errors_surface(monkeypatch):
    def urlopen(request, timeout=None):
        raise http_error(404 if request.full_url.endswith("/9") else 500, "x")

    monkeypatch.setattr(gitea.urllib.request, "urlopen", urlopen)
    provider = gitea.GiteaProvider("o/r", "codeberg.org", token="x")
    assert provider._issue(9) is None
    with pytest.raises(ProviderError):
        provider._issue(8)


def test_github_not_planned_close_reason_is_read():
    node = {"number": 1, "title": "T", "url": "u", "state": "CLOSED", "stateReason": "NOT_PLANNED",
            "assignees": {"nodes": []}, "labels": {"nodes": []}}
    assert github.to_ticket(node, 0).not_planned is True
    assert github.to_ticket({**node, "stateReason": "COMPLETED"}, 0).not_planned is False
    assert "stateReason" in github.ISSUE_FIELDS


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_gitea_only_sends_gets_with_the_token(monkeypatch):
    requests = []

    def urlopen(request, timeout=None):
        requests.append(request)
        return FakeResponse(b'{"login": "me"}')

    monkeypatch.setattr(gitea.urllib.request, "urlopen", urlopen)
    assert gitea.GiteaProvider("o/r", "codeberg.org", token="x").viewer() == "me"
    [request] = requests
    assert (request.get_method(), request.full_url, request.data) == ("GET", "https://codeberg.org/api/v1/user", None)
    assert request.get_header("Authorization") == "token x"


@pytest.mark.parametrize("provider", [github.GitHubProvider("o/r"), gitlab.GitLabProvider("g/p"),
                                      gitea.GiteaProvider("o/r", "codeberg.org", token="x")])
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


# A map (#1) with a decision ticket (#2), the spec (#3) and its implementation tickets (#4, #5);
# #6 mentions the map and #7 names another spec, so neither belongs to the spec.
FAMILY = {
    1: ("Map", "Notes", ["wayfinder:map"]),
    2: ("Decide", "Part of #1", ["wayfinder:grilling"]),
    3: ("Spec", "Part of #1\n\n## Problem Statement", ["ready-for-agent"]),
    4: ("Slice A", "## Parent\n\n#3\n\n## What to build", ["ready-for-agent"]),
    5: ("Slice B", "## Parent\n\n#3\n", []),
    6: ("Aside", "see #1", []),
    7: ("Elsewhere", "## Parent\n\n#30\n", []),
}


def assert_map_family(tickets, map_url, spec_url):
    by_number = {t.number: t for t in tickets}
    assert {1, 2, 3, 4, 5} <= set(by_number) and 7 not in by_number
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


def github_children(monkeypatch, sub_issues, references, labels_of=lambda n: FAMILY[n][2]):
    asked = []

    def fake_run_json(args, stdin=None):
        if "q" in json.loads(stdin)["variables"]:
            return {"data": {"search": page([family_node(1)])}}
        number = json.loads(stdin)["variables"]["number"]
        asked.append(number)
        issue = {"url": f"https://gh/{number}", "labels": {"nodes": [{"name": label} for label in labels_of(number)]},
                 "subIssues": {"pageInfo": {"hasNextPage": False}, "nodes": sub_issues.get(number, [])},
                 "timelineItems": {"pageInfo": {"hasNextPage": False},
                                   "nodes": [{"source": s} for s in references.get(number, [])] + [{"source": {}}]}}
        return {"data": {"repository": {"issue": issue}}}

    monkeypatch.setattr(github, "run_json", fake_run_json)
    return asked


def test_github_reads_each_maps_spec_and_its_implementation_tickets(monkeypatch):
    # #2 is a native sub-issue of the map and #4 one of the spec; #3 and #5 link by body only,
    # so GitHub reports them as cross-references; #6 and #8 (another repo) mention without linking.
    sub_issues = {1: [family_node(2, parent=1)], 3: [family_node(4, parent=3)]}
    references = {1: [family_node(3), family_node(6), family_node(2, parent=1)],
                  3: [family_node(5), family_node(4, parent=3), {**family_node(5, repo="x/y"), "number": 8}]}
    asked = github_children(monkeypatch, sub_issues, references)
    tickets = github.GitHubProvider("o/r").fetch(Query())
    assert_map_family(tickets, "https://gh/1", "https://gh/3")
    assert [t.number for t in tickets] == [1, 2, 3, 4, 5]  # the map, its order, then the spec, then its tickets
    assert sorted(asked) == [1, 3]  # the decision ticket is not a spec, so its children are not asked for


def test_github_map_order_is_the_maps_sub_issue_order(monkeypatch):
    calls = []

    def fake_run_json(args, stdin=None):
        body = json.loads(stdin)
        calls.append(body["variables"])
        after = body["variables"]["after"]
        nodes, more = ([{"number": 9}, {"number": 4}], True) if after is None else ([{"number": 6}], False)
        page = {"pageInfo": {"hasNextPage": more, "endCursor": "c1"}, "nodes": nodes}
        return {"data": {"repository": {"issue": {"subIssues": page}}}}

    monkeypatch.setattr(github, "run_json", fake_run_json)
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
    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError("https://codeberg.org/api/v1/x", 429, "Too Many Requests", headers,
                                     io.BytesIO(b""))

    monkeypatch.setattr(gitea.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gitea, "now", lambda: datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc))
    with pytest.raises(RateLimited) as raised:
        gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query())
    assert raised.value.status == 429 and raised.value.resets_at == resets_at
    assert resets_at is None or raised.value.resets_at.tzinfo is not None


def test_gitea_errors_carry_the_trackers_own_message(monkeypatch):
    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError("https://codeberg.org/api/v1/x", 403, "Forbidden", {},
                                     io.BytesIO(b'{"message": "token does not have required scope"}'))

    monkeypatch.setattr(gitea.urllib.request, "urlopen", urlopen)
    with pytest.raises(ProviderError) as raised:
        gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query())
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
