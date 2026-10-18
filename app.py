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
                   url_for, g, make_response, flash)

import adminstore
import auth
import audit
import budgets
import device_policy
import remote
import store

app = Flask(__name__)

# Flask's flash() pipeline (POST/Redirect/GET feedback for the no-JS Overview
# forms) rides on the signed session cookie; the app's own auth cookie
# (auth.SESSION_COOKIE) is separate. A per-process random key is enough — a
# flash only has to survive one redirect — and there is no secret to manage.
# PARENTAL_SECRET_KEY lets a multi-worker deploy pin the key if it ever needs
# to (single waitress process today).
app.secret_key = os.environ.get('PARENTAL_SECRET_KEY') or os.urandom(32)

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


@app.context_processor
def _multidevice_gate():
    """Feature gate (MULTIDEVICE_PLAN.md §5.5.3).

    With no devices.json the selector block renders nothing, so single-box
    installs are byte-identical to before. A corrupt store does not take
    the panel down: it is reported via /api/devices instead.
    """
    try:
        enabled = remote.devices_enabled()
    except Exception:
        enabled = False
    return {'devices_enabled': enabled}

# ---------------------------------------------------------------------------
# 5-section IA shell context (design-exports/SPEC.md "Shared shell").
# One place maps a request endpoint onto the active nav section and supplies
# the sidebar's machine card + user chip, so no route has to pass them.
# ---------------------------------------------------------------------------
NAV_SECTIONS = {
    'index': 'overview',
    'time_rules': 'time',
    'user_detail': 'time',
    'budgets': 'time',
    'processes': 'activity',
    'admin_audit': 'admin',
    'settings': 'settings',
}


@app.context_processor
def _shell_context():
    """Active section + sidebar/app-bar context for every rendered page."""
    device = (request.args.get('device', '') or '').strip()
    try:
        host = request.host
    except Exception:  # pragma: no cover - only outside a request
        host = 'localhost'
    ctx = {
        'active_nav': NAV_SECTIONS.get(request.endpoint or '', ''),
        'app_title': 'Parental Controls',
        'app_subtitle': 'Manage this Bazzite system',
        # Scheme-less on purpose: rendered pages must never carry an http://
        # literal (offline promise asserted by the UI tests).
        'machine_name': device or 'This box',
        'machine_url': host,
    }
    admin = getattr(g, 'admin', '')
    if admin:
        ctx['current_user'] = admin
        ctx['current_tier'] = getattr(g, 'tier', '') or ''
    return ctx


@app.context_processor
def _fixture_preview():
    """Design fixtures are documentation, not product content (owner intent).

    The Flowstep screens carry annotation cards, state/theme specimens and a
    worked confirmation-dialog example. Those are implementation guidance for
    the code agent — they must never render in the normal user experience.
    They stay reachable for review behind a dev-only preview: `?preview=1`
    (or `?preview=fixtures`) on any page, or PARENTAL_DEV_PREVIEW=1 in the
    environment. Nothing here enables any privileged behaviour.
    """
    raw = (request.args.get('preview', '') or '').strip().lower()
    enabled = raw in ('1', 'true', 'yes', 'on', 'dev', 'fixtures')
    if not enabled:
        enabled = os.environ.get('PARENTAL_DEV_PREVIEW', '') == '1'
    return {'show_fixtures': enabled}


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


# ---------------------------------------------------------------------------
# Status-detail sanitization (the ONE parent-safe mapping)
#
# Bug (owner report, 2026-10-18): the child cards on the Overview showed a
# warning box full of raw system text --
#
#     sudo: pam_open_session: Permission denied
#     sudo: policy plugin failed session initialization
#
# `get_lock_status` above captures the probe's stderr verbatim whenever
# `sudo passwd -S` exits non-zero, and every payload below copied that
# string straight into `status_detail`, which accountHtml()'s status pill
# and `templates/user.html` render to the parent. A parent can do nothing
# with "pam_open_session"; the raw text belongs in the admin-only audit
# log, never on a card.
#
# So the RAW probe output never crosses the payload boundary. Every
# producer below runs `status_display()` and puts its RETURN VALUE in
# `status_detail`. The helper is pure (text in, text + class out) so the
# Jinja/JS renderers cannot leak: by the time a row leaves app.py it
# already carries parent-safe copy, and there is no code path left that
# hands raw stderr to a template.
#
# agent.py carries a byte-identical mirror of this block (the agent must
# never import app -- see its module docstring), pinned by
# tests/test_status_detail.py::StatusDetailMirrorTest.

# Probe outcome classes. Only UNAVAILABLE ever reaches a parent as the
# "needs help" copy; OK is a real status the pill already had; VERBATIM is
# already-human product copy that must survive untouched.
STATUS_OK = 'ok'
STATUS_UNAVAILABLE = 'unavailable'
STATUS_VERBATIM = 'verbatim'

STATUS_UNAVAILABLE_COPY = 'Status check needs help — ask your admin'

# Sentinel: "no probe ran behind this detail". Distinct from None, which
# is a real probe outcome (get_lock_status returns None for "could not
# determine") and must therefore NOT be treated as "status succeeded".
_UNSET = object()

# BENIGN NOISE. These markers mean the probe's *session* layer (sudo's PAM
# stack / logind / systemd-logind via Varlink) was unhappy while the
# password-status read itself was fine. A line containing one of these is
# DROPPED before the detail is judged: with a real status word left over
# the parent keeps the normal pill and sees no warning box at all. This is
# the exact pair the VM produced -- pam_systemd's login policy answered
# `io.systemd.Login.TooManySessions`, `passwd -S` still reported the
# account status. Matched case-insensitively against each line.
STATUS_NOISE_PATTERNS = (
    'pam_open_session',
    'pam_close_session',
    'pam_unix',
    'pam_systemd',
    'policy plugin',
    'session initialization',
    'session opened',
    'session closed',
    'toomanysessions',
    'too many sessions',
    'io.systemd.login',
    'varlink',
)

# NEVER PARENT-SAFE. Anything still carrying one of these after the noise
# lines are dropped is a genuine probe failure or a leak: a missing
# binary, a permission error, a timeout, a malformed status line, or an
# exception string with a path/cgroup in it. Treated fail-closed --
# UNAVAILABLE -- so the parent gets the friendly copy and the raw text goes
# to the audit log.
STATUS_TECHNICAL_PATTERNS = (
    'sudo',
    'pam_',
    'policy plugin',
    'cgroup',
    '/sys/',
    '/usr/',
    '/bin/',
    '/sbin/',
    '/etc/',
    '/var/',
    'errno',
    'traceback',
    'exception',
    'command not found',
    'not found',
    'no such file',
    'permission denied',
    'not permitted',
    'operation not permitted',
    'access denied',
    'authentication failure',
    'incorrect password',
    'is not in the sudoers',
    'sorry, user',
    'timed out',
    'timeout',
    'unexpected passwd output',
    'unknown status',
    'failed',
    'error',
)

# The status words `get_lock_status` produces on a successful read. A
# residual line that is one of these is a real status (OK), which is what
# keeps the parent's pill reading "Unlocked"/"Locked"/"No password" even
# when the probe also logged PAM noise.
STATUS_HUMAN_WORDS = (
    'locked',
    'unlocked',
    'no password',
    'unreachable',
)


