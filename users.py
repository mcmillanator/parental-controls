"""Human users, lock status, and the name guard.

`get_human_users` enumerates the real login accounts on this box (UID within
`MIN_UID`..`MAX_UID`, a real shell, sorted by uid), `get_lock_status` reports
`sudo passwd -S` as (is_locked, detail) — memoized per user for
`_LOCK_STATUS_TTL_SECONDS`, dropped by `invalidate_lock_status` — and
`valid_username` is the app-side spelling of the shared
`device_policy.valid_username` check.

Moved out of app.py by RESTRUCTURE_PLAN.md slice 10 (A1); app.py re-exports
every name here by name, so `app.get_human_users` / `app.get_lock_status` keep
resolving — and keep being the attributes the suite's
`patch.object(app, ...)` sites bind. The cache block too (`_LOCK_STATUS_CACHE`,
`_LOCK_STATUS_TTL_SECONDS`, `invalidate_lock_status`) is re-exported, so the
lock/unlock cores in locks.py reach it as `app.invalidate_lock_status` at
call time and `tests/test_lock_status_cache.py` can pin the dict through the
facade (slice 10's in-place-mutation rule R6: the dict object moves, never a
rebinding, so `app._LOCK_STATUS_CACHE` stays the same object).

**Cross-seam convention.** This module never does `from app import x`. Where a
helper that stayed in the facade is needed the function opens with
`import app` and reads `app.x` — resolved at CALL time, so a patch on the app
module (65 `app.subprocess` sites alone) still reaches this code
(RESTRUCTURE_PLAN.md §1.1). The cache reads `time.monotonic` through the
module import for the same reason: the tests freeze it via
`patch.object(app.time, 'monotonic', ...)` and app.time IS this `time`.
"""

import pwd
import subprocess  # the TimeoutExpired class; the call itself is app.subprocess
import time  # monotonic only; patched through app.time at call time

MIN_UID = 1000
MAX_UID = 60000


def get_human_users():
    """Return a list of human users (UID >= MIN_UID, not system accounts)."""
    users = []
    for entry in pwd.getpwall():
        if entry.pw_uid < MIN_UID or entry.pw_uid > MAX_UID:
            continue
        # Skip system accounts that happen to have high UIDs
        if entry.pw_shell in ('/usr/sbin/nologin', '/sbin/nologin', '/bin/false'):
            continue
        users.append({
            'username': entry.pw_name,
            'uid': entry.pw_uid,
            'home': entry.pw_dir,
            'shell': entry.pw_shell,
        })
    users.sort(key=lambda u: u['uid'])
    return users


# ---------------------------------------------------------------------------
# Lock-status probe cache (live finding, 2026-10-18)
#
# `sudo passwd -S <user>` is a full sudo+PAM session — ONE PER ACCOUNT PER
# CALL. The Overview polls /api/users every 10s and builds a card per
# account, so a four-account box burned ~24 sudo PAM sessions a minute on
# the read alone (plus the per-minute enforce timer's own probes). Past
# logind's `maxlogins` ceiling logind refuses new sessions with
# `TooManySessions`, `sudo passwd -S` exits non-zero, `get_lock_status`
# returns (None, stderr) and every child card showed the "needs help"
# warning box the parent could do nothing about.
#
# So the probe result is cached per username for
# `_LOCK_STATUS_TTL_SECONDS`. What is cached is the RAW (locked, detail)
# tuple — never the classified display text: `status_row` (app.py) still
# runs `status_display()` and `audit_status_probe()` on every call, so the
# classification and the once-per-change admin audit are untouched. A
# cached repeat simply dedups to the same (class, raw) signature and emits
# nothing; a genuinely NEW failure still re-emits.
#
# TTL vs freshness: 30s outlives the 10s poll (a poll burst never
# re-probes the same account) and stays well inside a parent's attention
# span, so a change made outside the app surfaces within a few polls.
#
# INVALIDATION: every lock/unlock the app performs drops the entry at once,
# so a parent's own action shows up on the very next render instead of up
# to 30s later. It lives in the three cores every lock/unlock path funnels
# through — `run_lock_command`, `_lock_account_and_screen`,
# `_emergency_logout` (locks.py) — which reach this cache the way those
# cores reach every other facade-side helper: `app.invalidate_lock_status`
# resolved at call time, so a patch on the app module still binds (plan
# §1.1). The /api/* twins, the /overview/* form twins and the bedtime/bonus
# enforcement passes are covered by construction.
# ---------------------------------------------------------------------------

_LOCK_STATUS_TTL_SECONDS = 30.0
_LOCK_STATUS_CACHE = {}  # username -> (monotonic time, (locked, detail))


def _probe_lock_status(username):
    """Run `sudo passwd -S <user>` once. Returns (is_locked, detail_string).

    The uncached probe. Everything goes through `get_lock_status` below,
    which memoizes this for `_LOCK_STATUS_TTL_SECONDS`.
    """
    import app
    try:
        result = app.subprocess.run(
            ['sudo', 'passwd', '-S', username],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None, result.stderr.strip() or 'Unknown status'
        # Output format: <username> <status> <date> ...
        # Status: L = locked, P = password set (unlocked), NP = no password
        parts = result.stdout.strip().split()
        if len(parts) < 2:
            return None, 'Unexpected passwd output'
        status_field = parts[1]
        if status_field == 'L':
            return True, 'Locked'
        elif status_field == 'P':
            return False, 'Unlocked'
        elif status_field == 'NP':
            return True, 'No password'
        else:
            return None, f'Unknown status: {status_field}'
    except subprocess.TimeoutExpired:
        return None, 'Command timed out'
    except FileNotFoundError:
        return None, 'passwd command not found'
    except Exception as e:
        return None, str(e)


def get_lock_status(username):
    """Cached account-lock read. Returns (is_locked, detail_string).

    The RAW probe result is memoized per username for
    `_LOCK_STATUS_TTL_SECONDS` — see the cache block above for why (sudo
    session pressure on logind's `maxlogins`), what exactly is cached and
    why classification/auditing still see every transition. Callers must
    invalidate through `invalidate_lock_status` after they change the
    state the probe reads.
    """
    now = time.monotonic()
    hit = _LOCK_STATUS_CACHE.get(username)
    if hit is not None and now - hit[0] < _LOCK_STATUS_TTL_SECONDS:
        return hit[1]
    result = _probe_lock_status(username)
    _LOCK_STATUS_CACHE[username] = (now, result)
    return result


def invalidate_lock_status(username=None):
    """Drop cached lock-status probes: one account, or all when None.

    Called by every lock/unlock core so the parent's own action is
    reflected immediately; `username=None` clears the whole map (tests,
    and any future "the whole box just changed" hook).
    """
    if username is None:
        _LOCK_STATUS_CACHE.clear()
    else:
        _LOCK_STATUS_CACHE.pop(username, None)


def valid_username(username):
    """True when `username` is safe to pass as a literal usermod/loginctl
    argument.

    The rule lives in device_policy so the remote agent applies the exact
    same check on its own box (MULTIDEVICE_PLAN.md §4.4).
    """
    import app
    return app.device_policy.valid_username(username)
