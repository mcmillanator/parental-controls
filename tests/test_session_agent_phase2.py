#!/usr/bin/env python3
"""Phase 2: wire the focus feed into per-user budget accounting.

Covers the panel-side billing swap in ``budgets.py`` behind the
``focus_engine`` flag (SESSION_AGENT_PLAN.md §8/§9):

* focused seconds from a synthetic feed bill per category, clipped/capped;
* only freezable members bill ("only bill what you can freeze") -- a
  never-pause-only window bills nothing;
* locked ticks advance the feed cursor WITHOUT billing (mirror of the
  ``_locked_tick_bookkeeping`` CPU baseline);
* a missing/stale/malformed feed HALTS billing (used_seconds untouched)
  and surfaces ``no_data``/degraded -- never silently zero;
* ``focus_engine='cpu'`` (default) is byte-identical to today and the
  provider is never consulted; ``last_cpu`` keeps advancing under the
  focus engine so switching back is lossless.

Everything here is mocked: fake users, fake procs, a fake feed provider,
a temp store. No live account, no freeze/lock/notify, no sudo, no service
restart, no install.sh.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import budgets
import focus_feed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = os.path.join(ROOT, '.scratch')
os.makedirs(SCRATCH, exist_ok=True)

BOOT = 'boot-0123456789abcdef'
NOW = datetime(2026, 10, 12, 8, 0)          # wall clock for "now"
LAST_TICK = '2026-10-12T07:59:00'           # -> wall = 60 s
NOW_BT = 1000000.0                          # -> window = (999940, 1000000)
WIN_START = NOW_BT - 60.0


# ---------------------------------------------------------------------------
# Fixtures (mirroring the Phase-1 test helpers)
# ---------------------------------------------------------------------------

def iso(bt):
    base = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=float(bt))).isoformat()


def make_row(seq, bt, rtype='focus', boot=BOOT, v=1, **fields):
    row = {'v': v, 'seq': seq, 'agent': '0.1.0', 'boot_id': boot,
           'ts': iso(bt), 'bt': float(bt), 'type': rtype}
    if rtype == 'focus':
        row.setdefault('session', seq)
        row.setdefault('wm_class', 'firefox')
        row.setdefault('kind', 'app')
        row.setdefault('since', 0.0)
    elif rtype == 'state':
        row.setdefault('state', 'active')
    else:
        row.setdefault('code', 'x')
    row.update(fields)
    return row


def feed(*rows):
    import json
    return ''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in rows)


def firefox_proc(pid=100, cpu=100.0):
    return {'pid': pid, 'comm': 'firefox',
            'cmdline': '/usr/lib64/firefox/firefox',
            'args': '', 'etime': '01:00:00', 'cpu_seconds': cpu}


def vlc_proc(pid=200, cpu=100.0):
    return {'pid': pid, 'comm': 'vlc', 'cmdline': '/usr/bin/vlc',
            'args': '', 'etime': '01:00:00', 'cpu_seconds': cpu}


def bash_proc(pid=300, cpu=100.0):
    return {'pid': pid, 'comm': 'bash', 'cmdline': 'bash',
            'args': '', 'etime': '01:00:00', 'cpu_seconds': cpu}


FOCUS = budgets.FOCUS_ENGINE_FOCUS
FOCUS_CPU = budgets.FOCUS_ENGINE_CPU
NO_DATA = budgets.FOCUS_ENGINE_NO_DATA


def entry(limits=None, used=None, focus_engine=FOCUS_CPU, feed_seq=0,
          last_cpu=None, last_tick=LAST_TICK, day='2026-10-12', **kw):
    e = budgets.blank_entry()
    e['limits'] = limits if limits is not None else {}
    e['day'] = day
    e['used_seconds'] = used or {}
    e['last_tick'] = last_tick
    e['last_cpu'] = last_cpu or {}
    e['focus_engine'] = focus_engine
    e['feed_seq'] = feed_seq
    for key, value in kw.items():
        e[key] = value
    return e


def make_provider(body_or_map):
    """Callable feed provider returning a focus_feed parse result.

    ``body_or_map`` is a raw body string or {username: body}. Records each
    call as (username, cursor, now_bt) so tests can assert the CPU path
    never consults it.
    """
    calls = []

    def provider(username, entry_, now_bt):
        calls.append((username, entry_.get('feed_seq'), now_bt))
        body = (body_or_map.get(username)
                if isinstance(body_or_map, dict) else body_or_map)
        return focus_feed.parse_feed(body, feed_seq=entry_.get('feed_seq', 0),
                                     now_bt=now_bt)

    provider.calls = calls
    return provider


def run_tick(data, now=NOW, focus_provider=None, procs_by_user=None,
             account_locked=None, now_bt=NOW_BT, overrides=None,
             prefs=None, index=None, notify=None):
    """Run tick_budgets against a temp store; return (saved_data, summaries)."""
    procs_by_user = procs_by_user or {}
    with tempfile.TemporaryDirectory(dir=SCRATCH) as d:
        budgets_file = os.path.join(d, 'budgets.json')
        overrides_file = os.path.join(d, 'overrides.json')
        with mock.patch.object(budgets, 'BUDGETS_FILE', budgets_file), \
                mock.patch.object(budgets, 'OVERRIDES_FILE', overrides_file):
            budgets.save_budgets(data)
            if overrides is not None:
                budgets.save_overrides(overrides)
            summaries = budgets.tick_budgets(
                {}, now,
                procs_provider=lambda u: procs_by_user.get(u, []),
                prefs=prefs if prefs is not None else {},
                index=index if index is not None else {},
                act=False, notify=notify, account_locked=account_locked,
                focus_provider=focus_provider, now_bt=now_bt,
                run_watchdog=False)
            saved = budgets.load_budgets()
    return saved, summaries


def kid_summary(summaries):
    return next(s for s in summaries if s.get('user') == 'kid')


def full_tick_feed(seq=1, wm='firefox', app='firefox.desktop', kind='app',
                   since=0.0, start=WIN_START, end=NOW_BT):
    """One focused session spanning the whole tick + a heartbeat."""
    return feed(
        make_row(seq, start, wm_class=wm, app_id=app, kind=kind, since=since),
        make_row(seq + 1, end, rtype='state', state='active'))


# ===========================================================================
# Focus billing from a synthetic feed
# ===========================================================================

class TestFocusBilling(unittest.TestCase):
    def test_full_tick_bills_focused_seconds(self):
        saved, summaries = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc()]})
        self.assertAlmostEqual(saved['kid']['used_seconds']['browsers'],
                               60.0, places=3)
        self.assertTrue(kid_summary(summaries)['focus']['billable'])
        self.assertEqual(saved['kid']['feed_seq'], 2)

    def test_partial_tick_is_clipped(self):
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(
                full_tick_feed(start=WIN_START + 30)),
            procs_by_user={'kid': [firefox_proc()]})
        self.assertAlmostEqual(saved['kid']['used_seconds']['browsers'],
                               30.0, places=3)

    def test_since_bounds_the_billed_interval(self):
        body = feed(
            make_row(1, WIN_START, wm_class='firefox',
                     app_id='firefox.desktop', since=0.0),
            make_row(2, WIN_START + 30, wm_class='vlc', app_id='vlc.desktop',
                     since=30.0),
            make_row(3, NOW_BT, rtype='state', state='active'))
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(body),
            procs_by_user={'kid': [firefox_proc(), vlc_proc()]})
        self.assertAlmostEqual(saved['kid']['used_seconds']['browsers'],
                               30.0, places=3)

    def test_consecutive_same_window_merges(self):
        body = feed(
            make_row(1, WIN_START, wm_class='firefox',
                     app_id='firefox.desktop', since=0.0),
            make_row(2, WIN_START + 30, wm_class='firefox',
                     app_id='firefox.desktop', since=30.0),
            make_row(3, NOW_BT, rtype='state', state='active'))
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(body),
            procs_by_user={'kid': [firefox_proc()]})
        self.assertAlmostEqual(saved['kid']['used_seconds']['browsers'],
                               60.0, places=3)

    def test_two_categories_billed(self):
        body = feed(
            make_row(1, WIN_START, wm_class='firefox',
                     app_id='firefox.desktop', since=0.0),
            make_row(2, WIN_START + 30, wm_class='vlc', app_id='vlc.desktop',
                     since=30.0),
            make_row(3, NOW_BT, rtype='state', state='active'))
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60, 'media': 60},
                          focus_engine=FOCUS)},
            focus_provider=make_provider(body),
            procs_by_user={'kid': [firefox_proc(), vlc_proc()]})
        self.assertAlmostEqual(saved['kid']['used_seconds']['browsers'],
                               30.0, places=3)
        self.assertAlmostEqual(saved['kid']['used_seconds']['media'],
                               30.0, places=3)

    def test_narrower_tick_window_clips_session(self):
        # A shorter wall span (last tick 20 s ago) shrinks the billing window.
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS,
                          last_tick='2026-10-12T07:59:40')},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc()]})
        self.assertAlmostEqual(saved['kid']['used_seconds']['browsers'],
                               20.0, places=3)

    def test_heartbeat_only_feed_bills_zero_not_degraded(self):
        body = feed(make_row(1, NOW_BT, rtype='state', state='active'))
        saved, summaries = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(body),
            procs_by_user={'kid': [firefox_proc()]})
        self.assertEqual(saved['kid']['used_seconds'].get('browsers', 0.0), 0.0)
        info = kid_summary(summaries)['focus']
        self.assertTrue(info['billable'])
        self.assertFalse(info['degraded'])
        self.assertNotIn('degraded', kid_summary(summaries))


# ===========================================================================
# "Only bill what you can freeze"
# ===========================================================================

class TestFreezableGateIntegration(unittest.TestCase):
    def test_never_pause_only_window_bills_nothing(self):
        saved, _ = run_tick(
            {'kid': entry(limits={'other': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(
                full_tick_feed(wm='bash', app=None)),
            procs_by_user={'kid': [bash_proc()]})
        self.assertEqual(saved['kid']['used_seconds'].get('other', 0.0), 0.0)

    def test_focused_window_without_matching_proc_bills_nothing(self):
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [vlc_proc()]})  # firefox window, no firefox
        self.assertEqual(saved['kid']['used_seconds'].get('browsers', 0.0), 0.0)

    def test_category_without_limit_or_window_bills_nothing(self):
        saved, _ = run_tick(
            {'kid': entry(limits={'media': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc()]})
        self.assertEqual(saved['kid']['used_seconds'].get('browsers', 0.0), 0.0)
        self.assertEqual(saved['kid']['used_seconds'].get('media', 0.0), 0.0)

    def test_category_window_focus_billing_uses_window_used(self):
        # No daily limit: only an active category window provides the
        # allowance, yet the freezable gate must still bill the window.
        overrides = {'kid': {'windows': {
            'w-category-browsers': {
                'target_type': 'category', 'target': 'browsers',
                'days': [NOW.weekday()], 'start': '00:00', 'end': '23:59',
                'allowance_minutes': 30, 'enabled': True}}}}
        saved, _ = run_tick(
            {'kid': entry(focus_engine=FOCUS)},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc()]},
            overrides=overrides)
        self.assertAlmostEqual(
            saved['kid']['window_used']['w-category-browsers'], 60.0,
            places=3)


# ===========================================================================
# Locked account: advance the cursor, bill nothing
# ===========================================================================

class TestLockedFocusBookkeeping(unittest.TestCase):
    def test_locked_tick_advances_cursor_and_bills_nothing(self):
        saved, summaries = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc()]},
            account_locked=lambda u: True)
        kid = saved['kid']
        self.assertEqual(kid['used_seconds'].get('browsers', 0.0), 0.0)
        self.assertEqual(kid['feed_seq'], 2)
        s = kid_summary(summaries)
        self.assertEqual(s['notified'], [])
        self.assertIn('locked', s['note'])

    def test_unlock_then_tick_bills_only_new_focus(self):
        # Tick 1: locked -- the locked window's rows are consumed.
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(full_tick_feed(seq=1)),
            procs_by_user={'kid': [firefox_proc()]},
            account_locked=lambda u: True)
        self.assertEqual(saved['kid']['feed_seq'], 2)
        self.assertEqual(saved['kid']['used_seconds'].get('browsers', 0.0), 0.0)
        # Tick 2: unlocked, new rows only (cursor at 2) -- one fresh session.
        body = feed(
            make_row(3, NOW_BT, wm_class='firefox',
                     app_id='firefox.desktop', since=0.0),
            make_row(4, NOW_BT + 60, rtype='state', state='active'))
        saved2, _ = run_tick(
            {'kid': saved['kid']},
            now=NOW + timedelta(seconds=60), now_bt=NOW_BT + 60,
            focus_provider=make_provider(body),
            procs_by_user={'kid': [firefox_proc()]},
            account_locked=lambda u: False)
        self.assertAlmostEqual(saved2['kid']['used_seconds']['browsers'],
                               60.0, places=3)
        self.assertEqual(saved2['kid']['feed_seq'], 4)

    def test_locked_tick_does_not_bill_cpu_either(self):
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS,
                          last_cpu={'100': {'comm': 'firefox', 'cpu': 100.0}})},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc(cpu=200.0)]},
            account_locked=lambda u: True)
        self.assertEqual(saved['kid']['used_seconds'].get('browsers', 0.0), 0.0)
        # Baseline still advanced (rollback safety).
        self.assertEqual(saved['kid']['last_cpu']['100']['cpu'], 200.0)


# ===========================================================================
# Degraded / no_data: halt billing, never zero
# ===========================================================================

class TestDegradedNoData(unittest.TestCase):
    def test_missing_feed_halts_billing_and_marks_no_data(self):
        provider = lambda u, e, bt: focus_feed.parse_feed(
            None, feed_seq=e.get('feed_seq', 1))
        saved, summaries = run_tick(
            {'kid': entry(limits={'browsers': 60}, used={'browsers': 120.0},
                          focus_engine=FOCUS)},
            focus_provider=provider,
            procs_by_user={'kid': [firefox_proc()]})
        kid = saved['kid']
        # Halted: used_seconds is untouched, NOT zeroed/advanced.
        self.assertEqual(kid['used_seconds']['browsers'], 120.0)
        self.assertEqual(kid['focus_engine'], NO_DATA)
        self.assertEqual(kid['feed_status'], 'no_data')
        s = kid_summary(summaries)
        self.assertTrue(s['degraded'])
        self.assertFalse(s['focus']['billable'])

    def test_stale_feed_halts_billing(self):
        body = full_tick_feed()
        provider = make_provider(body)
        saved, summaries = run_tick(
            {'kid': entry(limits={'browsers': 60}, used={'browsers': 10.0},
                          focus_engine=FOCUS)},
            focus_provider=provider,
            procs_by_user={'kid': [firefox_proc()]},
            now_bt=NOW_BT + int(focus_feed.FEED_STALE_SECONDS) + 10)
        self.assertEqual(saved['kid']['used_seconds']['browsers'], 10.0)
        self.assertEqual(saved['kid']['focus_engine'], NO_DATA)
        self.assertTrue(kid_summary(summaries)['degraded'])

    def test_malformed_feed_fails_closed(self):
        saved, summaries = run_tick(
            {'kid': entry(limits={'browsers': 60}, used={'browsers': 5.0},
                          focus_engine=FOCUS)},
            focus_provider=make_provider('this is not json\n'),
            procs_by_user={'kid': [firefox_proc()]})
        self.assertEqual(saved['kid']['used_seconds']['browsers'], 5.0)
        self.assertEqual(saved['kid']['focus_engine'], NO_DATA)
        self.assertTrue(kid_summary(summaries)['degraded'])

    def test_oversized_feed_fails_closed(self):
        import json
        line = json.dumps(make_row(1, WIN_START), separators=(',', ':'))
        body = (line + '\n') * (focus_feed.MAX_FEED_BYTES // len(line) + 5)
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, used={'browsers': 7.0},
                          focus_engine=FOCUS)},
            focus_provider=make_provider(body),
            procs_by_user={'kid': [firefox_proc()]})
        self.assertEqual(saved['kid']['used_seconds']['browsers'], 7.0)
        self.assertEqual(saved['kid']['focus_engine'], NO_DATA)

    def test_non_monotonic_rows_fail_closed(self):
        # A provider handing raw non-monotonic rows makes aggregate_focus
        # raise FeedError; the tick must halt, not crash.
        bad = {'rows': [make_row(2, WIN_START), make_row(1, NOW_BT)],
               'last_seq': 2, 'last_heartbeat_bt': None, 'states': []}
        provider = lambda u, e, bt: bad
        saved, summaries = run_tick(
            {'kid': entry(limits={'browsers': 60}, used={'browsers': 3.0},
                          focus_engine=FOCUS)},
            focus_provider=provider,
            procs_by_user={'kid': [firefox_proc()]})
        self.assertEqual(saved['kid']['used_seconds']['browsers'], 3.0)
        self.assertEqual(saved['kid']['focus_engine'], NO_DATA)
        self.assertTrue(kid_summary(summaries)['degraded'])

    def test_provider_exception_fails_closed(self):
        def boom(u, e, bt):
            raise RuntimeError('transport exploded')

        saved, summaries = run_tick(
            {'kid': entry(limits={'browsers': 60}, used={'browsers': 8.0},
                          focus_engine=FOCUS)},
            focus_provider=boom,
            procs_by_user={'kid': [firefox_proc()]})
        self.assertEqual(saved['kid']['used_seconds']['browsers'], 8.0)
        self.assertEqual(saved['kid']['focus_engine'], NO_DATA)
        self.assertTrue(kid_summary(summaries)['degraded'])

    def test_degraded_then_fresh_recovers_to_focus(self):
        # Tick 1: stale -> no_data, billing halted.
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, used={'browsers': 40.0},
                          focus_engine=FOCUS)},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc()]},
            now_bt=NOW_BT + int(focus_feed.FEED_STALE_SECONDS) + 10)
        self.assertEqual(saved['kid']['focus_engine'], NO_DATA)
        # Tick 2: fresh feed at the new cursor recovers and bills.
        body = feed(
            make_row(3, NOW_BT, wm_class='firefox',
                     app_id='firefox.desktop', since=0.0),
            make_row(4, NOW_BT + 60, rtype='state', state='active'))
        saved2, summaries2 = run_tick(
            {'kid': saved['kid']},
            now=NOW + timedelta(seconds=60), now_bt=NOW_BT + 60,
            focus_provider=make_provider(body),
            procs_by_user={'kid': [firefox_proc()]})
        self.assertEqual(saved2['kid']['focus_engine'], FOCUS)
        self.assertEqual(saved2['kid']['feed_status'], 'ok')
        self.assertAlmostEqual(saved2['kid']['used_seconds']['browsers'],
                               100.0, places=3)
        self.assertFalse(kid_summary(summaries2).get('degraded', False))

    def test_no_data_engine_still_probes_and_recovers(self):
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=NO_DATA)},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc()]})
        self.assertEqual(saved['kid']['focus_engine'], FOCUS)
        self.assertAlmostEqual(saved['kid']['used_seconds']['browsers'],
                               60.0, places=3)


# ===========================================================================
# CPU engine regression (default, byte-identical)
# ===========================================================================

class TestCpuEngineRegression(unittest.TestCase):
    def test_default_engine_is_cpu(self):
        self.assertEqual(budgets.blank_entry()['focus_engine'], FOCUS_CPU)
        self.assertEqual(budgets.focus_engine_of({}), FOCUS_CPU)

    def test_unknown_engine_fails_closed_to_cpu(self):
        self.assertEqual(budgets.focus_engine_of({'focus_engine': 'wat'}),
                         FOCUS_CPU)

    def test_focus_alias_agent(self):
        self.assertEqual(budgets.focus_engine_of({'focus_engine': 'agent'}),
                         FOCUS)

    def test_cpu_engine_never_consults_provider(self):
        provider = make_provider(full_tick_feed())
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60},
                          last_cpu={'100': {'comm': 'firefox', 'cpu': 100.0}})},
            focus_provider=provider,
            procs_by_user={'kid': [firefox_proc(cpu=130.0)]})
        self.assertEqual(provider.calls, [])
        # CPU path: active delta bills the whole 60 s wall tick.
        self.assertAlmostEqual(saved['kid']['used_seconds']['browsers'],
                               60.0, places=3)

    def test_cpu_entry_byte_identical_with_and_without_provider(self):
        def fresh():
            return entry(
                limits={'browsers': 60},
                last_cpu={'100': {'comm': 'firefox', 'cpu': 100.0}})

        procs = [firefox_proc(cpu=130.0)]
        saved_none, s_none = run_tick(
            {'kid': fresh()}, focus_provider=None,
            procs_by_user={'kid': procs})
        saved_fake, s_fake = run_tick(
            {'kid': fresh()}, focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': procs})
        self.assertEqual(saved_none['kid'], saved_fake['kid'])
        self.assertEqual(
            [s for s in s_none if s.get('user') != '_watchdog'],
            [s for s in s_fake if s.get('user') != '_watchdog'])
        self.assertAlmostEqual(saved_none['kid']['used_seconds']['browsers'],
                               60.0, places=3)


# ===========================================================================
# Baseline safety across engine switches
# ===========================================================================

class TestEngineSwitchBaselineSafety(unittest.TestCase):
    def test_focus_tick_advances_last_cpu(self):
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS,
                          last_cpu={'100': {'comm': 'firefox', 'cpu': 100.0}})},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc(cpu=145.0)]})
        # last_cpu kept advancing even though the CPU gate did not bill.
        self.assertEqual(saved['kid']['last_cpu']['100']['cpu'], 145.0)

    def test_switch_focus_to_cpu_does_not_double_bill(self):
        # Tick 1: focus bills 60 s of focused time.
        saved, _ = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS,
                          last_cpu={'100': {'comm': 'firefox', 'cpu': 100.0}})},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc(cpu=130.0)]})
        self.assertAlmostEqual(saved['kid']['used_seconds']['browsers'],
                               60.0, places=3)
        # Tick 2: the parent flips focus_engine back to cpu. Baseline was
        # maintained, so only the NEW delta bills -- one more 60 s wall tick,
        # no phantom catch-up.
        rolled = saved['kid']
        rolled['focus_engine'] = FOCUS_CPU
        saved2, _ = run_tick(
            {'kid': rolled}, now=NOW + timedelta(seconds=60),
            procs_by_user={'kid': [firefox_proc(cpu=160.0)]})
        self.assertAlmostEqual(saved2['kid']['used_seconds']['browsers'],
                               120.0, places=3)

    def test_focus_summary_is_surfaced(self):
        saved, summaries = run_tick(
            {'kid': entry(limits={'browsers': 60}, focus_engine=FOCUS)},
            focus_provider=make_provider(full_tick_feed()),
            procs_by_user={'kid': [firefox_proc()]})
        info = kid_summary(summaries)['focus']
        self.assertEqual(info['engine'], FOCUS)
        self.assertTrue(info['billable'])
        self.assertAlmostEqual(info['seconds']['browsers'], 60.0, places=3)
        self.assertEqual(info['feed_seq'], 2)

    def test_focus_status_helper_reports_degraded(self):
        healthy = budgets.focus_status(entry(focus_engine=FOCUS))
        self.assertTrue(healthy['configured'])
        self.assertFalse(healthy['degraded'])
        broken = budgets.focus_status(
            entry(focus_engine=NO_DATA, feed_status='no_data'))
        self.assertTrue(broken['configured'])
        self.assertTrue(broken['degraded'])
        cpu = budgets.focus_status(entry())
        self.assertFalse(cpu['configured'])


if __name__ == '__main__':
    unittest.main()
