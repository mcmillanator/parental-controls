# Bazzite Parental Controls — Web UI

A web UI for managing user account lock/unlock, bedtimes, and per-category
app time budgets on Bazzite.

Release-ready 1.1 — parent-facing summary, deploy steps and rollback guide:
`RELEASE.md`.

## Features

- Lists all human users on the system (UID >= 1000, discovered dynamically)
- Shows lock/unlock status for each user
- Lock button: locks the account (`usermod -L`) and the screen, session preserved
- Unlock button: runs `sudo usermod -U <user>`
- Emergency logout button: locks the account AND terminates all sessions (the
  only action that ends a session)
- Parent-only access: child accounts can never log in (`AUTH_DESIGN.md`)
- Per-user details pages: bedtime schedule, account status, processes grouped
  by app category with Hide/Show-hidden filtering; duplicate processes
  grouped into one expandable row per command with a count badge
- Processes table auto-hides everything not explicitly user-started
  (background plumbing hides by default; real apps stay visible) and shows
  human-friendly app names from installed `.desktop` files where known
- Per-category daily time budgets enforced with the cgroup freezer
  (pause-not-kill, auto-resume at reset), with a watchdog that thaws a
  genuinely stuck freeze. A manual **Freeze/Unfreeze** toggle sits in each
  budget status cell and on each visible process-group row, independent of
  budget exhaustion
- Bedtime windows with pre-lock warnings and manual-override precedence;
  parent-granted bonus time (15/30/60/90/120-minute shortcuts plus a custom
  hours+minutes entry, 5-minute minimum, no stacking cap) keeps the account
  unlocked past bedtime until the bonus expires, then re-locks
- Tamper-evident audit log: lock/unlock/logout, bedtime, bonus, budget and
  admin actions are appended to a hash-chained `audit.jsonl`; parents can read
  it (newest first, with a chain-valid flag) via `GET /api/audit`
- Runtime state in SQLite (`state.db`, WAL + migrations, backup-before-migrate)
  with atomic JSON mirrors kept for rollback; a corrupt store fails closed
- All clock times shown in 12-hour format
- Status/process/budget cells update in place every 10 seconds (no page reloads;
  forms are never touched); errors display as toasts
- Optional TLS for LAN binds (self-signed cert generated at install time); a
  health-check timer probes the panel every 5 minutes
- Mobile-first, fully offline UI: a phone-first card layout that becomes tables
  on tablets/desktops, a glanceable status summary, and a sticky app bar + bottom
  tab bar for one-handed use; Bootstrap is vendored in `static/` (no CDN, no
  webfont), dark theme by default with an automatic light theme, and WCAG 2.2 AA
  accessibility (see *User interface* below and `UI_UX_PLAN.md`)

## User interface

The panel is first a **family status board** reached one-handed on a phone, so
the interface is designed mobile-first and must render identically on a home LAN
box **with no internet** (`UI_UX_PLAN.md`):

- **Layout** — one column of labeled card stacks on phones; the same markup
  restores semantic tables on tablets/desktops. The Home list, per-user detail
  page and the admin console share the mechanism (`.pc-table--stack` +
  `data-label`), and destructive actions are confirmed in a native `<dialog>`
  (no blocking `window.confirm`).
- **Offline assets** — Bootstrap 5.3.3 (`static/vendor/bootstrap/bootstrap.min.css`)
  and the design system (`static/css/app.css`) are vendored under `static/` and
  linked with `url_for('static', …)`; the app makes **no external request**, so
  there is no CDN, no webfont and no build step. The Bootstrap
  JavaScript bundle is deliberately *not* vendored — modals, menus and the icon
  sprite use native HTML (`<dialog>`, `<details>`, inline SVG). `install.sh`
  stages the whole app tree (including `static/` and `templates/`) into the
  install dir, so the deployed service serves its own stylesheets.
- **Theming** — dark is the default; a light theme follows
  `prefers-color-scheme: light` and an explicit `[data-theme="light"]` hook. Both
  themes are token-based (`static/css/app.css`) and hold WCAG 2.2 AA contrast for
  every status chip.
