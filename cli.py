#!/usr/bin/env python3
"""CLI recovery for Parental Controls (ships with MVP auth).

The can't-lock-yourself-out guarantee: works even if the app is wedged
or the credential is lost. Run as root (sudo) so it can read/write the
root-held auth store and run privileged commands:

  sudo parental-controls unlock <username>      # unlock a non-wheel account
  sudo parental-controls unfreeze-all           # thaw every frozen category
  sudo parental-controls reset-password <admin> # new generated password, once
  sudo parental-controls add-admin <username>   # app-only admin (or wheel row)
  sudo parental-controls remove-admin <username>

Install as /usr/local/bin/parental-controls (root-owned, 0755).
"""

import argparse
import os
import secrets
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
import budgets  # noqa: E402


def _load():
    try:
        return auth.load_store()
    except auth.AuthStoreError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        raise SystemExit(1)


def _save(store):
    try:
        auth.save_store(store)
    except auth.AuthStoreError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        raise SystemExit(1)


def cmd_unlock(args):
    username = args.username
    if auth.is_wheel_member(username):
        print(f'Refused: {username} is a wheel admin and can never be '
              'locked; nothing to unlock.')
        raise SystemExit(1)
    result = subprocess.run(['usermod', '-U', username],
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        print(f'usermod -U failed: {result.stderr.strip() or "failed"}',
              file=sys.stderr)
        raise SystemExit(1)
    print(f'{username} unlocked.')


def cmd_unfreeze_all(args):
    failures = []
    try:
        names = os.listdir(budgets.CGROUP_ROOT)
    except OSError as e:
        print(f'ERROR: cannot list {budgets.CGROUP_ROOT}: {e}',
              file=sys.stderr)
        raise SystemExit(1)
    count = 0
    for name in sorted(names):
        if not name.startswith('parental-'):
            continue
        rest = name[len('parental-'):]
        # Split username/category: category is always the last dash part
        # and must be a known category; username keeps the remainder so
        # names containing dashes still resolve correctly.
        parts = rest.split('-')
        user = cat = None
        for i in range(1, len(parts)):
            candidate_user = '-'.join(parts[:i])
            candidate_cat = '-'.join(parts[i:])
            if (budgets.valid_category(candidate_cat)
                    and budgets.valid_username(candidate_user)):
                user, cat = candidate_user, candidate_cat
                break
        if user is None:
            continue
        ok, msg = budgets.set_frozen(user, cat, False)
        if ok:
            count += 1
        else:
            failures.append(f'{name}: {msg}')
    print(f'Thawed {count} cgroup(s).')
    for f in failures:
        print(f'FAILED: {f}', file=sys.stderr)
    if failures:
        raise SystemExit(1)


def cmd_reset_password(args):
    store = _load()
    username = args.admin
    row = store.get('users', {}).get(username)
    if not isinstance(row, dict):
        print(f'ERROR: no such admin: {username}', file=sys.stderr)
        raise SystemExit(1)
    if auth.classify_admin(username, store) is None:
        print(f'ERROR: {username} is not an eligible admin account.',
              file=sys.stderr)
        raise SystemExit(1)
    generated = secrets.token_urlsafe(24)
    row['hash'] = auth.hash_password(generated)
    row['locked'] = False
    row['must_change'] = True
    _save(store)
    print(f'New app password for {username!r}: {generated}')
    print('Shown ONCE. It must be changed at first login.')


def cmd_add_admin(args):
    try:
        store = auth.load_store()
    except auth.AuthStoreError:
        if os.path.exists(auth.auth_file_path()):
            # Corrupt/unreadable: fail closed, never bootstrap over it.
            print('ERROR: auth store exists but is unreadable; refusing '
                  'to overwrite it.', file=sys.stderr)
            raise SystemExit(1)
        # First admin on a fresh install: start from a blank store.
        # (Every other command fails closed on a missing store.)
        store = auth.blank_store()
    username = args.username
    if not auth.valid_admin_username(username):
        print('ERROR: invalid username.', file=sys.stderr)
        raise SystemExit(1)
    if username in store.get('users', {}):
        print(f'ERROR: admin already exists: {username}', file=sys.stderr)
        raise SystemExit(1)
    if auth.is_system_user(username) and not auth.is_wheel_member(username):
        print(f'Refused: {username} is a non-admin system account and can '
              'never authenticate.', file=sys.stderr)
        raise SystemExit(1)
    if auth.is_wheel_member(username):
        tier = 'wheel'
    else:
        tier = 'app-only'
    generated = secrets.token_urlsafe(24)
    store['users'][username] = {
        'hash': auth.hash_password(generated),
        'must_change': True,
        'locked': False,
    }
    _save(store)
    print(f'Admin {username!r} created (tier: {tier}).')
    print(f'App password: {generated}')
    print('Shown ONCE. It must be changed at first login.')


def cmd_remove_admin(args):
    store = _load()
    username = args.username
    if auth.is_wheel_member(username):
        print(f'Refused: {username} is a wheel admin and can never be '
              'removed.', file=sys.stderr)
        raise SystemExit(1)
    if username not in store.get('users', {}):
        print(f'ERROR: no such admin: {username}', file=sys.stderr)
        raise SystemExit(1)
    del store['users'][username]
    _save(store)
    print(f'Admin {username!r} removed.')


def cmd_lock_admin(args):
    """Lock an admin's *app login* (not their system account)."""
    store = _load()
    username = args.admin
    if auth.is_wheel_member(username):
        print(f'Refused: wheel admins cannot be locked.',
              file=sys.stderr)
        raise SystemExit(1)
    row = store.get('users', {}).get(username)
    if not isinstance(row, dict):
        print(f'ERROR: no such admin: {username}', file=sys.stderr)
        raise SystemExit(1)
    row['locked'] = True
    _save(store)
    print(f'Admin {username!r} login locked.')


def main(argv=None):
    auth.ensure_hash_backend()
    parser = argparse.ArgumentParser(prog='parental-controls',
                                     description='Parental Controls recovery CLI')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('unlock', help='Unlock any non-wheel account')
    p.add_argument('username')
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser('unfreeze-all',
                       help='Thaw every frozen category cgroup')
    p.set_defaults(func=cmd_unfreeze_all)

    p = sub.add_parser('reset-password',
                       help='Set a new generated app password for an admin')
    p.add_argument('admin')
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser('add-admin', help='Add an app-only admin')
    p.add_argument('username')
    p.set_defaults(func=cmd_add_admin)

    p = sub.add_parser('remove-admin', help='Remove an app-only admin')
    p.add_argument('username')
    p.set_defaults(func=cmd_remove_admin)

    p = sub.add_parser('lock-admin', help="Lock an app-only admin's login")
    p.add_argument('admin')
    p.set_defaults(func=cmd_lock_admin)

    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print('WARNING: not running as root; privileged operations and the '
              'auth store may fail. Use sudo.', file=sys.stderr)
    args.func(args)


if __name__ == '__main__':
    main()
