# Freeze/Unfreeze Buttons — Implementation Plan (NO CODE)

Branch: `feature/freeze-buttons` (base: `master` @ `df5d382`).
Scope: Time Budgets table + Processes table on `templates/user.html`.
Author: planning agent. Implementer: separate agent — follow this file exactly.

Conventions used below: `file:line` numbers refer to the worktree at plan
time. "Frozen" = cgroup `cgroup.freeze == 1` (desired) — see
`budgets.py:_read_desired_frozen`.

---

## 1. Current-state inventory

### 1a. budgets.py — freeze/pause functions

| Function | Location | What it does |
|---|---|---|
| `is_never_pause(proc)` | `budgets.py:217` | Match-time + freeze-time gate. NEVER-pause when `comm.lower()` ∈ `NEVER_PAUSE_COMMS` (`budgets.py:200`), or argv0-basename ∈ set, or cmdline contains `app.py` + (`parental` or `flask`). |
| `NEVER_PAUSE_COMMS` | `budgets.py:200` | Compositors, audio, dbus/systemd, sshd/sudo/su, shells, python/flask/gunicorn, plasmashell/kded6/ksmserver/klauncher/kactivitymanage/kglobalaccel6/kaccess/kwalletd6, gnome-shell/mutter, pulseaudio. |
| `freezable_members(procs, username, category, limits, prefs, index)` | `budgets.py:1271` | **Match-time gate**: returns [] unless `limits[category] > 0`; else visible-after-`apply_filter` AND `categorize == category` AND NOT `is_never_pause`. NOTE: requires a configured budget limit — manual freeze with no limit needs a decision (§2, Q2). |
| `verify_freeze_safety(username, member_pids, expected_freezable)` | `budgets.py:1234` | **Freeze-time gate**: every cgroup member must be owned by user's UID (`os.stat /proc/<pid>`); every member must be in `expected_freezable` or re-checked via `/proc/<pid>/comm` + `is_never_pause`; unknown-but-present member aborts (`not in the freezable set`); empty-expected + non-empty-members aborts. Exited PIDs skipped. |
| `ensure_cgroup(username, category)` | `budgets.py:935` | `sudo mkdir -p parental-<user>-<cat>`. Only our name pattern (path built by `cgroup_path`, `budgets.py:902`, which validates username + category). |
| `set_frozen(username, category, frozen)` | `budgets.py:960` | `sudo tee cgroup.freeze` + readback verify. Thaw of a missing cgroup returns `(True, 'no cgroup (already unfrozen)')` — fail-open thaw, fail-closed freeze. |
| `read_freeze(username, category)` | `budgets.py:952` | `sudo cat cgroup.freeze` → True/False/None. |
| `cgroup_members(username, category)` | `budgets.py:984` | `sudo cat cgroup.procs` → [pids]; [] on any failure. |
| `migrate_pid(username, category, pid)` | `budgets.py:1002` | `sudo tee cgroup.procs`. No-op behaviorally (does not stop the process); placement only. |
| `unfreeze_all_user(username)` | `budgets.py:1011` | Thaws every `parental-<username>-<valid-category>` dir. Only our pattern; skips non-category suffixes (e.g. a `quarantine` group is NOT touched here — watchdog reports it). |
| `account_and_enforce(...)` | `budgets.py:1292` | Per-user tick. Reset branch (`budgets.py:1333`): clears `used_seconds/paused/resumed/warned_low/warned_paused`, sets `day`, calls `unfreeze_all_user`. Exhaust branch (`budgets.py:1409`): `not paused and not resumed` → ensure → migrate members → `verify_freeze_safety` → `set_frozen(True)` → `paused[cat]=True`. Late-start branch (`budgets.py:1442`): `exhausted and paused and members` → migrate newcomers → re-freeze. Pre-place branch (`budgets.py:1464`): `members and not paused` → migrate only, never freeze. Admin guard (`budgets.py:1322`): `is_admin_protected` (wheel member, `budgets.py:89`) returns early — never auto-frozen, not even snapshotted (`tick_budgets`, `budgets.py:1504`). |
| `resume_category(username, category)` | `budgets.py:1542` | **Existing manual Resume**: `set_frozen(False)` → pop `paused[cat]` → set `resumed[cat]=True` (inhibits re-freeze until reset). Message: `'Resumed; will not re-pause until the next reset'`. |
| `set_limit(username, category, minutes)` | `budgets.py:823` | `minutes<=0` clears limit AND pops `paused`/`resumed` AND calls `set_frozen(False)`. Positive values only write `limits[cat]`. |
| `budget_status(username, data)` | `budgets.py:855` | Per-category `{minutes, used_minutes, remaining_minutes, paused, exhausted}`. `paused` reads `entry.paused`. No manual-frozen key exists yet. |
| `budget_reset_needed(...)` | `budgets.py:880` | Bedtime-end transition OR new calendar date when out of bedtime. |
| `watchdog_tick(...)` | `budgets.py:1115` | Frozen = desired (`cgroup.freeze==1`) AND effective (`cgroup.events frozen 1`). Legit ONLY if `budgets_data[user].paused[cat]` (`budgets.py:1201`). Else thaw + report. Foreign `parental-*` → `unknown`, never touched. Mid-transition / unreadable → skip. Ordering contract: enforcement first, watchdog second, same dict (`tick_budgets`, `budgets.py:1528`). |
| `parse_managed_cgroup(name)` | `budgets.py:1056` | `parental-<user>-<cat>` split on LAST hyphen; None unless `valid_username` + `valid_category`. |
| `cgroup_path(username, category)` | `budgets.py:902` | Raises `ValueError` on bad username/category. All freezer ops route through it. |
| `is_admin_protected(username)` | `budgets.py:89` | Wheel supplementary OR primary GID. False on unknown group/user. |

