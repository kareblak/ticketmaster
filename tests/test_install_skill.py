from ticketmaster.install_skill import install_skill


def test_installs_skill_and_replaces_old_symlink(tmp_path):
    old_target = tmp_path / "old"
    old_target.mkdir()
    (tmp_path / "ticketmaster").symlink_to(old_target)
    path = install_skill(tmp_path)
    assert path == tmp_path / "ticketmaster" / "SKILL.md"
    assert not (tmp_path / "ticketmaster").is_symlink()
    text = path.read_text()
    assert text.startswith("---\nname: ticketmaster") and "ticketmaster open $ARGUMENTS" in text
    assert old_target.exists()  # the old symlink target is left alone


def test_reinstall_overwrites(tmp_path):
    install_skill(tmp_path)
    (tmp_path / "ticketmaster" / "stale.txt").write_text("x")
    install_skill(tmp_path)
    assert sorted(p.name for p in (tmp_path / "ticketmaster").iterdir()) == ["SKILL.md"]
