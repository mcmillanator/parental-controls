# SESSION_AGENT_PLAN — Per-User Session Agent for Focus-Based Usage Measurement

Feature branch: `feature/session-agent-plan` (worktree `session-agent-plan/`), based on `master` @ `bde1ba3`.
Research + design only — **no code in this file and nothing here is implemented.**

Planned implementation branch (for the implementing agent, later): `feature/session-agent`, worktree `session-agent/`.
Phase tracked in `PLAN.md` Phase 4: *"Per-user session agent for focus-based usage measurement (replaces CPU-delta proxy)"*.

## Table of contents

1. Problem statement — what CPU-time deltas mis-measure
2. Architecture decision — chosen design + rejected options with reasons
3. Deployment on Bazzite/immutable OS
4. How "locked or gone" reports zero
5. Never-pause categories under the focus engine
6. Suspend / sleep handling
7. Privacy — what the agent records
8. Data/contract — feed schema, transport, panel ingestion, aggregation
9. Interplay with the existing tick, cgroup freezer, day reset, weekly windows
10. Security — isolation, tamper tradeoff
11. Phasing, test strategy, rollout, rollback
12. Open questions with recommendations
13. Appendix: constants and files touched (implementation map)

---

## 1. Problem statement

Today (`budgets.py`, module docstring) usage is measured as **active wall-clock
minutes derived from CPU-time deltas**:

- Every 60 s tick, `list_user_processes()` reads `ps -o pid,comm,etime,time,args`
  per user; `compute_deltas()` diffs each PID's cumulative CPU time against the
  previous tick (first sighting / PID reuse → delta 0).
- A category is charged the whole tick's wall-clock duration when **any** of its
  *freezable* members (visible, categorized, not NEVER-pause, limit configured —
  `freezable_members()`) accumulated ≥ `ACTIVE_THRESHOLD_SECONDS` (1.0 CPU-sec
  per 60 s tick, ≈1.7% CPU) of CPU time; idle categories charge nothing
  (`account_and_enforce()` `category_active`).
- A **locked** account (usermod -L) bills nothing: `_locked_tick_bookkeeping()`
  advances `last_cpu`/`last_tick` baselines without charging, so the first tick
  after unlock bills only new activity (per approved decision 2026-10-07).
- Charge per tick is capped at `MAX_TICK_WALL_SECONDS` (300 s).

**What this mis-measures (the problem):**

1. **Idle-but-running apps still burn CPU.** A paused game, a video player
   playing background audio, a chat app with an obnoxious autoplay tab, Steam
   download churn, a browser tab running a busy script — any process averaging
   ≥1.7% CPU posts deltas, so the *whole minute* is charged even though the child
   is doing homework in another window. The budget for a category drains while
   the child is not engaging with that category at all.
2. **The active-threshold is a blunt instrument.** It cannot tell "Firefox is
   focused and the child is reading" from "Firefox is minimized, one tab is
   rendering". All it sees is aggregate CPU. The *Elapsed* column parents are
   told usage tracks is the wrong axis for a "you get 60 minutes of X" promise.
3. **The locked-account rule now halts billing entirely** — forcing us to
   choose between two bad outcomes. Locked = the child provably cannot reach the
   app, so billing *nothing* is right; but the same mechanism means a child who
   is locked *out* (screen locked by the parent) has zero recorded usage for
   that window even if... nothing. In practice the lock rule is correct; the
   problem is it is the *only* lever we have. There is no notion of "app is in
   use" independent of "account is unlocked".
4. **What real parental UX needs:** the promise parents make is per-**category
   engaged time** — "60 minutes of games, 90 of media". They want, as closely as
   is honestly measurable on a desktop, *"time the child spent actively engaged
   with that category"* — focused-window time, not CPU burns from background
   processes and not wall-clock while the screen is locked or the machine sleeps.
   They also want that number to be **explainable**: "the games budget ran out
   because you played" should be true, not "because Steam was downloading".

The approved decision at 2026-10-07 said: *"CPU-time deltas until a session
agent exists"* and *"Enforcement may eventually need a per-user session agent
(focus time, warnings) — not this phase."* This plan is that phase: replace the
CPU-delta *activity gate* with **focused-window time measured inside the child's
own session**, keep the cgroup freezer and the tick/enforcement machinery, and
keep the CPU proxy available as a per-user fallback engine.

---

## 2. Architecture decision

### Options considered