### 1b. app.py — budget endpoints & payloads

| Endpoint | Location | Method/fields |
|---|---|---|
| `POST /api/budgets` (`api_budgets_set`) | `app.py:1386` | `username, category, minutes`. Empty minutes → 0 → clear path. Delegates to `budgets.set_limit`. No `_wheel_target_refused` check (budget tick skips wheel anyway). |
| `POST /api/budgets/resume` (`api_budgets_resume`) | `app.py:1408` | `username, category`. Delegates to `budgets.resume_category`. This is the **existing manual Resume button** backend. |
| `GET /api/budgets/<username>` (`api_budgets_get`) | `app.py:1424` | Returns `budget_status` dict directly. |
| `POST /api/process-prefs` | `app.py:1365` | `username, comm, action=hide\|show`. Pattern to copy for new endpoints (validation order: presence → `valid_username` → delegate). |
| `GET /user/<username>` (`user_detail`) | `app.py:1433` | Renders `user.html` with `budget_state=data['budget_state']`, `groups`, `hidden`, `category_labels`, `categories`. |
| `GET /api/user/<username>/live` (`api_user_live`) | `app.py:1532` | Payload: `{success, locked, status_detail, in_bedtime, manual_override, bonus, groups, hidden, error, budget}`. `budget` = `budget_status` output. `groups` slim: `{comm, display_name, count, elapsed, members:[{pid, comm, display_name, elapsed}]}` — **no category per group** (category is the dict key), **no pid category**. `hidden` slim adds `categories[], uniform_category`, members carry `category`. |
| `get_user_live(username)` | `app.py:1452` | Builds all of the above. Visible groups: `group_by_category` → per-cat `group_by_comm` (`app.py:1507`). Hidden: categorized then `group_by_comm` across categories (`app.py:1515`). |
| `require_admin` | `app.py:862` | Session → tier → must_change → CSRF (`X-CSRF-Token`) for POST. All new endpoints get it free via decorator. |
| `_wheel_target_refused(username)` | `app.py:926` | 403 JSON when `auth.is_wheel_member(username)`. Used by lock/logout/bonus endpoints — NOT by budget endpoints today. §3 decides whether freeze endpoints use it. |

### 1c. templates/user.html — UI elements

| Element | Location | Notes |
|---|---|---|
| Budget card table | `user.html:246-282` | Columns Category / Limit / Used / Left / Status. Row ids `budget-<cat>`, `budget-used-<cat>`, `budget-left-<cat>`, `budget-status-<cat>`. |
| Manual Resume button | `user.html:274` + `user.html:925` (`resumeCategory`) | Server-rendered only when `b.paused`; `renderBudgets` re-renders it on each live poll when `b.paused` (`user.html:648`). POSTs `username,category` to `/api/budgets/resume` with `X-CSRF-Token`, toasts, calls `refreshUserLive()`. No confirm dialog (deliberate parent action, instantly reversible). |
| `renderBudgets(data)` | `user.html:637` | Updates used/left/status cells only; never touches limit inputs. New buttons MUST be rendered here (not just server-side) or the 10s `refreshUserLive` (`user.html:676`, interval `user.html:949`) will wipe them. |
| Process group rows | `user.html:302-317` (visible), `user.html:342-355` (hidden) | Per-comm groups: toggle-expand button, name + `× count`, used (billable budget usage since the last reset, not ps ETIME), **per-group Hide/Show button** (`hideGroup`/`showGroup`, `user.html:878-884`, `data-comm` attribute pattern — comm never in a JS string literal). |
| `renderProcesses(data)` / `groupRowsHtml(cat, group)` | `user.html:560` / `user.html:431` | Full rebuild every 10s; expand state survives via `expanded` Set (`user.html:380`); focus preserved (`user.html:622`). New process-table buttons must follow this pattern: render from `LIVE_DATA`, no local state. |
| `groupDetailHtml` member rows | `user.html:411` | Columns PID / Command / Used / Category. Currently no per-member buttons. |
| Confirm-dialog conventions | `user.html:716-854` | Lock/logout/unlock/clear/revoke/custom-bonus use `confirm()`; one-click bonus shortcuts and Hide/Show do NOT (instantly reversible, low stakes). Freeze/unfreeze is instantly reversible → **no confirm** (§3 justification). |
| Bonus Revoke button | `user.html:177`, `user.html:836` | Uses confirm (re-lock side effect). Freeze has no lock side effect — different case. |

