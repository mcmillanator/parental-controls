"""Tick orchestration: summary emission, locked bookkeeping, the 60s tick.

`emit_tick_summaries`/`summary_line` are the one-line-per-user journal
surface (FIX-3, TIME_TRACKING_QA.md F8: an enforcement failure that repeats
every 60s must not be silent). `_clear_window_target_freeze` releases a
window target's freeze on an open/close transition, `tick_desktop_index`
gives the tick the same per-user .desktop index the UI uses (cached), and
`tick_budgets` runs the whole pass: snapshots, locked-account bookkeeping,
`account_and_enforce` per user, then the watchdog against the SAME in-memory
document (enforcement FIRST, watchdog SECOND — no flapping).

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 9; the facade
re-exports every name by name, and enforce_bedtime_timer.py needs no edit.
"""

import audit
import time


# ---------------------------------------------------------------------------
# Tick: accounting + reset + enforcement (called every 60s)
# ---------------------------------------------------------------------------

def _clear_window_target_freeze(entry, window, username, act, summary):
    """Clear a window target's freeze flags on an open/close transition.

    On open: fresh allowance. On close: auto-resume. Pops the auto-pause
    and resume-inhibit flags and (when acting) thaws the target's cgroup
    so the daily governor or the next window re-evaluates cleanly.
    """
    import budgets
    target = window['target']
    if window['target_type'] == 'category':
        entry.setdefault('paused', {}).pop(target, None)
        entry.setdefault('resumed', {}).pop(target, None)
        if act:
            ok, msg = budgets.set_frozen(username, target, False)
            if not ok:
                summary['note'] = f'window thaw failed for {target}: {msg}'
    else:
        entry.setdefault('app_paused', {}).pop(target, None)
        entry.setdefault('app_resumed', {}).pop(target, None)
        if act:
            ok, msg = budgets.set_frozen_app(username, target, False)
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


def _locked_tick_bookkeeping(entry, procs, now, bedtime_entry):
    """Advance a locked account's accounting baselines, billing nothing.

    Mirrors the baseline half of account_and_enforce (was_in_bedtime
    tracking, the compute_deltas last_cpu advance, and the last_tick
    wall-clock base) so the first tick after unlock bills only NEW CPU
    activity, never the locked window. No billing, no pause/notify/freeze
    decisions — already-paused/frozen state is unchanged and simply
    persists.
    """
    import budgets
    entry['was_in_bedtime'] = bool(
        bedtime_entry and budgets._in_bedtime(bedtime_entry, now))
    if not entry.get('limits'):
        # Mirrors account_and_enforce: no configured limits -> drop the
        # baseline (and leave last_tick alone, exactly like that path).
        entry['last_cpu'] = {}
        return
    entry['last_cpu'] = budgets.compute_deltas(
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
    import budgets
    now = time.monotonic()
    hit = budgets._DESKTOP_INDEX_CACHE.get(username)
    if hit is not None and now - hit[0] < budgets._DESKTOP_INDEX_TTL_SECONDS:
        return hit[1]
    try:
        index = budgets.build_desktop_index(username)
    except OSError:
        index = {}
    budgets._DESKTOP_INDEX_CACHE[username] = (now, index)
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
    import budgets
    data = budgets.load_budgets()
    overrides_map = budgets.load_overrides()
    if prefs is None:
        prefs = budgets.load_prefs()
    if now_bt is None:
        now_bt = budgets.boottime_now()
    changed = True  # usage baselines always advance; persist every tick
    summaries = []
    usernames = [u for u in data if budgets.valid_username(u)]
    # A user may have only an overrides config (app override / window)
    # with no category limits yet: give them a blank budget entry so the
    # tick governs them too.
    for u in overrides_map:
        if u not in data and budgets.valid_username(u):
            data[u] = budgets.blank_entry()
            usernames.append(u)
    for username in usernames:
        entry = data[username]
        user_overrides = overrides_map.get(username)
        if budgets.is_admin_protected(username):
            # Admin self-protection: not even a live `ps` snapshot is taken
            # for wheel members — skip entirely with a logged note.
            note = (f'skipped budget tick for {username}: '
                    f'{budgets.ADMIN_GROUP} member (admin self-protection)')
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
            procs = budgets.list_user_processes(username)
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
            if budgets.focus_engine_of(entry) in budgets.FOCUS_FAMILY:
                provider = focus_provider or budgets._default_focus_provider
                try:
                    parsed = provider(username, entry, now_bt)
                except Exception:
                    parsed = None
                budgets._locked_feed_bookkeeping(entry, parsed)
            summaries.append({'user': username, 'reset': False, 'froze': [],
                              'unfroze': [], 'notified': [], 'note':
                              f'skipped {username}: account locked'})
            continue
        summaries.append(budgets.account_and_enforce(
            username, entry, procs, (bedtime_map or {}).get(username),
            now, prefs,
            index if index is not None else tick_desktop_index(username),
            act, notify, overrides=user_overrides,
            focus_provider=focus_provider, now_bt=now_bt))
    if changed:
        try:
            budgets.save_budgets(data)
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
            w = budgets.watchdog_tick(
                data, cgroup_root=cgroup_root or budgets.CGROUP_ROOT)
        except Exception as e:
            print(f'WATCHDOG: pass failed: {e}', flush=True)
            w = {'thawed': [], 'legit': [], 'clean': [], 'unknown': [],
                 'skipped': [], 'note': f'pass failed: {e}'}
        summaries.append(dict({'user': '_watchdog'}, **w))
    return summaries
