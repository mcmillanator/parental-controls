#!/usr/bin/env python3
"""Parental Controls remote agent (managed box B).

MULTIDEVICE_PLAN.md §4.1/§4.5 Phase 1: a minimal, headless WSGI app that
exposes the panel's privileged operations over an authenticated
machine-to-machine link. It is served on loopback (default 127.0.0.1:5001)
by `wsgi_agent.py`, usually reached from the hub over an SSH tunnel.

Hard properties (all pinned by tests/):

* **Stdlib + shared modules only.** No Flask, no sessions, no cookies. It
  imports ``auth`` (live wheel membership), ``budgets`` (freeze/thaw,
  process prefs), ``audit`` (the box's own hash chain), ``store`` and
  ``device_policy`` (the shared refusal policy). It never imports ``app``.
* **Every safety refusal is re-evaluated against THIS box.** ``device_policy``
  calls ``auth.is_wheel_member`` / ``budgets.is_admin_protected``, which read
  the local wheel group every call. A hub admin who is wheel on the hub
  cannot lock *this* box's wheel members; being unprivileged here is not a
  waiver of this box's protections (§4.4).
* **Bearer token auth.** ``Authorization: Bearer <token>`` compared with
  ``secrets.compare_digest`` against ``/etc/parental-controls/agent_token``
  (0600). No CSRF: bearer headers are not cookies, so cross-site request
  forgery does not apply. Missing/short/rotated token -> 401. The token is
  never logged and never appears in an audit detail.
* **Audit stays local and honest.** Actions append to this box's own
  ``audit.jsonl`` with the hub actor (``remote:<actor>@<panel>``) and a
  ``via: remote`` marker. The claimed actor is for the trail only, never for
  authorization.
"""

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler  # noqa: F401 (documented stdlib base)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audit  # noqa: E402
import auth  # noqa: E402
import budgets  # noqa: E402
import store  # noqa: E402
import device_policy  # noqa: E402

VERSION = '1.1-multidevice'
AGENT_NAME = 'parental-controls-agent'
API_PREFIX = '/api/v1'

TOKEN_FILE_DEFAULT = '/etc/parental-controls/agent_token'
TOKEN_FILE_ENV = 'PARENTAL_CONTROLS_AGENT_TOKEN_FILE'
TOKEN_ENV = 'PARENTAL_CONTROLS_AGENT_TOKEN'

BEDTIME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'bedtime.json')

BONUS_KEYS = ('bonus_until', 'bonus_minutes', 'bonus_granted_by',
              'bonus_granted_at')
BONUS_HISTORY_CAP = 20
BONUS_CUSTOM_MIN = 5

MIN_UID = 1000
MAX_UID = 60000

_ACTOR_RX = re.compile(r'[^A-Za-z0-9_.@:-]')


# ---------------------------------------------------------------------------
# Token store (0600). Fail closed: no token file -> authenticated routes 503.
# ---------------------------------------------------------------------------

def token_path():
    return os.environ.get(TOKEN_FILE_ENV) or TOKEN_FILE_DEFAULT


def load_token(path=None):
    """Return the configured agent token, or None when absent/empty.

    An unreadable or missing file is *not* an error here: the caller must
    refuse authenticated routes rather than invent a token.
    """
    env = os.environ.get(TOKEN_ENV)
    if env:
        return env
    path = path or token_path()
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            token = fh.read().strip()
    except OSError:
        return None
    return token or None


def token_ok(supplied, token=None):
    """Constant-time token check. False when either side is missing."""
    token = token if token is not None else load_token()
    if not token or not supplied:
        return False
    try:
        return secrets.compare_digest(str(supplied), str(token))
    except (TypeError, ValueError):
        return False


def bearer_token(environ):
    """Extract the bearer token from a WSGI environ ('' when absent)."""
    header = environ.get('HTTP_AUTHORIZATION', '') or ''
    if not header.startswith('Bearer '):
        return ''
    return header[len('Bearer '):].strip()


def redacted_headers(environ):
    """Request headers safe to log/inspect: never the bearer token."""
    out = {}
    for key, value in environ.items():
        if not key.startswith('HTTP_'):
            continue
        name = key[5:].replace('_', '-').title()
        if name.lower() == 'authorization':
            out[name] = '<redacted>'
        else:
            out[name] = value
    return out


def _sanitize_actor(value, fallback='unknown'):
    value = _ACTOR_RX.sub('', str(value or ''))[:64]
    return value or fallback


def remote_actor(environ):
    """Audit actor for a remote action: ``remote:<hub>@<panel>``.

    Used for the trail only. Never for authorization: authorization is the
    hub's own human session plus THIS box's target checks.
    """
    actor = _sanitize_actor(environ.get('HTTP_X_PANEL_ACTOR'), 'unknown')
    panel = _sanitize_actor(environ.get('HTTP_X_PANEL_ID'), '')
    return f'remote:{actor}@{panel}' if panel else f'remote:{actor}'


