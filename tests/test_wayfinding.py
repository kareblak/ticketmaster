import pytest

from ticketmaster.wayfinding import TRACKER_DOC, drift, setup

HEAD = "# Issue tracker: GitHub\n\nIssues live as GitHub issues.\n\n## Conventions\n\n- Create with `gh issue create`.\n"


def write_doc(root, text):
    path = root / TRACKER_DOC
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_setup_appends_the_section_and_keeps_the_rest(tmp_path):
    path = write_doc(tmp_path, HEAD)
    setup(tmp_path, "github")
    text = path.read_text()
    assert text.startswith(HEAD)
    assert "\n## Wayfinding operations\n" in text
    assert "sub-issue" in text and "dependencies/blocked_by" in text and "out-of-scope" in text


def test_setup_replaces_an_existing_section_in_place(tmp_path):
    tail = "## When a skill says \"fetch the relevant ticket\"\n\nRun `gh issue view <n>`.\n"
    old = "## Wayfinding operations\n\n- **Blocking**: a `Blokkert av` line, improvised.\n\n"
    path = write_doc(tmp_path, HEAD + "\n" + old + tail)
    setup(tmp_path, "github")
    text = path.read_text()
    assert "Blokkert av" not in text
    assert text.startswith(HEAD + "\n## Wayfinding operations\n")
    assert text.endswith("\n\n" + tail)
    assert text.count("## Wayfinding operations") == 1
    setup(tmp_path, "github")
    assert path.read_text() == text  # running it again changes nothing


def test_setup_creates_the_doc_when_there_is_none(tmp_path):
    path = setup(tmp_path, "forgejo")
    text = path.read_text()
    assert path == tmp_path / TRACKER_DOC
    assert text.startswith("# Issue tracker: Gitea/Forgejo\n\n## Wayfinding operations\n")


@pytest.mark.parametrize(
    "kind,child,blocking",
    [
        ("github", "sub-issue", "dependencies/blocked_by"),
        ("gitlab", "`Part of #<map>`", "link_type=blocks"),
        ("gitea", "`Part of #<map>`", "/issues/<child>/dependencies"),
    ],
)
def test_each_tracker_pins_its_encodings_with_english_keywords(tmp_path, kind, child, blocking):
    text = setup(tmp_path, kind).read_text()
    assert child in text and blocking in text
    assert "--add-label out-of-scope" in text or "--label out-of-scope" in text or "--add-labels out-of-scope" in text
    assert "`Part of` and `Blocked by:`" in text and "always English" in text


def test_drift_names_a_missing_or_outdated_section(tmp_path):
    assert drift(tmp_path, "github") == "missing"  # no doc at all
    path = write_doc(tmp_path, HEAD)
    assert drift(tmp_path, "github") == "missing"
    setup(tmp_path, "github")
    assert drift(tmp_path, "github") is None
    path.write_text(path.read_text().replace("sub-issue", "task list"))
    assert drift(tmp_path, "github") == "outdated"
    assert drift(tmp_path, "gitlab") == "outdated"  # written for another tracker
    assert drift(tmp_path, "fake") is None  # a tracker setup does not know is not checked


def test_cli_setup_writes_the_section_for_the_repos_tracker(tmp_path, monkeypatch, capsys):
    from ticketmaster.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["setup", "--provider", "gitlab", "--repo", "group/project"]) == 0
    assert drift(tmp_path, "gitlab") is None
    assert "GitLab" in capsys.readouterr().out
