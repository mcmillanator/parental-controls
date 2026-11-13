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

CALENDAR (CALENDAR_DESIGN.md, 2026-11-13): this module also owns the impure
glue around the pure `schedule` evaluator — `load_schedule`/`save_schedule`
(whole-doc 'schedule' table), `ensure_migrated` (idempotent hard-cutover
migration from the bedtime table + windows sub-doc), `user_windows`
(the enforcement read: calendar blocks once a user is migrated, legacy
windows derived on the fly before), and the write-throughs the old
window/bedtime routes use so the schedule table stays the single source of
truth while the legacy tables stay exact mirrors.
"""

import audit
import schedule
import store
from datetime import datetime, timedelta

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
    return store.load_table(store.db_path_for(budgets.OVERRIDES_FILE),
                            'overrides')


def save_overrides(data):
    import budgets
    store.save_table(store.db_path_for(budgets.OVERRIDES_FILE), 'overrides',
                     data, budgets.OVERRIDES_FILE)


# ---------------------------------------------------------------------------
# Calendar schedule glue (CALENDAR_DESIGN.md) — the 'schedule' table is the
# enforcement source of truth; the bedtime table and the windows sub-doc are
# write-synchronized mirrors (see _sync_windows_mirror). Pure evaluation
# lives in schedule.py; everything here is table IO + wiring.
# ---------------------------------------------------------------------------

def load_schedule():
    import budgets
    return store.load_table(store.db_path_for(budgets.OVERRIDES_FILE),
                            'schedule')


def save_schedule(data):
    import budgets
    store.save_table(store.db_path_for(budgets.OVERRIDES_FILE), 'schedule',
                     data, budgets.SCHEDULE_FILE)


def user_schedule(username, schedule_map=None):
    """One user's schedule doc ({} when absent/unreadable)."""
    if schedule_map is None:
        schedule_map = load_schedule()
    doc = schedule_map.get(username) if isinstance(schedule_map, dict) else None
    return doc if isinstance(doc, dict) else {}


def user_windows(username, overrides=None, schedule_doc=None, today=None):
    """Blocks governing this user's category/app targets (window source).

    Schedule-first (hard cutover): once the doc carries the migration
    marker, ONLY the schedule table is consulted — legacy windows are the
    migration source and rollback mirror, never a second enforcement
    input. Before migration the same block shape is derived from the
    legacy windows on the fly (window ids preserved, so the runtime
    window_used/window_active keys are continuous across the cutover).
    Returns normalized blocks in id order, including disabled ones — the
    enforcement lifecycle iterates them all and is_block_active() filters.
    """
    import budgets
    if schedule.doc_is_migrated(schedule_doc):
        return [block for _bid, block in sorted(
            schedule.iter_blocks(schedule_doc), key=lambda pair: pair[0])
            if block['target'] != schedule.TARGET_BEDTIME]
    if overrides is None:
        overrides = budgets.load_overrides().get(username)
    today = today or datetime.now().date()
    out = []
    for _wid, win in sorted(iter_windows(overrides),
                            key=lambda pair: pair[0]):
        block = schedule.window_to_block(win, today)
        if block is not None:
            out.append(block)
    return out


def block_active(block, now):
    """Enabled + time-active for ONE block (the evaluator's predicate).

    The block-shaped replacement for ``budgets._in_window(win, now)`` at
    the enforcement call sites; fails closed on malformed blocks.
    """
    return schedule.is_block_active(block, now)


def active_capped(blocks, target_type, target, now):
    """The capped window-replacement block for one target, or None.

    Same contract as ``active_window`` for its consumers (allowance
    replaces the daily limit while active), now block-shaped. At most one
    same-target block is active at any minute: same child+target blocks
    must not overlap (enforced at write time, owner rule #4).
    """
    canonical = schedule.target_of(target_type, target)
    if canonical is None:
        return None
    for block in blocks:
        if (block['target'] == canonical
                and block['mode'] == schedule.MODE_CAPPED
                and schedule.is_block_active(block, now)):
            return block
    return None


