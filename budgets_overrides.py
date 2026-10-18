"""App overrides and weekly windows: normalize, iterate, set, clear.

Per-app allowances/exemptions plus weekly allowance windows for a category or
one app: `normalize_override`/`normalize_window` fail closed on anything
malformed, `iter_overrides`/`iter_windows`/`override_map`/`active_window`/
`overrides_view` read them, and `set_app_override`/`set_window`/
`update_window`/`clear_window` write them — each thaw the target's cgroup
when a freeze has to be released.

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 5; the facade
re-exports every name by name. `OVERRIDES_FILE`, `WINDOW_MIN_MINUTES`, the
window predicates, the budgets document and the freezer verbs resolve through
`budgets` at call time — see the cross-seam convention in budgets_guards.py.
"""

import audit
import store
from datetime import datetime

# ---------------------------------------------------------------------------
# overrides config store — per-app allowances/exemptions + weekly windows
# (whole-document SQLite table; OVERRIDES_FILE is the legacy import source
# and rollback mirror)
#
# Shape: {username: {"app_overrides": {comm: {...}}, "windows": {wid: {...}}}}
# Fail closed everywhere: a malformed override/window is treated as absent
# (category rules govern) and must never silently exempt an app.
# ---------------------------------------------------------------------------

def load_overrides():
    import budgets
    return store.load_table(store.db_path_for(budgets.OVERRIDES_FILE), 'overrides')


def save_overrides(data):
    import budgets
    store.save_table(store.db_path_for(budgets.OVERRIDES_FILE), 'overrides',
                     data, budgets.OVERRIDES_FILE)


def window_id_for(target_type, target):
    """Stable per-target window id (V1: one window per target)."""
    return 'w-%s-%s' % (target_type, target)


def _config_entry(data, username):
    """Per-user config doc with both top-level keys present."""
    entry = data.get(username)
    if not isinstance(entry, dict):
        entry = {}
        data[username] = entry
    if not isinstance(entry.get('app_overrides'), dict):
        entry['app_overrides'] = {}
    if not isinstance(entry.get('windows'), dict):
        entry['windows'] = {}
    return entry


def normalize_override(raw):
    """A validated app override, or None (fail closed).

    {'mode': 'limit'|'exempt', 'minutes': float|None, 'set_by', 'set_at'}
    Unknown mode / missing / non-positive minutes -> None: the override is
    ignored and the category rules govern (never silently exempt).
    """
    if not isinstance(raw, dict):
        return None
    mode = raw.get('mode')
    if mode == 'exempt':
        return {'mode': 'exempt', 'minutes': None,
                'set_by': raw.get('set_by'), 'set_at': raw.get('set_at')}
    if mode != 'limit':
        return None
    try:
        minutes = float(raw.get('minutes'))
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    return {'mode': 'limit', 'minutes': minutes,
            'set_by': raw.get('set_by'), 'set_at': raw.get('set_at')}


def normalize_window(raw, window_id=None):
    """A validated window dict, or None (fail closed).

    Unknown target/target_type, empty/bad ``days``, unparseable times,
    ``start == end``, or a sub-``WINDOW_MIN_MINUTES`` allowance -> None
    (the window is ignored; the daily rules govern).
    """
    import budgets
    if not isinstance(raw, dict):
        return None
    target_type = raw.get('target_type')
    target = raw.get('target')
    if target_type == 'category':
        if not budgets.valid_category(target):
            return None
    elif target_type == 'app':
        if not budgets.valid_comm(target):
            return None
    else:
        return None
    start = budgets._parse_hhmm(raw.get('start', ''))
    end = budgets._parse_hhmm(raw.get('end', ''))
    if start is None or end is None or start == end:
        return None
    try:
        days = sorted({int(d) for d in raw.get('days', [])})
    except (TypeError, ValueError):
        return None
    if not days or any(d < 0 or d > 6 for d in days):
        return None
    try:
        allowance = int(raw.get('allowance_minutes'))
    except (TypeError, ValueError):
        return None
    if allowance < budgets.WINDOW_MIN_MINUTES:
        return None
    return {
        'id': window_id or window_id_for(target_type, target),
        'target_type': target_type,
        'target': target,
        'days': days,
        'start': raw.get('start'),
        'end': raw.get('end'),
        'allowance_minutes': allowance,
        'enabled': bool(raw.get('enabled', True)),
        'set_by': raw.get('set_by'),
        'set_at': raw.get('set_at'),
    }


def parse_days(value):
    """CSV/list of day numbers -> sorted list, or None on any bad token."""
    if isinstance(value, (list, tuple, set, frozenset)):
        tokens = list(value)
    else:
        tokens = str(value or '').replace(' ', '').split(',')
    out = set()
    for token in tokens:
        if token in ('', None):
            continue
        try:
            day = int(token)
        except (TypeError, ValueError):
            return None
        if day < 0 or day > 6:
            return None
        out.add(day)
    return sorted(out) or None


