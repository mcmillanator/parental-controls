#!/usr/bin/env python3
"""Calendar schedule blocks — the PURE evaluator (CALENDAR_DESIGN.md).

One recurring "block" model replaces the bedtime table and the weekly
windows sub-doc of overrides. A block targets exactly one of:

* ``'bedtime'``   — the device LOCKS while the block is active (today's
  bedtime semantics: midnight-crossing, was_in_bedtime transitions).
* a category      — ``games`` / ``browsers`` / ``media`` / ``chat`` / ``other``
  (the fixed set from ``budgets.CATEGORIES``, mirrored here as literals).
* ``'app:<comm>'`` — one application by its validated comm.

Modes (only for category/app targets; bedtime blocks carry ``mode=None``):

* ``'permitted'`` — usage allowed during the block with no minute cap, and
  the target GATES: with >=1 enabled permitted block, usage outside every
  enabled block of the target is not allowed (frozen — the owner's gating
  rule "blocks gate only where present").
* ``'capped'``    — the window REPLACES the daily limit while active,
  exactly today's weekly-window semantics, with ``allowance_minutes``.

Recurrence is PRESETS ONLY, stored in the ``preset`` string (RRULE-lite):

    ONE                          single occurrence on ``dtstart``
    DAILY                        every day from ``dtstart``
    SCHOOL                       Mon-Fri from ``dtstart``
    WEEKLY:BYDAY=<c>(,<c>)*      the chosen weekdays (MO..SU canonical order)
    BIWEEKLY:BYDAY=<c>(,<c>)*    every 2nd week, anchored on the week
                                 (Monday-based) that contains ``dtstart``

plus an optional inclusive ``until`` date on any preset. Day codes are
ISO-style weekday abbreviations mapping to ``date.weekday()``:
MO=0(Tue=TU=1 ... Sun=SU=6). Wall-clock naive semantics (device-local;
DST just shifts — same as today). ``end < start`` means "ends next day"
(mirrors the existing predicate in ``app.is_in_bedtime`` /
``budgets._in_window``).

Hard rules (AGENTS.md / RESTRUCTURE_PLAN.md):

* **Pure + stdlib-only.** No I/O, no local imports (not even ``store`` or
  ``budgets``) — ``agent.py`` imports this WITHOUT app/flask, and
  ``SharedCorePurityTest`` AST-pins it. Evaluator functions take plain
  dicts and a ``datetime``.
* **Fail closed.** ``normalize_block`` returns None on anything malformed
  (unknown target, unparseable times, bad BYDAY, sub-``WINDOW_MIN_MINUTES``
  cap, start == end, missing dtstart, until < dtstart); a malformed stored
  block is treated as absent by every reader.
* **Migration conversion is deterministic:** every converter takes an
  explicit ``now``/``today`` — same inputs produce byte-identical blocks.
"""

import re
from datetime import date as _date
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Vocabulary — mirrors of literals defined elsewhere (kept as literals here
# so this module stays import-free; equivalence tests pin the values).
# ---------------------------------------------------------------------------

TARGET_BEDTIME = 'bedtime'
CATEGORIES = ('games', 'browsers', 'media', 'chat', 'other')
MODE_PERMITTED = 'permitted'
MODE_CAPPED = 'capped'
BLOCK_MODES = (MODE_PERMITTED, MODE_CAPPED)

# Mirror of budgets.WINDOW_MIN_MINUTES (pinned equal by tests): a capped
# block below this is malformed.
WINDOW_MIN_MINUTES = 5

PRESET_ONE = 'ONE'
PRESET_DAILY = 'DAILY'
PRESET_SCHOOL = 'SCHOOL'
WEEKLY_PREFIX = 'WEEKLY:BYDAY='
BIWEEKLY_PREFIX = 'BIWEEKLY:BYDAY='
WEEKDAY_CODES = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')
_CODE_TO_WEEKDAY = {code: i for i, code in enumerate(WEEKDAY_CODES)}

# comm charset: the same regex budgets uses for cgroup-safe names.
_COMM_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')
_ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')

_MIDNIGHT = 24 * 60

# Upper bound on expand()/overlap work: 18 months of days is far beyond any
# panel range request; longer series are periodic, so the cap never hides a
# real conflict between two unbounded-until presets within one cycle.
MAX_EXPAND_DAYS = 550


def parse_hhmm(value):
    """'HH:MM' -> minutes past midnight, or None (mirrors _parse_hhmm)."""
    try:
        hours, minutes = str(value).strip().split(':')
        h, m = int(hours), int(minutes)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError, TypeError):
        pass
    return None


