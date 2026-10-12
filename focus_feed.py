"""Panel-side focus feed: strict parser/validator, liveness, aggregation.

This is the *panel* half of the per-user session agent
(``SESSION_AGENT_PLAN.md`` §8). It is stdlib-only and has no side effects on
import: nothing here touches the network, the auth store, ``state.db`` or any
child's session. The child-side agent (``session_agent.py``) writes a JSONL
feed; this module turns that text into per-category focused seconds.

Design rules pinned by ``tests/test_session_agent.py``:

* **Strict, fail-closed parsing.** Malformed rows are counted and skipped;
  a body that violates the size cap is rejected outright; a non-monotonic
  ``seq`` is an error the aggregator refuses to bill. Missing/stale data is
  *never* silently treated as zero engaged time (see ``aggregate_focus``'s
  ``billable``/``degraded`` flags).
* **Only bill what you can freeze.** A focused window contributes seconds to
  category *C* only when at least one live process matching that window is a
  freezable member of *C* (visible, categorized, not NEVER-pause, limit
  configured) — reusing ``budgets.freezable_members`` so the panel and the UI
  can never disagree.
* **Never count gaps as zero.** Intervals are the only thing billed; a feed
  with no fresh liveness marks the aggregate ``degraded`` and ``billable``
  false so the caller stops billing instead of assuming zero.

Clock: focus rows carry ``bt`` (``CLOCK_BOOTTIME`` seconds) and ``ts`` (ISO).
Interval math is done in boottime seconds, which advance across suspend (unlike
``CLOCK_MONOTONIC``). ``aggregate_focus`` therefore takes its ``window`` as a
pair of boottime seconds; use :func:`wallclock_to_boottime` to map a tick's
wall-clock window onto that axis.
"""

import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone

# --- schema / limits (mirrored in session_agent.py; a test pins equality) ---
SCHEMA_VERSION = 1
AGENT_TYPES = ('focus', 'state', 'error')
KINDS = ('app', 'shell', 'lockscreen')
STATES = ('active', 'no_session', 'suspend', 'no_engine', 'locked')

MAX_LINE_BYTES = 4096
MAX_FEED_BYTES = 128 * 1024
MAX_ROWS_PER_DRAIN = 200
# Missing/stale heartbeat beyond this many seconds -> the caller must fail
# closed (no_data), never bill zero.
FEED_STALE_SECONDS = 180.0
# Longest single focus session the panel will ever bill; anything longer is a
# sleep/clock artifact and is clamped.
MAX_FOCUS_SESSION_SECONDS = 4 * 3600.0
# Kept from the CPU engine: a single tick can never bill phantom hours.
MAX_TICK_WALL_SECONDS = 300.0

WINDOW_TOKEN_RE = re.compile(r'^[A-Za-z0-9_.\-+]+$')
FEED_PATH_RE = re.compile(r'^/run/user/[0-9]+/parental-controls/focus\.jsonl$')


class FeedError(Exception):
    """A feed body violated a hard invariant the panel must not bill around."""


def feed_path(uid):
    """The feed path for a child's runtime dir; uid is coerced to int."""
    return '/run/user/%d/parental-controls/focus.jsonl' % int(uid)


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------

def _valid_window_token(value):
    return bool(value) and bool(WINDOW_TOKEN_RE.match(value))


