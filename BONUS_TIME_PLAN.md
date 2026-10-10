# Bonus Time — Research & Implementation Plan

One-click + custom parent-granted extra time past bedtime, with countdown,
re-lock on expiry, and an audit trail that feeds the planned Phase-3 audit log.

Status: **implemented on `feature/bonus-time` (worktree `bonus-time/`), not yet
merged or deployed.** Implementation follows the plan; see the diff for details.
Tests: `tests/test_bonus_time.py` (29 tests, part of the full 225-test suite;
all green).

## 0. What exists today (file/function references)

- `master/app.py:503-578` — `enforce_bedtime(now=None)`: runs every 60s in **two
  processes**: the `bedtime_loop()` thread (`app.py:581-594`) and the systemd
  timer (`systemd/parental-controls-enforce.timer`: `OnUnitActiveSec=1min`,
  runs `enforce_bedtime_timer.py` → same `enforce_bedtime()` import).
- Bedtime entry shape in `bedtime.json` (gitignored runtime state):
  `{start, end, days, locked_by_bedtime, manual_override, warned_for?}`.
  Example live entry: `bluefox` `19:00–10:00` all days.
- Ownership flags (`app.py:260-327`):
  - `note_manual_unlock()` — clears `locked_by_bedtime`; sets `manual_override`
    if currently in bedtime (unbounded "stay unlocked till window ends").
  - `note_manual_lock()` — clears **both** flags (ownership transfer; see
    `QA_LOCK_UNLOCK.md` root cause 1: a stale `locked_by_bedtime` let the timer
    `usermod -U` a deliberately locked account).
  - `enforce_bedtime()` re-reads ownership from disk immediately before any
    auto-unlock (`app.py:562-566`) — the cross-process snapshot rule.
- Bedtime save (`app.py:855-949`) preserves `locked_by_bedtime`, forces
  `manual_override=False`, and enforces immediately. Clear (`app.py:875-886`)
  deletes the entry and unlocks if `locked_by_bedtime`.
- Account lock = `sudo usermod -L` (+ `loginctl terminate-user` for bedtime,
  screen-lock only for `/api/lock`); unlock = `sudo usermod -U`
  (`run_lock_command`, `app.py:177-237`). No new privileged commands are needed.
- Budget freezer (`budgets.py`) is orthogonal: cgroup freeze per app category,
  driven by `tick_budgets()` on the same 60s cadence. Bonus time must not touch it.
- UI: `templates/user.html` — Bedtime card top, Account Status card, 10s
  `refreshUserLive()` targeted DOM refresh (forms never touched). Destructive
  convention: single `confirm()` for lock/unlock/clear, **double** `confirm()`
  for emergency logout (`user.html:444,464,484,505`).
- API convention: `POST` form fields, `@require_admin` (session + CSRF
  `X-CSRF-Token`), `valid_username()` (rejects leading `-`), JSON
  `{success, message/error}`. Live endpoint `GET /api/user/<u>/live`
  (`app.py:1148-1182`) already ships `manual_override`.
- Tests: stdlib `unittest`, fully mocked, `_AdminSessionMixin` /
  temp-bedtime-file harness in `tests/test_lock_regression.py:63+`.
  Live tests only via throwaway accounts, never `tester`/`bluefox`/`stan`.
- Phase-3 audit log is planned in `PLAN.md:65-70` (who did what to whom, when).

## 1. Recommended semantics

### 1.1 Storage: extend `bedtime.json`, do not add a file

Add optional keys to the existing per-user bedtime entry:

```json
"bonus_until": "2026-10-10T21:47:00",
"bonus_minutes": 30,
"bonus_granted_by": "stan",
"bonus_granted_at": "2026-10-10T21:17:00",
"bonus_history": [
  {"at": "...", "by": "stan", "minutes": 30,
   "until": "...", "kind": "grant|extend|revoke|expire|lock-cancelled"}
]
```

Justification:

1. **Atomicity with ownership flags.** Grant/expiry/lock must update
   `bonus_until` together with `locked_by_bedtime`/`manual_override` in one
   read-modify-write. A second file (`bonus.json`) doubles the existing
   no-file-locking race (`QA_LOCK_UNLOCK.md` risk #4) instead of reusing the
   one snapshot + disk re-read pattern that already works.
2. **Both enforcers already load it.** `enforce_bedtime()` and
   `tick_budgets(load_bedtime(), …)` read `bedtime.json` every pass — no new
   load path, no new file to preserve across `install.sh` staging.
3. **Backward compatible.** All keys optional; old code ignores unknown keys;
   missing `bonus_until`/`None` = no bonus. No migration.
4. **Fail closed on corruption.** Unparseable `bonus_until` → treated as *no
   bonus* (enforcement proceeds, never suppressed by corrupt state).

Cap `bonus_history` at the last 20 entries per user (drop oldest on append) so
the file cannot grow unbounded before the Phase-3 SQLite migration.

### 1.2 Core rule

> While `bonus_until` is set and `now < bonus_until`, bedtime enforcement
> **suppresses the bedtime lock** for that user. At the first tick where
> `now >= bonus_until`, the bonus is cleared and normal bedtime logic runs
> (re-lock if still in window, stay unlocked if not).

### 1.3 Interaction matrix

| Situation | Behavior |
|---|---|
| Grant **past bedtime** (bedtime-locked) | `usermod -U` immediately, clear `locked_by_bedtime`, set bonus fields (new `note_bonus_grant()` helper, mirroring `note_manual_unlock()` but writing bonus keys instead of `manual_override`). Account usable at once. |
| Grant **before/in-window** while already unlocked | Record only; takes effect when the window would otherwise lock. |
| Grant while `manual_override` is set | **Grant clears `manual_override`** and replaces the unbounded override with the bounded bonus, so expiry re-locks. Rationale: without this, an old override would silently keep the account unlocked after the bonus visibly "ended" — the exact class of surprise `QA_LOCK_UNLOCK.md` fixed. Document in UI ("replaces current override"). |
| **Manual lock during bonus** (`/api/lock`, `/api/lock-screen`, `/api/logout`) | Manual lock wins instantly. Extend `note_manual_lock()` to also clear `bonus_until` (+ `bonus_minutes`/`granted_*`, keep `bonus_history` with a `lock-cancelled` entry). Rationale: deliberate parental lock is the strongest signal and must transfer ownership (root cause 1 rule). |
| Manual **unlock** during bonus | Keep bonus as-is (expiry still applies). No flag change beyond existing `note_manual_unlock()` — but since grant cleared the override, this sets a fresh one; document that Unlock-after-grant reverts to unbounded override semantics. |
| Budget freezer pauses | **No interaction.** Bonus gates only the `usermod` account lock. A budget-paused category stays frozen during bonus; `budgets.py` unchanged. State this in the UI copy so parents are not surprised ("bonus extends login time, not app time limits"). |
| Bedtime **save** during bonus | Preserve `bonus_*` keys across save (extend the preserved-fields write at `app.py:906-915`). New schedule enforces immediately, but active bonus still suppresses the lock. |
| Bedtime **clear** during bonus | Clear bonus fields too (bonus without a schedule is meaningless). Keep history? No — entry is deleted, consistent with current clear. |
| Service / timer restart | Bonus is a wall-clock ISO timestamp in `bedtime.json`; both enforcer processes read it fresh each pass. Restarts are inert; an expiry that passed during downtime is applied on the next tick (locks if still in window, else no-op). No in-memory timers anywhere. |
| Wheel admins | Refuse bonus grants for wheel members (`_wheel_target_refused`, same 403 as lock paths): wheel accounts can never be bedtime-locked, so a grant is either a no-op or a state write on an admin — refuse for invariant consistency. |

### 1.4 Denominations (the 5 shortcuts)

Proposed: **15 / 30 / 60 / 90 / 120 minutes.**

- 15 — "finish up and save" (matches the existing 15-min pre-bedtime warning,
  `BEDTIME_WARN_MINUTES`, so the mental model is already trained).
- 30 — one episode / one homework task.
- 60 — the standard extension unit.
- 90 — feature-film / long task; fills the gap where parents otherwise stack
  60+30.
- 120 — the required 2-hour maximum single grant; doubles as the proposed cap
  (see §7 open questions).

All whole minutes (budgets UI already promises "whole minutes only"), evenly
covers 15 min → 2 h, no seconds ever displayed.

## 2. Countdown / expiry mechanics

- **Who ticks: `enforce_bedtime()` itself.** Add a per-user bonus check at the
  top of the existing loop (`app.py:512`). No new thread, no new timer unit:
  both enforcement paths (app thread + systemd timer) then behave identically
  with zero drift between them. Granularity is the existing ~60s tick
  (+`AccuracySec=10s`), so a "15 min" grant may run up to ~61s long — document
  in UI copy ("about 15 minutes").
- **Per-user pass order:** (1) if `bonus_until` set and expired → clear bonus
  keys, append `expire` history entry, fall through; (2) if bonus active →
  `continue` (skip lock *and* skip the pre-warning? No — keep warnings; only
  skip the lock. Also skip auto-unlock bookkeeping while active); (3) existing
  in-bed/out-of-bed logic unchanged.
- **At expiry:** clear `bonus_until`/`bonus_minutes`/`granted_*` (lazy clear,
  one `save_bedtime`), then run the normal branch: still in bedtime → lock +
  `locked_by_bedtime=True` + `_safe_notify("Bonus time ended", …)`; out of
  bedtime → stay unlocked (window-end override clearing proceeds as usual).
- **Wall-clock semantics, no pause.** Bonus burns real elapsed time including
  suspend/sleep. Rejected alternative: activity-gated countdown — no suspend
  detection or session-agent infra exists (Phase 4 item), and parents think in
  clock time ("15 more minutes").
- **Stacking = extend.** Grant while active: `new_until = max(now,
  current_until) + minutes`. A parent adding 30 min to 10 remaining gets 40,
  not 30. History kind `extend`. Cap stacked total (propose: cannot exceed
  180 min from now — §7).
- **Revoke:** clears `bonus_until` immediately, appends `revoke` entry, and
  re-runs that user's enforcement inline (locks at once if in bedtime) rather
  than waiting up to 60s.
- **Child notification:** `_safe_notify(username, "Bonus time: N minutes",
  "Device stays unlocked until HH:MM")` on grant; `"Bonus time ended"` at
  expiry-lock. Same best-effort, never-blocks pattern as existing warnings.

## 3. API surface and UI

### API (all `POST`, form-encoded, `@require_admin` + `X-CSRF-Token`)

| Endpoint | Fields | Success behavior |
|---|---|---|
| `POST /api/bonus` | `username`, `minutes` | Validate; extend-or-set `bonus_until`; unlock immediately if bedtime-locked; return `{success, message, locked: False, bonus_until, remaining_minutes}` |
| `POST /api/bonus/revoke` | `username` | Clear bonus, history `revoke`, re-enforce inline; return `{success, message, locked: <bool>}` |

Validation: `valid_username()`; `minutes` integer 1..480 (custom-entry ceiling;
shortcuts only ever send the 5 allowlisted values — server re-validates
anyway); require an existing bedtime entry with a valid schedule
(`400 "No bedtime schedule set"` — granting bonus with nothing to suspend is a
logic error, and it avoids creating phantom entries); wheel refusal per §1.3.
`minutes<=0`, non-numeric, or missing → `400`.

Reads: extend `GET /api/user/<u>/live` payload with
`bonus: {active: bool, until: str|null, remaining_minutes: int|null,
granted_by, last_minutes}` and include the same block in `user_detail` context
+ `/api/users` entries (cheap: already loads bedtime).

History/audit: every grant/extend/revoke/expire/lock-cancel appends
`{at: now.isoformat(), by: g.admin (or "system" for expire), minutes, until,
kind}`. This is the deliberate feed for the Phase-3 audit log (`PLAN.md:67`):
fields map 1:1 to a future `(actor, action, target, timestamp, detail)` row.

### UI placement (`templates/user.html`)

New **"Bonus Time"** section inside/just below the Bedtime Settings card:

- Status line: `Bonus active: 23 min left (until 21:47, granted by stan)` or
  `No bonus time active.` + `Revoke` button (only when active).
- Five shortcut buttons: `+15m  +30m  +1h  +1h30  +2h` — **no confirm dialog**
  (the feature requirement is one-click; grants only *extend* access, so they
  are non-destructive).
- Custom row: `<input type="number" min=1 max=480> minutes  [Grant]` — single
  `confirm("Grant N minutes to X? Stays unlocked until HH:MM")`, consistent
  with the existing single-confirm convention for non-destructive unlock.
- Revoke uses a single confirm (it *removes* access → destructive-adjacent,
  same tier as Lock's single confirm; reserve double-confirm for session
  termination only).
- Account Status badges: new `badge-bonus` (suggest gold `#f0c040` — distinct
  from purple In-Bedtime and orange Override) rendered in both server template
  and `renderAccount()`; new `renderBonus()` fed by the extended live payload
  in the existing 10s `refreshUserLive()`; never touch the custom-minutes input
  during refresh (existing forms-never-touched rule).

## 4. Edge cases (with resolutions)

1. **Bonus spans midnight / window end.** Bonus is an absolute timestamp, not
   window-relative — nothing special happens at midnight. If the window ends
   mid-bonus, the out-of-bedtime branch runs (override cleared, no lock);
   bonus persists inertly until its own expiry, when the lazy clear fires.
2. **Grant during active bedtime while unlocked via override.** Resolved §1.3:
   grant clears the override and substitutes the bounded bonus.
3. **Manual lock during bonus.** Resolved §1.3: wins, cancels bonus, history
   `lock-cancelled`. (Mirrors root-cause-1 ownership rule.)
4. **Overlapping grants.** Extend semantics (§2); each grant separately
   validated, capped, and history-logged.
5. **Bedtime save during bonus.** `bonus_*` keys join `locked_by_bedtime` in
   the preserved set; `manual_override` reset behavior unchanged (grant
   already cleared it).
6. **Bedtime clear during bonus.** Bonus fields cleared with the entry; if
   `locked_by_bedtime` was set the existing unlock path runs.
7. **Timezone / day-wrap.** All timestamps are naive local ISO
   (`datetime.now()`), the same clock `is_in_bedtime()` uses — no tz-aware
   datetimes anywhere in the codebase, so no conversion bugs. Overnight
   windows (`19:00–10:00`) need no special-casing.
8. **Two writers, no locking** (known risk, `QA_LOCK_UNLOCK.md` #4). Expiry is
   idempotent (clear-if-expired); concurrent grant+expiry resolves on the next
   tick. Accept; Phase-4 SQLite removes the class.
9. **Grant for user with no schedule / invalid schedule.** `400`, no state
   write.
10. **Clock jumps (NTP/suspend).** Wall-clock expiry inherits the same
    behavior as `minutes_until_bedtime()`; a large forward jump simply expires
    the bonus early (fail-closed toward enforcement). `MAX_TICK_WALL_SECONDS`
    capping is budget-specific and intentionally *not* applied here — bonus is
    a deadline, not accrued usage.

## 5. Test plan

New `tests/test_bonus_time.py` (stdlib `unittest`, fully mocked, reusing
`_AdminSessionMixin` + temp-`BEDTIME_FILE` harness from
`tests/test_lock_regression.py:63+`; no real accounts, no sudo):

Pure/enforcement unit tests (inject `now=` into `enforce_bedtime`):

1. `test_grant_unlocks_bedtime_locked` — bedtime-locked + grant → `usermod -U`
   called, `locked_by_bedtime` cleared, `bonus_until ≈ now+N`.
2. `test_active_bonus_suppresses_lock` — in-window, unlocked, bonus active →
   no `usermod -L` across the pass.
3. `test_expiry_relocks_in_window` — tick past `bonus_until` while in bedtime
   → `usermod -L`, `locked_by_bedtime=True`, bonus keys cleared, `expire`
   history entry, expiry notification queued.
4. `test_expiry_out_of_window_stays_unlocked` — no lock call, bonus cleared.
5. `test_stacking_extends` — grant during active bonus → `until` advances by
   full N from previous `until`, kind `extend`.
6. `test_revoke_relocks_immediately` — revoke in-window → inline lock without
   waiting a tick.
7. `test_manual_lock_cancels_bonus` — `note_manual_lock` clears `bonus_until`,
   history `lock-cancelled`; window end never auto-unlocks.
8. `test_grant_clears_manual_override` — override replaced by bounded bonus;
   expiry then re-locks (pins §1.3 decision).
9. `test_save_preserves_bonus` / `test_clear_drops_bonus` — bedtime save/clear
   paths.
10. `test_corrupt_bonus_until_enforces` — garbage timestamp → lock proceeds
    (fail-closed).
11. `test_no_bonus_no_behavior_change` — existing enforce matrix unaffected
    (regression guard; run full existing suite).

HTTP/API tests (Flask test client, mocked `run_lock_command`/`get_lock_status`):

12. `test_api_grant_requires_auth_and_csrf` (401/403 matrix, mirrors auth tests).
13. `test_api_grant_validation` — bad username, leading dash, `minutes` 0 /
    negative / non-numeric / >480, no-schedule user → `400`, no state write.
14. `test_api_grant_wheel_refused` — 403, no state write.
15. `test_live_payload_includes_bonus` — `/api/user/<u>/live` bonus block shape.
16. `test_history_appends_and_caps_at_20` — 21 grants → 20 retained, newest last.

Live tests: **none required.** Optionally, one `pcagent`-only spot check (never
`tester`/`bluefox`/`stan`): grant 1 min custom past a temporary bedtime,
assert `passwd -S` shows `P` during bonus and the timer re-locks after — then
remove the schedule and leave `pcagent` unlocked. Prefer mocked-only; the
expiry wait makes live coverage poor value.

Target: full suite stays green via `cd master && .venv/bin/python -m unittest
discover -s tests`.

## 6. Rollout (no migration, no privilege changes)

- **No unit changes** (same 60s tick, no new timer/service), **no sudoers
  changes** (only existing `usermod -L/-U` + `notify-send` paths),
  **no schema migration** (optional JSON keys).
- Steps: implement on `feature/bonus-time` worktree → mocked tests green →
  merge to `master` → `sudo ./install.sh` (stages code, preserves runtime
  `bedtime.json`, restarts service) → smoke test `curl localhost:5000/` (login
  redirect expected) → exercise grant/revoke/expiry on `pcagent` only, confirm
  `pcagent` left unlocked and schedule-free → confirm
  `parental-controls-enforce.timer` log shows normal passes.
- Rollback: redeploy previous master; unknown `bonus_*` keys are ignored by
  old code (a stale future `bonus_until` would simply sit inert — old code
  never reads it — until the new code returns; note in release notes).

## 7. Open questions for the parent

1. Denominations: OK with **15 / 30 / 60 / 90 / 120**? Any swap (e.g. 45 for
   90)?
2. Caps: max single grant 120 min? Max stacked total (propose 180 min from
   now)? Custom-entry ceiling (propose 480)?
3. Custom-entry floor: 1 minute, or 5? → **Decided 2026-10-10: 5 minutes**
   (`BONUS_CUSTOM_MIN = 5`; shortcuts start at 15, unaffected).
4. Bonus with **no bedtime set**: refuse (recommended) or allow as a plain
   timed unlock?
5. Should bonus also pause/extend **budget** time, or stay login-only
   (recommended: login-only)?
6. History retention: last-20 in `bedtime.json` enough until the Phase-3 audit
   log lands, or keep full per-user history somewhere? → **Decided 2026-10-10:
   last-20 is enough** (`BONUS_HISTORY_CAP = 20`, unchanged).
7. Notification wording for grant/expiry (child-facing tone)?
8. Revoke confirm: single (recommended) or double?
