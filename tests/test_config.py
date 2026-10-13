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