| Option | Shape | Verdict |
|---|---|---|
| **(a) systemd *user* service, OnCalendar tick** | A `--user` service with a `OnCalendar=*-*-* *:*:00` timer runs once a minute in the child's session, asks the desktop which window is focused, and reports "0 or 1 minutes per category" to the panel via IPC/log. | **Rejected as the primary** — granularity and correctness are identical to today's tick (whole-minute quantization, no session continuity), it still cannot answer "did the child switch away at 14:03 and back at 14:07?", and a minute-granularity reporter cannot do resume/suspend splitting well. Kept only as the *fallback backend inside* the chosen agent (see "poll backend"). |
| **(b) always-on per-user session agent** | A small headless process, started with the child's graphical session and kept alive (linger), that subscribes to **window-focus events** (KWin scripting, with an `activeWindow()` poll fallback) and appends **focus-session rows** to a feed the panel drains. | **CHOSEN.** Event-driven gives exact engaged-time intervals, handles suspend (BOOTTIME), lock/gone (state rows), and is a natural fit for future per-app overrides and weekly windows. |
| **(c) hybrid: focus agent + keep CPU tick as co-pilot** | The CPU tick stays live and both signals are merged (e.g. "whichever is smaller") to resist tamper. | **Partially adopted, not as the billing core.** Double-signal merging has no principled rule (which one is "right"?), doubles accounting complexity, and the CPU signal is exactly the mis-measuring signal we are removing. Instead the CPU engine is retained **as a per-user fallback/rollback engine** behind a `focus_engine` flag, and `last_cpu` baselines keep advancing so switching back is lossless (see §11). |

### Chosen design

> **A per-user session agent (`parental-session-agent`), installed as a systemd
> user service with linger, runs in the child's graphical session, records
> window-focus sessions (KWin scripting events, `activeWindow()` polling fallback)
> into a small JSONL feed, and emits heartbeat/state rows. The panel pulls the
> feed on every budget tick (60 s), validates it strictly, aggregates focused
> seconds into per-category minutes with the *existing* categorization and
> freezable-members gate, and keeps using all existing enforcement (cgroup
> freezer, day reset, locked-account zero-billing). The CPU-delta engine remains
> the default and a one-flag rollback path.**

Why this exact shape:

1. **Focus is the right measured axis.** "Engaged time" on a desktop is
   overwhelmingly "which window has focus". It excludes minimized-but-busy apps
   entirely (fixing problem 1), removes the 1.7%-CPU threshold guesswork
   (problem 2), and makes budgets *explainable*.
2. **Event-driven beats ticking for accounting.** Focus intervals are naturally
   variable-length; emitting a row per focus *change* gives the panel exact
   interval data to clip against billing windows, bedtime transitions, suspend
   resumptions and (future) weekly windows, with none of the whole-minute
   quantization of option (a).
3. **It fits the existing architecture with minimal surgery.** Everything
   that must not change (enforcement, freezer, watchdog, reset, lock semantics,
   admin UI auth, notifications) stays exactly as-is. We only (i) replace the
   CPU-delta **activity gate** in `account_and_enforce()` with a
   focus-seconds figure from a pure aggregation function, and (ii) add the agent
   + feed + per-user flag. Module-level purity (`budgets.py` stays stdlib-only,
   unit-testable with system python3) is preserved: the agent talks to the panel
   **only through a text feed**, not through app imports or sockets into the app
   process (see §8, channel choice).
4. **It respects the two hard security constraints simultaneously.** The agent
   runs *as the child* (so it can read the session bus and the desktop focus
   API without any privilege), yet it never authenticates to the panel, holds no
   token, and sends nothing the panel executes — it writes a file the panel
   drains (child accounts never authenticate; panel is never driven by a child).
5. **Pluggable engines.** The same agent binary supports a KWin-script event
   backend, an `activeWindow()` poll backend, and a `no-engine` (no-data) mode —
   robust across DEs and Bazzite variants without changing the panel.

### Rejected alternatives, with reasons

- **(a) OnCalendar user tick**: rejected above (granularity) — but the poll
  backend inside the agent is exactly this shape as an *emergency* backend.
- **D-Bus subscription from a *system* service**: the panel must not hold a
  session-bus conversation as root per child; the machine transport exists
  (`systemd-run --machine=<u>@.host`) but is for one-shots, and a persistent
  system-side listener for each user's KWin signals is more privilege surface
  for zero benefit over the child-side agent.
- **Measuring via `xdotool`/X11 only**: KDE Plasma runs Wayland-first on
  Bazzite; X11-only focus probing is a fallback, not a base.
- **Using per-app CPU as the category-active seed but matching focus only to
  switch "on/off"**: hybrid-on/off, rejected because it inherits the CPU
  mis-measurement (a busy minimized app would keep the flag on 100% of the
  time it holds background CPU).
- **An agent that writes *directly* into the panel's SQLite `state.db`**: the
  child owns the agent's process, so a compromised agent could trash the panel's
  store; the feed channel keeps the child write-path strictly separate, and the
  panel remains the only writer to `state.db`.

---

## 3. Deployment on Bazzite / immutable OS

Constraints on Bazzite (Fedora Atomic, ostree/bootc — no persistent `/usr`
writes; `/etc` and `/var` and **home dirs persist** across updates):

1. **Agent code ships in the install dir, like everything else.**
   `install.sh` stages `session_agent.py` (+ the KWin script files) into
   `@APP_DIR@` as part of the existing code sync. The install dir is owned by
   the `parental-controls` service user, mode 0755 → the child can *read* the
   agent code but cannot modify it. The agent is stdlib-only (no new
   dependencies shipped into the venv).
