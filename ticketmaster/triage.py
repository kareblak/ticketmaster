"""The triage labels that hold a ticket back from an AFK start, named as the repo's `docs/agents/triage-labels.md` says.

That doc (written by `/setup-matt-pocock-skills`) maps each canonical triage role to the label the repo's tracker
uses. A ticket waiting on information or reserved for a human is not for an agent to start.
"""

from __future__ import annotations

import re
from pathlib import Path

TRIAGE_DOC = Path("docs/agents/triage-labels.md")
# Canonical role → why a ticket carrying its label is not started.
DEFAULT_HOLDS = {
    "needs-info": "waits on more information",
    "ready-for-human": "needs a human to implement it",
}
# `| \`needs-info\` | \`waiting\` | …`: the canonical role, then the repo's label.
_ROW = re.compile(r"^\|\s*`(?P<role>[^`]+)`\s*\|\s*`(?P<label>[^`]+)`\s*\|", re.MULTILINE)


def hold_labels(root: Path | None) -> dict[str, str]:
    """The repo's label for each holding role → why it holds; canonical names where the doc is missing or silent."""
    path = None if root is None else root / TRIAGE_DOC
    try:
        text = path.read_text(encoding="utf-8") if path is not None and path.is_file() else ""
    except OSError:
        text = ""
    names = {m.group("role").strip(): m.group("label").strip() for m in _ROW.finditer(text)}
    return {names.get(role, role): reason for role, reason in DEFAULT_HOLDS.items()}
