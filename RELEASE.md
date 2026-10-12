# Bazzite Parental Controls — Release 1.1

Parent-facing release document: what this is, what was verified working, how to
deploy it, and how to undo a deploy if something goes wrong.

This document covers **1.1** (current) and carries the **1.0** release notes
below for reference. If you are upgrading from 1.0, the installer is the whole
upgrade path; see [Release notes — 1.1](#release-notes--11) and
[Upgrading from 1.0](#upgrading-from-10).

## What this release is

A small web panel, running on the family machine, that lets a parent:

- **Lock and unlock a child's account** — the account really is locked
  (`usermod -L`), so the correct password stops working at the greeter. The
  child stays logged in; their screen is locked, their apps keep running.
- **End a session outright** — *Emergency logout* locks the account **and** ends
  every session (`loginctl terminate-user`). This is the only control that does.
- **Set a bedtime** — the account locks itself at bedtime, unlocks itself at the
  end of the window, with a warning 15 minutes ahead. A parent can override, and
  can grant the child **bonus time** past bedtime.
- **Set a daily budget per app category** (Games / Browsers / Media / Chat /
  Other). When a category runs out it is **paused** with the cgroup freezer —
  processes are frozen, not killed — and it resumes automatically at the next
  window. A parent can also **freeze or unfreeze a category by hand** at any
  time, independent of the budget.
- **See what happened** — a tamper-evident audit log records who locked,
  unlocked, logged out, changed bedtime/bonus/budgets and managed admins, and
  when. The parent-facing panel reads it; the hash chain shows if the file was
  altered.

Children can never log in to the panel. Only parents (admins) can.

## Verified environment

- **Bazzite 44.20261002.0 (Kinoite)**, `BUILD_ID="Stable (F44.20261002)"` —
  the **first Bazzite version this release was verified running on**.
- Code baseline: `master` @ `108392d` (plus these documentation updates).
- Test suite: **526 tests, 9 environmental skips** (22 files, stdlib
  `unittest`).
- Panel served by waitress on `127.0.0.1:5000`; runtime state lives in SQLite
  (`state.db`) in the install dir, next to atomic JSON rollback mirrors.

## Verified working feature set

| Area | What is proven |
|------|----------------|
| Accounts | Dynamic user discovery (`/etc/passwd`, UID >= 1000), per-user lock status |
| Locking | Manual Lock, bedtime auto-lock, bedtime-save enforce, bonus grant/revoke/expiry — **all preserve the session**; screen-lock targets the child's graphical sessions only; offline users degrade to a note, never a failure |
| Emergency logout | `/api/logout` is the only path that terminates sessions |
| Bedtime | Window + days of week, 15-minute warning, self-lock at start, self-unlock at end, manual-override precedence |
| Bonus time | 15/30/60/90/120-minute shortcuts + custom hours+minutes (5-minute minimum); grants stack with no cap; cleared when the window ends; last 20 actions kept per user |
| Budgets | Per-category daily budgets with the cgroup freezer (pause, not kill), auto-resume at reset, manual Resume button, low-budget warnings |
| Manual freeze | Freeze/Unfreeze toggle in each budget status cell and on visible process-group rows; `manual_frozen` is distinct from budget exhaustion and is released by the daily reset or by clearing the limit |
| Audit log | Append-only hash-chained `audit.jsonl` covering lock/unlock/logout, bedtime, bonus, budget and admin actions; admin-only `GET /api/audit` returns newest entries plus `chain_ok`. Tamper-evident, **not** encrypted; logging never blocks enforcement |
| State store | Runtime state in SQLite (`state.db`): WAL, `schema_version`, ordered migrations, backup-before-migrate, fail-closed corruption recovery, one-time legacy JSON import; atomic JSON mirrors kept for rollback |
| Processes | Curated filtering + per-row Hide/Show, duplicate processes collapsed into one expandable row per command, auto-hide of plumbing processes, friendly app names from `.desktop` files |
| Safety | Wheel admins can never be locked, terminated or removed — refused in the handler, not just hidden in the UI; confirm dialogs on destructive actions |
| Security | App-only admin logins with server-side sessions (30-minute idle / 12-hour absolute), two admin tiers, CSRF on every state-changing request, localhost-only bind by default, `wheel` never lockable |
| LAN (opt-in) | Plain-HTTP LAN binds are refused by the installer; `--tls` serves HTTPS via cheroot with an install-time self-signed cert, `--insecure-lan` is the deliberate plain-HTTP escape hatch. Loopback stays plain HTTP by design |
| Operations | systemd unit with `ProtectSystem=strict` (and directory-level `/etc` writable), one-command installer, `visudo`-validated scoped sudoers, recovery CLI at `/usr/local/bin/parental-controls`, dedicated `parental-controls` service user, health-check timer every 5 minutes |

## Deploying

Deploy from the `master` checkout. The checkout is a *build* input only — the
service runs from the install dir, so no checkout path is ever baked into the
systemd units.

```bash
# 1. From the app checkout on master:
sudo ./install.sh

# 2. Smoke-test — expect 302 (redirect to the login page):
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/
```

`install.sh` is idempotent and safe to re-run; re-running it *is* the update
mechanism. It creates the dedicated `parental-controls` service user if missing,
stages the code into the install dir (`@APP_DIR@`, default `/opt/parental-controls`),
builds the venv, renders and installs the systemd units and both sudoers files
(validated with `visudo -c`), creates the auth-store directory, installs the
root-only recovery CLI at `/usr/local/bin/parental-controls`, enables + starts the
service, the enforcement timer and the health-check timer, and finally verifies
that `GET /` returns the login redirect (plus one live health probe).

Useful flags: `--install-dir DIR`, `--user USER`, `--bind HOST`, `--port PORT`,
`--open-firewall`, `--dry-run` (preview everything, change nothing), and for a
LAN bind `--tls [--tls-cert CERT --tls-key KEY] [--tls-name NAME]
[--regen-tls-cert]` or `--insecure-lan`. Plain-HTTP LAN binds are refused.

Runtime state (`state.db` and the `bedtime.json` / `budgets.json` /
`process_prefs.json` mirrors, plus `audit.jsonl`) is explicitly excluded from the
code sync, so a deploy never overwrites or deletes it.

## First login (admin bootstrap)

On a machine that has never run the panel, the first start creates the auth store
and prints a one-time initial password to the service journal:

```bash
journalctl -u parental-controls.service | grep 'AUTH BOOTSTRAP'
```

If the invoking user is not a `wheel` member, the journal instead prints the
command to add the first admin. The generated password is shown **once** and must
be changed at first login — the app forces it.

Then open the panel (localhost only by default, so tunnel it):

```bash
ssh -L 5000:localhost:5000 <user>@<host>
# open http://localhost:5000 and log in
# (with --tls on a LAN bind, open https://<host>:5000 and trust the
#  fingerprint the installer printed)
```

Lost the password, or the app is wedged? Use the recovery CLI, which works
without the panel. `install.sh` installs it root-only at
`/usr/local/bin/parental-controls` (mode 0750), so on a deployed machine the
short form works; from a bare checkout use the venv interpreter instead:

```bash
sudo parental-controls reset-password <admin>
sudo parental-controls unlock <username>
sudo parental-controls unfreeze-all
# or, without the wrapper:
sudo @APP_DIR@/.venv/bin/python @APP_DIR@/cli.py unlock <username>
```

The CLI assumes you can already get a root shell — it is not a boot-time
recovery mechanism for a machine with no admin credentials at all (still open,
`PLAN.md` Phase 3).

## Known limitations

These are deliberate, current, and not defects in disguise:

- **No TLS on loopback.** The panel binds `127.0.0.1` by default and is meant to
  be reached over an SSH tunnel, so loopback stays plain waitress HTTP by design.
  A LAN bind must serve HTTPS: the installer refuses a plain-HTTP LAN bind unless
  you pass `--insecure-lan` (persistent warning banner) — the cookies then travel
  in the clear.
- **A blanket passwordless-sudo rule for the operations account still exists on
  the development machine** (`stan-nopasswd`), by owner decision; it masks the
  scoped sudoers rules there. The panel itself now runs as the dedicated
  `parental-controls` account, which the blanket never covers. The installer
  never creates such a rule — a fresh install only writes the two scoped files —
  but check for a pre-existing blanket rule on any machine you deploy to, because
  while one exists least privilege cannot be *proven*. Tracked as a sudo audit
  (`PLAN.md` Phase 3; argument-regex containment work exists and its landing is
  recorded in `sudo-audit.md`).
- **Locking does not stop running apps.** `usermod -L` blocks authentication;
  budget enforcement (the freezer) is what pauses running applications. Only
  Emergency logout ends a session.
- **The audit log is tamper-evident, not encrypted.** `audit.jsonl` is plaintext
  by design; the hash chain detects modification, deletion or reordering, but
  anyone who can read the file can read every entry. Logging is best-effort and
  never blocks an enforcement action.
- **SQLite narrows, but does not eliminate, multi-writer races.** The web app,
  its 60-second thread and the enforce timer share `state.db` with WAL and a busy
  timeout, and whole-document writes are last-writer-wins. A corrupt store fails
  closed (read as empty, never fabricated) rather than crashing — keep the JSON
  mirrors as your rollback copy. Per-field normalization is still on the roadmap.
- **Usage is billed by CPU time, not focus time.** A background process that
  barely uses the CPU does not burn budget. Accurate focus measurement needs a
  per-user session agent (not shipped). A **locked** account does not burn
  budget either: while usermod -L is in effect (manual Lock, Lock Screen,
  bedtime auto-lock) ticks advance the accounting baselines without billing,
  so the first tick after unlock bills only new activity.
- **The UI loads Bootstrap 5 from a CDN**, so styling needs internet access on
  the machine showing the panel. Functionality is unaffected offline.
- **Bonus time is per account, not per app.** It suppresses the bedtime lock
  only; budgets keep running.
- **The recovery CLI assumes a root shell** (see First login above).
- **Per-app overrides and weekly budget windows are not shipped**, and the panel
  still manages a single device.

## Release notes — 1.1

Second release. Baseline `master` @ `108392d`; verified on **Bazzite
44.20261002.0 (Kinoite)**. **526 tests, 9 environmental skips** (22 files,
stdlib `unittest`).

**State and data**
- **Runtime state moved to SQLite** (`state.db`): one JSON-document table per
  store, `schema_version` + ordered migrations applied on open,
  backup-before-migrate (`state.db.bak.<version>`), WAL + busy timeout for the
  three writers, and fail-closed corruption recovery (an unreadable store reads
  as empty, never fabricated). The migration is **automatic** on first run: a
  freshly created database absorbs the sibling `bedtime.json` /
  `budgets.json` / `process_prefs.json` once, after which it is the single source
  of truth. Atomic JSON mirrors are rewritten on every save as a rollback copy
  (never read back). `auth.json` deliberately stays a separate fail-closed
  credential store.

**Accountability**
- **Tamper-evident audit log** (`audit.py`, append-only `audit.jsonl`):
  sha256-chained entries (`prev_hash`/`hash`), hooks on lock/unlock/logout,
  bedtime set/clear, bonus grant/revoke, budget set/freeze/thaw and admin
  add/remove. Admin-only `GET /api/audit?limit=N` returns the newest entries
  plus `chain_ok` (and the first bad sequence, if any). Logging is
  best-effort — it can never block an enforcement action.

**Controls**
- **Manual freeze/unfreeze buttons**: a Freeze/Unfreeze toggle in each budget
  Status cell and on each visible process-group row
  (`POST /api/budgets/freeze`, `POST /api/budgets/unfreeze`). A manual freeze is
  recorded as `manual_frozen`, distinct from budget-exhaustion `paused`, so the
  watchdog treats it as legitimate; it is cleared by the daily reset or by
  clearing the category limit. Wheel/admin targets are refused in the handler.
- Lock paths unchanged and still session-preserving; only Emergency logout
  terminates.

**Operations**
- **TLS for LAN binds**: waitress has no TLS, so a LAN bind is served over HTTPS
  by cheroot (`tls_serve.py`) with an install-time self-signed certificate
  (0600, never auto-regenerated; `--tls-cert`/`--tls-key` to bring your own;
  `--regen-tls-cert` to rotate; the fingerprint is printed for TOFU). Plain-HTTP
  LAN binds are refused unless `--insecure-lan` is given, which adds a persistent
  warning banner. Loopback stays byte-identical waitress HTTP.
- **Health check**: `parental-controls-healthcheck.timer` probes every 5 minutes
  (the unit is active, `GET /` answers 200 or a 302 to the login page, and the
  auth store parses as the service user) and surfaces breakage as a failed unit.
  It never notifies a child and never restarts the panel.
- **Dedicated service user**: the panel and its oneshots now run as the system
  user `parental-controls` (nologin shell, locked password, no sudo of its own —
  only the scoped sudoers grants), created idempotently by `install.sh`. The
  shared human/agent login account no longer owns the network-exposed process.
  `pcverify` keeps the scoped least-privilege verification role.
- **Recovery CLI installed on-machine**: `install.sh` writes a root-only wrapper
  (0750) at `/usr/local/bin/parental-controls`.

**Quality**
- **Katalon Recorder browser QA suite** (`katalon/`) with a CI validator
  (`tests/test_katalon_objects.py`) that pins every selector and expected string
  against the templates and `app.py`. See `katalon/README.md`.

### Upgrading from 1.0

The installer is the upgrade path — there is no separate migration command:

```bash
# from the 1.1 checkout, on the machine:
sudo ./install.sh          # add --tls/--bind etc. only if you want a LAN bind
```

What happens on the first 1.1 deploy over a 1.0 install:

1. **The dedicated `parental-controls` service user is created** (idempotent:
   an existing account is left untouched). 1.0 ran the panel as the invoking
   sudo user; after the upgrade it runs as `parental-controls`, and
   `/etc/parental-controls` is `chown`ed (recursively, ownership only —
   contents are not changed) to that account.
2. **The state migration is automatic.** The first time the new code opens the
   store it creates `state.db` and imports the existing `bedtime.json` /
   `budgets.json` / `process_prefs.json` once. Nothing needs editing; the JSON
   files stay on disk as rollback mirrors.
3. The recovery CLI appears at `/usr/local/bin/parental-controls`, and the
   health-check timer is enabled.
4. A rendered unit change (the new `User=`, or the TLS ExecStart) restarts the
   service; an identical re-install leaves it running.

Before upgrading, take the backup described under
[Rollback / downgrade](#rollback--downgrade) — it is cheap and it is the only
thing not automatically preserved.

## Release notes — 1.0

First release-ready build, verified on **Bazzite 44.20261002.0 (Kinoite)**
(`BUILD_ID="Stable (F44.20261002)"`) — the first Bazzite version this release
ran on. Highlights since the prototype tags:

**Security and operations (the MVP gate)**
- App-only admin authentication with two tiers (`wheel` members and app-only
  admins), server-side sessions, forced password change, CLI recovery. Child
  accounts can never authenticate.
- CSRF token required on every state-changing request.
- Flask dev server replaced by **waitress**; binds `127.0.0.1` by default with an
  explicit LAN opt-in.
- systemd hardening (`ProtectSystem=strict` plus the directory-level `/etc`
  exception that `usermod` requires), scoped sudoers validated with `visudo -c`.
- **Freezer watchdog**: a genuinely stuck `cgroup.freeze=1` with no paused budget
  behind it is detected and thawed automatically — this is the self-heal for the
  2026-10-09 incident that froze a whole desktop.
- Responsive Bootstrap UI.

**Controls and enforcement**
- Lock regression fixed: **every** lock path preserves the session; only
  Emergency logout terminates. (Earlier builds logged the child out when the
  bedtime timer fired.)
- Parent-granted bonus time past bedtime, with the parent-decided policy:
  no stacked cap, custom entry in hours + minutes, 5-minute minimum, 15/30/60/90/120
  shortcuts, last 20 actions kept, leftover bonus cleared at the end of a window.
- Warnings now reach the child: `notify_user()` runs through
  `sudo systemd-run --machine=<user>@.host --user notify-send`, backed by a scoped
  sudoers grant, verified working under scoped sudoers.

**Interface**
- Processes auto-hide plumbing, show friendly app names, group duplicate
  processes into expandable rows, and remember Hide/Show choices.
- All clock times in 12-hour format.
- MIT license.

## Rollback / downgrade

### Where the state lives

| File | Holds | Survives a redeploy? |
|------|-------|----------------------|
| `@APP_DIR@/state.db` (+ `-wal`, `-shm`) | SQLite source of truth: bedtime schedules + lock ownership + bonus grants/history, per-category limits and today's usage, per-user Hide/Show choices | Yes |
| `@APP_DIR@/bedtime.json`, `budgets.json`, `process_prefs.json` | Best-effort atomic **mirrors** of the same documents (rollback copy, refreshed on every save; never read back by 1.1) | Yes |
| `@APP_DIR@/audit.jsonl` | Hash-chained audit log (plaintext, append-only) | Yes |
| `/etc/parental-controls/auth.json` | Admin accounts and password hashes | Yes (outside the install dir entirely) |
| `/etc/parental-controls/tls/` | TLS key/cert when `--tls` was used | Yes (outside the install dir entirely) |

All of these are gitignored (the auth and TLS material live outside the repo
entirely), and `install.sh` excludes them from the code sync, so deploying any
version over any version leaves them alone. **A backup is a copy of `state.db`,
the three JSON mirrors, `audit.jsonl` and `auth.json`** (`/etc/parental-controls`
is `chown`ed on deploy but its contents are not changed).

### Is a downgrade safe?

Mostly yes, with honest caveats. There are two independent stories: the
JSON→SQLite migration, and the older bonus/state quirks.

**1. Downgrading across the SQLite migration (1.1 → 1.0 or earlier).**

- A pre-SQLite build knows nothing about `state.db`. Its `install.sh` does not
  exclude the database, so redeploying that older version **deletes `state.db`
  (and its WAL sidecar)** from the install dir when it stages with
  `rsync --delete` (the normal path; the tar fallback would keep the file
  around, inert) — the three JSON files *are* excluded by the old installer,
  so they survive.
- That is by design and is why the mirrors exist: the downgraded build reads
  `bedtime.json` / `budgets.json` / `process_prefs.json` directly, and they hold
  the last state the 1.1 build saved.
- **Going back up is the caveat.** A 1.1 build only imports legacy JSON when
  `state.db` does **not** exist. If the old build created no database, the first
  1.1 run re-imports the mirrors — fine. But any state written *while
  downgraded* (a schedule changed on the 1.0 panel) is **not** re-imported once a
  database exists; the 1.1 build treats `state.db` as the truth. To be safe,
  re-apply schedule/budget changes after upgrading again, or keep the pre-downgrade
  `state.db` backup and restore it.
- A corrupt `state.db` is never silently replaced: reads fail closed to empty
  (like the old JSON loaders), and the recovery is to delete the database and let
  the next open re-import the mirrors.

**2. Downgrading past the bonus-time feature.** `load_bedtime()` /
`save_bedtime()` read and write the whole per-user document, and every code path
that updates bedtime state edits individual keys in place. Older builds never read
the `bonus_*` keys, so they pass them through **untouched and inert** — a stale
`bonus_until` cannot confuse an older build, and nothing crashes.

Two caveats, both verified against the pre-bonus build (`de3f147`):

1. **Saving a bedtime schedule on a pre-bonus build drops the bonus keys.** That
   older `POST /api/bedtime` handler rebuilds the user's entry from scratch
   (`start`, `end`, `days`, `locked_by_bedtime`, `manual_override`). The grant is
   lost, not corrupted; the account simply locks on schedule.
2. **Clearing a bedtime deletes the whole entry**, bonus keys included — on
   every version, not just old ones.

While downgraded, an active bonus does nothing: bedtime locks the account as
usual. Nothing is left unlocked by accident, because the account was already
unlocked by the grant.

### Rolling back

```bash
# 1. Check out the previous good commit/tag (at or after fa162cc, when the
#    one-command installer landed):
git checkout <previous-tag-or-commit>

# 2. Redeploy from that checkout — state is preserved (see the SQLite caveat
#    above about what a downgrade does with state.db):
sudo ./install.sh

# 3. Smoke-test:
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/   # expect 302
```

Before deploying a *different* code version onto a machine, take the backup
above — it is the only thing that is not automatically preserved.

### If a stale `bonus_until` sits inert

- **Best: revoke it before you roll back.** On the current build use the
  *Revoke* button (or `POST /api/bonus/revoke`). It drops the live keys
  immediately and re-locks the account if it is inside the bedtime window.
- **Rolling back with one still in the file?** Harmless — old code ignores it.
  On the way back up, the newer build reads it again: if `bonus_until` is still
  in the future it will suppress the bedtime lock again (which may surprise you
  hours later), and if it is in the past it is recorded as `expire` and dropped.
- **To clear it by hand on the 1.1 build**: the mirror JSON is never read back,
  so editing `bedtime.json` no longer changes anything. Either use the *Revoke*
  button, or restore a backup of `state.db` taken before the grant. A value that
  is not a parseable ISO timestamp is treated as "no bonus" anyway, so a
  corrupted value fails closed — it never leaves the account unlocked.

### Two rollbacks to think twice about

- **Below `91bdc87`** (the MVP-auth merge): there is **no authentication**. Do
  not deploy that anywhere reachable — localhost + SSH tunnel only.
- **Below `fbecb33`/`921ea54`** (the lock-logs-out fix): locking runs
  `loginctl terminate-user` again, so bedtime locks will **log the child out of
  their session**, not just lock the screen.