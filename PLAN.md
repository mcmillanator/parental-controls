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
- [x] Dedicated /bedtime settings page
- [x] Immediate lock/unlock on bedtime save/clear
- [x] Lock Screen button (locks account + screen without logout)
- [x] Emergency logout endpoint (/api/logout)
- [x] User details page with bedtime settings at top, then account status, then processes (/user/<username>)
- [x] Merge feature/screen-lock to master
- [ ] Systemd service for auto-start (later)
- [ ] Scheduled timers (7pm lock / 10am unlock) (later)
- [ ] Notifications before lock (notify-send via systemd-run) (later)
- [ ] Authentication / authorization (later)
- [ ] Lockout prevention / safeguards (later)
- [ ] Multi-device support (later)
- [ ] Polished UI/UX (later)