def iter_overrides(overrides):
    """(comm, normalized_override) for every valid app override."""
    import budgets
    if not isinstance(overrides, dict):
        return
    raw = overrides.get('app_overrides')
    if not isinstance(raw, dict):
        return
    for comm, value in raw.items():
        if not budgets.valid_comm(comm):
            continue
        norm = normalize_override(value)
        if norm is not None:
            yield comm, norm


def iter_windows(overrides):
    """(window_id, normalized_window) for every valid configured window."""
    if not isinstance(overrides, dict):
        return
    raw = overrides.get('windows')
    if not isinstance(raw, dict):
        return
    for wid, value in raw.items():
        norm = normalize_window(value, window_id=wid)
        if norm is not None:
            yield wid, norm


def override_map(overrides):
    return {comm: ov for comm, ov in iter_overrides(overrides)}


def active_window(overrides, target_type, target, now):
    """The enabled, currently-active window governing ``target`` (or None)."""
    import budgets
    for _wid, win in iter_windows(overrides):
        if win['target_type'] != target_type or win['target'] != target:
            continue
        if win['enabled'] and budgets._in_window(win, now):
            return win
    return None


def overrides_view(username, data=None):
    """UI-facing config view: {'app_overrides': {...}, 'windows': [...]}."""
    import budgets
    if data is None:
        data = budgets.load_overrides()
    entry = data.get(username) if isinstance(data, dict) else None
    raw = entry if isinstance(entry, dict) else {}
    view = {
        'app_overrides': {comm: ov for comm, ov in iter_overrides(raw)},
        'windows': [win for _wid, win in iter_windows(raw)],
    }
    return view


def _reset_window_runtime(username, window_id):
    """Drop one window's runtime counters from the budgets doc."""
    import budgets
    try:
        data = budgets.load_budgets()
    except Exception:
        return
    entry = data.get(username)
    if not isinstance(entry, dict):
        return
    for key in ('window_used', 'window_active'):
        flags = entry.get(key)
        if isinstance(flags, dict):
            flags.pop(window_id, None)
    try:
        budgets.save_budgets(data)
    except OSError:
        pass


def _clear_app_runtime(username, comm):
    """Drop one app's runtime flags from the budgets doc (best effort)."""
    import budgets
    try:
        data = budgets.load_budgets()
    except Exception:
        return
    entry = data.get(username)
    if not isinstance(entry, dict):
        return
    for key in ('app_paused', 'app_resumed', 'app_manual_frozen',
                'app_manual_frozen_at'):
        flags = entry.get(key)
        if isinstance(flags, dict):
            flags.pop(comm, None)
    try:
        budgets.save_budgets(data)
    except OSError:
        pass


def set_app_override(username, comm, mode, minutes=None, actor='system'):
    """Set/clear a per-app override.

    mode 'exempt' means unlimited (never billed, never placed in a
    cgroup); 'limit' needs a positive integer ``minutes``. Any other
    mode, or a non-positive ``minutes`` with mode=limit, CLEARS the
    override. Clearing also releases the app's freeze and thaws its
    group (mirrors ``set_limit``).
    """
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    if not budgets.valid_comm(comm):
        return False, f'Invalid command: {comm}'
    data = budgets.load_overrides()
    entry = _config_entry(data, username)
    overrides = entry['app_overrides']

    mode = (mode or '').strip().lower()
    to_set = None
    clear = False
    if mode in ('', 'none', 'absent', 'clear'):
        clear = True
    elif mode == 'exempt':
        to_set = {'mode': 'exempt'}
    elif mode == 'limit':
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            return False, 'Minutes must be a number'
        if minutes < 0:
            return False, 'Minutes must not be negative'
        if minutes == 0:
            clear = True
        else:
            to_set = {'mode': 'limit', 'minutes': int(minutes)}
    else:
        return False, f'Unknown mode: {mode}'

    if clear:
        overrides.pop(comm, None)
        try:
            budgets.save_overrides(data)
        except OSError as e:
            return False, str(e)
        _clear_app_runtime(username, comm)
        ok, msg = budgets.set_frozen_app(username, comm, False, actor=actor)
        audit.safe_log(actor, 'budget.override_cleared',
                       f'{username}:{comm}',
                       {'ok': True, 'unfreeze': msg})
        return True, ('Override cleared'
                      + ('' if ok else f' (unfreeze: {msg})'))

    to_set['set_by'] = actor
    to_set['set_at'] = datetime.now().isoformat()
    overrides[comm] = to_set
    try:
        budgets.save_overrides(data)
    except OSError as e:
        return False, str(e)
    audit.safe_log(actor, 'budget.override_set', f'{username}:{comm}',
                   {'mode': to_set['mode'],
                    'minutes': to_set.get('minutes')})
    return True, 'OK'


