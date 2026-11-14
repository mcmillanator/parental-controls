# CALENDAR_DESIGN.md — one calendar model for bedtime + weekly windows

**Date:** 2026-11-13 · **Branch:** `feature/calendar-schedule` · **Status:**
backend slice implemented; UI slice deferred (owner-approved split).

Replaces the two time-config tables — the `bedtime` table (per-user
`{start, end, days}` row) and the `windows` sub-doc of `overrides` — with
ONE recurring **block** model enforced from one source of truth: the
whole-doc `schedule` table (schema v4).

## 1. The model

A **block** grants time-bound behaviour to one **target**:

| target | shape | meaning while the block is ACTIVE |
|---|---|---|
| `bedtime` | one row per user is the norm (`b-bedtime-1`) | the device **locks** — today's bedtime semantics verbatim (midnight-crossing, `was_in_bedtime` transitions, manual-override, bonus suppression) |
| `games`/`browsers`/`media`/`chat`/`other` | the fixed `budgets.CATEGORIES` set | see modes |
| `app:<comm>` | comm validated by the same `[A-Za-z0-9_.-]+` charset as cgroups | see modes |

Modes (only for category/app targets; bedtime blocks carry `mode: null`):

* `capped` + `allowance_minutes` — the window **replaces** the daily limit
  while active: EXACTLY today's weekly-window semantics (per-id
  `window_used`/`window_active` runtime counters, open/close transitions,
  daily-reset survival). Migration produces only these.
* `permitted` — usage **allowed during the block, no minute cap**; while a
  target has >=1 ENABLED permitted block it **gates**: inside any of them
  the target is unlimited (and any gating freeze thaws), outside all of
  them the target is frozen (limit 0 path). This is the literal reading of
  owner rule #2 ("blocks gate only where present") applied to permitted
  blocks, because rules #1+#3 (capped = exact window semantics + the
  equivalence safety net) FORBID gating from capped blocks — migrated
  configs must not change behaviour at cutover.

**Bedtime wins device-wide**: an active bedtime block locks the account;
the tick already bills nothing for locked accounts.

Stored block (canonical shape written by every write path):

```json
{"id": "b-1", "target": "games", "mode": "capped",
 "allowance_minutes": 45, "preset": "SCHOOL",
 "dtstart": "2026-11-13", "start": "16:00", "end": "18:00",
 "until": null, "enabled": true, "set_by": "stan",
 "set_at": "2026-11-13T10:00:00"}
```

Table doc: `schedule[username] = {"events": {id: block}, "migrated": iso}`.

## 2. Preset grammar (RRULE-lite, presets only, series-only editing)

Exact canonical strings (`preset` field):

```
ONE                          single occurrence on dtstart (dtstart IS the day)
DAILY                        every day >= dtstart
SCHOOL                       Mon-Fri >= dtstart
WEEKLY:BYDAY=<c>(,<c>)*      chosen weekdays, canonical MO,TU,WE,TH,FR,SA,SU order,
                             unique codes (MO=Monday .. SU=Sunday == date.weekday())
BIWEEKLY:BYDAY=<c>(,<c>)*    every 2nd week, anchored on the Monday-based week
                             containing dtstart: floor((monday(day) -
                             monday(dtstart)).days / 7) % 2 == 0
```

* Any preset may carry `until` (inclusive `YYYY-MM-DD`, stored as a block
  FIELD, not inside the preset string — one canonical place; the mission's
  "UNTIL=<date> option" is modelled as `block.until`). No COUNT in v1.
* `dtstart` is REQUIRED on every block (fail closed). For recurring
  presets it is inclusive: occurrences start on dtstart. Migrated blocks
  are anchored `migration day - 1` so an in-progress midnight-crossing
  window keeps enforcing across the cutover (a past evening can never
  "fire again").