def has_bedtime_config(schedule_doc, bedtime_entry):
    """Whether the user has a bedtime ANCHOR (daily-reset reference).

    Schedule-first: migrated users anchor on their ENABLED bedtime blocks
    (a disabled block locks nothing, so the day resets at midnight like
    a user with no bedtime); non-migrated users on the legacy entry shape.
    """
    if schedule.doc_is_migrated(schedule_doc):
        return any(block['target'] == schedule.TARGET_BEDTIME
                   and block['enabled']
                   for _bid, block in schedule.iter_blocks(schedule_doc))
    return bool(bedtime_entry and bedtime_entry.get('start'))


def in_bedtime_for(schedule_doc, bedtime_entry, now):
    """Bedtime predicate over whichever source governs the user.

    Migrated -> the pure schedule evaluator (bedtime blocks lock). Not
    migrated -> the relocated legacy predicate (schedule.in_bedtime_entry,
    the verbatim old body). Fail-closed both ways.
    """
    if schedule.doc_is_migrated(schedule_doc):
        return schedule.in_bedtime(schedule_doc, now)
    if not bedtime_entry:
        return False
    return schedule.in_bedtime_entry(bedtime_entry, now)


# ---------------------------------------------------------------------------
# The single write funnel — every calendar mutation lands here, so the
# 'schedule' doc is the source of truth and the overrides windows sub-doc
# stays an exact mirror for anything legacy-shape can express
# (CALENDAR_DESIGN.md §mirrors). The BEDTIME-entry mirror (start/end/days
# on the live row) is owned by app.py/agent.py — they hold that table.
# ---------------------------------------------------------------------------

def _block_to_legacy_window(block):
    """Mirror one capped block back to the legacy windows shape, or None.

    Only presets the old shape can express round-trip: DAILY, SCHOOL and
    WEEKLY:BYDAY without an UNTIL bound. ONE/BIWEEKLY/UNTIL-bound and
    permitted-mode blocks have no legacy window equivalent and are simply
    absent from the mirror (the schedule table still governs; the mirror
    only matters for pre-migration readers and the corruption fallback).
    """
    parsed = schedule.parse_preset(block.get('preset'))
    if parsed is None or block.get('mode') != schedule.MODE_CAPPED:
        return None
    freq, days = parsed
    if freq in ('ONE', 'BIWEEKLY') or block.get('until'):
        return None
    if freq == 'DAILY':
        days = [0, 1, 2, 3, 4, 5, 6]
    elif freq == 'SCHOOL':
        days = [0, 1, 2, 3, 4]
    kind = schedule.target_kind(block.get('target'))
    if kind == 'category':
        target_type, target = 'category', block['target']
    elif kind == 'app':
        target_type, target = 'app', block['target'][4:]
    else:
        return None
    return {
        'id': block['id'], 'target_type': target_type, 'target': target,
        'days': list(days), 'start': block['start'], 'end': block['end'],
        'allowance_minutes': block['allowance_minutes'],
        'enabled': block['enabled'],
        'set_by': block.get('set_by'), 'set_at': block.get('set_at'),
    }


def _sync_windows_mirror(username, doc, actor='system'):
    """Regenerate overrides[username]['windows'] from the doc's blocks."""
    events = doc.get('events') if isinstance(doc, dict) else None
    if not isinstance(events, dict):
        return
    mirrored = {}
    for bid, value in events.items():
        block = schedule.normalize_block(value, block_id=bid)
        if block is None:
            continue
        legacy = _block_to_legacy_window(block)
        if legacy is not None:
            mirrored[bid] = legacy
    try:
        data = load_overrides()
    except Exception:
        return
    entry = _config_entry(data, username)
    entry['windows'] = mirrored
    try:
        save_overrides(data)
    except OSError as e:
        print('schedule: windows mirror sync failed for %s: %s'
              % (username, e), flush=True)


def _load_events(doc):
    events = doc.get('events') if isinstance(doc, dict) else None
    return events if isinstance(events, dict) else {}


def _stamp_migrated(doc, now):
    if not schedule.doc_is_migrated(doc):
        doc['migrated'] = now.isoformat(timespec='seconds')
    return doc


