import io
import json
import urllib.error

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
        "labels": {"nodes": [{"title": "review::ux"}]}, "blockedByIssues": {"nodes": []}, "mergeRequestsCount": 1,
    }
    t = gitlab.to_ticket(node)
    assert (t.number, t.is_open, t.open_blockers, t.has_open_change_request) == (12, True, 0, True)
    assert t.review_tags == ["ux"] and t.author == "gl-user"


def test_gitlab_parent_falls_back_to_mentions(monkeypatch):
    def node(iid, body):
        return {"iid": str(iid), "title": "t", "webUrl": "u", "description": body, "state": "opened",
                "closedAt": None, "assignees": {"nodes": []}, "labels": {"nodes": []},
                "blockedByIssues": {"nodes": []}, "mergeRequestsCount": 0}

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
    t = gitea.to_ticket(issue, (), ["Refactor\nFixes #4"])
    assert t.has_open_change_request and t.review_tags == ["perf"] and t.assignees == ()
    assert t.author == "gt-user"
    assert gitea.to_ticket(issue, (), ["Fixes #40"]).has_open_change_request is False


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
            return [{"number": 1, "state": "open"}, {"number": 3, "state": "closed"}]
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
                "assignees": {"nodes": []}, "labels": {"nodes": []},
                "blockedBy": {"nodes": [{"number": n, "state": s, "repository": {"nameWithOwner": repo}}
                                        for n, s, repo in blocked_by]}}

    calls = []

    def fake_run_cli(args, stdin=None, check=True):
        body = json.loads(stdin)
        calls.append(body)
        if "q" in body["variables"]:
            nodes = [node(7, "Blocked by: #8, #50, #51", [(8, "OPEN", "o/r"), (3, "OPEN", "x/y"), (9, "CLOSED", "o/r")])]
            nodes = nodes if "is:open" in body["variables"]["q"] else []
            return json.dumps({"data": {"search": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}})
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
             "closedAt": None, "assignees": {"nodes": []}, "labels": {"nodes": []}, "mergeRequestsCount": 0,
             "blockedByIssues": {"nodes": [{"iid": "13", "state": "opened", "reference": "g/p#13"},
                                           {"iid": "14", "state": "closed", "reference": "g/p#14"},
                                           {"iid": "3", "state": "opened", "reference": "g/other#3"}]}}

    def fake_run_json(args, stdin=None):
        variables = json.loads(stdin)["variables"]
        if "iids" in variables:
            assert variables["iids"] == ["20"]
            return {"data": {"project": {"issues": {"nodes": [{"iid": "20", "state": "opened"}]}}}}
        nodes = [issue] if variables["state"] == "opened" else []
        return {"data": {"project": {"issues": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}

    monkeypatch.setattr(gitlab, "run_json", fake_run_json)
    [t] = gitlab.GitLabProvider("g/p").fetch(Query())
    assert (t.blocked_by, t.open_blockers) == (("#13", "g/other#3", "#20"), 3)


def test_gitlab_viewer(monkeypatch):
    monkeypatch.setattr(gitlab, "run_json", lambda args, stdin=None: {"data": {"currentUser": {"username": "gl"}}})
    assert gitlab.GitLabProvider("g/p").viewer() == "gl"


def test_gitea_native_and_body_blockers(monkeypatch):
    def fake_get(self, path, params=None):
        if path.endswith("/issues"):
            return [{"number": 1, "title": "t", "html_url": "u", "state": "open", "labels": [],
                     "body": "Blocked by: #5, #6"}] if params["state"] == "open" else []
        if path.endswith("/1/dependencies"):
            return [{"number": 2, "state": "open", "repository": {"full_name": "o/r"}},
                    {"number": 3, "state": "open", "repository": {"full_name": "x/y"}},
                    {"number": 4, "state": "closed", "repository": {"full_name": "o/r"}}]
        if path.endswith("/issues/5"):
            return {"number": 5, "state": "open"}
        if path.endswith("/issues/6"):
            raise ProviderError("codeberg.org/x: HTTP 404 Not Found")
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


def test_github_delete_runs_gh_issue_delete(monkeypatch):
    calls = []
    monkeypatch.setattr(github, "run_cli", lambda args, stdin=None: calls.append(args) or "")
    github.GitHubProvider("o/r", host="ghe.example.com").delete(7)
    assert calls == [["gh", "issue", "delete", "7", "--repo", "ghe.example.com/o/r", "--yes"]]


def test_gitlab_delete_calls_the_issue_delete_api(monkeypatch):
    calls = []
    monkeypatch.setattr(gitlab, "run_cli", lambda args, stdin=None: calls.append(args) or "")
    gitlab.GitLabProvider("g/sub/p", host="gl.example.com").delete(12)
    assert calls == [["glab", "api", "--hostname", "gl.example.com", "-X", "DELETE", "projects/g%2Fsub%2Fp/issues/12"]]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_gitea_delete_sends_delete_with_the_token(monkeypatch):
    requests = []

    def urlopen(request, timeout=None):
        requests.append(request)
        return FakeResponse(b"")  # 204 No Content

    monkeypatch.setattr(gitea.urllib.request, "urlopen", urlopen)
    gitea.GiteaProvider("o/r", "codeberg.org", token="x").delete(4)
    [request] = requests
    assert (request.get_method(), request.full_url) == ("DELETE", "https://codeberg.org/api/v1/repos/o/r/issues/4")
    assert request.get_header("Authorization") == "token x"


@pytest.mark.parametrize("module", [github, gitlab])
def test_cli_delete_errors_are_provider_errors(monkeypatch, module):
    stderr = "GraphQL: kareblak does not have the correct permissions to execute `DeleteIssue`"
    monkeypatch.setattr(providers.subprocess, "run", lambda *a, **k: FakeCompleted(stderr))
    provider = github.GitHubProvider("o/r") if module is github else gitlab.GitLabProvider("g/p")
    with pytest.raises(ProviderError, match="permissions"):
        provider.delete(1)


def test_gitea_delete_forbidden_is_a_provider_error(monkeypatch):
    def urlopen(request, timeout=None):
        raise http_error(403, "Forbidden")

    monkeypatch.setattr(gitea.urllib.request, "urlopen", urlopen)
    with pytest.raises(ProviderError, match="403 Forbidden"):
        gitea.GiteaProvider("o/r", "codeberg.org", token="x").delete(4)
