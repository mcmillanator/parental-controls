#!/usr/bin/env python3
"""MVP authentication & authorization for Parental Controls.

Design: AUTH_DESIGN.md (binding). Summary of the model:

- Two admin tiers, both with **app passwords only**. No PAM, no setuid
  helper, no system-password auth — in any version.
- Wheel admins: row in ``auth.json`` AND live membership in the system
  ``wheel`` group (re-checked on every request).
- App-only admins: row in ``auth.json`` for a username with **no**
  corresponding system account. A row for a non-wheel system account
  (e.g. the child) can never authenticate.
- Wheel admins can never be locked or removed (enforced here + in handlers).
- ``argon2`` hashes; fail closed when the backend or store is unavailable.
- Server-side sessions: the cookie carries only an opaque session ID.
  Idle 30 min / absolute 12 h, measured with a monotonic clock.
"""

import grp
import json
import os
import pwd
import secrets
import tempfile
import threading
import time

# ---------------------------------------------------------------------------
# Password hashing backend: argon2-cffi, else fail closed. There is no
# plaintext fallback and no weak-hash fallback — startup refuses to run.
# ---------------------------------------------------------------------------

try:
    from argon2 import PasswordHasher as _PasswordHasher
    from argon2.exceptions import VerifyMismatchError as _VerifyMismatchError
    _HASHER = _PasswordHasher()
    _HASH_BACKEND = 'argon2-cffi'
except ImportError:
    _HASHER = None
    _VerifyMismatchError = Exception
    _HASH_BACKEND = None


def ensure_hash_backend():
    """Fail closed when no password-hashing backend is installed."""
    if _HASHER is None:
        raise SystemExit(
            'FATAL: no password hashing backend installed '
            '(need argon2-cffi). Refusing to start rather than run '
            'without secure credential verification.')


def hash_password(password):
    ensure_hash_backend()
    return _HASHER.hash(password)


def verify_password(stored_hash, password):
    """True on match, False on mismatch. Never raises on bad input."""
    if _HASHER is None or not stored_hash or not password:
        return False
    try:
        return _HASHER.verify(stored_hash, password)
    except _VerifyMismatchError:
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Auth store (auth.json)
# ---------------------------------------------------------------------------

DEFAULT_AUTH_FILE = '/etc/parental-controls/auth.json'


def auth_file_path():
    return os.environ.get('PARENTAL_CONTROLS_AUTH', DEFAULT_AUTH_FILE)


class AuthStoreError(Exception):
    pass


def blank_store():
    return {'users': {}}


def load_store(path=None):
    """Load the auth store. Raises AuthStoreError when missing/corrupt."""
    path = path or auth_file_path()
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        raise AuthStoreError(f'auth store missing: {path}')
    except (json.JSONDecodeError, OSError) as e:
        raise AuthStoreError(f'auth store unreadable ({path}): {e}')
    if not isinstance(data, dict) or not isinstance(
            data.get('users'), dict):
        raise AuthStoreError(f'auth store corrupt ({path}): bad shape')
    return data


def save_store(store, path=None):
    """Atomic write (temp + rename), mode 0600. Raises AuthStoreError."""
    path = path or auth_file_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent or '.', prefix='.auth-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(store, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError) as e:
        raise AuthStoreError(f'auth store write failed ({path}): {e}')


def init_store(path=None):
    """Fail-closed startup check.

    - Missing file: first run -> bootstrap a credential for the invoking
      admin (only when they are a wheel member), print it once, flag
      must_change. When the invoking user is not a wheel member, create
      an empty store and log the CLI command to add an admin.
    - Corrupt file or unwritable location: raise AuthStoreError (the
      caller refuses to start).
    Returns (store, bootstrap_password_or_None).
    """
    path = path or auth_file_path()
    try:
        store = load_store(path)
    except AuthStoreError:
        if os.path.exists(path):
            raise  # corrupt/unreadable: fail closed, never bootstrap over it
        store = blank_store()
        invoker = None
        try:
            invoker = pwd.getpwuid(os.geteuid()).pw_name
        except (KeyError, OSError):
            invoker = None
        bootstrap_pw = None
        if invoker and is_wheel_member(invoker):
            bootstrap_pw = secrets.token_urlsafe(24)
            store['users'][invoker] = {
                'hash': hash_password(bootstrap_pw),
                'must_change': True,
                'locked': False,
            }
            save_store(store, path)  # raises -> fail closed
            print(f'AUTH BOOTSTRAP: initial app password for {invoker!r}: '
                  f'{bootstrap_pw}', flush=True)
            print('This is printed ONCE. It must be changed at first login.',
                  flush=True)
        else:
            save_store(store, path)
            print('AUTH BOOTSTRAP: empty store created. Add an admin with:',
                  flush=True)
            print('  sudo parental-controls add-admin <username>', flush=True)
        return store, bootstrap_pw
    # Store exists: prove we can write before serving (password changes,
    # lockouts and revocations all need writes). Read-only store = unknown
    # auth state = refuse to start.
    parent = os.path.dirname(path)
    if not os.access(path, os.W_OK) or (
            parent and not os.access(parent, os.W_OK)):
        raise AuthStoreError(f'auth store not writable ({path}): refusing '
                             'to run with unknown auth state')
    return store, None


# ---------------------------------------------------------------------------
# Identity: wheel membership is read live from the system on every call.
# ---------------------------------------------------------------------------