* Wall-clock naive semantics (device-local); DST just shifts — same as
  today. `end < start` means the occurrence ends the NEXT day; the
  midnight tail belongs to the anchor day (the existing predicate rule in
  `app.is_in_bedtime` / `budgets._in_window`, mirrored exactly).
* v1 is series-only: no per-occurrence exceptions, no exception fields.

## 3. Migration (auto, idempotent, hard cutover)

Trigger: the panel's calendar routes (`GET /api/schedule`,
`GET /api/schedule/events`, and writes go through the same doc funnel).
Deliberately NOT in `tick_budgets` or the agent verbs: those legitimately
run with per-test redirected stores, and a tick-time migration wrote docs
into unpatched homes (observed during development). Enforcement stays
behaviourally identical WITHOUT any doc via the on-the-fly derivation
(`user_windows` derives legacy windows into identical blocks; the
equivalence tests pin it), so nothing waits on the trigger.

Mapping table (old -> new):

| legacy config | block |
|---|---|
| bedtime row `{start, end, days}` | `b-bedtime-1`, target `bedtime`, `mode: null`, preset from days (7->DAILY, Mon-Fri->SCHOOL, else WEEKLY:BYDAY), same start/end, `until: null`, enabled, `dtstart: today-1` |
| ENABLED valid window `w-<type>-<t>` | target `category|app:<t>`, `mode: capped`, `allowance_minutes` copied, preset from days, `dtstart: today-1`, **id preserved** -> `window_used[wid]`/`window_active[wid]` runtime keys carry over untouched |
| disabled window | NOT migrated (marker still stamped) |
| dead window (normalize_window -> None: bad times, start==end, days empty/bad, allowance < 5) | NOT migrated |
| garbage bedtime row (bad/empty fields) | no block; marker still stamped |
| `bonus_*` live state | never a block — see §5 |

Idempotent: `migrated` marker gates re-runs; deterministic: every converter
is pure over `(config, now)` — the same input + timestamp produces
byte-identical docs. Audit: one `schedule.migrate` event per user with the
created block ids. The legacy tables stay in place untouched by migration
as the backup/mirror described below.

## 4. Enforcement swap (all names survive — phase-1 rule)

* `app.is_in_bedtime` / `agent.is_in_bedtime` — the name and the legacy
  `(entry, now)` call shape are UNCHANGED (pinned patch surfaces with
  strict-signature lambdas in the suite). The relocated legacy predicate
  now lives in `schedule.in_bedtime_entry`; callers that know the username
  route through `app._bedtime_flag` / `agent._bedtime_flag`, which resolve
  migrated docs via `schedule.in_bedtime` and otherwise call the bare
  `is_in_bedtime(entry, now)` exactly as before. `is_in_bedtime` also
  accepts `schedule_doc=` for direct callers.
* `budgets._in_window` / `_in_bedtime` / `active_window` / `iter_windows`
  / `normalize_window` — all KEEP their definitions and patch surfaces
  (migration source + legacy readers). Enforcement call sites moved to:
  * `budgets.user_windows(username, overrides, schedule_doc, today)` —
    blocks from the doc when migrated, else derived from legacy windows
    (ids preserved);
    `budgets_enforce.account_and_enforce` gained `schedule_doc=None`.
  * `budgets.block_active` (evaluator), `budgets.active_capped` (replaces
    `active_window` at the app-target/`budget_status` sites),
    `budgets.has_bedtime_config` / `budgets.in_bedtime_for` (bedtime
    reset anchor + predicate).
  * Permitted gating lives in `account_and_enforce`'s category/app loops:
    unlimited inside (thaws a prior gating freeze), limit-0 outside.
* `budgets_tick._clear_window_target_freeze` — accepts BOTH shapes (block
  target strings and legacy `target_type`+`target`).
* `app.minutes_until_bedtime` / `app.bedtime_window_id` /
  `app.countdown_state` — schedule-aware (next bedtime BLOCK powers the
  countdown + warned_for window id); legacy bodies untouched for
  non-migrated users.
