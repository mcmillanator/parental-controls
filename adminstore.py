#!/usr/bin/env python3
"""Shared admin-store mutations for the web console and the recovery CLI.

Both surfaces mutate the same ``auth.json`` rows:

- Web UI: ``POST /api/admins`` (add) and ``POST /api/admins/remove``.
- Recovery CLI: ``parental-controls add-admin`` / ``remove-admin``.

These functions centralize validation + persistence so the two surfaces
cannot drift (previously the logic was duplicated inline). They never
touch system accounts (no usermod, no PAM): app passwords live only in
the auth store.

``cmd_*`` handlers in cli.py translate the exceptions into their exact
historical messages/exit codes; app.py translates them into JSON with the
matching status codes. Audit logging stays with the caller because the
two surfaces log different actors (``cli:add-admin`` vs the web admin).

Protections mirrored from cli.py (AUTH_DESIGN.md):
- A non-admin system account (e.g. the child) can never be given a row.
- Wheel admins can never be removed.
- The *web console* additionally refuses to add a wheel member's row
  (wheel rows are bootstrap/CLI territory only); the CLI keeps its
  historical behavior and may still mint a wheel row.
"""

import secrets

import auth


class AdminStoreError(Exception):
    """Base class for admin-store mutation failures (fail closed)."""


class RefusedError(AdminStoreError):
    """A policy refusal: wheel target, or a non-admin system account."""


class InvalidUsernameError(AdminStoreError):
    """Username fails the auth store charset/format rules."""


class AdminExistsError(AdminStoreError):
    """The username already has a store row."""


class NoSuchAdminError(AdminStoreError):
    """The username has no store row to remove."""


class AuthStoreUnavailable(AdminStoreError):
    """Store missing/corrupt/unwritable: never mutate an unknown state."""


def load_checked(store=None):
    """Return `store`, else load the live auth store (fail closed)."""
    if store is not None:
        return store
    try:
        return auth.load_store()
    except auth.AuthStoreError as e:
        raise AuthStoreUnavailable(str(e)) from e


def save_checked(store):
    """Persist the store atomically (fail closed)."""
    try:
        auth.save_store(store)
    except auth.AuthStoreError as e:
        raise AuthStoreUnavailable(str(e)) from e


def add_admin(username, store=None, allow_wheel=True):
    """Create an admin row and return ``(tier, generated_password)``.

    The minted row always starts ``must_change=True`` / ``locked=False``
    and the password is generated server-side, so it can be shown exactly
    once (mirrors cli.py ``cmd_add_admin`` / bootstrap).

    ``allow_wheel=False`` refuses a wheel target (web-console rule);
    the CLI passes the default and may still mint a wheel row, keeping
    its historical behavior byte-identical.

    Raises AdminStoreError subclasses on any refusal or persistence
    failure; the store is mutated only when every validation passes.
    """
    username = str(username or '')
    if not auth.valid_admin_username(username):
        raise InvalidUsernameError('invalid username.')
    store = load_checked(store)
    if not isinstance(store, dict) or not isinstance(
            store.get('users'), dict):
        raise AuthStoreUnavailable('auth store corrupt: bad shape')
    if username in store['users']:
        raise AdminExistsError(f'admin already exists: {username}')
    if auth.is_system_user(username) and not auth.is_wheel_member(username):
        raise RefusedError(
            f'{username} is a non-admin system account and can never '
            'authenticate.')
    if auth.is_wheel_member(username) and not allow_wheel:
        raise RefusedError(f'{username} is a wheel admin and can never be '
                           'added from the web console.')
    tier = 'wheel' if auth.is_wheel_member(username) else 'app-only'
    generated = secrets.token_urlsafe(24)
    store['users'][username] = {
        'hash': auth.hash_password(generated),
        'must_change': True,
        'locked': False,
    }
    save_checked(store)
    return tier, generated


def remove_admin(username, store=None):
    """Remove an app-only admin row. Wheel targets are always refused.

    The wheel check runs before the load so a live wheel member is
    refused even when the store is missing/corrupt (fail-closed, mirrors
    the historical web handler's pre-flight refusal).

    Raises RefusedError (wheel target), NoSuchAdminError,
    AuthStoreUnavailable.
    """
    username = str(username or '')
    if auth.is_wheel_member(username):
        raise RefusedError(f'{username} is a wheel admin and can never be '
                           'removed.')
    store = load_checked(store)
    # Belt and braces: wheel membership is read live on every call, so a
    # membership change between load and removal still refuses.
    if auth.is_wheel_member(username):
        raise RefusedError(f'{username} is a wheel admin and can never be '
                           'removed.')
    if username not in store.get('users', {}):
        raise NoSuchAdminError(f'no such admin: {username}')
    del store['users'][username]
    save_checked(store)
    return store