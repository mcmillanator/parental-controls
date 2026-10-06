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
- [ ] Web UI built and tested
- [ ] Systemd service for auto-start (later)
