"""Freezer watchdog: detect + self-heal stuck/orphan freezes (prototype).

Incident (2026-10-09): a stale `parental-tester-quarantine` cgroup held an
entire desktop session frozen (cgroup.freeze=1, 17 procs incl.
kwin/pipewire/dbus) with no corresponding budget state. The desktop stayed
black until manually thawed. This watchdog makes that class of incident
self-heal on the next 60s tick.

Ordering contract (no flapping vs. legitimate freezes): enforcement runs
FIRST, watchdog SECOND, against the SAME in-memory budgets dict — so a
category the current tick legitimately froze already has
paused[category]=True set and the watchdog sees it as legitimate.
`tick_budgets()` implements exactly this order (see below).

Frozen means BOTH files agree: desired state `cgroup.freeze == '1'` AND
effective state `cgroup.events` reports `frozen 1`. A cgroup mid-transition
(freeze=1 written, events still `frozen 0` while processes settle) is
skipped, never thawed — this avoids flapping with a freeze that is still
landing. Any unreadable/ambiguous state is logged + skipped, never thawed
blindly. Only UNFREEZE writes are ever issued, only on OUR
`parental-<user>-<category>` and `parental-<user>-app-<comm>` groups.
No signals, no kills, no membership changes anywhere.

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 7; the facade
re-exports every name by name. `verify_freeze_safety` reads the account owner
through `budgets.pwd`, and the gates resolve `budgets.filter`/`budgets.freeze`
at call time — see the cross-seam convention in budgets_guards.py.
"""

import os


def parse_managed_cgroup(name):
    """Split a managed group name -> (username, bucket), or None.

    ``bucket`` is a category name (``parental-<user>-<category>``) or an
    ``app-<comm>`` bucket (``parental-<user>-app-<comm>``); use
    ``split_bucket`` to tell them apart. Returns None for anything not
    matching our naming scheme (foreign groups such as a reappearing
    ``parental-*-quarantine``). The category split is on the LAST hyphen
    so hyphenated usernames still parse; app groups are recognized by the
    ``-app-`` infix (a real category never contains a hyphen).
    """
    import budgets
    if not name.startswith('parental-'):
        return None
    rest = name[len('parental-'):]
    if '-' not in rest:
        return None
    username, last = rest.rsplit('-', 1)
    if budgets.valid_username(username) and budgets.valid_category(last):
        return username, last
    idx = rest.rfind('-' + budgets.APP_BUCKET_PREFIX)
    if idx > 0:
        username = rest[:idx]
        comm = rest[idx + len(budgets.APP_BUCKET_PREFIX) + 1:]
        if budgets.valid_username(username) and budgets.valid_comm(comm):
            return username, budgets._app_bucket(comm)
    return None


def _read_desired_frozen(cgroup_dir):
    """Desired freezer state from `cgroup.freeze`: True/False/None.

    None = missing, unreadable, or unexpected content (ambiguous: the
    caller must skip, never thaw blindly). Plain open() on purpose: these
    files are world-readable, and this keeps the watchdog usable without
    sudo for reads.
    """
    try:
        with open(os.path.join(cgroup_dir, 'cgroup.freeze'), 'r') as f:
            content = f.read().strip()
    except OSError:
        return None
    if content == '1':
        return True
    if content == '0':
        return False
    return None


def _read_effective_frozen(cgroup_dir):
    """Effective freezer state from `cgroup.events` (`frozen 1/0`).

    None = missing, unreadable, or unparseable (ambiguous: skip).
    """
    try:
        with open(os.path.join(cgroup_dir, 'cgroup.events'), 'r') as f:
            text = f.read()
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == 'frozen':
            if parts[1] == '1':
                return True
            if parts[1] == '0':
                return False
            return None
    return None


