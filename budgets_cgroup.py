"""The cgroup freezer: paths, scoped-sudo read/write, freeze, migrate.

Every privileged write the budget model performs goes through here —
`sudo cat`, `sudo tee`, `sudo mkdir -p` on `parental-<user>-<category>` and
`parental-<user>-app-<comm>` groups only. `set_frozen`/`set_frozen_app` freeze
and thaw (verified by readback), `migrate_pid*`/`cgroup_members*` manage
membership, `split_bucket`/`thaw_bucket` name the groups the watchdog and the
CLI address, and `unfreeze_all_user` thaws everything for one user at reset.

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 6; the facade
re-exports every name by name. `CGROUP_ROOT`, the validators and `subprocess`
resolve through `budgets` at call time, so `budgets.CGROUP_ROOT` and
`mock.patch.object(budgets.subprocess, 'run')` keep reaching these calls —
see the cross-seam convention in budgets_guards.py.
"""

import os

import audit

# ---------------------------------------------------------------------------
# Cgroup freezer ops (privileged via sudo tee; validated username/category)
# ---------------------------------------------------------------------------

def cgroup_path(username, category):
    import budgets
    if (not budgets.valid_username(username)
            or not budgets.valid_category(category)):
        raise ValueError('Invalid username or category for cgroup path')
    return os.path.join(budgets.CGROUP_ROOT, f'parental-{username}-{category}')


def cgroup_path_app(username, comm):
    """Path of the per-app group ``parental-<user>-app-<comm>``.

    ``valid_comm`` pins the same charset the sudoers grant is already
    anchored on (``parental-[A-Za-z0-9_.-]+``), so no privilege grant is
    needed for these groups.
    """
    import budgets
    if not budgets.valid_username(username) or not budgets.valid_comm(comm):
        raise ValueError('Invalid username or comm for app cgroup path')
    return os.path.join(budgets.CGROUP_ROOT, f'parental-{username}-app-{comm}')


