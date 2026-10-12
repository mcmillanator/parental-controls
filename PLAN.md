# Bazzite Parental Controls — Project Plan

## Goal

A web-based UI for parent-controlled account lock/unlock, bedtimes and per-category
app time budgets on Bazzite. The prototype phase, the MVP security/operations gate
and the post-MVP hardening wave (SQLite state, tamper-evident audit log, manual
freeze buttons, TLS for LAN binds, health check, dedicated service user) are all
**complete and merged** — this branch is the release-ready 1.1 baseline
(parent-facing summary: `RELEASE.md`).

## Target Platform

Bazzite (Fedora-based immutable desktop OS). First version this release was
verified running on: **Bazzite 44.20261002.0 (Kinoite)**, `BUILD_ID="Stable (F44.20261002)"`.

## Architecture

- **Web UI**: Python Flask app served by waitress, bound to `127.0.0.1:5000` by
  default (LAN bind is an explicit opt-in). A loopback bind stays byte-identical
  waitress HTTP; a LAN bind is refused unless it serves HTTPS through cheroot with
  an install-time self-signed certificate (`--tls`, never auto-regenerated) or the
  operator knowingly passes `--insecure-lan` (persistent warning banner). Loopback
  has no TLS by design — it is reached over an SSH tunnel
- **Runtime state**: SQLite (`state.db`, WAL, `schema_version`, ordered
  migrations, backup-before-migrate) holding the bedtime/budgets/process_prefs
  documents keyed by user. Atomic JSON mirrors are a best-effort rollback copy
  only (never read back). `auth.json` deliberately stays a separate fail-closed
  credential store
- **Audit log**: append-only hash-chained JSONL (`audit.jsonl`) for
  lock/unlock/logout/bedtime/bonus/budget/admin actions, exposed admin-only via
  `GET /api/audit`. Tamper-evident, not encrypted; logging never blocks
  enforcement
- **Auth**: app-only admin logins with server-side sessions, two tiers
  (`wheel` members + app-only admins); no PAM anywhere. CSRF token required on
  every state-changing request
- **User discovery**: Reads `/etc/passwd` for human users (UID >= 1000)
- **Lock mechanism**: `sudo usermod -L <user>` + screen-lock of the user's graphical sessions (`loginctl lock-session`, session preserved). Every lock path — manual Lock, `/api/lock-screen`, bedtime auto-lock, bedtime-save enforce, bonus grant/revoke/expiry — preserves the session. Session termination (`loginctl terminate-user`) lives ONLY in the emergency-logout endpoint (`/api/logout`)
- **Unlock mechanism**: `sudo usermod -U <user>`
- **Sudoers**: Dedicated rule in `/etc/sudoers.d/parental-controls` for passwordless operation
- **Service user**: the panel and the enforcement/healthcheck oneshots run as
  the dedicated system user `parental-controls` (nologin shell, no home,
  locked password, no sudo of its own — only the scoped sudoers grants).
  The shared human/agent login account never owns this network-exposed
  process; `install.sh` creates the account idempotently (`--user` overrides)
- **Deployment**: code is staged into the install dir (`@APP_DIR@`, default
  `/opt/parental-controls`); runtime state stays beside the code and is never
  overwritten by a redeploy. `install.sh` also installs the root-only recovery
  CLI at `/usr/local/bin/parental-controls` (0750) and enables a health-check
  timer that probes the panel every 5 minutes
- **Monitoring**: `parental-controls-healthcheck.timer` probes unit state, an
  HTTP/HTTPS 200/302 on `/`, and auth-store readability/parseability every 5
  minutes, surfacing breakage as a failed unit. It never notifies a child and
  never restarts the panel

## Development Workflow

- `master` branch: the app code (app under master/ is the build target)
- Feature worktrees are sibling directories in the project root (`<name>/`), one per branch, merged back to `master` when complete
- Agents work in feature branch/worktrees, merge back when complete

## Prototype Scope (phase closed)

### In Scope
- List all human users on the system dynamically
- Show lock/unlock status per user
- Lock/Unlock buttons with immediate effect
- No hardcoded usernames
- No authentication — *prototype only*; superseded by the app-only admin auth in the MVP gate below

