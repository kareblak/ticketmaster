# Ticketmaster

A live kanban board of a repo's issues, laid out in lanes, from which a card starts an agent session in its own
worktree.

## Language

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
A spec whose body links a wayfinder map with `Part of #<map>`; it belongs to that map's lane.

**Standalone spec**:
An issue with the spec signature, no map link and no `wayfinder:` label.
_Avoid_: orphan spec, PRD

**Loose ticket**:
An issue with the ticket signature whose parent is neither a map spec nor a standalone spec, including one with no
parent at all.
_Avoid_: orphan ticket, unparented ticket

### Starting work

**Board work**:
What `s` starts a session on: wayfinder work (a map, a ticket on one, a map spec and its tickets) or standalone
work (a standalone spec and the tickets in its spec lane). Anything else is refused.
