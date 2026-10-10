# QA Report — Lock / Unlock core mechanisms

Branch: `qa/lock-unlock`
Scope: the product's two core functions (lock, screen-lock, unlock, logout, and
the bedtime/timer enforcement paths that drive them).

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

**Note:** this makes screen-lock depend on a sudoers rule that is **not currently
installed**, so screen lock will be a no-op until it is added (see Remaining
risk #1). The account lock is unaffected.

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

**Full suite: 122 tests, all green (was 61).**

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
   Child logged in and at the desktop. Click **Lock Screen** for the child.
   - Expected: the **child's** screen locks. Your own screen must stay unlocked.
   - Before the fix your screen would lock instead and the child stayed live.

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

4. **Lock and Lock Screen both preserve the session.**
   **Lock Screen** and **Lock** both lock the account (`usermod -L`) and
   screen-lock the child's graphical sessions; the child stays logged in
   with apps running. Only **Emergency logout** (`/api/logout`)
   terminates the child's session. (Corrected 2026-10-10: this item
   previously said Lock must terminate; that was the lock-logs-out
   regression.)
   **Lock Screen** must leave the child logged in (screen locked, session alive).
   **Lock** must do the same (account locked + screen locked, session alive).
   Only **Emergency logout** terminates the session.

5. **Wheel-admin protection still holds.**
   A `wheel` member must never be auto-locked by bedtime.

## Remaining risks / follow-ups (not fixed here)

1. **RESOLVED — sudoers templates now cover the screen-lock commands.**
    The `sudoers-parental-controls` template in this repo permits
    `loginctl show-user *`, `loginctl lock-session *` and
    `loginctl show-session *` (sbin + bin paths, pinned by
    `tests/test_deployment.py::TestSudoersTemplates`), so screen lock is
    authorized once rendered by `install.sh`. Production `/etc/sudoers.d/`
    was NOT touched — it is security-sensitive. Original report kept below:

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

   Note this gap is currently invisible on this box because
   `/etc/sudoers.d/stan-nopasswd` grants stan `NOPASSWD: ALL`, which masks
   every sudoers restriction. That rule should be reviewed separately — it is
   effectively "the app's user can be root with no password for anything",
   which defeats least-privilege sudoers entirely.

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

5. **No authentication on the app** — pre-existing MVP gate item, unrelated to
   this work, but it means anyone reaching port 5000 can drive these endpoints.