### MVP Scope (not prototype) — security and operations gate — **COMPLETE**
The prototype is functionally complete and tagged, and the security gate that
made leaving the service running safe is now closed: every item below is
implemented, tested and merged to `master` (the working branches
`feature/mvp-auth`, `feature/freezer-watchdog` and `feature/bootstrap` were
merged and deleted; nothing in this section is in progress).

- [x] Authentication / authorization: parent login with server-side sessions; child
      accounts get no access to the UI or API (design: `AUTH_DESIGN.md`)
      - Two admin tiers, both authenticating with app passwords (system-password/PAM
        auth **rejected — no PAM helper in any version**): `wheel` members
        (**cannot be locked or removed**) and app-only admins (**removable by wheel
        admins**; cannot act on wheel admins)
      - Child accounts never authenticate — not even read-only
      - CLI recovery command ships alongside the app, so a lost password can never
        lock a parent out of their own machine
- [x] Bind `127.0.0.1` by default, with an explicit opt-in flag for LAN access
      (access via localhost + SSH tunnel; no TLS in MVP)
- [x] CSRF protection on every state-changing endpoint
- [x] Production WSGI server (waitress) replacing the Flask dev server
- [x] Systemd hardening for the service unit (`ProtectSystem=strict` with
      directory-level `/etc` writable, `PrivateTmp`, kernel/hostname/clock
      protections; least-privilege sudoers review)
- [x] Watchdog on freezer state: detect and self-heal a stuck `cgroup.freeze=1`
      (a stuck freeze froze an entire desktop on 2026-10-09) — runs on every
      budget tick, in both the app's 60s thread and the systemd enforce timer
- [x] Polished UI/UX, including responsive design via Bootstrap

### Post-MVP phases (open roadmap)
Phase 3 (hardening, after MVP):
- [x] TLS when LAN-exposed — **shipped.** Plain-HTTP LAN binds are refused by the
      installer; `--tls` serves cheroot+HTTPS with an install-time self-signed
      certificate (0600, never auto-regenerated, `--tls-cert`/`--tls-key` override,
      TOFU fingerprint printed, `--regen-tls-cert` to rotate), `--insecure-lan`
      knowingly stays on HTTP with a persistent banner. Loopback intentionally
      remains plain waitress HTTP
- [x] Audit log of every action — **shipped** (`audit.py`, `audit.jsonl`).
      Append-only, hash-chained JSONL (sha256 over each entry including the
      previous hash) with hooks on lock/unlock/logout/bedtime/bonus/budget/admin
      actions and an admin-only `GET /api/audit?limit=N` returning the newest
      entries plus `chain_ok`. Tamper-*evident*, not encrypted; logging is
      best-effort and never blocks enforcement
- [ ] Sudo audit: retire the blanket passwordless-sudo rule on the dev machine so
      the scoped `/etc/sudoers.d/parental-controls*` rules are actually the only path to
      root. The panel itself already runs as the dedicated `parental-controls`
      user (never covered by any blanket); the remaining blanket is
      `stan-nopasswd` on the shared human/agent ops account, which still masks
      scoped-rule verification on dev. Containment work (argument-regex
      hardening of the trailing-wildcard grants) exists; its landing and
      verification are tracked on `feature/sudo-audit` and will be recorded in
      `sudo-audit.md` by that work
- [x] Admin recovery escape hatch: `cli.py` (`unlock`, `unfreeze-all`,
      `reset-password`, `add-admin`, `remove-admin`, `lock-admin`) runs as root and
      works even if the app is wedged; wheel targets are refused in the handler
      itself, not just hidden in the UI. `install.sh` installs it as
      `/usr/local/bin/parental-controls` (root-only, 0750)
- [ ] Physical escape hatch for a machine with no admin credentials at all (CLI still
      assumes someone can get a root shell)

Phase 4 (data & accuracy):
- [x] JSON state → SQLite with migrations — **shipped** (`store.py`,
      `state.db`). One table per store with **per-field granularity**
      (one row per `(username, field)`, one stable TEXT value type,
      `load_*` reconstructs the identical whole-document dict shape),
      `schema_version` + ordered migrations (v1 whole-document → v2
      per-field → v3 whole-document `overrides` config table), WAL +
      busy timeout for the writers, backup-before-migrate
      (`state.db.bak.<version>`), fail-closed corruption recovery, and a
      one-time legacy-JSON import. Atomic JSON mirrors remain as the
      rollback copy. A corrupt store fails closed rather than being
      silently replaced
