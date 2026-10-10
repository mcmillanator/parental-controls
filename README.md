# Bazzite Parental Controls — Web UI

A web UI for managing user account lock/unlock, bedtimes, and per-category
app time budgets on Bazzite.

## Features

- Lists all human users on the system (UID >= 1000, discovered dynamically)
- Shows lock/unlock status for each user
- Lock button: locks the account (`usermod -L`) and the screen, session preserved
- Unlock button: runs `sudo usermod -U <user>`
- Emergency logout button: locks the account AND terminates all sessions
- Per-user details pages: bedtime schedule, account status, processes grouped
  by app category with Hide/Show-hidden filtering
- Per-category daily time budgets enforced with the cgroup freezer
  (pause-not-kill, auto-resume at reset)
- Bedtime windows with pre-lock warnings and manual-override precedence
- Status/process/budget cells update in place every 10 seconds (no page reloads;
  forms are never touched); errors display as toasts

## Requirements

- Python 3
- Flask, argon2-cffi, waitress (see `requirements.txt`)
- sudo access (the app runs `usermod` and `loginctl` via sudo)

## Authentication (MVP)

Two admin tiers, both with **app passwords only** (never Linux passwords;
no PAM anywhere). `wheel` members are admins automatically once they have
a login row; app-only admins have no system account. Child accounts can
never log in. Full design: `AUTH_DESIGN.md`.

First run bootstraps the store (`/etc/parental-controls/auth.json`, or
`$PARENTAL_CONTROLS_AUTH` for dev/test): if the invoking user is a wheel
member, a one-time initial password is printed to stdout (change it at
first login — the app forces this). Otherwise add an admin via the CLI:

```bash
sudo parental-controls unlock <username>      # unlock a non-wheel account
sudo parental-controls unfreeze-all           # thaw every frozen cgroup
sudo parental-controls reset-password <admin> # new generated password, once
sudo parental-controls add-admin <username>   # app-only admin (or wheel row)
sudo parental-controls remove-admin <username>
```

The CLI is `cli.py` (install as `/usr/local/bin/parental-controls`).
Sessions are server-side (30-min idle / 12-h absolute); every POST needs
the `X-CSRF-Token` header or `csrf_token` form field.

## Setup

The checkout is only for development; `install.sh` stages a copy into a
system location, so the checkout path is never baked into the running service.

```bash
# From this folder (the app checkout)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Running

### Quick start (recommended)

```bash
./run.sh
```

This creates a venv (if needed), installs dependencies, and starts the app
via waitress on `127.0.0.1:5000` (localhost + SSH tunnel, no TLS in MVP).
Pass `./run.sh --allow-lan` to bind `0.0.0.0` explicitly.

### Manual

```bash
.venv/bin/waitress-serve --host=127.0.0.1 --port=5000 wsgi:application
# dev server with the same bind default: .venv/bin/python app.py [--allow-lan]
```

The app listens on `127.0.0.1:5000` by default.

## Usage

Open `http://<host>:5000` in a browser. You'll see a table of users with their
lock status and buttons to lock or unlock each account.

## Security Note

MVP authentication is enforced: every page and API route requires an
admin session (`@require_admin`), wheel admins can never be locked or
removed, and the service binds localhost only. LAN exposure needs the
explicit opt-in flag, and cookies travel in the clear until TLS lands
(post-MVP).

## API Endpoints

All pages and `/api/*` routes require an admin session except `/login`.
Every POST needs the `X-CSRF-Token` header or `csrf_token` form field.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Main UI page |
| GET | `/login` | Login page |
| GET | `/user/<username>` | User details page |
| GET | `/api/users` | JSON list of users with lock status |
| GET | `/api/user/<username>/live` | Live status/process/budget data (targeted refresh) |
| GET | `/api/bedtime` | All bedtime settings |
| POST | `/api/lock` | Lock account + screen, session preserved (form: `username`) |
| POST | `/api/lock-screen` | Screen-lock endpoint (no UI button; Lock covers it) |
| POST | `/api/unlock` | Unlock a user (form: `username`) |
| POST | `/api/logout` | Emergency logout: lock + terminate all sessions |
| POST | `/api/bedtime` | Set/clear bedtime (`username`, `start`, `end`, days) |
| GET/POST | `/api/budgets`, `/api/budgets/resume`, `/api/budgets/<user>` | Budget limits, status, manual resume |
| POST | `/api/process-prefs` | Hide/show a process comm for a user |

## Deploying to a real machine

One command (as root, from the app checkout). Idempotent — safe to re-run:

```bash
sudo ./install.sh [--install-dir DIR] [--user USER] [--dry-run]
```

The app is **staged into a system path** so the deployed systemd units never
reference the checkout (e.g. a home directory). Defaults: install dir =
`/opt/parental-controls`, service user = the invoking sudo user. What it does:

1. Stages a copy of the app into the install dir (code only; existing runtime
   state is preserved, and a first run seeds `*.json` state from the checkout)
2. Creates the venv and installs `requirements.txt` (as the service user)
3. Renders `systemd/*.service|timer` against the install dir and installs them
4. Renders both `sudoers-*` files and installs to `/etc/sudoers.d/` (validated
   with `visudo -c`)
5. Creates `/etc/parental-controls` (0700, owned by the service user) for the
   auth store
6. Enables + starts the app service and the enforcement timer
7. Verifies: service active, `GET /` returns the login redirect

Then finish setup:

```bash
# 1. Get the one-time admin credential (forced to change on first login)
journalctl -u parental-controls.service | grep 'AUTH BOOTSTRAP'
# 2. From your machine (localhost + SSH tunnel, no TLS in MVP):
ssh -L 5000:localhost:5000 <user>@<host>
# 3. Open http://localhost:5000, log in, change the password immediately.
```

To preview without changing anything: `sudo ./install.sh --dry-run`.

## Systemd Integration

Files live in `systemd/` (with `@APP_DIR@` / `@SERVICE_USER@` placeholders
rendered by `install.sh` — never copy them by hand):

| File | Purpose |
|------|---------|
| `parental-controls.service` | Runs the Flask app on boot (Restart=always, RestartSec=5, service user) |
| `parental-controls-enforce.service` | Oneshot that runs one bedtime-enforcement pass |
| `parental-controls-enforce.timer` | Runs the enforcement oneshot every minute |
| `enforce_bedtime_timer.py` | Script invoked by the enforce service |

Check status with:

```bash
systemctl status parental-controls.service
systemctl list-timers parental-controls-enforce.timer
```

## Testing

Run the suite from the **checkout** (not the install dir) in the dev venv —
system Python has no Flask:

```bash
.venv/bin/python -m unittest discover -s tests
```

Notes:

- Stdlib `unittest` only — no pytest. ~186 tests, roughly one minute.
- Most tests are mocked/integration tests using the Flask test client, but a
  few are **live**: `test_lock_usermod_real.py` creates a throwaway account,
  really runs `usermod`/`loginctl` against it, and deletes it in teardown.
  `bluefox`, `stan`, `tester` (and root) are forbidden targets — the live
  tests fail rather than touch a real human account.
- Live tests run via the same sudoers rules the app needs; they never talk to
  the running service on `:5000`.
- They're slow on purpose (every live test locks/unlocks the throwaway account
  through the real stack).
