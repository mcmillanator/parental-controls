"""The per-target enforcement decision and the per-user tick.

`_enforce_target` is the shared billing + freeze state machine for ONE
target (category or app): charge the tick's wall clock when anything was
active, warn when minutes run low, and freeze/thaw through the scoped cgroup
verbs behind the readback safety gate. `account_and_enforce` runs it for one
user: admin self-protection, weekly-window open/close transitions, the daily
reset (thawing everything not covered by an active window), the CPU or focus
billing engine, then every category target and every app override.

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 9. It is the
most cross-seam module in the family — every freezer verb, gate, window
helper and store call resolves through `budgets` at call time so the suite's
patch targets still reach it (see the cross-seam convention in
budgets_guards.py).
"""

from datetime import datetime


def _enforce_target(username, t, wall, deltas, summary, act):
    """Shared billing + freeze state machine for one target.

    ``t`` bundles: limit_sec, used, used_key, paused, paused_key, resumed,
    resumed_key, manual, manual_key, members, ensure, migrate,
    get_members, set_frozen, label, tag, warned_low, low_key, today_str,
    notify_low() -> bool, notify_paused(). The branches mirror the
    original per-category tick exactly, so with no app override or window
    configured the behavior is unchanged.
    """
    import budgets
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
    elif any(deltas.get(p['pid'], 0.0) >= budgets.ACTIVE_THRESHOLD_SECONDS
             for p in members):
        used[key] += wall
    exhausted = used[key] >= limit_sec
    remaining = limit_sec - used[key]
    if (not exhausted and 0 < remaining <= budgets.LOW_WARN_SECONDS
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
            budgets._audit_auto_pause_failed(username, t['tag'],
                                     'ensure-cgroup', msg)
            return
        for proc in members:
            t['migrate'](proc['pid'])
        expected = {p['pid']: p for p in members}
        current = t['get_members']()
        safe, reason = budgets.verify_freeze_safety(
            username, current, expected)
        if not safe:
            summary['note'] = f"freeze aborted for {t['label']}: {reason}"
            budgets._audit_auto_pause_failed(username, t['tag'],
                                     'verify-freeze-safety', reason)
            return
        if not members and not current:
            paused[pkey] = True  # exhausted, nothing running yet
            t['notify_paused']()
            return
        if not members:
            summary['note'] = (f"freeze aborted for {t['label']}: "
                               'cgroup has members but nothing is freezable')
            budgets._audit_auto_pause_failed(
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
            budgets._audit_auto_pause_failed(username, t['tag'],
                                             'freeze-write', msg)
    elif exhausted and paused.get(pkey) and members and act:
        # Exhausted earlier while nothing was running: a matching app
        # launched since. Migrate it in and (re-)freeze.
        ok, _msg = t['ensure']()
        if not ok:
            # FIX-3: was a silent discard (_msg was dropped entirely).
            summary['note'] = f"ensure cgroup (re-freeze): {_msg}"
            budgets._audit_auto_pause_failed(username, t['tag'],
                                     'ensure-cgroup-re-freeze', _msg)
        else:
            current = set(t['get_members']())
            for proc in members:
                if proc['pid'] not in current:
                    t['migrate'](proc['pid'])
            expected = {p['pid']: p for p in members}
            safe, reason = budgets.verify_freeze_safety(
                username, t['get_members'](), expected)
            if safe:
                ok, msg = t['set_frozen'](True)
                if ok:
                    summary['froze'].append(t['tag'] + ' (late start)')
                else:
                    summary['note'] = (
                        f"re-freeze failed for {t['label']}: {msg}")
                    budgets._audit_auto_pause_failed(username, t['tag'],
                                             're-freeze-write', msg)
            else:
                summary['note'] = (
                    f"re-freeze aborted for {t['label']}: {reason}")
                budgets._audit_auto_pause_failed(username, t['tag'],
                                         're-freeze-verify', reason)
    elif manual.get(t['manual_key']) and act:
        # Manually-frozen steady state: re-assert for late starters only.
        ok, _msg = t['ensure']()
        if not ok:
            summary['note'] = f"ensure cgroup (manual): {_msg}"
            budgets._audit_auto_pause_failed(username, t['tag'],
                                     'ensure-cgroup-manual', _msg)
        else:
            current = set(t['get_members']())
            for proc in members:
                if proc['pid'] not in current:
                    t['migrate'](proc['pid'])
            expected = {p['pid']: p for p in members}
            safe, reason = budgets.verify_freeze_safety(
                username, t['get_members'](), expected)
            if safe:
                ok, msg = t['set_frozen'](True)
                if ok:
                    summary['froze'].append(t['tag'] + ' (manual)')
                else:
                    summary['note'] = (
                        f"manual re-freeze failed for {t['label']}: {msg}")
                    budgets._audit_auto_pause_failed(username, t['tag'],
                                             'manual-re-freeze-write', msg)
            else:
                summary['note'] = (
                    f"manual re-freeze aborted for {t['label']}: {reason}")
                budgets._audit_auto_pause_failed(username, t['tag'],
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
    import budgets
    today_str = now.date().isoformat()
    has_bedtime = bool(bedtime_entry and bedtime_entry.get('start'))
    now_in = bool(bedtime_entry and budgets._in_bedtime(bedtime_entry, now))
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

    if budgets.is_admin_protected(username):
        # Admin self-protection: never auto-freeze a wheel member's apps.
        note = (f'skipped {username}: {budgets.ADMIN_GROUP} member '
                '(admin self-protection)')
        print(note, flush=True)
        summary['note'] = note
        return summary

    # ---- weekly-window lifecycle: open/close transitions ----
    window_used = entry.setdefault('window_used', {})
    window_active = entry.setdefault('window_active', {})
    configured_windows = [w for _wid, w in budgets.iter_windows(overrides)]
    active_windows = []
    for win in configured_windows:
        wid = win['id']
        was_active = bool(window_active.get(wid, False))
        is_active = bool(win['enabled'] and budgets._in_window(win, now))
        if is_active:
            active_windows.append(win)
        if is_active and not was_active:
            window_used[wid] = 0.0            # open: fresh allowance
            budgets._clear_window_target_freeze(
                entry, win, username, act, summary)
        elif was_active and not is_active:
            window_used[wid] = 0.0            # close: auto-resume
            budgets._clear_window_target_freeze(
                entry, win, username, act, summary)
        window_active[wid] = is_active

    if entry.get('day') is None:
        entry['day'] = today_str
    if budgets.budget_reset_needed(entry.get('day'), has_bedtime, was_in,
                                    now_in, today_str):
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
                ok, _res = budgets.unfreeze_all_user(
                    username, keep_categories=keep_cats,
                    keep_apps=keep_apps)
            else:
                ok, _res = budgets.unfreeze_all_user(username)
            summary['unfroze'].append('all' if ok else 'failed')
    entry['was_in_bedtime'] = now_in

    limits = entry.get('limits', {}) or {}
    app_overrides = budgets.override_map(overrides)
    if not limits and not configured_windows and not app_overrides:
        entry['last_cpu'] = {}
        return summary

    deltas, new_last = budgets.compute_deltas(entry.get('last_cpu', {}), procs)
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
    wall = max(0.0, min(wall, budgets.MAX_TICK_WALL_SECONDS))
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
    if budgets.focus_engine_of(entry) in budgets.FOCUS_FAMILY:
        if now_bt is None:
            now_bt = budgets.boottime_now()
        provider = focus_provider or budgets._default_focus_provider
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
        focus_seconds, focus_info = budgets._focus_billing(
            username, entry, procs, focus_limits, prefs, index, window_bt,
            provider, now_bt)
        if focus_info.get('billable'):
            entry['focus_engine'] = budgets.FOCUS_ENGINE_FOCUS
            entry['feed_status'] = 'ok'
        else:
            # Missing/stale/malformed feed: HALT billing (used_seconds is
            # left untouched — never silently billed as zero) and surface
            # ``no_data``. A fresh feed recovers on the next tick.
            entry['focus_engine'] = budgets.FOCUS_ENGINE_NO_DATA
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
        if category not in budgets.CATEGORIES:
            continue
        members = budgets._visible_category_members(
            procs, username, category, prefs, index,
            exclude_comms=governed_comms)
        label = budgets.CATEGORY_LABELS.get(category, category)
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
            'ensure': lambda c=category: budgets.ensure_cgroup(username, c),
            'migrate': lambda pid, c=category: budgets.migrate_pid(
                username, c, pid),
            'get_members': lambda c=category: budgets.cgroup_members(
                username, c),
            'set_frozen': lambda fr, c=category: budgets.set_frozen(
                username, c, fr),
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
        win = budgets.active_window(overrides, 'app', comm, now)
        members = budgets.freezable_app_members(procs, username, comm, prefs)
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
            'ensure': lambda c=comm: budgets.ensure_cgroup_app(username, c),
            'migrate': lambda pid, c=comm: budgets.migrate_pid_app(
                username, c, pid),
            'get_members': lambda c=comm: budgets.cgroup_members_app(
                username, c),
            'set_frozen': lambda fr, c=comm: budgets.set_frozen_app(
                username, c, fr),
            'label': comm, 'tag': 'app:' + comm,
            'warned_low': app_warned_low, 'low_key': comm,
            'today_str': today_str,
            'notify_low': _low_note(app_warned_low, comm, comm),
            'notify_paused': _paused_note(app_warned_paused, comm, comm),
        }, wall, deltas, summary, act)

    return summary