# ---------------------------------------------------------------------------
# Local system state (this box only)
# ---------------------------------------------------------------------------

def get_human_users():
    import pwd
    users = []
    for entry in pwd.getpwall():
        if entry.pw_uid < MIN_UID or entry.pw_uid > MAX_UID:
            continue
        if entry.pw_shell in ('/usr/sbin/nologin', '/sbin/nologin',
                              '/bin/false'):
            continue
        users.append({'username': entry.pw_name, 'uid': entry.pw_uid,
                      'home': entry.pw_dir, 'shell': entry.pw_shell})
    users.sort(key=lambda u: u['uid'])
    return users


def get_lock_status(username):
    """(is_locked, detail) via local ``sudo passwd -S`` (mirrors app)."""
    try:
        result = subprocess.run(
            ['sudo', 'passwd', '-S', username],
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None, result.stderr.strip() or 'Unknown status'
        parts = result.stdout.strip().split()
        if len(parts) < 2:
            return None, 'Unexpected passwd output'
        status = parts[1]
        if status == 'L':
            return True, 'Locked'
        if status == 'P':
            return False, 'Unlocked'
        if status == 'NP':
            return True, 'No password'
        return None, f'Unknown status: {status}'
    except subprocess.TimeoutExpired:
        return None, 'Command timed out'
    except FileNotFoundError:
        return None, 'passwd command not found'
    except Exception as e:  # never break a read on an odd system
        return None, str(e)


def load_bedtime():
    return store.load_table(store.db_path_for(BEDTIME_FILE), 'bedtime')


def save_bedtime(data):
    store.save_table(store.db_path_for(BEDTIME_FILE), 'bedtime', data,
                     BEDTIME_FILE)


def _safe_audit(actor, action, target='', detail=None):
    try:
        audit.safe_log(actor, action, target, detail)
    except Exception as e:
        print(f'audit (suppressed): {e}')


def _safe_notify(username, title, body):
    """Best-effort desktop notification (same argv the panel uses)."""
    try:
        if not username or not device_policy.valid_username(username):
            return False
        subprocess.run(
            ['sudo', 'systemd-run', f'--machine={username}@.host', '--user',
             'notify-send', '-u', 'critical', str(title), str(body)],
            capture_output=True, text=True, timeout=15)
        return True
    except Exception:
        return False


def _parse_hhmm(value):
    try:
        hours, minutes = str(value).strip().split(':')
        h, m = int(hours), int(minutes)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError, TypeError):
        pass
    return None


def format_12h(value):
    if isinstance(value, datetime):
        h, m = value.hour, value.minute
    else:
        if not value:
            return ''
        try:
            h, m = [int(x) for x in str(value).strip().split(':')[:2]]
        except (ValueError, TypeError):
            return value
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return value
    suffix = 'AM' if h < 12 else 'PM'
    return f'{h % 12 or 12}:{m:02d} {suffix}'


def format_bonus_minutes(total):
    total = int(total)
    hours, minutes = divmod(total, 60)
    parts = []
    if hours:
        parts.append(f'{hours} hour' + ('' if hours == 1 else 's'))
    if minutes or not parts:
        parts.append(f'{minutes} minute' + ('' if minutes == 1 else 's'))
    return ' '.join(parts)


def is_in_bedtime(entry, now=None):
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
    today = now.weekday()
    yesterday = (today - 1) % 7
    if start < end:
        return today in days and start <= cur < end
    if today in days and cur >= start:
        return True
    if yesterday in days and cur < end:
        return True
    return False


def _parse_bonus_until(entry):
    if not isinstance(entry, dict):
        return None
    raw = entry.get('bonus_until')
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _append_bonus_history(entry, kind, minutes, until, by):
    if not isinstance(entry.get('bonus_history'), list):
        entry['bonus_history'] = []
    entry['bonus_history'].append({
        'at': datetime.now().isoformat(timespec='seconds'),
        'by': by or 'system', 'minutes': minutes, 'until': until,
        'kind': kind,
    })
    del entry['bonus_history'][:-BONUS_HISTORY_CAP]


def _drop_bonus_keys(entry):
    for key in BONUS_KEYS:
        entry.pop(key, None)