### 1d. State keys (budgets.json per-user entry)

Shape documented at `budgets.py:793-800`. Freeze-relevant keys today:
`limits{cat:min}`, `paused{cat:bool}` (auto-exhaustion flag), `resumed{cat:bool}`
(manual override of auto-pause, inhibits re-freeze), `day`, `used_seconds`,
`warned_low/paused`, `was_in_bedtime`, `last_cpu`, `last_tick`.
No manual-freeze key exists — §2 adds one.

### 1e. cli.py / sudoers / tests

- `cli.py:60` `cmd_unfreeze_all` thaws every `parental-*-<valid-category>`
  cgroup via `budgets.set_frozen(user, cat, False)`. No budgets.json edits —
  after this plan, CLI-thawed categories with `manual_frozen` set would look
  frozen-but-aren't to the UI; §2 covers reconciliation (tick re-freezes or
  watchdog sees clean; UI reads cgroup truth — see Q3).
- `sudoers-parental-controls-budgets:10-14`: grants `tee` on
  `parental-*/cgroup.freeze` + `cgroup.procs`, `cat` on both, `mkdir -p
  parental-*`. **No new grants needed** — manual freeze uses exactly
  `ensure_cgroup/migrate_pid/set_frozen/cgroup_members` (§7).
- Test conventions: `tests/test_budgets.py:1-80` (stdlib-only, `sys.path`
  worktree root, synthetic procs + `tester_ps_snapshot.txt` fixture);
  `tests/test_lock_regression.py:63-119` (`_AdminSessionMixin`: temp auth
  store, mocked wheel membership, real login over test client, `authed_post`
  with CSRF); mocked `subprocess.run` via `_result`/`_run_side_effect`
  (`test_lock_regression.py:40-53`); mocked `budgets.set_frozen` /
  `cgroup_members` for Flask tests (never touch real cgroups).

---

## 2. State model: manual freeze vs auto pause

### Problem
Today `paused[cat]=True` means exactly one thing: "budget exhausted, cgroup
frozen by the tick". A parent pressing Freeze on a category with time left
must not (a) burn/lose remaining budget, (b) be undone by the next 60s tick
(pre-place branch migrates but never freezes — a manual freeze would survive
accidentally today, but the late-start/exhaust branches would then also set
`paused` and confuse Resume), (c) block day-reset auto-resume, (d) confuse
the watchdog into thawing a deliberate freeze (watchdog legitimacy =
`paused` only, `budgets.py:1201`).

### Proposal: add `manual_frozen{cat: bool}` (+ optional `manual_frozen_at{cat: iso}`)

| Key | Set by | Cleared by | Tick effect |
|---|---|---|---|
| `paused` | tick on exhaustion | reset; Resume/Unfreeze; `set_limit` clear | unchanged |
| `resumed` | Resume/Unfreeze (inhibits re-freeze) | reset; `set_limit` clear | unchanged: `exhausted and not paused and not resumed` gate (`budgets.py:1409`) |
| **`manual_frozen`** (NEW) | `freeze_category` | `unfreeze_category` (= new Resume backend); reset; `set_limit` clear; `unfreeze_all_user` wrapper if extended | tick MUST NOT set `paused` for a manually-frozen category (it is already frozen deliberately); tick MUST re-assert the freeze for late-start members (same safety gates); tick MUST NOT thaw it |

Detailed semantics:

1. **Freeze with budget remaining**: sets `manual_frozen[cat]=True` only.
   `used_seconds` keeps accruing underneath (wall-clock active ticks still
   bill — the app is frozen so deltas go quiet; usage effectively pauses
   while frozen, which is the honest accounting). `paused` stays False, so
   the Status cell shows "Frozen (manual)" not "out of time".
2. **Freeze on an already-exhausted+paused category**: adds
   `manual_frozen[cat]=True` alongside `paused`. Unfreeze then clears both
   and sets `resumed` (single click returns to running; the tick will not
   instantly re-freeze because `resumed` inhibits — same guarantee Resume
   gives today).
