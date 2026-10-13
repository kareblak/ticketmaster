"""`ticketmaster install-skill`: put the /ticketmaster skill into Claude Code's skills directory."""

from __future__ import annotations

import os
import shutil
from importlib import resources
from pathlib import Path


def skills_dir() -> Path:
    return Path(os.environ.get("CLAUDE_SKILLS_DIR", Path.home() / ".claude" / "skills"))


def install_skill(target_root: Path | None = None) -> Path:
    """Writes SKILL.md to <skills>/ticketmaster/, replacing an older install (dir or symlink)."""
    target = (target_root or skills_dir()) / "ticketmaster"
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    source = resources.files("ticketmaster").joinpath("skill/SKILL.md")
    (target / "SKILL.md").write_text(source.read_text())
    return target / "SKILL.md"