def _hhmm(minutes):
    return '%02d:%02d' % (minutes // 60, minutes % 60)


def parse_date(value):
    """'YYYY-MM-DD' (or date/datetime) -> date, or None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    try:
        return datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _iso(d):
    return d.isoformat() if d is not None else None


def target_kind(target):
    """'bedtime' | 'category' | 'app' | None for a block target string."""
    if not isinstance(target, str):
        return None
    if target == TARGET_BEDTIME:
        return TARGET_BEDTIME
    if target in CATEGORIES:
        return 'category'
    if target.startswith('app:'):
        comm = target[4:]
        if comm and _COMM_RE.match(comm):
            return 'app'
    return None


def target_of(target_type, target):
    """(target_type, target) legacy pair -> canonical block target string."""
    if target_type == TARGET_BEDTIME:
        return TARGET_BEDTIME
    if target_type == 'category':
        return target if target in CATEGORIES else None
    if target_type == 'app':
        if isinstance(target, str) and target and _COMM_RE.match(target):
            return 'app:' + target
    return None


def parse_preset(value):
    """Preset string -> (freq, sorted day indices) or None.

    freq in {'ONE','DAILY','SCHOOL','WEEKLY','BIWEEKLY'}; day indices are
    ``date.weekday()`` ints (empty for ONE/DAILY/SCHOOL). Codes must be
    unique and valid; the canonical spelling re-serializes in MO..SU order.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if text in (PRESET_ONE, PRESET_DAILY, PRESET_SCHOOL):
        return (text, ())
    for prefix, freq in ((WEEKLY_PREFIX, 'WEEKLY'), (BIWEEKLY_PREFIX,
                                                     'BIWEEKLY')):
        if text.startswith(prefix):
            codes = text[len(prefix):].split(',')
            days = set()
            for code in codes:
                idx = _CODE_TO_WEEKDAY.get(code.strip())
                if idx is None:
                    return None
                if idx in days:
                    return None  # duplicates are not canonical
                days.add(idx)
            if not days:
                return None
            return (freq, tuple(sorted(days)))
    return None


def format_preset(freq, days):
    """Canonical preset string from (freq, day indices); None if invalid."""
    if freq in ('ONE', 'DAILY', 'SCHOOL'):
        return {'ONE': PRESET_ONE, 'DAILY': PRESET_DAILY,
                'SCHOOL': PRESET_SCHOOL}[freq]
    if freq not in ('WEEKLY', 'BIWEEKLY'):
        return None
    if not days or any(d < 0 or d > 6 for d in days):
        return None
    codes = ','.join(WEEKDAY_CODES[d] for d in sorted(set(days)))
    return (WEEKLY_PREFIX if freq == 'WEEKLY' else BIWEEKLY_PREFIX) + codes


def preset_for_days(days):
    """Best preset covering exactly ``days`` (weekday ints 0-6) or None.

    7 days -> DAILY, Mon-Fri -> SCHOOL, otherwise WEEKLY:BYDAY=<sel>.
    (BIWEEKLY is never derived: legacy configs have no every-2nd-week.)
    """
    try:
        sel = sorted({int(d) for d in days})
    except (TypeError, ValueError):
        return None
    if not sel or any(d < 0 or d > 6 for d in sel):
        return None
    if sel == [0, 1, 2, 3, 4, 5, 6]:
        return PRESET_DAILY
    if sel == [0, 1, 2, 3, 4]:
        return PRESET_SCHOOL
    return format_preset('WEEKLY', sel)


def _week_index(anchor, day):
    """0-based Monday-anchored week number of ``day`` within a biweekly run."""
    monday = day - timedelta(days=day.weekday())
    anchor_monday = anchor - timedelta(days=anchor.weekday())
    return (monday - anchor_monday).days // 7


def matches_date(block, day):
    """True if the block's recurrence starts an occurrence ON ``day``.

    Fails closed for malformed blocks (use normalize_block/iter_blocks).
    Honors dtstart (inclusive), until (inclusive) and the ONE/DAILY/
    SCHOOL/WEEKLY/BIWEEKLY grammar above.
    """
    if not isinstance(block, dict):
        return False
    parsed = parse_preset(block.get('preset'))
    start = parse_date(block.get('dtstart'))
    if parsed is None or start is None or day < start:
        return False
    until = block.get('until')
    if until is not None:
        until = parse_date(until)
        if until is None or day > until:
            return False
    freq, days = parsed
    if freq == 'ONE':
        return day == start
    if freq == 'DAILY':
        return True
    if day.weekday() not in days and freq in ('WEEKLY', 'BIWEEKLY'):
        return False
    if freq == 'SCHOOL' and day.weekday() > 4:
        return False
    if freq == 'BIWEEKLY':
        return _week_index(start, day) % 2 == 0
    return True


def block_interval(block, day):
    """Concrete (start_dt, end_dt) for the occurrence anchored on ``day``.

    end < start means the interval runs past midnight into day+1.
    Returns None when the block does not occur on ``day`` or is malformed.
    """
    if not matches_date(block, day):
        return None
    start = parse_hhmm(block.get('start'))
    end = parse_hhmm(block.get('end'))
    if start is None or end is None or start == end:
        return None
    s_dt = datetime(day.year, day.month, day.day) + timedelta(minutes=start)
    e_dt = datetime(day.year, day.month, day.day) + timedelta(minutes=end)
    if end < start:
        e_dt += timedelta(days=1)
    return (s_dt, e_dt)


def is_block_active(block, now):
    """Minute-resolution activity check for ONE stored block.

    A block whose occurrence started YESTERDAY and crosses midnight is
    active "now" if yesterday matched and the time is inside the tail —
    exactly the predicate in app.is_in_bedtime / budgets._in_window.
    Enabled flag honored; malformed -> False (fail closed).
    """
    if not isinstance(block, dict) or not block.get('enabled', False):
        return False
    start = parse_hhmm(block.get('start'))
    end = parse_hhmm(block.get('end'))
    if start is None or end is None or start == end:
        return False
    cur = now.hour * 60 + now.minute
    today = now.date()
    if start < end:
        if not matches_date(block, today):
            return False
        return start <= cur < end
    if matches_date(block, today) and cur >= start:
        return True
    yesterday = today - timedelta(days=1)
    if matches_date(block, yesterday) and cur < end:
        return True
    return False


def normalize_block(raw, block_id=None):
    """A validated canonical block dict, or None (FAIL CLOSED).

    Requirements: valid id (or ``block_id`` override), valid target, mode
    consistent with the target (bedtime -> None; category/app -> permitted
    or capped), capped -> int allowance >= WINDOW_MIN_MINUTES, parseable
    canonical preset (WEEKLY/BIWEEKLY -> explicit BYDAY list, canonical
    order, no duplicates), dtstart required (ISO date; for ONE it IS the
    occurrence date), start/end HH:MM with start != end, optional inclusive
    until >= dtstart, bool enabled, set_by/set_at passed through.
    Times are canonicalized to 'HH:MM'; ONE/DAILY/SCHOOL carry no BYDAY.
    """
    if not isinstance(raw, dict):
        return None
    bid = block_id if block_id is not None else raw.get('id')
    if not isinstance(bid, str) or not _ID_RE.match(bid):
        return None
    kind = target_kind(raw.get('target'))
    if kind is None:
        return None
    mode = raw.get('mode')
    if kind == TARGET_BEDTIME:
        if mode is not None:
            return None
    else:
        if mode not in BLOCK_MODES:
            return None
    allowance = None
    if mode == MODE_CAPPED:
        try:
            allowance = int(raw.get('allowance_minutes'))
        except (TypeError, ValueError):
            return None
        if allowance < WINDOW_MIN_MINUTES:
            return None
    parsed = parse_preset(raw.get('preset'))
    if parsed is None:
        return None
    freq, days = parsed
    preset = format_preset(freq, days)
    if preset is None:
        return None
    dtstart = parse_date(raw.get('dtstart'))
    if dtstart is None:
        return None
    start = parse_hhmm(raw.get('start'))
    end = parse_hhmm(raw.get('end'))
    if start is None or end is None or start == end:
        return None
    until = raw.get('until')
    if until is not None:
        until = parse_date(until)
        if until is None or until < dtstart:
            return None
    return {
        'id': bid,
        'target': raw.get('target'),
        'mode': mode,
        'allowance_minutes': allowance,
        'preset': preset,
        'dtstart': _iso(dtstart),
        'start': _hhmm(start),
        'end': _hhmm(end),
        'until': _iso(until),
        'enabled': bool(raw.get('enabled', True)),
        'set_by': raw.get('set_by'),
        'set_at': raw.get('set_at'),
    }


def doc_is_migrated(doc):
    """True if the user's schedule doc carries the migration marker."""
    return isinstance(doc, dict) and bool(doc.get('migrated'))


def iter_blocks(doc):
    """(block_id, normalized_block) for every VALID stored block.

    Whole-doc style like overrides: doc is {username-doc} shape
    ``{'events': {id: {...}}, 'migrated': iso}``. Malformed entries are
    skipped (fail closed); doc/key garbage yields nothing.
    """
    if not isinstance(doc, dict):
        return
    events = doc.get('events')
    if not isinstance(events, dict):
        return
    for bid, value in events.items():
        norm = normalize_block(value, block_id=bid)
        if norm is not None:
            yield bid, norm


def blocks_for(doc, target):
    """Normalized blocks targeting ``target`` (any enabled state)."""
    return [b for _bid, b in iter_blocks(doc) if b['target'] == target]


def active_blocks(doc, target_type, target, now):
    """Enabled blocks of one target active at ``now`` (list, may be many
    across targets but at most one per target for non-overlapping sets).

    ``target_type``/``target`` use the legacy pair spelling (bedtime/
    category/app). Unknown target -> [].
    """
    canonical = target_of(target_type, target)
    if canonical is None:
        return []
    return [b for b in blocks_for(doc, canonical)
            if is_block_active(b, now)]


def has_enabled_blocks(doc, target_type, target):
    """True if the target is governed by >=1 ENABLED block (gating probe)."""
    canonical = target_of(target_type, target)
    if canonical is None:
        return False
    return any(b['enabled'] for b in blocks_for(doc, canonical))


def active_capped_block(doc, target_type, target, now):
    """The active CAPPED block governing the target (window replacement)."""
    for block in active_blocks(doc, target_type, target, now):
        if block['mode'] == MODE_CAPPED:
            return block
    return None


def permitted_active(doc, target_type, target, now):
    """True while an enabled PERMITTED block of the target is active."""
    for block in active_blocks(doc, target_type, target, now):
        if block['mode'] == MODE_PERMITTED:
            return True
    return False


def in_bedtime(doc, now):
    """Device-wide bedtime predicate over the schedule doc.

    True iff an enabled bedtime-target block is active at ``now``. This is
    the replacement for the legacy ``is_in_bedtime(entry)`` table read once
    a user is migrated (CALENDAR_DESIGN.md: bedtime blocks lock/unlock with
    today's semantics; midnight-crossing handled by is_block_active).
    """
    return bool(active_blocks(doc, TARGET_BEDTIME, TARGET_BEDTIME, now))


def next_start(blocks, now, target=None):
    """(start_dt, block) of the earliest ENABLED occurrence starting after
    ``now``, or (None, None).

    Scans 15 days — the weekly/biweekly cycle (14 days) plus a day of
    slack — so any recurring series that will restart within a fortnight
    is found. Pure; blocks may be raw (normalized here, fail closed).
    ``target`` optionally restricts to one canonical target string.
    """
    best = (None, None)
    if not isinstance(now, datetime):
        return best
    day = now.date()
    horizon = day + timedelta(days=15)
    source = blocks if isinstance(blocks, (list, tuple)) else \
        [b for _bid, b in iter_blocks(blocks)]
    candidates = []
    for block in source:
        norm = normalize_block(block)
        if norm is not None and norm['enabled'] and norm.get('start'):
            candidates.append(norm)
    while day <= horizon:
        for block in candidates:
            if target is not None and block['target'] != target:
                continue
            interval = block_interval(block, day)
            if interval is None or interval[0] <= now:
                continue
            if best[0] is None or interval[0] < best[0]:
                best = (interval[0], block)
        day += timedelta(days=1)
    return best


def next_bedtime_start(doc, now):
    """(start_dt, block) for the next bedtime lock after ``now``."""
    return next_start(doc, now, target=TARGET_BEDTIME)


def current_bedtime_block(doc, now):
    """The active bedtime block at ``now`` (id-order first), or None."""
    active = active_blocks(doc, TARGET_BEDTIME, TARGET_BEDTIME, now)
    return active[0] if active else None


# ---------------------------------------------------------------------------
# Legacy-shape readers (pure) — the migration source + the fallback path
# while a user still has no schedule doc.
# ---------------------------------------------------------------------------

def in_bedtime_entry(entry, now):
    """The ORIGINAL bedtime predicate, relocated verbatim (fail closed).

    ``entry`` is the bedtime table row {start, end, days}. Kept so app.py
    and agent.py shims can evaluate legacy (non-migrated) configs with the
    exact historical semantics; the equivalence tests pin agreement
    between this and in_bedtime(migrated_doc).
    """
    if not entry:
        return False
    start = parse_hhmm(entry.get('start', ''))
    end = parse_hhmm(entry.get('end', ''))
    try:
        days = {int(d) for d in entry.get('days', [])}
    except (ValueError, TypeError):
        return False
    if start is None or end is None or not days or start == end:
        return False
    preset = preset_for_days(sorted(days))
    if preset is None:
        return False   # day numbers outside 0..6 never match (fail closed)
    block = normalize_block({
        'id': 'legacy-bedtime', 'target': TARGET_BEDTIME, 'mode': None,
        'preset': preset,
        'dtstart': _iso(now.date() - timedelta(days=1)),
        'start': entry.get('start'), 'end': entry.get('end'),
        'enabled': True,
    })
    if block is None:
        return False
    return is_block_active(block, now)


def window_to_block(window, today, block_id=None):
    """Legacy normalized window -> capped block (deterministic, pure).

    ``window`` is a normalize_window() result (target_type/target/days/
    start/end/allowance_minutes/enabled). ``today`` anchors the series:
    dtstart is set to the day BEFORE so a midnight-crossing window that
    is active right now (its tail belongs to yesterday's anchor) keeps
    enforcing across the cutover — the extra "yesterday" anchor can never
    fire again once today has passed. The block id DEFAULTS to the window
    id so window_used/window_active runtime keys survive migration
    untouched. Returns None when the pair cannot round-trip.
    """
    if not isinstance(window, dict):
        return None
    target = target_of(window.get('target_type'), window.get('target'))
    if target is None or target == TARGET_BEDTIME:
        return None
    preset = preset_for_days(window.get('days') or [])
    if preset is None:
        return None
    return normalize_block({
        'id': block_id or window.get('id'),
        'target': target,
        'mode': MODE_CAPPED,
        'allowance_minutes': window.get('allowance_minutes'),
        'preset': preset,
        'dtstart': _iso(today - timedelta(days=1)),
        'start': window.get('start'),
        'end': window.get('end'),
        'enabled': bool(window.get('enabled', True)),
        'set_by': window.get('set_by'),
        'set_at': window.get('set_at'),
    }, block_id=block_id or window.get('id'))


# ---------------------------------------------------------------------------
# Migration converters (pure, deterministic for a fixed `now`)
# ---------------------------------------------------------------------------

def blocks_from_bedtime_entry(entry, now):
    """Bedtime table row -> [one bedtime block] (or [] when absent/malformed).

    days -> preset (DAILY / SCHOOL / WEEKLY:BYDAY); start/end copied; the
    device locks during the block exactly like today. dtstart = the day
    BEFORE ``now`` so a currently-running overnight window's tail keeps
    locking across the cutover (it cannot fire "again" for a past evening).
    """
    if not entry or not isinstance(entry, dict):
        return []
    start = parse_hhmm(entry.get('start', ''))
    end = parse_hhmm(entry.get('end', ''))
    try:
        days = sorted({int(d) for d in entry.get('days', [])})
    except (ValueError, TypeError):
        return []
    if start is None or end is None or start == end or not days:
        return []
    block = normalize_block({
        'id': 'b-bedtime-1',
        'target': TARGET_BEDTIME,
        'mode': None,
        'preset': preset_for_days(days),
        'dtstart': _iso(now.date() - timedelta(days=1)),
        'start': _hhmm(start),
        'end': _hhmm(end),
        'enabled': True,
        'set_by': entry.get('set_by'),
        'set_at': now.isoformat(timespec='seconds'),
    })
    return [block] if block is not None else []


def blocks_from_windows(windows, now):
    """[normalized windows] -> [capped blocks]. DISABLED or dead windows
    are NOT migrated (per the owner-approved mapping). Ids are preserved.
    """
    out = []
    for win in windows or []:
        if not isinstance(win, dict) or not win.get('enabled', True):
            continue
        block = window_to_block(win, now.date())
        if block is not None:
            out.append(block)
    return out


def migrate(bedtime_entry, windows, now):
    """Build the migrated schedule doc from legacy config (pure).

    Deterministic for a fixed ``now``; callers persist and set the marker.
    Empty result -> {'events': {}, 'migrated': iso} so the marker still
    records the cutover (never resurrect legacy windows afterwards).
    """
    events = {}
    for block in blocks_from_bedtime_entry(bedtime_entry, now):
        events[block['id']] = block
    for block in blocks_from_windows(windows, now):
        events[block['id']] = block
    return {'events': events,
            'migrated': now.isoformat(timespec='seconds')}


# ---------------------------------------------------------------------------
# Expansion (for the future UI endpoint) + overlap validation
# ---------------------------------------------------------------------------

def expand(start_dt, end_dt, blocks):
    """Concrete [start_iso, end_iso) intervals for ``blocks`` in a range.

    Handles ONE, midnight-crossing (an occurrence anchored the day BEFORE
    the range may bleed into it), week/month boundaries and UNTIL edges.
    Returns sorted dicts:
      {'block_id','target','mode','allowance_minutes','start','end'}
    Malformed blocks and garbage ranges yield []. The scan horizon is
    capped at MAX_EXPAND_DAYS (fail-closed against absurd ranges).
    """
    out = []
    if not isinstance(start_dt, datetime) or not isinstance(end_dt, datetime):
        return []
    if end_dt <= start_dt:
        return []
    days = (end_dt.date() - start_dt.date()).days
    if days > MAX_EXPAND_DAYS:
        return []
    candidates = list(iter_blocks({'events': {b.get('id'): b
                                              for b in blocks
                                              if isinstance(b, dict)}})) \
        if isinstance(blocks, (list, tuple)) else list(iter_blocks(blocks))
    # One day before: yesterday's occurrence may cross midnight into range.
    day = start_dt.date() - timedelta(days=1)
    last = end_dt.date()
    while day <= last:
        for _bid, block in candidates:
            interval = block_interval(block, day)
            if interval is None:
                continue
            s_dt, e_dt = interval
            if e_dt <= start_dt or s_dt >= end_dt:
                continue  # outside the query range
            out.append({
                'block_id': block['id'],
                'target': block['target'],
                'mode': block['mode'],
                'allowance_minutes': block['allowance_minutes'],
                'start': s_dt.isoformat(),
                'end': e_dt.isoformat(),
            })
        day += timedelta(days=1)
    # Only ENABLED blocks expand for the UI; drop disabled silently.
    out = [row for row in out if _enabled_in(candidates, row['block_id'])]
    out.sort(key=lambda row: (row['start'], row['block_id']))
    return out


def _enabled_in(blocks, block_id):
    for _bid, block in blocks:
        if block['id'] == block_id:
            return bool(block.get('enabled'))
    return False


def _occurs_same_day(a, b, day):
    """Half-open interval intersection of two blocks anchored on ``day``."""
    ia = block_interval(a, day)
    ib = block_interval(b, day)
    if ia is None or ib is None:
        return False
    return ia[0] < ib[1] and ib[0] < ia[1]


def find_overlap(blocks, candidate):
    """Id of an existing ENABLED same-target block conflicting with
    ``candidate``, else None.

    Same child+target blocks must not overlap in time (owner rule #4);
    different targets never conflict. The search window is the 14-day
    periodic cycle starting at the later dtstart (both presets repeat on a
    period dividing 14 days), bounded by the earlier until — exact for the
    preset grammar. ONE blocks are matched by their single date directly.
    """
    cand = candidate if candidate.get('enabled', True) else None
    if cand is None:
        return None
    target = cand['target']
    lo = parse_date(cand['dtstart'])
    hi = parse_date(cand['until']) if cand['until'] else None
    for _bid, other in iter_blocks({'events': {b['id']: b for b in blocks
                                               if isinstance(b, dict)}}):
        if other['id'] == cand['id'] or other['target'] != target:
            continue
        if not other.get('enabled', True):
            continue
        o_lo = parse_date(other['dtstart'])
        o_hi = parse_date(other['until']) if other['until'] else None
        window_start = max(lo, o_lo)
        bounds = [b for b in (hi, o_hi) if b is not None]
        window_end = min(bounds) if bounds else window_start + timedelta(days=13)
        if window_end < window_start:
            continue
        day = window_start
        while day <= min(window_end,
                         window_start + timedelta(days=13)):
            if _occurs_same_day(cand, other, day):
                return other['id']
            day += timedelta(days=1)
        # Crossing-midnight tail: an occurrence anchored the day BEFORE
        # another's anchor can still overlap it; the day-by-day scan above
        # compares same-anchor intervals, so also probe previous days.
        day = window_start + timedelta(days=1)
        while day <= min(window_end,
                         window_start + timedelta(days=14)):
            prev = day - timedelta(days=1)
            ic = block_interval(cand, prev)
            io = block_interval(other, day)
            if ic is not None and io is not None and \
                    ic[0] < io[1] and io[0] < ic[1]:
                return other['id']
            ic = block_interval(cand, day)
            io = block_interval(other, prev)
            if ic is not None and io is not None and \
                    ic[0] < io[1] and io[0] < ic[1]:
                return other['id']
            day += timedelta(days=1)
    return None
