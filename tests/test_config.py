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


@pytest.mark.parametrize("value, number", [
    ("#42", 42), ("42", 42), ("TM-1", "TM-1"), ("tm-1", "TM-1"), ("https://github.com/o/r/issues/42", 42),
    ("https://gitlab.com/g/p/-/issues/42", 42), ("https://linear.app/team/issue/TM-1/pick-a-stack", "TM-1"),
    ("https://gitlab.com/g/p/-/work_items/42", 42),
])
def test_cli_parses_each_map_form_as_the_map_argument(value, number):
    from ticketmaster.cli import parse_args

    assert (parse_args([value]).command, parse_args([value]).map) == ("open", number)
    assert parse_args(["open", value, "--columns", "one"]).map == number


@pytest.mark.parametrize("value", ["TM-", "pick-a-stack", "https://github.com/o/r", "#x"])
def test_cli_refuses_a_map_that_names_no_issue(value, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    from ticketmaster.cli import parse_args

    with pytest.raises(SystemExit):
        parse_args(["open", value])


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


def test_version_is_0_3_2(capsys):
    from ticketmaster.cli import main

    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "ticketmaster 0.3.2"


def linear_repo(tmp_path, toml):
    (tmp_path / ".git").mkdir()  # a repo without a remote: Linear must not need one
    (tmp_path / ".ticketmaster.toml").write_text(toml)
    return tmp_path


def test_linear_is_configured_by_provider_and_team_without_a_git_remote(tmp_path, monkeypatch):
    from ticketmaster.config import resolve_provider

    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_x")
    provider = resolve_provider(linear_repo(tmp_path, 'provider = "linear"\nteam = "TM"\n'))
    assert (provider.name, provider.team, provider.repo) == ("linear", "TM", "TM")


def test_linear_reads_the_key_from_the_configured_linear_variable(tmp_path, monkeypatch):
    from ticketmaster.config import resolve_provider

    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setenv("LINEAR_API_KEY_ACME", "lin_api_acme")
    repo = linear_repo(tmp_path, 'provider = "linear"\nteam = "TM"\nkey_env = "LINEAR_API_KEY_ACME"\n')
    assert resolve_provider(repo).transport.api_key == "lin_api_acme"


def test_a_key_env_without_the_linear_prefix_is_refused(tmp_path, monkeypatch):
    from ticketmaster.config import resolve_provider

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    repo = linear_repo(tmp_path, 'provider = "linear"\nteam = "TM"\nkey_env = "GITHUB_TOKEN"\n')
    with pytest.raises(ProviderError, match="key_env") as error:
        resolve_provider(repo)
    assert "ghp_secret" not in str(error.value)


def test_provider_linear_without_a_team_prints_the_config_lines_to_add(tmp_path, monkeypatch, capsys):
    from ticketmaster import cli, herdr

    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.chdir(linear_repo(tmp_path, ""))
    assert cli.main(["--provider", "linear"]) == 2
    err = capsys.readouterr().err
    assert 'provider = "linear"' in err and 'team = "<key>"' in err and ".ticketmaster.toml" in err


def test_the_default_interval_is_30_seconds_on_linear_and_5_elsewhere():
    from ticketmaster.cli import parse_args, passthrough
    from ticketmaster.config import default_interval

    assert (default_interval("linear"), default_interval("github"), default_interval("gitea")) == (30, 5, 5)
    assert parse_args(["--provider", "linear"]).interval is None
    assert "--interval" not in passthrough(parse_args([]))  # the board pane picks the provider's default too
    assert passthrough(parse_args(["--interval", "12"]))[-2:] == ["--interval", "12"]


def test_a_key_of_the_linear_team_asks_linear_for_that_map_also_when_closed():
    from types import SimpleNamespace

    from ticketmaster.cli import named_number, parse_args

    linear, github = SimpleNamespace(name="linear", team="TM"), SimpleNamespace(name="github", repo="o/r")
    assert named_number(parse_args(["TM-1"]).map, linear) == 1
    assert named_number(parse_args(["https://linear.app/w/issue/TM-14/slug"]).map, linear) == 14
    assert named_number(parse_args(["ENG-1"]).map, linear) is None  # another team's key: the board filter alone
    assert named_number(parse_args(["TM-1"]).map, github) is None
    assert named_number(parse_args(["#7"]).map, github) == 7 and named_number(None, linear) is None


def test_the_interval_is_the_flag_then_the_repos_saved_one_then_the_providers_default():
    from ticketmaster.config import board_interval

    assert board_interval(12.0, {"interval": 20}, "github") == 12
    assert board_interval(None, {"interval": 20}, "github") == 20
    assert board_interval(None, {"interval": 0.5}, "linear") == 0.5  # no floor
    assert board_interval(None, {}, "github") == 5 and board_interval(None, {}, "linear") == 30
    for unusable in (0, -3, "abc", True, None, float("nan")):  # a hand-edited settings.json
        assert board_interval(None, {"interval": unusable}, "linear") == 30


@pytest.mark.parametrize("text", ["abc", "", "0", "-1", "nan", "inf"])
def test_an_interval_must_be_a_positive_number(text):
    from ticketmaster.config import parse_interval

    with pytest.raises(ValueError):
        parse_interval(text)


def test_any_positive_interval_is_accepted():
    from ticketmaster.config import parse_interval

    assert (parse_interval("0.25"), parse_interval(" 10 "), parse_interval("120")) == (0.25, 10, 120)


@pytest.mark.parametrize("text", ["abc", "", "2.5", "1e3", "0", "-1"])
def test_a_fetch_limit_must_be_a_positive_whole_number(text):
    from ticketmaster.config import parse_fetch_limit

    with pytest.raises(ValueError):
        parse_fetch_limit(text)


def test_the_fetch_limit_is_the_repos_saved_one_else_the_default():
    from ticketmaster.config import board_fetch_limit, parse_fetch_limit

    assert (parse_fetch_limit(" 50 "), parse_fetch_limit("1"), parse_fetch_limit("5000")) == (50, 1, 5000)
    assert board_fetch_limit({"fetch_limit": 50}) == 50 and board_fetch_limit({}) == 300
    for unusable in (0, -3, 2.5, "50", True, None):  # a hand-edited settings.json
        assert board_fetch_limit({"fetch_limit": unusable}) == 300
