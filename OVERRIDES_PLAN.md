# Per-App Overrides & Weekly Budget Windows — Research & Implementation Plan

Feature: give parents (1) a per-app (per-`comm`) allowance or unlimited exemption
for a single user, and (2) recurring weekly day/time windows that replace the
allowance for a category or an app while they are active. Deferred from
PLAN.md Phase 1 budgets ("Per-category budgets first; apps displayed grouped by
category; per-app overrides deferred to a later phase"; tracked as PLAN.md Phase 4).

Status: **design only.** No code, no tests, no template edits, no live
accounts/services touched. Implemented on `feature/overrides` (worktree
`overrides/`), merged per the rollout plan below.

---

## 1. Current-state inventory

### 1.1 Per-comm visibility prefs (`process_prefs`) vs per-category limits

- **Visibility (hide/show) is per-comm and controls the UI only.** `budgets.py`
  `load_prefs/save_prefs/set_pref` + `apply_filter` shape
  `{username: {"hidden_comm": [...], "shown_comm": [...]}}`; effective hidden =
  `(default_hidden | auto_hidden | hidden_comm) − shown_comm`. The module docstring
  is explicit: *"Hidden never means unmanaged: budgets/freezer matching is
  untouched."* There is **no allowance concept** in `process_prefs`.
- **Limits are per-category, per-user.** Budget store (`load_budgets/save_budgets`,
  table `budgets`) holds
  `{username: {limits: {cat: minutes}, day, used_seconds: {cat: float},
  paused/resumed/manual_frozen/manual_frozen_at/warned_low/warned_paused:
  {cat: ...}, was_in_bedtime, last_cpu, last_tick}}`. One `used_seconds` bucket per
  category; **no per-comm accounting anywhere.**
- **comm → category mapping** (`categorize(proc, index)`): curated `CATEGORY_OVERRIDES`
  regex whole-word match on `comm + cmdline` (Steam chain, browsers, media, chat) >
  `.desktop Categories` index > fallback `'other'`. One comm can legally appear in
  multiple categories (`group_by_comm` returns `categories[]`); enforcement keys on
  comms in `freezable_members` + `categorize`.

### 1.2 The 60s tick (`account_and_enforce`, `tick_budgets`)

1. Reset check once per user via `budget_reset_needed` (bedtime-end transition for
   bedtime users, calendar-day change for no-bedtime users); on reset: `used_seconds`,
   `paused`, `resumed`, `manual_frozen*`, `warned_*` cleared and `unfreeze_all_user`.
2. `compute_deltas` per-PID CPU deltas; wall-clock `wall` capped at
   `MAX_TICK_WALL_SECONDS` (300s).
3. Per category with `limits[cat] > 0`: `members = freezable_members(...)` (visible +
   categorized into cat + not `NEVER_PAUSE` + limit configured). If **any** member was
   active (`delta >= ACTIVE_THRESHOLD_SECONDS = 1.0`), the whole `wall` is charged to
   `used_seconds[cat]`; exhaustion → migrate members into the category cgroup, verify
   freeze safety, freeze. Manual-freeze branch re-asserts the freeze for late starters.
4. `tick_budgets` loads prefs once, passes `prefs`/`index` down; the app thread
   (`app.py bedtime_loop:913-928`) calls it without `index`, so live enforcement
   classifies via `CATEGORY_OVERRIDES` + fallback only.

**Where a per-app allowance must hook in:** the billing decision
(`category_active` / `used[category] += wall` at budgets.py:1450-1456), the
freezable-member gate (`freezable_members:1324-1338`), the freeze/migrate block
(1451-1556), the reset block (1383-1398), and the status builder
(`budget_status:876-903`). All live in `budgets.py`; the app layer only surfaces
state.

### 1.3 Cgroup bucket model + watchdog

- One freezer group per user/category: `parental-<username>-<category>` (`cgroup_path`),
  created via `ensure_cgroup`, PID pre-placement via `migrate_pid`, freeze via
  `set_frozen` (write `cgroup.freeze`, verify by readback), safety gate
  `verify_freeze_safety` (all members owned by the user, none on `NEVER_PAUSE`).
- `parse_managed_cgroup` splits on the **last** hyphen; anything failing
  `valid_username` + `valid_category` is treated as a **foreign group**: logged,
  reported, **never touched**. Consequence: `parental-<user>-app-<comm>` groups would
  be classified foreign today, and `unfreeze_all_user` (which filters suffixes by
  `valid_category`) would skip them too. **Both must be extended** for per-app cgroups.
- Sudoers (`sudoers-parental-controls-budgets`) is regex-anchored on
  `parental-[A-Za-z0-9_.-]+/...` — **any** group whose full name matches that charset
  is already grantable. As long as `valid_comm` reuses the same charset
  (`[A-Za-z0-9_.-]`, no leading `-`), **no sudoers change is needed.**

### 1.4 Temporal model to mirror

- Bedtime entry: `{start, end, days:[0-6], ...}` with `_in_bedtime` overnight-wrap
  logic and `_parse_hhmm`. This is the exact model weekly windows should reuse
  (recurring day-of-week + HH:MM range).
- Bonus time (`BONUS_TIME_PLAN.md`) is the precedent for adding a bounded allowance
  *inside an existing per-user doc*: optional keys, fail-closed parsing
  (corrupt → treated as absent, never suppresses enforcement), enforcement in the
  existing 60s tick, history cap, POST+CSRF endpoints, live payload extension.
- Store (`store.py`): one SQLite table per store, `username TEXT PRIMARY KEY,
  doc TEXT`; `SCHEMA_VERSION = 1`, ordered `MIGRATIONS`, backup-before-migrate,
  one-time legacy-JSON import, JSON rollback mirrors; `load_table/save_table`.

---

## 2. Design

### 2.1 Data model

Two complementary pieces, deliberately split by write-frequency and reset-lockstep:

**A. Config — new `overrides` store (SQLite table, schema v2).** Parent edits only,
rarely written, needs independent read paths and cheap presence checks. Per-user
document:

```json
{
  "app_overrides": {
    "<comm>": {
      "mode": "limit" | "exempt",
      "minutes": 120,
      "set_by": "stan",
      "set_at": "2026-10-11T12:00:00"
    }
  },
  "windows": {
    "<window_id>": {
      "target_type": "category" | "app",
      "target": "games" | "firefox",
      "days": [0, 1, 2, 3, 4, 5, 6],
      "start": "14:00",
      "end": "17:00",
      "allowance_minutes": 60,
      "enabled": true,
      "set_by": "stan",
      "set_at": "2026-10-11T12:00:00"
    }
  }
}
```

- `window_id` = `over-<n>` counter per user (or `<target_type>-<target>`, V1: one
  window per target → id = `"w-" + target_type + "-" + target` is enough and
  collision-proof by validation).
- `mode=exempt` means *unlimited, never bill, never freeze*. `mode=limit` carries the
  allowance. Missing `minutes` with `mode=limit`, unknown mode, unknown category,
  unparseable window fields → **fail closed**: that override/window is ignored
  entirely (the category rules govern), never crashes the tick.

**B. Runtime — new keys in the existing per-user budgets doc.** Mutated every tick
and reset in lockstep with the daily reset; kept in the same doc so one
load/save and one reset path apply (exactly the bonus-time reasoning, applied to
`used_seconds`'s sibling):

```json
{  // additions inside the existing budget entry
  "app_used":        {"<comm>": 0.0},
  "app_paused":      {"<comm>": false},
  "app_resumed":     {"<comm>": false},
  "app_manual_frozen": {"<comm>": false},
  "app_manual_frozen_at": {"<comm>": "iso"},
  "app_warned_low":  {"<comm>": "YYYY-MM-DD"},
  "app_warned_paused": {"<comm>": "YYYY-MM-DD"},
  "window_used":     {"<window_id>": 0.0},
  "window_active":   {"<window_id>": false}
}
```

**"Piggyback or carve?"** The allowance is a **carved new per-app value**
(`app_used`/`app_used` budget), not a multiplier of the category budget. But the
*state storage* piggybacks on the existing budget doc (runtime) + a new config
table (config) — best of both: no shape change to legacy parsers, one reset path.

**SQLite layout / migration.** `store.py`:
`SCHEMA_VERSION = 2`; `TABLES = ('bedtime', 'budgets', 'process_prefs',
'overrides')`; `LEGACY_FILES['overrides'] = 'overrides.json'`;
`MIGRATIONS += [(2, 'per-app overrides + weekly budget windows config table',
_migrate_2)]` where `_migrate_2` creates the `overrides` table
(`CREATE TABLE IF NOT EXISTS overrides (username TEXT PRIMARY KEY, doc TEXT NOT
NULL)`) — identical shape to the existing stores. `overrides.json` joins the
rollback-mirror set and must be added to `.gitignore` (the current file lists the
other three mirrors individually; a new unignored mirror file would show up as
untracked).

**Migration story (old configs keep working):**

- Old state (v1 db, existing `budgets` docs without the new keys): new code's
  `load_table('overrides')` returns `{}` on a missing table; runtime keys are all
  absent → `setdefault`d to empty dicts exactly like the existing per-category keys.
  Zero behavioral change; all 520+ existing tests must stay green untouched.
- Rollback: the v2 table is an ordinary SQLite table; old binaries never read it and
  **never touch its data**. See §4.2 for the one real rollback hazard (per-app
  cgroups left frozen by new code).

### 2.2 Semantics

**Effective allowance per target (category or app), evaluated each tick:**

| # | Configuration present | Effective allowance | Usage counter | Resets when |
|---|---|---|---|---|
| 1 | App `exempt` | unlimited — no billing, never in any cgroup | — | — |
| 2 | App override `limit` + **no** window on that app | `app_overrides.<comm>.minutes` | `app_used[comm]` | daily reset (same rule as categories) |
| 3 | App override `limit` + **active** window on that app | window `allowance_minutes` | `window_used[id]` | window close (auto-resume) |
| 4 | No app override, category has **active** window | window `allowance_minutes` | `window_used[id]` | window close |
| 5 | No app override, no active window, category daily limit | `limits[cat]` | `used_seconds[cat]` | daily reset (today's rule) |
| 6 | No app override, no window, no daily limit | unmanaged (today's behavior) | — | — |

**Precedence (parent's intent, most specific wins):**

> App exemption > app allowance (with its own window or daily) > category window >
> category daily limit. Within a target, an **active** window replaces the daily
> allowance entirely (limit *and* reset timing); when no window is active the daily
> allowance governs. (Every app with an override is also excluded from its parent
> category's `members`, so a category freeze/window never bills or freezes it.)

**Window lifecycle (recurring day windows):**

- **Open/close transitions** are detected in the tick from `now` against
  `days/start/end` reusing `_in_bedtime`-style overnight-wrap logic (new
  `_in_window(window, now)` in `budgets.py`, stdlib-only like `_in_bedtime`).
- **Open** (`window_active[id]` False→True): `window_used[id] = 0`; any freeze
  previously recorded for that target is cleared and its cgroup thawed (fresh
  allowance).
- **Close** (True→False): `window_used[id] = 0` again; thaw the target's cgroup if
  the window exhausted it — **auto-resume**, matching the approved decision "Paused
  apps auto-resume at the next window". Daily governance resumes immediately using
  its own (untouched) counters.
- **Window spans midnight** (e.g. Sat+Sun entire weekend): this is the whole point —
  usage accumulates across calendar days; the **daily reset does NOT touch
  `window_used` for currently-active windows** and does NOT thaw window-governed
  freeses (`unfreeze_all_user` skip-list). Interaction with bedtime: windows and
  bedtime are orthogonal; bedtime locks the *account*, budgets freeze the *cgroup*.
  A window may be active while the account is bedtime-locked (locked ticks bill
  nothing — existing `account_locked` path already prevents billing).

**App override × the cgroup bucket (the key structural change):**

- A PID can live in exactly one cgroup (v2). Today all freezable members are
  pre-placed into the category group. With overrides:
  - **Exempt** app → never migrated into any `parental-*` group.
  - **Allowance** app → pre-placed into a new per-app group
    `parental-<user>-app-<comm>` instead of its category group; on exhaustion of its
    allowance, that group is frozen only (siblings in the category keep running —
    the reason per-app groups are required; freezing the whole category would freeze
    unrelated apps and would risk an exempt app if one were ever left in it).
  - **Category-governed** apps (no override) → unchanged category group behavior.
- New cgroup ops are thin wrappers over the existing privileged helpers
  (`ensure_cgroup`, `migrate_pid`, `set_frozen`, `cgroup_members`,
  `verify_freeze_safety`) — no new privilege grants (sudoers charset already covers
  the name).
- **Watchdog + unfreeze extensions (required, safety):**
  - `parse_managed_cgroup` must recognize `parental-<user>-app-<comm>` (try the
    category parse first, then `-app-` suffix parse with `valid_comm`) so per-app
    groups are legitimate-thawable, never "foreign/unknown".
  - `watchdog_tick` legitimacy check must also consult `app_paused` /
    `app_manual_frozen` for app groups.
  - `unfreeze_all_user` must thaw `parental-<user>-app-<comm>` groups at daily
    reset (auto-resume), skipping targets with an active-window freeze.
- The `NEVER_PAUSE` gate applies identically at match time and freeze time
  (`verify_freeze_safety` is per-group and already handles "member not in the
  freezable set" and "member is NEVER-pause" → abort freeze, log note, fail closed).

### 2.3 Endpoints + UI

All POST endpoints: form-encoded, `@require_admin`, `X-CSRF-Token` (exact
convention of `/api/budgets`, `/api/process-prefs`); `valid_username`, new
`budgets.valid_comm` (`^[A-Za-z0-9_.-]+$`, no leading `-`, len 1–15) and existing
`valid_category`; wheel targets refused with the `_wheel_target_refused`-style 403
(applies to `tester`/`bluefox`/`stan` refusal too). Every mutation calls
`audit.safe_log` (`budget.override_set/override_cleared/window_set/window_cleared`).

| Endpoint | Fields | Behavior |
|---|---|---|
| `POST /api/budgets/overrides` | `username, comm, mode` (`limit`\|`exempt`), `minutes` (mode=limit) | Set/clear a per-app override; `mode=absent` or `minutes=0`/`mode=none` clears (and releases any app freeze + thaws its group, mirroring `set_limit`). |
| `POST /api/budgets/windows` | `username, target_type` (`category`\|`app`), `target`, `days` (csv 0-6), `start`, `end` (HH:MM), `allowance_minutes`, `enabled` (`1`/`0`) | Set/update a window for the target (V1: one window per target — reject with 400 on collision). `enabled=0` disables (window state reset, freeze released). |
| `DELETE`-equivalent | same route with `action=clear` | `window_clear` → removes the window + resets its runtime + thaws. |

Validation rules: `days` non-empty subset of 0–6; `start`/`end` parse via
`_parse_hhmm`, `start != end`; `allowance_minutes` integer ≥ `WINDOW_MIN_MINUTES`
(**recommend 5**, matching `BONUS_CUSTOM_MIN`); unknown `target` for `category`
rejected; `comm` target must pass `valid_comm`; malformed values → 400 **and no
state write** (match the bonus/`set_limit` contract). App override `minutes` must be
a positive integer when `mode=limit` (no cap; exemptions are the unbounded case).

Reads: extend `GET /api/user/<u>/live` and the `user_detail` context with
`overrides: {app_overrides: {...}, windows: [...]}` and extend `budget` with
`app_budgets: {comm: {mode, minutes, used_minutes, remaining_minutes, exempt,
paused, frozen, window_active}}` plus per-category `window_active`/`effective_source`
(`window|daily|none`) so the UI can badge windows. Keep the existing `budget_status`
keys byte-identical (regression guard: old JS keeps working; new fields additive).

UI (`templates/user.html`):

- **Budgets table rows** gain a `window_active` badge and, under the table, a new
  **"Weekly windows" subsection**: one row per configured window (target, days,
  HH:MM range, allowance, enabled toggle, Delete) + an add form (target type +
  category/app select, Mon–Sun checkboxes reusing `DAY_LABELS`, 12h start/end
  selects reusing the bedtime compose, allowance input). Save → `showToast` +
  `refreshUserLive()`.
- **Process group rows** (visible per-comm rows already carry Hide + Freeze buttons)
  gain an **Override** control: a small select/input (`Limit min/day`, `Unlimited`,
  `Clear`) posting to `/api/budgets/overrides`. Since apps are only listed while
  running, the Weekly-windows subsection also gets an "Add app override" row with a
  `<datalist>` of installed-app comms (from `build_desktop_names`) + free-text
  fallback, so parents can override a not-currently-running app.
- Extend `renderBudgets` (user.html:720) to update per-app rows + window badges from
  the live payload; refresh input fields are never touched by the 10s poll
  (existing forms-never-touched rule); new controls are static (server-rendered)
  like the budget inputs.

### 2.4 Safety

- **Admin/wheel protection:** `is_admin_protected` already short-circuits the whole
  tick for wheel members, and every endpoint refuses wheel targets — override/window
  writes to a wheel admin are 403s (no state write), exactly like bonus.
- **NEVER_PAUSE** still checked at match time (`freezable_members`/new
  `freezable_app` gate) and freeze time (`verify_freeze_safety`), for app groups too.
- **Fail-closed parsing:** corrupt/unknown fields in the `overrides` doc or the new
  budget-doc keys → the override is treated as absent (category rules govern);
  unreadable store → `load_table` returns `{}` (existing contract). A malformed
  override must never accidentally *exempt* an app.
- **Path safety:** `valid_comm` before any cgroup path/argv construction (leading
  `-` rejected, charset pinned — mirrors `valid_username`).
- **Reset behavior** (no orphan freeses): daily reset thaws all app groups except
  targets with an active window (handled at window close); window close always
  thaws its target; a category `set_limit` clear already releases category freeses
  (`set_limit:849-865`) — app override clear must do the same for app groups.
- **Watchdog** keeps its core invariant (only thaw our own groups, only when
  desired+effective frozen with no paused/manual flag, foreign groups report-only)
  — it just gains app-group awareness (§2.2).

---

## 3. Named test list (target ~24, new `tests/test_overrides.py` + store/template additions)

Stdlib `unittest`; budgets tests reuse the temp-`BUDGETS_FILE`/`_BedtimeFileMixin`
temp-dir harness (SQLite db lives next to the redirected file — already proven in
`test_lock_regression`/`test_bonus_time`); endpoint tests reuse
`_AdminSessionMixin` + Flask test client with subprocesses mocked. No real
accounts, no sudo, no service restarts.

**Unit — precedence & lifecycle (budgets.py):**
1. `test_precedence_exempt_over_app_allowance` — exempt comm never bills even with
   an allowance configured and active processes (no `used` delta, no freeze).
2. `test_precedence_app_allowance_over_category_window` — app with daily allowance
   inside a category window bills its own counter, not `window_used`.
3. `test_precedence_category_window_over_daily_limit` — no overrides, active window
   → window allowance governs; `used_seconds[cat]` untouched.
4. `test_precedence_daily_limit_when_no_window` — baseline unchanged (regression).
5. `test_window_open_resets_and_clean` — close→open transition zeroes `window_used`
   and clears the target freeze.
6. `test_window_close_auto_resumes_and_thaws` — exhausted window closes → thaw called
   for that target (dry-run summary records it), `window_used` reset.
7. `test_window_spans_midnight_accumulates` — weekend window (Sat+Sun 00:00–23:59)
   bills across calendar days; daily reset does not zero `window_used` while active.
8. `test_daily_reset_skips_active_window` — reset fires for the daily counters but
   leaves active-window counters + window-governed freeze intact.
9. `test_app_allowance_exhaust_freezes_app_group_only` — dry-run: sibling app in the
   same category keeps running; only the override comm freezes.
10. `test_app_exempt_never_placed_in_category_cgroup` — exempt PIDs absent from
    `members`/migrate calls (category freeze proceeds without them).
11. `test_override_corrupt_fails_closed` — garbage `mode`/missing `minutes`/negative
    allowance → treated as no override; category rules govern (no crash, no exempt).
12. `test_valid_comm_gate` — rejects `-x`, `.`-only, >15 chars, `/`, spaces.
13. `test_window_invalid_fields_fail_closed` — bad `days`/`start==end`/unparseable
    time → window ignored; tick identical to no-window.
14. `test_regression_no_overrides_no_behavior_change` — existing
    `account_and_enforce` matrix green with zero override config (run full suite).

**Endpoint — auth/validation (app.py test client):**
15. `test_api_auth_csrf_matrix` — unauthenticated 401, wrong CSRF 403 for both new
    endpoints.
16. `test_api_override_validation` — bad username/comm/mode, minutes<=0, wheel target
    → 400/403, **no state write**; valid set + clear round-trip persists.
17. `test_api_window_validation` — bad target_type/target/days/start/end/allowance;
    duplicate window for same target → 400; enable/disable/delete round-trip.
18. `test_api_window_collision_rejected` — second window for same target → 400.
19. `test_live_payload_overrides_and_app_budgets` — `/api/user/<u>/live` shape:
    `overrides`, `budget.app_budgets`, per-category `window_active`,
    `effective_source` present and stable across polls.
20. `test_audit_entries` — `budget.override_set/cleared/window_set/cleared` logged
    with actor.

**Template smoke / UI:**
21. `test_user_page_renders_with_overrides` — user.html renders with no overrides
    (regression) and with an app override + window (rows/badges/controls present,
    no JS exceptions; `X-Test-Client` context).
22. `test_render_budgets_preserves_legacy_cells` — `renderBudgets`-generated HTML for
    a category without overrides matches the pre-feature markup (guard the 10s poll).

**SQLite round-trip / migration (store.py):**
23. `test_schema_v2_migration_preserves_v1` — build a v1 `state.db` (3 tables,
    populated docs) → `open_db` → v2: `overrides` table exists, all v1 rows intact,
    backup `state.db.bak.1` written, `schema_version=2`.
24. `test_overrides_table_roundtrip_and_mirror` — `save_table/load_table('overrides')`
    round-trips config; `overrides.json` mirror written; corrupt override row skipped
    per-row (store's existing fail-closed skip), whole-store failure → `{}`.
25. `test_watchdog_app_group_aware` — `parse_managed_cgroup` parses
    `parental-kid-app-firefox`; frozen app group with no `app_paused`/`app_manual`
    flag is thawed (self-heal), with one is legit; foreign `parental-*` still
    report-only; `unfreeze_all_user` thaws app + category groups.

Target: full existing suite (520+) stays green via `.venv/bin/python -m unittest
discover -s tests` from inside the worktree.

---

## 4. Phased rollout + rollback

### 4.1 Phases (each independently mergeable; old configs keep working at every step)

- **Phase A — model + CRUD + UI (no enforcement change).** `store.py` v2 +
  `overrides` table + `overrides.json` mirror + `.gitignore`; `valid_comm`,
  `cgroup_path_app`, `load/save` helpers; `POST /api/budgets/overrides` +
  `POST /api/budgets/windows`; template sections + live payload fields; audits.
  Safe to deploy to the live service: config stores empty → zero behavior change.
- **Phase B — enforcement.** Tick changes (`account_and_enforce` window resolution,
  per-comm billing, app-cgroup freeze/thaw), `freezable_members` exclusion,
  watchdog + `parse_managed_cgroup` + `unfreeze_all_user` app-group awareness,
  reset-lockstep handling. This is where per-app cgroups start to appear; ship with
  the full named test list green, then `sudo ./install.sh` + smoke test
  (`curl localhost:5000/` → login redirect), and a `pcagent`-only spot check
  (never `tester`/`bluefox`/`stan`): set a 1-minute app override, let it exhaust,
  confirm the app group freezes and siblings in the category do not; clear the
  override, confirm thaw; leave `pcagent` unlocked and config-free.
- **Phase C — polish.** Child-facing notifications for app/window exhaustion
  (`notify` hook, same best-effort pattern), effective-allowance copy, README/API
  reference + RELEASE.md note.

### 4.2 Rollback

1. Redeploy the previous `master` via `./install.sh` (stages code, preserves
   `state.db` and mirrors).
2. Config is inert under old code: extra SQLite table and unknown budget-doc keys are
   never read; the UI simply lacks the new sections.
3. **The one real hazard:** per-app cgroups created by new code. Old
   `unfreeze_all_user` skips them (`valid_category` fails) and the old watchdog
   reports them as foreign and never thaws → a frozen app cgroup would stick.
   Mitigation (document in RELEASE.md): before rollback, clear overrides/windows in
   the UI (each clear thaws its group) or run the root CLI
   `parental-controls unfreeze-all`/extended `cli.py unfreeze-all`. Recommend a
   Phase-B addition: extend `cli.py unfreeze-all` to cover app groups (root
   escape hatch, consistent with the existing admin-recovery story).

---

## 5. Open questions for the owner (recommendation in bold)

1. **Exemption semantics:** "Unlimited" as a per-app mode — confirmed wanted, or only
   a bigger allowance? **Recommend: keep both `limit` and `exempt`; exempt is the
   simplest mental model and is orthogonal to the system `NEVER_PAUSE` safety list.**
2. **Per-app daily allowance reset rule:** same as categories (bedtime-end, midnight
   fallback) or fixed calendar midnight? **Recommend: same rule as categories** —
   one rule to understand, and locked ticks already bill nothing.
3. **Window allowance floor:** 1 min or 5 min? **Recommend 5 min**
   (`WINDOW_MIN_MINUTES = 5`), matching the bonus custom-entry floor.
4. **Multiple windows per target:** V1 one-window-per-target (reject collisions) or
   stacked/overlapping windows with union semantics? **Recommend V1: one window per
   target** — predictable precedence (table row 3/4), trivial migration if extended
   later.
5. **Control placement:** per-row Override control on running process groups + a
   datalist add-row for installed-but-not-running apps — acceptable, or should the
   app override live only in a dedicated section? **Recommend the hybrid (running
   apps are where the parent's attention is).**
6. **Window billing:** wall-clock like categories (recommend yes — consistent, and
   idle apps don't burn budget by the same `ACTIVE_THRESHOLD` rule)?
7. **Overlapping app + category windows:** app window wins for that app (recommend
   yes — already encoded in row 3 of the precedence table)?
8. **Notifications:** child-facing "your time for <App> is up" on app/window
   exhaustion, once per window/day (recommend yes, same best-effort `notify` hook)?
9. **Rollback drill:** OK to extend `cli.py unfreeze-all` to app groups so the
   rollback hazard in §4.2 has a one-command escape hatch (recommend yes)?