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
import threading
import time
import traceback
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify

import budgets

app = Flask(__name__)

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
    """Clear a bedtime manual override after a manual lock.

    A manual lock cancels the override; bedtime ownership
    (locked_by_bedtime) is left untouched.
    """
    try:
        data = load_bedtime()
        entry = data.get(username)
        if entry is not None and entry.get('manual_override'):
            entry['manual_override'] = False
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
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
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
def api_lock():
    """Lock a user account and screen without terminating the session.

    Runs:
      sudo usermod -L <user>      — lock the account
      loginctl lock-session       — lock the screen (session stays alive)
    """
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not all(c.isalnum() or c in '_-.' for c in username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

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

    # A manual lock cancels any bedtime manual override.
    clear_manual_override(username)

    # Lock the screen without terminating the session
    try:
        result = subprocess.run(
            ['sudo', 'loginctl', 'lock-session'],
            capture_output=True, text=True, timeout=10
        )
        screen_msg = result.stderr.strip() or 'Screen locked'
        if result.returncode != 0:
            screen_msg = f'loginctl lock-session: {result.stderr.strip() or "failed"}'
    except Exception as e:
        screen_msg = f'loginctl lock-session error: {e}'

    return jsonify({'success': True, 'message': f'Account locked and screen locked ({screen_msg})'})


@app.route('/api/lock-screen', methods=['POST'])
def api_lock_screen():
    """Lock the user's screen and account without terminating the session.

    Runs:
      sudo usermod -L <user>      — lock the account
      loginctl lock-session       — lock the screen (session stays alive)
    """
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not all(c.isalnum() or c in '_-.' for c in username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

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

    # A manual lock cancels any bedtime manual override.
    clear_manual_override(username)

    # Lock the screen without terminating the session
    try:
        result = subprocess.run(
            ['sudo', 'loginctl', 'lock-session'],
            capture_output=True, text=True, timeout=10
        )
        screen_msg = result.stderr.strip() or 'Screen locked'
        if result.returncode != 0:
            screen_msg = f'loginctl lock-session: {result.stderr.strip() or "failed"}'
    except Exception as e:
        screen_msg = f'loginctl lock-session error: {e}'

    return jsonify({'success': True, 'message': f'Account locked and screen locked ({screen_msg})'})


@app.route('/api/unlock', methods=['POST'])
def api_unlock():
    """Unlock a user account."""
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not all(c.isalnum() or c in '_-.' for c in username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

    success, message = run_lock_command(username, 'unlock')
    if success:
        # Manual unlock takes precedence over bedtime (see note_manual_unlock).
        note_manual_unlock(username)
    return jsonify({'success': success, 'message': message})


@app.route('/api/bedtime', methods=['GET'])
def api_bedtime_get():
    """Return all bedtime settings."""
    return jsonify(load_bedtime())


@app.route('/api/bedtime', methods=['POST'])
def api_bedtime_set():
    """Set bedtime for a user.

    Form fields:
      username, start, end, all_days, day_0 through day_6, save/clear
    """
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not all(c.isalnum() or c in '_-.' for c in username):
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
def api_logout():
    """Emergency logout — terminates the user's session.

    Runs:
      sudo usermod -L <user>      — lock the account
      sudo loginctl terminate-user <user>  — terminates all user sessions
    """
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not all(c.isalnum() or c in '_-.' for c in username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

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

    # A manual lock cancels any bedtime manual override.
    clear_manual_override(username)

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
    if not all(c.isalnum() or c in '_-.' for c in username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    ok, msg = budgets.set_pref(username, comm, action)
    if not ok:
        return jsonify({'success': False, 'error': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/budgets', methods=['POST'])
def api_budgets_set():
    """Set a per-category daily budget (minutes). 0/empty clears the limit.

    Form fields: username, category, minutes.
    """
    username = request.form.get('username', '').strip()
    category = request.form.get('category', '').strip()
    minutes = request.form.get('minutes', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not all(c.isalnum() or c in '_-.' for c in username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    if not minutes:
        minutes = 0
    ok, msg = budgets.set_limit(username, category, minutes)
    if not ok:
        return jsonify({'success': False, 'error': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/budgets/resume', methods=['POST'])
def api_budgets_resume():
    """Manually resume a budget-paused category (unfreeze until reset)."""
    username = request.form.get('username', '').strip()
    category = request.form.get('category', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not all(c.isalnum() or c in '_-.' for c in username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    ok, msg = budgets.resume_category(username, category)
    if not ok:
        return jsonify({'success': False, 'error': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/budgets/<username>')
def api_budgets_get(username):
    """JSON budget status (limits, usage, paused flags) for a user."""
    if not all(c.isalnum() or c in '_-.' for c in username):
        return jsonify({'error': 'Invalid username'}), 400
    return jsonify(budgets.budget_status(username))


@app.route('/user/<username>')
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
def api_user_live(username):
    """Live status/process/budget data for targeted DOM refresh (no reload)."""
    if not all(c.isalnum() or c in '_-.' for c in username):
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


if __name__ == '__main__':
    t = threading.Thread(target=bedtime_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=5000, debug=False)
