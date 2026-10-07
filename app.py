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
from datetime import datetime
from flask import Flask, render_template, request, jsonify

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
    """
    if action == 'lock':
        cmds = [
            ['sudo', 'usermod', '-L', username],
            ['sudo', 'loginctl', 'terminate-user', username],
        ]
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
# Bedtime enforcement (background thread)
# ---------------------------------------------------------------------------

def enforce_bedtime():
    """Lock users whose bedtime started; unlock only those bedtime locked."""
    data = load_bedtime()
    changed = False
    for username, entry in list(data.items()):
        in_bed = is_in_bedtime(entry)
        locked_by_bedtime = bool(entry.get('locked_by_bedtime', False))

        if in_bed:
            locked, _detail = get_lock_status(username)
            if locked is False:
                success, _msg = run_lock_command(username, 'lock')
                if success:
                    entry['locked_by_bedtime'] = True
                    changed = True
            elif locked is None and not locked_by_bedtime:
                # Couldn't determine status; try to lock anyway on next pass
                pass
        else:
            if locked_by_bedtime:
                success, _msg = run_lock_command(username, 'unlock')
                if success:
                    entry['locked_by_bedtime'] = False
                    changed = True

    if changed:
        save_bedtime(data)


def bedtime_loop():
    while True:
        try:
            enforce_bedtime()
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


@app.route('/bedtime')
def bedtime():
    """Render the dedicated bedtime settings page."""
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
    return render_template('bedtime.html', users=user_data, day_labels=DAY_LABELS)


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
    return jsonify({'success': success, 'message': message})


@app.route('/api/bedtime', methods=['GET'])
def api_bedtime_get():
    """Return all bedtime settings."""
    return jsonify(load_bedtime())


@app.route('/api/bedtime', methods=['POST'])
def api_bedtime_set():
    """Set bedtime for a user. Form fields: username, start, end, days[]."""
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not all(c.isalnum() or c in '_-.' for c in username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

    start = request.form.get('start', '').strip()
    end = request.form.get('end', '').strip()
    days_raw = request.form.getlist('days')

    # Allow clearing a bedtime by submitting empty start/end
    if not start and not end and not days_raw:
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

    try:
        days = sorted({int(d) for d in days_raw})
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid days'}), 400
    if not days or any(d < 0 or d > 6 for d in days):
        return jsonify({'success': False, 'error': 'Select at least one valid day'}), 400

    data = load_bedtime()
    existing = data.get(username, {})
    data[username] = {
        'start': start,
        'end': end,
        'days': days,
        'locked_by_bedtime': bool(existing.get('locked_by_bedtime', False)),
    }
    save_bedtime(data)

    # Immediately enforce the new bedtime for this user
    entry = data[username]
    if is_in_bedtime(entry):
        success, message = run_lock_command(username, 'lock')
        if success:
            entry['locked_by_bedtime'] = True
            save_bedtime(data)
            return jsonify({'success': True, 'message': 'Locked', 'locked': True})
        return jsonify({'success': False, 'message': message, 'locked': False})

    if entry.get('locked_by_bedtime'):
        success, message = run_lock_command(username, 'unlock')
        if success:
            entry['locked_by_bedtime'] = False
            save_bedtime(data)
            return jsonify({'success': True, 'message': 'Unlocked', 'locked': False})
        return jsonify({'success': False, 'message': message, 'locked': True})

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


@app.route('/user/<username>')
def user_detail(username):
    """Render the user details page for a specific user."""
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
        # Username not found
        return "User not found", 404
    
    # Get processes owned by this user
    processes = []
    try:
        result = subprocess.run(
            ['ps', '-u', username, '-o', 'pid,comm,etime'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                parts = line.split()
                if len(parts) >= 3:
                    pid = parts[0]
                    comm = parts[1]
                    # etimes shows seconds since epoch start, we need to calculate duration
                    # For simplicity, show the raw output
                    processes.append({'pid': pid, 'comm': comm})
    except Exception as e:
        processes = [{'error': str(e)}]
    
    return render_template('user.html', username=username, user=user_data[0] if user_data else None, processes=processes, day_labels=DAY_LABELS)


if __name__ == '__main__':
    t = threading.Thread(target=bedtime_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=5000, debug=False)
