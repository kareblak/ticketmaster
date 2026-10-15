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


def test_cli_accepts_bare_map_and_explicit_command():
    from ticketmaster.cli import parse_args

    assert (parse_args(["680"]).command, parse_args(["680"]).map) == ("open", 680)
    assert (parse_args(["open", "#12"]).command, parse_args(["open", "#12"]).map) == ("open", 12)
    assert parse_args(["https://github.com/o/r/issues/9"]).map == 9
    assert parse_args(["--repo", "o/r", "7"]).map == 7
    assert parse_args([]).command == "open"


@pytest.mark.parametrize("command", ["snapshot", "adopt", "setup", "install-skill", "upgrade"])
def test_removed_commands_get_argparses_invalid_choice_error(command, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    from ticketmaster.cli import parse_args

    with pytest.raises(SystemExit) as exit:
        parse_args([command])
    assert exit.value.code == 2
    assert f"invalid choice: '{command}'" in capsys.readouterr().err


@pytest.mark.parametrize("flag", [["--label", "x"], ["--done-days", "3"], ["--limit", "10"]])
def test_removed_flags_are_unknown_arguments(flag, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    from ticketmaster.cli import parse_args

    with pytest.raises(SystemExit) as exit:
        parse_args(flag)
    assert exit.value.code == 2
    assert f"unrecognized arguments: {flag[0]}" in capsys.readouterr().err


def test_version_is_0_3_0(capsys):
    from ticketmaster.cli import main

    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "ticketmaster 0.3.0"