* New panel API (same auth/CSRF/protected-target/audit pattern as
  `/api/budgets/windows`): `GET /api/schedule` (view incl. `invalid` ids),
  `POST /api/schedule` (create/edit, overlap-checked),
  `DELETE /api/schedule/<id>`, `GET /api/schedule/events?username&start&end`
  (expansion + the derived bonus band, `derived: true`).
* New table glue in `store.py` (`schedule` whole-doc table, v4 migration,
  `schedule.json` rollback mirror, gitignored) and `budgets.py`
  (`SCHEDULE_FILE`, facade re-exports of the whole calendar surface).
* `schedule.py` is the PURE evaluator: stdlib-only, zero local imports,
  importable by `agent.py` without app/flask. `SharedCorePurityTest`
  AST-pins it next to pcstatus/pcprobe; the agent's local-import
  allowlist gained exactly `schedule`.

## 5. Bonus time is UNTOUCHED

Bonus stays exactly where it lives today — `bonus_*` live-state keys on
the bedtime row + `bonus_state()` bookkeeping. It is NOT modelled as
events. The only calendar-side touch: `GET /api/schedule/events` reports
the current live bonus band as a `derived: true` pseudo-event (never
stored) so a future calendar UI can paint it.

## 6. Mirrors + fail-closed policy

Sources of truth after cutover: **schedule table = config truth**; the
`bedtime` row (config fields `start/end/days` only) and
`overrides[username]['windows']` are **write-synchronized mirrors**:

* Every calendar write (`upsert_block`, `remove_block`,
  `set_bedtime_block`, old-editor write-through, window routes on a
  migrated user) refreshes the mirror: the windows mirror regenerates
  from capped blocks that the legacy shape can EXPRESS (DAILY/SCHOOL/
  WEEKLY, no UNTIL); ONE/BIWEEKLY/UNTIL-bound/permitted blocks are absent
  from that mirror. The bedtime entry mirror renders the first (id-order)
  bedtime block; ONE maps to its weekday, BIWEEKLY to its BYDAY set
  (display-only lossiness; on a corruption fallback it leans MORE
  restrictive = fail-closed direction). Live-state keys
  (`locked_by_bedtime`, `manual_override`, `warned_for`, `bonus_*`) are
  never rewritten by the mirror sync — only the config fields.
* Enforcement NEVER consults the legacy shapes for a user whose doc is
  migrated. A malformed stored block is treated as absent everywhere
  (`iter_blocks` fails closed) — the API view reports its id under
  `invalid` so the parent can see and remove it.
* Corrupt/unreadable schedule table (whole store, e.g. a rolled-back
  `state.db`): reads yield no doc -> enforcement falls back to the legacy
  mirrors. Because every mutation synchronized the mirrors, the fallback
  equals the last committed truth — deleted rules do NOT resurrect (the
  hazard the mission named). A full re-migration from the mirrors is
  audit-logged loudly (`schedule.migrate` re-fires; the event itself is
  the alarm). Residual risk: a schedule write whose mirror sync failed
  (OSError printed) can lag the truth until the next successful write —
  enforcement is unaffected while the schedule table reads fine.

## 7. Semantics kept deliberately

* `start == end` remains invalid (zero-length window, legacy rule).
* Adjacent blocks (18:00 end / 18:00 start) are allowed; overlap check is
  half-open intersection on the same anchor day PLUS the
  cross-midnight-tail pairing.
* Same child+target overlap is rejected by every write path (400 +
  conflicting id); different targets overlap freely; bedtime wins
  device-wide.
* The 10s polling contracts, `/api/bedtime` / `/api/budgets/windows`
  request/response shapes, and the live payload keys are unchanged;
  `in_bedtime`/`countdown` payload fields are additive-compatible
  (`/api/user/<u>/live` gains a `schedule` key for the countdown).

## 8. Tests