def status_residual_lines(detail):
    """The detail's lines with the BENIGN noise lines dropped.

    Split on newlines, strip empties, and drop any line carrying a
    `STATUS_NOISE_PATTERNS` marker. Pure and total: never raises, whatever
    `detail` is (None, a number, a multi-kilobyte blob).
    """
    try:
        lines = str(detail if detail is not None else '').splitlines()
    except Exception:
        return []
    kept = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(pattern in low for pattern in STATUS_NOISE_PATTERNS):
            continue
        kept.append(line)
    return kept


def classify_status_detail(detail):
    """Classify one probe detail: OK / UNAVAILABLE / VERBATIM.

    The table, in order:
      1. noise lines dropped -> nothing left ......... UNAVAILABLE
         (pure PAM/systemd chatter with no usable status behind it: the
         probe told the parent nothing, so say so kindly)
      2. anything left carries a technical marker .... UNAVAILABLE
         (command not found, permission error, timeout, paths, cgroups)
      3. what is left is a known human status word .... OK
         (the normal pill, no warning box -- even when the probe also
         logged benign session noise)
      4. anything else ............................... VERBATIM
         (already-human product copy: passed through UNCHANGED)

    Pure function of the text: no I/O, no globals, no exception.
    """
    residual = status_residual_lines(detail)
    if not residual:
        return STATUS_UNAVAILABLE
    text = ' '.join(residual)
    low = text.lower()
    if any(pattern in low for pattern in STATUS_TECHNICAL_PATTERNS):
        return STATUS_UNAVAILABLE
    if any(low == word or low.startswith(word + ':')
           for word in STATUS_HUMAN_WORDS):
        return STATUS_OK
    return STATUS_VERBATIM


def status_display(detail, locked=_UNSET):
    """(parent_safe_text, klass) for one probe detail.

    This is the ONLY function that writes a `status_detail` value into a
    payload. Raw probe output can therefore not reach a rendered page for
    ANY input: OK details render the status word, UNAVAILABLE renders
    `STATUS_UNAVAILABLE_COPY`, VERBATIM renders the text unchanged
    because the table has already established it is non-technical.

    `locked` is the probe's other return value, and is consulted for exactly
    one degenerate case: the probe succeeded but produced no text at all.
    Then the status IS the answer and the parent should see it rather than
    the warning copy. Omit it for details with no probe behind them
    (remote rows, where `locked` may legitimately be None/unreachable).
    """
    residual = status_residual_lines(detail)
    klass = classify_status_detail(detail)
    if klass == STATUS_UNAVAILABLE:
        if not residual and locked is not _UNSET and locked is not None:
            # No text at all, but the probe told us a status: render it.
            return ('Locked' if locked else 'Unlocked'), STATUS_OK
        return STATUS_UNAVAILABLE_COPY, STATUS_UNAVAILABLE
    return ' '.join(residual), klass


# Dedup state for the admin audit hook: username -> (klass, raw detail).
# Process-local by design, exactly like budgets._LAST_AUTO_PAUSE_SIGS and
# budgets._LAST_SUMMARY_SIGS: the Overview polls every 10s, so without a
# "log only when it CHANGES" rule one broken account would append an
# identical line forever. The per-minute enforce timer process keeps its
# own fresh state and re-emits, so a persistent problem still surfaces in
# the journal; the long-running app thread stays quiet until the text
# changes.
_LAST_STATUS_PROBE_SIGS = {}


def audit_status_probe(username, klass, raw, state=None):
    """Audit one status-probe outcome that a parent must not see raw.

    Fires for UNAVAILABLE (the probe genuinely failed) and for OK-with-
    noise (the status is fine but the probe logged session chatter) --
    both carry text that belongs in the admin log, not on a card. A plain
    OK/VERBATIM outcome is not worth a line. Deduped to once per
    (username, class, raw) CHANGE, mirroring
    budgets._audit_auto_pause_failed's `state=None` injection so tests
    can pass their own map.
    """
    if klass not in (STATUS_UNAVAILABLE, STATUS_OK):
        return False
    noisy = any(pattern in str(raw if raw is not None else '').lower()
                for pattern in STATUS_NOISE_PATTERNS)
    if klass == STATUS_OK and not noisy:
        return False
    if state is None:
        state = _LAST_STATUS_PROBE_SIGS
    sig = (klass, raw)
    if state.get(username) == sig:
        return False
    state[username] = sig
    _safe_audit('system', 'account.status_probe', username,
                {'ok': klass == STATUS_OK, 'class': klass, 'raw': raw})
    return True


def status_row(username):
    """(locked, parent_safe_detail, status_ok) for one local account.

    The single payload-boundary helper: it probes once, sanitizes the
    detail, records the raw text in the admin audit log when the class
    demands it, and returns only parent-safe values. index(), api_users(),
    get_user_live() and time_rules() all build their rows from this, so
    no producer can forget the mapping.
    """
    locked, detail = get_lock_status(username)
    safe, klass = status_display(detail, locked)
    audit_status_probe(username, klass, detail)
    return locked, safe, klass != STATUS_UNAVAILABLE


def valid_username(username):
    """True when `username` is safe to pass as a literal usermod/loginctl
    argument.

    The rule lives in device_policy so the remote agent applies the exact
    same check on its own box (MULTIDEVICE_PLAN.md §4.4).
    """
    return device_policy.valid_username(username)


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


def hhmm_12h_parts(value):
    """'13:05' -> (1, 5, 'PM') — 12-hour <select> prefill for stored HH:MM.

    Inverse of the JS compose in templates/user.html: the bedtime form
    edits hour (1-12) / minute (00-59) / meridiem (AM/PM) selects and
    composes HH:MM back for the unchanged /api/bedtime contract. Edges:
    00:xx -> 12 AM, 12:xx -> 12 PM. Empty/unparseable input returns a
    safe default (12:00 AM) so the selects always render a value.
    """
    if not value:
        return (12, 0, 'AM')
    try:
        h, m = [int(x) for x in str(value).strip().split(':')[:2]]
    except (ValueError, TypeError):
        return (12, 0, 'AM')
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return (12, 0, 'AM')
    return (h % 12 or 12, m, 'AM' if h < 12 else 'PM')


def _twelve_parts(value):
    """Jinja-filter form of hhmm_12h_parts for the bedtime selects."""
    hour, minute, meridiem = hhmm_12h_parts(value)
    return {'hour': hour, 'minute': minute, 'meridiem': meridiem}


