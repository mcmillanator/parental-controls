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

import adminstore  # noqa: E402
import auth  # noqa: E402
import audit  # noqa: E402
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
    audit.safe_log('cli:unlock', 'account.unlock', username,
                   {'success': True})
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
        # Managed category (parental-<user>-<cat>) AND per-app
        # (parental-<user>-app-<comm>) groups both thaw — the rollback
        # escape hatch from OVERRIDES_PLAN.md §4.2. Foreign schemes are
        # left untouched.
        parsed = budgets.parse_managed_cgroup(name)
        if parsed is None:
            continue
        username, bucket = parsed
        ok, msg = budgets.thaw_bucket(username, bucket,
                                      actor='cli:unfreeze-all')
        if ok:
            count += 1
        else:
            failures.append(f'{name}: {msg}')
    audit.safe_log('cli:unfreeze-all', 'budget.unfreeze_all', '',
                   {'success': not failures, 'thawed': count,
                    'failures': failures})
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
    audit.safe_log('cli:reset-password', 'admin.reset_password', username,
                   {'success': True})
    print(f'New app password for {username!r}: {generated}')
    print('Shown ONCE. It must be changed at first login.')


def cmd_add_admin(args):
    # Store mutations + validations are shared with the web console
    # (adminstore.add_admin); the CLI keeps its historical messages,
    # exit codes and the missing-store bootstrap below.
    username = args.username
    try:
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
        tier, generated = adminstore.add_admin(username, store=store)
    except adminstore.RefusedError as e:
        print(f'Refused: {e}', file=sys.stderr)
        raise SystemExit(1)
    except adminstore.AdminStoreError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        raise SystemExit(1)
    audit.safe_log('cli:add-admin', 'admin.add', username,
                   {'success': True, 'tier': tier})
    print(f'Admin {username!r} created (tier: {tier}).')
    print(f'App password: {generated}')
    print('Shown ONCE. It must be changed at first login.')


def cmd_remove_admin(args):
    store = _load()
    username = args.username
    try:
        # Shared with the web console; wheel targets always refused.
        adminstore.remove_admin(username, store=store)
    except adminstore.RefusedError as e:
        print(f'Refused: {e}', file=sys.stderr)
        raise SystemExit(1)
    except adminstore.AdminStoreError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        raise SystemExit(1)
    audit.safe_log('cli:remove-admin', 'admin.remove', username,
                   {'success': True})
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
    audit.safe_log('cli:lock-admin', 'admin.lock', username,
                   {'success': True})
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