3. **Unfreeze/Resume** (single operation, §3): `set_frozen(False)` →
   pop `paused` → pop `manual_frozen` → set `resumed=True`. This subsumes
   `resume_category`; keep `resume_category` as a thin wrapper (back-compat
   for the existing endpoint + CLI-adjacent callers) OR repoint
   `/api/budgets/resume` at the new function. Recommendation: implement
   `budgets.unfreeze_category(username, category)` with the full semantics;
   reimplement `resume_category` as `unfreeze_category` alias (same return
   contract) so old clients keep working.
4. **Day reset** (`account_and_enforce` reset branch, `budgets.py:1333`):
   clear `manual_frozen` (+ `manual_frozen_at`) alongside
   `paused/resumed`; existing `unfreeze_all_user` call already thaws the
   cgroups. Manual freezes never survive a reset — matches "everything
   auto-unfreezes at reset" (`budgets.py:36`).
5. **`set_limit` clear path** (`budgets.py:837`): also pop
   `manual_frozen`/`manual_frozen_at` (clearing the limit releases ANY
   freeze — extend the existing comment).
6. **Tick steady-state for manually-frozen categories**: add a branch —
   if `manual_frozen.get(cat)` and `act`: ensure cgroup, migrate current
   `freezable_members` not yet in `cgroup_members`, re-run
   `verify_freeze_safety`, re-assert `set_frozen(True)` (covers late-start
   apps launched after the click; mirrors the late-start branch
   `budgets.py:1442`). If safety fails: do NOT thaw (fail-closed — a
   deliberate freeze stays frozen); record `summary['note']` and notify?
   Recommendation: note only, no notify (avoid spam; parent sees the state
   in the UI). If `not act` (tests): no-op.
7. **Watchdog legitimacy** (`budgets.py:1200`): legit if
   `paused[cat] OR manual_frozen[cat]`. One-line change; add the `or`.
8. **`budget_status`** (`budgets.py:855`): add `manual_frozen: bool` and
   `frozen: bool` (= `paused or manual_frozen`) per category. Keep `paused`
   and `exhausted` keys byte-identical (existing UI + `GET
   /api/budgets/<username>` consumers). `renderBudgets` keys off `frozen`
   for the toggle, `paused`/`manual_frozen` for the label.
9. **Reconciliation (cgroup truth vs JSON)**: the UI toggle reflects
   `frozen` from JSON (cheap, no sudo read per poll). If someone thaws
   behind our back (`cli unfreeze-all`, §1e), the next tick re-asserts
   manual freezes (rule 6) — self-healing within 60s. Do NOT add a
   `read_freeze` per category per poll (sudo fork × 5 per page view per
   10s; latency + log spam). See Q3.

### Alternatives rejected
- Reusing `paused` for manual freezes: Resume's `resumed` inhibit would
  then block nothing meaningful, exhaustion notifications (`warned_paused`)
  would fire wrongly, and "out of time" vs "frozen by parent" become
  indistinguishable in the UI and audit trail.
- A single `frozen_state: auto|manual` enum: churns every `paused.get`
  call site (tick ×3, watchdog, status, resume, set_limit); the additive
  boolean is a smaller, reviewable diff.

---

## 3. Budgets table

### Placement & behavior
- Status cell (`budget-status-<cat>`, `user.html:272`): replace with a
  **single toggle button** (not two buttons):
  - `frozen` → red-ish "Frozen — manual" / "Paused — out of time" badge +
    green **Unfreeze** button (same `btn-unlock` class as Resume today).
  - not frozen → "OK"/"Exhausted" text + **Freeze** button (`btn-lock`
    styling is red/lock-associated — use a neutral freeze class instead;
    add `.btn-freeze` reusing the `.btn-hide` grey or a new ice-blue; tiny
    CSS addition next to `.badge-paused`, `user.html:71`).
  - Categories with **no limit set** (`minutes` falsy): NO Freeze button
    (freezable_members requires a limit; freezing an unlimited category
    would create cgroup state the tick never manages). Show title tooltip
    "Set a limit first". This also answers Q2-default: gate on limit.