app.jinja_env.filters['twelve'] = format_12h
app.jinja_env.filters['twelve_parts'] = _twelve_parts
# Canonical duration display: minutes -> '1h 30m' (budgets/bonus/allowances).
app.jinja_env.filters['hm'] = budgets.format_hm


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
            # index=None -> tick_budgets builds the cached per-user
            # .desktop index, so the tick categorizes like the UI (FIX-2).
            summaries = budgets.tick_budgets(load_bedtime(), datetime.now(),
                                             notify=notify_user,
                                             account_locked=lambda u: get_lock_status(u)[0])
            # FIX-3 (TIME_TRACKING_QA.md F8): print the summaries this
            # thread used to discard — one line per user whose
            # note/froze/unfroze/degraded/skipped changed since last emit.
            budgets.emit_tick_summaries(summaries)
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
    The decision itself lives in device_policy (shared with the agent) so
    it is always made against the executing box's live wheel group.
    """
    refused = device_policy.wheel_refusal(username)
    if refused:
        return jsonify({'success': False, 'error': refused}), 403
    return None


def _form_bool(value, default=False):
    """Parse a form checkbox ('1'/'0'/'on'/absent) into a bool."""
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'on', 'yes')


def _refuse_protected_budget_target(username):
    """403 for a wheel/admin-protected target, else None.

    Shared by the override/window writers: a wheel member's config must
    be untouchable, matching the freeze/unfreeze endpoints.
    """
    refused = device_policy.budget_config_refusal(username)
    if refused:
        return jsonify({'success': False, 'error': refused}), 403
    return None


def _refuse_frozen_target(username):
    """403 for a wheel/admin-protected freeze target, else None."""
    refused = device_policy.freeze_refusal(username)
    if refused:
        return jsonify({'success': False, 'error': refused}), 403
    return None


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
    try:
        result = subprocess.run(
            ['sudo', 'usermod', '-L', username],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            msg = 'usermod -L: ' + (result.stderr.strip() or 'failed')
            _safe_audit(_req_actor(), 'account.lock', username,
                        {'success': False, 'message': msg, 'via': via})
            return False, msg
    except Exception as e:
        msg = 'usermod -L error: ' + str(e)
        _safe_audit(_req_actor(), 'account.lock', username,
                    {'success': False, 'message': msg, 'via': via})
        return False, msg

    # A manual lock takes ownership away from bedtime so the bedtime
    # timer cannot later auto-unlock it (see note_manual_lock).
    note_manual_lock(username)

    # Lock the screen without terminating the session.
    screen_msg = lock_user_sessions(username)

    _safe_audit(_req_actor(), 'account.lock', username,
                {'success': True, 'message': f'OK ({screen_msg})', 'via': via})
    return True, f'Account locked and screen locked ({screen_msg})'


def _unlock_account(username):
    """Unlock the account and hand ownership to the manual unlock."""
    success, message = run_lock_command(username, 'unlock')
    if success:
        # Manual unlock takes precedence over bedtime (see note_manual_unlock).
        note_manual_unlock(username)
    return success, message


def _emergency_logout(username):
    """Lock the account and terminate ALL its sessions immediately."""
    try:
        result = subprocess.run(
            ['sudo', 'usermod', '-L', username],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            msg = 'usermod -L: ' + (result.stderr.strip() or 'failed')
            _safe_audit(_req_actor(), 'account.logout', username,
                        {'success': False, 'message': msg})
            return False, msg
    except Exception as e:
        msg = 'usermod -L error: ' + str(e)
        _safe_audit(_req_actor(), 'account.logout', username,
                    {'success': False, 'message': msg})
        return False, msg

    # A manual lock takes ownership away from bedtime so the bedtime
    # timer cannot later auto-unlock it (see note_manual_lock).
    note_manual_lock(username)

    try:
        result = subprocess.run(
            ['sudo', 'loginctl', 'terminate-user', username],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            msg = ('loginctl terminate-user: '
                   + (result.stderr.strip() or 'failed'))
            _safe_audit(_req_actor(), 'account.logout', username,
                        {'success': False, 'message': msg})
            return False, msg
    except Exception as e:
        msg = 'loginctl terminate-user error: ' + str(e)
        _safe_audit(_req_actor(), 'account.logout', username,
                    {'success': False, 'message': msg})
        return False, msg

    _safe_audit(_req_actor(), 'account.logout', username, {'success': True})
    return True, 'Session terminated. User logged out.'


# ---------------------------------------------------------------------------
# Multi-device (MULTIDEVICE_PLAN.md Phase 1): hub -> agent routing
# ---------------------------------------------------------------------------

def _device_entries():
    """Token-free device list for the UI. [] when the store is unreadable."""
    try:
        return [remote.describe_device(d) for d in remote.list_devices()]
    except remote.DevicesError as e:
        print(f'DEVICES FAIL-CLOSED: {e}', flush=True)
        return []


def _remote_proxy(entry, method, subpath, params=None):
    """Thin seam over remote.proxy so tests can stub the transport.

    Returns (status, payload, token_rejected). Raises remote.RemoteError on
    a transport failure; callers must turn that into a visible error, never
    a silent success.
    """
    return remote.proxy(entry, method, subpath, params=params,
                        panel_actor=_req_actor())


def _sanitize_remote_status(row):
    """Re-run the status mapping over ONE remote row's status_detail.

    The agent applies `status_display` on its own box, but the hub must not
    TRUST that: an agent predating this fix would ship raw `sudo:`/PAM text
    straight into the Overview cards. Running the same pure mapping here
    makes the parent-facing contract a property of the HUB, not of which
    agent build happens to be registered. Non-dict rows pass through
    untouched (the payload contract tests cover the shape).
    """
    if not isinstance(row, dict) or 'status_detail' not in row:
        return row
    safe, klass = status_display(row.get('status_detail'),
                                 row.get('locked', _UNSET))
    row['status_detail'] = safe
    row.setdefault('status_ok', klass != STATUS_UNAVAILABLE)
    return row


def _remote_users(entry):
    """Return (users, error). users is None on failure (fail visible).

    Each row's `status_detail` is re-sanitized (see
    `_sanitize_remote_status`) so an older agent cannot leak raw system
    text into a parent-facing card.
    """
    status, payload, rejected = _remote_proxy(entry, 'GET', '/users')
    if rejected:
        return None, f'{entry.get("name")}: token rejected — re-register'
    if status != 200 or not isinstance(payload, list):
        return None, (f'{entry.get("name")}: '
                      f'{payload.get("error") or "unexpected response"}')
    return [_sanitize_remote_status(row) for row in payload], None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _admin_store_names(store):
    """Usernames with a dict row in the auth/admin store.

    This is the adminstore half of the admin-account predicate (the other
    half is live wheel membership, ``auth.is_wheel_member``). Built from the
    same rows ``_admin_rows()`` lists on the admin console, so the Overview
    hides exactly the accounts the console shows.
    """
    return {name for name, row in store.get('users', {}).items()
            if isinstance(row, dict)}


def _load_admin_store():
    """Auth store for the admin predicate, fail-soft to an empty store.

    An unreadable store means no *app-only* admins are known; wheel members
    are still filtered from the live group. Mirrors index()'s historical
    behaviour (empty admin panel on failure), never a hard 500 on a poll.
    """
    try:
        return auth.load_store()
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return {'users': {}}


def _is_admin_account(username, store_names):
    """THE admin-account predicate — one definition, shared by every path.

    `index()` and `api_users()` filter Overview rows with exactly this call
    (parity is the whole point: first paint and the 10s JS rebuild must drop
    the same usernames). The remote rows carry the same decision precomputed
    as their `wheel`/`admin` flags; the hub filters those consumer-side.
    """
    return auth.is_wheel_member(username) or username in store_names


@app.route('/')
@require_admin
def index():
    """Render the main page with user list (optionally a remote device).

    Admin accounts (wheel members + admin-store rows) are HIDDEN here
    (owner decision 2026-10-18): the Overview is the children board. The
    admin console at /admin/audit lists and manages them instead.
    """
    users = get_human_users()
    bedtime = load_bedtime()
    store_names = _admin_store_names(_load_admin_store())
    user_data = []
    for u in users:
        # Same predicate as api_users(): admins never reach the cards, the
        # summary counters, or the rebuild list.
        if _is_admin_account(u['username'], store_names):
            continue
        locked, detail, status_ok = status_row(u['username'])
        entry = bedtime.get(u['username'], {})
        bonus = bonus_state(entry)
        user_data.append({
            'username': u['username'],
            'uid': u['uid'],
            'home': u['home'],
            'shell': u['shell'],
            'locked': locked,
            'status_detail': detail,
            'status_ok': status_ok,
            'bedtime': entry,
            'in_bedtime': is_in_bedtime(entry),
            'bonus': bonus,
            # Live countdown for the account card (same value as the per-user
            # Time rules page — both built on countdown_state).
            'countdown': countdown_state({'bonus': bonus, 'bedtime': entry}),
            # Wheel admins are system-protected: the UI disables their
            # lock/unlock/logout actions and shows a Protected pill. The
            # endpoints refuse them anyway (defence in depth). Since the
            # Overview filter (2026-10-18) these rows never carry a wheel
            # member, but the flags stay on the payload: accountHtml()'s
            # wheel branch remains as defence and remote rows carry them.
            'wheel': auth.is_wheel_member(u['username']),
            # App-only admin (admin-store member). Filtered out above, so
            # always False here; kept as a payload-contract field (see
            # api_users()).
            'admin': u['username'] in store_names,
        })
    devices = _device_entries()
    selected_device = (request.args.get('device', '') or '').strip()
    device_error = None
    if selected_device:
        device_entry = remote.get_device(selected_device)
        if device_entry is None:
            device_error = f'Unknown device: {selected_device}'
            user_data = []
        else:
            try:
                remote_users, device_error = _remote_users(device_entry)
            except remote.RemoteError as e:
                remote_users, device_error = None, str(e)
            user_data = remote_users if remote_users is not None else []
            # Consumer-side admin filter for remote rows: the agent still
            # REPORTS its wheel/admin accounts (never change what a device
            # says about its children), the hub just drops those rows before
            # render. Same predicate the local path applies, precomputed on
            # the payload. Old agent payloads without the keys keep their
            # rows (flag absent is not a signal to hide an account).
            user_data = [r for r in user_data
                         if not (r.get('wheel') or r.get('admin'))]
    # Remote rows otherwise pass through untouched: the Overview's own 10s
    # poll re-fetches them from the agent via the device proxy, so a
    # countdown invented here for the first paint would vanish on the first
    # rebuild. `index.html` (and accountHtml) render the countdown only when
    # the row carries a `countdown` key; a remote row that lacks it (the
    # agent's /users payload does not include one yet) simply shows no
    # countdown — degrade, never crash. Local rows always get one just above.
    return render_template('index.html', users=user_data, day_labels=DAY_LABELS,
                           devices=devices, selected_device=selected_device,
                           device_error=device_error)


# ---------------------------------------------------------------------------
# Overview action forms (no-JS path)
#
# The Overview's lock / unlock account actions are plain
# <form method="post"> submissions, so every action works with JavaScript
# disabled. Each route runs the same core as its /api/* twin, flashes the
# outcome and redirects back to the Overview (POST/Redirect/GET).
# `static/js/app.js` + the page script only add the confirmation dialog on
# top; they never replace the server round-trip.
#
# The admin add/remove forms moved to the Admin console (/admin/audit) on
# 2026-10-18 (owner decision: admins are hidden from the Overview). Their
# route PATHS stay /overview/admins[/remove] — a pinned endpoint contract —
# but they redirect back to the console. See UI_UX_PLAN.md §7.4.
# ---------------------------------------------------------------------------

def _overview_redirect():
    """Back to the Overview, preserving a selected remote device view."""
    device = (request.form.get('device', '') or '').strip()
    if device:
        return redirect(url_for('index', device=device))
    return redirect(url_for('index'))


def _remote_overview_action(subpath):
    """Run an Overview action on the selected remote device.

    Returns (ok, message). Mirrors /api/device/<name>/v1/<subpath> so the no-JS
    form path reaches a remote box exactly like the JSON/fetch path, with the
    hub session + CSRF still guarding the browser side.
    """
    device = (request.form.get('device', '') or '').strip()
    entry = remote.get_device(device)
    if entry is None:
        return False, f'Unknown device: {device}'
    params = {k: v for k, v in request.form.items()
              if k not in ('csrf_token', 'device')}
    try:
        status, payload, rejected = _remote_proxy(
            entry, 'POST', '/' + subpath, params)
    except remote.RemoteError as e:
        _safe_audit(_req_actor(), 'device.command', device,
                    {'success': False, 'path': subpath, 'error': str(e)})
        return False, f'Device {device} unavailable: {e}'
    if rejected:
        return False, f'{device}: token rejected — re-register'
    if isinstance(payload, dict):
        ok = bool(payload.get('success'))
        msg = (payload.get('message') or payload.get('error')
               or ('Done' if ok else 'Action failed'))
    else:
        ok = 200 <= status < 300
        msg = 'Done' if ok else f'Device {device} returned {status}'
    _safe_audit(_req_actor(), 'device.command', device,
                {'success': ok, 'path': subpath, 'status': status,
                 'via': 'hub'})
    return ok, msg


def _overview_account_action(core, subpath, success_text, refuse_wheel=True):
    """Shared body for the two no-JS Overview account action routes.

    `core` is the same function the JSON endpoint uses; `subpath` is the
    remote agent path when a device is selected. Always flashes and redirects.
    """
    device = (request.form.get('device', '') or '').strip()
    username = request.form.get('username', '').strip()
    if device:
        ok, message = _remote_overview_action(subpath)
        flash(f'{device}: {message}' if ok else message,
              'ok' if ok else 'danger')
        return _overview_redirect()
    if not username:
        flash('No username provided', 'danger')
        return _overview_redirect()
    if not valid_username(username):
        flash('Invalid username', 'danger')
        return _overview_redirect()
    if refuse_wheel:
        # Wheel admins can never be locked / terminated — refused here, not
        # just in the UI (the same decision the JSON endpoint makes).
        refusal = device_policy.wheel_refusal(username)
        if refusal:
            flash(refusal, 'danger')
            return _overview_redirect()
    ok, message = core(username)
    flash(success_text.format(username=username) if ok else message,
          'ok' if ok else 'danger')
    return _overview_redirect()


@app.route('/overview/lock', methods=['POST'])
@require_admin
def overview_lock():
    """No-JS Overview form: lock the account + screen (PRG + flash)."""
    return _overview_account_action(
        lambda u: _lock_account_and_screen(u, 'overview.lock'),
        'lock', 'Locked {username} (account + screen).')


@app.route('/overview/unlock', methods=['POST'])
@require_admin
def overview_unlock():
    """No-JS Overview form: unlock the account (PRG + flash)."""
    return _overview_account_action(
        _unlock_account, 'unlock', 'Unlocked {username}.',
        refuse_wheel=False)


@app.route('/overview/admins', methods=['POST'])
@require_admin
def overview_admin_add():
    """No-JS Admin-console form: create an app-only admin (PRG + flash).

    The form lives on /admin/audit (the route path predates the move and is
    a pinned contract). The one-time generated password is flashed back so
    the wheel admin can copy it; it is shown ONCE, on the console the form
    redirects to, and never again.
    """
    username = request.form.get('username', '').strip()
    _status, payload = _add_app_admin(username)
    if payload.get('success'):
        flash(f'Admin {username} created. One-time password: '
              f'{payload.get("password")} — shown ONCE; copy it now.', 'ok')
    else:
        flash(payload.get('error') or 'Could not add admin.', 'danger')
    return redirect(url_for('admin_audit'))


@app.route('/overview/admins/remove', methods=['POST'])
@require_admin
def overview_admin_remove():
    """No-JS Admin-console form: remove an app-only admin (PRG + flash).

    Same contract note as overview_admin_add: the path is pinned, the form
    and the redirect live on /admin/audit.
    """
    username = request.form.get('username', '').strip()
    _status, payload = _remove_app_admin(username)
    if payload.get('success'):
        flash(payload.get('message') or f'Admin {username} removed.', 'ok')
    else:
        flash(payload.get('error') or 'Could not remove admin.', 'danger')
    return redirect(url_for('admin_audit'))


@app.route('/api/users')
@require_admin
def api_users():
    """JSON endpoint returning current child accounts with lock status.

    Filters admin accounts (wheel members + admin-store rows) with the
    EXACT predicate `index()` uses (`_is_admin_account`), so the first
    paint and the 10s JS rebuild never disagree about who is on the
    Overview. Remote-device rows are filtered consumer-side by the page
    script on the same `wheel`/`admin` payload flags.
    """
    users = get_human_users()
    bedtime = load_bedtime()
    store_names = _admin_store_names(_load_admin_store())
    user_data = []
    for u in users:
        if _is_admin_account(u['username'], store_names):
            continue
        locked, detail, status_ok = status_row(u['username'])
        entry = bedtime.get(u['username'], {})
        bonus = bonus_state(entry)
        user_data.append({
            'username': u['username'],
            'uid': u['uid'],
            'locked': locked,
            'status_detail': detail,
            'status_ok': status_ok,
            'bedtime': entry,
            'in_bedtime': is_in_bedtime(entry),
            'bonus': bonus,
            # Same countdown value as the server-rendered index.html cards and
            # the per-user Time rules page, so the 10s JS rebuild can never
            # disagree with the first paint.
            'countdown': countdown_state({'bonus': bonus, 'bedtime': entry}),
            # Wheel flag must mirror index(): accountHtml() branches on
            # `u.wheel` for the Protected pill, the "wheel admin" label and
            # the disabled actions. Without it the 10s JS rebuild renders
            # wheel admins as lockable children. Rows that survive the
            # admin filter never fire that branch — it stays as defence and
            # because remote rows carry the flag.
            'wheel': auth.is_wheel_member(u['username']),
            # Admin flag mirrors the agent payload's `admin` key so the
            # page script's filter reads one predicate shape for local and
            # remote rows alike (contract field + defence; filtered rows
            # never appear, so this is always False on surviving rows).
            'admin': u['username'] in store_names,
        })
    return jsonify(user_data)


@app.route('/api/lock', methods=['POST'])
@require_admin
def api_lock():
    """Lock a user account and screen without terminating the session.

    JSON endpoint (the Overview form path is /overview/lock). Runs:
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

    ok, msg = _lock_account_and_screen(username, 'api_lock')
    if not ok:
        return jsonify({'success': False, 'message': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/lock-screen', methods=['POST'])
@require_admin
def api_lock_screen():
    """Lock the user's screen and account without terminating the session.

    JSON endpoint with no Overview form twin: the Overview's single Lock
    button (POST /overview/lock) already locks the account *and* the screen,
    so the redundant form route was dropped. This endpoint — and the agent's
    /api/v1/lock-screen verb — are kept as documented API surface.
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

    ok, msg = _lock_account_and_screen(username, 'api_lock_screen')
    if not ok:
        return jsonify({'success': False, 'message': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/unlock', methods=['POST'])
@require_admin
def api_unlock():
    """Unlock a user account (JSON; the Overview form path is /overview/unlock)."""
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400

    success, message = _unlock_account(username)
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

    JSON endpoint (the per-user page posts here via fetch). Runs:
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

    ok, msg = _emergency_logout(username)
    if not ok:
        return jsonify({'success': False, 'message': msg}), 400
    return jsonify({'success': True, 'message': msg})


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


@app.route('/api/budgets/overrides', methods=['POST'])
@require_admin
def api_budgets_overrides():
    """Set/clear a per-app allowance or exemption.

    Form fields: username, comm, mode (limit|exempt|none), minutes.
    `mode=exempt` is unlimited; `mode=none`/absent or `minutes<=0`
    clears (and releases the app's freeze + thaws its group).
    """
    username = request.form.get('username', '').strip()
    comm = request.form.get('comm', '').strip()
    mode = request.form.get('mode', '').strip()
    minutes = request.form.get('minutes', '').strip()
    if not username:
        return jsonify({'success': False,
                        'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    refused = _refuse_protected_budget_target(username)
    if refused is not None:
        return refused
    if not comm:
        return jsonify({'success': False,
                        'error': 'No command provided'}), 400
    if not budgets.valid_comm(comm):
        return jsonify({'success': False,
                        'error': f'Invalid command: {comm}'}), 400
    ok, msg = budgets.set_app_override(
        username, comm, mode, minutes if minutes != '' else None,
        actor=_req_actor())
    if not ok:
        return jsonify({'success': False, 'error': msg}), 400
    return jsonify({'success': True, 'message': msg})


@app.route('/api/budgets/windows', methods=['POST'])
@require_admin
def api_budgets_windows():
    """Set/update/clear a recurring weekly window.

    Form fields: username, action (set|update|clear), target_type
    (category|app), target, days (csv 0-6), start, end (HH:MM),
    allowance_minutes, enabled (1|0), window_id (update/clear).
    """
    username = request.form.get('username', '').strip()
    action = (request.form.get('action', 'set').strip() or 'set')
    target_type = request.form.get('target_type', '').strip()
    target = request.form.get('target', '').strip()
    window_id = request.form.get('window_id', '').strip()
    if not username:
        return jsonify({'success': False,
                        'error': 'No username provided'}), 400
    if not valid_username(username):
        return jsonify({'success': False, 'error': 'Invalid username'}), 400
    refused = _refuse_protected_budget_target(username)
    if refused is not None:
        return refused

    if action == 'clear':
        ok, msg = budgets.clear_window(
            username, window_id=window_id or None,
            target_type=target_type or None, target=target or None,
            actor=_req_actor())
    elif action == 'update' or window_id:
        if not window_id:
            return jsonify({'success': False,
                            'error': 'window_id required'}), 400
        enabled = (_form_bool(request.form.get('enabled'))
                   if 'enabled' in request.form else None)
        ok, msg = budgets.update_window(
            username, window_id,
            days=request.form.get('days') or None,
            start=request.form.get('start') or None,
            end=request.form.get('end') or None,
            allowance_minutes=request.form.get('allowance_minutes') or None,
            enabled=enabled, actor=_req_actor())
    else:
        ok, msg = budgets.set_window(
            username, target_type, target,
            request.form.get('days', ''),
            request.form.get('start', '').strip(),
            request.form.get('end', '').strip(),
            request.form.get('allowance_minutes', '').strip(),
            enabled=_form_bool(request.form.get('enabled', '1'), default=True),
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
    refused = _refuse_frozen_target(username)
    if refused is not None:
        return refused
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
    refused = _refuse_frozen_target(username)
    if refused is not None:
        return refused
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


def _countdown_seconds(minutes):
    """Whole seconds for a minutes value; None when it is unusable."""
    if minutes is None:
        return None
    try:
        return max(0, int(round(float(minutes) * 60)))
    except (TypeError, ValueError):
        return None


def _format_seconds(seconds):
    """HH:MM:SS for a whole number of seconds (clamped at zero)."""
    total = max(0, int(seconds))
    return f'{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}'


def countdown_state(user):
    """Single source of truth for a live countdown value.

    ``user`` is a mapping carrying ``bonus`` (as produced by ``bonus_state``)
    and ``bedtime`` (the raw schedule entry) keys — the shape both the per-user
    Time rules page and the Overview account cards hold. Returns
    {'kind': 'bonus'|'bedtime', 'seconds': int} when there is something to
    count down to, else None (no active bonus and no bedtime schedule).

    The per-user ``_countdown_text`` and the Overview cards (server-rendered
    markup *and* the /api/users payload the JS rebuilds from) are all built on
    this, so the two screens can never disagree.
    """
    user = user or {}
    bonus = user.get('bonus') or {}
    if bonus.get('active') and bonus.get('remaining_minutes') is not None:
        seconds = _countdown_seconds(bonus.get('remaining_minutes'))
        if seconds is not None:
            return {'kind': 'bonus', 'seconds': seconds}
    entry = user.get('bedtime') or {}
    if entry.get('start'):
        seconds = _countdown_seconds(minutes_until_bedtime(entry))
        if seconds is not None:
            return {'kind': 'bedtime', 'seconds': seconds}
    return None


def _format_countdown(minutes):
    """HH:MM:SS for the live countdown card; '--:--:--' when unknown."""
    seconds = _countdown_seconds(minutes)
    if seconds is None:
        return '--:--:--'
    return _format_seconds(seconds)


def _countdown_text(user):
    """Countdown for the Time rules screen (design Screen 2).

    Bonus time remaining when a bonus is active, otherwise the time until the
    next bedtime window; '--:--:--' when there is nothing to count down.
    """
    state = countdown_state(user)
    if state is None:
        return '--:--:--'
    return _format_seconds(state['seconds'])


# Seconds -> HH:MM:SS for the Overview countdown chips (index.html).
app.jinja_env.filters['countdown'] = _format_seconds


@app.route('/user/<username>')
@require_admin
def user_detail(username):
    """Render the user details page for a specific user.

    With ?device=<name> the page is scoped to a registered box: the user
    detail is read from that box's agent and the page's own fetches are
    rewritten to the device proxy by the template's API shim.
    """
    selected_device = (request.args.get('device', '') or '').strip()
    devices = _device_entries()
    device_error = None
    if selected_device:
        entry = remote.get_device(selected_device)
        if entry is None:
            return 'Unknown device', 404
        try:
            payload, device_error = _remote_user_live(entry, username)
        except remote.RemoteError as e:
            payload, device_error = None, str(e)
        if payload is None:
            data = _empty_remote_live(username)
        else:
            data = _remote_live_context(username, payload)
    else:
        data = get_user_live(username)
        if data is None:
            # Username not found
            return "User not found", 404

    return render_template('user.html', username=username,
                           user=data['user'],
                           processes=data['processes'], visible=data['visible'],
                           hidden=data['hidden'], groups=data['groups'],
                           budget_state=data['budget_state'],
                           overrides=data.get('overrides'),
                           app_names=data.get('app_names') or [],
                           children=data.get('children') or [],
                           countdown_text=_countdown_text(data['user']),
                           countdown=countdown_state(data['user']),
                           category_labels=budgets.CATEGORY_LABELS,
                           categories=budgets.CATEGORIES,
                           day_labels=DAY_LABELS,
                           devices=devices, selected_device=selected_device,
                           device_error=device_error)


def _remote_user_live(entry, username):
    """(payload, error) for one remote user's live data (fail visible)."""
    if not valid_username(username):
        return None, 'Invalid username'
    status, payload, rejected = _remote_proxy(
        entry, 'GET', f'/user/{username}/live')
    if rejected:
        return None, f'{entry.get("name")}: token rejected — re-register'
    if status != 200 or not payload.get('success'):
        return None, (f'{entry.get("name")}: '
                      f'{payload.get("error") or "unexpected response"}')
    return payload, None


def _remote_live_context(username, payload):
    """Adapt an agent /user/<u>/live payload to the user.html context."""
    return {
        'user': {
            'username': username,
            'uid': payload.get('uid', ''),
            'home': payload.get('home', ''),
            'shell': payload.get('shell', ''),
            'locked': payload.get('locked'),
            # Same re-sanitizing as the Overview rows: a remote box running
            # an older agent must not be able to put raw sudo/PAM text on
            # this child's Time rules page.
            'status_detail': _sanitize_remote_status({
                'status_detail': payload.get('status_detail'),
                'locked': payload.get('locked'),
            }).get('status_detail'),
            'bedtime': payload.get('bedtime') or {},
            'in_bedtime': payload.get('in_bedtime'),
            'bonus': payload.get('bonus') or {},
        },
        'processes': payload.get('processes') or [],
        'visible': payload.get('visible') or [],
        'hidden': payload.get('hidden') or [],
        'groups': payload.get('groups') or {c: [] for c in budgets.CATEGORIES},
        'budget_state': payload.get('budget')
        or budgets.budget_status(username, {},
                                 overrides={'app_overrides': {},
                                            'windows': []}),
        'overrides': payload.get('overrides')
        or {'app_overrides': {}, 'windows': []},
        'app_names': payload.get('app_names') or [],
    }


def _empty_remote_live(username):
    """Empty context for an unreachable device (no stale data, fail visible)."""
    return {
        'user': {'username': username, 'uid': '', 'home': '', 'shell': '',
                 'locked': None, 'status_detail': 'unreachable',
                 'bedtime': {}, 'in_bedtime': False, 'bonus': {}},
        'processes': [], 'visible': [], 'hidden': [],
        'groups': {c: [] for c in budgets.CATEGORIES},
        'budget_state': budgets.budget_status(
            username, {}, overrides={'app_overrides': {}, 'windows': []}),
        'overrides': {'app_overrides': {}, 'windows': []},
        'app_names': [],
    }


def get_user_live(username):
    """Gather live data for the details page and the live-refresh endpoint.

    Returns None when the username is not a human user on this system.
    """
    users = get_human_users()
    bedtime = load_bedtime()
    user_data = []
    for u in users:
        if u['username'] == username:
            locked, detail, status_ok = status_row(u['username'])
            entry = bedtime.get(u['username'], {})
            user_data.append({
                'username': u['username'],
                'uid': u['uid'],
                'home': u['home'],
                'shell': u['shell'],
                'locked': locked,
                'status_detail': detail,
                'status_ok': status_ok,
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
        # Installed-app comms for the "add app override" datalist (covers
        # apps not currently running).
        app_names = sorted(names)
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
        app_names = []
    overrides_data = budgets.load_overrides()
    user_overrides = overrides_data.get(username)
    budget_state = budgets.budget_status(username, overrides=user_overrides)

    # Processes show billable USAGE since the last reset -- the same counter
    # the budget rows show -- never the process's ps ETIME wall-clock age.
    # Visible groups are single-category (bucketed by category BEFORE
    # grouping): the owning category's used_minutes apply unless the comm
    # carries an app override. Hidden groups can span categories and have
    # no single usage value, so they stay '' (rendered as an em dash).
    for cat, cat_groups in groups.items():
        for g in cat_groups:
            mins = budgets.group_usage_minutes(cat, g['comm'], budget_state)
            g['used_minutes'] = mins
            g['used'] = budgets.format_hm(mins) if mins is not None else ''
    for g in hidden:
        g['used_minutes'] = None
        g['used'] = ''

    return {
        'user': user_data[0],
        'processes': processes,
        'visible': visible,
        'hidden': hidden,
        'groups': groups,
        'budget_state': budget_state,
        'overrides': budgets.overrides_view(username, overrides_data),
        'app_names': app_names,
        # Human accounts on this box, for the per-child tabs on the Time rules
        # screen (design Screen 2). Computed here so no route re-reads the
        # passwd database.
        'children': [u['username'] for u in users],
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
               'used': g.get('used') or '',
               'members': [
                   {'pid': m['pid'], 'comm': m['comm'],
                    'display_name': m.get('display_name') or m.get('comm', '')}
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
              'category': m.get('category')}
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
        # Same countdown the first paint rendered (countdown_state), so the
        # 10s poll re-syncs the live ticker instead of drifting.
        'countdown': countdown_state(user),
        'groups': slim_groups,
        'hidden': slim_hidden,
        'error': error,
        'budget': data['budget_state'],
        'overrides': data.get('overrides')
        or {'app_overrides': {}, 'windows': []},
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


def _admin_rows(store):
    """Admin rows with live tier resolution.

    Shared by the JSON list endpoint and the server-rendered Admin-console
    table, so both show exactly the same accounts. `system_user` tells the
    console which rows have a real account behind them (only those get a
    Details link to the per-user page; app-only admins have no system
    account, so /user/<name> would 404 for them).
    """
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
    return admins


def _add_app_admin(username):
    """Create an app-only admin. Returns (status_code, payload_dict).

    Wheel actors only; wheel targets and non-wheel system accounts are
    refused (delegated to adminstore, which re-checks live). The password
    is generated server-side and returned ONCE in the payload.
    """
    if g.tier != 'wheel':
        return 403, {'success': False,
                     'error': 'Only wheel admins can add admins.'}
    if not auth.valid_admin_username(username):
        return 400, {'success': False, 'error': 'Invalid username.'}
    try:
        store = auth.load_store()
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return 500, {'success': False, 'error': 'Authentication unavailable'}
    try:
        tier, generated = adminstore.add_admin(
            username, store=store, allow_wheel=False)
    except adminstore.RefusedError as e:
        return 403, {'success': False, 'error': str(e)}
    except adminstore.AdminExistsError:
        return 400, {'success': False, 'error': 'Admin already exists.'}
    except adminstore.AuthStoreUnavailable as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return 500, {'success': False, 'error': 'Authentication unavailable'}
    _safe_audit(_req_actor(), 'admin.add', username,
                {'success': True, 'tier': tier})
    return 200, {'success': True,
                 'message': f'Admin {username} created. This password is '
                            'shown ONCE and must be changed at first login.',
                 'password': generated}


def _remove_app_admin(username):
    """Remove an app-only admin. Returns (status_code, payload_dict).

    Wheel actors only; wheel targets are never removable.
    """
    if g.tier != 'wheel':
        return 403, {'success': False,
                     'error': 'Only wheel admins can remove admins.'}
    if not auth.valid_admin_username(username):
        return 400, {'success': False, 'error': 'Invalid username.'}
    refusal = device_policy.wheel_refusal(username)
    if refusal:
        return 403, {'success': False, 'error': refusal}
    try:
        store = auth.load_store()
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return 500, {'success': False, 'error': 'Authentication unavailable'}
    try:
        # Re-checks wheel membership live after loading the store
        # (belt and braces), then deletes + persists.
        adminstore.remove_admin(username, store=store)
    except adminstore.RefusedError as e:
        return 403, {'success': False, 'error': str(e)}
    except adminstore.NoSuchAdminError:
        return 404, {'success': False, 'error': 'No such admin.'}
    except adminstore.AuthStoreUnavailable as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return 500, {'success': False, 'error': 'Authentication unavailable'}
    _safe_audit(_req_actor(), 'admin.remove', username, {'success': True})
    return 200, {'success': True, 'message': f'Admin {username} removed.'}


@app.route('/api/admins', methods=['GET'])
@require_admin
def api_admins_list():
    """List admin accounts with live tier resolution (JSON)."""
    try:
        store = auth.load_store()
    except auth.AuthStoreError as e:
        print(f'AUTH FAIL-CLOSED: {e}', flush=True)
        return jsonify({'success': False,
                        'error': 'Authentication unavailable'}), 500
    return jsonify({'success': True, 'admins': _admin_rows(store),
                    'self': g.admin, 'tier': g.tier})


@app.route('/api/admins', methods=['POST'])
@require_admin
def api_admins_add():
    """Create an app-only admin account. Wheel admins only (JSON).

    The web console only ever adds app-only admins: a wheel member's
    row is bootstrap/CLI territory (mirrors the CLI constraints, but the
    UI refuses wheel targets outright — wheel admins are never addable
    from the UI). Refuses non-wheel system accounts: that would hand a
    login to a regular local user (e.g. the child). The password is
    generated server-side and returned ONCE in the response.
    """
    username = request.form.get('username', '').strip()
    status, payload = _add_app_admin(username)
    return jsonify(payload), status


@app.route('/api/admins/remove', methods=['POST'])
@require_admin
def api_admins_remove():
    """Remove an app-only admin. Wheel admins only; wheel targets never (JSON)."""
    username = request.form.get('username', '').strip()
    status, payload = _remove_app_admin(username)
    return jsonify(payload), status


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


# ---------------------------------------------------------------------------
# Device endpoints (hub). The human authenticates with a session here; the
# *box* is addressed by the token stored in devices.json. Bearer tokens are
# forwarded only to the target agent, never to the browser, never logged.
# ---------------------------------------------------------------------------

@app.route('/api/devices')
@require_admin
def api_devices():
    """List registered devices with reachability.

    Feature gate: with no devices.json this reports ``multi_device: false``
    and an empty list — identical to single-box behavior.
    """
    enabled = remote.devices_enabled()
    devices = []
    error = None
    try:
        entries = remote.list_devices()
    except remote.DevicesError as e:
        entries = []
        error = str(e)
    for entry in entries:
        info = remote.describe_device(entry)
        online, reach_error = False, None
        try:
            status, payload, rejected = _remote_proxy(entry, 'GET', '/health')
            if rejected:
                reach_error = 'token rejected — re-register'
            elif status == 200 and payload.get('success'):
                online = True
                info['box_id'] = payload.get('box_id')
                info['version'] = payload.get('version')
            else:
                reach_error = payload.get('error') or 'unexpected response'
        except remote.RemoteError as e:
            reach_error = str(e)
        info['online'] = online
        info['error'] = reach_error
        devices.append(info)
    return jsonify({'success': True, 'multi_device': enabled,
                    'devices': devices, 'error': error})


@app.route('/api/device/<name>/v1/<path:subpath>',
           methods=['GET', 'POST'])
@require_admin
def api_device_proxy(name, subpath):
    """Forward an admin request to a registered box's agent.

    The hub session + CSRF guard the browser side (unchanged); the agent
    link is bearer-token authenticated with no CSRF. Unreachable targets
    fail with a clear 502 — never a silent success. The dispatch is audited
    on the hub; the agent audits the action on its own chain.
    """
    entry = remote.get_device(name)
    if entry is None:
        return jsonify({'success': False,
                        'error': f'Unknown device: {name}'}), 404
    params = request.form.to_dict() or request.args.to_dict()
    try:
        status, payload, rejected = _remote_proxy(entry, request.method,
                                                  '/' + subpath, params)
    except remote.RemoteError as e:
        _safe_audit(_req_actor(), 'device.command', name,
                    {'success': False, 'path': subpath, 'error': str(e)})
        return jsonify({'success': False,
                        'error': f'Device {name} unavailable: {e}'}), 502
    if isinstance(payload, dict):
        success = bool(payload.get('success'))
    else:
        success = 200 <= status < 300
    _safe_audit(_req_actor(), 'device.command', name,
                {'success': success, 'path': subpath,
                 'status': status, 'via': 'hub'})
    response = jsonify(payload)
    if rejected:
        response.status_code = 401
        return response
    response.status_code = status if 200 <= status < 600 else 502
    return response


@app.route('/admin/audit')
@require_admin
def admin_audit():
    """Admin console: tamper-evident audit viewer + admin management.

    Read-only audit table (newest-first) with chain status and a
    limit-based "load more" control (the button grows the limit via the
    existing /api/audit endpoint, capped at 1000 — nothing is deletable).
    The admin-management section is the ONE admin UI (moved here from the
    Overview, 2026-10-18): it lists every store row (wheel rows are shown
    as protected) and owns the add/remove controls. Those are plain
    server-rendered POST forms to /overview/admins[/remove] (pinned paths)
    that redirect back here with the flash; the page script only adds the
    confirmation dialog on remove. Wheel-actor only, targeting app-only
    admins only, enforced server-side by adminstore.add_admin/remove_admin.
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
        # One builder for the console table and the JSON list endpoint
        # (the Overview no longer renders admin rows at all).
        admins = _admin_rows(store)
    # Per-entry integrity (design Screen 5): every entry at or before the
    # first bad link is verified when the chain is intact; otherwise only
    # entries whose seq precedes the first bad seq are trusted.
    verified_count = 0
    for e in entries:
        e['verified'] = bool(chain_ok) or (
            bad_seq is not None and e.get('seq', 0) < bad_seq)
        if e['verified']:
            verified_count += 1
    return render_template('admin_audit.html', entries=entries,
                           chain_ok=chain_ok, bad_seq=bad_seq,
                           limit=limit, has_more=has_more,
                           admins=admins,
                           verified_count=verified_count,
                           entry_count=len(entries))


@app.route('/time-rules')
@require_admin
def time_rules():
    """Time rules section home: pick a child, then edit their rules.

    Overview of every human account with its live lock/bedtime state; each
    card links into the per-child Time rules screen (``/user/<username>``).
    """
    users = get_human_users()
    bedtime = load_bedtime()
    children = []
    for u in users:
        locked, detail, status_ok = status_row(u['username'])
        entry = bedtime.get(u['username'], {})
        children.append({
            'username': u['username'],
            'uid': u['uid'],
            'locked': locked,
            'status_detail': detail,
            'status_ok': status_ok,
            'bedtime': entry,
            'in_bedtime': is_in_bedtime(entry),
            'bonus': bonus_state(entry),
        })
    return render_template('time_rules.html', children=children,
                           day_labels=DAY_LABELS)


def _pick_child(requested):
    """(username, live_data|None) — the requested child, else the first one.

    ``live_data`` is None when there is no human account to show.
    """
    users = get_human_users()
    names = [u['username'] for u in users]
    if requested and requested in names:
        return requested, get_user_live(requested)
    if names:
        return names[0], get_user_live(names[0])
    return '', None


@app.route('/budgets', endpoint='budgets')
@require_admin
def time_budgets():
    """Time budgets section (design-exports/SPEC.md Screen 3).

    The same live budget data the per-child page renders, presented as the
    standalone budgets screen with a user picker. Read-only here: every edit
    posts to the existing /api/budgets* endpoints.
    """
    users = get_human_users()
    requested = (request.args.get('user', '') or '').strip()
    username, data = _pick_child(requested)
    if data is None:
        return render_template(
            'budgets.html', username=username, users=users,
            budget_state={}, overrides=None, app_names=[],
            categories=budgets.CATEGORIES,
            category_labels=budgets.CATEGORY_LABELS, day_labels=DAY_LABELS)
    return render_template(
        'budgets.html', username=username, users=users,
        budget_state=data['budget_state'], overrides=data.get('overrides'),
        app_names=data.get('app_names') or [],
        categories=budgets.CATEGORIES,
        category_labels=budgets.CATEGORY_LABELS, day_labels=DAY_LABELS)


@app.route('/processes')
@require_admin
def processes():
    """Processes section (design-exports/SPEC.md Screen 4).

    Grouped, per-PID expandable rows for one child, with the Visible/Hidden/All
    tabs and the shared override dialog. Read-only server render; hide/show,
    freeze and overrides post to the existing endpoints.
    """
    users = get_human_users()
    requested = (request.args.get('user', '') or '').strip()
    username, data = _pick_child(requested)
    if data is None:
        return render_template(
            'processes.html', username=username, users=users,
            groups={}, visible=[], hidden=[], budget_state={}, overrides=None,
            app_names=[], categories=budgets.CATEGORIES,
            category_labels=budgets.CATEGORY_LABELS, day_labels=DAY_LABELS)
    return render_template(
        'processes.html', username=username, users=users,
        groups=data['groups'], visible=data['visible'], hidden=data['hidden'],
        budget_state=data['budget_state'], overrides=data.get('overrides'),
        app_names=data.get('app_names') or [],
        categories=budgets.CATEGORIES,
        category_labels=budgets.CATEGORY_LABELS, day_labels=DAY_LABELS)


@app.route('/settings')
@require_admin
def settings():
    """Settings section (design-exports/SPEC.md Screen 6).

    Renders the notification / local-access / devices panels. Features the
    backend does not own yet (notification lead-time persistence, multi-device)
    render as clearly-labelled disabled placeholders, never invented state.
    """
    devices = []
    devices_error = None
    try:
        devices = _device_entries()
    except Exception as e:  # pragma: no cover - corrupt store is reported
        devices_error = str(e)
    return render_template('settings.html', devices=devices,
                           devices_error=devices_error)


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
