"""Linear OAuth 2.0 with PKCE, scope `read`: `ticketmaster login` and the board's token refresh.

The OAuth app is one the user registers in their own workspace (Linear: Settings, API, OAuth applications) with the
callback URL CALLBACK_URL; its client id is `oauth_client_id` in `.ticketmaster.toml`. Ticketmaster ships none, and
as a public client it holds no secret: PKCE stands in for one.

Only `login` opens a browser. The token goes to a mode-0600 JSON file under `~/.config/ticketmaster/`, one per client
id; the board refreshes it there when it has expired, or when Linear rejects it, and writes the rotated refresh token
back. That is a local write, never one to Linear.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import ProviderError

AUTHORIZE_URL = "https://linear.app/oauth/authorize"
TOKEN_URL = "https://api.linear.app/oauth/token"
SCOPE = "read"
CALLBACK_PORT = 8719
CALLBACK_PATH = "/callback"
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"  # the URL to register on the OAuth app
LOGIN_TIMEOUT = 300  # seconds `login` waits for the browser
EXPIRY_MARGIN = 60  # seconds before its expiry a token is refreshed
CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.A)  # also the token file's name, so no path in it
RELOGIN = "run `ticketmaster login` again"


def check_client_id(client_id: str) -> str:
    """A Linear OAuth app's client id; the error never repeats the value."""
    if not CLIENT_ID.fullmatch(client_id):
        raise ProviderError("bad oauth_client_id in .ticketmaster.toml: the client id of a Linear OAuth app "
                            "(letters, digits, `-` and `_`)")
    return client_id