def upsert_block(username, raw_block, actor='system', block_id=None):
    """Create/replace one calendar block (validated, overlap-checked).

    Returns (ok, message). The first calendar write is the cutover: the
    migration marker is stamped so enforcement reads ONLY the schedule
    from now on. Same child+target overlaps are rejected (owner rule #4);
    the windows mirror is refreshed on success.
    """
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    norm = schedule.normalize_block(raw_block, block_id=block_id)
    if norm is None:
        return False, ('Invalid block (target/mode/preset/times/allowance'
                       ' — see the calendar grammar)')
    doc_map = load_schedule()
    doc = doc_map.get(username)
    doc = doc if isinstance(doc, dict) else {}
    events = _load_events(doc)
    existing = [block for bid, block in schedule.iter_blocks(doc)
                if bid != norm['id']]
    conflict = schedule.find_overlap(existing, norm)
    if conflict is not None:
        return False, ('Overlaps block %s for the same target; blocks for '
                       'one target must not overlap' % conflict)
    events[norm['id']] = norm
    doc['events'] = events
    _stamp_migrated(doc, datetime.now())
    doc_map[username] = doc
    try:
        save_schedule(doc_map)
    except OSError as e:
        return False, str(e)
    _sync_windows_mirror(username, doc, actor=actor)
    audit.safe_log(actor, 'schedule.block_set',
                   '%s:%s' % (username, norm['id']),
                   {'target': norm['target'], 'mode': norm['mode'],
                    'preset': norm['preset'], 'start': norm['start'],
                    'end': norm['end'],
                    'allowance_minutes': norm['allowance_minutes'],
                    'enabled': norm['enabled']})
    return True, 'OK'


def remove_block(username, block_id, actor='system'):
    """Delete one block (series-only; no per-occurrence exceptions in v1)."""
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    doc_map = load_schedule()
    doc = doc_map.get(username)
    events = _load_events(doc)
    if block_id not in events:
        return False, 'No such block'
    events.pop(block_id)
    doc = doc if isinstance(doc, dict) else {}
    doc['events'] = events
    _stamp_migrated(doc, datetime.now())
    doc_map[username] = doc
    try:
        save_schedule(doc_map)
    except OSError as e:
        return False, str(e)
    _sync_windows_mirror(username, doc, actor=actor)
    audit.safe_log(actor, 'schedule.block_cleared',
                   '%s:%s' % (username, block_id), {'ok': True})
    return True, 'Block removed'


def set_bedtime_block(username, days, start, end, actor='system',
                      enabled=True):
    """Write-through for the OLD bedtime editor: replace the user's
    bedtime-target blocks with exactly one derived from (days,start,end).

    Full-replace semantics: the legacy form always owned the whole
    bedtime schedule, so clearing there clears ALL bedtime blocks (the
    device must not stay locked by an editor the parent just emptied).
    Non-bedtime blocks are untouched. Caller keeps owning the bedtime
    live-state row (this only mirrors the CONFIG into the calendar).
    """
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    doc_map = load_schedule()
    doc = doc_map.get(username)
    doc = doc if isinstance(doc, dict) else {}
    events = _load_events(doc)
    events = {bid: value for bid, value in events.items()
              if not (isinstance(value, dict)
                      and value.get('target') == schedule.TARGET_BEDTIME)}
    now = datetime.now()
    block = None
    if start or end:
        preset = schedule.preset_for_days(days)
        block = schedule.normalize_block({
            'id': 'b-bedtime-1', 'target': schedule.TARGET_BEDTIME,
            'mode': None, 'preset': preset,
            'dtstart': (now - timedelta(days=1)).date().isoformat(),
            'start': start, 'end': end, 'enabled': bool(enabled),
            'set_by': actor,
            'set_at': now.isoformat(timespec='seconds'),
        })
        if block is None:
            return False, 'Invalid bedtime (days/start/end)'
        events[block['id']] = block
    doc['events'] = events
    _stamp_migrated(doc, now)
    doc_map[username] = doc
    try:
        save_schedule(doc_map)
    except OSError as e:
        return False, str(e)
    _sync_windows_mirror(username, doc, actor=actor)
    audit.safe_log(actor, 'schedule.block_set',
                   '%s:b-bedtime-1' % username,
                   {'target': 'bedtime', 'days': list(days or []),
                    'start': start, 'end': end})
    return True, 'OK'


