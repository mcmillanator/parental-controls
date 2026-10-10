# PROCESS_GROUPING_PLAN — Grouped Process Display

Feature branch: `feature/process-grouping` (worktree `process-grouping/`), based on `master` @ de3f147.
Research + design only — nothing in this file is implemented.

## 0. Current state (what this feature changes)

- `budgets.list_user_processes()` (`budgets.py:508`) returns one dict per process:
  **`pid` (int), `comm` (str, 15-char `ps -o comm`), `etime` (str, raw ps ETIME),
  `cpu_seconds` (float), `args` (str), `cmdline` (str, `/proc/<pid>/cmdline`
  enrichment, falls back to `args`).** The fixture
  `tests/fixtures/tester_ps_snapshot.txt` carries the same fields with
  `cmdline := args` (no /proc enrichment in a captured snapshot).
- Visible processes are already grouped **by category** in
  `budgets.group_by_category()` (`budgets.py:463-470`) and shaped into
  `groups[cat]` lists; `app.get_user_live()` (`app.py:1089-1145`) annotates
  `p['elapsed'] = format_etime(p['etime'])` (whole minutes, `app.py:342-369`)
  and `api_user_live()` (`app.py:1148-1182`) slims each row to
  `{pid, comm, elapsed}` for the `/api/user/<username>/live` poll.
- The UI renders **one table row per PID** in two places:
  - Server-rendered: `templates/user.html:227-287` (Jinja loop over `groups[cat]`).
  - Live refresh: `renderProcesses()` at `templates/user.html:336-375`
    (rebuilds `#process-section` innerHTML every 10s, `user.html:601-603`).
- Hide/Show-hidden is already **per-comm**, not per-PID:
  `budgets.set_pref(username, comm, action)` (`budgets.py:251-279`), endpoint
  `app.py:1002-1020`, buttons `hideProc`/`showProc` (`user.html:524-560`).
  Hiding a comm hides **every instance** of it. This makes comm-keyed grouping
  the natural, 1:1 fit with existing semantics.

```none
tester_ps_snapshot.txt today: 26 rows, all plumbing daemons.
Runner for helpers like steamwebhelper (5+ instances, one per --type arg):
  pid  comm            etime        args
  9002 steamwebhelper  01:00:00     .../steamwebhelper --type=renderer
  9005 steamwebhelper  01:00:00     .../steamwebhelper --type=gpu
  ...the exact fire-and-forget pattern that floods the table today.
```

---

## 1. Group key: `comm` (recommended)

| Candidate | Example | Verdict |
|---|---|---|
| **`comm`** (ps output, 15-char truncated) | `steamwebhelper` | **Recommended** |
| argv0 basename (`_argv0_basename`, `budgets.py:167-171`) | `steamwebhelper` from `.../steamwebhelper` | Rejected |
| full cmdline | `... --type=renderer` vs `--type=gpu` | Rejected (defeats the purpose) |

### Why `comm`
1. **Stable across the flood you're trying to fix.** All five `steamwebhelper`
   instances share `comm`, but differ in cmdline (`--type=renderer/gpu/...`) —
   full-cmdline grouping would still produce five rows.
2. **Matches Hide/Show exactly.** `process_prefs.json` and `/api/process-prefs`
   are keyed by comm. A row-per-comm table makes Hide actions 1:1, and an
   expanded group shows exactly the members a Hide would hide.
3. **Matches the existing displayed column.** The current Command cell already
   shows `proc.comm`; the whole pipeline (denylist `is_default_hidden`,
   `set_pref`, `apply_filter`) reasons in comm space.

### Odd cases (all resolve cleanly under `comm`)
- **Truncated comms (Linux `TASK_COMM_LEN=16`).** `ps -o comm` returns 15
  chars (`xdg-desktop-por`, `dbus-broker-lau`) — the denylist
  (`DEFAULT_HIDDEN_COMMS`, `budgets.py:125-146`) already carries both
  spellings. Grouping by comm groups what the UI already displays; truncation
  is pre-existing, unchanged behavior. Two different binaries truncating to
  the *same* comm would merge — vanishingly rare, and they'd already share a
  Hide state today (same accepted risk, no regression).