- [ ] Per-user session agent for focus-based usage measurement (replaces CPU-delta proxy)
- [x] Per-app overrides and weekly budget windows (deferred from Phase 1
      budgets) — **shipped** (`feature/overrides`, `OVERRIDES_PLAN.md`,
      schema v3). Per-`comm` exemption/allowance overrides and recurring
      weekly day/time windows whose allowance replaces the daily limit
      while active (overnight wrap, auto-resume at close). New
      `parental-<user>-app-<comm>` cgroups (watchdog + daily-reset
      aware), fail-closed config parsing, POST endpoints with
      CSRF/wheel protection, live-payload/UI surfacing, and CLI
      `unfreeze-all` coverage for per-app groups

Phase 5 (scale & polish):
- [ ] Multi-device support (multiple Bazzite devices)
- [x] Child-facing warnings ("10 minutes left" via notify-send) — shipped as the
      pre-lock/pre-freeze warning path (bedtime, budget-low and bonus messages)

### Open after 1.1 (deliberately not done)

- **Per-field state is normalized, but writers still resolve last-writer-wins.** The
  v2 SQLite tables store one row per `(username, field)`; conflicting writers
  still resolve last-writer-wins (narrowed by WAL + `BEGIN IMMEDIATE`, not
  eliminated), and whole-table saves from `load_* → mutate → save_*` callers
  rewrite only the users present in the payload.
- **The audit log is hash-chained, not encrypted.** Anyone who can read
  `audit.jsonl` can read every entry; the chain proves tampering, it does not
  hide content.
- **No TLS on loopback**, by design — localhost is reached over an SSH tunnel.
- **The recovery CLI assumes a root shell** (a physical escape hatch for a machine
  with no admin credentials at all is still open, see Phase 3).
- **The `stan-nopasswd` blanket remains on this dev VM by owner decision**; it is
  an ops aid, not something the installer ever creates.

### Completed in prototype phase
Systemd service + auto-start, bedtime enforcement timer, notifications before lock,
lockout safeguards (confirm dialogs, wheel-admin auto-lock/auto-freeze exemption).

### Approved decisions (2026-10-07, from process-filtering research review)
- Enforcement builds on the existing `parental-*-games` / `parental-*-quarantine` cgroup experiment (cgroup freezer, not per-PID signals)
- Per-category budgets first; apps displayed grouped by category; per-app overrides deferred to a later phase
- Paused apps auto-resume at the next window (plus a manual Resume button for the parent)
- Budget day resets at bedtime end (midnight fallback when no bedtime is set)
- Background-idle must NOT burn budget (rules out raw ETIME accounting); CPU-time deltas until a session agent exists. A **locked** account (manual Lock, Lock Screen, bedtime auto-lock) must NOT burn budget either — locked ticks advance the accounting baselines without billing, so the first tick after unlock bills only new activity, never the locked window
- Enforcement may eventually need a per-user session agent (focus time, warnings) — not this phase

### Approved decisions (2026-10-10, bonus time — parent)
- **No stacked cap**: a new grant adds onto an active grant (`bonus_until` moves later).
  There is no maximum total.
- Custom entry is **hours + minutes**; the server re-validates and enforces a
  **5-minute minimum** (`BONUS_CUSTOM_MIN`)
- Shortcut denominations stay **15 / 30 / 60 / 90 / 120 minutes**
- History keeps the **last 20** entries per user (`bonus_history`); the
  tamper-evident audit log records grant/revoke/expiry independently
- A **leftover bonus is cleared when the bedtime window ends** — it is meaningless past
  the scheduled unlock and must not sit there as a stale "Bonus active"
- A manual lock still wins: it cancels an active bonus (`lock_cancelled` history entry)

## Test User

- Username: `bluefox`
- Password: `bluefox123`
- UID: 1002

## Files

