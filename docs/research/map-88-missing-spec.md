# Map #88: the spec could not be broken into tickets from the board

Report (user): "In map #88, the spec was not shown in ticketmaster, which did not allow me to 's' it, and create
tickets. I had to do that outside of ticketmaster, and find the spec in gh issues."

Follow-ups (user):

- The spec **did** show on the board once the tickets had been created outside ticketmaster.
- While #99 had no tickets, the board's status line showed a **recent** `updated` time with **no** rate-limit
  warning. The board was fetching normally.
- Decision: split #106 so that `s` → `/to-tickets` on map specs ships before #104. That split is now #109.
- Decision: merge the map spec into the lane's map card instead of showing it as a look-alike card in Frontier.
  #109 now covers both: "Merge the map spec into the map card; s on it starts /to-tickets".

Investigated 2026-10-15 against `main` at `bf94a21`, the running board (pid 70859, herdr pane `w18:pK`) and live
GitHub data.

## Summary

1. **`s` on a spec card is refused by design (confirmed).** This is why the user had to leave the board.
   `Ticket.start_refusal` refuses every `spec` card with "is a spec: s starts /implement on its implementation
   tickets" (`ticketmaster/model.py:83-84`). The README documents it (`README.md:89`, `README.md:112`), and no code
   path starts `/to-tickets`. This is fixed by #109 (split from #106).
2. **"Spec not shown until tickets existed": no cause found in the code or in the GitHub data.**
   - I replayed the exact zero-ticket state of map #88 through the real `BoardApp`, using the installed 0.3.2
     venv, on a GitHub fetch with every later issue removed. The spec card #99 renders in Frontier at all three
     terminal sizes I tried.
   - GitHub's timeline shows that everything the board needs to find #99 existed from 09:27:58Z, about 4.5 minutes
     before the tickets.
   - Nothing in the board's discovery or rendering depends on a spec having tickets.
   - The root cause of the "not shown" symptom is therefore **unconfirmed**. Ruled-out causes and remaining
     hypotheses are listed below.

## 1. How the board finds a map's spec on GitHub

| Step | Location | What it does |
|---|---|---|
| Maps | `providers/github.py:93-94` | Searches `is:open label:"wayfinder:map"`. |
| Closed maps with an open spec | `providers/github.py:95-102` | Searches `in:body "Part of"`, and `closed_map_refs` adds closed maps (only for closed maps). |
| Map family | `providers/github.py:104-105`, then `_map_family` at `:153-171` | For each map, `_children` (`:173-197`) reads the map's `subIssues` and the `CROSS_REFERENCED_EVENT`s in its timeline (`first: 100`, paginated). |
| Spec recognition | `providers/github.py:165-166`, `is_spec` at `providers/__init__.py:327-330` | A cross-referencing issue whose first non-blank line is `Part of #<map>` (`part_of`, `model.py:171-176`, regex at `model.py:26`) and that has no `wayfinder:` label is the spec. `as_spec` sets `map_number`. |
| Spec's tickets | `providers/github.py:167-170` | The spec's sub-issues plus the cross-referencing issues whose `## Parent` names the spec. These are fetched **after** the spec is found and never gate it. |
| Lane | `build_board` at `model.py:284-299`, `lane_tickets` at `model.py:185-188` | Every ticket with a `spec_kind` and `map_number == map` goes in the lane. `spec_kind` is `"spec"` when `part_of(body) == map_number` (`model.py:124-129`). |
| Column | `classify` at `model.py:240-248` | An open, unblocked, unassigned spec goes in **Frontier**. |
| Render | `_apply` at `board.py:724-737`, `_shown_lanes` at `board.py:759-767`, `_visible` at `board.py:742-744` | The board re-renders when the lanes differ. Cards are hidden only by the `/` text filter or the `f` picker (type, mode, assignee, author, map). |

What `s` does: `start_refusal` (`model.py:78-95`) runs before every start (`board.py:1029`). Its rules apply in this
order:

1. closed
2. **`spec_kind == "spec"`, refused**
3. blocked
4. claimed by someone else
5. impl with an open PR
6. not wayfinder work

An `impl` card starts `/implement` (`start.py:32`, `board.py:1059`). Every other card starts `/wayfinder`.

## 2. What #88 and its spec look like on GitHub

- **#88**: OPEN, labelled `wayfinder:map` (labelled 08:45:57Z), never closed or reopened. Sub-issues: #89, #90,
  #91, #92 and #95, all CLOSED with `wayfinder:*` labels.
  - Source: `gh issue view 88` and GraphQL `issue(number:88){subIssues, timelineItems}`.
- **Spec #99**: same title as the map, OPEN, label `ready-for-agent` (labelled 09:27:58Z), never assigned. Created
  09:27:57Z. Its body begins `Part of #88` and carries `## Problem Statement` and `## Solution`.
  - `userContentEdits` is empty and `lastEditedAt` is null, so `Part of #88` was there from creation.
  - It is not a sub-issue of #88, which is the expected convention.
  - `parse_blockers` on its body returns `()`, so it has no body blockers.
- **#88's cross-reference from #99**: `createdAt` 09:27:58Z, `referencedAt` 09:27:57Z, source type `Issue`.
  - `createdAt` here is the real time the event was written, not a copy of the source's time. For comparison,
    #101 was created at 09:32:11Z and edited at 09:32:33Z, and its cross-reference on #99 is stamped 09:32:34Z,
    which tracks the edit.
  - #88's timeline holds only three cross-references (a PR, #99, #109), well inside `first: 100`.
