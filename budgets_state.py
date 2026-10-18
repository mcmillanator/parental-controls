"""Budgets state IO and accounting.

`load_budgets`/`save_budgets` read and write the whole per-user budgets
document, `blank_entry` is its default shape, `set_limit` sets or clears a
daily allowance, `budget_status`/`app_budget_status` are the UI-facing views,
`group_usage_minutes` is the billable-minutes lookup and `budget_reset_needed`
is the pure reset rule (bedtime-end transition, else first tick of a new
date).

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 5; the facade
re-exports every name by name. `BUDGETS_FILE`, the window helpers, the
freezer verbs and the audit hook resolve through `budgets` at call time, so
the ten test modules that patch `budgets.BUDGETS_FILE` (and friends) keep
reaching this code — see the cross-seam convention in budgets_guards.py.
"""

import audit
import store
from datetime import datetime

# ---------------------------------------------------------------------------
# budgets store (SQLite table holding the same JSON document per user;
# BUDGETS_FILE is the legacy import source and rollback mirror)
# Shape: {username: {"limits": {cat: minutes}, "day": "YYYY-MM-DD",
#   "used_seconds": {cat: float}, "last_cpu": {pid: {comm, cpu}},
#   "paused": {cat: bool}, "resumed": {cat: bool},
#   "manual_frozen": {cat: bool}, "manual_frozen_at": {cat: iso},
#   "warned_low": {cat: "YYYY-MM-DD"}, "warned_paused": {cat: "YYYY-MM-DD"},
#   "was_in_bedtime": bool}}
# ---------------------------------------------------------------------------

def load_budgets():
    import budgets
    return store.load_table(store.db_path_for(budgets.BUDGETS_FILE), 'budgets')


def save_budgets(data):
    import budgets
    store.save_table(store.db_path_for(budgets.BUDGETS_FILE), 'budgets',
                     data, budgets.BUDGETS_FILE)


def blank_entry():
    import budgets
    return {'limits': {}, 'day': None, 'used_seconds': {}, 'last_cpu': {},
            'paused': {}, 'resumed': {}, 'manual_frozen': {},
            'manual_frozen_at': {}, 'warned_low': {},
            'warned_paused': {}, 'was_in_bedtime': False,
            'last_tick': None,
            # Per-app runtime (parallel to the per-category keys above):
            # keyed by comm. Populated by the tick, reset in lockstep
            # with the daily reset.
            'app_used': {}, 'app_paused': {}, 'app_resumed': {},
            'app_manual_frozen': {}, 'app_manual_frozen_at': {},
            'app_warned_low': {}, 'app_warned_paused': {},
            # Weekly-window runtime: keyed by window id. ``window_used``
            # is deliberately NOT cleared at the daily reset while its
            # window is active (spans-midnight accumulation).
            'window_used': {}, 'window_active': {},
            # Focus engine (session agent) runtime. Defaults preserve the
            # CPU engine exactly; ``feed_seq`` is a drain cursor (never
            # reset by the daily reset) and ``feed_boot_id`` invalidates
            # carried-over sessions across a reboot.
            # feed_seq is the last-consumed feed seq: parse_feed ignores
            # rows with seq <= cursor, and the agent's first row is seq=1,
            # so the cursor starts at 0 (nothing consumed yet).
            'focus_engine': budgets.FOCUS_ENGINE_CPU, 'feed_seq': 0,
            'feed_last_seen': None, 'feed_agent': None,
            'feed_boot_id': None, 'feed_status': None}


def set_limit(username, category, minutes, actor='system'):
    """Set/clear a per-category daily budget. minutes<=0 clears the limit."""
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    if not budgets.valid_category(category):
        return False, f'Unknown category: {category}'
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        return False, 'Minutes must be a number'
    data = budgets.load_budgets()
    entry = data.setdefault(username, blank_entry())
    limits = entry.setdefault('limits', {})
    if minutes <= 0:
        limits.pop(category, None)
        # Dropping the limit releases any freeze for that category
        # (auto-pause AND manual freeze alike).
        entry.get('paused', {}).pop(category, None)
        entry.get('resumed', {}).pop(category, None)
        entry.get('manual_frozen', {}).pop(category, None)
        entry.get('manual_frozen_at', {}).pop(category, None)
        try:
            budgets.save_budgets(data)
        except OSError as e:
            return False, str(e)
        ok, msg = budgets.set_frozen(username, category, False, actor=actor)
        audit.safe_log(actor, 'budget.limit_cleared',
                       f'{username}:{category}',
                       {'ok': True, 'unfreeze': msg})
        return True, 'Limit cleared' + ('' if ok else f' (unfreeze: {msg})')
    entry['limits'][category] = minutes
    try:
        budgets.save_budgets(data)
    except OSError as e:
        return False, str(e)
    audit.safe_log(actor, 'budget.set_limit', f'{username}:{category}',
                   {'minutes': minutes})
    return True, 'OK'