| File | Purpose |
|------|---------|
| `README.md` | Project overview, setup, API reference |
| `PLAN.md` | This file |
| `RELEASE.md` | Release notes, deploy/rollback guide (parent-facing) |
| `QA_LOCK_UNLOCK.md` | QA report for the lock/unlock core mechanisms |
| `AUTH_DESIGN.md` | Authentication/authorization design (MVP auth) |
| `BONUS_TIME_PLAN.md` | Bonus-time design |
| `PROCESS_GROUPING_PLAN.md` | Process-grouping design |
| `FREEZE_BUTTONS_PLAN.md` | Manual freeze/unfreeze buttons design |
| `TLS_PLAN.md` | TLS-for-LAN design |
| `sudo-audit.md` | Sudo coverage audit and blanket-removal procedure |
| `katalon/README.md` | Browser (Katalon Recorder) QA suite guide |
| `.gitignore` | Ignores worktrees, Python artifacts and runtime state (`*.json`) |

## Release status

**1.1 — release-ready.** Code baseline: `master` @ `108392d` (plus these
documentation updates). First version this release was verified running on:
**Bazzite 44.20261002.0 (Kinoite)**, `BUILD_ID="Stable (F44.20261002)"`.
Test suite: **526 tests, 9 environmental skips** (22 test files, stdlib
`unittest`). Parent-facing summary, deploy steps and the rollback story live in
`RELEASE.md`.

## Status

- [x] Test user created
- [x] Sudoers rule installed
- [x] Git repo initialized with worktree structure
- [x] Web UI built and tested (lock/unlock verified with bluefox)
- [x] App-only admin authentication with two admin tiers + CLI recovery, child accounts
      can never authenticate (`AUTH_DESIGN.md`)
- [x] CSRF token on every state-changing request
- [x] waitress WSGI server replaces the Flask dev server; binds `127.0.0.1` by default
      with an explicit `--allow-lan` / `--bind` opt-in
- [x] Systemd hardening for the service unit (`ProtectSystem=strict` + directory-level
      `/etc` writable; `NoNewPrivileges` deliberately omitted — passwordless sudo
      children are the enforcement path)
- [x] Freezer watchdog: a genuinely stuck `cgroup.freeze=1` with no paused budget state
      is thawed automatically, only for `parental-<user>-<category>` groups
- [x] Bootstrap UI (responsive) — Bootstrap 5 is loaded from a CDN
- [x] Notification warnings reach the child's session via
      `sudo systemd-run --machine=<user>@.host --user notify-send`, backed by a scoped
      sudoers grant (verified working under scoped sudoers)
- [x] Lock regression fix: every lock path (manual Lock, `/api/lock-screen`, bedtime
      auto-lock, bedtime-save enforce, bonus grant/revoke/expiry) preserves the session;
      only Emergency logout (`/api/logout`) runs `loginctl terminate-user`
- [x] All clock times displayed in 12-hour format
- [x] Bedtime schedule feature (start/end times, days of week)
- [x] Bedtime settings live on each user's details page (dedicated /bedtime page removed)
- [x] Immediate lock/unlock on bedtime save/clear
- [x] Lock action locks the account + screen without logging the child out (the
      `/api/lock-screen` endpoint does the same; the Lock button covers it)
