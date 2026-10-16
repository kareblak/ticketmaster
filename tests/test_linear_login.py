import base64
import hashlib
import io
import json
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

import pytest

from ticketmaster.providers import ProviderError
from ticketmaster.providers import linear, linear_oauth
from ticketmaster.providers.linear import LinearHTTP, LinearProvider
from ticketmaster.providers.linear_oauth import TokenStore

VIEWER = {"data": {"viewer": {"displayName": "kaare.blakstad"}}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeLinearHTTP:
    """Answers by URL: the GraphQL endpoint with `graphql`, the token endpoint with the next of `tokens`."""

    def __init__(self, tokens=(), graphql=VIEWER):
        self.tokens, self.graphql = list(tokens), graphql
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        answer = self.tokens.pop(0) if request.full_url == linear_oauth.TOKEN_URL else self.graphql
        return FakeResponse(json.dumps(answer).encode())

    def forms(self):
        return [dict(urllib.parse.parse_qsl(r.data.decode())) for r in self.requests
                if r.full_url == linear_oauth.TOKEN_URL]


def visit(*urls):
    """The browser following the redirect to the callback, in the background: login() is serving it."""
    def get():
        for url in urls:
            try:
                urllib.request.urlopen(url, timeout=10).read()
            except urllib.error.HTTPError:
                pass  # the callback answers a refused login or a forged state with 400

    threading.Thread(target=get, daemon=True).start()


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    return tmp_path / "config"


def fake_http(monkeypatch, **kwargs) -> FakeLinearHTTP:
    fake = FakeLinearHTTP(**kwargs)
    monkeypatch.setattr(LinearHTTP, "opener", staticmethod(lambda: fake))
    return fake


def stored(config_home, client_id="cid", **token):
    store = TokenStore.for_client(client_id)
    store.save({"access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 4_000_000_000, **token})
    return store


def test_login_runs_pkce_against_a_localhost_callback_and_writes_the_token_mode_0600(config_home, monkeypatch):
    fake = fake_http(monkeypatch, tokens=[{"access_token": "acc", "token_type": "Bearer", "expires_in": 86399,
                                           "refresh_token": "ref", "scope": "read"}])
    opened = []

    def browser(url):
        opened.append(url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        callback = f"{query['redirect_uri']}?" + urllib.parse.urlencode({"code": "the-code", "state": query["state"]})
        visit(callback)
        return True

    store = linear_oauth.login("cid", open_browser=browser, port=0, now=lambda: 1_000.0, out=io.StringIO())

    [url] = opened
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://linear.app/oauth/authorize"
    assert (query["client_id"], query["scope"], query["response_type"]) == ("cid", "read", "code")
    assert query["code_challenge_method"] == "S256" and query["redirect_uri"].startswith("http://localhost:")
    [form] = fake.forms()
    verifier = form.pop("code_verifier")
    assert 43 <= len(verifier) <= 128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == challenge
    assert form == {"grant_type": "authorization_code", "code": "the-code", "client_id": "cid",
                    "redirect_uri": query["redirect_uri"]}
    assert store.path == config_home / "ticketmaster" / "linear-cid.json"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert json.loads(store.path.read_text()) == {"access_token": "acc", "refresh_token": "ref",
                                                  "expires_at": 1_000.0 + 86399, "scope": "read"}


def test_login_refuses_a_callback_with_the_wrong_state(config_home, monkeypatch):
    fake = fake_http(monkeypatch)

    def browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        forged = {"code": "forged-code", "state": "forged"}  # first: it must not end the login
        refused = {"error": "access_denied", "state": query["state"]}
        visit(*(f"{query['redirect_uri']}?" + urllib.parse.urlencode(params) for params in (forged, refused)))
        return True

    with pytest.raises(ProviderError, match="access_denied"):
        linear_oauth.login("cid", open_browser=browser, port=0, out=io.StringIO())
    assert fake.forms() == [] and not TokenStore.for_client("cid").path.exists()


def test_an_expired_token_is_refreshed_and_the_rotated_token_written_back(config_home, monkeypatch):
    store = stored(config_home, expires_at=500)
    fake = fake_http(monkeypatch, tokens=[{"access_token": "new-access", "refresh_token": "new-refresh",
                                           "expires_in": 86399}])
    monkeypatch.setattr(linear_oauth.time, "time", lambda: 1_000.0)

    assert LinearProvider("TM", oauth_client_id="cid").viewer() == "kaare.blakstad"

    assert fake.forms() == [{"grant_type": "refresh_token", "refresh_token": "old-refresh", "client_id": "cid"}]
    [graphql] = [r for r in fake.requests if r.full_url == linear.LINEAR_URL]
    assert graphql.get_header("Authorization") == "Bearer new-access"
    assert json.loads(store.path.read_text()) == {"access_token": "new-access", "refresh_token": "new-refresh",
                                                  "expires_at": 1_000.0 + 86399}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_a_rejected_token_is_refreshed_once_and_the_request_retried(config_home, monkeypatch):
    stored(config_home)
    answers = iter([urllib_error(401, {"errors": [{"message": "Authentication required",
                                                   "extensions": {"code": "AUTHENTICATION_ERROR"}}]}), VIEWER])
    fake = fake_http(monkeypatch, tokens=[{"access_token": "new-access", "refresh_token": "new-refresh"}])
    graphql = []

    def open_(request, timeout=None):
        if request.full_url == linear_oauth.TOKEN_URL:
            return FakeLinearHTTP.open(fake, request)
        graphql.append(request.get_header("Authorization"))
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(json.dumps(answer).encode())

    fake.open = open_
    assert LinearProvider("TM", oauth_client_id="cid").viewer() == "kaare.blakstad"
    assert graphql == ["Bearer old-access", "Bearer new-access"]


def test_a_failed_refresh_says_to_log_in_again(config_home, monkeypatch):
    stored(config_home, expires_at=500)
    fake = fake_http(monkeypatch)

    def refused(request, timeout=None):
        raise urllib_error(400, {"error": "invalid_grant", "error_description": "refresh token revoked"},
                           url=linear_oauth.TOKEN_URL)

    fake.open = refused
    with pytest.raises(ProviderError, match="invalid_grant.*ticketmaster login"):
        LinearProvider("TM", oauth_client_id="cid").viewer()


def test_the_key_wins_over_a_stored_token_and_goes_bare(config_home, monkeypatch):
    stored(config_home)
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_secret")
    fake = fake_http(monkeypatch)

    LinearProvider("TM", oauth_client_id="cid").viewer()

    [request] = fake.requests
    assert request.get_header("Authorization") == "lin_api_secret"


def test_without_a_key_the_stored_token_goes_as_bearer(config_home, monkeypatch):
    stored(config_home)
    fake = fake_http(monkeypatch)

    LinearProvider("TM", oauth_client_id="cid").viewer()

    [request] = fake.requests
    assert request.get_header("Authorization") == "Bearer old-access"


@pytest.mark.parametrize("client_id", [None, "cid"])  # no client id configured, or no login for it yet
def test_with_neither_key_nor_token_the_refusal_names_both(config_home, client_id):
    with pytest.raises(ProviderError) as error:
        LinearProvider("TM", oauth_client_id=client_id)
    message = str(error.value)
    assert "LINEAR_API_KEY is not set" in message and "key_env" in message
    assert "ticketmaster login" in message and "oauth_client_id" in message


def test_a_bad_client_id_is_refused(config_home):
    with pytest.raises(ProviderError, match="bad oauth_client_id"):
        LinearProvider("TM", oauth_client_id="../../etc")


def urllib_error(code, body, url=linear.LINEAR_URL):
    return urllib.error.HTTPError(url, code, "Bad Request", Message(), io.BytesIO(json.dumps(body).encode()))


# ---------- the command line ----------

def linear_repo(tmp_path, toml):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".ticketmaster.toml").write_text(toml)
    return repo


def no_browser(monkeypatch):
    import webbrowser

    def refuse(*args, **kwargs):
        raise AssertionError("the board opened a browser")

    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, refuse)
    monkeypatch.setattr(linear_oauth, "login", refuse)


def test_login_without_oauth_client_id_prints_the_config_line(tmp_path, config_home, monkeypatch, capsys):
    from ticketmaster import cli

    no_browser(monkeypatch)
    monkeypatch.chdir(linear_repo(tmp_path, 'provider = "linear"\nteam = "TM"\n'))
    assert cli.main(["login"]) == 2
    err = capsys.readouterr().err
    assert 'oauth_client_id = "<client id>"' in err and ".ticketmaster.toml" in err
    assert "http://localhost:" in err  # the callback URL to register on the OAuth app


def test_login_runs_outside_herdr_and_reports_where_the_token_went(tmp_path, config_home, monkeypatch, capsys):
    from ticketmaster import cli
    from ticketmaster.backends import herdr

    monkeypatch.setattr(herdr, "unavailable", lambda env: "HERDR_ENV is not 1")
    calls = []
    monkeypatch.setattr(linear_oauth, "login", lambda client_id: calls.append(client_id) or TokenStore.for_client(client_id))
    monkeypatch.chdir(linear_repo(tmp_path, 'provider = "linear"\nteam = "TM"\noauth_client_id = "cid"\n'))
    assert cli.main(["login"]) == 0
    assert calls == ["cid"]
    assert "linear-cid.json" in capsys.readouterr().out


def test_login_is_for_linear_only(tmp_path, config_home, monkeypatch, capsys):
    from ticketmaster import cli

    no_browser(monkeypatch)
    monkeypatch.chdir(linear_repo(tmp_path, 'provider = "github"\nrepo = "o/r"\n'))
    assert cli.main(["login"]) == 2
    assert "gh auth login" in capsys.readouterr().err


@pytest.mark.parametrize("token", [None, {"expires_at": 500}])  # no login at all, or one whose refresh fails
def test_the_board_path_never_opens_a_browser(tmp_path, config_home, monkeypatch, capsys, token):
    from types import SimpleNamespace

    from ticketmaster import cli
    from ticketmaster.backends import herdr

    no_browser(monkeypatch)
    if token is not None:
        stored(config_home, **token)
        fake = fake_http(monkeypatch)
        fake.open = lambda request, timeout=None: (_ for _ in ()).throw(
            urllib_error(400, {"error": "invalid_grant"}, url=linear_oauth.TOKEN_URL))
    monkeypatch.setattr(herdr, "unavailable", lambda env: None)
    monkeypatch.setattr(herdr.Herdr, "open_board", lambda self, *args: SimpleNamespace(
        pane="p1", beside="claude", restarted=False))
    monkeypatch.chdir(linear_repo(tmp_path, 'provider = "linear"\nteam = "TM"\noauth_client_id = "cid"\n'))

    code = cli.main([])

    if token is None:
        assert code == 2 and "ticketmaster login" in capsys.readouterr().err
    else:
        from ticketmaster.config import resolve_provider

        assert code == 0  # the board pane meets the failed refresh, and shows it as a read error
        with pytest.raises(ProviderError, match="ticketmaster login"):
            resolve_provider(tmp_path / "repo").viewer()


def test_a_token_another_board_rotated_is_taken_instead_of_refreshing_again(config_home, monkeypatch):
    store = stored(config_home, expires_at=500)
    fake = fake_http(monkeypatch)  # no token answers: a refresh would fail
    session = linear_oauth.OAuthSession("cid", store)
    store.save({"access_token": "rotated", "refresh_token": "rotated-refresh", "expires_at": 4_000_000_000})

    assert session.refresh("old-access")["access_token"] == "rotated"
    assert fake.forms() == []


def test_a_401_without_a_json_body_is_refreshed_and_retried(config_home, monkeypatch):
    import urllib.error

    stored(config_home)
    fake = fake_http(monkeypatch, tokens=[{"access_token": "new-access", "refresh_token": "new-refresh"}])
    rejected = iter([True])

    def open_(request, timeout=None):
        if request.full_url == linear.LINEAR_URL and next(rejected, False):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", Message(), io.BytesIO(b"nope"))
        return FakeLinearHTTP.open(fake, request)

    monkeypatch.setattr(fake, "open", open_)
    assert LinearProvider("TM", oauth_client_id="cid").viewer() == "kaare.blakstad"
    assert fake.forms()[0]["refresh_token"] == "old-refresh"


def test_a_token_file_that_cannot_be_written_is_a_provider_error(config_home):
    config_home.mkdir()
    (config_home / "ticketmaster").write_text("a file, not a directory")
    with pytest.raises(ProviderError, match="cannot write the login"):
        TokenStore.for_client("cid").save({"access_token": "a"})


def test_login_takes_no_map(capsys, monkeypatch):
    from ticketmaster.cli import parse_args

    monkeypatch.setenv("NO_COLOR", "1")
    with pytest.raises(SystemExit):
        parse_args(["login", "42"])
    assert "login takes no map" in capsys.readouterr().err
