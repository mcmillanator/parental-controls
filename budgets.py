#!/usr/bin/env python3
"""Phase 1: process filtering, app categorization, per-category time budgets.

Prototype mindset: stdlib only (no Flask import here so unit tests run with
system python3), SQLite runtime state via store.py, no other DB.

Budget model
------------
Usage is measured in **active wall-clock minutes**, not raw CPU seconds and
not raw wall-clock: on every 60s tick we read each process's cumulative CPU
time (``ps -o time``) and compute the delta since the previous tick. If any
process in a budgeted category was active this tick (delta at or above
``ACTIVE_THRESHOLD_SECONDS``), the whole tick's wall-clock duration is
charged to that category; a tick with no active process charges nothing.
An idle app therefore contributes ~0, so background-idle MUST NOT burn
budget — while an app in actual use accrues time matching the Elapsed
column parents see.

A **locked** account (usermod -L: manual Lock, Lock Screen, bedtime
auto-lock — all reflected by get_lock_status) likewise MUST NOT burn
budget: if the child cannot reach the app, they are not spending time on
it. While locked, each tick advances the accounting baselines
(last_cpu / last_tick / was_in_bedtime) without billing a second, so the
first tick after unlock bills only NEW activity, never the locked window.

**Focus engine (opt-in, per user).** When an entry's ``focus_engine`` is
``'focus'`` (the session agent; see ``SESSION_AGENT_PLAN.md``), the tick
bills per-category *focused* seconds drained from the child's validated
feed (``focus_feed``) instead of the CPU-delta gate, behind the same
freezable-member gate. ``'cpu'`` (the default) is byte-identical to the
description above. A missing/stale/malformed feed flips the engine to
``'no_data'`` and HALTS billing for that tick (``used_seconds`` is left
untouched — never silently zeroed) until a fresh feed recovers it. The
``last_cpu`` baseline keeps advancing under both engines so switching back
to ``'cpu'`` is lossless.

**Active threshold:** per-process deltas smaller than
``ACTIVE_THRESHOLD_SECONDS`` (1.0 CPU-second per 60s tick, i.e. processes
averaging under ~1.7% CPU) are treated as background noise and ignored.

**Wall-clock cap:** the charged duration is capped at
``MAX_TICK_WALL_SECONDS`` (5 minutes) so sleep/downtime gaps never bill a
burst of phantom hours on the first tick back.

**Display:** all user-facing times are whole minutes only — no seconds are
ever shown. Internal accounting keeps float seconds to avoid rounding drift.

Budgets reset at **bedtime end** (the True->False transition of the bedtime
window, detected in the tick loop). Users with no bedtime reset at
**midnight** (first tick whose calendar date differs from the stored day).

Enforcement uses the cgroup freezer (``parental-<username>-<category>``),
never signals. On exhaust the category cgroup is frozen; the parent can
resume manually, and everything auto-unfreezes at reset.
"""

import glob
import grp
import os
import pwd
import re
import shlex
import subprocess
import time
from datetime import datetime

import audit
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESS_PREFS_FILE = os.path.join(BASE_DIR, 'process_prefs.json')
BUDGETS_FILE = os.path.join(BASE_DIR, 'budgets.json')
OVERRIDES_FILE = os.path.join(BASE_DIR, 'overrides.json')
CGROUP_ROOT = '/sys/fs/cgroup'

CATEGORIES = ('games', 'browsers', 'media', 'chat', 'other')
CATEGORY_LABELS = {
    'games': 'Games',
    'browsers': 'Browsers',
    'media': 'Media',
    'chat': 'Chat',
    'other': 'Other',
}

# CPU seconds per 60s tick below which a process counts as background-idle
# noise and does not burn budget. See module docstring.
ACTIVE_THRESHOLD_SECONDS = 1.0
# Upper bound on wall-clock seconds billed for a single tick. Ticks normally
# land ~60s apart; the cap only bites after sleep/service downtime so the
# first tick back cannot bill phantom hours.
MAX_TICK_WALL_SECONDS = 300.0
TICK_SECONDS = 60
# Remaining budget seconds at or below which a "minutes left" warning is
# sent (once per day per category).
LOW_WARN_SECONDS = 300.0
# Minimum allowance for a weekly window (mirrors the bonus custom-entry
# floor: BONUS_CUSTOM_MIN = 5). A window below this is rejected/malformed.
WINDOW_MIN_MINUTES = 5

# ---------------------------------------------------------------------------
# Focus engine (per-user session agent) — SESSION_AGENT_PLAN.md §8/§9.
#
# The measured axis is per-category *focused* seconds from the child's
# session feed (``focus_feed``), replacing the CPU-delta activity gate. The
# CPU engine stays the default, so the whole focus path is inert until a
# parent opts a child in via ``focus_engine = 'focus'``.
# ---------------------------------------------------------------------------
FOCUS_ENGINE_CPU = 'cpu'
FOCUS_ENGINE_FOCUS = 'focus'
# Auto-degraded marker: the parent configured the focus engine but the feed
# is missing/stale/malformed. Billing is HALTED for that tick (used_seconds
# is left untouched — never silently billed as zero) and the state is
# surfaced; a fresh feed recovers automatically to FOCUS_ENGINE_FOCUS.
FOCUS_ENGINE_NO_DATA = 'no_data'
FOCUS_ENGINES = (FOCUS_ENGINE_CPU, FOCUS_ENGINE_FOCUS, FOCUS_ENGINE_NO_DATA)
# Engines that engage the feed-drain + focus-billing path.
FOCUS_FAMILY = (FOCUS_ENGINE_FOCUS, FOCUS_ENGINE_NO_DATA)
# Plan §8.4 spelling; accepted as an alias for the focus engine.
_FOCUS_ENGINE_ALIASES = {FOCUS_ENGINE_FOCUS: FOCUS_ENGINE_FOCUS,
                         'agent': FOCUS_ENGINE_FOCUS}

_USERNAME_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')
# Same charset the sudoers grant is anchored on
# (parental-[A-Za-z0-9_.-]+): reusing it means per-app cgroup names
# (parental-<user>-app-<comm>) need NO new privilege grant.
_COMM_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')


# ---------------------------------------------------------------------------
# Cgroup freezer ops (privileged via sudo tee; validated username/category)
# ---------------------------------------------------------------------------

def cgroup_path(username, category):
    if not valid_username(username) or not valid_category(category):
        raise ValueError('Invalid username or category for cgroup path')
    return os.path.join(CGROUP_ROOT, f'parental-{username}-{category}')


def cgroup_path_app(username, comm):
    """Path of the per-app group ``parental-<user>-app-<comm>``.

    ``valid_comm`` pins the same charset the sudoers grant is already
    anchored on (``parental-[A-Za-z0-9_.-]+``), so no privilege grant is
    needed for these groups.
    """
    if not valid_username(username) or not valid_comm(comm):
        raise ValueError('Invalid username or comm for app cgroup path')
    return os.path.join(CGROUP_ROOT, f'parental-{username}-app-{comm}')


