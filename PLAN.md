# Bazzite Parental Controls — Project Plan

## Goal

A web-based UI for manually locking and unlocking user accounts on Bazzite. Prototype phase: focus on function, not form.

## Target Platform

Bazzite (Fedora-based immutable desktop OS)

## Architecture

- **Web UI**: Python Flask app serving on `0.0.0.0:5000`
- **User discovery**: Reads `/etc/passwd` for human users (UID >= 1000)
- **Lock mechanism**: `sudo usermod -L <user>` + `sudo loginctl terminate-user <user>`
- **Unlock mechanism**: `sudo usermod -U <user>`
- **Sudoers**: Dedicated rule in `/etc/sudoers.d/parental-controls` for passwordless operation

## Development Workflow

- `master` branch: project metadata only (README, PLAN, .gitignore)
- Feature worktrees under `.worktrees/` for isolated development
- Agents work in feature branch/worktrees, merge back when complete

## Prototype Scope (Current Phase)

### In Scope
- List all human users on the system dynamically
- Show lock/unlock status per user
- Lock/Unlock buttons with immediate effect
- No hardcoded usernames
- No authentication (user accepts responsibility for power)

### MVP Scope (not prototype) — security and operations gate
The prototype is functionally complete and tagged. Prototype phase ends here: no new
features until the app is safe to leave running. The blocker is that the app is
currently an unauthenticated, network-exposed remote lock/terminate service running
as `stan` with passwordless root sudo — anything that can reach port 5000 can lock any
account or kill any session.

- [ ] Authentication / authorization: parent login with server-side sessions; child
      accounts get no access to the UI or API (design: `AUTH_DESIGN.md`, in progress:
      `feature/mvp-auth`)
      - Two admin tiers, both authenticating with app passwords (system-password/PAM
        auth **rejected — no PAM helper in any version**): `wheel` members
        (**cannot be locked or removed**) and app-only admins (**removable by wheel
        admins**; cannot act on wheel admins)
      - Child accounts never authenticate — not even read-only
      - CLI recovery command ships in the same change, so a lost password can never
        lock a parent out of their own machine
- [ ] Bind `127.0.0.1` by default, with an explicit opt-in flag for LAN access
      (access via localhost + SSH tunnel; no TLS in MVP) — with MVP auth change
- [ ] CSRF protection on every state-changing endpoint — with MVP auth change
- [ ] Production WSGI server (gunicorn or waitress) replacing the Flask dev server
      — with MVP auth change
- [ ] Systemd hardening for the service unit (`NoNewPrivileges`, `ProtectSystem=strict`,
      least-privilege sudoers review) — with MVP auth change
- [ ] Watchdog on freezer state: detect and self-heal a stuck `cgroup.freeze=1`
      (a stuck freeze froze an entire desktop on 2026-10-09; it must self-heal)
      — in progress: `feature/freezer-watchdog`
- [ ] Polished UI/UX, including responsive design via Bootstrap — in progress:
      `feature/bootstrap`

### Post-MVP phases
Phase 3 (hardening, after MVP):
- [ ] TLS when LAN-exposed (session cookies otherwise travel in the clear)
- [ ] Audit log of every action (who locked/unlocked/froze whom, and when) — a parental
      control a parent may need to defend needs a tamper-evident record
- [ ] Unbreakable admin recovery path (CLI/physical escape hatch); manual lock of a wheel
      user currently succeeds behind only a dialog

Phase 4 (data & accuracy):
- [ ] JSON state → SQLite with migrations (atomic writes, corruption recovery, budget history)
- [ ] Per-user session agent for focus-based usage measurement (replaces CPU-delta proxy)
- [ ] Per-app overrides and weekly budget windows (deferred from Phase 1 budgets)

Phase 5 (scale & polish):
- [ ] Multi-device support (multiple Bazzite devices)
- [ ] Child-facing warnings ("10 minutes left" via notify-send)

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

## Test User

- Username: `bluefox`
- Password: `bluefox123`
- UID: 1002

## Files

| File | Purpose |
|------|---------|
| `README.md` | Project overview |
| `PLAN.md` | This file |
| `.gitignore` | Ignores worktrees, Python artifacts |
| `.worktrees/webui/` | Feature worktree for web UI development |

## Status

- [x] Test user created
- [x] Sudoers rule installed
- [x] Git repo initialized with worktree structure
- [x] Web UI built and tested (lock/unlock verified with bluefox)
- [x] Bedtime schedule feature (start/end times, days of week)
- [x] Bedtime settings live on each user's details page (dedicated /bedtime page removed)
- [x] Immediate lock/unlock on bedtime save/clear
- [x] Lock Screen button (locks account + screen without logout)
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
- [x] Bedtime lock ownership: flag set when usermod succeeds regardless of terminate-user result (offline users auto-unlock correctly)
- [x] Pre-lock warnings: 15-min bedtime warning + lock-time notice, budget low/pause warnings via notify-send (feature/notifications)
- [x] One-command installer (`install.sh`: venv, templated units/sudoers, auth dir, enable, verify) + deploy docs
- [x] Lock Screen targets graphical user sessions only (manager/tty sessions report "no lockable session" instead of failing)
- [x] Unlocked accounts in bedtime show manual Lock/Lock Screen buttons (no dead-end disabled button)
