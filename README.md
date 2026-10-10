# Bazzite Parental Controls — Web UI

A lightweight Flask web UI for managing user account lock/unlock on Bazzite.

## Features

- Lists all human users on the system (UID >= 1000, discovered dynamically)
- Shows lock/unlock status for each user
- Lock button: runs `sudo usermod -L <user>` + `sudo loginctl terminate-user <user>`
- Unlock button: runs `sudo usermod -U <user>`
- Auto-refreshes every 10 seconds
- Error messages displayed in the UI

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

```bash
# Clone or navigate to this directory
cd .worktrees/webui

# Create venv and install dependencies
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

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Main UI page |
| GET | `/api/users` | JSON list of users with lock status |
| POST | `/api/lock` | Lock a user (form field: `username`) |
| POST | `/api/unlock` | Unlock a user (form field: `username`) |

## Systemd Integration

Files live in `systemd/`:

| File | Purpose |
|------|---------|
| `parental-controls.service` | Runs the Flask app on boot (Restart=always, RestartSec=5, User=stan) |
| `parental-controls-enforce.service` | Oneshot that runs one bedtime-enforcement pass |
| `parental-controls-enforce.timer` | Runs the enforcement oneshot every minute |
| `enforce_bedtime_timer.py` | Script invoked by the enforce service |

### Install

```bash
# Copy the enforcement script next to app.py
cp enforce_bedtime_timer.py /var/home/stan/Documents/Projects/bazzite-parental-controls-2/master/

# Install units
sudo cp systemd/parental-controls.service /etc/systemd/system/
sudo cp systemd/parental-controls-enforce.service /etc/systemd/system/
sudo cp systemd/parental-controls-enforce.timer /etc/systemd/system/

# Enable
sudo systemctl daemon-reload
sudo systemctl enable --now parental-controls.service
sudo systemctl enable --now parental-controls-enforce.timer
```

Check status with:

```bash
systemctl status parental-controls.service
systemctl list-timers parental-controls-enforce.timer
```