def _parse_ts(value):
    """Parse an ISO-8601 timestamp into epoch seconds; None if unusable."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _missing_result(feed_seq):
    return {
        'rows': [], 'errors': [], 'ignored_below_cursor': 0,
        'stale': True, 'missing': True, 'too_large': False,
        'rate_limited': False, 'boot_changed': False, 'truncated_tail': False,
        'last_seq': feed_seq, 'last_bt': None, 'last_heartbeat_bt': None,
        'states': [], 'degraded': True,
    }


def parse_feed(text, feed_seq=1, expected_boot_id=None, now_bt=None):
    """Parse a feed body, returning a result dict. Never raises.

    ``text`` is the raw file body (or None when the file is missing).
    ``feed_seq`` is the last seq the panel billed; rows at or below it are
    ignored (already accounted). Rows above it are returned in ``rows``.

    Result keys: rows, errors, ignored_below_cursor, stale, missing,
    too_large, rate_limited, boot_changed, truncated_tail, last_seq,
    last_bt, last_heartbeat_bt, states, degraded.
    """
    if text is None:
        return _missing_result(feed_seq)
    result = {
        'rows': [], 'errors': [], 'ignored_below_cursor': 0,
        'stale': False, 'missing': False, 'too_large': False,
        'rate_limited': False, 'boot_changed': False, 'truncated_tail': False,
        'last_seq': feed_seq, 'last_bt': None, 'last_heartbeat_bt': None,
        'states': [], 'degraded': False,
    }
    if not isinstance(text, str):
        text = text.decode('utf-8', 'replace')
    try:
        body_bytes = len(text.encode('utf-8'))
    except Exception:
        body_bytes = len(text)
    if body_bytes > MAX_FEED_BYTES:
        # Fail closed: refuse to parse an oversized body rather than bill a
        # partial view of it.
        result['too_large'] = True
        result['errors'].append('feed exceeds size cap')
        result['stale'] = True
        result['degraded'] = True
        return result

    truncated_tail = bool(text) and not text.endswith('\n')
    lines = text.splitlines()
    last_seq = feed_seq
    boot = None
    valid = 0

    for index, raw in enumerate(lines):
        is_last = index == len(lines) - 1
        line = raw.strip()
        if not line:
            continue
        if len(line.encode('utf-8')) > MAX_LINE_BYTES:
            result['errors'].append('line too long')
            if is_last and truncated_tail:
                result['truncated_tail'] = True
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # A truncated final line is expected when the panel catches the
            # agent mid-write; tolerate it without an error.
            if is_last and truncated_tail:
                result['truncated_tail'] = True
            else:
                result['errors'].append('malformed JSON')
            continue
        if not isinstance(row, dict):
            result['errors'].append('row is not an object')
            continue
        error = _validate_row(row)
        if error:
            result['errors'].append(error)
            continue
        seq = int(row['seq'])
        if seq <= feed_seq:
            result['ignored_below_cursor'] += 1
            continue
        if seq <= last_seq and result['rows']:
            # Non-monotonic within the new rows: hard error, fail closed.
            result['errors'].append('non-monotonic seq %d' % seq)
            continue
        if valid >= MAX_ROWS_PER_DRAIN:
            result['rate_limited'] = True
            break
        row_boot = row.get('boot_id')
        if expected_boot_id and row_boot and row_boot != expected_boot_id:
            result['boot_changed'] = True
            boot = row_boot
        elif boot is None:
            boot = row_boot
        result['rows'].append(row)
        valid += 1
        last_seq = seq
        bt = float(row['bt'])
        if result['last_bt'] is None or bt > result['last_bt']:
            result['last_bt'] = bt
        if row['type'] == 'state':
            if (result['last_heartbeat_bt'] is None
                    or bt > result['last_heartbeat_bt']):
                result['last_heartbeat_bt'] = bt
            state = row.get('state')
            if state not in result['states']:
                result['states'].append(state)

    result['last_seq'] = last_seq
    result['boot_id'] = boot
    if now_bt is not None:
        result['stale'] = is_stale(result['last_bt'], now_bt)
    result['degraded'] = (result['last_bt'] is None) or result['stale']
    return result


def _validate_row(row):
    """Return an error string for an invalid row, or '' when it is valid."""
    if row.get('v') != SCHEMA_VERSION:
        return 'bad schema version'
    if row.get('type') not in AGENT_TYPES:
        return 'unknown row type'
    seq = row.get('seq')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return 'bad seq'
    if not isinstance(row.get('boot_id'), str) or not row.get('boot_id'):
        return 'bad boot_id'
    if not _is_number(row.get('bt')) or row['bt'] < 0:
        return 'bad bt'
    if _parse_ts(row.get('ts')) is None:
        return 'bad ts'
    rtype = row['type']
    if rtype == 'focus':
        if not isinstance(row.get('session'), int) \
                or isinstance(row['session'], bool) or row['session'] < 0:
            return 'bad session'
        if row.get('kind') not in KINDS:
            return 'bad kind'
        wm = row.get('wm_class')
        app = row.get('app_id')
        if wm is None and app is None:
            return 'focus row missing window identity'
        if wm is not None and not _valid_window_token(wm):
            return 'bad wm_class'
        if app is not None and not _valid_window_token(app):
            return 'bad app_id'
        since = row.get('since')
        if not _is_number(since) or since < 0 \
                or since > MAX_FOCUS_SESSION_SECONDS:
            return 'bad since'
    elif rtype == 'state':
        if row.get('state') not in STATES:
            return 'bad state'
    else:  # error
        if not isinstance(row.get('code'), str) or not row['code']:
            return 'bad error code'
    return ''


def is_stale(last_bt, now_bt, grace=FEED_STALE_SECONDS):
    """True when the last liveness seen is missing or older than ``grace``."""
    if last_bt is None:
        return True
    return (now_bt - last_bt) > grace


def load_feed(path, feed_seq=1, expected_boot_id=None, now_bt=None):
    """Read and parse ``path``. A missing/unreadable file is 'stale'."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    except (OSError, IOError):
        return _missing_result(feed_seq)
    return parse_feed(text, feed_seq=feed_seq,
                      expected_boot_id=expected_boot_id, now_bt=now_bt)


