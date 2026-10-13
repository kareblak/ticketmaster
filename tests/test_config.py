import pytest

from ticketmaster.config import guess_kind, make_provider, parse_remote
from ticketmaster.providers import ProviderError


@pytest.mark.parametrize(
    "url,host,repo",
    [
        ("git@github.com:shiplog/shiplog.git", "github.com", "shiplog/shiplog"),
        ("https://github.com/kareblak/ticketmaster", "github.com", "kareblak/ticketmaster"),
        ("https://gitlab.example.com/group/sub/proj.git", "gitlab.example.com", "group/sub/proj"),
        ("ssh://git@codeberg.org:2222/me/repo.git", "codeberg.org", "me/repo"),
        ("https://user@git.acme.io/team/app/", "git.acme.io", "team/app"),
    ],
)
def test_parse_remote(url, host, repo):
    remote = parse_remote(url)
    assert (remote.host, remote.repo) == (host, repo)


def test_guess_kind():
    assert guess_kind("github.com") == "github"
    assert guess_kind("gitlab.acme.io") == "gitlab"
    assert guess_kind("forgejo.acme.io") == "gitea"
    assert guess_kind("codeberg.org") == "gitea"
    assert guess_kind("git.acme.io") is None


def test_unknown_provider_errors():
    with pytest.raises(ProviderError):
        make_provider("bitbucket", "a/b", "bitbucket.org")


def test_cli_accepts_bare_parent_and_explicit_command():
    from ticketmaster.cli import parse_args

    assert (parse_args(["680"]).command, parse_args(["680"]).parent) == ("open", 680)
    args = parse_args(["adopt", "#12", "--label", "x"])
    assert (args.command, args.parent, args.label) == ("adopt", 12, "x")
    assert parse_args(["snapshot", "https://github.com/o/r/issues/9"]).parent == 9
    assert parse_args([]).command == "open"