- **Same comm / different binary** (e.g. two different `python3` scripts):
  grouped together — a group Hide hides both, which is *identical* to today's
  per-comm Hide semantics. The expanded rows expose each member's `cmdline`
  so the parent can still tell them apart.
- **Kernel threads** (`kworker`, `khugepaged`, `[kthreadd]`): never root-owned,
  so never present in `ps -u <user>` output (`list_user_processes`,
  `budgets.py:508`). None to worry about. `(sd-pam)` style session names are
  comms like any other and are denylisted.
- **Empty/`''` cmdline** (zombies, exec-bound processes): `cmdline` can be
  `''`. An argv0-basename key would collapse **all** such processes into one
  `''` group — a strong argument against basename. `comm` is never empty.

### Compose with category
The payload is already `groups[cat]`, so the group key is **`(category, comm)`**:
grouping happens *inside* each category bucket. The same comm may legitimately
appear in two categories (e.g. `python3` running a game script vs a utility);
each gets its own row in its own table. Expansion-state keys must therefore be
`category + '\u0000' + comm` (see §3).

---

## 2. Server-side grouping (recommended) over client-side

**Recommendation: group in `budgets.py` / `app.py`, ship groups in the `/live`
payload; the client stays a dumb renderer.**

### Justification
1. **Testability matches the suite.** A pure stdlib function
   `group_by_comm(procs) -> [Group]` in `budgets.py` is unit-testable in
   `tests/test_budgets.py` with `STEAM_PROCS` + a new multi-instance fixture,
   exactly like `group_by_category` is today.
2. **No JS test harness exists.** The repo has no `package.json`/node tooling
   (verified: no jsdom/playwright anywhere). The suite convention is stdlib
   unittest + mocked subprocess + Flask test client. Keeping logic in Python
   leaves the JS delta to pure rendering.
3. **Hide-state interplay is already server-side.** `apply_filter` splits
   visible/hidden *before* grouping; the server also computes `category` and
   `elapsed`. Grouping after that pipeline keeps one source of truth.
4. **Payload cost is ~free.** The 10s poll already transmits every visible PID
   (flat `slim_groups`). A group shape carrying `members` transmits the same
   PIDs plus one repeated `comm` per member — negligible.
5. Paren features compose: a "cap at N members" or "sort members by pid" rule
   belongs in one tested Python function, not one untested JS template string.

### Payload + fixture/test impact either way
- **New `budgets.group_by_comm(procs)`** (pure, no I/O):
  input `[proc dict]` (fields per §0), output `[ {comm, count, members,
  max_minutes, max_elapsed} ...]` — members sorted by `pid` asc.
- **`app.get_user_live()`** (`app.py:1089`): after `group_by_category`, map
  each `groups[cat]` through `group_by_comm`; do the same for `hidden`
  (grouped across categories; see §5).
- **`app.api_user_live()`** (`app.py:1148-1182`): replace the flat
  `slim_groups` builder (`app.py:1161-1166`) with a slim group dict:
  `{comm, count, elapsed (max member), members: [{pid, comm, elapsed}]}`.
  Keep top-level key names `groups`/`hidden` so `renderProcesses` churn is
  contained. Server-side grouping also means `paused` stays derivable
  client-side from the existing `budget` field (§4).
- **Fixture**: `tests/fixtures/tester_ps_snapshot.txt` needs **no change** (its
  26 rows are all single-instance plumbing; it still validates filtering).
  Add a synthetic multi-instance case to the test-only `STEAM_PROCS` list
  instead (multiple `steamwebhelper` PIDs), keeping the repo fixture stable.
- **Tests that change**: `tests/test_budgets.py::test_group_by_category`
  (add group assertions, `:176-182`); `tests/test_safeguards.py` template
  smoke tests (`:201-229`) — their `live` dicts use empty `groups`, so they
  keep passing, but the Jinja loop they exercise changes shape. No existing
  test hits `/api/user/<username>/live` directly (verified) — no JSON-shape
  breakage outside new tests.
- **Client-side grouping** would keep today's flat payload, but would push
  aggregation, sorting, mixed-category labeling, and the perf cap into
  untested JS and duplicate Hide semantics that already live server-side —
  strictly worse here.

---

## 3. Expand/collapse surviving the 10s refresh (+ accessibility)

### State (JS)
- One `const expanded = new Set()` in the page script, keyed
  `category + '\u0000' + comm` (a comm can span categories; §1).