# ---------------------------------------------------------------------------
# Transport (panel pulls the child's feed through a scoped sudoers grant)
# ---------------------------------------------------------------------------

def drain_argv(username, uid, wait=True):
    """The exact argv the panel runs to pull one child's feed.

    ``sudo systemd-run --machine=<child>@.host --user [--wait --collect]
    cat /run/user/<uid>/parental-controls/focus.jsonl``

    The sudoers grant (``sudoers-parental-controls``) pins this argv shape and
    the run-as target is root only because the machine transport needs it;
    the payload then runs inside the child's own user manager. The child's
    feed is fixed-path and the command performs no other action.

    NOTE (verify on a real box): whether ``--machine=<u>@.host --user`` reaches
    a *lingering, logged-out* user manager cannot be verified without a live
    child session (SESSION_AGENT_PLAN.md open question 1). If it does not, the
    fallback channel is a socket push; this module's schema and aggregation are
    unchanged either way.
    """
    argv = ['sudo', 'systemd-run', '--machine=%s@.host' % username, '--user']
    if wait:
        argv += ['--wait', '--collect']
    argv += ['cat', feed_path(uid)]
    return argv


def drain_feed(username, uid, timeout=15, runner=None):
    """Run the drain command and return (text, error). text is None on failure.

    A transport failure is deliberately *not* mapped to an empty feed: the
    caller sees ``None`` and must treat the user as measurement-degraded
    (fail closed), never as zero usage.
    """
    if runner is None:
        runner = subprocess.run
    argv = drain_argv(username, uid)
    # ``-n`` is consumed by sudo itself (never part of the sudoers match).
    argv = [argv[0], '-n'] + argv[1:]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # subprocess errors, timeouts, missing sudo...
        return None, str(exc)
    if getattr(proc, 'returncode', 1) != 0:
        return None, (getattr(proc, 'stderr', '') or 'drain failed').strip()
    return proc.stdout, ''


# ---------------------------------------------------------------------------
# Categorization + freezable gate (reuses budgets' machinery)
# ---------------------------------------------------------------------------

def _categories():
    try:
        import budgets
        return tuple(budgets.CATEGORIES)
    except Exception:
        return ('games', 'browsers', 'media', 'chat', 'other')


def map_window_to_category(wm_class, app_id, index=None):
    """Map a focused window to a budget category via the panel's rules.

    Mirrors ``budgets.categorize()``: curated overrides over
    ``wm_class + app_id`` first, then the ``.desktop`` index, else ``other``.
    Falls back to ``other`` (never raises) when the budgets machinery is not
    importable, so aggregation stays total.
    """
    try:
        import budgets
        proc = {'comm': (wm_class or '').lower(),
                'cmdline': ' '.join(x for x in (wm_class, app_id) if x)}
        return budgets.categorize(proc, index)
    except Exception:
        return 'other'


def window_matches_proc(wm_class, app_id, proc):
    """True when a focused window plausibly names this process."""
    comm = (proc.get('comm') or '').lower()
    cmdline = (proc.get('cmdline') or proc.get('args') or '').lower()
    wc = (wm_class or '').lower()
    aid = (app_id or '').lower()
    if wc and wc == comm:
        return True
    if aid and aid == comm:
        return True
    if wc and wc in cmdline:
        return True
    if aid and aid in cmdline:
        return True
    if wc:
        argv0 = cmdline.split(None, 1)[0] if cmdline else ''
        base = argv0.rsplit('/', 1)[-1]
        if base and base == wc:
            return True
    return False


def _freezable_pids(procs, username, category, limits, prefs, index):
    """PIDs that are freezable members of ``category`` (budgets machinery).

    Returns an empty set (nothing billable, fail closed) if the machinery is
    unavailable or the category has no configured limit.
    """
    try:
        import budgets
        members = budgets.freezable_members(procs, username, category,
                                            limits, prefs, index)
    except Exception:
        return set()
    return {p.get('pid') for p in members}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _window_bounds(window):
    start, end = window
    if end < start:
        start, end = end, start
    return float(start), float(end)


def wallclock_to_boottime(dt, now_dt, now_bt):
    """Map a wall-clock datetime onto the boottime axis via a fresh reference.

    ``now_dt``/``now_bt`` are sampled together (``datetime.now`` and
    ``CLOCK_BOOTTIME``). The result can be negative for times before boot.
    """
    return now_bt + (dt - now_dt).total_seconds()


