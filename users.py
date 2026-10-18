"""Human users, lock status, and the name guard.

`get_human_users` enumerates the real login accounts on this box (UID within
`MIN_UID`..`MAX_UID`, a real shell, sorted by uid), `get_lock_status` reports
`sudo passwd -S` as (is_locked, detail), and `valid_username` is the app-side
spelling of the shared `device_policy.valid_username` check.

Moved out of app.py by RESTRUCTURE_PLAN.md slice 10 (A1); app.py re-exports
every name here by name, so `app.get_human_users` / `app.get_lock_status` keep
resolving — and keep being the attributes the suite's
`patch.object(app, ...)` sites bind.

**Cross-seam convention.** This module never does `from app import x`. Where a
helper that stayed in the facade is needed the function opens with
`import app` and reads `app.x` — resolved at CALL time, so a patch on the app
module (65 `app.subprocess` sites alone) still reaches this code
(RESTRUCTURE_PLAN.md §1.1).
"""

import pwd
import subprocess  # the TimeoutExpired class; the call itself is app.subprocess

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


def get_lock_status(username):
    """Check if a user account is locked.

    Returns (is_locked, detail_string).
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


def valid_username(username):
    """True when `username` is safe to pass as a literal usermod/loginctl
    argument.

    The rule lives in device_policy so the remote agent applies the exact
    same check on its own box (MULTIDEVICE_PLAN.md §4.4).
    """
    import app
    return app.device_policy.valid_username(username)
