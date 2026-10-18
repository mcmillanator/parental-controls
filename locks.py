"""Account lock/unlock primitives: usermod + loginctl via scoped sudo.

`lock_user_sessions` screen-locks the target's graphical sessions (never
terminates them), `_session_supports_lock` keeps loginctl from being called on
a session it cannot lock, and `run_lock_command` is the one place a lock or
unlock happens (`sudo usermod -L/-U`) so the audit trail is identical whoever
asks. The "account-action cores" (`_lock_account_and_screen`,
`_unlock_account`, `_emergency_logout`) are shared by the JSON API and the
no-JS Overview forms,
so both entry points behave — and audit — identically.

Moved out of app.py by RESTRUCTURE_PLAN.md slice 10 (A2); app.py re-exports
every name here by name. `subprocess.run` resolves `app.subprocess.run` at
call time (65 patch sites in 8 test modules depend on it), and the audit /
ownership hooks resolve `app._safe_audit` / `app.note_manual_lock` /
`app.run_lock_command` the same way — see the cross-seam convention in
users.py.
"""

import subprocess  # the TimeoutExpired class; calls are app.subprocess.run


def lock_user_sessions(username):
    """Screen-lock every graphical session belonging to `username`.

    `loginctl lock-session` with no session ID locks the session of the
    *caller* -- which is the parent running this app, not the target
    child. That both fails to lock the child and (worse) locks the
    parent's own screen. So resolve the target's session IDs first.

    Only graphical (`wayland`/`x11`) sessions of class `user` are locked:
    manager/greeter/tty sessions reject the call with "Session does not
    support lock screen", which previously surfaced as a Lock Screen
    failure even when a lockable session existed.

    Returns a short human-readable note describing what happened.
    Never raises; screen lock is best-effort and must not fail a lock.
    """
    import app
    try:
        result = app.subprocess.run(
            ['sudo', 'loginctl', 'show-user', username, '-p', 'Sessions',
             '--value'],
            capture_output=True, text=True, timeout=15
        )
        session_ids = result.stdout.split() if result.returncode == 0 else []
        if not session_ids:
            return 'no active session'
        lockable = [sid for sid in session_ids
                    if _session_supports_lock(sid)]
        if not lockable:
            return 'no lockable (graphical) session'
        result = app.subprocess.run(
            ['sudo', 'loginctl', 'lock-session'] + lockable,
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return f'loginctl lock-session: {result.stderr.strip() or "failed"}'
        return f'{len(lockable)} session(s) locked'
    except subprocess.TimeoutExpired:
        return 'screen lock timed out'
    except Exception as e:
        return f'screen lock error: {e}'


def _session_supports_lock(session_id):
    """True when a session is a graphical user session logind can lock.

    Anything else (manager/greeter sessions, tty logins, vanished
    sessions) is skipped: locking those fails with "Session does not
    support lock screen". Never raises.
    """
    import app
    try:
        result = app.subprocess.run(
            ['sudo', 'loginctl', 'show-session', session_id,
             '-p', 'Class', '-p', 'Type'],
            capture_output=True, text=True, timeout=15
        )
    except (subprocess.TimeoutExpired, Exception):
        return False
    if result.returncode != 0:
        return False
    props = {}
    for line in result.stdout.splitlines():
        if '=' in line:
            key, _, value = line.partition('=')
            props[key.strip()] = value.strip()
    return (props.get('Class') == 'user'
            and props.get('Type') in ('wayland', 'x11'))


def run_lock_command(username, action, actor=None):
    """Run lock or unlock commands for a user.

    Returns (success, message).

    `actor` overrides the audit actor; when omitted it resolves to the
    logged-in admin inside requests and 'system' elsewhere (so web UI
    and enforcement/timer paths share this helper without the caller
    threading identity through). The audit write is best-effort and
    can never change the returned outcome.

    For 'lock', account ownership (usermod -L) is what success reflects,
    and the session is ALWAYS preserved: after locking the account the
    user's graphical sessions are screen-locked (lock_user_sessions),
    never terminated. Session termination lives ONLY in the /api/logout
    emergency-logout endpoint, which runs its own terminate-user call.

    Screen lock is best-effort and never fails the lock: an offline user
    (or a screen-lock failure) still reports success with a note, so
    callers tracking lock ownership still record it.

    Tradeoff: bedtime/bonus/revoke auto-locks no longer stop already-
    running apps (terminate-user did that as a side effect). App runtime
    is governed by the budgets cgroup freezer instead; the account lock
    itself still blocks all new authentication at the PAM layer.
    """
    import app
    act = actor or app._req_actor()
    audit_action = ('account.lock' if action == 'lock'
                    else 'account.unlock' if action == 'unlock'
                    else 'account.unknown')

    def _done(success, message):
        app._safe_audit(act, audit_action, username,
                        {'success': success, 'message': message})
        return success, message

    if action == 'lock':
        try:
            result = app.subprocess.run(
                ['sudo', 'usermod', '-L', username],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                return _done(False, (f'sudo usermod -L {username}: '
                               f'{result.stderr.strip() or "failed"}'))
        except subprocess.TimeoutExpired:
            return _done(False, f'sudo usermod -L {username}: timed out')
        except Exception as e:
            return _done(False, f'sudo usermod -L {username}: {e}')
        # Screen-lock the user's sessions (best-effort, never raises).
        # This deliberately never calls terminate-user: locking must not
        # log the user out.
        try:
            screen_msg = lock_user_sessions(username)
        except Exception as e:
            screen_msg = f'screen lock error: {e}'
        return _done(True, f'OK ({screen_msg})')
    elif action == 'unlock':
        cmds = [
            ['sudo', 'usermod', '-U', username],
        ]
    else:
        return _done(False, f'Unknown action: {action}')

    errors = []
    for cmd in cmds:
        try:
            result = app.subprocess.run(
                cmd, capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                errors.append(
                    f'{" ".join(cmd)}: {result.stderr.strip() or "failed"}'
                )
        except subprocess.TimeoutExpired:
            errors.append(f'{" ".join(cmd)}: timed out')
        except Exception as e:
            errors.append(f'{" ".join(cmd)}: {e}')

    if errors:
        return _done(False, '; '.join(errors))
    return _done(True, 'OK')


# ---------------------------------------------------------------------------
# Account-action cores (shared by the JSON API and the no-JS Overview forms)
#
# Every lock/unlock/logout path -- the /api/* JSON endpoints and the
# /overview/* plain-POST form endpoints -- runs exactly these functions, so
# the behaviour (and the audit trail) is identical whichever way the browser
# submits it. Each returns (ok, message); the caller decides JSON vs redirect.
# ---------------------------------------------------------------------------

def _lock_account_and_screen(username, via):
    """Lock the account (usermod -L) and best-effort screen-lock it.

    Session-preserving: it never terminates the user's sessions (that is
    /api/logout's job). Returns (ok, message).
    """
    import app
    try:
        result = app.subprocess.run(
            ['sudo', 'usermod', '-L', username],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            msg = 'usermod -L: ' + (result.stderr.strip() or 'failed')
            app._safe_audit(app._req_actor(), 'account.lock', username,
                        {'success': False, 'message': msg, 'via': via})
            return False, msg
    except Exception as e:
        msg = 'usermod -L error: ' + str(e)
        app._safe_audit(app._req_actor(), 'account.lock', username,
                    {'success': False, 'message': msg, 'via': via})
        return False, msg

    # A manual lock takes ownership away from bedtime so the bedtime
    # timer cannot later auto-unlock it (see note_manual_lock).
    app.note_manual_lock(username)

    # Lock the screen without terminating the session.
    screen_msg = lock_user_sessions(username)

    app._safe_audit(app._req_actor(), 'account.lock', username,
                {'success': True, 'message': f'OK ({screen_msg})', 'via': via})
    return True, f'Account locked and screen locked ({screen_msg})'


def _unlock_account(username):
    """Unlock the account and hand ownership to the manual unlock."""
    import app
    success, message = app.run_lock_command(username, 'unlock')
    if success:
        # Manual unlock takes precedence over bedtime (see note_manual_unlock).
        app.note_manual_unlock(username)
    return success, message


def _emergency_logout(username):
    """Lock the account and terminate ALL its sessions immediately."""
    import app
    try:
        result = app.subprocess.run(
            ['sudo', 'usermod', '-L', username],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            msg = 'usermod -L: ' + (result.stderr.strip() or 'failed')
            app._safe_audit(app._req_actor(), 'account.logout', username,
                        {'success': False, 'message': msg})
            return False, msg
    except Exception as e:
        msg = 'usermod -L error: ' + str(e)
        app._safe_audit(app._req_actor(), 'account.logout', username,
                    {'success': False, 'message': msg})
        return False, msg

    # A manual lock takes ownership away from bedtime so the bedtime
    # timer cannot later auto-unlock it (see note_manual_lock).
    app.note_manual_lock(username)

    try:
        result = app.subprocess.run(
            ['sudo', 'loginctl', 'terminate-user', username],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            msg = ('loginctl terminate-user: '
                   + (result.stderr.strip() or 'failed'))
            app._safe_audit(app._req_actor(), 'account.logout', username,
                        {'success': False, 'message': msg})
            return False, msg
    except Exception as e:
        msg = 'loginctl terminate-user error: ' + str(e)
        app._safe_audit(app._req_actor(), 'account.logout', username,
                    {'success': False, 'message': msg})
        return False, msg

    app._safe_audit(app._req_actor(), 'account.logout', username,
                    {'success': True})
    return True, 'Session terminated. User logged out.'
