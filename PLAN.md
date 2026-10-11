# Bazzite Parental Controls — Project Plan

## Goal

A web-based UI for parent-controlled account lock/unlock, bedtimes and per-category
app time budgets on Bazzite. The prototype phase and the MVP security/operations
gate are both **complete and merged** — this branch is the release-ready 1.0
baseline (parent-facing summary: `RELEASE.md`).

## Target Platform

Bazzite (Fedora-based immutable desktop OS). First version this release was
verified running on: **Bazzite 44.20261002.0 (Kinoite)**, `BUILD_ID="Stable (F44.20261002)"`.

## Architecture

- **Web UI**: Python Flask app served by waitress, bound to `127.0.0.1:5000` by
  default (LAN bind is an explicit opt-in)
- **Auth**: app-only admin logins with server-side sessions, two tiers
  (`wheel` members + app-only admins); no PAM anywhere. CSRF token required on
  every state-changing request
- **User discovery**: Reads `/etc/passwd` for human users (UID >= 1000)
- **Lock mechanism**: `sudo usermod -L <user>` + screen-lock of the user's graphical sessions (`loginctl lock-session`, session preserved). Every lock path — manual Lock, `/api/lock-screen`, bedtime auto-lock, bedtime-save enforce, bonus grant/revoke/expiry — preserves the session. Session termination (`loginctl terminate-user`) lives ONLY in the emergency-logout endpoint (`/api/logout`)
- **Unlock mechanism**: `sudo usermod -U <user>`
- **Sudoers**: Dedicated rule in `/etc/sudoers.d/parental-controls` for passwordless operation
- **Deployment**: code is staged into the install dir (`@APP_DIR@`, default
  `/opt/parental-controls`); runtime state stays beside the code and is never
  overwritten by a redeploy

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
- [ ] TLS when LAN-exposed (session cookies otherwise travel in the clear)
- [ ] Audit log of every action (who locked/unlocked/froze whom, and when) — a parental
      control a parent may need to defend needs a tamper-evident record. Today's audit
      trail is only the per-user bonus history (last 20 entries, `bonus_history`)
- [ ] Sudo audit: retire the blanket passwordless-sudo rule on the service account so
      the scoped `/etc/sudoers.d/parental-controls*` rules are actually the only path to
      root. While a blanket `NOPASSWD: ALL` for the service user exists, every scoped
      rule is masked and least privilege is unverifiable in practice (tracked on
      `feature/sudo-audit`)
- [x] Admin recovery escape hatch: `cli.py` (`unlock`, `unfreeze-all`,
      `reset-password`, `add-admin`, `remove-admin`, `lock-admin`) runs as root and
      works even if the app is wedged; wheel targets are refused in the handler
      itself, not just hidden in the UI
- [ ] Physical escape hatch for a machine with no admin credentials at all (CLI still
      assumes someone can get a root shell)

Phase 4 (data & accuracy):
- [ ] JSON state → SQLite with migrations (atomic writes, corruption recovery, budget history)
- [ ] Per-user session agent for focus-based usage measurement (replaces CPU-delta proxy)
- [ ] Per-app overrides and weekly budget windows (deferred from Phase 1 budgets)

Phase 5 (scale & polish):
- [ ] Multi-device support (multiple Bazzite devices)
- [x] Child-facing warnings ("10 minutes left" via notify-send) — shipped as the
      pre-lock/pre-freeze warning path (bedtime, budget-low and bonus messages)

### Completed in prototype phase
Systemd service + auto-start, bedtime enforcement timer, notifications before lock,
lockout safeguards (confirm dialogs, wheel-admin auto-lock/auto-freeze exemption).

### Approved decisions (2026-10-07, from process-filtering research review)
- Enforcement builds on the existing `parental-*-games` / `parental-*-quarantine` cgroup experiment (cgroup freezer, not per-PID signals)
- Per-category budgets first; apps displayed grouped by category; per-app overrides deferred to a later phase
- Paused apps auto-resume at the next window (plus a manual Resume button for the parent)
- Budget day resets at bedtime end (midnight fallback when no bedtime is set)
- Background-idle must NOT burn budget (rules out raw ETIME accounting); CPU-time deltas until a session agent exists
- Enforcement may eventually need a per-user session agent (focus time, warnings) — not this phase

### Approved decisions (2026-10-10, bonus time — parent)
- **No stacked cap**: a new grant adds onto an active grant (`bonus_until` moves later).
  There is no maximum total.
- Custom entry is **hours + minutes**; the server re-validates and enforces a
  **5-minute minimum** (`BONUS_CUSTOM_MIN`)
- Shortcut denominations stay **15 / 30 / 60 / 90 / 120 minutes**
- History keeps the **last 20** entries per user (`bonus_history`, feeding the future
  audit log)
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
| `RELEASE.md` | Release 1.0 notes, deploy/rollback guide (parent-facing) |
| `QA_LOCK_UNLOCK.md` | QA report for the lock/unlock core mechanisms |
| `AUTH_DESIGN.md` | Authentication/authorization design (MVP auth) |
| `BONUS_TIME_PLAN.md` | Bonus-time design |
| `PROCESS_GROUPING_PLAN.md` | Process-grouping design |
| `.gitignore` | Ignores worktrees, Python artifacts and runtime state (`*.json`) |

## Release status

**1.0 — release-ready.** Code baseline: `master` @ `24fb669`. First version this
release was verified running on: **Bazzite 44.20261002.0 (Kinoite)**,
`BUILD_ID="Stable (F44.20261002)"`. Parent-facing summary, deploy steps and the
rollback story live in `RELEASE.md`.

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