def _sudo_read_file(path):
    try:
        result = subprocess.run(
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
    try:
        result = subprocess.run(
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
    if os.path.isdir(path):
        return True, ''
    try:
        result = subprocess.run(
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
                            lambda: ensure_cgroup(username, category))


def set_frozen_app(username, comm, frozen, actor='system'):
    """Freeze (True) or thaw (False) a per-app cgroup; verify by readback."""
    try:
        path = os.path.join(cgroup_path_app(username, comm), 'cgroup.freeze')
    except ValueError as e:
        return False, str(e)
    return _set_frozen_path(path, frozen, actor, f'{username}:app:{comm}',
                            lambda: ensure_cgroup_app(username, comm))


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
    if bucket.startswith(APP_BUCKET_PREFIX):
        comm = bucket[len(APP_BUCKET_PREFIX):]
        if valid_comm(comm):
            return 'app', comm
        return None
    if valid_category(bucket):
        return 'category', bucket
    return None


def thaw_bucket(username, bucket, actor='system'):
    """Thaw one managed bucket (category or app), dispatching by kind."""
    parts = split_bucket(bucket)
    if parts is None:
        return False, 'unknown cgroup bucket'
    kind, target = parts
    if kind == 'app':
        return set_frozen_app(username, target, False, actor=actor)
    return set_frozen(username, target, False, actor=actor)


def unfreeze_all_user(username, actor='system', keep_categories=None,
                      keep_apps=None):
    """Thaw every parental-<username>-* cgroup (category + app groups).

    keep_categories/keep_apps list targets governed by an ACTIVE weekly
    window: their groups are skipped so a day reset does not undo a
    window-exhaustion freeze (the window close thaws them instead).
    """
    if not valid_username(username):
        return False, 'Invalid username'
    keep_categories = set(keep_categories or ())
    keep_apps = set(keep_apps or ())
    results = []
    try:
        names = os.listdir(CGROUP_ROOT)
    except OSError as e:
        return False, str(e)
    prefix = f'parental-{username}-'
    for name in names:
        if not name.startswith(prefix):
            continue
        bucket = name[len(prefix):]
        parts = split_bucket(bucket)
        if parts is None:
            continue
        kind, target = parts
        if kind == 'category':
            if target in keep_categories:
                continue
            ok, msg = set_frozen(username, target, False, actor=actor)
        else:
            if target in keep_apps:
                continue
            ok, msg = set_frozen_app(username, target, False, actor=actor)
        results.append((bucket, ok, msg))
    audit.safe_log(actor, 'budget.unfreeze_all', username,
                   {'thawed': [b for b, ok, _m in results if ok]})
    return True, results


# ---------------------------------------------------------------------------
# Freezer watchdog: detect + self-heal stuck/orphan freezes (prototype)
# ---------------------------------------------------------------------------
# Incident (2026-10-09): a stale `parental-tester-quarantine` cgroup held an
# entire desktop session frozen (cgroup.freeze=1, 17 procs incl.
# kwin/pipewire/dbus) with no corresponding budget state. The desktop stayed
# black until manually thawed. This watchdog makes that class of incident
# self-heal on the next 60s tick.
#
# Ordering contract (no flapping vs. legitimate freezes): enforcement runs
# FIRST, watchdog SECOND, against the SAME in-memory budgets dict — so a
# category the current tick legitimately froze already has
# paused[category]=True set and the watchdog sees it as legitimate.
# `tick_budgets()` implements exactly this order (see below).
#
# Frozen means BOTH files agree: desired state `cgroup.freeze == '1'` AND
# effective state `cgroup.events` reports `frozen 1`. A cgroup mid-transition
# (freeze=1 written, events still `frozen 0` while processes settle) is
# skipped, never thawed — this avoids flapping with a freeze that is still
# landing. Any unreadable/ambiguous state is logged + skipped, never thawed
# blindly. Only UNFREEZE writes are ever issued, only on OUR
# `parental-<user>-<category>` and `parental-<user>-app-<comm>` groups.
# No signals, no kills, no membership changes anywhere.

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
    if not name.startswith('parental-'):
        return None
    rest = name[len('parental-'):]
    if '-' not in rest:
        return None
    username, last = rest.rsplit('-', 1)
    if valid_username(username) and valid_category(last):
        return username, last
    idx = rest.rfind('-' + APP_BUCKET_PREFIX)
    if idx > 0:
        username = rest[:idx]
        comm = rest[idx + len(APP_BUCKET_PREFIX) + 1:]
        if valid_username(username) and valid_comm(comm):
            return username, _app_bucket(comm)
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
    root = cgroup_root or CGROUP_ROOT
    if budgets_data is None:
        try:
            budgets_data = load_budgets()
        except Exception as e:
            note = f'WATCHDOG: could not load budget state, skipping pass: {e}'
            print(note, flush=True)
            return {'thawed': [], 'legit': [], 'clean': [], 'unknown': [],
                    'skipped': [], 'note': note}
    if not isinstance(budgets_data, dict):
        budgets_data = {}
    if thaw_fn is None:
        if root != CGROUP_ROOT:
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
            thaw_fn = thaw_bucket
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
        parts = split_bucket(bucket)
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
    try:
        uid = pwd.getpwnam(username).pw_uid
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
            if is_never_pause({'comm': comm, 'cmdline': read_cmdline(pid)}):
                return False, (f'PID {pid} ({comm}) is NEVER-pause; '
                               'aborting freeze')
            return False, (f'PID {pid} not in the freezable set; '
                            'aborting freeze')
        if is_never_pause(proc):
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
    visible, _hidden = apply_filter(procs, username, prefs)
    exclude = exclude_comms or frozenset()
    out = []
    for proc in visible:
        if categorize(proc, index) != category:
            continue
        if is_never_pause(proc):
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
    visible, _hidden = apply_filter(procs, username, prefs)
    out = []
    for proc in visible:
        if (proc.get('comm') or '') != comm:
            continue
        if is_never_pause(proc):
            continue
        out.append(proc)
    return out


# ---------------------------------------------------------------------------
# Focus engine plumbing (feed cursor, provider, per-tick aggregation)
# ---------------------------------------------------------------------------

def focus_engine_of(entry):
    """Normalized engine for a budget entry, failing closed to ``'cpu'``.

    An absent/unknown value means today's CPU behavior (never a silent
    engine switch). ``'no_data'`` is a focus-family engine: it keeps
    probing the feed and recovers to ``'focus'`` once fresh. ``'agent'`` is
    accepted as an alias of ``'focus'`` (SESSION_AGENT_PLAN.md §8.4).
    """
    raw = (entry or {}).get('focus_engine', FOCUS_ENGINE_CPU)
    if raw in _FOCUS_ENGINE_ALIASES:
        return FOCUS_ENGINE_FOCUS
    if raw in FOCUS_ENGINES:
        return raw
    return FOCUS_ENGINE_CPU


def boottime_now():
    """``CLOCK_BOOTTIME`` seconds (advances across suspend), or monotonic.

    Focus-feed intervals are expressed in boottime seconds; this is the
    panel-side ``now`` on that axis. The monotonic fallback only matters on
    platforms without ``CLOCK_BOOTTIME`` and keeps the module importable.
    """
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError, ValueError):
        return time.monotonic()


def focus_status(entry):
    """UI-facing focus-engine health (pure read; never bills/mutates).

    Additive helper so callers can surface "usage not being measured"
    without changing any existing endpoint or budget_status shape.
    """
    entry = entry or {}
    engine = focus_engine_of(entry)
    configured = engine in FOCUS_FAMILY
    return {
        'engine': engine,
        'configured': configured,
        'degraded': configured and (
            engine == FOCUS_ENGINE_NO_DATA
            or entry.get('feed_status') == 'no_data'),
        'feed_seq': entry.get('feed_seq'),
        'feed_last_seen': entry.get('feed_last_seen'),
        'feed_agent': entry.get('feed_agent'),
    }


def _feed_cursor_update(entry, parsed):
    """Advance an entry's feed watermark from a parse result, billing nothing.

    The cursor is a *drain position*, not a budget quantity: it moves even
    on locked ticks and on ticks where billing is halted, so rows at or
    below it are never accounted twice (mirrors the ``last_cpu`` baseline
    advance). Accepts any dict; silently ignores a non-dict result.
    """
    if not isinstance(parsed, dict):
        return
    last_seq = parsed.get('last_seq')
    if isinstance(last_seq, int) and not isinstance(last_seq, bool):
        try:
            cur = int(entry.get('feed_seq', 0))
        except (TypeError, ValueError):
            cur = 0
        if last_seq > cur:
            entry['feed_seq'] = last_seq
    heartbeat_bt = parsed.get('last_heartbeat_bt')
    newest = None
    agent = None
    for row in parsed.get('rows') or []:
        if not isinstance(row, dict):
            continue
        if agent is None and row.get('agent'):
            agent = row['agent']
        bt = row.get('bt')
        if isinstance(bt, (int, float)) and not isinstance(bt, bool):
            if newest is None or bt > newest.get('bt', -1):
                newest = row
    if agent is not None:
        entry['feed_agent'] = agent
    if (newest is not None and newest.get('ts') is not None
            and heartbeat_bt is not None and newest.get('bt') == heartbeat_bt):
        entry['feed_last_seen'] = newest['ts']
    boot = parsed.get('boot_id')
    if boot:
        entry['feed_boot_id'] = boot


def _locked_feed_bookkeeping(entry, parsed):
    """Locked tick: consume the locked window's feed rows without billing.

    The focus-engine analogue of ``_locked_tick_bookkeeping``: the cursor
    advances so the first tick after unlock bills only NEW focus sessions,
    never the locked window.
    """
    _feed_cursor_update(entry, parsed)


def _default_focus_provider(username, entry, now_bt):
    """Production feed provider: pull one child's feed via ``focus_feed``.

    A drain/transport failure yields the feed module's ``missing`` result
    (stale/degraded), so the caller halts billing instead of guessing zero.
    """
    import focus_feed
    feed_seq = entry.get('feed_seq', 0)
    try:
        uid = pwd.getpwnam(username).pw_uid
    except (KeyError, TypeError):
        return focus_feed.parse_feed(None, feed_seq=feed_seq)
    try:
        text, _err = focus_feed.drain_feed(username, uid)
    except Exception:
        text = None
    return focus_feed.parse_feed(text, feed_seq=feed_seq,
                                 expected_boot_id=entry.get('feed_boot_id'),
                                 now_bt=now_bt)


def _focus_billing(username, entry, procs, limits, prefs, index, window_bt,
                   provider, now_bt):
    """Fetch + validate the feed and return per-category focused seconds.

    Returns ``(seconds_by_category, info)``. ``info`` carries the surfacing
    fields (engine, billable, degraded, feed_seq, states, errors, stats).
    Never raises: a transport/provider failure or malformed/oversized/
    non-monotonic feed all fail closed (bill nothing, ``degraded`` True).
    """
    import focus_feed
    info = {'engine': FOCUS_ENGINE_FOCUS, 'billable': False,
            'degraded': True, 'seconds': {}, 'stats': {}, 'states': [],
            'errors': []}
    try:
        parsed = provider(username, entry, now_bt)
    except Exception as exc:                       # transport/provider boom
        info['errors'] = [str(exc)]
        return {}, info
    if not isinstance(parsed, dict):
        info['errors'] = ['provider returned no parse result']
        return {}, info
    _feed_cursor_update(entry, parsed)
    info['feed_seq'] = entry.get('feed_seq')
    info['states'] = list(parsed.get('states') or [])
    info['errors'] = list(parsed.get('errors') or [])
    if (parsed.get('too_large') or parsed.get('missing')
            or parsed.get('stale') or parsed.get('degraded')):
        return {}, info
    try:
        agg = focus_feed.aggregate_focus(
            parsed, window_bt, procs=procs, limits=limits, username=username,
            prefs=prefs, index=index, now_bt=now_bt)
    except focus_feed.FeedError as exc:
        info['errors'] = info['errors'] + [str(exc)]
        return {}, info
    except Exception as exc:
        info['errors'] = info['errors'] + [str(exc)]
        return {}, info
    info['billable'] = bool(agg.get('billable'))
    info['degraded'] = bool(agg.get('degraded'))
    info['seconds'] = dict(agg.get('seconds') or {})
    info['stats'] = dict(agg.get('stats') or {})
    if not info['billable']:
        return {}, info
    return info['seconds'], info


# ---------------------------------------------------------------------------
# Tick: accounting + reset + enforcement (called every 60s)
# ---------------------------------------------------------------------------

def _clear_window_target_freeze(entry, window, username, act, summary):
    """Clear a window target's freeze flags on an open/close transition.

    On open: fresh allowance. On close: auto-resume. Pops the auto-pause
    and resume-inhibit flags and (when acting) thaws the target's cgroup
    so the daily governor or the next window re-evaluates cleanly.
    """
    target = window['target']
    if window['target_type'] == 'category':
        entry.setdefault('paused', {}).pop(target, None)
        entry.setdefault('resumed', {}).pop(target, None)
        if act:
            ok, msg = set_frozen(username, target, False)
            if not ok:
                summary['note'] = f'window thaw failed for {target}: {msg}'
    else:
        entry.setdefault('app_paused', {}).pop(target, None)
        entry.setdefault('app_resumed', {}).pop(target, None)
        if act:
            ok, msg = set_frozen_app(username, target, False)
            if not ok:
                summary['note'] = (f'window thaw failed for app:'
                                   f'{target}: {msg}')


# _audit_auto_pause_failed dedup state (FIX-3): 'user:tag' -> last logged
# (stage, reason). Same once-per-change semantics as the summary emitter.
_LAST_AUTO_PAUSE_SIGS = {}


def _audit_auto_pause_failed(username, tag, stage, reason, state=None):
    """Audit budget.auto_pause_failed (FIX-3, TIME_TRACKING_QA.md F8).

    Mirrors the manual-path audit hooks in _set_frozen_path: every auto
    freeze attempt that aborts (ensure/verify/write) is logged, but an
    IDENTICAL persistent failure is logged once per change, not 1440
    times a day (the exhaustion branch re-evaluates every 60s tick).
    """
    if state is None:
        state = _LAST_AUTO_PAUSE_SIGS
    target = f'{username}:{tag}'
    sig = (stage, reason)
    if state.get(target) == sig:
        return
    state[target] = sig
    audit.safe_log('system', 'budget.auto_pause_failed', target,
                   {'ok': False, 'stage': stage, 'reason': reason})


def _summary_signature(summary):
    """The observable enforcement fields of a tick summary, in a tuple."""
    return (summary.get('note') or '',
            tuple(summary.get('froze') or ()),
            tuple(summary.get('unfroze') or ()),
            bool(summary.get('degraded')),
            tuple(summary.get('skipped') or ()))


def summary_line(summary):
    """One journal line for a tick summary; '' when there is nothing to say.

    FIX-3 (TIME_TRACKING_QA.md F8): before this, both production tick
    callers discarded the per-user summaries, so an enforcement failure
    that repeated every 60s produced zero journal output.
    """
    note, froze, unfroze, degraded, skipped = _summary_signature(summary)
    if not (note or froze or unfroze or degraded or skipped):
        return ''
    parts = [f'budget-tick {summary.get("user", "?")}']
    if note:
        parts.append(f'note={note}')
    if froze:
        parts.append('froze=' + ','.join(froze))
    if unfroze:
        parts.append('unfroze=' + ','.join(unfroze))
    if degraded:
        parts.append('degraded')
    if skipped:
        parts.append('skipped=' + ','.join(skipped))
    return ' '.join(parts)


# emit_tick_summaries dedup state (FIX-3): user -> last emitted signature.
# Process-local by design: the app thread logs transitions, the per-minute
# timer process (fresh state) logs every repeating failure.
_LAST_SUMMARY_SIGS = {}


def emit_tick_summaries(summaries, state=None):
    """Print per-user tick summaries worth journaling; return the lines.

    One line per user whose note/froze/unfroze/degraded/skipped CHANGED
    since the previous emit (``state`` is a {user: signature} map; the
    module-level default persists for the long-running app thread, while
    the timer process starts fresh every run so a repeating failure keeps
    surfacing there — journald rate-limits, silence does not). Callers:
    enforce_bedtime_timer.py and app.py bedtime_loop (FIX-3).
    """
    if state is None:
        state = _LAST_SUMMARY_SIGS
    lines = []
    for summary in summaries or []:
        user = summary.get('user', '?')
        sig = _summary_signature(summary)
        if not any(sig):
            continue
        if state.get(user) == sig:
            continue
        state[user] = sig
        line = summary_line(summary)
        if line:
            print(line, flush=True)
            lines.append(line)
    return lines


def _enforce_target(username, t, wall, deltas, summary, act):
    """Shared billing + freeze state machine for one target.

    ``t`` bundles: limit_sec, used, used_key, paused, paused_key, resumed,
    resumed_key, manual, manual_key, members, ensure, migrate,
    get_members, set_frozen, label, tag, warned_low, low_key, today_str,
    notify_low() -> bool, notify_paused(). The branches mirror the
    original per-category tick exactly, so with no app override or window
    configured the behavior is unchanged.
    """
    used = t['used']
    key = t['used_key']
    limit_sec = t['limit_sec']
    used[key] = float(used.get(key, 0.0) or 0.0)
    members = t['members']
    # An active target burns the whole tick's wall-clock duration; an idle
    # one burns nothing — usage tracks the Elapsed time parents see.
    # Under the focus engine the caller supplies ``bill_seconds`` (focused
    # seconds aggregated from the session feed) and the CPU gate is skipped
    # entirely: 0.0 halts billing without the wall-clock charge.
    if 'bill_seconds' in t:
        used[key] += max(0.0, float(t['bill_seconds']))
    elif any(deltas.get(p['pid'], 0.0) >= ACTIVE_THRESHOLD_SECONDS
             for p in members):
        used[key] += wall
    exhausted = used[key] >= limit_sec
    remaining = limit_sec - used[key]
    if (not exhausted and 0 < remaining <= LOW_WARN_SECONDS
            and t['warned_low'].get(t['low_key']) != t['today_str']):
        if t['notify_low']():
            t['warned_low'][t['low_key']] = t['today_str']

    paused = t['paused']
    resumed = t['resumed']
    manual = t['manual']
    pkey = t['paused_key']
    if (exhausted and not paused.get(pkey)
            and not resumed.get(t['resumed_key'])
            and not manual.get(t['manual_key'])):
        if not act:
            paused[pkey] = True
            summary['froze'].append(t['tag'] + ' (dry-run)')
            t['notify_paused']()
            return
        ok, msg = t['ensure']()
        if not ok:
            summary['note'] = f"ensure cgroup: {msg}"
            _audit_auto_pause_failed(username, t['tag'],
                                     'ensure-cgroup', msg)
            return
        for proc in members:
            t['migrate'](proc['pid'])
        expected = {p['pid']: p for p in members}
        current = t['get_members']()
        safe, reason = verify_freeze_safety(username, current, expected)
        if not safe:
            summary['note'] = f"freeze aborted for {t['label']}: {reason}"
            _audit_auto_pause_failed(username, t['tag'],
                                     'verify-freeze-safety', reason)
            return
        if not members and not current:
            paused[pkey] = True  # exhausted, nothing running yet
            t['notify_paused']()
            return
        if not members:
            summary['note'] = (f"freeze aborted for {t['label']}: "
                               'cgroup has members but nothing is freezable')
            _audit_auto_pause_failed(
                username, t['tag'], 'verify-freeze-safety',
                'cgroup has members but nothing is freezable')
            return
        ok, msg = t['set_frozen'](True)
        if ok:
            paused[pkey] = True
            summary['froze'].append(t['tag'])
            t['notify_paused']()
        else:
            summary['note'] = f"freeze failed for {t['label']}: {msg}"
            _audit_auto_pause_failed(username, t['tag'], 'freeze-write', msg)
    elif exhausted and paused.get(pkey) and members and act:
        # Exhausted earlier while nothing was running: a matching app
        # launched since. Migrate it in and (re-)freeze.
        ok, _msg = t['ensure']()
        if not ok:
            # FIX-3: was a silent discard (_msg was dropped entirely).
            summary['note'] = f"ensure cgroup (re-freeze): {_msg}"
            _audit_auto_pause_failed(username, t['tag'],
                                     'ensure-cgroup-re-freeze', _msg)
        else:
            current = set(t['get_members']())
            for proc in members:
                if proc['pid'] not in current:
                    t['migrate'](proc['pid'])
            expected = {p['pid']: p for p in members}
            safe, reason = verify_freeze_safety(
                username, t['get_members'](), expected)
            if safe:
                ok, msg = t['set_frozen'](True)
                if ok:
                    summary['froze'].append(t['tag'] + ' (late start)')
                else:
                    summary['note'] = (
                        f"re-freeze failed for {t['label']}: {msg}")
                    _audit_auto_pause_failed(username, t['tag'],
                                             're-freeze-write', msg)
            else:
                summary['note'] = (
                    f"re-freeze aborted for {t['label']}: {reason}")
                _audit_auto_pause_failed(username, t['tag'],
                                         're-freeze-verify', reason)
    elif manual.get(t['manual_key']) and act:
        # Manually-frozen steady state: re-assert for late starters only.
        ok, _msg = t['ensure']()
        if not ok:
            summary['note'] = f"ensure cgroup (manual): {_msg}"
            _audit_auto_pause_failed(username, t['tag'],
                                     'ensure-cgroup-manual', _msg)
        else:
            current = set(t['get_members']())
            for proc in members:
                if proc['pid'] not in current:
                    t['migrate'](proc['pid'])
            expected = {p['pid']: p for p in members}
            safe, reason = verify_freeze_safety(
                username, t['get_members'](), expected)
            if safe:
                ok, msg = t['set_frozen'](True)
                if ok:
                    summary['froze'].append(t['tag'] + ' (manual)')
                else:
                    summary['note'] = (
                        f"manual re-freeze failed for {t['label']}: {msg}")
                    _audit_auto_pause_failed(username, t['tag'],
                                             'manual-re-freeze-write', msg)
            else:
                summary['note'] = (
                    f"manual re-freeze aborted for {t['label']}: {reason}")
                _audit_auto_pause_failed(username, t['tag'],
                                         'manual-re-freeze-verify', reason)
    elif members and act and not paused.get(pkey):
        # Pre-place matching processes so a later freeze is instant.
        ok, _msg = t['ensure']()
        if ok:
            current = set(t['get_members']())
            for proc in members:
                if proc['pid'] not in current:
                    t['migrate'](proc['pid'])


def account_and_enforce(username, entry, procs, bedtime_entry, now,
                        prefs=None, index=None, act=True, notify=None,
                        overrides=None, focus_provider=None, now_bt=None):
    """One budget tick for a single user. Mutates `entry` in place.

    ``overrides`` is this user's config doc
    (``{"app_overrides": ..., "windows": ...}``) or None/empty; absent
    config reproduces the pre-feature behavior exactly.

    ``focus_provider`` (``focus_provider(username, entry, now_bt)`` ->
    focus_feed parse result) and ``now_bt`` (``CLOCK_BOOTTIME`` seconds)
    drive the focus engine (SESSION_AGENT_PLAN.md §9). Both are ignored
    unless the entry's ``focus_engine`` is a focus-family value, so the
    default CPU behavior is byte-identical.

    Precedence (most specific wins): app exemption > app allowance (its
    own active window, else its daily override) > category active window
    > category daily limit. An active target window replaces the daily
    allowance entirely (limit AND reset timing).

    Returns a summary dict {reset, froze: [...], notified: [...], note}.
    With act=False no privileged cgroup operations are performed (used by
    unit tests). notify, when given, is a callable
    ``notify(username, title, body)`` used for best-effort warnings.
    """
    today_str = now.date().isoformat()
    has_bedtime = bool(bedtime_entry and bedtime_entry.get('start'))
    now_in = bool(bedtime_entry and _in_bedtime(bedtime_entry, now))
    was_in = bool(entry.get('was_in_bedtime', False))
    summary = {'user': username, 'reset': False, 'froze': [],
               'unfroze': [], 'notified': [], 'note': ''}

    def _warn(title, body, tag):
        """Best-effort notification; records tag in summary, never raises."""
        if notify is None:
            return False
        try:
            notify(username, title, body)
            summary['notified'].append(tag)
        except Exception as e:
            summary['note'] = 'notify failed: %s' % e
            return False
        return True

    if is_admin_protected(username):
        # Admin self-protection: never auto-freeze a wheel member's apps.
        note = (f'skipped {username}: {ADMIN_GROUP} member '
                '(admin self-protection)')
        print(note, flush=True)
        summary['note'] = note
        return summary

    # ---- weekly-window lifecycle: open/close transitions ----
    window_used = entry.setdefault('window_used', {})
    window_active = entry.setdefault('window_active', {})
    configured_windows = [w for _wid, w in iter_windows(overrides)]
    active_windows = []
    for win in configured_windows:
        wid = win['id']
        was_active = bool(window_active.get(wid, False))
        is_active = bool(win['enabled'] and _in_window(win, now))
        if is_active:
            active_windows.append(win)
        if is_active and not was_active:
            window_used[wid] = 0.0            # open: fresh allowance
            _clear_window_target_freeze(entry, win, username, act, summary)
        elif was_active and not is_active:
            window_used[wid] = 0.0            # close: auto-resume
            _clear_window_target_freeze(entry, win, username, act, summary)
        window_active[wid] = is_active

    if entry.get('day') is None:
        entry['day'] = today_str
    if budget_reset_needed(entry.get('day'), has_bedtime, was_in, now_in,
                           today_str):
        entry['used_seconds'] = {}
        entry['paused'] = {}
        entry['resumed'] = {}
        entry['manual_frozen'] = {}
        entry['manual_frozen_at'] = {}
        entry['warned_low'] = {}
        entry['warned_paused'] = {}
        entry['app_used'] = {}
        entry['app_paused'] = {}
        entry['app_resumed'] = {}
        entry['app_manual_frozen'] = {}
        entry['app_manual_frozen_at'] = {}
        entry['app_warned_low'] = {}
        entry['app_warned_paused'] = {}
        entry['day'] = today_str
        # window_used is NOT cleared for active windows (spans midnight).
        for wid in list(window_used):
            if not window_active.get(wid, False):
                window_used.pop(wid, None)
        summary['reset'] = True
        if act:
            keep_cats = {w['target'] for w in active_windows
                         if w['target_type'] == 'category'}
            keep_apps = {w['target'] for w in active_windows
                         if w['target_type'] == 'app'}
            if keep_cats or keep_apps:
                ok, _res = unfreeze_all_user(
                    username, keep_categories=keep_cats,
                    keep_apps=keep_apps)
            else:
                ok, _res = unfreeze_all_user(username)
            summary['unfroze'].append('all' if ok else 'failed')
    entry['was_in_bedtime'] = now_in

    limits = entry.get('limits', {}) or {}
    app_overrides = override_map(overrides)
    if not limits and not configured_windows and not app_overrides:
        entry['last_cpu'] = {}
        return summary

    deltas, new_last = compute_deltas(entry.get('last_cpu', {}), procs)
    entry['last_cpu'] = new_last
    # Wall-clock seconds since the previous tick. First tick (or unreadable
    # timestamp) bills nothing — baselines only. Capped so sleep/downtime
    # gaps cannot bill phantom hours.
    prev_iso = entry.get('last_tick')
    if prev_iso:
        try:
            wall = (now - datetime.fromisoformat(prev_iso)).total_seconds()
        except (ValueError, TypeError):
            wall = 0.0
    else:
        wall = 0.0
    wall = max(0.0, min(wall, MAX_TICK_WALL_SECONDS))
    entry['last_tick'] = now.isoformat()
    used = entry.setdefault('used_seconds', {})
    paused = entry.setdefault('paused', {})
    resumed = entry.setdefault('resumed', {})
    manual_frozen = entry.setdefault('manual_frozen', {})
    warned_low = entry.setdefault('warned_low', {})
    warned_paused = entry.setdefault('warned_paused', {})
    app_used = entry.setdefault('app_used', {})
    app_paused = entry.setdefault('app_paused', {})
    app_resumed = entry.setdefault('app_resumed', {})
    app_manual_frozen = entry.setdefault('app_manual_frozen', {})
    app_warned_low = entry.setdefault('app_warned_low', {})
    app_warned_paused = entry.setdefault('app_warned_paused', {})

    def _low_note(warned_map, key, label):
        def fn():
            return _warn('%s: 5 minutes left' % label,
                         'Less than 5 minutes of %s time left today.' % label,
                         key + ' (low)')
        return fn

    def _paused_note(warned_map, key, label):
        def fn():
            if warned_map.get(key) == today_str:
                return
            if _warn('%s paused \u2014 out of time' % label,
                     'Your %s time is up for today.' % label,
                     key + ' (paused)'):
                warned_map[key] = today_str
        return fn

    governed_comms = set(app_overrides)
    win_by_category = {w['target']: w for w in active_windows
                       if w['target_type'] == 'category'}

    # ---- focus engine: bill focused seconds instead of CPU deltas ----
    # The CPU path is untouched when focus_seconds stays None (default
    # ``focus_engine='cpu'``), so a rollback is byte-identical.
    focus_seconds = None
    if focus_engine_of(entry) in FOCUS_FAMILY:
        if now_bt is None:
            now_bt = boottime_now()
        provider = focus_provider or _default_focus_provider
        # Same tick span the CPU engine would bill, on the boottime axis.
        window_bt = (now_bt - wall, now_bt)
        # A category window may carry an allowance with no daily limit;
        # fold it in so the freezable gate ("only bill what you can
        # freeze") still sees the category as billable.
        focus_limits = dict(limits)
        for cat, win in win_by_category.items():
            try:
                focus_limits[cat] = float(win['allowance_minutes'])
            except (TypeError, ValueError):
                pass
        focus_seconds, focus_info = _focus_billing(
            username, entry, procs, focus_limits, prefs, index, window_bt,
            provider, now_bt)
        if focus_info.get('billable'):
            entry['focus_engine'] = FOCUS_ENGINE_FOCUS
            entry['feed_status'] = 'ok'
        else:
            # Missing/stale/malformed feed: HALT billing (used_seconds is
            # left untouched — never silently billed as zero) and surface
            # ``no_data``. A fresh feed recovers on the next tick.
            entry['focus_engine'] = FOCUS_ENGINE_NO_DATA
            entry['feed_status'] = 'no_data'
            focus_seconds = {}
            summary['degraded'] = True
        summary['focus'] = focus_info

    # ---- category targets (window replaces the daily limit) ----
    ordered = list(limits.keys())
    for cat in win_by_category:
        if cat not in ordered:
            ordered.append(cat)
    for category in ordered:
        if category not in CATEGORIES:
            continue
        members = _visible_category_members(
            procs, username, category, prefs, index,
            exclude_comms=governed_comms)
        label = CATEGORY_LABELS.get(category, category)
        win = win_by_category.get(category)
        if win is not None:
            limit_sec = float(win['allowance_minutes']) * 60.0
            used_map, used_key = window_used, win['id']
        else:
            try:
                limit_sec = float(limits.get(category, 0) or 0) * 60.0
            except (TypeError, ValueError):
                continue
            if limit_sec <= 0:
                continue
            used_map, used_key = used, category
        target = {
            'limit_sec': limit_sec,
            'used': used_map, 'used_key': used_key,
            'paused': paused, 'paused_key': category,
            'resumed': resumed, 'resumed_key': category,
            'manual': manual_frozen, 'manual_key': category,
            'members': members,
            'ensure': lambda c=category: ensure_cgroup(username, c),
            'migrate': lambda pid, c=category: migrate_pid(username, c, pid),
            'get_members': lambda c=category: cgroup_members(username, c),
            'set_frozen': lambda fr, c=category: set_frozen(username, c, fr),
            'label': label, 'tag': category,
            'warned_low': warned_low, 'low_key': category,
            'today_str': today_str,
            'notify_low': _low_note(warned_low, category, label),
            'notify_paused': _paused_note(warned_paused, category, label),
        }
        if focus_seconds is not None:
            # Focus engine: bill this category's focused seconds (0.0 when
            # the feed is degraded/missing -> billing halted for this tick).
            target['bill_seconds'] = float(focus_seconds.get(category, 0.0)
                                           or 0.0)
        _enforce_target(username, target, wall, deltas, summary, act)

    # ---- app targets ----
    for comm, ov in sorted(app_overrides.items()):
        if ov['mode'] == 'exempt':
            continue  # unlimited: never billed, never placed in a cgroup
        win = active_window(overrides, 'app', comm, now)
        members = freezable_app_members(procs, username, comm, prefs)
        if win is not None:
            limit_sec = float(win['allowance_minutes']) * 60.0
            used_map, used_key = window_used, win['id']
        else:
            limit_sec = float(ov['minutes']) * 60.0
            used_map, used_key = app_used, comm
        _enforce_target(username, {
            'limit_sec': limit_sec,
            'used': used_map, 'used_key': used_key,
            'paused': app_paused, 'paused_key': comm,
            'resumed': app_resumed, 'resumed_key': comm,
            'manual': app_manual_frozen, 'manual_key': comm,
            'members': members,
            'ensure': lambda c=comm: ensure_cgroup_app(username, c),
            'migrate': lambda pid, c=comm: migrate_pid_app(username, c, pid),
            'get_members': lambda c=comm: cgroup_members_app(username, c),
            'set_frozen': lambda fr, c=comm: set_frozen_app(username, c, fr),
            'label': comm, 'tag': 'app:' + comm,
            'warned_low': app_warned_low, 'low_key': comm,
            'today_str': today_str,
            'notify_low': _low_note(app_warned_low, comm, comm),
            'notify_paused': _paused_note(app_warned_paused, comm, comm),
        }, wall, deltas, summary, act)

    return summary


def _locked_tick_bookkeeping(entry, procs, now, bedtime_entry):
    """Advance a locked account's accounting baselines, billing nothing.

    Mirrors the baseline half of account_and_enforce (was_in_bedtime
    tracking, the compute_deltas last_cpu advance, and the last_tick
    wall-clock base) so the first tick after unlock bills only NEW CPU
    activity, never the locked window. No billing, no pause/notify/freeze
    decisions — already-paused/frozen state is unchanged and simply
    persists.
    """
    entry['was_in_bedtime'] = bool(
        bedtime_entry and _in_bedtime(bedtime_entry, now))
    if not entry.get('limits'):
        # Mirrors account_and_enforce: no configured limits -> drop the
        # baseline (and leave last_tick alone, exactly like that path).
        entry['last_cpu'] = {}
        return
    entry['last_cpu'] = compute_deltas(
        entry.get('last_cpu', {}), procs)[1]
    entry['last_tick'] = now.isoformat()


def tick_desktop_index(username):
    """Per-user .desktop categorization index with a short TTL cache.

    The tick must classify like the UI does (the /user page builds a
    fresh ``build_desktop_index(username)`` per render; the tick used to
    pass ``index=None``, so any app whose only category signal is its
    .desktop file billed to 'other' while the parent watched it under
    Games — TIME_TRACKING_QA.md F6, §7 FIX-2). The tick runs every 60s
    per user, so the glob result is cached per user for
    ``_DESKTOP_INDEX_TTL_SECONDS``.
    """
    now = time.monotonic()
    hit = _DESKTOP_INDEX_CACHE.get(username)
    if hit is not None and now - hit[0] < _DESKTOP_INDEX_TTL_SECONDS:
        return hit[1]
    try:
        index = build_desktop_index(username)
    except OSError:
        index = {}
    _DESKTOP_INDEX_CACHE[username] = (now, index)
    return index


def tick_budgets(bedtime_map, now, procs_provider=None, prefs=None,
                 index=None, act=True, notify=None, run_watchdog=True,
                 cgroup_root=None, account_locked=None, focus_provider=None,
                 now_bt=None):
    """Run one budget tick for every user in the budget store.

    bedtime_map: {username: bedtime_entry} (from the app's bedtime loader).
    procs_provider: callable(username) -> procs (default: live ps snapshot).
    notify: optional callable ``notify(username, title, body)`` for
      best-effort "minutes left" / "paused" warnings.
    index: categorization index applied to every user. Default None now
      builds the per-user .desktop index (tick_desktop_index, cached) so
      the tick groups processes exactly like the UI does (FIX-2); tests
      and callers that want a fixed index pass one explicitly.
    run_watchdog: when True (default), run the freezer watchdog AFTER
      enforcement against the same in-memory state, so categories this
      tick legitimately froze already carry paused=True and are never
      mistaken for stuck. The watchdog summary is appended as a
      {'user': '_watchdog', ...} entry. Both the app thread and the
      systemd timer path go through here, so both self-heal.
    cgroup_root: alternate cgroup tree for the watchdog (tests only;
      default: real /sys/fs/cgroup).
    account_locked: optional callable ``account_locked(username) -> bool``
      reporting whether the account is locked (usermod -L: manual Lock,
      Lock Screen, bedtime auto-lock — everything get_lock_status sees).
      While locked the child cannot reach the app, so that user's tick
      bills nothing and no pause/notify/minutes-left decisions are made;
      the accounting baselines still advance exactly as if billed, so the
      first tick after unlock bills only NEW activity, never the locked
      window. Default None (or absent) = every user unlocked, preserving
      the pure-module behavior.
    focus_provider: optional callable
      ``focus_provider(username, entry, now_bt) -> focus_feed parse
      result``, consulted only for users whose ``focus_engine`` is a
      focus-family value. Default None -> the real drain-based provider
      (``_default_focus_provider``). Tests inject a fake.
    now_bt: ``CLOCK_BOOTTIME`` seconds sampled once for the whole tick
      (tests inject a fixed value; default sampled here). Only the focus
      engine uses it.
    Returns a list of per-user summary dicts (+ watchdog entry).
    """
    data = load_budgets()
    overrides_map = load_overrides()
    if prefs is None:
        prefs = load_prefs()
    if now_bt is None:
        now_bt = boottime_now()
    changed = True  # usage baselines always advance; persist every tick
    summaries = []
    usernames = [u for u in data if valid_username(u)]
    # A user may have only an overrides config (app override / window)
    # with no category limits yet: give them a blank budget entry so the
    # tick governs them too.
    for u in overrides_map:
        if u not in data and valid_username(u):
            data[u] = blank_entry()
            usernames.append(u)
    for username in usernames:
        entry = data[username]
        user_overrides = overrides_map.get(username)
        if is_admin_protected(username):
            # Admin self-protection: not even a live `ps` snapshot is taken
            # for wheel members — skip entirely with a logged note.
            note = (f'skipped budget tick for {username}: {ADMIN_GROUP} '
                    'member (admin self-protection)')
            print(note, flush=True)
            summaries.append({'user': username, 'reset': False, 'froze': [],
                              'unfroze': [], 'note': note})
            continue
        for key in ('limits', 'used_seconds', 'last_cpu', 'paused', 'resumed',
                    'manual_frozen', 'manual_frozen_at',
                    'warned_low', 'warned_paused',
                    'app_used', 'app_paused', 'app_resumed',
                    'app_manual_frozen', 'app_manual_frozen_at',
                    'app_warned_low', 'app_warned_paused',
                    'window_used', 'window_active'):
            entry.setdefault(key, {})
        if procs_provider is not None:
            procs = procs_provider(username)
        else:
            procs = list_user_processes(username)
        if account_locked is not None and account_locked(username):
            # Locked account: the child cannot reach the app, so this tick
            # bills nothing and makes no pause/notify decisions —
            # already-paused/frozen state simply persists. Baselines
            # advance exactly as if billed, so the first tick after unlock
            # bills only NEW CPU, never the locked window.
            _locked_tick_bookkeeping(
                entry, procs, now, (bedtime_map or {}).get(username))
            # Focus engine: consume the locked window's feed rows (cursor
            # advance) so the first tick after unlock bills only NEW focus.
            if focus_engine_of(entry) in FOCUS_FAMILY:
                provider = focus_provider or _default_focus_provider
                try:
                    parsed = provider(username, entry, now_bt)
                except Exception:
                    parsed = None
                _locked_feed_bookkeeping(entry, parsed)
            summaries.append({'user': username, 'reset': False, 'froze': [],
                              'unfroze': [], 'notified': [], 'note':
                              f'skipped {username}: account locked'})
            continue
        summaries.append(account_and_enforce(
            username, entry, procs, (bedtime_map or {}).get(username),
            now, prefs,
            index if index is not None else tick_desktop_index(username),
            act, notify, overrides=user_overrides,
            focus_provider=focus_provider, now_bt=now_bt))
    if changed:
        try:
            save_budgets(data)
        except OSError as e:
            # FIX-3 (TIME_TRACKING_QA.md F8): a swallowed save error means
            # every enforcement decision this tick made evaporates before
            # the watchdog re-reads the store — say so loudly.
            print(f'BUDGETS SAVE FAILED: {e}', flush=True)
            audit.safe_log('system', 'budget.save_failed', '',
                           {'ok': False, 'error': str(e)})
    if run_watchdog:
        # Enforcement first, watchdog second, same in-memory `data`: a
        # category frozen above already has paused=True, so it reads as
        # legitimate and is never thawed back (no flapping).
        try:
            w = watchdog_tick(data, cgroup_root=cgroup_root or CGROUP_ROOT)
        except Exception as e:
            print(f'WATCHDOG: pass failed: {e}', flush=True)
            w = {'thawed': [], 'legit': [], 'clean': [], 'unknown': [],
                 'skipped': [], 'note': f'pass failed: {e}'}
        summaries.append(dict({'user': '_watchdog'}, **w))
    return summaries


def freeze_category(username, category, actor='system'):
    """Manual Freeze: freeze a category cgroup on the parent's click.

    Records ``manual_frozen[category]`` (plus ``manual_frozen_at``) so the
    tick re-asserts the freeze for late starters and the watchdog treats
    it as legitimate. ``paused`` is left untouched — a manual freeze is
    not an exhaustion, and unfreezing a dually-marked category clears
    both. Fail-closed throughout: any cgroup/safety failure returns
    ``(False, reason)`` WITHOUT persisting ``manual_frozen``.

    Uses only the existing privileged helpers (ensure_cgroup,
    migrate_pid, set_frozen, cgroup_members) — no new privilege grants.
    """
    if not valid_username(username):
        return False, 'Invalid username'
    if not valid_category(category):
        return False, f'Unknown category: {category}'
    if is_admin_protected(username):
        return False, (f'Refused: {username} is an admin and cannot '
                       'be frozen')
    data = load_budgets()
    entry = data.get(username, {})
    if float((entry.get('limits', {}) or {}).get(category, 0) or 0) <= 0:
        return False, 'Set a time limit for this category first'
    limits = entry.get('limits', {})
    try:
        prefs = load_prefs()
    except Exception:
        prefs = None
    try:
        index = build_desktop_index(username)
    except Exception:
        index = None
    try:
        procs = list_user_processes(username)
    except Exception as e:
        return False, f'could not snapshot processes: {e}'
    members = freezable_members(procs, username, category, limits, prefs,
                                index)
    ok, msg = ensure_cgroup(username, category)
    if not ok:
        return False, msg
    for proc in members:
        # Individual migrate failures are ignored: the readback safety
        # gate below is authoritative (matches the tick's ignore-and-verify
        # order).
        migrate_pid(username, category, proc['pid'])
    current = cgroup_members(username, category)
    safe, reason = verify_freeze_safety(username, current,
                                        {p['pid']: p for p in members})
    if not safe:
        return False, reason
    if not members and not current:
        # Nothing running yet: record the freeze without writing
        # cgroup.freeze. The tick's manual-frozen branch freezes late
        # starters under the same gates.
        pass
    else:
        ok, msg = set_frozen(username, category, True, actor=actor)
        if not ok:
            return False, msg
    try:
        data = load_budgets()
        entry = data.setdefault(username, blank_entry())
        entry.setdefault('manual_frozen', {})[category] = True
        entry.setdefault('manual_frozen_at', {})[category] = \
            datetime.now().isoformat()
        save_budgets(data)
    except OSError as e:
        return False, str(e)
    label = CATEGORY_LABELS.get(category, category)
    return True, f'Frozen {label} (manual freeze)'


def unfreeze_category(username, category, actor='system'):
    """Manual Unfreeze: thaw and inhibit re-freeze until the next reset.

    Subsumes the old Resume: clears BOTH ``paused`` and ``manual_frozen``
    (plus ``manual_frozen_at``) and sets ``resumed`` so the tick does not
    instantly re-freeze an exhausted category.
    """
    if not valid_username(username):
        return False, 'Invalid username'
    if not valid_category(category):
        return False, f'Unknown category: {category}'
    ok, msg = set_frozen(username, category, False, actor=actor)
    if not ok:
        return False, msg
    try:
        data = load_budgets()
        entry = data.setdefault(username, blank_entry())
        entry.get('paused', {}).pop(category, None)
        entry.get('manual_frozen', {}).pop(category, None)
        entry.get('manual_frozen_at', {}).pop(category, None)
        entry.setdefault('resumed', {})[category] = True
        save_budgets(data)
    except OSError as e:
        return False, str(e)
    audit.safe_log(actor, 'budget.resume', f'{username}:{category}',
                   {'ok': True})
    return True, 'Resumed; will not re-pause until the next reset'


def resume_category(username, category, actor='system'):
    """Manual Resume: unfreeze and inhibit re-freeze until the next reset.

    Thin alias over :func:`unfreeze_category` (kept so the existing
    ``/api/budgets/resume`` endpoint and CLI-adjacent callers keep
    working with the exact same return contract).
    """
    return unfreeze_category(username, category, actor=actor)


# ---------------------------------------------------------------------------
# Bedtime-window check, local copy so this module stays stdlib-only and
# importable without the Flask app (app.py passes its own bedtime map in).
# ---------------------------------------------------------------------------

def _parse_hhmm(value):
    try:
        hours, minutes = value.strip().split(':')
        h, m = int(hours), int(minutes)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return None


def _in_window(entry, now):
    """Recurring day-of-week + HH:MM range check with overnight wrap.

    Used for both bedtime and weekly budget windows (identical temporal
    model). Returns False for a missing/malformed entry or ``start ==
    end``.
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


def _in_bedtime(entry, now):
    return _in_window(entry, now)


# ---------------------------------------------------------------------------
# Named re-exports — the budgets facade surface (RESTRUCTURE_PLAN.md §3.2).
#
# budgets.py stays the ONE module object the runtime and the 40 test modules
# bind to, and the ONLY module `mock.patch` targets (490 sites). Every name
# moved out is therefore re-exported here BY NAME — never a wildcard — with
# the consumer that needs it named in the comment. Concern modules never
# import from here at module level; they resolve `budgets.<name>` at CALL
# time so a patch on this module still reaches them (plan §1.1).
#
# Code still DEFINED in this file keeps calling the bare name: this module's
# own global binding IS the attribute the suite patches, so it needs no
# rewrite.
# ---------------------------------------------------------------------------

# budgets_format.py — canonical duration display. Needs: agent.py, app.py,
# the session/focus paths, 2 test modules (`test_process_grouping`,
# `test_budgets`).
from budgets_format import (  # noqa: E402
    etime_to_minutes, format_hm, format_minutes, group_by_comm)

# budgets_guards.py — validation + admin self-protection. Needs:
# device_policy.py (is_admin_protected), app.py (all four + ADMIN_GROUP),
# cli.py/agent.py through `budgets.<name>`, 5 test modules.
from budgets_guards import (  # noqa: E402
    ADMIN_GROUP, is_admin_protected, valid_category, valid_comm,
    valid_username)

# budgets_proc.py — reading `ps` and CPU deltas. Needs: agent.py and app.py
# (list_user_processes), `test_budgets` (parse_cpu_seconds/compute_deltas).
from budgets_proc import (  # noqa: E402
    compute_deltas, list_user_processes, parse_cpu_seconds, read_cmdline)

# budgets_filter.py — process filtering + the NEVER-pause safety list. Needs:
# app.py and agent.py (apply_filter/load_prefs/set_pref/is_never_pause),
# the freezer gates, 8 test modules.
from budgets_filter import (  # noqa: E402
    DEFAULT_HIDDEN_COMMS, DEFAULT_HIDDEN_PATHS, DEFAULT_HIDDEN_PATH_PREFIXES,
    NEVER_PAUSE_COMMS, _argv0_basename, apply_filter, is_default_hidden,
    is_never_pause, load_prefs, save_prefs, set_pref)

# budgets_attribution.py — categorization + display names. Needs: agent.py
# and app.py (12 names), the tick, 10 test modules. The two cache dicts are
# re-exported BY IDENTITY (tests .clear() them in place — R6).
from budgets_attribution import (  # noqa: E402
    CATEGORY_OVERRIDES, _DESKTOP_INDEX_CACHE, _DESKTOP_INDEX_TTL_SECONDS,
    _DESKTOP_TOKEN_MAP, _GENERIC_DOT_PARTS, _GENERIC_INTERPRETERS,
    _JVM_APP_NAMES, _JVM_APP_RES, _OVERRIDE_RES, _cmdline_app_tokens,
    _desktop_category, _exec_candidates, _flatpak_appid_parts,
    _scan_desktop_files, annotate_display_names, build_desktop_index,
    build_desktop_names, categorize, desktop_dirs_for, group_by_category,
    is_user_app, mark_user_apps, parse_desktop_file)

# budgets_state.py — budgets document IO + accounting. Needs: app.py
# (budget_status/set_limit), agent.py (budget_status/group_usage_minutes),
# the tick, 10 test modules (`budgets.BUDGETS_FILE` is patched in 10).
from budgets_state import (  # noqa: E402
    app_budget_status, blank_entry, budget_reset_needed, budget_status,
    group_usage_minutes, load_budgets, save_budgets, set_limit)

# budgets_overrides.py — app overrides + weekly windows. Needs: app.py (7
# names), agent.py (overrides_view/load_overrides), `test_overrides` and
# `test_session_agent_phase2` (`budgets.OVERRIDES_FILE` is patched in 3).
from budgets_overrides import (  # noqa: E402
    _clear_app_runtime, _config_entry, _release_window_freeze,
    _reset_window_runtime, active_window, clear_window, iter_overrides,
    iter_windows, load_overrides, normalize_override, normalize_window,
    override_map, overrides_view, parse_days, save_overrides,
    set_app_override, set_window, update_window, window_id_for)
