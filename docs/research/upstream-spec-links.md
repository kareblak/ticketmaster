# How upstream wayfinder links specs and implementation tickets to a map

Research for [#57](https://github.com/kareblak/ticketmaster/issues/57), part of the map
[Read-only ticketmaster](https://github.com/kareblak/ticketmaster/issues/56).

## Short answer

- **Spec → map.** Upstream writes **no link at all**, on any store. `/to-spec` publishes a standalone
  issue (or `spec.md`) whose template has no parent or map field. The docs say to run
  `/to-spec #<map_issue>` once a map clears, but nothing in the resulting spec points back at the map.
  ticketmaster's `Part of #<map>` first line on a spec is a ticketmaster invention.
- **Implementation ticket → spec.**
  - GitHub: a native **sub-issue** of the spec, plus a `## Parent` body section. Upstream main makes the
    sub-issue explicit and gives the commands; 1.2.3 only says "native … sub-issue relationship where it has one".
    Without sub-issues, the fallback is `Part of #<parent>` at the top of the body.
  - GitLab: only the `## Parent` body section. The GitLab tracker doc defines no sub-issue operation.
  - Local markdown: the ticket and the spec share a directory, `.scratch/<feature-slug>/`. The ticket
    file has no parent field.
  - In every case `## Parent` is written only "if the source was an existing issue".
- **Out of scope.** For a wayfinder ticket, upstream **closes it** and adds one line (the gist, why, and
  a link) to the map's `## Out of scope` section. There is **no label**. Upstream main adds that
  `wayfinder:` labels are "the only labels a map and its tickets carry". Separately, `/triage` rejects
  enhancements with the `wontfix` label and a `.out-of-scope/<concept>.md` file.
- **Gitea/Forgejo.** Upstream does **not define it**. There is no tracker template for it, and setup offers
  GitHub, GitLab, Local markdown or "Other". The docs mention Gitea only as a community "Other" setup,
  and as an example of a tracker without native dependency links.

## Sources

- Installed plugin `mattpocock-skills` 1.2.3:
  `~/.claude/plugins/cache/claude-plugins-official/mattpocock-skills/1.2.3/` (cited as `1.2.3:<path>`).
- Upstream `github.com/mattpocock/skills` at `main` = `49dd158` (2026-10-09), cited as `main:<path>`.
  It is newer than 1.2.3: CHANGELOG is at 1.3.1, and unreleased changesets touch these files.
- The user-local variants `~/.agents/skills/{to-spec,to-tickets,wayfinder}/SKILL.md` are
  **byte-identical** to 1.2.3 (`diff -q`, no output). So they hold no local differences, and they carry the
  same staleness against `main`.
- ticketmaster: `ticketmaster/wayfinding.py` and `README.md` "Wayfinding rules", at `41787f4`.

## Per store

### GitHub

| Link | Upstream 1.2.3 | Upstream main (`49dd158`) | ticketmaster |
| --- | --- | --- | --- |
| Spec → map | None. The spec template has Problem Statement … Further Notes and no parent field (`1.2.3:skills/engineering/to-spec/SKILL.md:21-75`). It is published as one issue with the `ready-for-agent` label (`:19`) | Unchanged (`main:skills/engineering/to-spec/SKILL.md`, no diff) | `Part of #<map>` as the first line, and no `wayfinder:` label (`wayfinding.py:32`) |
| Impl → spec | `## Parent` section, "if the source was an existing issue, otherwise omit" (`1.2.3:skills/engineering/to-tickets/SKILL.md:86-88`). "Use the platform's native blocking / sub-issue relationship where it has one" (`:63`) | "If the source was an existing issue, make each ticket its sub-issue (tracker doc's operation)" (`main:…/to-tickets/SKILL.md:63`). The tracker doc gains **Make an issue a sub-issue of a parent**: `gh issue create --parent <parent>` or `gh issue edit <parent> --add-sub-issue <child>` (gh 2.94+), else `gh api … /sub_issues`. "Without sub-issues, put `Part of #<parent>` at the top of the child body" (`main:…/issue-tracker-github.md:10`). `## Parent` stays in the template | Sub-issue via `gh api …/sub_issues` **and** `## Parent` naming `#<spec>`, always (`wayfinding.py:35-41`). The board accepts either link alone (README:209-210) |
| Child → map | Sub-issue. Without sub-issues: a task list in the map, plus `Part of #<map>` at the top of the child (`1.2.3:…/issue-tracker-github.md:41`) | Same, pointing at the new sub-issue operation (`main:…/issue-tracker-github.md:42`) | Sub-issue, "nothing in the body". No fallback (`wayfinding.py:40`) |
| Blocking | Native `dependencies/blocked_by`, fallback `Blocked by: #<n>` line (`1.2.3:…/issue-tracker-github.md:42`) | Same. `## Blocked by` is "omitted when blockers were set as native edges" (`main:…/to-tickets/SKILL.md:101`) | Native only, "never a `Blocked by:` body line" (`wayfinding.py:25-26,42`) |
| Out of scope | Close it, and add one line to the map's `## Out of scope` (`1.2.3:skills/engineering/wayfinder/SKILL.md:101`). No label, no close reason | Same, plus "`wayfinder:` labels are the only labels a map and its tickets carry, never a triage label like `ready-for-agent`" (`main:…/wayfinder/SKILL.md:65`) | `out-of-scope` label, comment, and close as **not planned** (`wayfinding.py:44`) |

Known upstream rough edges: the to-tickets docs say tickets were often **not** created as sub-issues
of the spec on GitHub ([mattpocock/skills#554](https://github.com/mattpocock/skills/issues/554)). They
also say "Blocked by" ended up as body text instead of a native link (#513) (`1.2.3:docs/engineering/to-tickets.md:64-68`).
Main's changeset `.changeset/to-tickets-sub-issues.md` is the fix for #554.

### GitLab

| Link | Upstream (1.2.3 = main, apart from `-F json` → `-O json`) | ticketmaster |
| --- | --- | --- |
| Spec → map | None (same to-spec template) | `Part of #<map>` as the first line (`wayfinding.py:32`) |
| Impl → spec | `## Parent` body section (the to-tickets template). The GitLab tracker doc has **no** sub-issue operation (`1.2.3:…/issue-tracker-gitlab.md`), so main's "make each ticket its sub-issue (tracker doc's operation)" has nothing to apply | `## Parent` naming `#<spec>`, and explicitly no `Part of` line (`wayfinding.py:53`). Matches |
| Child → map | `Part of #<map>` at the top of the description. On tiers with native epics, the map may be an epic instead (`…/issue-tracker-gitlab.md:41-42`) | `Part of #<map>` as the first line, with no epic option (`wayfinding.py:52`). Matches |
| Blocking | The `/blocked_by #<n>` quick action, posted as a note (Premium/Ultimate). On the free tier, a `Blocked by:` line (`:43`) | REST `…/links` with `link_type=blocks`, native only, no free-tier fallback (`wayfinding.py:54`) |
| Out of scope | Close it, plus a line in the map's Out of scope section. `glab issue close` takes no comment, so post a note first (`:12`) | `out-of-scope` label, then a note, then close (`wayfinding.py:55`) |

### Local markdown

Upstream only (`1.2.3:…/issue-tracker-local.md`, unchanged on main). ticketmaster defines **no**
local-markdown rules: `TRACKER_NAMES`/`SECTIONS` cover only github, gitlab and gitea (`wayfinding.py:18,72`).

| Link | Upstream |
| --- | --- |
| Spec | `.scratch/<feature-slug>/spec.md` (`:8`) |
| Impl → spec | Directory co-location: `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01` in dependency order (`:9`; `to-tickets/SKILL.md:62`). The local ticket template has **no** parent field, only `**Blocked by:**` and `**Status:** ready-for-agent` (`to-tickets/SKILL.md:69-82`) |
| Spec → map | None. The map is `.scratch/<effort>/map.md`, with children at `.scratch/<effort>/issues/NN-<slug>.md` (`:25-26`) |
| Child → map | Directory co-location, plus a `Type:` line and a `Status: claimed`/`resolved` line (`:26`) |
| Blocking | `Blocked by: NN, NN`. Unblocked when every listed file is `resolved` (`:27`) |
| Out of scope | Wayfinder says "close it", but the local store has no closed state other than `Status:`. Unspecified |

Gaps to note for a reader of the local store:

- If a spec's `<feature-slug>` equals the map's `<effort>`, then `spec.md`, `map.md`, the decision tickets
  and the implementation tickets all share one `issues/` directory and one `NN` sequence. Upstream does
  not say whether that is intended.
- The two ticket kinds use different status vocabularies. Implementation tickets use the triage role,
  `ready-for-agent`, written in bold. Decision tickets use `claimed`/`resolved`, written as a plain line.

### Gitea / Forgejo

Upstream defines no Gitea/Forgejo store:

- Setup offers GitHub, GitLab, Local markdown or Other (`1.2.3:…/setup-matt-pocock-skills/SKILL.md:44-47`).
  Templates exist only for github, gitlab and local. The main tree has no Gitea file.
- `1.2.3:docs/engineering/setup-matt-pocock-skills.md:49`: "Other" is how the community runs "a Gitea CLI
  shaped like `gh`". The user's prose is recorded in `docs/agents/issue-tracker.md`.
- `1.2.3:docs/engineering/wayfinder.md:27`: "a tracker without native dependency links (a self-hosted
  Gitea, say) degrades wayfinder to inferring blockers from the map text".

ticketmaster's Gitea section is entirely its own. It encodes child → map as `Part of #<map>` and the
spec's tickets as `## Parent`. It encodes blocking as **native** `/issues/<n>/dependencies` and out of
scope as `out-of-scope` + close (`wayfinding.py:61-70`). Its native-dependency rule contradicts
upstream's docs, which call Gitea a tracker without native dependency links.

## Differences from ticketmaster, summarised

1. **Spec → map `Part of #<map>`**: ticketmaster-only on every store. Upstream's spec carries no link to
   the map. A board that "reads the store the way upstream defines it" (map #56's destination) cannot
   find a spec from its map. It would need a convention upstream lacks, or a heuristic such as a mention
   of `#<map>` in the spec body (`/to-spec #<map>` may or may not leave one).
2. **Impl → spec**: largely aligned. On GitHub, ticketmaster's "sub-issue and `## Parent`" matches the
   upstream template plus main's sub-issue rule. Upstream's GitHub fallback without sub-issues is
   `Part of #<parent>`, which ticketmaster's board would read as a spec → map link. GitLab matches
   (`## Parent`). Local markdown is not covered by ticketmaster.
3. **Out of scope**: upstream marks it as closed plus a line in the map's `## Out of scope`, with no
   label. The `out-of-scope` label and GitHub's "not planned" close reason are ticketmaster-only.
   Upstream main's "only `wayfinder:` labels" sentence arguably forbids the label. The board currently
   hides "not planned" closes unless they carry that label (README:173-175).
4. **Blocking**: upstream allows a `Blocked by:` body-line fallback (GitHub without dependencies, GitLab
   Free, local). ticketmaster forbids it. A read-only reader of upstream stores must parse it.
5. **Gitea/Forgejo**: not an upstream store. Supporting it is a ticketmaster extension; upstream's
   only path is "Other" with free prose.
6. **English keywords rule** (`wayfinding.py:29-30`): ticketmaster-only.
7. **Staleness**: the installed 1.2.3 and the `~/.agents/skills` copies predate main's changes to
   to-tickets (sub-issue of source, no `## Blocked by` with native edges) and to wayfinder (no triage
   labels, no PRs from research branches, resolve by label type). A repo's
   `docs/agents/issue-tracker.md` is a snapshot from whichever setup version wrote it.
