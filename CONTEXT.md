# Ticketmaster

A live kanban board of a repo's issues, laid out in lanes, from which a card starts an agent session in its own
worktree.

## Language

### Board

**Board view**:
How the board is shown: the TUI in a terminal, or the web view in a browser on the same machine. Independent of
the session backend: either view starts and finds sessions through whichever backend is in use. Each view keeps one
board per Claude session, so a TUI and a web board can run side by side.
_Avoid_: web client, front end

### Lanes

**Lane**:
One row of the board: a header issue (or none, for the Loose lane) and its tickets across Blocked, Frontier,
Claimed and Decided.

**Map lane**:
The lane of a `wayfinder:map` issue, holding its decision tickets, its map spec and that spec's tickets.

**Spec lane**:
The lane of a standalone spec, holding the spec as its header and the spec's tickets.

**Loose lane**:
The single shared lane, with no issue behind it, for loose tickets.

### Issues

**Signature**:
The pair of `##` headings that marks an issue as a skill's output: `Problem Statement` + `Solution` for a spec,
`What to build` + `Acceptance criteria` for a ticket. Both headings of the pair must be present.
_Avoid_: marker, template

**Map spec**:
The spec a wayfinder map leads to; it belongs to that map's lane. On GitHub and Linear it is linked as a native
sub-issue of the map, carrying the spec signature and no `wayfinder:` label; its body's first line
`Part of #<map>` is the fallback link, and the only one on GitLab and Gitea/Forgejo.

**Standalone spec**:
An issue with the spec signature, no map link (neither `Part of #<map>` nor a map as its sub-issue parent) and
no `wayfinder:` label.
_Avoid_: orphan spec, PRD

**Loose ticket**:
An issue with the ticket signature whose parent is neither a map spec nor a standalone spec, including one with no
parent at all.
_Avoid_: orphan ticket, unparented ticket

### Starting work

**Board work**:
What `s` starts a session on: wayfinder work (a map, a ticket on one, a map spec and its tickets) or standalone
work (a standalone spec and the tickets in its spec lane), or a loose ticket. Anything else is refused.

### Sessions

**Session backend**:
What opens the board, starts, finds and goes to sessions, and releases them for cleanup: herdr panes and
workspaces, or plain terminal windows. Auto-detected (herdr inside herdr, else plain), overridable per run, shell or
repo.
_Avoid_: workbench, multiplexer

**Session**:
A Claude session whose working directory is a card's worktree, whichever backend or hand opened it.

**Session status**:
What the board knows of a session: working, needs you, idle, done (herdr only), or unknown. A worktree without a
session has no status (no agent).
_Avoid_: agent state

**Unknown**:
A live session whose status the session source reports but doesn't name. The session exists; only its state is
unclear.

**Status unavailable**:
The session source itself can't be read and has never answered, so the board can't tell whether a worktree has a
session at all. Not the same as unknown.
