import io
import json
import urllib.error

import pytest

from ticketmaster import providers
from ticketmaster.providers import ProviderError, Query, RateLimited, github, gitlab, gitea, mentions_parent
from ticketmaster.providers.gitea import token_from_tea_config


def test_github_node_to_ticket():
    node = {
        "number": 7, "title": "T", "url": "https://x/7", "body": "b", "state": "OPEN", "closedAt": None,
        "author": {"login": "opener"},
        "assignees": {"nodes": [{"login": "kareblak"}]},
        "labels": {"nodes": [{"name": "review:security"}]},
        "issueDependenciesSummary": {"blockedBy": 1},
        "closedByPullRequestsReferences": {"nodes": [{"state": "MERGED"}, {"state": "OPEN"}]},
        "parent": {"number": 2, "url": "https://x/2", "labels": {"nodes": [{"name": "wayfinder:map"}]}},
    }
    t = github.to_ticket(node, order=3)
    assert (t.parent_number, t.parent_url, t.parent_labels) == (2, "https://x/2", ("wayfinder:map",))
    assert (t.number, t.is_open, t.open_blockers, t.has_open_change_request, t.order) == (7, True, 1, True, 3)
    assert t.assignees == ("kareblak",) and t.review_tags == ["security"]
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


def test_github_sends_variables_as_json(monkeypatch):
    calls = []

    def fake_run_json(args, stdin=None):
        calls.append((args, json.loads(stdin)))
        return {"data": {"search": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}

    monkeypatch.setattr(github, "run_json", fake_run_json)
    github.GitHubProvider("o/r").fetch(github.Query(label="bug"))
    queries = [body["variables"]["q"] for _, body in calls]
    assert queries[0] == 'repo:o/r is:issue label:"bug" is:open'
    assert queries[1].startswith('repo:o/r is:issue label:"bug" is:closed closed:>=')


def test_github_default_scope_is_every_issue(monkeypatch):
    calls = []

    def fake_run_json(args, stdin=None):
        calls.append(json.loads(stdin)["variables"]["q"])
        return {"data": {"search": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}

    monkeypatch.setattr(github, "run_json", fake_run_json)
    github.GitHubProvider("o/r").fetch(github.Query())
    assert calls[0] == "repo:o/r is:issue is:open"
    assert calls[1].startswith("repo:o/r is:issue is:closed closed:>=") and len(calls) == 2
    assert not any("assignee" in q for q in calls)


def test_gitlab_node_to_ticket():
    node = {
        "iid": "12", "title": "T", "webUrl": "https://gl/12", "description": "d", "state": "opened",
        "closedAt": None, "author": {"username": "gl-user"}, "assignees": {"nodes": []},
        "labels": {"nodes": [{"title": "review::ux"}]}, "blockedByCount": 0, "mergeRequestsCount": 1,
    }
    t = gitlab.to_ticket(node)
    assert (t.number, t.is_open, t.open_blockers, t.has_open_change_request) == (12, True, 0, True)
    assert t.review_tags == ["ux"] and t.author == "gl-user"


def test_gitlab_parent_falls_back_to_mentions(monkeypatch):
    def node(iid, body):
        return {"iid": str(iid), "title": "t", "webUrl": "u", "description": body, "state": "opened",
                "closedAt": None, "assignees": {"nodes": []}, "labels": {"nodes": []},
                "blockedByCount": 0, "mergeRequestsCount": 0}

    def fake_run_json(args, stdin=None):
        variables = json.loads(stdin)["variables"]
        if "iid" in variables:
            issue = {"webUrl": "https://gl/1", "labels": {"nodes": [{"title": "wayfinder:map"}]}}
            return {"data": {"project": {"issue": issue}}}
        nodes = [node(1, "map"), node(2, "Part of #1"), node(3, "see #10")] if variables["state"] == "opened" else []
        return {"data": {"project": {"issues": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}

    monkeypatch.setattr(gitlab, "run_json", fake_run_json)
    tickets = gitlab.GitLabProvider("g/p").fetch(gitlab.Query(parent=1))
    assert [t.number for t in tickets] == [2]
    assert (tickets[0].parent_url, tickets[0].parent_labels) == ("https://gl/1", ("wayfinder:map",))


def test_gitea_issue_to_ticket_detects_closing_pr():
    issue = {"number": 4, "title": "T", "html_url": "https://gt/4", "body": "", "state": "open",
             "closed_at": None, "user": {"login": "gt-user"}, "assignees": None, "labels": [{"name": "review:perf"}]}
    t = gitea.to_ticket(issue, 0, ["Refactor\nFixes #4"])
    assert t.has_open_change_request and t.review_tags == ["perf"] and t.assignees == ()
    assert t.author == "gt-user"
    assert gitea.to_ticket(issue, 0, ["Fixes #40"]).has_open_change_request is False


def test_gitea_fetch_default_scope_is_every_issue_with_blockers(monkeypatch):
    issues_open = [
        {"number": 1, "title": "mine", "html_url": "u", "state": "open", "assignees": [{"login": "me"}], "labels": []},
        {"number": 2, "title": "free", "html_url": "u", "state": "open", "assignees": [], "labels": []},
        {"number": 3, "title": "theirs", "html_url": "u", "state": "open", "assignees": [{"login": "bob"}], "labels": []},
    ]

    def fake_get(self, path, params=None):
        if path == "/user":
            return {"login": "me"}
        if path.endswith("/issues"):
            return issues_open if params["state"] == "open" else []
        if path.endswith("/pulls"):
            return []
        if path.endswith("/2/dependencies"):
            return [{"state": "open"}, {"state": "closed"}]
        return []

    monkeypatch.setattr(gitea.GiteaProvider, "_get", fake_get)
    provider = gitea.GiteaProvider("o/r", "codeberg.org", token="x")
    tickets = {t.number: t for t in provider.fetch(gitea.Query())}
    assert sorted(tickets) == [1, 2, 3]  # all open issues, whoever is assigned
    assert tickets[2].open_blockers == 1 and tickets[1].open_blockers == 0


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
    def fake_get(self, path, params=None):
        if path.endswith("/dependencies"):
            raise RateLimited("codeberg.org: HTTP 429 rate limit exceeded")
        if path.endswith("/issues"):
            return [{"number": 1, "title": "t", "html_url": "u", "state": "open", "labels": []}] \
                if params["state"] == "open" else []
        return []

    monkeypatch.setattr(gitea.GiteaProvider, "_get", fake_get)
    with pytest.raises(RateLimited):
        gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query())


def test_gitea_other_http_errors_are_not_rate_limited(monkeypatch):
    def urlopen(request, timeout=None):
        raise http_error(404, "Not Found")

    monkeypatch.setattr(gitea.urllib.request, "urlopen", urlopen)
    with pytest.raises(ProviderError) as raised:
        gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query())
    assert not isinstance(raised.value, RateLimited)


def test_spec_links_to_the_map_with_a_part_of_line():
    assert providers.part_of("Part of #5\n\n## Problem Statement", 5)
    assert providers.part_of("part of #5", 5)
    assert not providers.part_of("Part of #50", 5)
    assert not providers.part_of("This is not part of #5", 5)  # the keyword starts a line


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
    assert {2, 3, 4, 5} <= set(by_number) and 7 not in by_number and 1 not in by_number
    assert (by_number[3].parent_number, by_number[3].parent_url, by_number[3].parent_labels) == (1, map_url, ("wayfinder:map",))
    for n in (4, 5):
        assert (by_number[n].parent_number, by_number[n].parent_url) == (3, spec_url)
        assert by_number[n].parent_labels == ("ready-for-agent",)
    assert by_number[2].is_wayfinder_ticket and not by_number[3].is_wayfinder_ticket
    assert not by_number[4].is_wayfinder_ticket


def test_gitlab_map_board_includes_the_spec_and_its_implementation_tickets(monkeypatch):
    def node(iid):
        title, body, labels = FAMILY[iid]
        return {"iid": str(iid), "title": title, "webUrl": f"https://gl/{iid}", "description": body,
                "state": "opened", "closedAt": None, "assignees": {"nodes": []},
                "labels": {"nodes": [{"title": label} for label in labels]},
                "blockedByCount": 0, "mergeRequestsCount": 0}

    def fake_run_json(args, stdin=None):
        variables = json.loads(stdin)["variables"]
        if "iid" in variables:
            issue = node(int(variables["iid"]))
            return {"data": {"project": {"issue": {"webUrl": issue["webUrl"], "labels": issue["labels"]}}}}
        nodes = [node(n) for n in FAMILY] if variables["state"] == "opened" else []
        return {"data": {"project": {"issues": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}

    monkeypatch.setattr(gitlab, "run_json", fake_run_json)
    assert_map_family(gitlab.GitLabProvider("g/p").fetch(Query(parent=1)), "https://gl/1", "https://gl/3")


def test_gitea_map_board_includes_the_spec_and_its_implementation_tickets(monkeypatch):
    def issue(n):
        title, body, labels = FAMILY[n]
        return {"number": n, "title": title, "html_url": f"https://gt/{n}", "body": body, "state": "open",
                "assignees": [], "labels": [{"name": label} for label in labels]}

    dependencies = []

    def fake_get(self, path, params=None):
        if path.endswith("/issues"):
            return [issue(n) for n in FAMILY] if params["state"] == "open" else []
        if path.endswith("/dependencies"):
            dependencies.append(int(path.split("/")[-2]))
            return []
        if "/issues/" in path:
            return issue(int(path.rsplit("/", 1)[-1]))
        return []

    monkeypatch.setattr(gitea.GiteaProvider, "_get", fake_get)
    tickets = gitea.GiteaProvider("o/r", "codeberg.org", token="x").fetch(Query(parent=1))
    assert_map_family(tickets, "https://gt/1", "https://gt/3")
    assert 7 not in dependencies and 1 not in dependencies  # blockers only for the board's tickets


def github_node(n, parent=None, repo="o/r"):
    title, body, labels = FAMILY[n]
    node = {"number": n, "title": title, "url": f"https://gh/{n}", "body": body, "state": "OPEN",
            "assignees": {"nodes": []}, "labels": {"nodes": [{"name": label} for label in labels]},
            "repository": {"nameWithOwner": repo}}
    if parent is not None:
        node["parent"] = {"number": parent, "url": f"https://gh/{parent}",
                          "labels": {"nodes": [{"name": label} for label in FAMILY[parent][2]]}}
    return node


def test_github_map_board_includes_the_spec_and_its_implementation_tickets(monkeypatch):
    # #2 is a native sub-issue of the map and #4 one of the spec; #3 and #5 link by body only,
    # so GitHub reports them as cross-references; #6 and #8 (another repo) mention without linking.
    sub_issues = {1: [github_node(2, parent=1)], 3: [github_node(4, parent=3)]}
    references = {1: [github_node(3), github_node(6), github_node(2, parent=1)],
                  3: [github_node(5), github_node(4, parent=3), {**github_node(5, repo="x/y"), "number": 8}]}
    asked = []

    def fake_run_json(args, stdin=None):
        variables = json.loads(stdin)["variables"]
        number = variables["number"]
        asked.append(number)
        issue = {"url": f"https://gh/{number}",
                 "labels": {"nodes": [{"name": label} for label in FAMILY[number][2]]},
                 "subIssues": {"pageInfo": {"hasNextPage": False}, "nodes": sub_issues.get(number, [])},
                 "timelineItems": {"pageInfo": {"hasNextPage": False},
                                   "nodes": [{"source": s} for s in references.get(number, [])] + [{"source": {}}]}}
        return {"data": {"repository": {"issue": issue}}}

    monkeypatch.setattr(github, "run_json", fake_run_json)
    tickets = github.GitHubProvider("o/r").fetch(Query(parent=1))
    assert_map_family(tickets, "https://gh/1", "https://gh/3")
    assert [t.number for t in tickets] == [2, 3, 4, 5]  # map order, then the spec, then its tickets
    assert sorted(asked) == [1, 3]  # the decision ticket is not a spec, so its children are not asked for