2. **User unit lives in the child's home** — `~/.config/systemd/user/
   parental-session-agent.service` — so it survives every ostree update
   (home is not touched by the image) and needs no `/usr/lib/systemd/user`
   write. The unit's `ExecStart` references `@APP_DIR@`(resolved at install
   time — never a checkout path).
3. **Linger keeps the agent running without a login.** `loginctl enable-linger
   <child>` (run by the installer as root, idempotent) makes the child's user
   manager start at boot and stay alive headlessly. This is what gives us:
   - the agent survives logout (so it can emit `no_session` state instead of
     vanishing — the panel needs that to distinguish *"child not at the
     machine"* from *"agent dead"*), and
   - the feed stays drainable between logins.
   `loginctl disable-linger <child>` is the uninstall path.
4. **KWin focus source ships as a KWin script package** in
   `~/.local/share/kwin/scripts/parental-usage-focus/…` (metadata.desktop +
   contents/code/main.js), enabled via the `kwinrc` Plugins key at install
   time; the script forwards `workspace.activeWindow` changes to the agent
   (D-Bus to the agent's own bus name, or appends to the feed directly).
   Fallback: the agent **polls** `org.kde.KWin.activeWindow()` +
   `getWindowInfo()` every 5 s and diffs (no script needed) — the poll backend
   is the v1 default because it has no auto-load/session-lifecycle machinery.
5. **No `/usr` writes, no systemd *system* unit for the agent, no new polkit
   rules.** Only a new *scoped sudoers grant* for the panel's feed-drain
   command (§8, channel choice) and the installer's own root session (linger).
   Re-running `install.sh` re-renders everything; `install.sh --uninstall` (or
   the CLI recovery tool) removes unit + linger + KWin package + feed dirs.
6. **Runtime data** lives under the child's `XDG_RUNTIME_DIR`
   (`/run/user/<uid>/parental-controls/`, tmpfs — cleared on reboot, which is
   fine: aggregates are what persist, in the panel's `state.db`).
7. **Bazzite variants**: KDE (Kinoite-derived) is the primary target and the
   only one with a first-class focus engine. GNOME Bazzite → the agent starts,
   finds no supported engine, and emits `state:no_engine`; the panel runs that
   child on the **CPU fallback engine** (flagged in the UI). Cost of graceful
   degradation ≈ zero.

---

## 4. How "locked or gone" reports zero

The invariant from the 2026-10-07 decision must hold: *a locked account must not
burn budget*, and (extended here) *a child who is not at the machine must not
burn budget*.

- **Locked** (manual Lock, Lock Screen, bedtime auto-lock — all preserve the
  session, so the agent *keeps running* under the locked graphical session).
  The panel already gates on `account_locked(username)` in `tick_budgets()`;
  that gate stays. While locked the panel advances the **feed cursor** (its
  `feed_seq` watermark) without billing — the exact analogue of today's
  `_locked_tick_bookkeeping()` `last_cpu` advance — so the first tick after
  unlock bills only *new* focus sessions, never the locked window. The agent
  itself records whatever window holds focus (KWin's lock screen), but the panel
  never bills locked ticks, so this is defensively irrelevant.
- **Gone** (logged out). With linger the agent keeps running headless (no seat),
  sees no KWin bus / no graphical session, and emits periodic
  `{"type":"state","state":"no_session"}` heartbeats and *no* `focus` rows.
  The panel's aggregation window gets zero focused seconds → bills zero, and the
  cursor advances (child is simply not using the machine).
- **Distinguishing "gone" from "agent dead":** billing is driven by *fresh
  heartbeat* + *focus rows*. `no_session` heartbeats prove the agent is alive;
  a *missing or stale* heartbeat (> grace, default 3 min) means the agent is not
  delivering measurements, and the panel must **fail closed** — see §10
  (tamper/availability) — it must never silently assume zero on missing data.

---

## 5. Never-pause categories under the focus engine

`NEVER_PAUSE_COMMS` (`budgets.py`) exists for **enforcement safety**: those
processes (shells, compositor, audio, panel itself…) are never migrated into a
frozen cgroup, so a category whose *only* members are never-pause processes is
**unfreezable**.

The CPU engine already encodes an invariant: *you may only bill what you can
freeze* — `category_active` is computed over `freezable_members()`, so a
never-pause-only category never exhausts and never freezes. The focus engine
preserves this invariant exactly:

> A focused window bills seconds into category **C** only when **at least one
> currently-running process matching that window is a freezable member of C**
> (visible after filtering, categorized C, not NEVER-pause, limit configured).

Consequences:

- A focused terminal (`konsole` window; `bash` underneath): `konsole` itself is
  not never-pause and is freezable → bills into its category (`other` via the
  desktop index). This is *new* vs the CPU engine (where shell-heavy windows
  almost never posted deltas) and is honest: a child staring at a terminal *is*
  engaged. The matching rule uses the existing categorization machinery, so the
  same curated overrides and `.desktop` index decide which window is which.
- A focused **lock screen / shell / compositor window**: agent tags the row
  `kind: shell|lockscreen` (KWin reports these windows like any other — wmClass
  such as `kscreenlocker_greet`) and the panel ignores non-`app` kinds. Belt and
  braces, the account_locked gate already zeroes billing while locked.
- A window whose processes are *all* never-pause (pathological; e.g. a game
  that execs only through a `bash` wrapper with nothing else freezable): bills
  nothing under the focus engine — identical to today's CPU engine — so it can
  neither exhaust nor freeze. Documented, accepted, consistent.
- The **freeze on exhaust** path is untouched: it still migrates
  `freezable_members()` of the exhausted category into
  `parental-<user>-<category>` and freezes. Because billing required ≥1
  freezable member, every exhausted category is freezeable. No new privilege,
  no new sudoers surface for freezing.

---

## 6. Suspend / sleep handling

- **Measurement basis is CLOCK_BOOTTIME** (`time.CLOCK_BOOTTIME`, Python ≥3.7),
  which advances across suspend, unlike `CLOCK_MONOTONIC`. Focus-interval
  durations (`since`) are boottime seconds so a suspend gap is detectable.
- **Agent side:** on wake, the agent notes a boottime jump (a `focus` row whose
  `since` would exceed `MAX_FOCUS_SESSION_SECONDS`, default 4 h) → it *closes*
  the open session at the pre-suspend instant and emits its normal heartbeat;
  the panel never sees one suspiciously long interval. Message-level: the agent
  also emits `{"type":"state","state":"suspend"}` when it notices the gap.
- **Panel side (belt and braces):** the existing per-tick charge cap
  `MAX_TICK_WALL_SECONDS` (300 s) stays; the aggregation clips every focus
  interval to the tick window, and a single session is additionally clamped to
  `MAX_FOCUS_SESSION_SECONDS`. A machine asleep for 6 hours produces: no focus
  rows while asleep, resume splits sessions, first tick back bills ≤ the tick
  cap. No phantom hours.
- **Logged-out-but-lingering across reboot:** feed rows carry `boot_id`; a
  changed `boot_id` invalidates any carried-over session and the heartbeat
  refresh restarts cleanly.

---

## 7. Privacy — what the agent records

Minimal-by-construction; the agent is keepable dumb because it never needs to
know limits, categories, or even who it is beyond "the user of this session".

**Records** (one JSON object per row, see §8 schema):

- window identity as reported by the desktop: `wm_class` (e.g.
  `org.mozilla.firefox`) and, when resolvable, `app_id` (`.desktop` id /
  flatpak id),
- focus interval: `since` (boottime seconds this window held focus since the
  previous focus change), session sequence id,
- a `kind` tag: `app` | `shell` | `lockscreen`,
- state/heartbeat rows (no window data),
- error rows (agent-internal, no window data).

**Does NOT record:** window **captions/titles** (which can embed a chat message,
document name or URL), keystrokes, clipboard, screenshots, URL/history, audio,
or any per-site detail. `wm_class` is coarse — "Firefox", not
"youtube.com/…". This keeps the feed PII-light and makes the privacy contract
simple to state and to verify in tests (schema tests assert the forbidden keys
never appear). Adding an opt-in "recent apps with titles" view later is an open
question (§12.5); v1 ships without captions.

**Where it lives:** the feed is written under the child's own
`XDG_RUNTIME_DIR` (mode 0600, owned by the child, tmpfs), so it is readable by
no other local user and does not survive reboot. Aggregate per-category minutes
(as today) are what the panel persists in `state.db` and shows to admins. The
audit log records engine switches and measurement-gap events, not the feed.

---

## 8. Data/contract

### 8.1 Feed channel — chosen: JSONL feed, panel pulls via scoped sudoers

| Channel | Verdict |
|---|---|
| **JSONL feed, `systemd-run --machine=<child>@.host --user cat <feed>`** (chosen) | Panel-driven read; no new listening surface in the panel process; reuses the exact transport family the project already trusts (`notify_user()` runs `sudo systemd-run --machine=<u>@.host --user notify-send …`); the sudoers grant runs the cat **as the child**, so it adds no capability the child doesn't already have. |
| Unix socket (push, SO_PEERCRED uid attribution) | Rejected for v1: adds an *unauthenticated* input path into the panel process (new parsing/DoS surface on a fail-closed, least-privilege codebase). Kept as fallback if the pull transport proves unreliable for lingering headless users (§12.1). |
| Shared group / ACL directory | Rejected: multi-child isolation + SELinux (child processes writing `var_lib_t`) + group-membership sprawl. |
| API token / authenticated POST | Rejected outright: violates "child never authenticates; child drives nothing" (AUTH_DESIGN non-negotiables) and drags in credential handling. |

**Transport mechanics.** On every budget tick, for each user with
`focus_engine=agent`, the panel runs (as today's notifications do, via sudo):

```text
sudo systemd-run --machine=<child>@.host --user --wait --collect \
  cat /run/user/<uid>/parental-controls/focus.jsonl
