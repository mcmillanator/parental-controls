"""The agent/UI-facing budget verbs + the window and bedtime predicates.

`freeze_category` / `unfreeze_category` / `resume_category` are the manual
(parent-clicked) freeze verbs: they record `manual_frozen`/`resumed` so the
tick re-asserts and the watchdog treats them as legitimate. `_parse_hhmm`,
`_in_window` and `_in_bedtime` are the recurring day-of-week + HH:MM range
check shared by bedtime and the weekly budget windows — a local copy so the
budgets model stays stdlib-only and importable without the Flask app
(app.py passes its own bedtime map in).

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 7; the facade
re-exports every name by name. Every freezer, store, audit and
categorization call resolves through `budgets` at call time — see the
cross-seam convention in budgets_guards.py.
"""

import audit
from datetime import datetime


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
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    if not budgets.valid_category(category):
        return False, f'Unknown category: {category}'
    if budgets.is_admin_protected(username):
        return False, (f'Refused: {username} is an admin and cannot '
                       'be frozen')
    data = budgets.load_budgets()
    entry = data.get(username, {})
    if float((entry.get('limits', {}) or {}).get(category, 0) or 0) <= 0:
        return False, 'Set a time limit for this category first'
    limits = entry.get('limits', {})
    try:
        prefs = budgets.load_prefs()
    except Exception:
        prefs = None
    try:
        index = budgets.build_desktop_index(username)
    except Exception:
        index = None
    try:
        procs = budgets.list_user_processes(username)
    except Exception as e:
        return False, f'could not snapshot processes: {e}'
    members = budgets.freezable_members(procs, username, category, limits,
                                     prefs, index)
    ok, msg = budgets.ensure_cgroup(username, category)
    if not ok:
        return False, msg
    for proc in members:
        # Individual migrate failures are ignored: the readback safety
        # gate below is authoritative (matches the tick's ignore-and-verify
        # order).
        budgets.migrate_pid(username, category, proc['pid'])
    current = budgets.cgroup_members(username, category)
    safe, reason = budgets.verify_freeze_safety(username, current,
                                        {p['pid']: p for p in members})
    if not safe:
        return False, reason
    if not members and not current:
        # Nothing running yet: record the freeze without writing
        # cgroup.freeze. The tick's manual-frozen branch freezes late
        # starters under the same gates.
        pass
    else:
        ok, msg = budgets.set_frozen(username, category, True, actor=actor)
        if not ok:
            return False, msg
    try:
        data = budgets.load_budgets()
        entry = data.setdefault(username, budgets.blank_entry())
        entry.setdefault('manual_frozen', {})[category] = True
        entry.setdefault('manual_frozen_at', {})[category] = \
            datetime.now().isoformat()
        budgets.save_budgets(data)
    except OSError as e:
        return False, str(e)
    label = budgets.CATEGORY_LABELS.get(category, category)
    return True, f'Frozen {label} (manual freeze)'


def unfreeze_category(username, category, actor='system'):
    """Manual Unfreeze: thaw and inhibit re-freeze until the next reset.

    Subsumes the old Resume: clears BOTH ``paused`` and ``manual_frozen``
    (plus ``manual_frozen_at``) and sets ``resumed`` so the tick does not
    instantly re-freeze an exhausted category.
    """
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    if not budgets.valid_category(category):
        return False, f'Unknown category: {category}'
    ok, msg = budgets.set_frozen(username, category, False, actor=actor)
    if not ok:
        return False, msg
    try:
        data = budgets.load_budgets()
        entry = data.setdefault(username, budgets.blank_entry())
        entry.get('paused', {}).pop(category, None)
        entry.get('manual_frozen', {}).pop(category, None)
        entry.get('manual_frozen_at', {}).pop(category, None)
        entry.setdefault('resumed', {})[category] = True
        budgets.save_budgets(data)
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
    import budgets
    return budgets.unfreeze_category(username, category, actor=actor)


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