def _upsert_window_block(username, window):
    """Mirror a legacy window set/update into the schedule doc IF migrated."""
    doc_map = load_schedule()
    doc = doc_map.get(username)
    if not schedule.doc_is_migrated(doc):
        return
    block = schedule.window_to_block(window, datetime.now().date())
    if block is None:
        return
    events = _load_events(doc)
    events[block['id']] = block
    doc['events'] = events
    doc_map[username] = doc
    try:
        save_schedule(doc_map)
    except OSError as e:
        print('schedule: window block mirror failed for %s: %s'
              % (username, e), flush=True)


def _drop_window_block(username, window_id):
    """Mirror a legacy window clear/disable into the schedule doc."""
    doc_map = load_schedule()
    doc = doc_map.get(username)
    if not schedule.doc_is_migrated(doc):
        return
    events = _load_events(doc)
    if window_id not in events:
        return
    events.pop(window_id)
    doc['events'] = events
    doc_map[username] = doc
    try:
        save_schedule(doc_map)
    except OSError as e:
        print('schedule: window block drop failed for %s: %s'
              % (username, e), flush=True)


def ensure_migrated(bedtime_map, overrides_map, schedule_map=None, now=None,
                    actor='system'):
    """Idempotent, deterministic hard-cutover migration.

    Every valid username with legacy config (a bedtime entry with any of
    start/end/days, or a non-empty windows sub-doc) and NO migrated doc
    gets ``schedule.migrate(...)`` blocks: bedtime entry -> one bedtime
    block; each ENABLED valid window -> one capped block (same id, so the
    per-window runtime keys carry over); disabled/dead windows are NOT
    migrated — but the marker is still stamped, so a rule the parent
    deleted is never resurrected by a later re-run (the enforcement
    fallback reads the write-synchronized mirror, see CALENDAR_DESIGN.md
    §fail-closed). Users with neither legacy config nor a doc are left
    untouched (no empty docs). Persists once on change; audit-logs one
    'schedule.migrate' event per user. Returns (schedule_map, migrated).
    """
    import budgets
    now = now or datetime.now()
    if schedule_map is None:
        schedule_map = load_schedule()
    migrated = []
    bedtime_map = bedtime_map if isinstance(bedtime_map, dict) else {}
    overrides_map = (overrides_map if isinstance(overrides_map, dict)
                     else {})
    for username in sorted(set(bedtime_map) | set(overrides_map)):
        if not budgets.valid_username(username):
            continue
        if schedule.doc_is_migrated(schedule_map.get(username)):
            continue
        entry = bedtime_map.get(username)
        ovr = overrides_map.get(username)
        has_bedtime = (isinstance(entry, dict)
                       and any(k in entry for k in ('start', 'end', 'days')))
        raw_windows = ovr.get('windows') if isinstance(ovr, dict) else None
        has_windows = bool(raw_windows) if isinstance(raw_windows, dict) \
            else False
        if not has_bedtime and not has_windows:
            continue
        windows = [win for _wid, win in iter_windows(ovr)]
        doc = schedule.migrate(entry if has_bedtime else None, windows, now)
        schedule_map[username] = doc
        migrated.append(username)
        audit.safe_log(actor, 'schedule.migrate', username, {
            'blocks': sorted(doc['events']),
            'bedtime': has_bedtime, 'windows': len(windows)})
    if migrated:
        try:
            save_schedule(schedule_map)
        except OSError as e:
            print('schedule: migration save failed: %s' % e, flush=True)
    return schedule_map, migrated


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
    _upsert_window_block(username, norm)
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
    _upsert_window_block(username, norm)
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
    _drop_window_block(username, window_id)
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
        return budgets.set_frozen(username, window['target'], False,
                                   actor=actor)
    return budgets.set_frozen_app(username, window['target'], False,
                                   actor=actor)