- `renderProcesses()` already rebuilds `#process-section` innerHTML wholesale
  every poll (`user.html:374`). It **captures the current open set before
  rebuilding** (read `data-group-key` attributes on the live DOM, not the old
  closure), then emits:
  - a **group row** `<tr class="group-row" data-group-key="games\0steamwebhelper">`
    with a real `<button class="btn-group-toggle" aria-expanded="false|true"
    aria-controls="detail-games-steamwebhelper">`; and
  - when open, an inserted **detail row** immediately after:
    `<tr class="group-detail" id="detail-games-steamwebhelper"><td colspan="4">`
    containing a nested table of member rows (PID / Command+cmdline / Elapsed /
    Category / Actions).
- Clicking the toggle button flips the key in `expanded` and calls
  `renderProcesses()` render *or* just toggles the sibling row's visibility.
  Recommendation: **toggle visibility in place** (no full re-render) and let
  the next scheduled poll re-render naturally — cheaper and avoids focus loss
  on every click. Either way the set survives, which is the requirement.
- **Prune** keys whose group no longer exists in the newest payload after
  render (group died, or moved to hidden). Rationale: a leftover open-state for
  a vanished comm would surprise on reappearance; pruning keeps the set small.
  (Alternative — keep keys forever — noted in §9 open questions.)
- New PIDs in an open group appear policy-free: the payload always carries the
  full current member list, so the next render shows the newcomers; dead PIDs
  vanish the same way. No diffing needed.

### Accessibility
- The toggle is a real `<button>` (native Enter/Space, focusable, no
  div/onclick hack). `aria-expanded` reflects the set, `aria-controls` points
  at the detail row id.
- The detail row is one accessible `<tr>` with a nested table inside a
  `colspan` cell — groups its members under one heading when rendered; the
  button's accessible name includes the count ("steamwebhelper, 5 instances")
  so screen-reader users don't need to expand to know it's a group.
- Focus preservation on the 10s rebuild: the button the parent is interacting
  with loses focus when innerHTML is replaced — a pre-existing limitation of
  the whole-section rebuild (Hide buttons already do this today). Mitigation
  (cheap, recommended): before re-render, record the focused element's
  `data-group-key`; after render, `focus()` the matching toggle if the focus
  was inside `#process-section`. No behavior change otherwise.
- No `confirm()` anywhere in this feature (consistent with the convention:
  confirm only on destructive buttons; group expand/collapse/Hide are not
  destructive).

---

## 4. Collapsed-row aggregates

A group dict carries (server-computed):
`{comm, count, members[], max_minutes, max_elapsed, uniform_category, categories}`.

