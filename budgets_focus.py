"""Focus engine (per-user session agent): feed cursor, billing, degrade.

Opt-in per child: when a budget entry's `focus_engine` is a focus-family
value the tick bills per-category *focused* seconds drained from the child's
validated feed (`focus_feed`) instead of the CPU-delta gate.
`focus_engine_of` normalizes the configured engine (failing closed to 'cpu'),
`_default_focus_provider` pulls the feed, `_focus_billing` validates and
aggregates it, and `_feed_cursor_update`/`_locked_feed_bookkeeping` advance
the drain watermark. A missing/stale/malformed feed HALTs billing for that
tick rather than silently billing zero (SESSION_AGENT_PLAN.md §8/§9).

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 8 — the
cycle-breaker slice.

**The mutual lazy import stays lazy.** `import focus_feed` remains INSIDE
`_default_focus_provider`/`_focus_billing` exactly as before, because
`focus_feed` imports `budgets` at module level. Both sides defer or the
panel cannot boot; this module must never grow a module-level edge to
`focus_feed`, and `focus_feed` must never grow one to `budgets_*`
(RESTRUCTURE_PLAN.md §7 risk R2).
"""

import time


# Focus engine plumbing (feed cursor, provider, per-tick aggregation)
# ---------------------------------------------------------------------------

def focus_engine_of(entry):
    """Normalized engine for a budget entry, failing closed to ``'cpu'``.

    An absent/unknown value means today's CPU behavior (never a silent
    engine switch). ``'no_data'`` is a focus-family engine: it keeps
    probing the feed and recovers to ``'focus'`` once fresh. ``'agent'`` is
    accepted as an alias of ``'focus'`` (SESSION_AGENT_PLAN.md §8.4).
    """
    import budgets
    raw = (entry or {}).get('focus_engine', budgets.FOCUS_ENGINE_CPU)
    if raw in budgets._FOCUS_ENGINE_ALIASES:
        return budgets.FOCUS_ENGINE_FOCUS
    if raw in budgets.FOCUS_ENGINES:
        return raw
    return budgets.FOCUS_ENGINE_CPU


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
    import budgets
    entry = entry or {}
    engine = focus_engine_of(entry)
    configured = engine in budgets.FOCUS_FAMILY
    return {
        'engine': engine,
        'configured': configured,
        'degraded': configured and (
            engine == budgets.FOCUS_ENGINE_NO_DATA
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
    import budgets
    import focus_feed
    feed_seq = entry.get('feed_seq', 0)
    try:
        uid = budgets.pwd.getpwnam(username).pw_uid
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
    import budgets
    import focus_feed
    info = {'engine': budgets.FOCUS_ENGINE_FOCUS, 'billable': False,
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