```

- Output is parsed line-by-line; only rows with `seq > entry.feed_seq` are new.
  The panel never streams unboundedly: feed ≤ 128 KB, panel drains each tick.
- Failure of the drain command (no user manager, transport error) → treated as
  **stale heartbeat** → fail-closed §10, not "bill zero" and not a crash.
- **Sudoers** (rendered by `install.sh`, `visudo -c`-checked, deployment-tested):
  extend `sudoers-parental-controls` with a grant for
  `systemd-run ^--machine=[A-Za-z0-9_.-]+@[.]host --user --wait --collect cat /run/user/[0-9]+/parental-controls/focus[.]jsonl$`
  (both `/usr/sbin` and `/usr/bin`). The payload is fixed (`cat` + exact path
  shape + exact flags); the run-as target is the child themselves, so the grant
  grants the service user nothing the child could not already read, and it
  authorizes **no other command** (deployment tests pin the negative cases, in
  the style of `test_systemd_run_rule_is_scoped_to_the_machine_transport`).

Rationale for the file-over-IPC choice: the panel remains the only process that
opens and parses the feed, attachable to the existing 60 s tick with no new
threads, and the child's write path can't reach `state.db` or the panel socket.

### 8.2 Feed schema (v1)

One JSON object per line, written by the agent, in
`/run/user/<uid>/parental-controls/focus.jsonl`:

```jsonc
// focus row — one per focus change (plus one at agent start)
{"v":1,"seq":412,"agent":"0.1.0","boot_id":"<journald-style boot id>",
 "ts":"2026-10-11T21:03:45.000+00:00","bt":91234.56,
 "type":"focus","session":17,
 "wm_class":"org.mozilla.firefox","app_id":"firefox.desktop",
 "kind":"app","since":187.3}
