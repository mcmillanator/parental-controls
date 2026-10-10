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

### Out of Scope (Later Phases)
- Authentication / authorization
- Lockout prevention / safeguards
- Scheduled timers (7pm lock, 10am unlock)
- Notifications before lock
- Polished UI/UX
- Multi-device support

### Planned (Post-Prototype)
- Systemd service to auto-start the Flask app on boot
- Scheduled timers (e.g., cron or systemd timer) to enforce lock/unlock at fixed times
- Lockout prevention / safeguards
- Notifications before lock (e.g., notify-send via systemd-run)
- Multi-device support (multiple Bazzite devices)
- Polished UI/UX

### MVP Scope (not prototype)
- Polished UI/UX, including responsive design via Bootstrap (add Bootstrap CSS/JS to templates and be done; still prototype phase, so no custom design work)

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
- [ ] Systemd service for auto-start (later)
- [ ] Scheduled timers (7pm lock / 10am unlock) (later)
- [ ] Authentication / authorization (later)
- [x] Lockout prevention / safeguards: confirm dialogs on destructive buttons, Emergency logout button, wheel admins never auto-locked/auto-frozen
- [ ] Multi-device support (later)
- [ ] Polished UI/UX, including responsive design via Bootstrap (MVP)
- [x] Process filtering: curated denylist + per-row Hide / Show-hidden toggle (`process_prefs.json`)
- [x] Apps grouped by category on the details page (`.desktop` Categories + curated overrides)
- [x] Per-category daily time budgets with cgroup-freezer pause, auto-resume at reset, manual Resume button
- [x] Pre-lock warnings: 15-min bedtime warning + lock-time notice, budget low/pause warnings via notify-send (feature/notifications)