APP_USERNAME_RE = '^[A-Za-z0-9_.-]+$'

import re as _re
_USERNAME_RX = _re.compile(r'^[A-Za-z0-9_.\-]+$')


def valid_admin_username(username):
    return bool(username) and bool(_USERNAME_RX.match(username))


def is_wheel_member(username):
    """Live check: supplementary wheel membership or wheel primary GID."""
    if not username:
        return False
    try:
        wheel = grp.getgrnam('wheel')
    except KeyError:
        return False
    if username in wheel.gr_mem:
        return True
    try:
        return pwd.getpwnam(username).pw_gid == wheel.gr_gid
    except KeyError:
        return False


def is_system_user(username):
    """True when the username exists in /etc/passwd."""
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def classify_admin(username, store):
    """Return 'wheel' / 'app-only', or None when `username` may not act.

    - Wheel tier: has a store row AND is currently a wheel member.
    - App-only tier: has a store row AND has no system account.
    - A store row for a non-wheel system account (the child, or any other
      local user) yields None: such accounts never authenticate.
    """
    if not username:
        return None
    row = store.get('users', {}).get(username)
    if not isinstance(row, dict):
        return None
    if is_wheel_member(username):
        return 'wheel'
    if is_system_user(username):
        return None
    return 'app-only'


def is_locked_admin(username, store):
    row = store.get('users', {}).get(username)
    return bool(isinstance(row, dict) and row.get('locked', False))


def is_must_change(username, store):
    row = store.get('users', {}).get(username)
    return bool(isinstance(row, dict) and row.get('must_change', False))


# ---------------------------------------------------------------------------
# Server-side sessions. Cookie = opaque ID only; data lives here and dies
# with the process (restart invalidates everything: parents re-login).
# ---------------------------------------------------------------------------

SESSION_COOKIE = 'pc_sid'
IDLE_TIMEOUT_SECONDS = 30 * 60
ABSOLUTE_LIFETIME_SECONDS = 12 * 3600

_sessions = {}
_sessions_lock = threading.Lock()


def create_session(username):
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _sessions_lock:
        _sessions[sid] = {
            'username': username,
            'csrf': csrf,
            'created': now,
            'last_seen': now,
        }
    return sid, csrf


def get_session(sid):
    """Return the session dict (and refresh idle TTL) or None."""
    if not sid:
        return None
    now = time.monotonic()
    with _sessions_lock:
        sess = _sessions.get(sid)
        if sess is None:
            return None
        if now - sess['created'] > ABSOLUTE_LIFETIME_SECONDS:
            _sessions.pop(sid, None)
            return None
        if now - sess['last_seen'] > IDLE_TIMEOUT_SECONDS:
            _sessions.pop(sid, None)
            return None
        sess['last_seen'] = now
        return dict(sess)


def destroy_session(sid):
    if not sid:
        return
    with _sessions_lock:
        _sessions.pop(sid, None)


def clear_sessions():
    with _sessions_lock:
        _sessions.clear()


# ---------------------------------------------------------------------------
# CSRF: one token per session; every state-changing request echoes it via
# form field `csrf_token` or the `X-CSRF-Token` header (templates use fetch).
# ---------------------------------------------------------------------------

CSRF_FIELD = 'csrf_token'
CSRF_HEADER = 'X-CSRF-Token'


def validate_csrf(session_csrf, request):
    """True when the request echoes the session's CSRF token."""
    if not session_csrf:
        return False
    sent = request.headers.get(CSRF_HEADER, '')
    if not sent:
        sent = request.form.get(CSRF_FIELD, '')
    if not sent:
        return False
    return secrets.compare_digest(str(sent), str(session_csrf))


# ---------------------------------------------------------------------------
# Login brute-force protection: exponential backoff per source address plus
# a lockout-with-notification after repeated failures. In-memory; a restart
# resets counters (acceptable: localhost-only bind keeps the surface tiny).
# ---------------------------------------------------------------------------

LOGIN_BACKOFF_BASE = 2
LOGIN_BACKOFF_MAX = 300
LOGIN_LOCKOUT_AFTER = 15

_failed_logins = {}
_failed_logins_lock = threading.Lock()


def login_backoff_remaining(ip):
    with _failed_logins_lock:
        rec = _failed_logins.get(ip)
        if not rec:
            return 0
        wait = min(LOGIN_BACKOFF_BASE ** max(0, rec['count'] - 1),
                   LOGIN_BACKOFF_MAX)
        remaining = (rec['at'] + wait) - time.monotonic()
        return max(0.0, remaining)


def record_login_failure(ip):
    with _failed_logins_lock:
        rec = _failed_logins.setdefault(ip, {'count': 0, 'at': 0.0})
        rec['count'] += 1
        rec['at'] = time.monotonic()
        count = rec['count']
    if count == LOGIN_LOCKOUT_AFTER:
        print(f'AUTH LOCKOUT: {count} failed logins from {ip}; '
              f'backoff active. If this was you, wait and retry; '
              f'admins can always recover with '
              f'`sudo parental-controls reset-password <admin>`.',
              flush=True)
    return count


def record_login_success(ip):
    with _failed_logins_lock:
        _failed_logins.pop(ip, None)


def clear_login_failures():
    with _failed_logins_lock:
        _failed_logins.clear()


RECOVERY_HINT = ('Account locked or unavailable. Recover from the console: '
                 '`sudo parental-controls reset-password <admin>` '
                 '(or `unlock <username>` for non-wheel accounts).')