- [x] Emergency logout endpoint (/api/logout)
- [x] User details page with bedtime settings at top, then account status, then processes (/user/<username>)
- [x] Process elapsed time shown in minutes/hours on details page
- [x] Merge feature/screen-lock to master
- [x] Clear bedtime button works via AJAX with toast + refresh (no raw JSON page)
- [x] Auto-refresh pauses while form inputs are focused (no lost form data)
- [x] Manual override precedence: unlocking during bedtime stays unlocked until manual lock or next bedtime occurs
- [x] No page auto-refresh: targeted DOM updates every 10s (table rebuild on main page; status/process/budget cells on details page; forms never touched)
- [x] Systemd service for auto-start
- [x] Scheduled timers (bedtime enforcement every 60s)
- [x] Lockout prevention / safeguards: confirm dialogs on destructive buttons, Emergency logout button, wheel admins never auto-locked/auto-frozen
- [x] Multi-device support → moved to Phase 5 (post-MVP)
- [x] Polished UI/UX → moved to MVP scope
- [x] Process filtering: curated denylist + per-row Hide / Show-hidden toggle (`process_prefs.json`)
- [x] Apps grouped by category on the details page (`.desktop` Categories + curated overrides)
- [x] Per-category daily time budgets with cgroup-freezer pause, auto-resume at reset, manual Resume button
- [x] Bedtime lock ownership: flag set when usermod succeeds regardless of screen-lock result (offline users auto-unlock correctly); locks never terminate the session
- [x] Pre-lock warnings: 15-min bedtime warning + lock-time notice, budget low/pause warnings via notify-send (feature/notifications)
- [x] ProtectSystem=strict with directory-level /etc writable (usermod regression fix; per-file shadow binds denied by SELinux; `full` also mounts /etc read-only)
- [x] Sudoers least-privilege repair: installer replaces `@SERVICE_USER@`, sbin+bin paths for all binaries, `passwd` scoped to `-S` (was masked by stan-nopasswd blanket on dev)
- [x] Lock Screen targets graphical user sessions only (manager/tty sessions report "no lockable session" instead of failing)
- [x] Unlocked accounts in bedtime show an enabled manual Lock button (no dead-end disabled button)
- [x] Parent-granted bonus time past bedtime: 5 one-click shortcuts (15/30/60/90/120 min) + a custom hours+minutes entry (minimum 5 min), tracked in `bedtime.json` with a capped audit trail (last 20 entries); grants **stack with no upper cap**, suppress the bedtime lock until expiry then re-lock in-window, leftover bonus is cleared when the bedtime window ends; manual lock cancels, revoke re-locks inline (`feature/bonus-time`, `BONUS_TIME_PLAN.md`)
- [x] Duplicate processes grouped into one expandable row per category with a count badge and max elapsed; click toggles individual PIDs; expand state survives the 10s refresh; per-comm Hide at group level (`feature/process-grouping`, `PROCESS_GROUPING_PLAN.md`)
- [x] Processes table auto-hides everything not explicitly user-started (pipewire hidden by default, Firefox visible) with human-friendly app names from `.desktop` files where known; `shown_comm` re-shows, `hidden_comm` still hides, budgets/freezer matching untouched (`feature/auto-hide`)
- [x] Runtime state migrated from JSON files to SQLite `state.db` (WAL, `schema_version`, ordered migrations, backup-before-migrate, fail-closed corruption recovery, one-time legacy import); atomic JSON mirrors kept as the rollback copy; `auth.json` stays a separate fail-closed store (`feature/sqlite`, `store.py`)
- [x] Tamper-evident audit log: append-only hash-chained JSONL `audit.jsonl` with hooks on lock/unlock/logout/bedtime/bonus/budget/admin actions and an admin-only `GET /api/audit?limit=N` (entries + `chain_ok`); never raises, never blocks enforcement (`feature/audit`)
- [x] Manual Freeze/Unfreeze toggle in each budget Status cell and on visible process-group rows (`POST /api/budgets/freeze`, `/api/budgets/unfreeze`); `manual_frozen` is distinct from budget-exhaustion `paused` and is released by the daily reset or by clearing the limit (`feature/freeze-buttons`, `FREEZE_BUTTONS_PLAN.md`)
- [x] TLS for LAN binds: plain-HTTP LAN binds refused by the installer; `--tls` serves HTTPS via cheroot with an install-time self-signed cert (0600, never auto-regenerated, `--tls-cert`/`--tls-key` override, TOFU fingerprint printed), `--insecure-lan` for the deliberate plain-HTTP case; loopback stays byte-identical waitress HTTP (`feature/tls`, `TLS_PLAN.md`)
- [x] Health check: `parental-controls-healthcheck.timer` probes every 5 minutes (unit active, HTTP/HTTPS 200/302 on `/`, auth store parseable by the service user) and surfaces breakage as a failed unit; it never notifies a child and never restarts the panel (`feature/health-check`)
- [x] Dedicated service user: the panel and its oneshots run as system user `parental-controls` (nologin, locked password, no sudo of its own, scoped sudoers only), created idempotently by `install.sh`; `pcverify` keeps the scoped least-privilege verification role
- [x] Recovery CLI installed by `install.sh` at `/usr/local/bin/parental-controls` (root-only, 0750), so the escape hatch exists on-machine without the checkout
- [x] Katalon Recorder browser QA suite (`katalon/objects`, `katalon/test-cases`, `katalon/README.md`) plus a CI validator (`tests/test_katalon_objects.py`) that pins every selector and expected string against `templates/` and `app.py` (`feature/katalon-qa`)
