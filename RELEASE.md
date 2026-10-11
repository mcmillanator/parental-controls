# Bazzite Parental Controls — Release 1.0

Parent-facing release document: what this is, what was verified working, how to
deploy it, and how to undo a deploy if something goes wrong.

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
  window.

Children can never log in to the panel. Only parents (admins) can.

## Verified environment

- **Bazzite 44.20261002.0 (Kinoite)**, `BUILD_ID="Stable (F44.20261002)"` —
  the **first Bazzite version this release was verified running on**.
- Code baseline: `master` @ `24fb669` (plus these documentation updates).
- Panel served by waitress on `127.0.0.1:5000`; state lives in the install dir.

## Verified working feature set

| Area | What is proven |
|------|----------------|
| Accounts | Dynamic user discovery (`/etc/passwd`, UID >= 1000), per-user lock status |
| Locking | Manual Lock, bedtime auto-lock, bedtime-save enforce, bonus grant/revoke/expiry — **all preserve the session**; screen-lock targets the child's graphical sessions only; offline users degrade to a note, never a failure |
| Emergency logout | `/api/logout` is the only path that terminates sessions |
| Bedtime | Window + days of week, 15-minute warning, self-lock at start, self-unlock at end, manual-override precedence |
| Bonus time | 15/30/60/90/120-minute shortcuts + custom hours+minutes (5-minute minimum); grants stack with no cap; cleared when the window ends; last 20 actions kept per user |
| Budgets | Per-category daily budgets with the cgroup freezer (pause, not kill), auto-resume at reset, manual Resume button, low-budget warnings |
| Processes | Curated filtering + per-row Hide/Show, duplicate processes collapsed into one expandable row per command, auto-hide of plumbing processes, friendly app names from `.desktop` files |
| Safety | Wheel admins can never be locked, terminated or removed — refused in the handler, not just hidden in the UI; confirm dialogs on destructive actions |
| Security | App-only admin logins with server-side sessions (30-minute idle / 12-hour absolute), two admin tiers, CSRF on every state-changing request, localhost-only bind by default, `wheel` never lockable |
| Operations | systemd unit with `ProtectSystem=strict` (and directory-level `/etc` writable), one-command installer, `visudo`-validated scoped sudoers, recovery CLI |

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
mechanism. It stages the code into the install dir (`@APP_DIR@`, default
`/opt/parental-controls`), builds the venv, renders and installs the systemd
units and both sudoers files (validated with `visudo -c`), creates the auth-store
directory, enables + starts the service and the enforcement timer, and finally
verifies that `GET /` returns the login redirect.

Useful flags: `--install-dir DIR`, `--user USER`, `--bind HOST`, `--port PORT`,
`--open-firewall`, `--dry-run` (preview everything, change nothing).

Runtime state (`bedtime.json`, `budgets.json`, `process_prefs.json`) is
explicitly excluded from the code sync, so a deploy never overwrites or deletes
it.

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
```

Lost the password, or the app is wedged? Use the recovery CLI, which works
without the panel:

```bash
sudo @APP_DIR@/.venv/bin/python @APP_DIR@/cli.py reset-password <admin>
sudo @APP_DIR@/.venv/bin/python @APP_DIR@/cli.py unlock <username>
sudo @APP_DIR@/.venv/bin/python @APP_DIR@/cli.py unfreeze-all
```

(The installer does **not** put the CLI on `PATH`; copy it to
`/usr/local/bin/parental-controls` once if you want a shorter command.)

## Known limitations

These are deliberate, current, and not defects in disguise:

- **No TLS.** The panel binds `127.0.0.1` by default and is meant to be reached
  over an SSH tunnel. If you opt into a LAN bind, session cookies travel in the
  clear.
- **A blanket passwordless-sudo rule for the service account still exists on the
  development machine**, which masks the scoped sudoers rules there. The
  installer never creates such a rule — a fresh install only writes the two
  scoped files — but check for a pre-existing blanket rule on any machine you
  deploy to, because while one exists least privilege cannot be *proven*.
  Tracked as a sudo audit (`PLAN.md` Phase 3).
- **Locking does not stop running apps.** `usermod -L` blocks authentication;
  budget enforcement (the freezer) is what pauses running applications. Only
  Emergency logout ends a session.
- **No tamper-evident audit log.** The only trail is the per-user bonus history
  (last 20 entries). "Who locked whom, and when" is not recorded durably yet.
- **JSON state, two writers, no file locking.** The web app, its 60-second
  thread and the enforce timer all read-modify-write the same files, and a
  corrupt file is read as empty rather than refused — the service stays up but
  forgets the schedule until you restore a backup. Keep a copy of the four
  state files. SQLite migration is on the roadmap.
- **Usage is billed by CPU time, not focus time.** A background process that
  barely uses the CPU does not burn budget. Accurate focus measurement needs a
  per-user session agent (not shipped).
- **The UI loads Bootstrap 5 from a CDN**, so styling needs internet access on
  the machine showing the panel. Functionality is unaffected offline.
- **Bonus time is per account, not per app.** It suppresses the bedtime lock
  only; budgets keep running.

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
| `@APP_DIR@/bedtime.json` | Bedtime schedules, lock ownership, bonus grants + history | Yes |
| `@APP_DIR@/budgets.json` | Per-category limits and today's usage | Yes |
| `@APP_DIR@/process_prefs.json` | Per-user Hide/Show process choices | Yes |
| `/etc/parental-controls/auth.json` | Admin accounts and password hashes | Yes (outside the install dir entirely) |

All four are gitignored (and the auth store lives outside the repo entirely),
and `install.sh` excludes the three in the install dir from the code sync, so
deploying any version over any version leaves them alone. **A backup is just a
copy of those four files.**

### Is a downgrade safe?

Mostly yes, with two honest caveats.

`load_bedtime()` / `save_bedtime()` read and write the whole JSON document, and
every code path that updates bedtime state edits individual keys in place
(bedtime enforcement, manual lock/unlock bookkeeping, bonus grant/revoke).
Older builds never read the `bonus_*` keys, so they pass them through
**untouched and inert** — a stale `bonus_until` cannot confuse an older build,
and nothing crashes.

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

# 2. Redeploy from that checkout — state is preserved:
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
- **To clear it by hand**: stop the service, remove the `bonus_until`,
  `bonus_minutes`, `bonus_granted_by`, `bonus_granted_at` and `bonus_history`
  keys from that user's object in `@APP_DIR@/bedtime.json`, start the service.
  A value that is not a parseable ISO timestamp is treated as "no bonus" anyway,
  so a corrupted value fails closed — it never leaves the account unlocked.

### Two rollbacks to think twice about

- **Below `91bdc87`** (the MVP-auth merge): there is **no authentication**. Do
  not deploy that anywhere reachable — localhost + SSH tunnel only.
- **Below `fbecb33`/`921ea54`** (the lock-logs-out fix): locking runs
  `loginctl terminate-user` again, so bedtime locks will **log the child out of
  their session**, not just lock the screen.