def watchdog_tick(budgets_data=None, cgroup_root=None, thaw_fn=None):
    """One watchdog pass: thaw stuck/orphan freezes, report foreign groups.

    budgets_data: budget-store-shaped dict (default: loaded from the store).
    cgroup_root: directory to scan (default: real /sys/fs/cgroup).
    thaw_fn: callable(username, bucket) -> (ok, msg) used for the
      unfreeze write, where bucket is a category or ``app-<comm>``
      (default: thaw_bucket on the real tree, direct '0' write on any
      alternate root so tests never touch sudo or the real hierarchy).

    Returns a summary dict {thawed, legit, clean, unknown, skipped, note}.
    Only OUR managed groups are ever thawed, and only when truly frozen
    (desired AND effective) with NO paused/manual-frozen flag for that
    user/bucket (category flags for category groups, ``app_*`` flags for
    app groups).
    Foreign `parental-*` groups are logged loudly and reported in
    `unknown` but NEVER touched.
    """
    import budgets
    root = cgroup_root or budgets.CGROUP_ROOT
    if budgets_data is None:
        try:
            budgets_data = budgets.load_budgets()
        except Exception as e:
            note = f'WATCHDOG: could not load budget state, skipping pass: {e}'
            print(note, flush=True)
            return {'thawed': [], 'legit': [], 'clean': [], 'unknown': [],
                    'skipped': [], 'note': note}
    if not isinstance(budgets_data, dict):
        budgets_data = {}
    if thaw_fn is None:
        if root != budgets.CGROUP_ROOT:
            # Alternate/test root: direct write, no sudo, no real tree.
            def thaw_fn(username, bucket, _root=root):
                try:
                    cdir = os.path.join(
                        _root, f'parental-{username}-{bucket}')
                    with open(os.path.join(cdir, 'cgroup.freeze'), 'w') as f:
                        f.write('0')
                except (OSError, ValueError) as e:
                    return False, str(e)
                return True, ''
        else:
            thaw_fn = budgets.thaw_bucket
    summary = {'thawed': [], 'legit': [], 'clean': [], 'unknown': [],
               'skipped': [], 'note': ''}
    try:
        names = os.listdir(root)
    except OSError as e:
        note = f'WATCHDOG: cannot list {root}, skipping pass: {e}'
        print(note, flush=True)
        summary['note'] = note
        return summary
    for name in names:
        if not name.startswith('parental-'):
            continue
        parsed = parse_managed_cgroup(name)
        if parsed is None:
            print(f'WATCHDOG: unknown parental cgroup {name} present; '
                  f'leaving untouched (foreign group, report only)',
                  flush=True)
            summary['unknown'].append(name)
            continue
        username, bucket = parsed
        parts = budgets.split_bucket(bucket)
        if parts is None:
            summary['unknown'].append(name)
            continue
        kind, target = parts
        cdir = os.path.join(root, name)
        desired = _read_desired_frozen(cdir)
        if desired is None:
            print(f'WATCHDOG: {name}: cannot read cgroup.freeze, '
                  f'skipping (never thaw on ambiguous state)', flush=True)
            summary['skipped'].append(name + ' (unreadable freeze state)')
            continue
        if not desired:
            summary['clean'].append(name)
            continue
        effective = _read_effective_frozen(cdir)
        if effective is None:
            print(f'WATCHDOG: {name}: freeze=1 but cgroup.events unreadable, '
                  f'skipping (never thaw on ambiguous state)', flush=True)
            summary['skipped'].append(name + ' (unreadable events)')
            continue
        if not effective:
            # Freeze requested but not yet effective: still settling.
            # Skip quietly-ish so the next tick re-checks without flapping.
            summary['skipped'].append(
                name + ' (transition: freeze=1, frozen 0)')
            continue
        # Truly frozen (desired + effective). Legitimate ONLY when the
        # budget state marks this bucket paused (auto-exhaustion) OR
        # manually frozen by the parent. App buckets consult the parallel
        # app_* maps.
        if kind == 'app':
            paused_key, manual_key = 'app_paused', 'app_manual_frozen'
        else:
            paused_key, manual_key = 'paused', 'manual_frozen'
        try:
            user_entry = budgets_data.get(username) or {}
            paused = bool(user_entry.get(paused_key, {}).get(target, False))
            manual = bool(user_entry.get(manual_key, {}).get(target, False))
        except (AttributeError, TypeError):
            paused, manual = False, False
        if paused or manual:
            summary['legit'].append(name)
            continue
        print(f'WATCHDOG: STUCK freeze detected: {name} is frozen '
              f'(freeze=1, frozen 1) with NO paused budget state; '
              f'thawing to self-heal', flush=True)
        try:
            ok, msg = thaw_fn(username, bucket)
        except Exception as e:
            ok, msg = False, str(e)
        if ok:
            print(f'WATCHDOG: THAWED stuck cgroup {name} '
                  f'(orphan freeze self-healed)', flush=True)
            summary['thawed'].append(name)
        else:
            print(f'WATCHDOG: FAILED to thaw stuck cgroup {name}: {msg}',
                  flush=True)
            summary['skipped'].append(name + f' (thaw failed: {msg})')
    if summary['thawed']:
        summary['note'] = ('self-healed stuck freeze(s): '
                           + ', '.join(summary['thawed']))
    if summary['unknown']:
        note = ('foreign parental cgroup(s) present, left untouched: '
                + ', '.join(summary['unknown']))
        summary['note'] = (summary['note'] + '; ' + note if summary['note']
                           else note)
    return summary


