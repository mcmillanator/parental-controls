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
import sys
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


def state_dir_owner(path=None):
    """(uid, gid) of the directory that holds the store, or None.

    install.sh renders the state directory (``/etc/parental-controls``) and
    the service unit's ``User=`` from the same ``SERVICE_USER`` and chowns
    the directory to it, so on a deployed box the directory owner *is* the
    account that must be able to read ``auth.json``. That makes it the only
    ownership hint available to a root-only writer such as the recovery CLI.
    """
    path = path or auth_file_path()
    parent = os.path.dirname(path) or '.'
    try:
        st = os.stat(parent)
    except OSError:
        return None
    return (st.st_uid, st.st_gid)


def store_owner(path=None):
    """(uid, gid) a writer should give the store when nothing else is said.

    Preserve the owner of an existing store -- the atomic replace must never
    change *who can read the credentials*. For a brand-new store there is
    nothing to preserve, so inherit the state directory's owner: that is the
    service user on an installed box, which keeps a store created by root
    (e.g. the first ``add-admin``) readable by the service instead of
    leaving it root-only and fail-closed.
    """
    path = path or auth_file_path()
    try:
        st = os.stat(path)
    except OSError:
        return state_dir_owner(path)
    return (st.st_uid, st.st_gid)


def _owner_name(uid, gid):
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = str(gid)
    return f'{user}:{group}'


def _apply_owner(tmp, owner, path):
    """Give the temp file the intended owner before the atomic replace.

    Best-effort by design, and deliberately *not* fail-closed:

    - A non-root writer (the service rewriting its own store, an unpackaged
      run) simply cannot chown; failing the write would then make password
      changes, lockouts and revocations impossible, which is its own denial
      of service.
    - The recovery CLI is the documented last-resort escape hatch
      ("can't-lock-yourself-out"); refusing to persist a reset password
      because of an ownership repair would strand the operator.

    But a mis-owned store is *not* a cosmetic problem: it makes every
    request fail closed with "Authentication unavailable", which is exactly
    the 2026-10-14 outage. So a failed chown is reported loudly on stderr
    (under systemd it lands in the unit's journal) with the remedy, rather
    than being swallowed.
    """
    if not owner:
        return
    try:
        owner = (int(owner[0]), int(owner[1]))
    except (TypeError, ValueError, IndexError):
        return
    try:
        st = os.stat(tmp)
    except OSError:
        return
    if (st.st_uid, st.st_gid) == owner:
        return
    try:
        os.chown(tmp, owner[0], owner[1])
    except OSError as e:
        print(f'AUTH WARNING: {path} could not be handed to '
              f'{_owner_name(*owner)} ({e}); the service may be unable to '
              f'read the auth store and answer every request with an '
              f'authentication error. Fix with: '
              f'sudo chown {_owner_name(*owner)} {path}', file=sys.stderr,
              flush=True)


def save_store(store, path=None, owner=None):
    """Atomic write (temp + rename), mode 0600. Raises AuthStoreError.

    ``owner`` is an optional ``(uid, gid)`` for the resulting file; when it
    is omitted the store keeps the owner it already had, or -- for a new
    store -- inherits the state directory's (see ``store_owner``). Ownership
    is applied to the temp file before the replace so there is no window in
    which the store exists with the wrong owner.
    """
    path = path or auth_file_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if owner is None:
            owner = store_owner(path)
        fd, tmp = tempfile.mkstemp(dir=parent or '.', prefix='.auth-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(store, f, indent=2)
            os.chmod(tmp, 0o600)
            _apply_owner(tmp, owner, path)
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
