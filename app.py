#!/usr/bin/env python3
"""Parental Controls Web UI for Bazzite.

A lightweight Flask app that lists human users on the system and allows
locking/unlocking their accounts via sudo commands.
"""

import subprocess
import pwd
import html
import json
import os
import re
import threading
import time
import traceback
import functools
from datetime import datetime, timedelta
from flask import (Flask, render_template, request, jsonify, redirect,
                   url_for, g, make_response)

import adminstore
import auth
import audit
import budgets
import store

app = Flask(__name__)

# Detail-row id sanitizer used by the server-rendered expand/collapse
# markup (mirrored by the JS detailId() helper in templates/user.html).
app.jinja_env.filters['slugify_id'] = (
    lambda value: re.sub(r'[^A-Za-z0-9_.-]', '-', str(value)))

# MVP: no TLS on localhost, so Secure cookies are opt-in for the day a
# TLS-terminating setup exists (PARENTAL_TLS=1). HttpOnly + SameSite=Lax
# are always set; CSRF tokens are the primary cross-site defense.
app.config['SECURE_COOKIES'] = os.environ.get('PARENTAL_TLS', '') == '1'

# Set by install.sh --insecure-lan (Environment=PARENTAL_INSECURE_LAN=1,
# rendered only in that unit variant): every page shows a persistent
# banner warning that logins travel in the clear. Absent otherwise.
app.config['INSECURE_LAN'] = (
    os.environ.get('PARENTAL_INSECURE_LAN', '') == '1')


@app.context_processor
def _insecure_lan_banner():
    return {'insecure_lan': app.config.get('INSECURE_LAN', False)}

MIN_UID = 1000
MAX_UID = 60000

BEDTIME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bedtime.json')

DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