def set_window(username, target_type, target, days, start, end,
               allowance_minutes, enabled=True, actor='system'):
    """Create a weekly window for a target.

    V1: one window per target. A second create for the same target is a
    collision -> (False, reason); use ``update_window`` to edit it.
    """
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    if target_type == 'category':
        if not budgets.valid_category(target):
            return False, f'Unknown category: {target}'
    elif target_type == 'app':
        if not budgets.valid_comm(target):
            return False, f'Invalid command: {target}'
    else:
        return False, f'Unknown target type: {target_type}'
    days = parse_days(days)
    if days is None:
        return False, 'Invalid days'
    window_id = window_id_for(target_type, target)
    data = budgets.load_overrides()
    entry = _config_entry(data, username)
    if window_id in entry['windows']:
        return False, 'A window already exists for this target'
    norm = normalize_window({
        'target_type': target_type, 'target': target, 'days': days,
        'start': start, 'end': end, 'allowance_minutes': allowance_minutes,
        'enabled': enabled, 'set_by': actor,
        'set_at': datetime.now().isoformat(),
    }, window_id=window_id)
    if norm is None:
        return False, 'Invalid window fields'
    entry['windows'][window_id] = norm
    try:
        budgets.save_overrides(data)
    except OSError as e:
        return False, str(e)
    audit.safe_log(actor, 'budget.window_set', f'{username}:{window_id}',
                   {'target_type': target_type, 'target': target,
                    'days': norm['days'], 'start': norm['start'],
                    'end': norm['end'],
                    'allowance_minutes': norm['allowance_minutes'],
                    'enabled': norm['enabled']})
    return True, 'OK'


def update_window(username, window_id, days=None, start=None, end=None,
                  allowance_minutes=None, enabled=None, actor='system'):
    """Edit/disable an existing window (keyed by its stable id)."""
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    data = budgets.load_overrides()
    entry = _config_entry(data, username)
    windows = entry['windows']
    stored = windows.get(window_id)
    if not isinstance(stored, dict):
        return False, 'No such window'
    merged = dict(stored)
    if days is not None:
        merged['days'] = parse_days(days)
        if merged['days'] is None:
            return False, 'Invalid days'
    if start is not None:
        merged['start'] = start
    if end is not None:
        merged['end'] = end
    if allowance_minutes is not None:
        merged['allowance_minutes'] = allowance_minutes
    if enabled is not None:
        merged['enabled'] = bool(enabled)
    norm = normalize_window(merged, window_id=window_id)
    if norm is None:
        return False, 'Invalid window fields'
    norm['set_by'] = actor
    norm['set_at'] = datetime.now().isoformat()
    windows[window_id] = norm
    try:
        budgets.save_overrides(data)
    except OSError as e:
        return False, str(e)
    if enabled is not None and not bool(enabled):
        # Disabling resets the window's runtime and releases its freeze.
        _reset_window_runtime(username, window_id)
        _release_window_freeze(username, norm, actor=actor)
    audit.safe_log(actor, 'budget.window_set', f'{username}:{window_id}',
                   {'target_type': norm['target_type'],
                    'target': norm['target'],
                    'enabled': norm['enabled']})
    return True, 'OK'


def clear_window(username, window_id=None, target_type=None, target=None,
                 actor='system'):
    """Remove a window, reset its runtime, and thaw its target."""
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    if window_id is None:
        if not target_type or not target:
            return False, 'window id required'
        window_id = window_id_for(target_type, target)
    data = budgets.load_overrides()
    entry = _config_entry(data, username)
    removed = entry['windows'].pop(window_id, None)
    if not isinstance(removed, dict):
        return False, 'No such window'
    try:
        budgets.save_overrides(data)
    except OSError as e:
        return False, str(e)
    _reset_window_runtime(username, window_id)
    norm = normalize_window(removed, window_id=window_id)
    if norm is not None:
        _release_window_freeze(username, norm, actor=actor)
    audit.safe_log(actor, 'budget.window_cleared', f'{username}:{window_id}',
                   {'ok': True})
    return True, 'Window cleared'


def _release_window_freeze(username, window, actor='system'):
    """Thaw a window's target group (open/close/disable/clear)."""
    import budgets
    if window['target_type'] == 'category':
        return budgets.set_frozen(username, window['target'], False, actor=actor)
    return budgets.set_frozen_app(username, window['target'], False, actor=actor)