def _sudo_read_file(path):
    import budgets
    try:
        result = budgets.subprocess.run(
            ['sudo', '-n', 'cat', path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception:
        return None


def _sudo_write_file(path, content):
    """Write via `sudo tee` (the only privileged write pattern used)."""
    import budgets
    try:
        result = budgets.subprocess.run(
            ['sudo', '-n', 'tee', path],
            input=content, capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0, (
            '' if result.returncode == 0
            else (result.stderr.strip() or 'tee failed'))
    except Exception as e:
        return False, str(e)


def _ensure_cgroup_dir(path):
    """Create a cgroup dir (sudo mkdir -p); never touches other groups."""
    import budgets
    if os.path.isdir(path):
        return True, ''
    try:
        result = budgets.subprocess.run(
            ['sudo', '-n', 'mkdir', '-p', path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or 'mkdir failed'
        return True, ''
    except Exception as e:
        return False, str(e)


def ensure_cgroup(username, category):
    """Create parental-<user>-<cat> if missing. Never touches other groups."""
    return _ensure_cgroup_dir(cgroup_path(username, category))


def ensure_cgroup_app(username, comm):
    """Create parental-<user>-app-<comm> if missing."""
    return _ensure_cgroup_dir(cgroup_path_app(username, comm))


def _read_freeze_path(path):
    content = _sudo_read_file(path)
    if content is None:
        return None
    return content.strip() == '1'


def read_freeze(username, category):
    return _read_freeze_path(os.path.join(cgroup_path(username, category),
                                          'cgroup.freeze'))


def read_freeze_app(username, comm):
    return _read_freeze_path(os.path.join(cgroup_path_app(username, comm),
                                          'cgroup.freeze'))


def _set_frozen_path(path, frozen, actor, target, ensure):
    """Freeze/thaw one cgroup.freeze file; verify by readback.

    Shared by the category and per-app helpers. The audit hook is
    best-effort and can never fail the freeze itself.
    """
    if not os.path.isdir(os.path.dirname(path)):
        if frozen:
            ok, msg = ensure()
            if not ok:
                return False, msg
        else:
            audit.safe_log(actor, 'budget.thaw', target,
                           {'ok': True, 'note': 'no cgroup (already unfrozen)',
                            'frozen': False})
            return True, 'no cgroup (already unfrozen)'
    ok, msg = _sudo_write_file(path, '1' if frozen else '0')
    if not ok:
        audit.safe_log(actor, 'budget.freeze' if frozen else 'budget.thaw',
                       target, {'ok': False, 'error': msg,
                                'frozen': bool(frozen)})
        return False, msg
    content = _sudo_read_file(path)
    if content is None:
        audit.safe_log(actor, 'budget.freeze' if frozen else 'budget.thaw',
                       target,
                       {'ok': False, 'error': 'could not verify freeze state',
                        'frozen': bool(frozen)})
        return False, 'could not verify freeze state'
    state = content.strip() == '1'
    if state != bool(frozen):
        audit.safe_log(actor, 'budget.freeze' if frozen else 'budget.thaw',
                       target,
                       {'ok': False,
                        'error': 'freeze state mismatch after write',
                        'frozen': bool(frozen)})
        return False, 'freeze state mismatch after write'
    audit.safe_log(actor, 'budget.freeze' if frozen else 'budget.thaw',
                   target, {'ok': True, 'frozen': bool(frozen)})
    return True, ''


def set_frozen(username, category, frozen, actor='system'):
    """Freeze (True) or thaw (False) a category cgroup; verify by readback."""
    try:
        path = os.path.join(cgroup_path(username, category), 'cgroup.freeze')
    except ValueError as e:
        return False, str(e)
    return _set_frozen_path(path, frozen, actor, f'{username}:{category}',
                            lambda: budgets.ensure_cgroup(username, category))


def set_frozen_app(username, comm, frozen, actor='system'):
    """Freeze (True) or thaw (False) a per-app cgroup; verify by readback."""
    try:
        path = os.path.join(cgroup_path_app(username, comm), 'cgroup.freeze')
    except ValueError as e:
        return False, str(e)
    return _set_frozen_path(path, frozen, actor, f'{username}:app:{comm}',
                            lambda: budgets.ensure_cgroup_app(username, comm))


def _cgroup_members_path(path):
    content = _sudo_read_file(path)
    if content is None:
        return []
    pids = []
    for token in content.split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def cgroup_members(username, category):
    """PIDs currently in the category cgroup ([] if missing/unreadable)."""
    try:
        path = os.path.join(cgroup_path(username, category), 'cgroup.procs')
    except ValueError:
        return []
    return _cgroup_members_path(path)


def cgroup_members_app(username, comm):
    """PIDs currently in the per-app cgroup ([] if missing/unreadable)."""
    try:
        path = os.path.join(cgroup_path_app(username, comm), 'cgroup.procs')
    except ValueError:
        return []
    return _cgroup_members_path(path)


def migrate_pid(username, category, pid):
    """Move one PID into the category cgroup (no-op behaviorally)."""
    try:
        path = os.path.join(cgroup_path(username, category), 'cgroup.procs')
    except ValueError as e:
        return False, str(e)
    return _sudo_write_file(path, str(int(pid)))


def migrate_pid_app(username, comm, pid):
    """Move one PID into the per-app cgroup (no-op behaviorally)."""
    try:
        path = os.path.join(cgroup_path_app(username, comm), 'cgroup.procs')
    except ValueError as e:
        return False, str(e)
    return _sudo_write_file(path, str(int(pid)))


APP_BUCKET_PREFIX = 'app-'


def _app_bucket(comm):
    """Cgroup bucket name for a per-app group (the suffix after <user>-)."""
    return APP_BUCKET_PREFIX + comm


def split_bucket(bucket):
    """('app', comm) | ('category', cat) | None for a parental suffix."""
    import budgets
    if bucket.startswith(APP_BUCKET_PREFIX):
        comm = bucket[len(APP_BUCKET_PREFIX):]
        if budgets.valid_comm(comm):
            return 'app', comm
        return None
    if budgets.valid_category(bucket):
        return 'category', bucket
    return None


def thaw_bucket(username, bucket, actor='system'):
    """Thaw one managed bucket (category or app), dispatching by kind."""
    import budgets
    parts = budgets.split_bucket(bucket)
    if parts is None:
        return False, 'unknown cgroup bucket'
    kind, target = parts
    if kind == 'app':
        return budgets.set_frozen_app(username, target, False, actor=actor)
    return budgets.set_frozen(username, target, False, actor=actor)


def unfreeze_all_user(username, actor='system', keep_categories=None,
                      keep_apps=None):
    """Thaw every parental-<username>-* cgroup (category + app groups).

    keep_categories/keep_apps list targets governed by an ACTIVE weekly
    window: their groups are skipped so a day reset does not undo a
    window-exhaustion freeze (the window close thaws them instead).
    """
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    keep_categories = set(keep_categories or ())
    keep_apps = set(keep_apps or ())
    results = []
    try:
        names = os.listdir(budgets.CGROUP_ROOT)
    except OSError as e:
        return False, str(e)
    prefix = f'parental-{username}-'
    for name in names:
        if not name.startswith(prefix):
            continue
        bucket = name[len(prefix):]
        parts = budgets.split_bucket(bucket)
        if parts is None:
            continue
        kind, target = parts
        if kind == 'category':
            if target in keep_categories:
                continue
            ok, msg = budgets.set_frozen(username, target, False, actor=actor)
        else:
            if target in keep_apps:
                continue
            ok, msg = budgets.set_frozen_app(username, target, False,
                                            actor=actor)
        results.append((bucket, ok, msg))
    audit.safe_log(actor, 'budget.unfreeze_all', username,
                   {'thawed': [b for b, ok, _m in results if ok]})
    return True, results