// since = boottime seconds the PREVIOUS window held focus (interval end at ts)

// state row — heartbeat AND mode signal; agent emits ~every 30 s, always
{"v":1,"seq":415,"agent":"0.1.0","boot_id":"<…>","ts":"…","bt":…,
 "type":"state","state":"active|no_session|suspend|no_engine|locked"}
// "active" = graphical session running + focus engine attached; heartbeats double as liveness.

// error row — agent self-diagnosis, never window data
{"v":1,"seq":416,"agent":"0.1.0","boot_id":"<…>","ts":"…","bt":…,
 "type":"error","code":"kwin_subscribe_failed","msg":"falling back to poll"}
```

Rules the panel enforces while parsing (all unit-tested):

- `v == 1` and known `type`; unknown/malformed rows are skipped and counted
  (a parse-error counter is surfaced in the user's budget status).
- `seq` strictly increases across rows; a row with `seq <= feed_seq` is ignored;
  a *gap* (seq jumps) is tolerated for 300 s then treated as stale.
- `boot_id` change → discard any carried-over session state, refresh cursor.
- Size limits: line ≤ 4 KB, file ≤ 128 KB, ≤ 200 rows per 60 s drain (rate-limit
  a runaway agent).
- `wm_class`/`app_id` must match `^[A-Za-z0-9_.\-+]+$` (no path/control chars);
  `kind` in `app|shell|lockscreen`; `since` finite, `0 ≤ since ≤
  MAX_FOCUS_SESSION_SECONDS`.

### 8.3 Panel-side aggregation (pure, stdlib-only)

New pure function in `budgets.py`:

```python
def aggregate_focus(rows, window, procs, limits, prefs=None, index=None):
    """rows: new feed rows since the cursor; window: (start_dt, end_dt) tick
    window (clipped to the day/week slice); procs: live ps snapshot needed for
    the freezable-member gate.
    Returns {category: focused_seconds} and a clamped total."""
