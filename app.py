#!/usr/bin/env python3
"""Parental Controls Web UI for Bazzite.

A lightweight Flask app that lists human users on the system and allows
locking/unlocking their accounts via sudo commands.
"""

import subprocess
import pwd
import html
import os
import json
import secrets
import threading
import time
import traceback
import functools
from datetime import datetime, timedelta
from flask import (Flask, render_template, request, jsonify, redirect,
                   url_for, g, make_response)

import auth
import budgets

app = Flask(__name__)

# MVP: no TLS on localhost, so Secure cookies are opt-in for the day a
# TLS-terminating setup exists (PARENTAL_TLS=1). HttpOnly + SameSite=Lax
# are always set; CSRF tokens are the primary cross-site defense.
app.config['SECURE_COOKIES'] = os.environ.get('PARENTAL_TLS', '') == '1'

MIN_UID = 1000
MAX_UID = 60000

BEDTIME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bedtime.json')

DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


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
    try:
        result = subprocess.run(
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

    Beyond the usual charset check this rejects a leading '-': usernames
    are interpolated as a bare argv element, and a name like '-x' is
    parsed by usermod as an option rather than a login (verified:
    `usermod -U -x` -> "invalid option"). No real account can begin with
    '-', so rejecting one costs nothing and closes an argument-injection
    path into a command that runs as root.
    """
    return bool(username) and not username.startswith('-') and all(
        c.isalnum() or c in '_-.' for c in username
    )


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
    try:
        result = subprocess.run(
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
        result = subprocess.run(
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
    try:
        result = subprocess.run(
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


def run_lock_command(username, action):
    """Run lock or unlock commands for a user.

    Returns (success, message).

    For 'lock', account ownership (usermod -L) is what success reflects:
    a terminate-user failure (e.g. user not logged in) is reported as a
    warning note, not a failure — so callers tracking lock ownership
    still record it.
    """
    if action == 'lock':
        try:
            result = subprocess.run(
                ['sudo', 'usermod', '-L', username],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                return False, (f'sudo usermod -L {username}: '
                               f'{result.stderr.strip() or "failed"}')
        except subprocess.TimeoutExpired:
            return False, f'sudo usermod -L {username}: timed out'
        except Exception as e:
            return False, f'sudo usermod -L {username}: {e}'
        try:
            result = subprocess.run(
                ['sudo', 'loginctl', 'terminate-user', username],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                return True, ('OK (session terminate skipped: '
                              f'{result.stderr.strip() or "not logged in"})')
        except subprocess.TimeoutExpired:
            return True, 'OK (session terminate timed out)'
        except Exception as e:
            return True, f'OK (session terminate error: {e})'
        return True, 'OK'
    elif action == 'unlock':
        cmds = [
            ['sudo', 'usermod', '-U', username],
        ]
    else:
        return False, f'Unknown action: {action}'

    errors = []
    for cmd in cmds:
        try:
            result = subprocess.run(
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
        return False, '; '.join(errors)
    return True, 'OK'


# ---------------------------------------------------------------------------
# Bedtime settings storage (simple JSON file)
# ---------------------------------------------------------------------------

def load_bedtime():
    """Load bedtime settings from JSON file. Returns dict keyed by username."""
    try:
        with open(BEDTIME_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_bedtime(data):
    """Persist bedtime settings to JSON file."""
    with open(BEDTIME_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def note_manual_unlock(username):
    """Record a manual unlock for bedtime bookkeeping.

    Clears bedtime ownership and, if the user is currently in bedtime,
    sets a manual override so enforcement does not re-lock until this
    bedtime window ends or the parent manually locks again.
    """
    try:
        data = load_bedtime()
        entry = data.get(username)
        if entry is not None:
            entry['locked_by_bedtime'] = False
            if is_in_bedtime(entry):
                entry['manual_override'] = True
            save_bedtime(data)
    except OSError:
        pass


def clear_manual_override(username):
    """Clear a bedtime manual override.

    Only clears the override flag; see note_manual_lock for the full
    manual-lock bookkeeping.
    """
    try:
        data = load_bedtime()
        entry = data.get(username)
        if entry is not None and entry.get('manual_override'):
            entry['manual_override'] = False
            save_bedtime(data)
    except OSError:
        pass


def note_manual_lock(username):
    """Record a manual (parent-issued) lock for bedtime bookkeeping.

    A manual lock transfers ownership away from bedtime: it clears
    locked_by_bedtime so the bedtime timer will not later treat the
    account as "locked by bedtime" and silently `usermod -U` it when the
    window ends -- which would undo a deliberate parental lock and let a
    locked account authenticate again.

    It also clears any manual override, since that flag means "parent
    unlocked during the current bedtime window" and a new lock
    supersedes it.

    Called only by the HTTP lock endpoints. Bedtime auto-locks
    (enforce_bedtime) set locked_by_bedtime directly and must not call
    this, or bedtime would lose its own ownership.
    """
    try:
        data = load_bedtime()
        entry = data.get(username)
        if entry is None:
            return
        changed = False
        if entry.get('locked_by_bedtime'):
            entry['locked_by_bedtime'] = False
            changed = True
        if entry.get('manual_override'):
            entry['manual_override'] = False
            changed = True
        if changed:
            save_bedtime(data)
    except OSError:
        pass


def _parse_hhmm(value):
    """Parse 'HH:MM' into minutes since midnight. Returns None if invalid."""
    try:
        hours, minutes = value.strip().split(':')
        h, m = int(hours), int(minutes)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return None


def format_etime(etime):
    """Format ps ETIME (e.g. '21:47:19', '05:23', '1-02:03:04') as '21h 47m'.

    Falls back to the raw string when it cannot be parsed.
    """
    try:
        days = 0
        rest = etime.strip()
        if '-' in rest:
            days_part, rest = rest.split('-', 1)
            days = int(days_part)
        fields = [int(f) for f in rest.split(':')]
        if len(fields) == 3:
            hours, minutes, seconds = fields
        elif len(fields) == 2:
            hours, minutes, seconds = 0, fields[0], fields[1]
        else:
            return etime
        parts = []
        if days:
            parts.append(f'{days}d')
        if hours:
            parts.append(f'{hours}h')
        if minutes:
            parts.append(f'{minutes}m')
        if not parts:
            parts.append(f'{seconds}s')
        return ' '.join(parts)
    except (ValueError, AttributeError):
        return etime


def is_in_bedtime(entry, now=None):
    """Return True if `now` falls inside the configured bedtime window.

    Handles windows that span midnight (e.g. 21:00-08:00): if the bedtime
    starts on day X and runs past midnight, the early-morning hours belong
    to day X's window.
    """
    if not entry:
        return False
    start = _parse_hhmm(entry.get('start', ''))
    end = _parse_hhmm(entry.get('end', ''))
    try:
        days = {int(d) for d in entry.get('days', [])}
    except (ValueError, TypeError):
        return False
    if start is None or end is None or not days or start == end:
        return False

    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    today = now.weekday()  # Monday=0 ... Sunday=6
    yesterday = (today - 1) % 7

    if start < end:
        # Same-day window, e.g. 12:00-14:00
        return today in days and start <= cur < end
    # Overnight window, e.g. 21:00-08:00
    if today in days and cur >= start:
        return True
    if yesterday in days and cur < end:
        return True
    return False


# ---------------------------------------------------------------------------
# Desktop notifications (best-effort warnings before automatic locks)
# ---------------------------------------------------------------------------

def notify_user(username, title, body):
    """Send a desktop notification to a user's running session.

    Runs:
      systemd-run --machine=<user>@.host --user \
        notify-send -u critical <title> <body>

    Best-effort: fails silently (logs only, returns False) when the user
    has no active session. Never raises. Warnings are additive — a failed
    notification never blocks a lock.
    """
    try:
        if not username or not all(
                c.isalnum() or c in '_-.' for c in str(username)):
            print(f'notify_user: invalid username {username!r}')
            return False
        result = subprocess.run(
            ['systemd-run', f'--machine={username}@.host', '--user',
             'notify-send', '-u', 'critical', str(title), str(body)],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            print(f'notify_user: no active session for {username}: '
                  f'{result.stderr.strip() or "systemd-run failed"}')
            return False
        return True
    except Exception as e:
        print(f'notify_user: {username}: {e}')
        return False


# Warn once when a bedtime window starts within this many minutes.
BEDTIME_WARN_MINUTES = 15


def _safe_notify(username, title, body):
    """notify_user that can never raise (belt and braces: notify_user
    already swallows its own errors, but enforcement must not depend on
    any notifier — warnings are additive, never block a lock)."""
    try:
        notify_user(username, title, body)
    except Exception as e:
        print(f'notify (suppressed): {e}')


def bedtime_window_id(entry):
    """Stable id for a bedtime schedule, e.g. '21:00-08:00'.

    Used to track warned state per window so the 60s loop doesn't spam.
    """
    try:
        return f"{entry.get('start')}-{entry.get('end')}"
    except AttributeError:
        return ''


def minutes_until_bedtime(entry, now=None):
    """Minutes until the next bedtime window starts (float).

    Returns 0 when currently inside a window, None when the entry has no
    valid schedule. Pure function of (entry, now) — unit-tested with fake
    times.
    """
    if not entry:
        return None
    start = _parse_hhmm(entry.get('start', ''))
    end = _parse_hhmm(entry.get('end', ''))
    try:
        days = {int(d) for d in entry.get('days', [])}
    except (ValueError, TypeError):
        return None
    if start is None or end is None or not days or start == end:
        return None
    now = now or datetime.now()
    if is_in_bedtime(entry, now):
        return 0
    for offset in range(8):
        day = now + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        candidate = datetime(day.year, day.month, day.day,
                             start // 60, start % 60)
        if candidate > now:
            return (candidate - now).total_seconds() / 60.0
    return None


# ---------------------------------------------------------------------------
# Bedtime enforcement (background thread)
# ---------------------------------------------------------------------------

def enforce_bedtime(now=None):
    """Lock users whose bedtime started; unlock only those bedtime locked.

    Wheel members (admins) are never auto-locked: skipped with a logged
    note so a parent cannot bedtime-lock themselves out.
    """
    now = now or datetime.now()
    data = load_bedtime()
    changed = False
    for username, entry in list(data.items()):
        if budgets.is_admin_protected(username):
            print(f'skipped bedtime enforcement for {username}: '
                  f'{budgets.ADMIN_GROUP} member (admin self-protection)',
                  flush=True)
            continue
        in_bed = is_in_bedtime(entry, now)
        locked_by_bedtime = bool(entry.get('locked_by_bedtime', False))

        if in_bed:
            if entry.get('manual_override'):
                # Parent manually unlocked during this bedtime window:
                # leave the account alone until the window ends.
                continue
            locked, _detail = get_lock_status(username)
            if locked is False:
                success, _msg = run_lock_command(username, 'lock')
                if success:
                    entry['locked_by_bedtime'] = True
                    changed = True
                    # Warn again at lock time (best-effort, never blocks).
                    _safe_notify(username, 'Locked for bedtime',
                                 'Good night! The device unlocks at '
                                 f'{entry.get("end")} '
                                 'unless a parent changes it.')
        else:
            # Pre-warning: bedtime starts soon, warn once per window.
            window_id = bedtime_window_id(entry)
            mins = minutes_until_bedtime(entry, now)
            if (mins is not None and 0 < mins <= BEDTIME_WARN_MINUTES
                    and entry.get('warned_for') != window_id):
                _safe_notify(username, 'Bedtime in 15 minutes',
                             f'Bedtime starts at {entry.get("start")}. '
                             'Finish up — the device will lock.')
                entry['warned_for'] = window_id
                changed = True
            elif (entry.get('warned_for') is not None
                    and (mins is None or mins <= 0
                         or mins > BEDTIME_WARN_MINUTES)):
                # Window ended, schedule cleared, or next window far away:
                # drop the flag so the next window warns again.
                entry.pop('warned_for', None)
                changed = True
            if locked_by_bedtime:
                # Re-read ownership from disk immediately before unlocking.
                # `data` is a snapshot taken at the top of this pass, and
                # the systemd timer runs in a *separate process* from the
                # web UI -- a manual lock written since the snapshot must
                # win, or we would usermod -U an account the parent just
                # deliberately locked. Never unlock on a stale snapshot.
                current = load_bedtime().get(username, {})
                if not current.get('locked_by_bedtime', False):
                    entry['locked_by_bedtime'] = False
                    changed = True
                    continue
                success, _msg = run_lock_command(username, 'unlock')
                if success:
                    entry['locked_by_bedtime'] = False
                    changed = True
            if entry.get('manual_override'):
                # Bedtime window ended: clear the override so the next
                # bedtime enforces normally again.
                entry['manual_override'] = False
                changed = True

    if changed:
        save_bedtime(data)


def bedtime_loop():
    while True:
        try:
            enforce_bedtime()
        except Exception:
            traceback.print_exc()
        try:
            # Per-category time budgets: CPU-time accounting + cgroup
            # freezer enforcement, same 60s cadence as bedtime.
            budgets.tick_budgets(load_bedtime(), datetime.now(),
                                 notify=notify_user)
        except Exception:
            traceback.print_exc()
        time.sleep(60)


# ---------------------------------------------------------------------------
# Authentication & authorization (MVP, see AUTH_DESIGN.md + auth.py)
# ---------------------------------------------------------------------------

def _is_api_request():
    return request.path.startswith('/api/')


def _auth_cookie_response(response, sid):
    response.set_cookie(auth.SESSION_COOKIE, sid, httponly=True,
                        samesite='Lax',
                        secure=app.config['SECURE_COOKIES'])
    return response


def _clear_auth_cookie(response):
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


# Endpoints reachable while must_change is set (login/logout/change-pw).
_MUST_CHANGE_EXEMPT = {'login', 'change_password', 'logout', 'static'}


def require_admin(view):
    """Gate every page and API route (AUTH_DESIGN.md "Request handling").

    1. No valid session -> 302 /login (pages) or 401 JSON (/api/*).
    2. Session user not currently an admin (revoked wheel member, child
       account, deleted row) -> 403, session destroyed.
    3. Locked admin -> session destroyed, 403 with CLI recovery hint.
    4. must_change set -> everything but login/change-pw/logout redirects.
    5. POST/PUT/PATCH/DELETE without a valid CSRF token -> 403.
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        sid = request.cookies.get(auth.SESSION_COOKIE, '')
        sess = auth.get_session(sid)
        if sess is None:
            if _is_api_request():
                return jsonify({'success': False,
                                'error': 'Authentication required'}), 401
            return redirect(url_for('login', next=request.path))
        try:
            store = auth.load_store()
        except auth.AuthStoreError as e:
            print(f'AUTH FAIL-CLOSED: {e}', flush=True)
            if _is_api_request():
                return jsonify({'success': False,
                                'error': 'Authentication unavailable'}), 500
            return 'Authentication unavailable', 500
        username = sess.get('username', '')
        if auth.is_locked_admin(username, store):
            auth.destroy_session(sid)
            msg = 'Admin account is locked. ' + auth.RECOVERY_HINT
            if _is_api_request():
                return jsonify({'success': False, 'error': msg}), 403
            return msg, 403
        tier = auth.classify_admin(username, store)
        if tier is None:
            # Revoked (removed from wheel), removed app-only admin, or a
            # non-admin who somehow holds a session: refuse, kill session.
            auth.destroy_session(sid)
            if _is_api_request():
                return jsonify({'success': False,
                                'error': 'Forbidden'}), 403
            return redirect(url_for('login'))
        if (auth.is_must_change(username, store)
                and request.endpoint not in _MUST_CHANGE_EXEMPT):
            if _is_api_request():
                return jsonify({'success': False,
                                'error': 'Password change required',
                                'must_change': True}), 403
            return redirect(url_for('change_password'))
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if not auth.validate_csrf(sess.get('csrf'), request):
                if _is_api_request():
                    return jsonify(
                        {'success': False,
                         'error': 'Missing or invalid CSRF token'}), 403
                return 'Missing or invalid CSRF token', 403
        g.admin = username
        g.tier = tier
        g.csrf_token = sess.get('csrf', '')
        return view(*args, **kwargs)
    return wrapper


def _wheel_target_refused(username):
    """Refuse lock/remove-style actions targeting a wheel admin.

    Returns a 403 JSON response when `username` is a wheel member,
    else None. Enforced in the handler, never just hidden in the UI.
    """
    if auth.is_wheel_member(username):
        return jsonify({
            'success': False,
            'error': (f'Refused: {username} is a wheel admin and can '
                      'never be locked, terminated, or removed.')}), 403
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
@require_admin
def index():
    """Render the main page with user list."""
    users = get_human_users()
    bedtime = load_bedtime()
    user_data = []
    for u in users:
        locked, detail = get_lock_status(u['username'])
        entry = bedtime.get(u['username'], {})
        user_data.append({
            'username': u['username'],
            'uid': u['uid'],
            'home': u['home'],
            'shell': u['shell'],
            'locked': locked,
            'status_detail': detail,
            'bedtime': entry,
            'in_bedtime': is_in_bedtime(entry),
        })
    return render_template('index.html', users=user_data, day_labels=DAY_LABELS)


@app.route('/api/users')
@require_admin
def api_users():
    """JSON endpoint returning current user list with lock status."""
    users = get_human_users()
    bedtime = load_bedtime()
    user_data = []
    for u in users:
        locked, detail = get_lock_status(u['username'])
        entry = bedtime.get(u['username'], {})
        user_data.append({
            'username': u['username'],
            'uid': u['uid'],
            'locked': locked,
            'status_detail': detail,
            'bedtime': entry,
            'in_bedtime': is_in_bedtime(entry),
        })
    return jsonify(user_data)


@app.route('/api/lock', methods=['POST'])
@require_admin
def api_lock():
    """Lock a user account and screen without terminating the session.

    Runs:
      sudo usermod -L <user>      — lock the account
      loginctl lock-session       — lock the screen (session stays alive)
    """
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

    # Wheel admins can never be locked — refused here, not just in the UI.
    refused = _wheel_target_refused(username)
    if refused is not None:
        return refused

    # Lock the account
    try:
        result = subprocess.run(
            ['sudo', 'usermod', '-L', username],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
                msg = 'usermod -L: ' + (result.stderr.strip() or 'failed')
                return jsonify({'success': False, 'message': msg}), 400
    except Exception as e:
        return jsonify({'success': False, 'message':  'usermod -L error: ' + str(e)}), 400

    # A manual lock takes ownership away from bedtime so the bedtime
    # timer cannot later auto-unlock it (see note_manual_lock).
    note_manual_lock(username)

    # Lock the screen without terminating the session
    screen_msg = lock_user_sessions(username)

    return jsonify({'success': True, 'message': f'Account locked and screen locked ({screen_msg})'})


@app.route('/api/lock-screen', methods=['POST'])
@require_admin
def api_lock_screen():
    """Lock the user's screen and account without terminating the session.

    Runs:
      sudo usermod -L <user>      — lock the account
      loginctl lock-session       — lock the screen (session stays alive)
    """
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

    # Wheel admins can never be locked — refused here, not just in the UI.
    refused = _wheel_target_refused(username)
    if refused is not None:
        return refused

    # Lock the account (without terminating the session)
    try:
        result = subprocess.run(
            ['sudo', 'usermod', '-L', username],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
                msg = 'usermod -L: ' + (result.stderr.strip() or 'failed')
                return jsonify({'success': False, 'message': msg}), 400
    except Exception as e:
        return jsonify({'success': False, 'message':  'usermod -L error: ' + str(e)}), 400

    # A manual lock takes ownership away from bedtime so the bedtime
    # timer cannot later auto-unlock it (see note_manual_lock).
    note_manual_lock(username)

    # Lock the screen without terminating the session
    screen_msg = lock_user_sessions(username)

    return jsonify({'success': True, 'message': f'Account locked and screen locked ({screen_msg})'})


@app.route('/api/unlock', methods=['POST'])
@require_admin
def api_unlock():
    """Unlock a user account."""
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

    success, message = run_lock_command(username, 'unlock')
    if success:
        # Manual unlock takes precedence over bedtime (see note_manual_unlock).
        note_manual_unlock(username)
    return jsonify({'success': success, 'message': message})


@app.route('/api/bedtime', methods=['GET'])
@require_admin
def api_bedtime_get():
    """Return all bedtime settings."""
    return jsonify(load_bedtime())


@app.route('/api/bedtime', methods=['POST'])
@require_admin
def api_bedtime_set():
    """Set bedtime for a user.

    Form fields:
      username, start, end, all_days, day_0 through day_6, save/clear
    """
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

    start = request.form.get('start', '').strip()
    end = request.form.get('end', '').strip()
    all_days = 'all_days' in request.form

    # --- CLEAR HANDLING (absolute priority) ---
    # Checkbox or explicit clear request → always clear the bedtime
    if request.form.get('clear') or not start and not end:
        data = load_bedtime()
        existing = data.get(username, {})
        if username in data:
            del data[username]
            save_bedtime(data)
        if existing.get('locked_by_bedtime'):
            success, message = run_lock_command(username, 'unlock')
            if not success:
                return jsonify({'success': False, 'message': message, 'locked': True})
            return jsonify({'success': True, 'message': 'Unlocked', 'locked': False})
        return jsonify({'success': True, 'message': 'Bedtime cleared', 'locked': False})

    if _parse_hhmm(start) is None or _parse_hhmm(end) is None:
        return jsonify({'success': False, 'error': 'Invalid time format (use HH:MM)'}), 400

    # Determine days: all_days checkbox takes precedence, otherwise individual days
    if all_days:
        days = [0, 1, 2, 3, 4, 5, 6]
    else:
        days = [i for i in range(7) if f'day_{i}' in request.form]
        if not days:
            return jsonify({'success': False, 'error': 'Select at least one day or check "Apply to all days"'}), 400

    try:
        days = sorted(days)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid days'}), 400

    data = load_bedtime()
    existing = data.get(username, {})
    data[username] = {
        'start': start,
        'end': end,
        'days': days,
        # Preserve locked_by_bedtime if it was already set, otherwise default to False
        'locked_by_bedtime': bool(existing.get('locked_by_bedtime', False)),
        # Saving a schedule is the latest parent action: clear any override
        # so the new schedule enforces immediately.
        'manual_override': False,
    }
    save_bedtime(data)

    # Immediately enforce the new bedtime for this user
    entry = data[username]
    if is_in_bedtime(entry):
        # Only lock if not already manually unlocked
        if not entry.get('locked_by_bedtime'):
            if budgets.is_admin_protected(username):
                # Saving a schedule must not auto-lock an admin out.
                print(f'skipped bedtime auto-lock for {username}: '
                      f'{budgets.ADMIN_GROUP} member (admin self-protection)',
                      flush=True)
                return jsonify({'success': True,
                                'message': 'Bedtime saved (auto-lock skipped: admin user)',
                                'locked': False})
            success, message = run_lock_command(username, 'lock')
            if success:
                entry['locked_by_bedtime'] = True
                save_bedtime(data)
                _safe_notify(username, 'Locked for bedtime',
                             'Good night! The device unlocks at '
                             f'{entry.get("end")} '
                             'unless a parent changes it.')
                return jsonify({'success': True, 'message': 'Locked', 'locked': True})
        return jsonify({'success': True, 'message': 'Already locked by bedtime', 'locked': True})
    else:
        # Outside bedtime - if was previously bedtime-locked, unlock now
        if existing.get('locked_by_bedtime'):
            success, message = run_lock_command(username, 'unlock')
            if success:
                entry['locked_by_bedtime'] = False
                save_bedtime(data)
                return jsonify({'success': True, 'message': 'Unlocked', 'locked': False})
        return jsonify({'success': True, 'message': 'Bedtime saved', 'locked': False})


@app.route('/api/logout', methods=['POST'])
@require_admin
def api_logout():
    """Emergency logout — terminates the user's session.

    Runs:
      sudo usermod -L <user>      — lock the account
      sudo loginctl terminate-user <user>  — terminates all user sessions
    """
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

    # Terminating a wheel admin's sessions is a lock-equivalent action:
    # refused here, not just in the UI.
    refused = _wheel_target_refused(username)
    if refused is not None:
        return refused

    # Lock the account
    try:
        result = subprocess.run(
            ['sudo', 'usermod', '-L', username],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return jsonify({'success': False, 'message': 'usermod -L: ' + (result.stderr.strip() or 'failed')}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': 'usermod -L error: ' + str(e)}), 400

    # A manual lock takes ownership away from bedtime so the bedtime
    # timer cannot later auto-unlock it (see note_manual_lock).
    note_manual_lock(username)

    # Terminate the user's sessions
    try:
        result = subprocess.run(
            ['sudo', 'loginctl', 'terminate-user', username],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return jsonify({'success': False, 'message': 'loginctl terminate-user: ' + (result.stderr.strip() or 'failed')}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': 'loginctl terminate-user error: ' + str(e)}), 400

    return jsonify({'success': True, 'message': 'Session terminated. User logged out.'})


@app.route('/api/process-prefs', methods=['POST'])
@require_admin
def api_process_prefs():
    """Hide a comm or re-show a default-hidden one.

    Form fields: username, comm, action (hide|show).
    """
    username = request.form.get('username', '').strip()
    comm = request.form.get('comm', '').strip()
    action = request.form.get('action', '').strip()
    if not username or not comm:
        return jsonify({'success': False,
                        'error': 'username and comm required'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    ok, msg = budgets.set_pref(username, comm, action)
    if not ok:
        return jsonify({'success': False, 'error': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/budgets', methods=['POST'])
@require_admin
def api_budgets_set():
    """Set a per-category daily budget (minutes). 0/empty clears the limit.

    Form fields: username, category, minutes.
    """
    username = request.form.get('username', '').strip()
    category = request.form.get('category', '').strip()
    minutes = request.form.get('minutes', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    if not minutes:
        minutes = 0
    ok, msg = budgets.set_limit(username, category, minutes)
    if not ok:
        return jsonify({'success': False, 'error': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/budgets/resume', methods=['POST'])
@require_admin
def api_budgets_resume():
    """Manually resume a budget-paused category (unfreeze until reset)."""
    username = request.form.get('username', '').strip()
    category = request.form.get('category', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    ok, msg = budgets.resume_category(username, category)
    if not ok:
        return jsonify({'success': False, 'error': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/budgets/<username>')
@require_admin
def api_budgets_get(username):
    """JSON budget status (limits, usage, paused flags) for a user."""
    if not valid_username(username):
        return jsonify({'error': 'Invalid username'}), 400
    return jsonify(budgets.budget_status(username))


@app.route('/user/<username>')
@require_admin
def user_detail(username):
    """Render the user details page for a specific user."""
    data = get_user_live(username)
    if data is None:
        # Username not found
        return "User not found", 404

    return render_template('user.html', username=username,
                           user=data['user'],
                           processes=data['processes'], visible=data['visible'],
                           hidden=data['hidden'], groups=data['groups'],
                           budget_state=data['budget_state'],
                           category_labels=budgets.CATEGORY_LABELS,
                           categories=budgets.CATEGORIES,
                           day_labels=DAY_LABELS)


def get_user_live(username):
    """Gather live data for the details page and the live-refresh endpoint.

    Returns None when the username is not a human user on this system.
    """
    users = get_human_users()
    bedtime = load_bedtime()
    user_data = []
    for u in users:
        if u['username'] == username:
            locked, detail = get_lock_status(u['username'])
            entry = bedtime.get(u['username'], {})
            user_data.append({
                'username': u['username'],
                'uid': u['uid'],
                'home': u['home'],
                'shell': u['shell'],
                'locked': locked,
                'status_detail': detail,
                'bedtime': entry,
                'in_bedtime': is_in_bedtime(entry),
            })
            break
    else:
        return None

    # Get processes owned by this user (read-only ps + /proc cmdline),
    # filtered through the curated denylist + parent prefs, grouped by
    # app category for the per-category budget UI.
    processes = []
    try:
        processes = budgets.list_user_processes(username)
        for p in processes:
            p['elapsed'] = format_etime(p['etime'])
    except Exception as e:
        processes = [{'error': str(e)}]

    if processes and 'error' not in processes[0]:
        prefs = budgets.load_prefs()
        visible, hidden = budgets.apply_filter(processes, username, prefs)
        index = budgets.build_desktop_index(username)
        groups = budgets.group_by_category(visible, index)
        # Annotate hidden rows with their category too (shown on toggle).
        for p in hidden:
            p['category'] = budgets.categorize(p, index)
    else:
        visible, hidden, groups = [], processes, {c: [] for c in budgets.CATEGORIES}
    budget_state = budgets.budget_status(username)

    return {
        'user': user_data[0],
        'processes': processes,
        'visible': visible,
        'hidden': hidden,
        'groups': groups,
        'budget_state': budget_state,
    }


@app.route('/api/user/<username>/live')
@require_admin
def api_user_live(username):
    """Live status/process/budget data for targeted DOM refresh (no reload)."""
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    data = get_user_live(username)
    if data is None:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    user = data['user']
    error = None
    if data['processes'] and 'error' in data['processes'][0]:
        error = data['processes'][0].get('error')
    slim_groups = {
        cat: [{'pid': p['pid'], 'comm': p['comm'],
               'elapsed': p.get('elapsed') or p.get('etime', '')}
              for p in procs]
        for cat, procs in data['groups'].items()
    }
    slim_hidden = [
        {'pid': p.get('pid'), 'comm': p.get('comm'),
         'category': p.get('category')}
        for p in data['hidden'] if 'error' not in p
    ]
    return jsonify({
        'success': True,
        'locked': user['locked'],
        'status_detail': user['status_detail'],
        'in_bedtime': user['in_bedtime'],
        'manual_override': bool(user['bedtime'].get('manual_override', False)),
        'groups': slim_groups,
        'hidden': slim_hidden,
        'error': error,
        'budget': data['budget_state'],
    })


# ---------------------------------------------------------------------------
# Login, logout, password change, admin management
# ---------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 12


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Admin login (app passwords only — never system passwords)."""
    if request.method == 'GET':
        sid = request.cookies.get(auth.SESSION_COOKIE, '')
        if auth.get_session(sid) is not None:
            target = request.args.get('next', '/') or '/'
            if not target.startswith('/') or target.startswith('//'):
                target = '/'
            return redirect(target)
        return render_template('login.html', next=request.args.get('next', '/'))

    ip = request.remote_addr or 'unknown'
    wait = auth.login_backoff_remaining(ip)
    if wait > 0:
        resp = make_response(
            render_template('login.html', next=request.form.get('next', '/'),
                            error='Too many failed attempts. Try again soon.'),
            429)
        resp.headers['Retry-After'] = str(int(wait) + 1)
        return resp

    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    next_url = request.form.get('next', '/') or '/'
    if not next_url.startswith('/') or next_url.startswith('//'):
        next_url = '/'

    def _fail():
        auth.record_login_failure(ip)
        return render_template('login.html', next=next_url,
                               error='Invalid username or password.'), 401

    try:
        store = auth.load_store()
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return 'Authentication unavailable', 500

    if not auth.valid_admin_username(username):
        return _fail()
    row = store.get('users', {}).get(username)
    if not isinstance(row, dict):
        return _fail()
    if auth.classify_admin(username, store) is None:
        # Unknown tier, revoked, or a system non-admin (child): one
        # generic answer, no oracle for account enumeration.
        return _fail()
    if auth.is_locked_admin(username, store):
        auth.record_login_failure(ip)
        return render_template(
            'login.html', next=next_url,
            error='This admin account is locked. ' + auth.RECOVERY_HINT), 403
    if not auth.verify_password(row.get('hash'), password):
        return _fail()

    auth.record_login_success(ip)
    sid, _csrf = auth.create_session(username)
    if auth.is_must_change(username, store):
        resp = make_response(redirect(url_for('change_password')))
    else:
        resp = make_response(redirect(next_url))
    return _auth_cookie_response(resp, sid)


@app.route('/logout', methods=['GET', 'POST'])
def logout():
    """Destroy the admin's web session (NOT the emergency user logout;
    that lives at /api/logout and stays behind @require_admin)."""
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE, ''))
    resp = make_response(redirect(url_for('login')))
    return _clear_auth_cookie(resp)


@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    """Forced password change (first login) + voluntary rotation."""
    sid = request.cookies.get(auth.SESSION_COOKIE, '')
    sess = auth.get_session(sid)
    if sess is None:
        return redirect(url_for('login', next='/change-password'))
    try:
        store = auth.load_store()
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return 'Authentication unavailable', 500
    username = sess.get('username', '')
    if (auth.classify_admin(username, store) is None
            or auth.is_locked_admin(username, store)):
        auth.destroy_session(sid)
        resp = make_response(redirect(url_for('login')))
        return _clear_auth_cookie(resp)
    forced = auth.is_must_change(username, store)

    if request.method == 'GET':
        return render_template('change_password.html', username=username,
                               forced=forced, csrf_token=sess.get('csrf', ''))

    if not auth.validate_csrf(sess.get('csrf'), request):
        return render_template('change_password.html', username=username,
                               forced=forced,
                               csrf_token=sess.get('csrf', ''),
                               error='Missing or invalid CSRF token.'), 403
    current = request.form.get('current', '')
    new = request.form.get('new', '')
    confirm = request.form.get('confirm', '')
    row = store['users'][username]
    if not auth.verify_password(row.get('hash'), current):
        return render_template('change_password.html', username=username,
                               forced=forced,
                               csrf_token=sess.get('csrf', ''),
                               error='Current password is incorrect.'), 403
    if len(new) < MIN_PASSWORD_LENGTH:
        return render_template(
            'change_password.html', username=username, forced=forced,
            csrf_token=sess.get('csrf', ''),
            error=f'New password must be at least {MIN_PASSWORD_LENGTH} '
                  'characters.'), 400
    if new != confirm:
        return render_template('change_password.html', username=username,
                               forced=forced,
                               csrf_token=sess.get('csrf', ''),
                               error='New passwords do not match.'), 400
    row['hash'] = auth.hash_password(new)
    row['must_change'] = False
    try:
        auth.save_store(store)
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return 'Authentication unavailable', 500
    return redirect(url_for('index'))


@app.route('/api/admins', methods=['GET'])
@require_admin
def api_admins_list():
    """List admin accounts with live tier resolution."""
    try:
        store = auth.load_store()
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return jsonify({'success': False,
                        'error': 'Authentication unavailable'}), 500
    admins = []
    for username, row in store.get('users', {}).items():
        if not isinstance(row, dict):
            continue
        admins.append({
            'username': username,
            'tier': auth.classify_admin(username, store),
            'locked': bool(row.get('locked', False)),
            'must_change': bool(row.get('must_change', False)),
            'system_user': auth.is_system_user(username),
        })
    admins.sort(key=lambda a: a['username'])
    return jsonify({'success': True, 'admins': admins,
                    'self': g.admin, 'tier': g.tier})


@app.route('/api/admins', methods=['POST'])
@require_admin
def api_admins_add():
    """Create an admin account. Wheel admins only.

    Accepts a wheel member (gives them a login row) or a non-system
    username (app-only admin). Refuses non-wheel system accounts: that
    would hand a login to a regular local user (e.g. the child). The
    password is generated server-side and returned ONCE in the response.
    """
    if g.tier != 'wheel':
        return jsonify({'success': False,
                        'error': 'Only wheel admins can add admins.'}), 403
    username = request.form.get('username', '').strip()
    if not auth.valid_admin_username(username):
        return jsonify({'success': False,
                        'error': 'Invalid username.'}), 400
    try:
        store = auth.load_store()
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return jsonify({'success': False,
                        'error': 'Authentication unavailable'}), 500
    if username in store.get('users', {}):
        return jsonify({'success': False,
                        'error': 'Admin already exists.'}), 400
    if auth.is_system_user(username) and not auth.is_wheel_member(username):
        return jsonify({
            'success': False,
            'error': (f'Refused: {username} is a non-admin system account '
                      'and can never authenticate.')}), 403
    generated = secrets.token_urlsafe(24)
    store['users'][username] = {
        'hash': auth.hash_password(generated),
        'must_change': True,
        'locked': False,
    }
    try:
        auth.save_store(store)
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return jsonify({'success': False,
                        'error': 'Authentication unavailable'}), 500
    return jsonify({'success': True,
                    'message': f'Admin {username} created. This password is '
                               'shown ONCE and must be changed at first login.',
                    'password': generated})


@app.route('/api/admins/remove', methods=['POST'])
@require_admin
def api_admins_remove():
    """Remove an app-only admin. Wheel admins only; wheel targets never."""
    if g.tier != 'wheel':
        return jsonify({'success': False,
                        'error': 'Only wheel admins can remove admins.'}), 403
    username = request.form.get('username', '').strip()
    if not auth.valid_admin_username(username):
        return jsonify({'success': False,
                        'error': 'Invalid username.'}), 400
    refused = _wheel_target_refused(username)
    if refused is not None:
        return refused
    try:
        store = auth.load_store()
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return jsonify({'success': False,
                        'error': 'Authentication unavailable'}), 500
    # Re-check after loading the live store: a wheel member is never
    # removable even if their row looks app-only (belt and braces).
    refused = _wheel_target_refused(username)
    if refused is not None:
        return refused
    if username not in store.get('users', {}):
        return jsonify({'success': False,
                        'error': 'No such admin.'}), 404
    del store['users'][username]
    try:
        auth.save_store(store)
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return jsonify({'success': False,
                        'error': 'Authentication unavailable'}), 500
    return jsonify({'success': True,
                    'message': f'Admin {username} removed.'})


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Parental Controls Web UI')
    parser.add_argument('--allow-lan', action='store_true',
                        help='Bind 0.0.0.0 (default: 127.0.0.1 only; '
                             'localhost + SSH tunnel, no TLS in MVP)')
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()
    try:
        auth.ensure_hash_backend()
        auth.init_store()
    except auth.AuthStoreError as e:
        print(f'FATAL: {e}', flush=True)
        raise SystemExit(1)
    host = '0.0.0.0' if args.allow_lan else '127.0.0.1'
    if args.allow_lan:
        print('WARNING: --allow-lan: bound to all interfaces without TLS; '
              'session cookies travel in the clear.', flush=True)
    t = threading.Thread(target=bedtime_loop, daemon=True)
    t.start()
    app.run(host=host, port=args.port, debug=False)