def verify_freeze_safety(username, member_pids, expected_freezable):
    """Freeze-time gate: every cgroup member must be owned by the user and
    not on the NEVER-pause list. expected_freezable maps pid -> proc dict
    from a fresh snapshot. Returns (ok, reason)."""
    import budgets
    try:
        uid = budgets.pwd.getpwnam(username).pw_uid
    except KeyError:
        return False, f'unknown user {username}'
    expected = set(expected_freezable)
    for pid in member_pids:
        try:
            st = os.stat(f'/proc/{pid}')
        except OSError:
            continue  # exited since the read; harmless
        if st.st_uid != uid:
            return False, f'PID {pid} not owned by {username}; aborting freeze'
        proc = expected_freezable.get(pid)
        if proc is None:
            # Member we did not classify this tick (stale/unknown): read its
            # comm and apply the NEVER-pause gate before allowing a freeze.
            try:
                with open(f'/proc/{pid}/comm') as f:
                    comm = f.read().strip()
            except OSError:
                continue
            if budgets.is_never_pause(
                    {'comm': comm, 'cmdline': budgets.read_cmdline(pid)}):
                return False, (f'PID {pid} ({comm}) is NEVER-pause; '
                               'aborting freeze')
            return False, (f'PID {pid} not in the freezable set; '
                            'aborting freeze')
        if budgets.is_never_pause(proc):
            return False, f"PID {pid} ({proc.get('comm')}) is NEVER-pause"
    if not expected and member_pids:
        return False, 'cgroup has members but nothing is freezable'
    return True, ''


def _visible_category_members(procs, username, category, prefs, index,
                              exclude_comms=None):
    """Visible + categorized-into-category + not NEVER-pause members.

    ``exclude_comms`` drops comms that carry an app override (exempt or
    allowance) so a category freeze/window never bills or freezes them.
    """
    import budgets
    visible, _hidden = budgets.apply_filter(procs, username, prefs)
    exclude = exclude_comms or frozenset()
    out = []
    for proc in visible:
        if budgets.categorize(proc, index) != category:
            continue
        if budgets.is_never_pause(proc):
            continue
        if proc.get('comm') in exclude:
            continue
        out.append(proc)
    return out


def freezable_members(procs, username, category, limits, prefs=None,
                      index=None, exclude_comms=None):
    """Match-time gate: visible after filtering AND in the budgeted category
    AND not NEVER-pause AND category actually has a limit configured."""
    if float(limits.get(category, 0) or 0) <= 0:
        return []
    return _visible_category_members(procs, username, category, prefs, index,
                                     exclude_comms)


def freezable_app_members(procs, username, comm, prefs=None):
    """Match-time gate for a per-app allowance: visible, same comm, and
    not NEVER-pause. (Exempt apps call this? No — exempt comms never
    enter a cgroup, so callers skip them entirely.)"""
    import budgets
    visible, _hidden = budgets.apply_filter(procs, username, prefs)
    out = []
    for proc in visible:
        if (proc.get('comm') or '') != comm:
            continue
        if budgets.is_never_pause(proc):
            continue
        out.append(proc)
    return out