```

Rules:

1. Reconstruct focus intervals from consecutive `focus` rows (`since` +
   `ts`); a final open session is extrapolated **only** to the heartbeat
   timestamp, never past it.
2. Map `wm_class`/`app_id` → category via the **existing** machinery:
   `CATEGORY_OVERRIDES` keyword regexes over `wm_class + app_id`, then the
   `.desktop` index (`build_desktop_index`) / desktop Categories tokens, else
   `other` — mirrored from `categorize()` so the panel and the UI can never
   disagree.
3. **Freezable gate** (§5): seconds count for category C only when ≥1 process
   in `procs` matching the window is a freezable member of C
   (`freezable_members()` reused as-is). `kind != app` rows contribute nothing.
4. Clip intervals to `window`; clamp the per-tick total to
   `MAX_TICK_WALL_SECONDS` (kept, sleep-hardening) and any single session to
   `MAX_FOCUS_SESSION_SECONDS`.
5. Return per-category float seconds — the drop-in replacement for the
   `category_active` branch of `account_and_enforce()`.

### 8.4 Budget-entry / flag changes

`blank_entry()` gains (defaults preserve current behavior exactly):

```python
'focus_engine': 'cpu',   # 'cpu' | 'agent' | 'no_data' (auto-degraded)
'feed_seq': 1,           # feed cursor watermark
'feed_last_seen': None,  # ISO ts of last valid heartbeat
'feed_agent': None,      # agent version string (display/health)
```

`last_cpu` **keeps advancing every tick regardless of engine** — it is the
rollback baseline and costs ~nothing.

---

## 9. Interplay with the existing tick, freezer, day reset, weekly windows

- **Tick loop:** unchanged. `tick_budgets()` still iterates users, still takes
  `procs_provider`, `account_locked`, `notify`, `run_watchdog`. A new
  `focus_provider` callable (`fetch_focus(username, entry, window)`) is injected
  exactly like `procs_provider`; tests substitute a fake.
- **Billing swap:** inside `account_and_enforce()`, when
  `entry['focus_engine'] == 'agent'` and the feed is fresh,
  `used[category] += focused_seconds[category]` replaces the
  `category_active → += wall` branch. When the engine is `cpu` (default) or
  the feed is degraded (`no_data`), the existing `compute_deltas`/`wall` path
  runs — byte-for-byte the current behavior. Freeze-on-exhaust, low-warn,
  pause-notify, late-start re-freeze, manual-freeze re-assert: **all unchanged**.
- **cgroup freezer:** untouched. Freezing still happens through
  `ensure_cgroup`/`migrate_pid`/`set_frozen`/`verify_freeze_safety` with the
  same sudoers grants. The focus engine only changes *which category accrues*,
  never *how enforcement happens*.
- **Day reset:** untouched — `budget_reset_needed()` still fires at bedtime-end
  (or midnight for no-bedtime users); the aggregator window is re-derived from
  the same `now`/day logic. `feed_seq` is **not** reset by the day reset (the
  watermark is a drain position, not a budget quantity).
- **Locked ticks:** `_locked_tick_bookkeeping()` extends to also advance
  `feed_seq`/`feed_last_seen` (`_locked_feed_bookkeeping()`) so the first tick
  after unlock bills only new focus; `last_cpu` bookkeeping for rollback stays.
- **Weekly windows (future, Phase 4 roadmap):** a weekly budget is a different
  *reset rule + aggregation window*, not a different measurement. The
  aggregator already takes an explicit `window`; a weekly slice is just a wider
  window with a weekly reset rule in `budget_reset_needed`. The agent needs
  **zero** changes.
- **Notifications/warnings:** unchanged; they key off `used_seconds` which the
  focus engine now feeds. "5 minutes left" and "paused" behave as today.

---

## 10. Security

### 10.1 Isolation

- **The agent runs as the child** (user unit, child's own session): it can read
  and write only the child's session state. It cannot read
  `/etc/parental-controls/auth.json` (0600, service-user-owned), the panel's
  `state.db`/`audit.jsonl` (service-user-owned install dir), or any other
  user's `XDG_RUNTIME_DIR` (0700).
- **The agent authenticates nowhere and holds no secrets** — not for the panel,
  not for the desktop (KWin scripting runs inside the *child's own* session bus;
  no polkit, no root). The no-PAM / no-child-login principles are untouched:
  the agent starts inside an **already-established** login session (no
  authentication event), and lock/unlock continues to be `usermod`-driven by the
  panel as root.
- **The panel accepts a feed, never commands.** The drain is a read-only cat of
  a bounded, schema-validated text file parsed line-by-line inside the tick;
  the child's feed rows can never execute in the panel, reach the admin UI
  session store, or touch the auth store.
- **Least privilege:** one new sudoers grant, fixed argv, run-as = the target
  child (no new capability for the child); install.sh re-renders + `visudo -c`
  + the deployment test-suite pins both allow and deny cases (style of
  `tests/test_deployment.py`).
- **User-unit hardening where it applies:** `NoNewPrivileges=yes`,
  `PrivateTmp=yes`, `ProtectSystem=strict`(reads), `RestrictSUIDSGID=yes`;
  user units cannot use the full system-unit set, and the agent is
  unprivileged anyway — the hardening is defense-in-depth, not load-bearing.

### 10.2 Tamper — the honest tradeoff

**A determined child can always under-report.** The agent runs in the child's
own session; the child can kill it, edit its unit, delete the feed, or simply
run the app minimized and do homework in another window (which is *legitimate*
usage under the new rule, not fraud). The CPU proxy was harder to fake only
because it measured from the system side — and it was precisely that
measurement that over-billed idle apps. This is an inherent, stated tradeoff,
not a bug:

1. **We do not claim tamper-proofness.** The UI/README state the contract:
   *"usage is good-faith engaged time measured in the child's session"*.
2. **Fail closed on measurement gaps** — the important part. Missing or stale
   heartbeat (**≠** `no_session`, which is a live heartbeat saying "not at the
   machine") for > 3 min → the panel: stops billing for that user
   (never guesses), flips `focus_engine` to `no_data`, surfaces "usage not
   being measured" prominently in the UI, writes an audit entry, and lets the
   parent choose **Resume CPU fallback** or **re-enable agent**. Absence of
   data is *never* silently treated as zero engaged time (that would make
   "delete the feed" a free pass).
3. **Row sanity is enforced** (monotonic seq, boot_id, duration caps, rate
   limit, charset) so a forged feed is at worst *plausible-looking low usage*,
   not "10,000 hours" or a panel crash.
4. **The audit log** records engine switches and gap events; day-end totals are
   audit-logged as today, so a discrepancy (feed claims 2 h, child visibly on
   machine 6 h) is at least visible to the parent.
5. **Fallback engine stays ready** (§11) so a parent who distrusts agent
   numbers flips one flag back to CPU accounting per child.
6. **A fully-frozen/never-pause category** still cannot be tricked into
   exhausting-and-freezing (billing gated on freezable members) — tamper that
   *over*-reports is structurally impossible from the feed.

---

## 11. Phasing, test strategy, rollout, rollback

### Phasing

| Phase | Scope | Definition of done | Branch |
|---|---|---|---|
| P1 | Agent core: feed writer, state machine, focus backends (poll default; KWin-script event optional), schema. Agent unit tests. No deployment, no panel changes. | All agent unit tests pass; feed rows hand-verified against a scripted fake desktop source. | `feature/session-agent` (worktree `session-agent/`) |
| P2 | Panel ingestion: drain transport + strict parser + cursor + `aggregate_focus()` + `focus_engine` flag plumbing + locked-cursor bookkeeping. Full module tests with fake feed providers. Deployment still not enabled (flag defaults `cpu`). | `account_and_enforce` bills from fake feeds correctly; CPU path byte-identical when flag=cpu; 526 existing tests still green. | same branch |
| P3 | Installer: stage agent + KWin script into `@APP_DIR@`, per-user unit into child config, `loginctl enable-linger`, new sudoers grant, healthcheck probe for feed liveness, uninstall path. Deployment tests (extend `tests/test_deployment.py`, `tests/test_service_user.py`). | `./install.sh` renders units/sudoers with no checkout paths; `visudo -c` passes; `pcverify`/scoped-sudoers verification passes for the new grant. | same branch |
| P4 | Live validation + rollout: enable on the dedicated agent test account (`pcagent` only — never `tester`/`bluefox`/`stan`), verify real KWin focus in a real session, then flip `focus_engine=agent` per child from the admin UI (audit-logged). Headless e2e harness. | Focus engine measures a scripted desktop session within ±tolerance; rollback flip works; gap → `no_data` surfaced. | same branch |

### Test strategy

All stdlib `unittest`, run with `.venv/bin/python` from inside the worktree.
Planned tests by area (≈ **73 new tests**):

| Area | Tests | What they pin |
|---|---|---|
| Feed parser/schema | 8 | valid row; wrong `v`; malformed JSON; unknown type; non-monotonic seq; oversized line; truncated tail tolerated; boot_id change resets state |
| Heartbeat/staleness | 5 | fresh; stale > grace; missing file; mid-drain truncation; rotation/epoch handling |
| `aggregate_focus` | 10 | single window full tick; partial tick overlap; consecutive same-window merge; suspend gap split; per-session cap; per-tick cap; unmappable → `other`+gate; `kind!=app` excluded; bedtime-boundary split; wide (weekly) window |
| Category mapping + freezable gate | 6 | wm_class→category via overrides; via desktop index; freezable member present ⇒ billable; all-never-pause ⇒ not billable; focused-but-no-matching-procs ⇒ not billable; multi-PID match |
| Locked/gone integration | 6 | locked ⇒ cursor advances, zero billed; unlock ⇒ bills only new sessions; locked during gap; engine switch mid-day keeps `used_seconds` sane; `last_cpu` maintained for rollback; locked ⇒ no notify |
| Feed cursor/rotation | 5 | cursor starts at 1; seq gap tolerated then stale; file replaced; cursor ahead of file; single-writer concurrency |
| Engine flag plumbing | 4 | default `cpu`; per-user `agent`; unknown value fail-closed→`cpu`; switch audit-logged |
| Sudoers + transport | 8 | grant regex present for sbin+bin; allows exact panel argv; denies cat-other-paths / other commands / no `--machine`; app uses granted argv; drain-command failure ⇒ no_data (not crash); missing feed ⇒ clean no_data; rendered sudoers passes `visudo -c`; grants nothing else |
| Installer staging | 7 | unit rendered into child config dir; linger idempotent; agent file perms (readable, not child-writable); KWin package files; uninstall removes unit+linger; chown preservation; no checkout path in rendered artifacts |
| Mocked tick | 6 | `tick_budgets` with fake feed provider; freeze still fires on focus-exhaust; watchdog interplay; day reset with feed; `budget_status` roundtrip; CPU path regression |
| Headless e2e harness (P4) | 8 | scripted fake agent over the real drain transport; fake KWin source; end-to-end aggregation + freeze; gap→no_data; rollback flip |

Existing 526 tests must stay green at every phase (P2 explicitly regression-pins
the CPU path). Real-kernel/live aspects (P4) reuse the `pcagent` scratch-account
conventions from AGENTS.md — never `tester`/`bluefox`/`stan`; leave `pcagent`
unlocked at rest.

### Rollout

- Feature-flagged **per user** via `focus_engine` (default `cpu`, i.e. today's
  exact behavior) — shipping changes nothing for a family until a parent opts
  a child in from the admin UI.
- Opt-in per child; each switch is audit-logged (`budget.focus_engine`).
- Enablement requires: child has a KDE session on a supported engine
  (else agent runs, reports `no_engine`, panel keeps `cpu` and flags it).
- Healthcheck (extend `healthcheck.sh`) probes per-child feed freshness and
  surfaces stale agents as a failed/ degraded health state (never notifies a
  child).

### Rollback

- **Instant, per child:** flip `focus_engine` back to `cpu` — `last_cpu`
  baselines were maintained throughout, so the CPU proxy resumes seamlessly with
  no double-billing and no gap in enforcement.
- **Full uninstall:** re-run `install.sh` (removes units, disables linger,
  removes KWin package + feed dirs) or the root CLI
  (`parental-controls focus-uninstall <user>` — CLI recovery pattern).
- **Data:** `state.db` keeps `used_seconds` from whichever engine was active;
  no schema migration is required by v1 (new entry keys are additive; WAL +
  ordered-migrations etiquette preserved if a bump is ever needed).

---

## 12. Open questions with recommendations

1. **Does `systemd-run --machine=<child>@.host --user cat …` work for a
   *lingering, logged-out* user manager?** The notify path today is
   graphical-session-only by nature; a plain `cat` needs only the user manager.
   *Recommendation:* verify first thing in P2 against `pcagent` (linger on,
   no login). If the transport refuses headless users, switch the channel to
   the **socket push + SO_PEERCRED** fallback (§8.1) — the feed schema and
   aggregation are identical either way, so this decision is isolated to the
   drain function + sudoers grant + one deployment test.
2. **KWin source reliability: script vs poll.** A KWin script package gives
   exact events but adds auto-load/`kwinrc` machinery that can break across
   Plasma releases; polling `activeWindow()` every 5 s is robust and needs no
   session hooking. *Recommendation:* ship **poll as the v1 default**; add the
   KWin-script backend as an optional P4 enhancement once the poll path is
   verified in a real session on the Bazzite target.
3. **GNOME/Bazzite-GNOME.** No public focus API. *Recommendation:* v1 is
   KDE-only; other DEs degrade to the CPU engine with a UI note
   (`no_engine`). Revisit when/if the product targets GNOME.
4. **wmClass → `.desktop` identity for flatpaks.** `build_desktop_index()`
   already resolves flatpak ids from cmdlines; window `wm_class` vs `app_id`
   can disagree for some flatpaks (browser webapps etc.). *Recommendation:*
   aggregate by wm_class with app_id as a secondary hint, and add a
   fixture-driven test using real Bazzite flatpaks (Steam, Firefox) captured in
   a session.
5. **Captions/PII (privacy §7).** v1 records none. *Recommendation:* keep it
   that way; if parents later want "recent apps with titles", gate it behind an
   explicit per-child admin opt-in and store captions **on the panel side only**
   (never in the child-readable feed).
6. **Measurement-gap policy.** Options: (i) hold billing + `no_data` UI
   (recommended), (ii) silent CPU fallback, (iii) bill zero. *Recommendation:*
   (i) — (ii) silently changes semantics mid-day, (iii) creates the
   delete-the-feed free pass. Owner to confirm.
7. **Weekly windows (future).** Only a reset rule + aggregation window change;
   the feed and agent are unaffected. No open design question now, tracked in
   Phase 4 roadmap.
8. **Healthcheck scope.** Probes only the panel today. *Recommendation:* extend
   to per-child feed freshness in P3 (never restarts the agent, never notifies a
   child — matching the healthcheck service's existing rules).
9. **Multi-device (Phase 5).** Feeds are per-device; cross-device totals need a
   device dimension in `state.db`. Noted, not designed here.
10. **Audit-trail for feeds.** The panel persists only aggregates; raw feeds
    die with tmpfs. *Recommendation:* acceptable for v1 (aggregates + audit of
    engine switches), revisit if parents request per-app history.

---

## 13. Appendix — implementation map (for the implementing agent)

Files touched (all under `master/` at implementation time; worktree convention):

| File | Change |
|---|---|
| `session_agent.py` (new) | Agent: stdlib-only, feed writer, state machine, poll backend (fallback: KWin-script backend), schema constants, `--dry-run`/`--feed-path` flags for testing. Runs as `--user` service. |
| `budgets.py` | `aggregate_focus()`; `fetch_focus()` provider wiring in `tick_budgets()`; `account_and_enforce()` billing swap behind `focus_engine`; `_locked_feed_bookkeeping()`; `blank_entry()` new keys; new constants `MAX_FOCUS_SESSION_SECONDS`, `FEED_STALE_SECONDS`, feed charset/limits. `compute_deltas`/`last_cpu` kept for rollback. |
| `app.py` | Per-user `focus_engine` admin control (+ audit hook), degraded-status surfacing in `/user/<username>` and API; `notify`/flag plumbing unchanged. |
| `systemd/` (new files) | `parental-session-agent@.service` **template for user units** (rendered per child into `~/.config/systemd/user/`); KWin script package files under `templates/parental-usage-focus/…`. |
| `sudoers-parental-controls` | New scoped `systemd-run … cat <feed>` grant (both sbin/bin). |
| `install.sh` | Stage agent + KWin package; per-user unit render into child config; `loginctl enable-linger <child>`; sudoers render already covered; uninstall path; no checkout paths (use `@APP_DIR@`). |
| `healthcheck.sh` | Per-child feed freshness probe (P3). |
| `tests/` | ~73 new tests per the matrix in §11; extend `test_deployment.py` (sudoers/units), `test_service_user.py`, `test_budgets.py`. |
| `PLAN.md` | Check off "Per-user session agent…" only when P4 closes; note the flag/engine model in decisions. |
| `SESSION_AGENT_PLAN.md` | This file; keep current as implementation proceeds. |
| `RELEASE.md` | Parent-facing note on the measurement contract (§10.2) when shipped. |