* `tests/test_schedule_eval.py` — evaluator tables (normalize/preset/
  activity/UNTIL/BIWEEKLY anchor), expand (midnight/week/month/UNTIL),
  overlap matrix, migration converters, the EQUIVALENCE NET: frozen old
  predicates vs migrated docs over 16-day x 30-min sampled timelines
  (bedtime matrix 13 configs, window matrix >=6 valid configs;
  >7500 + >4600 sampled comparisons) plus a 10-day x 15-min direct
  `_in_window` pin; constant mirrors (CATEGORIES/WINDOW_MIN_MINUTES).
* `tests/test_schedule_budgets.py` — store v4 (round-trip, mirror, legacy
  import, v3-upgrade with backup), `ensure_migrated` matrix (idempotent/
  deterministic/marker-for-dead-config/no-doc-for-empty), tick
  hermeticity (tick must NOT create docs) + doc reading,
  permitted gating (inside/outside/beats-capped/app-target/no-blocks),
  window write-through + upsert/remove/bedtime funnel + mirrors,
  `budget_status` over blocks, dead-block fail-closed.
* `tests/test_schedule_api.py` — auth/CSRF/protected matrix, validation
  rejects (no state written), CRUD + mirror sync, overlap 400, bedtime
  entry mirror (live keys survive), first-access migration via
  GET /api/schedule (idempotent), old-editor write-through (incl. clear),
  events expansion + derived bonus band + range validation, `_bedtime_flag`
  / `is_in_bedtime` shims, `enforce_bedtime` from blocks.
* `tests/test_schedule_agent.py` — agent shims, verb write-through for a
  pre-migrated doc, "agent never creates a doc", payload `in_bedtime`
  from blocks.
* `SharedCorePurityTest.SHARED` gained `schedule.py`; the agent local-import
  allowlist gained `schedule`.

## 9. Deferred to the UI slice

Calendar page/templates/JS (explicitly out of this slice), per-occurrence
exceptions, richer target grammar, hub->agent schedule proxying, and
retiring the old `overrides.windows` writer once the panel speaks
`/api/schedule` natively.

## 10. UI slice (2026-11-14, `feature/calendar-ui`)

Owner-approved build of the parent-facing **Calendar page** on the
`/api/schedule` backend above, replacing the Time-rules landing page and
the Weekly-Windows editors. Decisions + pins:

* **Vendored artifact:** `static/vendor/fullcalendar/index.global.min.js` —
  FullCalendar **Standard Bundle v6.1.22**, MIT license
  (`package fullcalendar` on npm; upstream
  https://github.com/fullcalendar/fullcalendar). Single file, no separate
  CSS artifact: v6 inlines its stylesheets and injects them as `<style>`
  elements from the JS (verified in the bundle: `createElement("style")` +
  the `.fc-*` selector set). Committed with a VENDORING banner; hashes,
  byte counts and the upgrade procedure live in
  `static/vendor/fullcalendar/NOTICE.txt`. **No CDN/network reference may
  ever appear in templates or static** — pinned by
  `tests/test_ui_calendar.py` (offline-asset guard for the new page) and
  `tests/test_ui_assets.py` (rendered-page sweep on /calendar).
* **Page:** `/calendar` (replaces `/time-rules` as the `time` nav section).
  Child selector follows the budgets-page pattern (`?user=`), week grid
  (`timeGridWeek`) default + month toggle (`dayGridMonth`), live `now`
  indicator, events fed by `GET /api/schedule/events?username&start&end`.
* **Editor:** drag-select creates, resize/move edits, **series-only** (v1
  has no per-occurrence exception model — §2). Modal writes
  `POST /api/schedule`; delete writes `DELETE /api/schedule/<id>` with a
  confirm. Preset dropdown emits exactly the §2 canonical strings
  (`ONE` / `DAILY` / `SCHOOL` / `WEEKLY:BYDAY=…` / `BIWEEKLY:BYDAY=…`);
  `until` is a block FIELD (`UNTIL=<date>` in the mission shorthand is
  stored as `until`, never inside the preset string).
* **Polling choice:** the grid re-fetches events on every successful
  mutation **plus a gentle 60 s timer** (calendar minute resolution needs
  no more; the Overview's 10 s contract is for lock/budget status and is
  untouched). `nowIndicator` is client-side, so the marker moves without
  network.
* **Fail closed:** an events fetch failure renders an explicit error
  banner over the grid (copy style mirrors `_insecure_banner`/status-probe
  language) — a render error never implies "no restrictions"; enforcement
  is unaffected (server-side).
* **Retired surfaces (hard cutover):** `templates/time_rules.html` + the
  `/time-rules` route/nav entry, the `POST /api/bedtime` config writer
  (GET stays for the live chips/mirror readers), the Weekly-Windows UI
  section on `budgets.html` + the per-child windows/bedtime editors on
  `user.html`, and the `POST /api/budgets/windows` writer (§9's
  "retire the old overrides.windows writer — now"). Legacy tables remain
  migration source + write-synchronized mirrors maintained by the
  schedule API itself (§6) — no UI path writes them anymore. Daily
  limits, per-app overrides, freeze/resume and the bonus dialog are
  unchanged.

### 10.1 VERIFY-ON-VM checklist (UI slice — owner/manager, on the box)

Deploy the merged branch (`sudo ./install.sh` from the checkout), then
against the URL install.sh prints:

1. **Login → sidebar shows 'Calendar' (clock icon)** where 'Time rules'
   used to be; `/time-rules` now 404s. Daily-limits page still reachable
   from the child page links.
2. **Open /calendar** with an existing legacy bedtime row + weekly
   windows: first load triggers the migration (audit `schedule.migrate`);
   the week grid shows the bedtime block (violet, midnight-crossing),
   each capped window as its own block, and — if a bonus is live — the
   striped blue band. Click the band → hint strip pointing at the child
   page's bonus dialog. No browser-console 404s; devtools Network tab
   shows ZERO off-origin requests (offline promise).
3. **Drag Mon 21:00→Tue 08:00** on the grid (or single-click a slot, or
   press the '+ New block' toolbar button — all three create paths land in
   the same modal; 2026-11-14 fix) → modal: target Bedtime,
   repeat Every day, save → block appears; Overview/user-page chips +
   countdown reflect it (mirror writes work); `curl -s -b <session>
   .../api/bedtime` shows the mirrored start/end/days.
4. **Cap-edit**: drag/create a Games block 16:00–18:00, Capped 45m;
   then drag its bottom edge to resize → toast "Resize applied to the
   whole series"; next day's occurrence moved too (series-only).
5. **Overlap**: create a second Games block overlapping the first →
   modal stays open with the inline error naming the conflicting id
   (no vanishing alert).
6. **Bedtime lock path**: create an in-window bedtime block while the
   child is logged in → account locks without terminating the session
   (journal shows `usermod -L`, no `terminate-user`); delete the series
   → bedtime-owned lock releases; a MANUALLY locked account does not.
7. **Delete series** via the modal → confirm dialog → block vanishes on
   refresh; enforcement stops at the next tick.
8. **budgets.html**: no Weekly-Windows card (only the one-line Calendar
   pointer); daily limits, App override dialog, freeze/resume behave
   exactly as before; `/user/<child>` shows the read-only bedtime mirror
   + "Edit bedtime on the Calendar".
9. **Month toggle** renders the same blocks in `dayGridMonth`; the now
   indicator tracks the minute without any network call; kill the
   service's data dir read (or set an invalid `?user=`) → the grid shows
   the explicit "Calendar could not refresh" banner, never an empty
   "no restrictions" grid.
10. Full suite on the box: expect `Ran 1681, OK` with the VM's ~22-skip
    profile (skip-delta vs backend slice's baseline as before).