- **Tickets**: #101 and #103-#108, created 09:32:11-09:32:49Z. Each is a sub-issue of #99, with `## Parent` set to
  `#99`. #100 ("probe", created 09:32:05Z, body "probe") is unrelated. #109 (09:38:03Z) is the #106 split.

## 3. The mismatch

### `s` on the spec: missing feature (confirmed)

The data follows the board's convention, and the board refuses the action on purpose (`model.py:83-84`). #99
stories 31-35 specify the change. #109 implements it for map specs, and #106 implements it for standalone specs.

### "Not shown until tickets existed": not reproduced

What I checked, each of which renders #99 in Frontier or rules a cause out:

1. **Live fetch now.** `GitHubProvider.fetch` + `build_board` puts lane 88 in these columns:
   - Frontier `[99, 101]`
   - Blocked `[103..108]`
   - Decided `[89, 90, 91, 92, 95]`

   The running pane `w18:pK`, read-only through `herdr pane read`, shows the same board with `updated 11:39:41 ·
   every 5s` and no rate-limit warning.
2. **Zero-ticket replay through the real app.** This is a script outside the repo, run with the installed venv's
   python.
   - It fetched live GitHub with `_children` patched so that #99 has no sub-issues or references, and #100-#119
     are dropped from #88's results. The fetch returned `[88, 89, 90, 91, 92, 95, 99]`.
   - It fed those tickets to `BoardApp` through a fake provider, under `run_test` at 106×55, 200×60 and 60×40 (the
     last one in single-column mode).
   - The displayed cards were `[88, 99, 89, 90, 91, 92, 95]` at every size.
3. **The running code is the code I tested.** The installed venv package
   (`~/.claude/plugins/data/ticketmaster-kareblak/venv/.../ticketmaster`) is byte-identical to `main` for
   `model.py`, `board.py`, `start.py`, `providers/github.py` and `providers/__init__.py` (`diff -q`).
4. **No saved filters.** `~/.config/ticketmaster/settings.json` was last written on Oct 13 and holds a Gitea repo
   only. The `f` picker writes this file on every change (`board.py:1102-1116`), so the picker was not used on
   this repo. A `/` text filter is unsaved, but it would show in the status line, and #99's body would match most
   words anyway.
5. **No rate limiting or staleness.** This comes from the user's answer and from the pane's status line now.
6. **Linear and the other trackers** do not treat an empty map differently. Linear finds specs by a description
   search (`providers/linear.py:102-103`, `:285-306`), and GitLab and Gitea use `with_specs`
   (`providers/__init__.py:139-147`). Neither depends on tickets.
7. **Caches across refreshes.** The only one is `IssueStates` (`providers/__init__.py:248-263`), which remembers
   closed or missing issues for **blocker** counts only. It cannot hide a card.

Remaining hypotheses, none confirmed:

- **A. The user saw the spec card but took it for the map.** #99 has the map's exact title. Its summary line opens
  with `Part of #88`, and its chip line ends with the map's title (`spec ◆ Show standalone…`). In Frontier it looks
  like a copy of the lane header. Pressing `s` on it gives "is a spec: s starts /implement on its implementation
  tickets", which reads like "this is not the thing to start". This fits the facts best, but it contradicts the
  user's account.
- **B. GitHub served #88's timeline without the #99 event for a while.** Read-replica or cache lag is possible in
  principle, but the event's `createdAt` contradicts it and there is no way to check after the fact.
- **C. The user looked at a different board instance.** Only one board process is running now (pid 70859, started
  08:41:51Z). Panes labelled `ticketmaster` for other repos (`w7:p4`, `wJ:pF`, `w19:p2`) have no board process, so
  an exited board cannot be ruled out.

## 4. Fix options

1. **Recommended, and chosen: ship #109.** The spec leaves the columns and is shown on the map card (`▣ #<spec>`
   with its ticket progress, or `no tickets yet`). `s` on the map card of a map whose spec has no tickets starts
   `/to-tickets <spec-url>`, focused, in `issue-<spec>`. This removes the look-alike card behind hypothesis A.
   - Trade-off: it fixes the real blocker and hypothesis A without new infrastructure, but it does not explain the
     report if the card truly was absent.
2. **Add a debug fetch log** (opt-in, e.g. `TICKETMASTER_DEBUG=path`). It would record, per refresh, the time, the
   lane composition and each map's cross-reference sources.
   - Trade-off: it is the only way to catch B or an unknown bug next time, but it costs a small amount of code and
     disk, and the information arrives only after a recurrence.
3. **Also discover specs with the `in:body "Part of"` search** that the board already runs (`github.py:95-96`),
   not only through #88's timeline. Today that search is used only for closed maps.
   - Trade-off: it makes discovery robust to timeline lag (B) at no extra GraphQL cost, but if B is not the cause
     it only adds a second path to maintain.

## Open questions

1. When #99 was "not shown", was its card perhaps in Frontier under the map's own title (hypothesis A)? If the user
   is sure the Frontier column of lane #88 was empty, hypotheses B and C remain, and option 2 is needed to catch a
   recurrence.
