#!/usr/bin/env python3
"""Parental Controls Web UI for Bazzite.

A lightweight Flask app that lists human users on the system and allows
locking/unlocking their accounts via sudo commands.
"""

import subprocess
import pwd
import html
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

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
    try:
        result = subprocess.run(
            ['passwd', '-S', username],
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


@app.route('/')
def index():
    """Render the main page with user list."""
    users = get_human_users()
    user_data = []
    for u in users:
        locked, detail = get_lock_status(u['username'])
        user_data.append({
            'username': u['username'],
            'uid': u['uid'],
            'home': u['home'],
            'shell': u['shell'],
            'locked': locked,
            'status_detail': detail,
        })
    return render_template('index.html', users=user_data)


@app.route('/api/users')
def api_users():
    """JSON endpoint returning current user list with lock status."""
    users = get_human_users()
    user_data = []
    for u in users:
        locked, detail = get_lock_status(u['username'])
        user_data.append({
            'username': u['username'],
            'uid': u['uid'],
            'locked': locked,
            'status_detail': detail,
        })
    return jsonify(user_data)


@app.route('/api/lock', methods=['POST'])
def api_lock():
    """Lock a user account."""
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    # Basic sanitization — only allow valid username characters
    if not all(c.isalnum() or c in '_-.' for c in username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

    success, message = run_lock_command(username, 'lock')
    return jsonify({'success': success, 'message': message})


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