def budget_status(username, data=None, overrides=None, now=None):
    """UI-friendly status: {cat: {minutes, used_minutes, remaining_minutes,
    paused}} for every category, plus an ``app_budgets`` map.

    Legacy per-category keys stay byte-identical (regression guard); the
    new keys are additive: ``window_active`` and ``effective_source``
    (``window|daily|none``). All displayed times are whole minutes —
    seconds are never shown.
    """
    import budgets
    if data is None:
        data = budgets.load_budgets()
    if overrides is None:
        try:
            overrides = budgets.load_overrides().get(username)
        except Exception:
            overrides = None
    now = now or datetime.now()
    entry = data.get(username, {})
    limits = entry.get('limits', {})
    used = entry.get('used_seconds', {})
    paused = entry.get('paused', {})
    manual_frozen = entry.get('manual_frozen', {})
    active_by_cat = {}
    for _wid, win in budgets.iter_windows(overrides):
        if (win['target_type'] == 'category' and win['enabled']
                and budgets._in_window(win, now)):
            active_by_cat[win['target']] = win
    status = {}
    for cat in budgets.CATEGORIES:
        limit_min = float(limits.get(cat, 0) or 0)
        used_sec = float(used.get(cat, 0) or 0)
        is_paused = bool(paused.get(cat, False))
        is_manual = bool(manual_frozen.get(cat, False))
        win = active_by_cat.get(cat)
        if win is not None:
            effective_source = 'window'
        elif limit_min > 0:
            effective_source = 'daily'
        else:
            effective_source = 'none'
        status[cat] = {
            'minutes': limit_min,
            'used_minutes': int(round(used_sec / 60.0)),
            'remaining_minutes': int(round(max(0.0, limit_min * 60 - used_sec) / 60.0))
            if limit_min > 0 else None,
            'paused': is_paused,
            'exhausted': bool(limit_min > 0 and used_sec >= limit_min * 60),
            'manual_frozen': is_manual,
            'frozen': bool(is_paused or is_manual),
            'window_active': bool(win),
            'effective_source': effective_source,
            'window_allowance_minutes': (win['allowance_minutes']
                                         if win is not None else None),
        }
    status['app_budgets'] = app_budget_status(
        username, data=data, overrides=overrides, now=now)
    return status


def app_budget_status(username, data=None, overrides=None, now=None):
    """Per-app view for the UI/live payload, keyed by comm.

    {'mode', 'exempt', 'minutes', 'used_minutes', 'remaining_minutes',
    'paused', 'manual_frozen', 'frozen', 'window_active',
    'effective_source'} — only for comms with a valid override.
    """
    import budgets
    if data is None:
        data = budgets.load_budgets()
    if overrides is None:
        try:
            overrides = budgets.load_overrides().get(username)
        except Exception:
            overrides = None
    now = now or datetime.now()
    entry = data.get(username) if isinstance(data, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    app_used = entry.get('app_used', {}) or {}
    app_paused = entry.get('app_paused', {}) or {}
    app_manual = entry.get('app_manual_frozen', {}) or {}
    window_used = entry.get('window_used', {}) or {}
    out = {}
    for comm, ov in budgets.iter_overrides(overrides):
        is_paused = bool(app_paused.get(comm, False))
        is_manual = bool(app_manual.get(comm, False))
        if ov['mode'] == 'exempt':
            out[comm] = {
                'mode': 'exempt', 'exempt': True, 'minutes': None,
                'used_minutes': 0, 'remaining_minutes': None,
                'paused': is_paused, 'manual_frozen': is_manual,
                'frozen': bool(is_paused or is_manual),
                'window_active': False, 'effective_source': 'exempt',
            }
            continue
        win = budgets.active_window(overrides, 'app', comm, now)
        if win is not None:
            limit_min = float(win['allowance_minutes'])
            used_sec = float(window_used.get(win['id'], 0.0) or 0.0)
            source = 'window'
        else:
            limit_min = float(ov['minutes'])
            used_sec = float(app_used.get(comm, 0.0) or 0.0)
            source = 'daily'
        out[comm] = {
            'mode': 'limit', 'exempt': False, 'minutes': limit_min,
            'used_minutes': int(round(used_sec / 60.0)),
            'remaining_minutes': int(round(
                max(0.0, limit_min * 60 - used_sec) / 60.0)),
            'paused': is_paused, 'manual_frozen': is_manual,
            'frozen': bool(is_paused or is_manual),
            'window_active': bool(win), 'effective_source': source,
        }
    return out


def group_usage_minutes(category, comm, budget_state):
    """Billable minutes a process group has accrued since the last reset.

    The Processes table shows budget USAGE, not the process's ps ETIME
    wall-clock age, so it resets with the budget day reset (and a
    matching window reset) exactly like the budget rows. An app override
    governs its comm: use the override's per-app ``used_minutes`` (``0``
    for an ``exempt`` override, which is never billed at all). Otherwise
    the owning CATEGORY's ``used_minutes`` applies.

    ``budget_state`` is a ``budget_status()`` payload. Unknown comm and
    category both return ``None`` so the caller can render its own empty
    placeholder (``'—'``).
    """
    if not isinstance(budget_state, dict):
        return None
    app_budgets = budget_state.get('app_budgets') or {}
    app = app_budgets.get(comm) if isinstance(app_budgets, dict) else None
    if isinstance(app, dict) and 'used_minutes' in app:
        return app.get('used_minutes')
    cat = budget_state.get(category)
    if isinstance(cat, dict) and 'used_minutes' in cat:
        return cat.get('used_minutes')
    return None


def budget_reset_needed(entry_day, has_bedtime, was_in_bedtime, now_in_bedtime,
                        today_str):
    """Pure reset rule (unit-tested):

    - Bedtime users: reset on the bedtime-end transition (was in bedtime,
      now out). Overnight windows keep accruing past midnight until the
      morning transition.
    - No-bedtime users: reset on the first tick of a new calendar date.
    - Staleness guard: any stored day older than today while currently out
      of bedtime resets (covers downtime spanning the transition).
    """
    if was_in_bedtime and not now_in_bedtime:
        return True
    if entry_day != today_str and not now_in_bedtime:
        return True
    return False