- Label copy: `Paused — out of time` (auto, keep today's string) vs
  `Frozen — by parent` (manual) vs `Frozen — out of time + manual` (both;
  Unfreeze clears both per §2.2).
- Both server render (`user.html:272` Jinja block) AND `renderBudgets`
  (`user.html:637`) must render the toggle — live refresh rebuilds the
  cell every 10s.

### Endpoints (new; exact shapes)
```
POST /api/budgets/freeze    fields: username, category
POST /api/budgets/unfreeze  fields: username, category
(keep POST /api/budgets/resume as alias → unfreeze_category)
```
- Decorator: `@require_admin` (CSRF enforced automatically).
- Validation order (mirror `api_process_prefs`, `app.py:1365`):
  1. `username` present else 400 `No username provided`.
  2. `app.valid_username(username)` else 400 `Invalid username`.
  3. `budgets.valid_category(category)` else 400 `Unknown category: …`
     (note: `resume_category` already returns this; validate in handler
     for a clean 400 before any sudo).
  4. `_wheel_target_refused(username)` → 403 (DECISION: yes — freezing a
     wheel admin's apps is a lock-equivalent action; consistent with
     lock/logout/bonus. The tick can never freeze wheel users anyway, so
     allowing manual freeze would punch a hole in admin self-protection).
  5. `budgets.is_admin_protected(username)` → 403 with admin message
     (belt and braces: covers wheel membership changes between the
     `auth.is_wheel_member` check and enforcement; mirrors
     `api_bedtime_set`, `app.py:1285`).
  6. Delegate: `budgets.freeze_category` / `budgets.unfreeze_category` →
     `{success, message}` JSON; non-ok → 400.
- `budgets.freeze_category(username, category)` (NEW, in budgets.py):
  1. `valid_username` / `valid_category` → `(False, …)`.
  2. `is_admin_protected(username)` → `(False, '… admin …')`.
  3. Load budgets; require `limits[cat] > 0` else
     `(False, 'Set a time limit for this category first')`.
  4. Snapshot: `procs = list_user_processes(username)`;
     `members = freezable_members(procs, username, category, limits, prefs,
     index)` (match-time gate incl. NEVER_PAUSE).
  5. `ensure_cgroup` → abort `(False, msg)` on failure (fail-closed).
  6. Migrate each member (`migrate_pid`; ignore individual failures —
     readback gate below is authoritative… or abort? Recommendation:
     ignore-and-verify, matching the tick `budgets.py:1419`).
  7. `current = cgroup_members(...)`;
     `verify_freeze_safety(username, current, {p.pid: p})` → abort
     `(False, reason)` on unsafe (fail-closed; cgroup left UNfrozen —
     `set_frozen(True)` has not run yet, matching tick order).
  8. Empty-category case: no members AND no current → record
     `manual_frozen` WITHOUT freezing (nothing to freeze; mirrors tick
     `budgets.py:1427`). Next tick's rule-6 branch freezes late starters.
  9. `set_frozen(username, category, True)` → abort `(False, msg)` on
     failure.
  10. Persist `manual_frozen[cat]=True` (+ `manual_frozen_at[cat]=now`)
      via `load_budgets`/`save_budgets`; return `(True, 'Frozen …')`.
  11. Notify? Recommendation: NO notify-send on manual actions (the parent
      clicked; the child sees the freeze). Keep notifications for
      auto-exhaustion only.
- `budgets.unfreeze_category(username, category)` (NEW): `set_frozen(False)`
  → pop `paused` + `manual_frozen`/`manual_frozen_at` → set `resumed=True`
  → `(True, 'Resumed; will not re-pause until the next reset')` (keep the
  exact Resume message so existing UI copy/tests still match).
- No `pid`/`comm` fields: category-granular by design (freezer is a
  category cgroup; PID granularity is impossible — §4).

### Confirm dialog
**No confirm** on Freeze or Unfreeze. Justification: matches the two
closest conventions — one-click bonus grants (deliberate, reversible,
no confirm, `user.html:797`) and Hide/Show (reversible, no confirm);
unlike Lock (auth-blocking) / Emergency-logout (session-killing) /
Revoke (re-locks) it has no destructive side effect, and Unfreeze is a
one-click undo. Also avoids confirm-fatigue on a toggle used repeatedly.

### Live-refresh wiring
- `renderBudgets` gains the toggle branch (frozen → Unfreeze button w/
  `onclick="unfreezeCategory('user','cat')"`; else Freeze button w/
  `onclick="freezeCategory('user','cat')"`). Buttons call `fetch` POST
  with `X-CSRF-Token`, toast, `refreshUserLive()` — copy `resumeCategory`
  (`user.html:925`) verbatim, two functions `freezeCategory` /
  `unfreezeCategory`.
- `api_user_live` `budget` payload gains `manual_frozen` + `frozen` keys
  (via `budget_status` change — no handler edit needed beyond what
  `budget_status` returns).
- Preserve the `data-comm`-style escaping discipline: category values
  come from the server's `CATEGORIES` constant (safe alphabet), interpolated
  into `onclick` exactly as `resumeCategory` does today.

---

## 4. Processes table: per-ROW vs per-GROUP — DECISION: per-GROUP

### Decision
**One Freeze/Unfreeze toggle per visible process-group row, acting on the
group's category. No per-PID buttons. No buttons on mixed-category or
hidden groups (see below). No new endpoints — group buttons POST to the
same `/api/budgets/freeze|unfreeze`.**

### Reasons
1. **The freezer has no PID granularity.** Freezing is `cgroup.freeze=1`
   on `parental-<user>-<category>`; every member stops. A per-PID "freeze"
   button would have to migrate that PID into a category cgroup and freeze
   the whole cgroup — freezing its neighbours too while claiming to freeze
   one app. Dishonest UI.
2. **Unfreezing one member of a frozen category is impossible.** Thawing is
   also cgroup-wide; "unfreeze PID x" would silently unfreeze the entire
   category. A per-row Unfreeze would lie about its blast radius.
3. **Category routing for a single PID is ambiguous.** A PID's category
   comes from `categorize(proc, index)` (overrides > .desktop > `other`).
   Migrating an arbitrary PID into a cgroup whose other members were
   placed by the tick splits management: the tick's `freezable_members`
   may not include it next tick (e.g. hidden comms), leaving strays the
   watchdog then flags.
4. **Group → category mapping already exists and is visible.**
   `get_user_live` buckets visible procs by category BEFORE `group_by_comm`
   (`app.py:1507`), so every visible group row belongs to exactly one
   category (the section header). The button freezes what the header says.

### Exact behavior
- Visible group row (`group-row`, `user.html:304` + `groupRowsHtml`,
  `user.html:431`): append a Freeze/Unfreeze toggle in the action cell
  next to Hide, reflecting `LIVE_DATA.budget[cat].frozen` for the row's
  section category. `onclick="freezeCategory('user','cat')"` /
  `unfreezeCategory(...)` — identical handlers to §3.
- **Uniform-category invariant**: visible groups are always single-category
  by construction (bucketed pre-grouping). No mixed case to handle in the
  visible table. State this in a code comment at the render site.
- **Hidden table** (`user.html:342`, `groupRowsHtml('hidden', …)`):
  NO freeze buttons. Reasons: (a) groups may span categories
  (`uniform_category` False, "Mixed" label) — one button cannot name its
  target; (b) hidden rows include NEVER_PAUSE plumbing the gates would
  refuse anyway, producing confusing "Freeze failed" toasts; (c) hidden
  processes are still budget-managed (filter ≠ unmanaged,
  `budgets.py:290`), so their categories are freezable from the Budgets
  table. Per-member `category` labels stay as-is for information.
- **Detail (expanded) member rows** (`groupDetailHtml`, `user.html:411`):
  NO per-member buttons (reasons 1–3). Member rows keep PID/Command/Used/
  Category display only (Used shows the group's budget usage, not ps ETIME).
- **NEVER_PAUSE handling per member**: unchanged — enforced inside
  `freeze_category` via `freezable_members` (match) +
  `verify_freeze_safety` (freeze). A group containing a NEVER_PAUSE member
  fails the whole freeze fail-closed with the gate's reason surfaced in
  the toast (e.g. `PID 1234 (bash) is NEVER-pause`). The UI does not
  pre-filter — server is authoritative (never trust the client to enforce
  safety).
- **Cross-category groups**: cannot occur in the visible table by
  construction; hidden table gets no buttons. If a future refactor merges
  the tables, the rule is: button target = the row's section category; a
  row without exactly one category gets no button (disabled with tooltip
  "Mixed category — freeze from the Budgets table").
- **No-limit categories**: group Freeze button hidden/disabled when
  `budget[cat].minutes` is falsy (same gate as §3; server re-validates).

### Live payload changes (minimal)
- Option A (recommended): NO payload change — `renderProcesses` already
  receives `data.budget` in the same `/live` response; `groupRowsHtml`
  needs the row's category (already a parameter) + budget frozen state
  (read from a module-level `LIVE_BUDGET` set in `refreshUserLive` before
  `renderProcesses`). Zero server diff.
- Option B: add `frozen` per group server-side. Rejected — duplicates
  budget state, risks skew between the two copies.

---

## 5. Safety section: invariants + pinning tests

| # | Invariant | Enforced in | Pinned by test |
|---|---|---|---|
| S1 | NEVER_PAUSE comms are never frozen (match time) | `freezable_members` skips `is_never_pause` (`budgets.py:1282`) | U3 (existing pattern; add manual-freeze variant F-U3) |
| S2 | NEVER_PAUSE / foreign-owned PIDs abort a freeze (freeze time) | `verify_freeze_safety` in `freeze_category` step 7 + tick rule-6 branch | F-U4, F-U5 |
| S3 | Only `parental-<user>-<category>` cgroups touched; foreign `parental-*` never frozen/unfrozen by this feature | `cgroup_path` validation; no new path construction; watchdog `unknown` untouched | F-U6, F-E10 |
| S4 | Wheel/admin users can never be frozen (auto OR manual) | `is_admin_protected` gate in `freeze_category` + `_wheel_target_refused` in handlers | F-E4, F-E5 |
| S5 | Invalid username/category never reaches sudo (path traversal / arg injection) | `valid_username` + `valid_category` in handler AND `freeze/unfreeze_category` AND `cgroup_path` ValueError | F-E2, F-E3, F-U1 |
| S6 | Fail-closed: cgroup/verify failure → no freeze recorded, no `manual_frozen` persisted | order: verify BEFORE `set_frozen` BEFORE persist (§3 steps 7→9→10) | F-U7 |
| S7 | Thaw path fail-open, freeze path fail-closed | `set_frozen` existing contract (`budgets.py:966`) reused unchanged | existing tests (no new) |
| S8 | Day reset clears manual freezes (no permanent freeze) | reset branch clears `manual_frozen` + `unfreeze_all_user` | F-U8 |
| S9 | Watchdog never thaws a deliberate manual freeze | legitimacy `paused OR manual_frozen` | F-U9 |
| S10 | Clearing a limit releases manual freeze | `set_limit` pops `manual_frozen` + `set_frozen(False)` | F-U10 |
| S11 | No new sudo grants; only `tee/cat/mkdir` on `parental-*` paths | code uses existing helpers only | F-D1 (sudoers template unchanged), §7 |
| S12 | CSRF + admin auth on all new endpoints | `@require_admin` | F-E6, F-E7 |
| S13 | Manual freeze never locks accounts / signals / kills | feature touches only cgroup freezer writes | F-E11 (no `usermod`/`loginctl`/signal calls) |

---

## 6. Test list (20 tests)

Conventions: unit tests in `tests/test_budgets.py` style (stdlib-only,
mock `subprocess.run`, `account_and_enforce(..., act=False)` or mocked
`set_frozen`/`ensure_cgroup`/`cgroup_members`/`list_user_processes`);
endpoint tests in `tests/test_lock_regression.py` style
(`_AdminSessionMixin` + `authed_post`, mocked `budgets.*`, mocked
`subprocess.run` so no real cgroup/sudo). Template tests: Flask test
client GET `/user/<u>` asserting button presence/absence + a
`/api/user/<u>/live` JSON shape test. No test touches
tester/bluefox/stan/pcagent, real cgroups, or sudo.

### Unit — budgets.py (10)
- F-U1 `freeze_category` rejects bad username/category without side
  effects (no subprocess calls; `(False, …)`).
- F-U2 `freeze_category` requires a configured limit (`limits[cat]<=0` →
  `(False, 'Set a time limit…')`, no cgroup writes).
- F-U3 NEVER_PAUSE members excluded at match time (steam + bash procs →
  bash not in migrated set; freeze proceeds for the freezable subset).
- F-U4 `verify_freeze_safety` aborts manual freeze when a cgroup member is
  not user-owned or not in the freezable set (mock `os.stat` uid +
  `cgroup_members` with a stray PID → `(False, …)`, `set_frozen(True)`
  never called).
- F-U5 NEVER_PAUSE PID present in cgroup aborts freeze with NEVER-pause
  reason.
- F-U6 empty category (no members, empty cgroup) records `manual_frozen`
  WITHOUT calling `set_frozen(True)` (late-start path covers it).
- F-U7 `set_frozen` failure → `(False, …)` and `manual_frozen` NOT
  persisted (read budgets.json back).
- F-U8 reset branch clears `manual_frozen`/`manual_frozen_at` and calls
  `unfreeze_all_user` (drive via `account_and_enforce` with
  `budget_reset_needed` True, `act=False` + mocked unfreeze).
- F-U9 watchdog treats `manual_frozen` (no `paused`) as legit (build fake
  root with frozen cgroup + budgets_data carrying only `manual_frozen` →
  `thawed == []`, `legit == [name]`).
- F-U10 `set_limit` clear pops `manual_frozen` and thaws (mocked
  `set_frozen`; assert called with False).

### Endpoint — app.py Flask client (7)
- F-E1 freeze/unfreeze happy path: mocked `budgets.freeze_category` →
  200 `{success: True}`; assert called with `(user, cat)`; toast path is
  client-side (no test).
- F-E2 invalid username (`-x`, `a/b`, empty) → 400 on both endpoints,
  delegate never called.
- F-E3 unknown category → 400, delegate never called.
- F-E4 wheel target → 403 (mock `auth.is_wheel_member` True).
- F-E5 admin-protected target → 403 even when `is_wheel_member` False but
  `budgets.is_admin_protected` True.
- F-E6 unauthenticated POST → 401 JSON (no session cookie).
- F-E7 missing/invalid CSRF → 403.
- (fold into F-E1 file: resume-alias test — `POST /api/budgets/resume`
  on a manually-frozen category clears `manual_frozen` and sets `resumed`;
  mock `budgets.unfreeze_category` or drive real function with mocked
  `set_frozen` + temp budgets file.)

### Template / live payload (2)
- F-T1 `GET /user/<u>` renders Freeze buttons for limited categories and
  Unfreeze for frozen ones; no Freeze button when no limit set (mock
  `get_user_live` or temp budgets + mocked ps helpers; assert on
  `budget-status-<cat>` HTML).
- F-T2 `/api/user/<u>/live` `budget[cat]` carries `manual_frozen` +
  `frozen` keys; groups payload unchanged (Option A — assert no `frozen`
  per group).

### Safety / deployment (2)
- F-D1 sudoers templates byte-identical (`sudoers-parental-controls-budgets`
  grants already cover `freeze/procs` tee — assert no new helper performs
  privileged writes except via `_sudo_write_file`/`ensure_cgroup`).
- F-D2 full-suite impact: run the whole `tests/` suite once from the
  worktree (approved TMPDIR pattern); all pre-existing tests pass
  unmodified (especially `test_budgets.py`, `test_watchdog.py`,
  `test_lock_regression.py`).

Total: **21 tests** (10 unit + 7 endpoint + 2 template + 2 safety/deploy).

---

## 7. Rollout

- **install.sh / sudoers impact: NONE.** Manual freeze reuses
  `ensure_cgroup` (`mkdir`), `migrate_pid` (`tee cgroup.procs`),
  `set_frozen` (`tee cgroup.freeze`), `cgroup_members`/`read_freeze`
  (`cat`) — all covered by `sudoers-parental-controls-budgets:10-14`.
  No template edit, no re-render, no `visudo` action. (F-D1 pins this:
  if the implementer adds any privileged operation outside these five
  helpers, stop and re-plan.)
- **Deploy**: merge `feature/freeze-buttons` → `master`; `sudo
  ./install.sh` from `master` (stages code to `/opt/parental-controls`,
  preserves `budgets.json`, restarts service); smoke-test:
  1. `curl localhost:5000/` → login redirect (service alive).
  2. Log in, open a user page, set a small limit on an idle category →
     Freeze button visible; press Freeze → badge `Frozen — by parent`,
     Unfreeze appears; press Unfreeze → `OK`, `resumed` set.
  3. `sudo cat /sys/fs/cgroup/parental-<user>-<cat>/cgroup.freeze` reads
     `1` while frozen, `0` after.
  4. `sudo parental-controls unfreeze-all` thaws (recovery path intact);
     next tick re-asserts manual freezes only.
- **Rollback**: revert commit / redeploy `master`; manually-frozen
  categories thaw via `unfreeze-all` or day reset; stale `manual_frozen`
  keys are ignored by old code (unknown keys are inert in
  `budget_status`/tick — additive-key discipline) and cleared at next
  reset after re-upgrade. No migration, no data loss (usage/limits
  untouched by this feature).
- **Never during rollout**: touch tester/bluefox/stan/pcagent accounts;
  restart `parental-controls.service` by hand (installer owns it); run
  `install.sh` from the worktree.

---

## 8. Open questions for the owner (with recommendations)

1. **Should Freeze require a budget limit to be set first?**
   Recommended: YES (gate with "Set a limit first" tooltip). The tick's
   match gate (`freezable_members`) requires a limit; un-limited manual
   freezes would create freezer state the tick never manages or resets
   cleanly. Alternative (allow limit-less freeze) needs a tick bypass and
   more tests — not worth it.
2. **One toggle button vs separate Freeze/Unfreeze buttons?**
   Recommended: SINGLE toggle (shows the action available now). Follows
   the Lock/Unlock precedent (one visible at a time, `user.html:236`).
   Halves the render logic and can't show a stale pair after refresh.
3. **Should the UI read live cgroup truth (`read_freeze`) or JSON state?**
   Recommended: JSON (`budget_status.frozen`). Live reads cost 5 sudo
   forks per poll per viewer and spam the journal; drift self-heals
   within one 60s tick (§2.9). Revisit only if drift reports appear.
4. **Should manual freeze send a desktop notification to the child?**
   Recommended: NO. Auto-exhaustion notifies (child must learn time is
   up); a parent clicking Freeze is already at the screen and can tell
   the child. Avoids notify-send sudo traffic on a deliberate action.
5. **Should wheel/admin targets refuse manual freeze (like Lock does)?**
   Recommended: YES, 403 (`_wheel_target_refused` + `is_admin_protected`
   belt-and-braces). The tick can never auto-freeze admins; letting the
   UI do it would let a parent freeze their own desktop session with one
   click. CLI `unfreeze-all` stays the recovery path.
6. **Should process-group Freeze buttons also appear in the hidden table?**
   Recommended: NO (mixed categories + NEVER_PAUSE plumbing → confusing
   failures; Budgets table covers those categories). If you want them,
   say so — cost is +2 tests and a mixed-group disabled-state rule.
7. **Should Unfreeze on an auto-paused category keep today's "Resume …
   will not re-pause until reset" inhibit?** Recommended: YES (Unfreeze
   subsumes Resume; one click → running until reset). The alternative
   (unfreeze now, re-freeze next tick while still exhausted) makes the
   button look broken.