| Column | Rule |
|---|---|
| Command | `comm` + count badge `× N` (hidden when `count == 1`) |
| **Elapsed** | **max (oldest)** member's elapsed — computed from raw `etime`
  minutes, **never by string comparison** (`format_etime` output like `'21h 47m'`
  sorts lexicographically wrong). Parse each member's `etime` via the same
  mm logic as `format_etime` (`app.py:342-369`), take the max minutes, format
  once. **Total/sum is rejected**: summing wall-clock of parallel helper
  processes is meaningless (five `steamwebhelper`s alive 1h are not "5h of
  use"). Sum of *CPU* seconds is available (`cpu_seconds`) but the UI already
  displays wall time only. |
| Category | **Uniform by construction for visible groups** — grouping happens inside
  `group_by_category`, so every member has the table's category. Show it in
  expanded rows anyway (cheap, already computed). For the **hidden** table,
  which groups across categories, show the label when all members agree and
  **`Mixed`** otherwise. |
| Budget-paused | Pause is **category-scoped, never per-member**: the freezer
  freezes the whole `parental-<user>-<category>` cgroup
  (`budgets.py:707-753`), so "any member paused" ⇔ `budget_state[cat].paused`
  — already in the `/live` payload (`data.budget[cat].paused`, `budgets.py:646`).
  Recommendation: show one `badge-paused` on the **category header** (`<h3>`,
  like the budgets table does) rather than repeating it on every group row;
  optionally mirror it per group row if the parent wants it louder. |

---

## 5. Interaction with Hide/Show-hidden, category sections, budget actions

- **Hide/Show-hidden granularity: per-group (comm).** Hide is already stored
  per-comm (`set_pref`, `process_prefs.json`) — the current UI merely *shows*
  one Hide button per PID while hiding all of them. Grouping fixes the
  mis-encoding. So: **one Hide button on the group row**;
  expanded member rows show **no Hide button** (it would be redundant —
  hiding one member hides the group). Show-hidden: the hidden table also
  groups by comm, one Show button per group row, and its toggle shows
  `Show hidden (N groups, M processes)`.
- **Category sections unchanged structurally**: each `<h3>Category (N)</h3>`
  now counts **groups** instead of processes. The budget table, bedtime, and
  account sections are untouched.
- **Budget actions at group level: default none.** Pause/resume is per-category
  (cgroup freezer + `resumeCategory`, `user.html:582-599`). A group contains one
  category by construction, so a group-level Resume would be a category-level
  Resume wearing a costume — and a parent must be able to resume a paused
  category with *no* members running, which a group row can't express. The
  existing per-category Resume in the Time Budgets table stays the single
  control.
- **Server shape for hidden rows**: group `hidden` by comm across categories;
  member `category` kept per-member (drives the Mixed label, §4). The Show
  action stays per-comm, so one Show per group row.

---

## 6. Edge cases

| Case | Handling |
|---|---|
| PID churn between polls | Full member list re-sent each poll; open groups re-render their current members. PIDs that exited vanish, new PIDs appear — zero diffing logic in JS. |
| Group appears/disappears | Rows appear/disappear with the payload; the expand-set prunes keys absent after render (§3). |
| All members hidden (whole group hidden) | Happens only *across* polls: parent clicks group Hide → `/api/process-prefs` POST → `refreshUserLive()` → `apply_filter` moves the whole comm to hidden in the same response. The visible table drops the group; the expand-set prune drops its key; the hidden table gains the group. No per-member partial case is possible (hide is comm-keyed). |
| Empty groups | `group_by_comm` emits only non-empty groups. `count == 1` renders like today's row, no `× N` badge; the disclosure is present but inert (or hidden — recommendation: render the button with `aria-expanded=false` and `disabled` when `count == 1`, so the DOM shape is uniform and a11y tools see no phantom expander). |
| 100+ member groups (Chromium renderer storms) | The 10s payload already carries every visible PID today (flat list), so grouping introduces **no** payload regression — the cost question is DOM size when expanded. A 200-row table is fine on a desktop browser; recommend **no cap in v1** but sort members by `pid` asc for stable order. If the parent prefers a cap, add a single Python constant (`MAX_EXPANDED_MEMBERS`, e.g. 50) rendering the first N + `… N more not shown` — one tested knob. |
| Same comm in two categories | Two separate rows in two category tables; expand keys include the category so state can't cross (§1/§3). |
| Mixed-category hidden groups | "Mixed" label per §4. |

---

## 7. Test plan (no live sessions — mocks/fixtures only)

Extend the existing stdlib-suite conventions (`tests/test_budgets.py`:
fixture `tests/fixtures/tester_ps_snapshot.txt` + `parse_snapshot` + synthetic
`STEAM_PROCS`; Flask test client with mocked `get_user_live`/auth per
`tests/test_safeguards.py:201-229`; `mock.patch.object(...)` for subprocess).

**New/updated tests**

1. `group_by_comm` (new pure fn) in `tests/test_budgets.py`:
   - duplicate comms group (`STEAM_PROCS` extended with a second and third
     `steamwebhelper` PID) → one group, `count == 3`, members include all PIDs,
     sorted by pid;
   - singleton comms → `count == 1`;
   - empty input → `[]`;
   - deterministic order (input shuffle → same output order);
   - max elapsed chosen by **minutes**, not string (procs at `1h 2m` vs `59m`
     vs `3d` · assert oldest wins).
2. Aggregates: count, `max_minutes`, `max_elapsed` formatting stays whole
   minutes only (assert no seconds in any emitted value — convention).
3. Category composition: same comm under two categories → two groups with
   correct per-table membership; hidden-group `Mixed` label when member
   categories differ.
4. `get_user_live` / `api_user_live` shaping in `tests/test_safeguards.py`
   (or a new `tests/test_process_grouping.py`): test client against a mock
   that returns duplicate-comm witness rows; assert the JSON `groups[cat]`
   entries are group dicts (`comm`, `count`, `members`) and that no flat
   per-PID duplicate rows remain. Assert `budget` still present (drives the
   paused badge) and single-instance groups keep `count == 1`.
5. Template smoke: render `/user/kid` with a live dict containing duplicate
   comms; assert the `× N` badge, exactly one group row per comm (no duplicate
   rows), `aria-expanded`/`aria-controls` attributes present, and the hidden
   table groups by comm. Existing `test_user_page_has_emergency_logout`
   (`test_safeguards.py:201`) must still pass unchanged (empty groups).
6. Regression: full `unittest discover -s tests` (~205 tests incl. the
   mocked-subprocess accounting/watchdog suites) green. The live-PAM suite
   (`test_lock_usermod_real.py`) is untouched — this feature never exercises
   real accounts.
7. JS: no node/jsdom in the repo; JS stays declarative render-only. The
   expand-set logic is exercised indirectly via the template tests +
   keep each JS function under ~30 lines and free of control flow that would
   need unit-level coverage. (If the parent wants real JS unit tests, that is
   a separate toolchain decision — see §9.)

---

## 8. Rollout

**Files changed (all in this worktree, merge to `master` after review):**
- `budgets.py` — add `group_by_comm()` (+ hidden-group aggregation, max-elapsed
  helper). Pure, no I/O, no new imports.
- `app.py` — `get_user_live()` applies `group_by_comm` to visible+hidden;
  `api_user_live()` slim payload groups (`:1161-1171`).
- `templates/user.html` — Jinja loops (`:227-287`), `renderProcesses`
  (`:336-375`), toggle button + detail-row markup, CSS for the count badge and
  expander, prepare-expanded-capture JS.
- `tests/test_budgets.py`, `tests/test_safeguards.py` (or new
  `tests/test_process_grouping.py`) — per §7.
- **Unchanged**: `auth.py`, `install.sh`, `systemd/`, `sudoers-*`, runtime
  JSON (`process_prefs.json`, `budgets.json`, `bedtime.json`), requirements.

**Deploy** (per `AGENTS.md`, performed by the operator after merge — *not* in
this research phase):
1. `sudo ./install.sh` re-runs (stages code via rsync, preserves runtime
   state, re-renders units).
2. **No unit/sudoers changes are expected**, and `install.sh` restarts the
   service **only when unit files change or installed units are newer than the
   running service** (`NEED_RESTART="$UNITS_CHANGED"` + timestamp check,
   `install.sh:203-218`). With no unit changes the installer stages new code
   **without restarting** — the running process keeps serving the old UI until
   the operator restarts the service (per the hard rule, we never touch
   `parental-controls.service` ourselves).
3. Verify: `curl localhost:5000/` → login redirect; log in; open a user's
   details page; confirm group rows render, expand/collapse survive ≥2 refresh
   cycles (20s), and grouping matches the live snapshot.

---

## 9. Open questions for the parent

1. **Elapsed aggregate**: max (oldest) recommended. Do you want sum-of-wall
   (rejected: meaningless for parallel helpers), sum-of-CPU (`cpu_seconds`, not
   currently displayed anywhere), or max + sum-of-CPU in a tooltip?
2. **Per-group Hide** on the group row only, or also a redundant Hide in the
   expanded member rows (harmless, but noise)? See §5.
3. **Paused badge placement**: category header only (recommended), or repeated
   per group row in a paused category?
4. **Persist expand state across page loads** (sessionStorage) or keep it
   in-page-only per the requirement (survives the 10s refresh only)?
5. **Perf cap** for expanded groups: render-all (recommended, payload already
   carries every PID) vs a tested `MAX_EXPANDED_MEMBERS` cap with a "+N more"
   note?
6. **Singleton disclosure**: render the toggle disabled for `count == 1`
   (uniform DOM) or omit it entirely for singletons?
7. **Hidden table count text**: `Show hidden (N groups, M processes)` as
   proposed, or keep process count only?
8. **Group-view toggle**: do you want a flat/grouped view switch in the UI, or
   is one grouped representation the goal? (Not planned here; grouping is
   always-on.)
9. **JS unit tests**: introduce a Node/jsdom toolchain (new repo convention) or
   keep JS declarative and covered via the Python contract tests (§7)?