def bonus_state(entry, now=None):
    now = now or datetime.now()
    until = _parse_bonus_until(entry)
    if until is None:
        return {'active': False, 'until': None, 'remaining_minutes': None,
                'granted_by': None, 'last_minutes': None}
    return {
        'active': until > now,
        'until': until.isoformat(),
        'remaining_minutes': max(0, int((until - now).total_seconds() // 60)),
        'granted_by': entry.get('bonus_granted_by'),
        'last_minutes': entry.get('bonus_minutes'),
    }


def _session_supports_lock(session_id):
    try:
        result = subprocess.run(
            ['sudo', 'loginctl', 'show-session', session_id,
             '-p', 'Class', '-p', 'Type'],
            capture_output=True, text=True, timeout=15)
    except Exception:
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


def lock_user_sessions(username):
    """Screen-lock the target's graphical sessions (never terminates)."""
    try:
        result = subprocess.run(
            ['sudo', 'loginctl', 'show-user', username, '-p', 'Sessions',
             '--value'], capture_output=True, text=True, timeout=15)
        session_ids = result.stdout.split() if result.returncode == 0 else []
        if not session_ids:
            return 'no active session'
        lockable = [sid for sid in session_ids if _session_supports_lock(sid)]
        if not lockable:
            return 'no lockable (graphical) session'
        result = subprocess.run(
            ['sudo', 'loginctl', 'lock-session'] + lockable,
            capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return f'loginctl lock-session: {result.stderr.strip() or "failed"}'
        return f'{len(lockable)} session(s) locked'
    except subprocess.TimeoutExpired:
        return 'screen lock timed out'
    except Exception as e:
        return f'screen lock error: {e}'


def run_lock_command(username, action, actor=None):
    """Local lock/unlock (session-preserving), mirroring app.run_lock_command."""
    act = actor or 'system'
    audit_action = ('account.lock' if action == 'lock'
                    else 'account.unlock' if action == 'unlock'
                    else 'account.unknown')

    def _done(success, message):
        _safe_audit(act, audit_action, username,
                    {'success': success, 'message': message,
                     'via': 'remote'})
        return success, message

    if action == 'lock':
        try:
            result = subprocess.run(['sudo', 'usermod', '-L', username],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                return _done(False,
                             f'sudo usermod -L {username}: '
                             f'{result.stderr.strip() or "failed"}')
        except subprocess.TimeoutExpired:
            return _done(False, f'sudo usermod -L {username}: timed out')
        except Exception as e:
            return _done(False, f'sudo usermod -L {username}: {e}')
        try:
            screen_msg = lock_user_sessions(username)
        except Exception as e:
            screen_msg = f'screen lock error: {e}'
        return _done(True, f'OK ({screen_msg})')
    elif action == 'unlock':
        try:
            result = subprocess.run(['sudo', 'usermod', '-U', username],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                return _done(False,
                             f'sudo usermod -U {username}: '
                             f'{result.stderr.strip() or "failed"}')
        except subprocess.TimeoutExpired:
            return _done(False, f'sudo usermod -U {username}: timed out')
        except Exception as e:
            return _done(False, f'sudo usermod -U {username}: {e}')
        return _done(True, 'OK')
    return _done(False, f'Unknown action: {action}')


def note_manual_lock(username, actor=None):
    """Manual lock bookkeeping (clears bedtime ownership + any bonus)."""
    try:
        data = load_bedtime()
        entry = data.get(username)
        if entry is not None:
            changed = False
            if entry.get('locked_by_bedtime'):
                entry['locked_by_bedtime'] = False
                changed = True
            if entry.get('manual_override'):
                entry['manual_override'] = False
                changed = True
            if _parse_bonus_until(entry) is not None:
                _append_bonus_history(entry, 'lock_cancelled',
                                      entry.get('bonus_minutes'),
                                      entry.get('bonus_until'), 'manual')
                _drop_bonus_keys(entry)
                changed = True
            if changed:
                save_bedtime(data)
    except OSError:
        pass
    _safe_audit(actor or 'system', 'bedtime.manual_lock', username,
                {'success': True, 'via': 'remote'})


def note_manual_unlock(username, actor=None):
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
    _safe_audit(actor or 'system', 'bedtime.manual_unlock', username,
                {'success': True, 'via': 'remote'})


def note_bonus_grant(username, minutes, grantor=None, now=None):
    now = now or datetime.now()
    data = load_bedtime()
    entry = data.get(username)
    if entry is None:
        _safe_audit(grantor or 'system', 'bonus.grant', username,
                    {'success': False, 'minutes': minutes,
                     'error': 'no bedtime entry', 'via': 'remote'})
        return None
    existing = _parse_bonus_until(entry)
    base = max(now, existing) if existing is not None else now
    until = base + timedelta(minutes=minutes)
    kind = 'extend' if existing is not None and existing > now else 'grant'
    entry['bonus_until'] = until.isoformat(timespec='seconds')
    entry['bonus_minutes'] = minutes
    entry['bonus_granted_by'] = grantor or 'admin'
    entry['bonus_granted_at'] = now.isoformat(timespec='seconds')
    entry['manual_override'] = False
    entry['locked_by_bedtime'] = False
    _append_bonus_history(entry, kind, minutes, entry['bonus_until'],
                          entry['bonus_granted_by'])
    save_bedtime(data)
    _safe_audit(grantor or 'system', 'bonus.grant', username,
                {'success': True, 'minutes': minutes,
                 'until': entry['bonus_until'], 'kind': kind, 'via': 'remote'})
    return until


def note_bonus_revoke(username, by=None):
    data = load_bedtime()
    entry = data.get(username)
    if entry is None or _parse_bonus_until(entry) is None:
        _safe_audit(by or 'system', 'bonus.revoke', username,
                    {'success': False, 'error': 'no active bonus time',
                     'via': 'remote'})
        return False
    _append_bonus_history(entry, 'revoke', entry.get('bonus_minutes'),
                          entry.get('bonus_until'), by or 'admin')
    _drop_bonus_keys(entry)
    save_bedtime(data)
    _safe_audit(by or 'system', 'bonus.revoke', username,
                {'success': True, 'via': 'remote'})
    return True


# ---------------------------------------------------------------------------
# Shared target checks (device_policy) — evaluated on THIS box, every call
# ---------------------------------------------------------------------------

def _user_error(username):
    """400 payload for a missing/invalid username, else None."""
    if not username:
        return {'success': False, 'error': 'No username provided'}
    if not device_policy.valid_username(username):
        return {'success': False, 'error': 'Invalid username'}
    return None


# ---------------------------------------------------------------------------
# Read verbs
# ---------------------------------------------------------------------------

def read_health():
    try:
        box_id = socket.gethostname()
    except Exception:
        box_id = 'unknown'
    return {
        'success': True,
        'agent': AGENT_NAME,
        'version': VERSION,
        'box_id': box_id,
        'token_configured': load_token() is not None,
    }


def read_users():
    users = get_human_users()
    bedtime = load_bedtime()
    data = []
    for u in users:
        locked, detail = get_lock_status(u['username'])
        entry = bedtime.get(u['username'], {})
        data.append({
            'username': u['username'], 'uid': u['uid'],
            'locked': locked, 'status_detail': detail,
            'bedtime': entry,
            'in_bedtime': is_in_bedtime(entry),
            'bonus': bonus_state(entry),
        })
    return data


def read_user_live(username):
    """Mirror of the panel's /api/user/<u>/live payload."""
    users = get_human_users()
    bedtime = load_bedtime()
    user_row = None
    for u in users:
        if u['username'] == username:
            locked, detail = get_lock_status(u['username'])
            entry = bedtime.get(u['username'], {})
            user_row = {
                'username': u['username'], 'uid': u['uid'],
                'home': u['home'], 'shell': u['shell'],
                'locked': locked, 'status_detail': detail,
                'bedtime': entry, 'in_bedtime': is_in_bedtime(entry),
                'bonus': bonus_state(entry),
            }
            break
    if user_row is None:
        return None
    processes = []
    try:
        processes = budgets.list_user_processes(username)
    except Exception as e:
        processes = [{'error': str(e)}]
    app_names = []
    if processes and 'error' not in processes[0]:
        import pwd
        prefs = budgets.load_prefs()
        try:
            home = pwd.getpwnam(username).pw_dir
        except KeyError:
            home = None
        index = budgets.build_desktop_index(username)
        names = budgets.build_desktop_names(username)
        app_names = sorted(names)
        budgets.mark_user_apps(processes, names, home)
        visible, hidden = budgets.apply_filter(processes, username, prefs)
        budgets.annotate_display_names(processes, names)
        groups = budgets.group_by_category(visible, index)
        groups = {cat: budgets.group_by_comm(procs)
                  for cat, procs in groups.items()}
        for p in hidden:
            p['category'] = budgets.categorize(p, index)
        hidden = budgets.group_by_comm(hidden)
    else:
        visible, hidden = [], []
        groups = {c: [] for c in budgets.CATEGORIES}
    overrides_data = budgets.load_overrides()
    user_overrides = overrides_data.get(username)
    budget_state = budgets.budget_status(username, overrides=user_overrides)
    # Processes show billable USAGE since the last reset (the same counter
    # the budget rows show), never the process's ps ETIME wall-clock age.
    for cat, cat_groups in groups.items():
        for g in cat_groups:
            mins = budgets.group_usage_minutes(cat, g['comm'], budget_state)
            g['used_minutes'] = mins
            g['used'] = budgets.format_hm(mins) if mins is not None else ''
    for g in hidden:
        g['used_minutes'] = None
        g['used'] = ''
    groups_slim = {
        cat: [{'comm': g['comm'], 'display_name': g.get('display_name')
               or g['comm'], 'count': g['count'],
               'used': g.get('used') or '',
               'members': [{'pid': m['pid'], 'comm': m['comm'],
                            'display_name': m.get('display_name')
                            or m.get('comm', '')}
                           for m in g['members']]}
              for g in procs]
        for cat, procs in groups.items()
    }
    hidden_slim = [
        {'comm': g['comm'], 'display_name': g.get('display_name') or g['comm'],
         'count': g['count'], 'categories': g.get('categories', []),
         'uniform_category': g.get('uniform_category', True),
         'members': [{'pid': m.get('pid'), 'comm': m.get('comm'),
                      'display_name': m.get('display_name') or m.get('comm', ''),
                      'category': m.get('category')}
                     for m in g['members']]}
        for g in hidden if 'error' not in g
    ]
    error = None
    if processes and 'error' in processes[0]:
        error = processes[0].get('error')
    return {
        'success': True,
        'locked': user_row['locked'],
        'status_detail': user_row['status_detail'],
        'in_bedtime': user_row['in_bedtime'],
        'manual_override': bool(user_row['bedtime'].get('manual_override')),
        'bonus': user_row['bonus'],
        'uid': user_row['uid'],
        'home': user_row['home'],
        'shell': user_row['shell'],
        'bedtime': user_row['bedtime'],
        'processes': processes,
        'visible': visible,
        'groups': groups_slim,
        'hidden': hidden_slim,
        'error': error,
        'app_names': app_names,
        'budget': budget_state,
        'overrides': budgets.overrides_view(username, overrides_data),
    }


def read_audit(limit=100):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))
    entries = audit.read_entries(limit=limit)
    ok, bad_seq = audit.verify_chain()
    return {'success': True, 'entries': entries, 'chain_ok': ok,
            'bad_seq': bad_seq, 'limit': limit}


# ---------------------------------------------------------------------------
# Write verbs. Each returns (status_code, payload_dict).
# ---------------------------------------------------------------------------

def do_lock(username, actor, lock_screen=True):
    err = _user_error(username)
    if err:
        return 400, err
    refused = device_policy.wheel_refusal(username)
    if refused:
        return 403, {'success': False, 'error': refused}
    success, message = run_lock_command(username, 'lock', actor=actor)
    if not success:
        return 400, {'success': False, 'message': message, 'via': 'remote'}
    note_manual_lock(username, actor=actor)
    # run_lock_command returns 'OK (<screen note>)'; mirror the panel's
    # user-facing text exactly.
    screen_note = message[3:-1] if message.startswith('OK (') else message
    return 200, {'success': True,
                 'message': f'Account locked and screen locked '
                            f'({screen_note})'}


def do_unlock(username, actor):
    err = _user_error(username)
    if err:
        return 400, err
    success, message = run_lock_command(username, 'unlock', actor=actor)
    if success:
        note_manual_unlock(username, actor=actor)
    return (200 if success else 400), (
        {'success': True, 'message': 'Account unlocked'} if success
        else {'success': False, 'message': message, 'via': 'remote'})


def do_logout(username, actor):
    """Emergency logout: lock + terminate-user (the only session-ender)."""
    err = _user_error(username)
    if err:
        return 400, err
    refused = device_policy.wheel_refusal(username)
    if refused:
        return 403, {'success': False, 'error': refused}
    try:
        result = subprocess.run(['sudo', 'usermod', '-L', username],
                                capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            msg = 'usermod -L: ' + (result.stderr.strip() or 'failed')
            _safe_audit(actor, 'account.logout', username,
                        {'success': False, 'message': msg, 'via': 'remote'})
            return 400, {'success': False, 'message': msg}
    except Exception as e:
        msg = 'usermod -L error: ' + str(e)
        _safe_audit(actor, 'account.logout', username,
                    {'success': False, 'message': msg, 'via': 'remote'})
        return 400, {'success': False, 'message': msg}
    note_manual_lock(username, actor=actor)
    try:
        result = subprocess.run(['sudo', 'loginctl', 'terminate-user',
                                 username],
                                capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            msg = ('loginctl terminate-user: '
                   + (result.stderr.strip() or 'failed'))
            _safe_audit(actor, 'account.logout', username,
                        {'success': False, 'message': msg, 'via': 'remote'})
            return 400, {'success': False, 'message': msg}
    except Exception as e:
        msg = 'loginctl terminate-user error: ' + str(e)
        _safe_audit(actor, 'account.logout', username,
                    {'success': False, 'message': msg, 'via': 'remote'})
        return 400, {'success': False, 'message': msg}
    _safe_audit(actor, 'account.logout', username,
                {'success': True, 'via': 'remote'})
    return 200, {'success': True,
                 'message': 'Session terminated. User logged out.'}


def do_bonus_grant(username, minutes, hours, actor):
    err = _user_error(username)
    if err:
        return 400, err
    refused = device_policy.wheel_refusal(username)
    if refused:
        return 403, {'success': False, 'error': refused}
    try:
        minutes = int(minutes or 0)
        hours = int(hours or 0)
    except (ValueError, TypeError):
        return 400, {'success': False, 'error': 'Invalid hours/minutes'}
    if minutes < 0 or hours < 0:
        return 400, {'success': False, 'error': 'Invalid hours/minutes'}
    total = hours * 60 + minutes
    if total < BONUS_CUSTOM_MIN:
        return 400, {'success': False,
                     'error': f'minimum bonus is {BONUS_CUSTOM_MIN} minutes'}
    entry = load_bedtime().get(username) or {}
    if not entry.get('start') or not entry.get('end') or not entry.get('days'):
        return 400, {'success': False,
                     'error': 'No bedtime schedule set for this user'}
    success, message = run_lock_command(username, 'unlock', actor=actor)
    if not success:
        return 400, {'success': False, 'message': message, 'locked': True}
    until = note_bonus_grant(username, total, grantor=actor)
    if until is None:
        return 400, {'success': False, 'error': 'Could not record bonus time',
                     'locked': False}
    duration = format_bonus_minutes(total)
    _safe_notify(username, 'Bonus time granted',
                 f'{duration} — device stays unlocked until '
                 f'{format_12h(until)}.')
    return 200, {'success': True,
                 'message': f'Granted {duration} of bonus time',
                 'locked': False, 'bonus_until': until.isoformat(),
                 'remaining_minutes': bonus_state(
                     load_bedtime().get(username, {}))['remaining_minutes']}


def do_bonus_revoke(username, actor):
    err = _user_error(username)
    if err:
        return 400, err
    refused = device_policy.wheel_refusal(username)
    if refused:
        return 403, {'success': False, 'error': refused}
    if not note_bonus_revoke(username, by=actor):
        return 400, {'success': False, 'error': 'No active bonus time'}
    now = datetime.now()
    data = load_bedtime()
    entry = data.get(username, {})
    locked_now = bool(get_lock_status(username)[0])
    if (is_in_bedtime(entry, now) and not entry.get('manual_override')
            and not locked_now):
        success, _msg = run_lock_command(username, 'lock', actor=actor)
        if success:
            entry['locked_by_bedtime'] = True
            save_bedtime(data)
            locked_now = True
            _safe_notify(username, 'Locked for bedtime',
                         f'Good night! The device unlocks at '
                         f'{format_12h(entry.get("end"))} unless a parent '
                         'changes it.')
    return 200, {'success': True, 'message': 'Bonus time revoked',
                 'locked': locked_now}


def do_bedtime(username, form, actor):
    err = _user_error(username)
    if err:
        return 400, err
    start = (form.get('start') or '').strip()
    end = (form.get('end') or '').strip()
    all_days = 'all_days' in form
    if form.get('clear') or (not start and not end):
        data = load_bedtime()
        existing = data.get(username, {})
        if username in data:
            del data[username]
            save_bedtime(data)
        _safe_audit(actor, 'bedtime.clear', username,
                    {'success': True, 'via': 'remote'})
        if existing.get('locked_by_bedtime'):
            success, message = run_lock_command(username, 'unlock', actor=actor)
            if not success:
                return 400, {'success': False, 'message': message,
                             'locked': True}
            return 200, {'success': True, 'message': 'Unlocked',
                         'locked': False}
        return 200, {'success': True, 'message': 'Bedtime cleared',
                     'locked': False}
    if _parse_hhmm(start) is None or _parse_hhmm(end) is None:
        return 400, {'success': False,
                     'error': 'Invalid time format (use HH:MM)'}
    if all_days:
        days = [0, 1, 2, 3, 4, 5, 6]
    else:
        days = [i for i in range(7) if f'day_{i}' in form]
        if not days:
            return 400, {'success': False,
                         'error': 'Select at least one day or check '
                                  '"Apply to all days"'}
    data = load_bedtime()
    existing = data.get(username, {})
    data[username] = {
        'start': start, 'end': end, 'days': sorted(days),
        'locked_by_bedtime': bool(existing.get('locked_by_bedtime', False)),
        'manual_override': False,
    }
    for key in BONUS_KEYS + ('bonus_history',):
        if key in existing:
            data[username][key] = existing[key]
    save_bedtime(data)
    _safe_audit(actor, 'bedtime.set', username,
                {'success': True, 'start': start, 'end': end,
                 'days': sorted(days), 'via': 'remote'})
    entry = data[username]
    if (_parse_bonus_until(entry) is not None
            and _parse_bonus_until(entry) > datetime.now()):
        return 200, {'success': True,
                     'message': 'Bedtime saved (bonus time active — lock '
                                'suppressed until bonus ends)',
                     'locked': bool(get_lock_status(username)[0])}
    if is_in_bedtime(entry):
        if not entry.get('locked_by_bedtime'):
            if budgets.is_admin_protected(username):
                return 200, {'success': True,
                             'message': 'Bedtime saved (auto-lock skipped: '
                                        'admin user)', 'locked': False}
            success, message = run_lock_command(username, 'lock', actor=actor)
            if success:
                entry['locked_by_bedtime'] = True
                save_bedtime(data)
                _safe_notify(username, 'Locked for bedtime',
                             f'Good night! The device unlocks at '
                             f'{format_12h(entry.get("end"))} unless a '
                             'parent changes it.')
                return 200, {'success': True, 'message': 'Locked',
                             'locked': True}
        return 200, {'success': True, 'message': 'Already locked by bedtime',
                     'locked': True}
    if existing.get('locked_by_bedtime'):
        success, message = run_lock_command(username, 'unlock', actor=actor)
        if success:
            entry['locked_by_bedtime'] = False
            save_bedtime(data)
            return 200, {'success': True, 'message': 'Unlocked',
                         'locked': False}
    return 200, {'success': True, 'message': 'Bedtime saved', 'locked': False}


def do_process_prefs(username, comm, action, actor):
    err = _user_error(username)
    if err:
        return 400, err
    if not comm:
        return 400, {'success': False, 'error': 'username and comm required'}
    ok, msg = budgets.set_pref(username, comm, action)
    if not ok:
        return 400, {'success': False, 'error': msg}
    _safe_audit(actor, 'process.pref', username,
                {'success': True, 'comm': comm, 'action': action,
                 'via': 'remote'})
    return 200, {'success': True, 'message': msg}


def do_freeze(username, category, actor, thaw=False):
    err = _user_error(username)
    if err:
        return 400, err
    if not budgets.valid_category(category):
        return 400, {'success': False,
                     'error': f'Unknown category: {category}'}
    refused = device_policy.freeze_refusal(username)
    if refused:
        return 403, {'success': False, 'error': refused}
    if thaw:
        ok, msg = budgets.unfreeze_category(username, category, actor=actor)
    else:
        ok, msg = budgets.freeze_category(username, category, actor=actor)
    if not ok:
        return 400, {'success': False, 'error': msg}
    return 200, {'success': True, 'message': msg}


# ---------------------------------------------------------------------------
# Router / WSGI app
# ---------------------------------------------------------------------------

_ROUTES = (
    ('GET', re.compile(r'^/api/v1/health/?$'), 'health'),
    ('GET', re.compile(r'^/api/v1/users/?$'), 'users'),
    ('GET', re.compile(r'^/api/v1/user/([^/]+)/live/?$'), 'user_live'),
    ('GET', re.compile(r'^/api/v1/audit/?$'), 'audit'),
    ('POST', re.compile(r'^/api/v1/lock/?$'), 'lock'),
    ('POST', re.compile(r'^/api/v1/lock-screen/?$'), 'lock_screen'),
    ('POST', re.compile(r'^/api/v1/unlock/?$'), 'unlock'),
    ('POST', re.compile(r'^/api/v1/logout/?$'), 'logout'),
    ('POST', re.compile(r'^/api/v1/bonus/?$'), 'bonus'),
    ('POST', re.compile(r'^/api/v1/bonus/revoke/?$'), 'bonus_revoke'),
    ('GET', re.compile(r'^/api/v1/bedtime/?$'), 'bedtime_get'),
    ('POST', re.compile(r'^/api/v1/bedtime/?$'), 'bedtime_set'),
    ('POST', re.compile(r'^/api/v1/process-prefs/?$'), 'process_prefs'),
    ('POST', re.compile(r'^/api/v1/budgets/freeze/?$'), 'freeze'),
    ('POST', re.compile(r'^/api/v1/budgets/unfreeze/?$'), 'unfreeze'),
)

TOKEN_EXEMPT = {'health'}


def match_route(method, path):
    """Return (route_name, groups) for an exact method+path match.

    When the path exists but not for this method, returns (None, 'method')
    so the caller can answer 405 instead of a misleading 404.
    """
    path_matched = False
    for verb, rx, name in _ROUTES:
        m = rx.match(path)
        if not m:
            continue
        path_matched = True
        if verb == method:
            return name, m.groups()
    return None, ('method' if path_matched else None)


def _parse_form(body):
    """application/x-www-form-urlencoded or JSON body -> dict of strings."""
    if not body:
        return {}
    text = body.decode('utf-8', 'replace')
    stripped = text.strip()
    if stripped.startswith('{'):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                return {k: ('' if v is None else str(v))
                        for k, v in obj.items()}
        except (ValueError, TypeError):
            return {}
    from urllib.parse import parse_qs
    parsed = parse_qs(text, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _read_body(environ):
    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0 or length > 1024 * 1024:
        return b''
    return environ['wsgi.input'].read(length)


class AgentApp:
    """Minimal WSGI application implementing the agent API surface."""

    def __init__(self, token_loader=load_token):
        self._token_loader = token_loader

    def __call__(self, environ, start_response):
        method = environ.get('REQUEST_METHOD', 'GET').upper()
        path = environ.get('PATH_INFO', '') or '/'
        if not path.startswith(API_PREFIX):
            return self._json(start_response, 404,
                              {'success': False, 'error': 'Not found'})
        name, args = match_route(method, path)
        if name is None and args == 'method':
            return self._json(start_response, 405,
                              {'success': False,
                               'error': 'Method not allowed'})
        if name is None:
            return self._json(start_response, 404,
                              {'success': False, 'error': 'Not found'})
        if name not in TOKEN_EXEMPT:
            token = self._token_loader()
            if not token:
                return self._json(start_response, 503, {
                    'success': False,
                    'error': 'agent token not provisioned'})
            if not token_ok(bearer_token(environ), token):
                return self._json(start_response, 401, {
                    'success': False,
                    'error': 'invalid or missing bearer token'})
        try:
            status, payload = self._dispatch(name, args, environ)
        except Exception:
            traceback.print_exc()
            status, payload = 500, {'success': False,
                                    'error': 'internal agent error'}
        return self._json(start_response, status, payload)

    def _dispatch(self, name, args, environ):
        if name == 'health':
            return 200, read_health()
        actor = remote_actor(environ)
        if name == 'users':
            return 200, read_users()
        if name == 'user_live':
            data = read_user_live(args[0])
            if data is None:
                return 404, {'success': False, 'error': 'User not found'}
            return 200, data
        if name == 'audit':
            from urllib.parse import parse_qs
            qs = parse_qs(environ.get('QUERY_STRING', ''))
            limit = (qs.get('limit') or ['100'])[0]
            return 200, read_audit(limit)
        form = _parse_form(_read_body(environ))
        if name in ('lock', 'lock_screen'):
            return do_lock(form.get('username', '').strip(), actor)
        if name == 'unlock':
            return do_unlock(form.get('username', '').strip(), actor)
        if name == 'logout':
            return do_logout(form.get('username', '').strip(), actor)
        if name == 'bonus':
            return do_bonus_grant(form.get('username', '').strip(),
                                  form.get('minutes', 0),
                                  form.get('hours', 0), actor)
        if name == 'bonus_revoke':
            return do_bonus_revoke(form.get('username', '').strip(), actor)
        if name == 'bedtime_get':
            return 200, load_bedtime()
        if name == 'bedtime_set':
            return do_bedtime(form.get('username', '').strip(), form, actor)
        if name == 'process_prefs':
            return do_process_prefs(form.get('username', '').strip(),
                                    form.get('comm', '').strip(),
                                    form.get('action', '').strip(), actor)
        if name == 'freeze':
            return do_freeze(form.get('username', '').strip(),
                             form.get('category', '').strip(), actor)
        if name == 'unfreeze':
            return do_freeze(form.get('username', '').strip(),
                             form.get('category', '').strip(), actor,
                             thaw=True)
        return 404, {'success': False, 'error': 'Not found'}

    @staticmethod
    def _json(start_response, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers = [('Content-Type', 'application/json'),
                   ('Content-Length', str(len(body))),
                   ('Cache-Control', 'no-store')]
        start_response(f'{status} {"OK" if status < 400 else "Error"}',
                       headers)
        return [body]


application = AgentApp()


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description='Parental Controls remote agent (managed box).')
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5001)
    parser.add_argument('--allow-lan', action='store_true',
                        help='Bind all interfaces (plain HTTP is refused '
                             'unless --insecure-lan is also given).')
    parser.add_argument('--insecure-lan', action='store_true')
    args = parser.parse_args(argv)
    if args.allow_lan or args.bind not in ('127.0.0.1', '::1', 'localhost'):
        if not args.insecure_lan:
            print('refusing non-loopback bind without --insecure-lan: the '
                  'agent speaks plain HTTP; use an SSH tunnel or TLS.',
                  file=sys.stderr)
            raise SystemExit(2)
        print('WARNING: --insecure-lan: the agent is reachable beyond this '
              'machine without TLS.', file=sys.stderr)
    if load_token() is None:
        print(f'FATAL: agent token missing ({token_path()}); provision it '
              'with install.sh --enable-agent. Refusing to start.',
              file=sys.stderr)
        raise SystemExit(1)
    host = '0.0.0.0' if args.allow_lan else args.bind
    from waitress import serve
    serve(application, host=host, port=args.port)


if __name__ == '__main__':
    main()