- **Accessibility** — skip link, `<html lang>`, visible `:focus-visible` rings,
  44 px touch targets (32 px minimum for small buttons), `aria-live` toast and
  background-refresh regions, `prefers-reduced-motion` support, and status shown
  as icon **and** text **and** colour.
- **No-JS-framework navigation** — a sticky app bar (tablet/desktop) and a bottom
  tab bar (phone) link Home / Admin and hold the account menu (Change password,
  Log out).

The design system lives in `static/css/app.css`; the review checklist, phasing
and every screen mapping are in `UI_UX_PLAN.md`.

## Requirements

- Python 3
- Flask, argon2-cffi, waitress (see `requirements.txt`); `cheroot` is the
  TLS-capable server used for `--tls` LAN binds (also in `requirements.txt`)
- A modern browser — the UI needs **no internet access**; all assets are served
  locally from `/static`
- sudo access (the app runs `usermod`, `loginctl`, `passwd -S` and, for
  notifications, `systemd-run` through sudo — scoped rules live in
  `sudoers-parental-controls` / `sudoers-parental-controls-budgets`)

## Authentication (MVP)

Two admin tiers, both with **app passwords only** (never Linux passwords;
no PAM anywhere). `wheel` members are admins automatically once they have
a login row; app-only admins have no system account. Child accounts can
never log in. Full design: `AUTH_DESIGN.md`.

First run bootstraps the store (`/etc/parental-controls/auth.json`, or
`$PARENTAL_CONTROLS_AUTH` for dev/test): if the invoking user is a wheel
member, a one-time initial password is printed to stdout (change it at
first login — the app forces this). Otherwise add an admin via the CLI.

The CLI is `cli.py`. The installer installs a root-only wrapper at
`/usr/local/bin/parental-controls` (mode 0750, owned by root), so on a deployed
machine use the short form; from a checkout (no install) run it with that
tree's venv interpreter (it needs argon2-cffi):

```bash
sudo parental-controls unlock <username>      # unlock a non-wheel account
sudo parental-controls unfreeze-all           # thaw every frozen cgroup
sudo parental-controls reset-password <admin> # new generated password, once
sudo parental-controls add-admin <username>   # app-only admin (or wheel row)
sudo parental-controls remove-admin <username>
# without the wrapper:
sudo @APP_DIR@/.venv/bin/python @APP_DIR@/cli.py unfreeze-all
```
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
via waitress on `127.0.0.1:5000` (localhost + SSH tunnel; loopback is plain
HTTP by design). Pass `./run.sh --allow-lan` to bind `0.0.0.0` explicitly —
that is a dev-only plain-HTTP bind; a real LAN deployment uses
`install.sh --tls` (HTTPS) instead.

### Manual

```bash
.venv/bin/waitress-serve --host=127.0.0.1 --port=5000 wsgi:application
# dev server with the same bind default: .venv/bin/python app.py [--allow-lan]
# TLS LAN bind (as the installer runs it): python3 tls_serve.py \
#     --host 0.0.0.0 --port 5000 --cert CERT --key KEY
```

The app listens on `127.0.0.1:5000` by default.

## Usage

Open `http://<host>:5000` in a browser. You'll see the users on the system with
their live lock status and a Lock/Unlock action per user; on a phone the list
renders as one card per child with a summary strip at the top, and the same page
reflows to a table on a larger screen. Opening a user shows their bedtime, bonus
time, budgets and processes.

## Security Note

MVP authentication is enforced: every page and API route requires an
admin session (`@require_admin`), wheel admins can never be locked or
removed, and the service binds localhost only by default. LAN exposure needs
the explicit opt-in flag. The installer refuses a plain-HTTP LAN bind: pass
`--tls` for HTTPS (install-time self-signed certificate — browsers will warn
until you trust it; compare the printed fingerprint), or `--insecure-lan` to
knowingly serve plain HTTP with a persistent warning banner. Loopback stays
plain HTTP so the recommended SSH tunnel keeps working unchanged. Tamper-evident
audit entries (hash-chained, **not** encrypted) are readable by admins via
`GET /api/audit`.

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
| POST | `/api/bonus` | Grant bonus time (`username`, `minutes` and/or `hours`) |
| POST | `/api/bonus/revoke` | Revoke bonus time and re-lock if still in bedtime |
| GET/POST | `/api/budgets`, `/api/budgets/<user>` | Budget limits and status |
| POST | `/api/budgets/resume` | Manual resume (alias of unfreeze) (`username`, `category`) |
| POST | `/api/budgets/freeze` | Manually freeze a category (`username`, `category`) |
| POST | `/api/budgets/unfreeze` | Manually thaw a category (`username`, `category`) |
| GET | `/api/audit` | Admin-only: newest audit entries + `chain_ok` (`?limit=N`, default 100, max 1000) |
| POST | `/api/process-prefs` | Hide/show a process comm for a user |
| GET | `/api/devices` | Multi-device: registered boxes + reachability (`multi_device: false` in single-box mode) |
| GET/POST | `/api/device/<name>/v1/<path>` | Multi-device: proxy one request to a registered box's agent (admin session + CSRF) |

## Multi-device (one hub + managed boxes)

Phase 1 of `MULTIDEVICE_PLAN.md`: one panel (hub) can moderate other Bazzite
boxes that run a headless **agent**. All privileged work still happens on the
box that executes it, with that box's live `wheel` group and its own sudoers
and audit chain.

```bash
# On each managed box (loopback + SSH tunnel by default):
sudo ./install.sh --enable-agent            # mints /etc/parental-controls/agent_token (0600), prints it ONCE

# From the hub, over a tunnel (ssh -L 5001:localhost:5001 <user>@boxB):
sudo python3 remote.py device add kidsbox \
    --url http://localhost:5001 --token <printed-once>   # writes /etc/parental-controls/devices.json (0600)
```

With no `devices.json`, the hub is **byte-identical** to a single-box install.
A device can be removed with `python3 remote.py device remove kidsbox` (or by
deleting `devices.json`) — no state changes anywhere else. Plain-HTTP device
URLs are refused unless loopback (or an explicit `--insecure-lan`).

## Deploying to a real machine

One command (as root, from the app checkout). Idempotent — safe to re-run:

```bash
sudo ./install.sh [--install-dir DIR] [--user USER] [--bind HOST] [--port PORT]
                  [--open-firewall] [--dry-run]
                  [--tls [--tls-cert CERT --tls-key KEY] [--tls-name NAME]
                   [--regen-tls-cert] | --insecure-lan]
```

The app is **staged into a system path** so the deployed systemd units never
reference the checkout (e.g. a home directory). Defaults: install dir =
`/opt/parental-controls`, service user = `parental-controls` (a dedicated
system account — nologin shell, no home, locked password — created by the
installer when missing; override with `--user USER`), bind =
`127.0.0.1:5000`, no firewall change. What it does:

1. Creates the `parental-controls` service user when missing (idempotent;
   an existing account is left untouched — migration never resets it)
2. Stages a copy of the app into the install dir (code only; existing runtime
   state — `state.db` and the JSON mirrors — is preserved, and a first run
   seeds state from the checkout)
3. Creates the venv and installs `requirements.txt` (as the service user,
   with `HOME`/`XDG_CACHE_HOME` pointed at an owned dir — the account is
   intentionally homeless)
4. Generates the TLS certificate when `--tls` is given (into
   `/etc/parental-controls/tls/`, 0600 key, never auto-regenerated) and renders
   the matching `tls_serve.py`/waitress variant into the service unit
5. Renders `systemd/*.service|timer` against the install dir and installs them
6. Renders both `sudoers-*` files and installs to `/etc/sudoers.d/` (validated
   with `visudo -c`)
7. Creates `/etc/parental-controls` (0700, owned by the service user) for the
   auth store
8. Installs the root-only recovery CLI at `/usr/local/bin/parental-controls`
   (0750)
9. Opens `PORT/tcp` in the default firewalld zone — only with `--open-firewall`
10. Enables + starts the app service, the enforcement timer and the health-check
    timer
11. Verifies: service active as the service user (`User=`), auth store owned
    and writable by it, `GET /` returns the login redirect, and one live
    health-check probe succeeds

`--bind`/`--port` render into the service unit (`--bind 0.0.0.0` listens on
all interfaces). A non-loopback bind is **refused** unless it serves TLS:
`--tls` generates a self-signed certificate (or use `--tls-cert`/`--tls-key`,
`--tls-name` to set the SAN identity) and serves HTTPS via cheroot;
`--insecure-lan` knowingly stays on plain HTTP and shows a persistent warning
banner on every page. `--tls` never implies `--open-firewall` — pass it
explicitly. Loopback keeps the plain waitress HTTP path unchanged, so the SSH
tunnel remains the recommended way in.
Re-running the installer is the update mechanism: if a rendered unit differs
from the installed one, the service restarts to pick it up; identical
re-installs leave the running service alone.

Then finish setup:

```bash
# 1. Get the one-time admin credential (forced to change on first login)
journalctl -u parental-controls.service | grep 'AUTH BOOTSTRAP'
# 2. From your machine (loopback + SSH tunnel; loopback is plain HTTP):
ssh -L 5000:localhost:5000 <user>@<host>
# 3. Open http://localhost:5000, log in, change the password immediately.
#    (For a --tls LAN bind, open https://<host>:5000 instead and trust the
#    fingerprint the installer printed.)
```

Lost the admin password, or the panel is wedged? The recovery CLI is
root-only and works without the web app:

```bash
sudo parental-controls reset-password <admin>
sudo parental-controls unlock <username>
sudo parental-controls unfreeze-all
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
| `parental-controls-healthcheck.service` | Oneshot liveness probe (`healthcheck.sh`) |
| `parental-controls-healthcheck.timer` | Probes unit state + HTTP/HTTPS + auth store every 5 minutes |

Check status with:

```bash
systemctl status parental-controls.service
systemctl list-timers parental-controls-enforce.timer
systemctl list-timers parental-controls-healthcheck.timer
journalctl -u parental-controls-healthcheck.service
```

The health check never notifies a child and never restarts the panel; a failure
is surfaced as a failed unit for a human to act on.

## Testing

Run the suite from the **checkout** (not the install dir) in the dev venv —
system Python has no Flask:

```bash
.venv/bin/python -m unittest discover -s tests
```

Notes:

- Stdlib `unittest` only — no pytest. **1238 tests across 47 files, 14
  environmental skips** (skips are for missing optional tooling/privileges),
  under a minute for a full run.
- The mobile-first UI has its own guardrails: `test_ui_assets.py` (vendored
  offline assets + the design-system contract), `test_ui_home.py`,
  `test_ui_user.py`, `test_ui_admin.py` (page conversions keep the pinned
  selectors) and `test_ui_a11y.py` (contrast, focus/targets, theming, states).
  They check source and the Flask-rendered HTML; none need a browser or network.
- Most tests are mocked/integration tests using the Flask test client, but a
  few are **live**: `test_lock_usermod_real.py` creates a throwaway account,
  really runs `usermod`/`loginctl` against it, and deletes it in teardown.
  `bluefox`, `stan`, `tester` (and root) are forbidden targets — the live
  tests fail rather than touch a real human account.
- Live tests run via the same sudoers rules the app needs; they never talk to
  the running service on `:5000`.
- They're slow on purpose (every live test locks/unlocks the throwaway account
  through the real stack).
- Browser-level QA is a separate, self-contained Katalon Recorder suite — see
  `katalon/README.md`. Its Selenium-IDE HTML (`katalon/parental-controls.html`)
  is validated in CI by `tests/test_katalon_suite.py`, which pins every locator
  and expected string against `templates/` and `app.py`.

## License

SPDX-License-Identifier: MIT

MIT License — see `LICENSE`. Copyright (c) 2026 mcmillanator.
