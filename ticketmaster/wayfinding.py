"""`ticketmaster setup`: the wayfinding linking rules, kept in the repo's `docs/agents/issue-tracker.md`.

Wayfinder links tickets the way that doc's "Wayfinding operations" section says, and the board reads the
same encodings, so ticketmaster owns the section: `setup` writes it (or replaces it in place) per tracker,
and `drift` tells the board when the repo's copy is missing or differs from what this version writes.
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import OUT_OF_SCOPE_LABEL

TRACKER_DOC = Path("docs/agents/issue-tracker.md")
HEADING = "## Wayfinding operations"
SETUP_COMMAND = "ticketmaster setup"
TRACKER_NAMES = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea/Forgejo"}

PREAMBLE = f"""{HEADING}

<!-- Written by `{SETUP_COMMAND}`, which replaces this section; the ticketmaster board reads it. -->

Used by `/wayfinder`; the ticketmaster board reads the same encodings. The **map** is a single issue with
**child** issues as tickets. Link tickets only as the bullets below say: blocking is always the tracker's
native link, never a `Blocked by:` body line. %s
"""

ENGLISH = f"""The keywords `Part of` and `## Parent` and the `{OUT_OF_SCOPE_LABEL}` label are
always English, even when the rest of the issue is written in another language."""

SPEC = """- **Spec → map**: once the map is resolved, its destination becomes a spec (`/to-spec`): an issue with no `wayfinder:` label whose first line is `Part of #<map>`. The board shows it, and its implementation tickets, beside the map's decision tickets, and starts the spec and its implementation tickets with `/wayfinder <map> <ticket>`; they still land through a PR.
"""

PARENT = ("its `## Parent` section names `#<spec>`, and it has no `wayfinder:` label, so the board starts it like the spec, "
          "with wayfinder on the map")

GITHUB = PREAMBLE % ENGLISH + f"""
- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child → map**: a GitHub **sub-issue** of the map, nothing in the body: `gh api --method POST repos/<owner>/<repo>/issues/<map>/sub_issues -F sub_issue_id=<child-db-id>`. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`).
""" + SPEC + f"""- **Implementation ticket → spec**: each ticket from `/to-tickets` is a GitHub **sub-issue** of the spec, `gh api --method POST repos/<owner>/<repo>/issues/<spec>/sub_issues -F sub_issue_id=<ticket-db-id>`; {PARENT}.
- **Blocking**: GitHub's **native issue dependencies**: `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`. A ticket is unblocked when every blocker is closed.
- **Database ids**: the `<…-db-id>` values above are numeric database ids (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`), _not_ the `#number` or `node_id`.
- **Out of scope**: label it `{OUT_OF_SCOPE_LABEL}`, say why, and close it as not planned: `gh issue edit <n> --add-label {OUT_OF_SCOPE_LABEL}`, then `gh issue close <n> --reason "not planned" --comment "<why>"`.
- **Frontier query**: the map's open sub-issues, in order (`gh api repos/<owner>/<repo>/issues/<map>/sub_issues`), minus any with an open blocker (`issue_dependencies_summary.blocked_by > 0`) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me`, the session's first write.
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.
"""

GITLAB = PREAMBLE % ENGLISH + f"""
- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `glab issue create --label wayfinder:map`.
- **Child → map**: the first line of the child's description is `Part of #<map>`. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`).
""" + SPEC + f"""- **Implementation ticket → spec**: each ticket from `/to-tickets` has no `Part of` line; {PARENT}.
- **Blocking**: GitLab's **native `blocks` issue link** (GitLab Premium or Ultimate), from the blocker to the child: `glab api --method POST projects/:id/issues/<blocker>/links -f target_project_id=:id -f target_issue_iid=<child> -f link_type=blocks`. A ticket is unblocked when every blocker is closed.
- **Out of scope**: label it `{OUT_OF_SCOPE_LABEL}`, say why, and close it: `glab issue update <n> --label {OUT_OF_SCOPE_LABEL}`, `glab issue note <n> -m "<why>"`, then `glab issue close <n>`.
- **Frontier query**: open issues labelled `wayfinder:<type>` whose description starts with `Part of #<map>`, in issue order, minus any with an open blocker (`blockedByCount > 0`) or an assignee; first wins.
- **Claim**: `glab issue update <n> --assignee <your-username>`, the session's first write.
- **Resolve**: `glab issue note <n> -m "<answer>"`, then `glab issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.
"""

GITEA = PREAMBLE % ENGLISH + f"""
- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `tea issues create --labels wayfinder:map`.
- **Child → map**: the first line of the child's body is `Part of #<map>`. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`).
""" + SPEC + f"""- **Implementation ticket → spec**: each ticket from `/to-tickets` has no `Part of` line; {PARENT}.
- **Blocking**: Gitea/Forgejo's **native issue dependencies**: `POST /api/v1/repos/<owner>/<repo>/issues/<child>/dependencies` with body `{{"owner": "<owner>", "repo": "<repo>", "index": <blocker>}}` (token from the `tea` login, header `Authorization: token <token>`). A ticket is unblocked when every blocker is closed.
- **Out of scope**: label it `{OUT_OF_SCOPE_LABEL}`, say why, and close it: `tea issues edit <n> --add-labels {OUT_OF_SCOPE_LABEL}`, `tea comment <n> "<why>"`, then `tea issues close <n>`.
- **Frontier query**: open issues labelled `wayfinder:<type>` whose body starts with `Part of #<map>`, in issue order, minus any with an open dependency (`GET …/issues/<n>/dependencies`) or an assignee; first wins.
- **Claim**: `tea issues edit <n> --add-assignees <your-username>`, the session's first write.
- **Resolve**: `tea comment <n> "<answer>"`, then `tea issues close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.
"""

SECTIONS = {"github": GITHUB, "gitlab": GITLAB, "gitea": GITEA}

# The section runs from its heading to the next heading of the same or a higher level, or the end of the file.
_SECTION = re.compile(rf"^{re.escape(HEADING)}[ \t]*(?:\n|\Z).*?(?=^#{{1,2}}\s|\Z)", re.MULTILINE | re.DOTALL)


def section(kind: str) -> str:
    """The section this version writes for a tracker kind (`github`, `gitlab`, `gitea`/`forgejo`)."""
    return SECTIONS[normalize(kind)]


def normalize(kind: str) -> str:
    return "gitea" if kind == "forgejo" else kind


def setup(root: Path, kind: str) -> Path:
    """Writes the section into the repo's tracker doc: replaced in place, appended, or in a new doc."""
    path = root / TRACKER_DOC
    text = path.read_text(encoding="utf-8") if path.is_file() else f"# Issue tracker: {TRACKER_NAMES[normalize(kind)]}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(with_section(text, section(kind)), encoding="utf-8")
    return path


def with_section(text: str, new: str) -> str:
    match = _SECTION.search(text)
    if match is None:
        return text.rstrip("\n") + "\n\n" + new
    after = text[match.end():]
    return text[: match.start()] + new + ("\n" + after if after else "")


def drift(root: Path, kind: str) -> str | None:
    """`missing` or `outdated` when the repo's section is not what this version writes; None when current or unknown."""
    if normalize(kind) not in SECTIONS:
        return None
    path = root / TRACKER_DOC
    match = _SECTION.search(path.read_text(encoding="utf-8")) if path.is_file() else None
    if match is None:
        return "missing"
    return None if match.group(0).rstrip("\n") == section(kind).rstrip("\n") else "outdated"