@dataclass(frozen=True)
class TokenStore:
    """One OAuth app's token: `access_token`, `refresh_token`, `expires_at` (epoch seconds) and `scope`."""

    path: Path

    @classmethod
    def for_client(cls, client_id: str) -> TokenStore:
        # The same per-user directory as config.settings_path.
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ticketmaster"
        return cls(base / f"linear-{check_client_id(client_id)}.json")

    def load(self) -> dict | None:
        try:
            token = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        return token if isinstance(token, dict) and token.get("access_token") else None

    def save(self, token: dict) -> None:
        """Written to a mode-0600 file beside it, then renamed over it, so no reader sees half a token."""
        partial = self.path.with_name(f".{self.path.name}.{os.getpid()}")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump(token, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(partial, self.path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)
            raise ProviderError(f"linear: cannot write the login to {self.path}: {exc.strerror or exc}") from exc

    def locked(self):
        """An exclusive lock on the token, held by one refresh at a time across every board of the user."""
        return _Lock(self.path.with_name(f".{self.path.name}.lock"))


class _Lock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ProviderError(f"linear: cannot lock the login at {self.path}: {exc.strerror or exc}") from exc
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        return False


def from_reply(reply: dict, now: float, previous: dict | None = None) -> dict:
    """The token to store from the token endpoint's reply; a refresh that rotates nothing keeps the old refresh
    token."""
    if not isinstance(reply, dict) or not reply.get("access_token"):
        raise ProviderError("linear: the token endpoint sent no access token")
    token = {"access_token": reply["access_token"]}
    refresh = reply.get("refresh_token") or (previous or {}).get("refresh_token")
    if refresh:
        token["refresh_token"] = refresh
    if isinstance(reply.get("expires_in"), (int, float)):
        token["expires_at"] = now + reply["expires_in"]
    if reply.get("scope"):
        token["scope"] = reply["scope"]
    return token


def token_post(form: dict) -> dict:
    """POSTs a form to Linear's token endpoint, redirects refused as for the GraphQL endpoint."""
    from .linear import LinearHTTP

    request = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with LinearHTTP.opener().open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            reply = json.load(exc)
        except ValueError:
            reply = {}
        reason = ": ".join(str(reply[k]) for k in ("error", "error_description") if reply.get(k))
        raise ProviderError(f"linear: the token endpoint refused (HTTP {exc.code}) {reason}".rstrip(), exc.code) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError(f"linear: the token endpoint: {exc}") from exc


def expired(token: dict) -> bool:
    return token.get("expires_at") is not None and token["expires_at"] - EXPIRY_MARGIN <= time.time()


class OAuthSession:
    """The Authorization header for the stored token, refreshed first when it has expired.

    The file is read for every request, and a refresh holds the token's lock and reads it again first, so of
    several boards meeting the same expired token one refreshes and the others take its rotated token instead of
    sending a refresh token Linear no longer accepts."""

    def __init__(self, client_id: str, store: TokenStore):
        self.client_id, self.store = client_id, store

    def authorization(self) -> str:
        token = self._token()
        if expired(token):
            token = self.refresh(token["access_token"])
        return f"Bearer {token['access_token']}"

    def refresh(self, stale: str) -> dict:
        """A new token in place of the access token `stale`, unless another board has already replaced it."""
        with self.store.locked():
            token = self._token()
            if token["access_token"] != stale and not expired(token):
                return token
            return self._refresh(token)

    def _refresh(self, token: dict) -> dict:
        if not token.get("refresh_token"):
            raise ProviderError(f"linear: the stored login has expired and has no refresh token; {RELOGIN}")
        form = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": self.client_id}
        try:
            reply = token_post(form)
        except ProviderError as exc:
            raise ProviderError(f"{exc}; {RELOGIN}", exc.status) from exc
        fresh = from_reply(reply, time.time(), token)
        self.store.save(fresh)
        return fresh

    def _token(self) -> dict:
        token = self.store.load()
        if token is None:
            raise ProviderError(f"linear: no stored login in {self.store.path}; {RELOGIN}")
        return token


def pkce_pair() -> tuple[str, str]:
    """A code verifier (86 URL-safe characters) and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(client_id: str, open_browser: Callable[[str], bool] | None = None, port: int = CALLBACK_PORT,
          now: Callable[[], float] = time.time, out: TextIO = sys.stdout, timeout: float = LOGIN_TIMEOUT) -> TokenStore:
    """Opens Linear's consent page in the browser, takes the code on a localhost callback, exchanges it with the
    PKCE verifier and stores the token. `port=0` takes a free port (the tests: Linear only accepts the registered
    callback URL)."""
    if open_browser is None:
        import webbrowser

        open_browser = webbrowser.open
    store = TokenStore.for_client(client_id)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    with _CallbackServer(state, port) as server:
        redirect_uri = f"http://localhost:{server.server_address[1]}{CALLBACK_PATH}"
        url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        print(f"Opening Linear in the browser to log in; if it does not open, go to:\n\n  {url}\n", file=out, flush=True)
        open_browser(url)
        code = server.wait(timeout)
    reply = token_post({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                        "client_id": client_id, "code_verifier": verifier})
    store.save(from_reply(reply, now()))
    return store


class _CallbackServer(http.server.HTTPServer):
    """Serves the callback on 127.0.0.1 until a request with the login's `state` comes; any other is answered and
    ignored, so a stray or forged request cannot end the login."""

    def __init__(self, state: str, port: int):
        self.state, self.result = state, None
        try:
            super().__init__(("127.0.0.1", port), _CallbackHandler)
        except OSError as exc:
            raise ProviderError(f"linear: cannot listen on localhost:{port} for the login callback: {exc}") from exc

    def wait(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while self.result is None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ProviderError(f"linear: no login callback within {timeout:g} seconds; {RELOGIN}")
            self.timeout = left
            self.handle_request()
        params = self.result
        if params.get("error"):
            reason = ": ".join(params[k] for k in ("error", "error_description") if params.get(k))
            raise ProviderError(f"linear: the login was refused: {reason}")
        if not params.get("code"):
            raise ProviderError("linear: the login callback came without a code")
        return params["code"]


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server: _CallbackServer
    timeout = 10  # a connection that sends nothing (a browser's preconnect) must not hold up the login

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        params = dict(urllib.parse.parse_qsl(parts.query))
        if parts.path != CALLBACK_PATH or not secrets.compare_digest(params.get("state", ""), self.server.state):
            self._reply(400, "Not this login's callback.")
            return
        self.server.result = params
        if params.get("error"):
            self._reply(400, "Linear refused the login; see the terminal.")
        else:
            self._reply(200, "Logged in to Linear for ticketmaster. You can close this tab.")

    def _reply(self, status: int, text: str) -> None:
        body = f"<!doctype html><meta charset=utf-8><title>ticketmaster</title><p>{text}</p>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass
