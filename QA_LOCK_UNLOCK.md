# QA Report — Lock / Unlock core mechanisms

Branch: `qa/lock-unlock` (merged to `master`)
Scope: the product's two core functions (lock, screen-lock, unlock, logout, and
the bedtime/timer enforcement paths that drive them).

> **Release-1.0 refresh (2026-10-10, docs only).** The findings below stand. What
> changed since the original run: the lock-logs-out regression was fixed so *all*
> lock paths preserve the session (`921ea54`), app-only admin auth + CSRF shipped
> (`feature/mvp-auth`), and the notification sudo grant is now deployed and
> reached through `sudo` (`98b2cfd`, `24fb669`). Remaining-risk items 5 and 6
> below record those closures; item 7 is the one live gap they leave behind.
> The physical-machine checks above and the suite counts still describe the
> state of the original run unless explicitly re-dated.

## Summary

Two real bugs, both fixed. The critical one is exactly the owner's report: **a
locked account could be silently unlocked by the bedtime timer and log back in.**

| Report | Verdict |
|---|---|
| "If I log out of a 'locked' account I can log back in without an issue." | **Confirmed, reproduced, root-caused, fixed.** |
| "None of the buttons are working right now." | **Partly a real bug** (screen-lock targeted the wrong session). The account lock itself works. |

`usermod` was **not** at fault. I verified against real `/etc/shadow` and the real
PAM stack that `-L` prepends exactly one `!`, is idempotent, and that `-U` removes
exactly one `!` and never over-strips. The bugs were in the app's own state
bookkeeping and command targeting.

## How authentication was actually tested

`sudo -u <user> <cmd>` is **not** a lock test — sudo has already authorized you and
bypasses PAM password auth entirely. Every auth assertion here went through
`su - <user>` driven over a pty (`pexpect`), which runs the real PAM stack:

| Account state | `passwd -S` | `su -` with correct password |
|---|---|---|
| Locked | `L` | **REJECTED** (`su:`) |
| Unlocked | `P` | **ACCEPTED** (reached shell) |

Harness validated against both states before being trusted — an early run reported
REJECTED for an *unlocked* account because my prompt regex was wrong, not because
PAM was refusing. That false result is why the control case is asserted in
`tests/test_lock_usermod_real.py`.

## Root cause 1 (CRITICAL): manual locks were undone by the bedtime timer

**Where:** `app.py` — `/api/lock`, `/api/lock-screen`, `/api/logout` called
`clear_manual_override()`, which cleared the `manual_override` flag but left
`locked_by_bedtime` set.

**Mechanism:** if the account was *already* bedtime-locked and the parent then
locked it manually, ownership still said "bedtime". When the bedtime window ended,
`enforce_bedtime()` read that stale flag, ran `usermod -U`, and undid the
deliberate parental lock. The app still showed the account as Locked right up until
the unlock happened.

This matters because enforcement runs **two ways**: the 60s thread inside the web
app, and `parental-controls-enforce.timer` in a **separate process**. A parent
pressing "Lock" in the UI could be undone within 60 seconds by a process they never
interacted with.

**Reproduced with real `usermod` + real PAM**, on a disposable account:

```
1. bedtime lock         passwd -S=L  su=REJECTED   locked_by_bedtime=True
2. parent manual lock   passwd -S=L  su=REJECTED   locked_by_bedtime=True  <-- stale
3. bedtime window ends  passwd -S=P  su=ACCEPTED   <-- parent's lock undone
RESULT: BUG CONFIRMED
```

**Fix (commit `6926098`):**
1. `note_manual_lock()` replaces `clear_manual_override()` on the three manual-lock
   endpoints. It clears **both** flags, transferring ownership away from bedtime.
   Bedtime's own auto-locks still set `locked_by_bedtime` directly and keep their
   ownership, so automatic unlock at the end of a window still works.
2. `enforce_bedtime()` now re-reads ownership from disk immediately before
   `usermod -U`, closing the cross-process window where a manual lock lands between
   the snapshot and the unlock.

## Root cause 2: "Lock Screen" locked the parent's screen, not the child's

**Where:** `/api/lock` and `/api/lock-screen` ran `sudo loginctl lock-session` with
**no session ID**.

Per `man loginctl`: *"If no argument is specified, the session of the caller is
locked."* The caller is the Flask app running as the parent. So the button locked
**the parent's own screen** and left the child untouched — which reads to the user
as "the button does nothing".

Confirmed two ways:
- `/api/lock` and `/api/lock-screen` were **byte-identical implementations**.
- Invoking `loginctl lock-session` with no ID under a sessionless context (how the
  systemd unit runs) fails outright: `Failed to issue method call: Unknown object
  '/org/freedesktop/login1/session/auto'`.

**Fix (commit `6926098`):** `lock_user_sessions()` resolves the target's sessions
via `loginctl show-user <u> -p Sessions --value` first, then locks exactly those.
Best-effort and never raises — a failed screen lock must not fail the account lock.

**Note:** this made screen-lock depend on sudoers rules that were **not installed**
at the time of this run, so screen lock was a no-op until they were added and
deployed (both are done now — see Remaining risk #1; the account lock was never
affected).

## Root cause 3 (found while testing): leading-dash username injection

**Where:** all ten state-changing endpoints validated usernames with
`all(c.isalnum() or c in '_-.' ...)`, which accepts a leading `-`.

The name is passed as a bare argv element to a root command via sudo, so
`/api/unlock` with `username=-x` became `sudo usermod -U -x` →
`usermod: invalid option -- 'x'`. It fails loudly here, but it is the same shape as
option injection into a command that rewrites `/etc/passwd` and `/etc/shadow`.

**Fix (commit `35b7316`):** `valid_username()` keeps the charset check and rejects
a leading `-`. No real account can begin with `-` (none in `/etc/passwd`; `useradd`
requires `--badname`), so this costs nothing.

## What was verified as working (no change needed)

- `usermod -L` prepends exactly one `!`; `-U` removes exactly one and never
  over-strips; double/triple lock is idempotent (no `!!` stacking).
- `passwd -S` read-back maps correctly to `L`/`P`/`NP` and matches reality after
  each operation. Unknown status, command failure, timeout and truncated output all
  report *unknown* rather than silently reporting "Unlocked".
- `run_lock_command('lock')` preserves the session: `usermod -L` plus a
  best-effort screen-lock (`lock_user_sessions`). It never calls
  `terminate-user` — session termination lives only in `/api/logout`
  (emergency logout, by design). An offline user (or a screen-lock
  failure) still reports success with a note, so the offline
  auto-unlock bookkeeping is sound.
- Account lock genuinely blocks authentication at the PAM layer. PAM
  configuration is stock (`pam_unix.so nullok`, no `sssd-shadowutils` in the login
  path); `nullok` only permits an *empty* password and no account has one.
- Wheel-admin self-protection is intact through all lock paths.

## Tests

`tests/test_lock_regression.py` — **52 tests**, fully mocked, never touch a real
account:

- Shadow contract: one-bang prepend, one-bang removal, no over-strip, idempotent
  double/triple lock, and all 64 short lock/unlock sequences leaving the hash intact
  with at most one leading `!`.
- `run_lock_command` success/failure matrix, including offline-user
  success (screen-lock failure degrades to a note, never a failure).
- Lock status read-back vs reality after every operation.
- **Manual lock ownership** — the owner's critical report: a manual lock survives
  the bedtime window ending and the schedule being cleared; a stale snapshot never
  unlocks; bedtime still auto-unlocks what it locked.
- Safeguards not bypassed: wheel admins never auto-locked, bedtime keeps ownership,
  locking never invokes the cgroup freezer, lock endpoints never touch budgets.
- Screen lock resolves target sessions and never issues a bare `lock-session`.
- All manual-lock endpoints call `note_manual_lock`, only after `usermod` succeeded,
  and reject invalid/leading-dash usernames with no `shell=True`.

`tests/test_lock_usermod_real.py` — **9 tests**, real `/etc/shadow` + real PAM
via `su -`. Creates and deletes its own throwaway account; never touches
`tester`/`stan`/`bluefox`/root/PID 1; never calls `terminate-user`,
`lock-session` or `kill`; skips itself without privileges.

**Full suite: 122 tests, all green (was 61)** — the counts at the time of this
run. The suite has grown substantially since (`tests/`, 13 files, 300+ test
methods); those later runs are not re-verified in this report.

I confirmed the tests actually catch the bugs by reverting each fix in turn:

| Reverted fix | Tests that failed |
|---|---|
| Manual-lock ownership | 4 (`test_manual_lock_survives_*`, `test_note_manual_lock_*`) |
| Target session resolution | 2 (`test_never_calls_bare_lock_session`, `test_resolves_target_sessions_first`) |
| Stale-snapshot re-read | 1 (`test_stale_snapshot_does_not_unlock`) |

## Safety compliance

- `bluefox` was locked/unlocked via real `usermod` and restored **byte-identical**
  to its original shadow field (`!$y$j9T$Ftw5QTFb/...`, `passwd -S` = `L`). Its
  bedtime config was never modified. Its session was never terminated.
- `tester` and `stan` were never locked, unlocked, terminated or signalled.
- PID 1 / system users untouched.
- `parental-controls.service` was **not** restarted or stopped; all HTTP testing
  used Flask's test client. `master` worktree is clean and unmodified.
- `/opt/parental` read-only.
- Scratch accounts (`qa_lockprobe`, `qa_lockprobe_regress`) deleted; scratch files
  live in `/tmp/opencode`, outside the repo.
- No secrets or real password hashes committed — tests use a generated password
  and `$y$...PLACEHOLDER...` hash.

One process note: an early draft of `test_invalid_username_rejected` was **not**
mocked and briefly ran a real `usermod -U -x` before failing. That was the bug
above, not an accident of the fix — the command was rejected by `usermod`
(`invalid option`), and I confirmed all protected accounts were intact
immediately. The test is now mocked so a bug in it can never reach a root command.

## Not verifiable headlessly — please confirm on the physical machine

This environment is a VM: display manager is `plasmalogin`, there is no
`/etc/gdm/custom.conf`, and no graphical seat session could be exercised from
here. The account-level mechanism is proven; the **greeter/session** half is not.

Please run these as the parent, at the machine:

1. **Screen lock (the fix's main user-visible change).**
   Child logged in and at the desktop. Click **Lock** for the child.
   - Expected: the account locks (status column reads Locked) **and the
     child's** screen locks. Your own screen must stay unlocked.
   - Before the fix your screen would lock instead and the child stayed live.
   - There is no separate "Lock Screen" button any more — the button was
     removed once Lock was confirmed to do both.

2. **Locked account cannot log back in (the critical report).**
   - Click **Lock** for the child. Confirm the status column reads Locked.
   - Have the child log out at the greeter and try to sign back in with the
     correct password.
   - Expected: **rejected**. Confirm it stays rejected over >60 s, so the bedtime
     timer has run at least once.

3. **Manual lock is now sticky across bedtime boundaries.**
   Set a bedtime window that is currently active, confirm the lock, then press
   **Lock** manually as well. Wait for the window to end.
   - Expected: the account **stays locked**. This is the intended behaviour
     change — previously the manual lock was silently reverted here.
   - To get bedtime's automatic unlock back, unlock manually or clear the schedule.

4. **All lock paths preserve the session.**
   Every lock path — the **Lock** button, the `/api/lock-screen` endpoint, the
   bedtime auto-lock, the enforce-on-bedtime-save path, and bonus
   grant/revoke/expiry — does `usermod -L` and then screen-locks the child's
   graphical sessions. The child stays logged in with apps running; the account
   simply cannot authenticate again.
   Only **Emergency logout** (`/api/logout`) runs `loginctl terminate-user` and
   ends the sessions. (Updated 2026-10-10: an earlier draft of this item said
   "Lock must terminate the session"; that was the lock-logs-out regression,
   fixed in `921ea54`.) The UI has a single **Lock** button, which performs the
   account lock and the screen lock together.

5. **Wheel-admin protection still holds.**
   A `wheel` member must never be auto-locked by bedtime.

## Remaining risks / follow-ups

1. **RESOLVED — sudoers cover the screen-lock commands, and they are deployed.**
    The `sudoers-parental-controls` template in this repo permits
    `loginctl show-user *`, `loginctl lock-session *` and
    `loginctl show-session *` (sbin + bin paths, pinned by
    `tests/test_deployment.py::TestSudoersTemplates`), and `sudo ./install.sh`
    has rendered them into `/etc/sudoers.d/parental-controls` (visudo-validated).
    Screen lock is authorized on a correctly-scoped box. Original report kept
    below:

    Original report: `/etc/sudoers.d/parental-controls` previously permitted only
   `usermod -L *`, `usermod -U *` and `loginctl terminate-user *`.
   The screen-lock fix calls `loginctl show-user *` (read-only session lookup) and
   `loginctl lock-session *`, neither of which is permitted. Both are invoked
   through `sudo`, so on a hardened box they fail and screen-lock degrades to
   the harmless `"no active session"` message.

   Add to `/etc/sudoers.d/parental-controls` (keep it least-privilege):
   ```
   stan ALL=(root) NOPASSWD: /usr/bin/loginctl show-user *, /usr/bin/loginctl lock-session *
   ```
   then `sudo visudo -c` to validate. I did **not** modify sudoers — it is
   security-sensitive and affects the running production unit.

   I verified the failure mode is graceful, not destructive: `lock_user_sessions`
   returns a short note and never raises, and the **account lock still works**
   either way. Only the screen lock is affected.

   Note this gap was invisible on this box because
   `/etc/sudoers.d/stan-nopasswd` grants stan `NOPASSWD: ALL`, which masks
   every sudoers restriction. That blanket rule is **still in place** and is
   tracked as its own open item (item 7 below).

2. **Lock does not kill processes.** `usermod -L` prevents *authentication*; it
   does not stop an already-running session or its processes. Since the
   2026-10-10 lock-logs-out fix, bedtime/bonus locks also preserve the
   session (screen-lock only) — running apps are governed by the budgets
   cgroup freezer instead, and only Emergency logout (`/api/logout`)
   terminates a session. If a session is offline, nothing is
   terminated — by design, but worth knowing.

3. **`manual_override` semantics changed subtly.** A manual lock now also clears
   the override flag, so "parent unlocked during this window" is discarded by a
   subsequent manual lock. Covered by tests; flagging as intended behaviour.

4. **Two writers, no file locking.** `bedtime.json` is read-modify-written by the
   web app, its 60s thread, and the systemd timer with no locking. The fix narrows
   the window but does not eliminate the race. Atomic writes / SQLite are already
   tracked in PLAN.md Phase 4.

5. **RESOLVED — authentication now ships.** This was a pre-existing MVP gate item:
   at the time of this QA run anyone reaching port 5000 could drive these endpoints.
   Every page and `/api/*` route now requires an admin session
   (`feature/mvp-auth`, design in `AUTH_DESIGN.md`), with CSRF on every
   state-changing request and a localhost-only bind by default.

6. **Notifications under scoped sudoers — RESOLVED, deployed (2026-10-10).**
   `notify_user()` reaches the child's session via
   `systemd-run --machine=<user>@.host --user notify-send -u critical ...`, and the
   machine transport needs root to open the *system* bus. `sudoers-parental-controls`
   grants exactly that, nothing more:

   ```
   @SERVICE_USER@ ALL=(root) NOPASSWD: /usr/sbin/systemd-run --machine=*@.host --user notify-send -u critical * *, /usr/bin/systemd-run --machine=*@.host --user notify-send -u critical * *
   ```

   Pinned by `tests/test_deployment.py::TestSudoersTemplates` (grant present,
   scoped, matches the argv `notify_user()` really runs, authorizes nothing
   else, every granted binary exists). Both original follow-ups are now closed:
   - **Deploy pending — DONE.** `sudo ./install.sh` rendered the rule into
     `/etc/sudoers.d/parental-controls` (validated with `visudo -c`).
   - **sudo prefix missing — DONE** (`24fb669`). `notify_user()` now runs
     `['sudo', 'systemd-run', ...]`, like every other privileged call in the app,
     so the grant actually applies. Without the prefix polkit refused with
     "Failed to connect to system scope bus via machine transport: Permission
     denied" and warnings stayed silent no-ops.

   Verified working under scoped sudoers. Warnings remain best-effort by design:
   a failed notification never blocks a lock, and an offline user simply gets
   nothing.

7. **Open: blanket passwordless sudo still masks every scoped rule.** On this
   machine the service account keeps a blanket `NOPASSWD: ALL` rule
   (`/etc/sudoers.d/stan-nopasswd`), which overrides the scoped
   `/etc/sudoers.d/parental-controls*` rules. Consequence: the least-privilege
   story above cannot be *verified in practice* on this box — a bug that ran the
   wrong command would still succeed. Retiring the blanket rule is tracked as a
   sudo audit (`PLAN.md` Phase 3); it is the single most valuable hardening item
   left before this runs on someone else's machine.

8. **Open: multi-writer state files.** See item 4 above — no file locking between
   the web app, its 60s thread and the enforce timer.