def aggregate_focus(rows, window, procs=None, limits=None, username=None,
                    prefs=None, index=None, heartbeat_bt=None, now_bt=None,
                    cap_seconds=MAX_TICK_WALL_SECONDS):
    """Turn new feed rows into per-category focused seconds.

    ``rows`` is either an iterable of validated feed rows or the result dict
    from :func:`parse_feed`. ``window`` is a ``(start, end)`` pair of boottime
    seconds (see :func:`wallclock_to_boottime`). ``procs`` is a live ``ps``
    snapshot used only for the freezable gate; ``limits``/``prefs``/``index``
    are the same values ``budgets`` uses.

    Returns::

        {'seconds': {category: float}, 'total': float,
         'billable': bool, 'degraded': bool, 'stats': {...}}

    ``billable`` is False when the feed has no fresh liveness inside the
    window: the caller must **not** bill (that is the "never count gaps as
    zero" rule). Malformed sequencing raises :class:`FeedError` (fail closed).
    """
    parsed_boot = None
    if isinstance(rows, dict) and 'rows' in rows:
        parsed = rows
        rows = parsed['rows']
        parsed_boot = parsed.get('last_heartbeat_bt')
    rows = list(rows)
    start, end = _window_bounds(window)
    limits = limits or {}
    procs = list(procs or [])
    if heartbeat_bt is None:
        heartbeat_bt = parsed_boot
    focus_in_order = [r for r in rows if r.get('type') == 'focus']
    _assert_monotonic(focus_in_order)
    focus = sorted(focus_in_order, key=lambda r: r['seq'])

    liveness = [float(r['bt']) for r in rows
                if isinstance(r.get('bt'), (int, float))]
    last_liveness = max(liveness) if liveness else None
    if heartbeat_bt is None:
        heartbeat_bt = last_liveness
    # A feed with no liveness in the window is degraded: bill nothing.
    degraded = (last_liveness is None) or (last_liveness < start)
    if now_bt is not None and is_stale(last_liveness, now_bt):
        degraded = True

    horizon = end
    if heartbeat_bt is not None:
        horizon = min(end, heartbeat_bt)

    cats = _categories()
    seconds = {cat: 0.0 for cat in cats}
    stats = {'intervals': 0, 'skipped_kind': 0, 'not_billable': 0,
             'unmapped': 0, 'clamped': 0, 'degraded': degraded}
    freezable = {cat: _freezable_pids(procs, username, cat, limits, prefs,
                                      index)
                 for cat in cats}

    for i, row in enumerate(focus):
        if row.get('kind') != 'app':
            stats['skipped_kind'] += 1
            continue
        cat = map_window_to_category(row.get('wm_class'), row.get('app_id'),
                                     index)
        if cat not in seconds:
            stats['unmapped'] += 1
            seconds[cat] = 0.0
        pids = freezable.get(cat, set())
        if not pids or not any(window_matches_proc(row.get('wm_class'),
                                                   row.get('app_id'), p)
                               for p in procs if p.get('pid') in pids):
            # Only bill what you can freeze.
            stats['not_billable'] += 1
            continue
        start_i = float(row['bt'])
        if i + 1 < len(focus):
            nxt = focus[i + 1]
            end_i = float(nxt['bt'])
            declared = nxt.get('since')
            if _is_number(declared):
                end_i = min(end_i, start_i + max(0.0, declared))
        else:
            end_i = horizon if horizon is not None else end
        duration = end_i - start_i
        if duration <= 0:
            continue
        if duration > MAX_FOCUS_SESSION_SECONDS:
            duration = MAX_FOCUS_SESSION_SECONDS
            stats['clamped'] += 1
        lo = max(start_i, start)
        hi = min(start_i + duration, end)
        if hi <= lo:
            continue
        seconds[cat] += hi - lo
        stats['intervals'] += 1

    total = sum(seconds.values())
    if cap_seconds is not None and total > cap_seconds > 0:
        scale = cap_seconds / total
        seconds = {cat: value * scale for cat, value in seconds.items()}
        total = cap_seconds
        stats['tick_capped'] = True
    return {'seconds': seconds, 'total': total, 'billable': not degraded,
            'degraded': degraded, 'stats': stats}


def _assert_monotonic(focus_rows):
    prev = None
    for row in focus_rows:
        seq = row.get('seq')
        if prev is not None and seq <= prev:
            raise FeedError('non-monotonic focus seq: %r after %r'
                            % (seq, prev))
        prev = seq


# ---------------------------------------------------------------------------
# Miscellaneous helpers
# ---------------------------------------------------------------------------

def parse_iso(value):
    """Public ISO helper (epoch seconds or None); mirrors row validation."""
    return _parse_ts(value)


def boot_id_path():
    return '/proc/sys/kernel/random/boot_id'