# Bonus time (parent-granted extra time past bedtime). The five
# one-click shortcuts are 15/30/60/90/120; the custom entry takes hours +
# minutes with no upper cap (parent decision 2026-10-10: no stacked cap,
# lock when the bonus runs out). New grants stack onto any active bonus.
# Floors/history caps below are the BONUS_TIME_PLAN.md §7 decisions.
BONUS_SHORTCUTS = (15, 30, 60, 90, 120)
BONUS_CUSTOM_MIN = 5
BONUS_HISTORY_CAP = 20


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
    act = actor or _req_actor()
    audit_action = ('account.lock' if action == 'lock'
                    else 'account.unlock' if action == 'unlock'
                    else 'account.unknown')

    def _done(success, message):
        _safe_audit(act, audit_action, username,
                    {'success': success, 'message': message})
        return success, message

    if action == 'lock':
        try:
            result = subprocess.run(
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
        return _done(False, '; '.join(errors))
    return _done(True, 'OK')


# ---------------------------------------------------------------------------
# Bedtime settings storage (SQLite via store.py; BEDTIME_FILE is kept as the
# legacy import source and rollback mirror — the db is the single source
# of truth and the JSON file is never read back after migration)
# ---------------------------------------------------------------------------

def load_bedtime():
    """Load bedtime settings. Returns dict keyed by username."""
    return store.load_table(store.db_path_for(BEDTIME_FILE), 'bedtime')


def save_bedtime(data):
    """Persist bedtime settings (atomic commit + rollback mirror)."""
    store.save_table(store.db_path_for(BEDTIME_FILE), 'bedtime',
                     data, BEDTIME_FILE)


def note_manual_unlock(username, actor=None):
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
    _safe_audit(actor or _req_actor(), 'bedtime.manual_unlock', username,
                {'success': True})


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


# ---------------------------------------------------------------------------
# Bonus time (parent-granted extra minutes past bedtime)
# ---------------------------------------------------------------------------
#
# Live state is stored inside the user's bedtime entry (no new file,
# no migration):
#   bonus_until     ISO timestamp when the grant expires
#   bonus_minutes   minutes granted by the most recent grant
#   bonus_granted_by  admin who granted it
#   bonus_granted_at  when
#   bonus_history   capped audit trail feeding the Phase-3 audit log

BONUS_KEYS = ('bonus_until', 'bonus_minutes', 'bonus_granted_by',
              'bonus_granted_at')


def _parse_bonus_until(entry):
    """DateTime of entry['bonus_until'], or None when absent/unparseable.

    Fail closed: a corrupt value is treated as *no bonus*, so bedtime
    enforcement never trusts garbage state (it will simply lock again).
    """
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
    """Append a bonus audit entry, keeping only the newest BONUS_HISTORY_CAP."""
    if not isinstance(entry.get('bonus_history'), list):
        entry['bonus_history'] = []
    entry['bonus_history'].append({
        'at': datetime.now().isoformat(timespec='seconds'),
        'by': by or 'system',
        'minutes': minutes,
        'until': until,
        'kind': kind,
    })
    del entry['bonus_history'][:-BONUS_HISTORY_CAP]


def _drop_bonus_keys(entry):
    """Remove live bonus state (history is kept for the audit trail)."""
    for key in BONUS_KEYS:
        entry.pop(key, None)


def compute_bonus_until(entry, minutes, now=None):
    """The grant's new bonus_until (stacking onto an active grant).

    Returns (until, kind) where kind is 'grant' or 'extend'. Grants stack
    without an upper cap (parent decision 2026-10-10); expiry re-locks.
    """
    now = now or datetime.now()
    existing = _parse_bonus_until(entry)
    base = max(now, existing) if existing is not None else now
    until = base + timedelta(minutes=minutes)
    kind = 'extend' if existing is not None and existing > now else 'grant'
    return until, kind


def format_bonus_minutes(total):
    """Human duration for toasts/notifications: '30 minutes', '1 hour 5 minutes'."""
    total = int(total)
    hours, minutes = divmod(total, 60)
    parts = []
    if hours:
        parts.append(f'{hours} hour' + ('' if hours == 1 else 's'))
    if minutes or not parts:
        parts.append(f'{minutes} minute' + ('' if minutes == 1 else 's'))
    return ' '.join(parts)


def note_bonus_grant(username, minutes, grantor=None, now=None):
    """Record a bonus grant for `username`.

    Clears manual_override (a bonus is bounded, an override is not) and
    clears locked_by_bedtime so the bonus actually means "stays
    unlocked". Returns the until datetime, or None when the user has no
    entry. The account itself is unlocked by the caller
    (mirrors api_unlock → note_manual_unlock).
    """
    now = now or datetime.now()
    data = load_bedtime()
    entry = data.get(username)
    if entry is None:
        _safe_audit(grantor or _req_actor(), 'bonus.grant', username,
                    {'success': False, 'minutes': minutes,
                     'error': 'no bedtime entry'})
        return None
    until, kind = compute_bonus_until(entry, minutes, now)
    entry['bonus_until'] = until.isoformat(timespec='seconds')
    entry['bonus_minutes'] = minutes
    entry['bonus_granted_by'] = grantor or 'admin'
    entry['bonus_granted_at'] = now.isoformat(timespec='seconds')
    entry['manual_override'] = False
    entry['locked_by_bedtime'] = False
    _append_bonus_history(entry, kind, minutes, entry['bonus_until'],
                          entry['bonus_granted_by'])
    save_bedtime(data)
    _safe_audit(grantor or _req_actor(), 'bonus.grant', username,
                {'success': True, 'minutes': minutes,
                 'until': entry['bonus_until'], 'kind': kind})
    return until


def note_bonus_revoke(username, by=None):
    """Cancel active bonus time; returns True if one existed and was removed."""
    data = load_bedtime()
    entry = data.get(username)
    if entry is None or _parse_bonus_until(entry) is None:
        _safe_audit(by or _req_actor(), 'bonus.revoke', username,
                    {'success': False, 'error': 'no active bonus time'})
        return False
    _append_bonus_history(entry, 'revoke', entry.get('bonus_minutes'),
                          entry.get('bonus_until'), by or 'admin')
    _drop_bonus_keys(entry)
    save_bedtime(data)
    _safe_audit(by or _req_actor(), 'bonus.revoke', username,
                {'success': True})
    return True


def bonus_state(entry, now=None):
    """Summary dict for the UI/live payload.

    {'active', 'until', 'remaining_minutes', 'granted_by', 'last_minutes'}
    """
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


def note_manual_lock(username, actor=None):
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
            _safe_audit(actor or _req_actor(), 'bedtime.manual_lock',
                        username, {'success': True, 'note': 'no bedtime entry'})
            return
        changed = False
        if entry.get('locked_by_bedtime'):
            entry['locked_by_bedtime'] = False
            changed = True
        if entry.get('manual_override'):
            entry['manual_override'] = False
            changed = True
        # A manual lock also cancels any active bonus time: the parent's
        # lock is the strongest signal and must win over a timer grant.
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
    _safe_audit(actor or _req_actor(), 'bedtime.manual_lock', username,
                {'success': True})


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


def format_12h(value):
    """'21:00' -> '9:00 PM'; datetime -> same 12-hour form.

    Display-only: storage, schedules, and API payloads stay 24h/ISO.
    Empty -> ''; unparseable passes through unchanged (fail-open display:
    a page must never crash on odd data).
    """
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


app.jinja_env.filters['twelve'] = format_12h


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
      sudo systemd-run --machine=<user>@.host --user \
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
        # Must go through sudo: the machine transport is opened on the
        # *system* bus, which the unprivileged service user cannot reach
        # ("Failed to connect to system scope bus via machine transport:
        # Permission denied"). Sudoers scopes this to notify-send only --
        # see sudoers-parental-controls.
        result = subprocess.run(
            ['sudo', 'systemd-run', f'--machine={username}@.host', '--user',
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


def _req_actor():
    """Actor name for audit entries: the logged-in admin in requests,
    'system' everywhere else (timer loops, direct helper calls).

    Never raises: accessing `g` outside an app/request context raises
    RuntimeError, which falls back to 'system'.
    """
    try:
        return getattr(g, 'admin', 'system') or 'system'
    except Exception:
        return 'system'


def _safe_audit(actor, action, target='', detail=None):
    """audit.safe_log that can never raise (mirrors _safe_notify: audit
    is additive — a logging failure must never break a lock/unlock)."""
    try:
        audit.safe_log(actor, action, target, detail)
    except Exception as e:
        print(f'audit (suppressed): {e}')


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

        # --- Bonus time tick ---
        # A live bonus (bonus_until in the future) suppresses the lock
        # only: the account stays unlocked past its window until the
        # bonus expires (warnings/auto-unlock bookkeeping still run, per
        # BONUS_TIME_PLAN.md §2). An expired bonus behaves as no bonus
        # (enforcement below resumes and re-locks if still in-window).
        bonus_until = _parse_bonus_until(entry)
        bonus_expired_this_pass = False
        bonus_active = False
        if bonus_until is not None:
            if bonus_until > now:
                bonus_active = True
            else:
                _append_bonus_history(entry, 'expire', entry.get('bonus_minutes'),
                                      entry.get('bonus_until'), 'system')
                bonus_expired_this_pass = True
                changed = True
                _drop_bonus_keys(entry)

        in_bed = is_in_bedtime(entry, now)
        locked_by_bedtime = bool(entry.get('locked_by_bedtime', False))

        if in_bed:
            if entry.get('manual_override'):
                # Parent manually unlocked during this bedtime window:
                # leave the account alone until the window ends.
                continue
            if bonus_active:
                # Bonus time overrides the lock only — the account stays
                # unlocked past bedtime until the bonus expires. Nothing
                # else to do for an in-window user this pass.
                continue
            locked, _detail = get_lock_status(username)
            if locked is False:
                # Session-preserving lock (usermod -L + screen-lock):
                # bedtime never logs the user out.
                success, _msg = run_lock_command(username, 'lock')
                if success:
                    entry['locked_by_bedtime'] = True
                    changed = True
                    # Warn again at lock time (best-effort, never blocks).
                    if bonus_expired_this_pass:
                        _safe_notify(username, 'Bonus time ended',
                                     f'The device unlocks at '
                                     f'{format_12h(entry.get("end"))} '
                                     'unless a parent changes it.')
                    else:
                        _safe_notify(username, 'Locked for bedtime',
                                     'Good night! The device unlocks at '
                                     f'{format_12h(entry.get("end"))} '
                                     'unless a parent changes it.')
        else:
            # Bedtime is over (or no valid schedule): a leftover bonus is
            # meaningless past the scheduled unlock, so clear it instead of
            # letting a stale "Bonus active" linger (parent decision
            # 2026-10-10). Expiry inside a window is handled above.
            if _parse_bonus_until(entry) is not None:
                _append_bonus_history(entry, 'window_end',
                                      entry.get('bonus_minutes'),
                                      entry.get('bonus_until'), 'system')
                _drop_bonus_keys(entry)
                changed = True
            # Pre-warning: bedtime starts soon, warn once per window.
            window_id = bedtime_window_id(entry)
            mins = minutes_until_bedtime(entry, now)
            if (mins is not None and 0 < mins <= BEDTIME_WARN_MINUTES
                    and entry.get('warned_for') != window_id):
                _safe_notify(username, 'Bedtime in 15 minutes',
                             f'Bedtime starts at {format_12h(entry.get("start"))}. '
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
            if locked_by_bedtime and not bonus_active:
                # Re-read ownership from disk immediately before unlocking.
                # `data` is a snapshot taken at the top of this pass, and
                # the systemd timer runs in a *separate process* from the
                # web UI -- a manual lock written since the snapshot must
                # win, or we would usermod -U an account the parent just
                # deliberately locked. Never unlock on a stale snapshot.
                # (While a bonus is active, skip this bookkeeping entirely:
                # the grant owns the unlock state.)
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
            # freezer enforcement, same 60s cadence as bedtime. Locked
            # accounts bill nothing (get_lock_status feeds account_locked).
            budgets.tick_budgets(load_bedtime(), datetime.now(),
                                 notify=notify_user,
                                 account_locked=lambda u: get_lock_status(u)[0])
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
            'bonus': bonus_state(entry),
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
            'bonus': bonus_state(entry),
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
                _safe_audit(_req_actor(), 'account.lock', username,
                            {'success': False, 'message': msg,
                             'via': 'api_lock'})
                return jsonify({'success': False, 'message': msg}), 400
    except Exception as e:
        _safe_audit(_req_actor(), 'account.lock', username,
                    {'success': False, 'message': 'usermod -L error: ' + str(e),
                     'via': 'api_lock'})
        return jsonify({'success': False, 'message':  'usermod -L error: ' + str(e)}), 400

    # A manual lock takes ownership away from bedtime so the bedtime
    # timer cannot later auto-unlock it (see note_manual_lock).
    note_manual_lock(username)

    # Lock the screen without terminating the session
    screen_msg = lock_user_sessions(username)

    _safe_audit(_req_actor(), 'account.lock', username,
                {'success': True, 'message': f'OK ({screen_msg})',
                 'via': 'api_lock'})
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
                _safe_audit(_req_actor(), 'account.lock', username,
                            {'success': False, 'message': msg,
                             'via': 'api_lock_screen'})
                return jsonify({'success': False, 'message': msg}), 400
    except Exception as e:
        _safe_audit(_req_actor(), 'account.lock', username,
                    {'success': False, 'message': 'usermod -L error: ' + str(e),
                     'via': 'api_lock_screen'})
        return jsonify({'success': False, 'message':  'usermod -L error: ' + str(e)}), 400

    # A manual lock takes ownership away from bedtime so the bedtime
    # timer cannot later auto-unlock it (see note_manual_lock).
    note_manual_lock(username)

    # Lock the screen without terminating the session
    screen_msg = lock_user_sessions(username)

    _safe_audit(_req_actor(), 'account.lock', username,
                {'success': True, 'message': f'OK ({screen_msg})',
                 'via': 'api_lock_screen'})
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


@app.route('/api/bonus', methods=['POST'])
@require_admin
def api_bonus_grant():
    """Grant bonus time: keep a user's account unlocked past bedtime.

    Form fields: username, minutes and/or hours (shortcuts send minutes;
    the custom entry sends hours + minutes; the server re-validates).
    Grants stack onto an active bonus with no upper cap; expiry re-locks.
    A grant is a bounded manual unlock: it clears manual_override.
    """
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    refused = _wheel_target_refused(username)
    if refused is not None:
        return refused

    try:
        minutes = int(request.form.get('minutes', '0').strip() or 0)
        hours = int(request.form.get('hours', '0').strip() or 0)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'Invalid hours/minutes'}), 400
    if minutes < 0 or hours < 0:
        return jsonify({'success': False, 'error': 'Invalid hours/minutes'}), 400
    total = hours * 60 + minutes
    if total < BONUS_CUSTOM_MIN:
        return jsonify({'success': False, 'error':
                        f'minimum bonus is {BONUS_CUSTOM_MIN} minutes'}), 400
    minutes = total

    entry = load_bedtime().get(username) or {}
    if not entry.get('start') or not entry.get('end') or not entry.get('days'):
        return jsonify({'success': False,
                        'error': 'No bedtime schedule set for this user'}), 400

    # Unlock first so the bonus is immediately usable, then record it
    # (mirrors api_unlock → note_manual_unlock ordering).
    now = datetime.now()
    success, message = run_lock_command(username, 'unlock')
    if not success:
        return jsonify({'success': False, 'message': message,
                        'locked': True}), 400
    until = note_bonus_grant(username, minutes, grantor=g.admin, now=now)
    if until is None:
        # Should not happen (entry presence was validated above); leave
        # the account usable rather than double-locking.
        return jsonify({'success': False, 'error':
                        'Could not record bonus time',
                        'locked': False}), 400
    duration = format_bonus_minutes(minutes)
    _safe_notify(username, 'Bonus time granted',
                 f'{duration} — device stays unlocked until '
                 f'{format_12h(until)}.')
    return jsonify({'success': True,
                    'message': f'Granted {duration} of bonus time',
                    'locked': False,
                    'bonus_until': until.isoformat(),
                    'remaining_minutes': bonus_state(
                        load_bedtime().get(username, {}), now)['remaining_minutes']})


@app.route('/api/bonus/revoke', methods=['POST'])
@require_admin
def api_bonus_revoke():
    """Cancel bonus time immediately; re-lock now if still in bedtime.

    Form fields: username. Revoking drops the bonus and re-runs this
    user's enforcement inline (locks at once when in window) instead of
    waiting up to 60s for the next enforce tick.
    """
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    refused = _wheel_target_refused(username)
    if refused is not None:
        return refused

    if not note_bonus_revoke(username, by=g.admin):
        return jsonify({'success': False, 'error': 'No active bonus time'}), 400

    # Re-run this user's enforcement inline: lock at once if in bedtime
    # and not manually overridden.
    now = datetime.now()
    data = load_bedtime()
    entry = data.get(username, {})
    locked_now = bool(get_lock_status(username)[0])
    if (is_in_bedtime(entry, now)
            and not entry.get('manual_override')
            and not locked_now):
        # Session-preserving re-lock (never terminates the session).
        success, _msg = run_lock_command(username, 'lock')
        if success:
            entry['locked_by_bedtime'] = True
            save_bedtime(data)
            locked_now = True
            _safe_notify(username, 'Locked for bedtime',
                         'Good night! The device unlocks at '
                         f'{format_12h(entry.get("end"))} '
                         'unless a parent changes it.')
    return jsonify({'success': True, 'message': 'Bonus time revoked',
                    'locked': locked_now})


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
        _safe_audit(_req_actor(), 'bedtime.clear', username,
                    {'success': True})
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
    # Preserve any live bonus-plus-term state across schedule edits (a
    # saved window must not silently eat an active bonus grant).
    for key in BONUS_KEYS + ('bonus_history',):
        if key in existing:
            data[username][key] = existing[key]
    save_bedtime(data)
    _safe_audit(_req_actor(), 'bedtime.set', username,
                {'success': True, 'start': start, 'end': end, 'days': days})

    # Immediately enforce the new bedtime for this user
    entry = data[username]
    # An active bonus-plus-term keeps the account unlocked: the saved
    # schedule must not contradict the parent's explicit grant.
    if _parse_bonus_until(entry) is not None and _parse_bonus_until(entry) > datetime.now():
        return jsonify({'success': True,
                        'message': 'Bedtime saved (bonus time active — '
                                   'lock suppressed until bonus ends)',
                        'locked': bool(get_lock_status(username)[0])})
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
            # Session-preserving auto-lock (usermod -L + screen-lock).
            success, message = run_lock_command(username, 'lock')
            if success:
                entry['locked_by_bedtime'] = True
                save_bedtime(data)
                _safe_notify(username, 'Locked for bedtime',
                             'Good night! The device unlocks at '
                             f'{format_12h(entry.get("end"))} '
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
            _safe_audit(_req_actor(), 'account.logout', username,
                        {'success': False,
                         'message': 'usermod -L: ' + (result.stderr.strip() or 'failed')})
            return jsonify({'success': False, 'message': 'usermod -L: ' + (result.stderr.strip() or 'failed')}), 400
    except Exception as e:
        _safe_audit(_req_actor(), 'account.logout', username,
                    {'success': False,
                     'message': 'usermod -L error: ' + str(e)})
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
            _safe_audit(_req_actor(), 'account.logout', username,
                        {'success': False,
                         'message': 'loginctl terminate-user: ' + (result.stderr.strip() or 'failed')})
            return jsonify({'success': False, 'message': 'loginctl terminate-user: ' + (result.stderr.strip() or 'failed')}), 400
    except Exception as e:
        _safe_audit(_req_actor(), 'account.logout', username,
                    {'success': False,
                     'message': 'loginctl terminate-user error: ' + str(e)})
        return jsonify({'success': False, 'message': 'loginctl terminate-user error: ' + str(e)}), 400

    _safe_audit(_req_actor(), 'account.logout', username, {'success': True})
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
    ok, msg = budgets.set_limit(username, category, minutes,
                                actor=_req_actor())
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
    ok, msg = budgets.resume_category(username, category,
                                      actor=_req_actor())
    if not ok:
        return jsonify({'success': False, 'error': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/budgets/freeze', methods=['POST'])
@require_admin
def api_budgets_freeze():
    """Manually freeze a budgeted category (parent's Freeze button)."""
    username = request.form.get('username', '').strip()
    category = request.form.get('category', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    if not budgets.valid_category(category):
        return jsonify({'success': False,
                        'error': f'Unknown category: {category}'}), 400
    refused = _wheel_target_refused(username)
    if refused is not None:
        return refused
    if budgets.is_admin_protected(username):
        return jsonify({'success': False,
                        'error': (f'Refused: {username} is an admin and '
                                  'cannot be frozen')}), 403
    ok, msg = budgets.freeze_category(username, category,
                                      actor=_req_actor())
    if not ok:
        return jsonify({'success': False, 'error': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/budgets/unfreeze', methods=['POST'])
@require_admin
def api_budgets_unfreeze():
    """Manually unfreeze a frozen category (parent's Unfreeze button)."""
    username = request.form.get('username', '').strip()
    category = request.form.get('category', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    if not budgets.valid_category(category):
        return jsonify({'success': False,
                        'error': f'Unknown category: {category}'}), 400
    refused = _wheel_target_refused(username)
    if refused is not None:
        return refused
    if budgets.is_admin_protected(username):
        return jsonify({'success': False,
                        'error': (f'Refused: {username} is an admin and '
                                  'cannot be frozen')}), 403
    ok, msg = budgets.unfreeze_category(username, category,
                                        actor=_req_actor())
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
                'bonus': bonus_state(entry),
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
        try:
            home = pwd.getpwnam(username).pw_dir
        except KeyError:
            home = None
        index = budgets.build_desktop_index(username)
        # Auto-hide: flag explicitly user-started processes first; the
        # filter below hides everything else unless the parent re-shows
        # it via shown_comm. The membership index covers EVERY installed
        # app (names, not just categorized ones) so uncategorized apps
        # like terminals stay visible. Absent flags default to visible
        # so callers passing unmarked snapshots behave as before.
        names = budgets.build_desktop_names(username)
        budgets.mark_user_apps(processes, names, home)
        visible, hidden = budgets.apply_filter(processes, username, prefs)
        budgets.annotate_display_names(processes, names)
        groups = budgets.group_by_category(visible, index)
        # Group duplicate comms inside each category bucket: each bucket
        # becomes [{comm, count, members, max_elapsed, ...}] group dicts.
        groups = {cat: budgets.group_by_comm(procs)
                  for cat, procs in groups.items()}
        # Annotate hidden rows with their category, then group across
        # categories (members keep per-member categories for the Mixed
        # label when the group spans categories).
        for p in hidden:
            p['category'] = budgets.categorize(p, index)
        hidden = budgets.group_by_comm(hidden)
    else:
        visible, hidden, groups = [], [], {c: [] for c in budgets.CATEGORIES}
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
        cat: [{'comm': g['comm'],
               'display_name': g.get('display_name') or g['comm'],
               'count': g['count'],
               'elapsed': g.get('max_elapsed') or '',
               'members': [
                   {'pid': m['pid'], 'comm': m['comm'],
                    'display_name': m.get('display_name') or m.get('comm', ''),
                    'elapsed': m.get('elapsed') or m.get('etime', '')}
                   for m in g['members']]}
              for g in procs]
        for cat, procs in data['groups'].items()
    }
    slim_hidden = [
        {'comm': g['comm'],
         'display_name': g.get('display_name') or g['comm'],
         'count': g['count'],
         'categories': g.get('categories', []),
         'uniform_category': g.get('uniform_category', True),
         'members': [
             {'pid': m.get('pid'), 'comm': m.get('comm'),
              'display_name': m.get('display_name') or m.get('comm', ''),
              'category': m.get('category'),
              'elapsed': m.get('elapsed') or m.get('etime', '')}
             for m in g['members']]}
        for g in data['hidden'] if 'error' not in g
    ]
    return jsonify({
        'success': True,
        'locked': user['locked'],
        'status_detail': user['status_detail'],
        'in_bedtime': user['in_bedtime'],
        'manual_override': bool(user['bedtime'].get('manual_override', False)),
        'bonus': user['bonus'],
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
    """Create an app-only admin account. Wheel admins only.

    The web console only ever adds app-only admins: a wheel member's
    row is bootstrap/CLI territory (mirrors the CLI constraints, but the
    UI refuses wheel targets outright — wheel admins are never addable
    from the UI). Refuses non-wheel system accounts: that would hand a
    login to a regular local user (e.g. the child). The password is
    generated server-side and returned ONCE in the response.
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
    try:
        tier, generated = adminstore.add_admin(
            username, store=store, allow_wheel=False)
    except adminstore.RefusedError as e:
        return jsonify({'success': False, 'error': str(e)}), 403
    except adminstore.AdminExistsError:
        return jsonify({'success': False,
                        'error': 'Admin already exists.'}), 400
    except adminstore.AuthStoreUnavailable as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return jsonify({'success': False,
                        'error': 'Authentication unavailable'}), 500
    _safe_audit(_req_actor(), 'admin.add', username,
                {'success': True, 'tier': tier})
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
    try:
        # Re-checks wheel membership live after loading the store
        # (belt and braces), then deletes + persists.
        adminstore.remove_admin(username, store=store)
    except adminstore.RefusedError as e:
        return jsonify({'success': False, 'error': str(e)}), 403
    except adminstore.NoSuchAdminError:
        return jsonify({'success': False,
                        'error': 'No such admin.'}), 404
    except adminstore.AuthStoreUnavailable as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return jsonify({'success': False,
                        'error': 'Authentication unavailable'}), 500
    _safe_audit(_req_actor(), 'admin.remove', username, {'success': True})
    return jsonify({'success': True,
                    'message': f'Admin {username} removed.'})


@app.route('/api/audit')
@require_admin
def api_audit():
    """Read the tamper-evident audit log (newest first) + chain status.

    Query param: limit=N (default 100, capped at 1000).
    """
    try:
        limit = int(request.args.get('limit', '100'))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))
    entries = audit.read_entries(limit=limit)
    ok, bad_seq = audit.verify_chain()
    return jsonify({'success': True, 'entries': entries,
                    'chain_ok': ok, 'bad_seq': bad_seq, 'limit': limit})


@app.route('/admin/audit')
@require_admin
def admin_audit():
    """Admin console: tamper-evident audit viewer + admin management.

    Read-only audit table (newest-first) with chain status and a
    limit-based "load more" control (the button grows the limit via the
    existing /api/audit endpoint, capped at 1000 — nothing is deletable).
    The admin-management section lists every store row (wheel rows are
    shown as protected); the add/remove actions are wheel-actor only and
    target app-only admins only, enforced server-side by
    adminstore.add_admin/remove_admin.
    """
    try:
        limit = int(request.args.get('limit', '100'))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))
    # Peek one past the limit purely to decide whether "load more" can
    # grow (the page never exceeds the cap).
    entries = audit.read_entries(limit=limit + 1)
    has_more = len(entries) > limit
    entries = entries[:limit]
    for e in entries:
        e['detail_summary'] = _audit_detail_summary(e.get('detail'))
    chain_ok, bad_seq = audit.verify_chain()
    admins = []
    try:
        store = auth.load_store()
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        store = None
    if store is not None:
        for username, row in store.get('users', {}).items():
            if not isinstance(row, dict):
                continue
            admins.append({
                'username': username,
                'tier': auth.classify_admin(username, store),
                'locked': bool(row.get('locked', False)),
                'must_change': bool(row.get('must_change', False)),
            })
        admins.sort(key=lambda a: a['username'])
    return render_template('admin_audit.html', entries=entries,
                           chain_ok=chain_ok, bad_seq=bad_seq,
                           limit=limit, has_more=has_more,
                           admins=admins)


AUDIT_DETAIL_SUMMARY_LEN = 120


def _audit_detail_summary(detail):
    """One-line detail summary for the audit table (display only)."""
    if detail is None:
        return ''
    if isinstance(detail, str):
        text = detail
    else:
        try:
            text = json.dumps(detail, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(detail)
    if len(text) <= AUDIT_DETAIL_SUMMARY_LEN:
        return text
    return text[:AUDIT_DETAIL_SUMMARY_LEN - 1] + '\u2026'


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
              'session cookies travel in the clear '
              '(dev-only; production LAN requires install.sh --tls).',
              flush=True)
    t = threading.Thread(target=bedtime_loop, daemon=True)
    t.start()
    app.run(host=host, port=args.port, debug=False)
