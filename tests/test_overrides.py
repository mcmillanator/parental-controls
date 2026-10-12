#!/usr/bin/env python3
"""Per-app overrides + weekly budget windows (OVERRIDES_PLAN.md).

Covers: precedence/lifecycle unit logic in budgets.py, endpoint auth +
validation, live-payload shape, template smoke, SQLite v2->v3 migration +
mirror round-trip, and watchdog app-group awareness.

Stdlib only. All cgroup/subprocess work is mocked; every cgroup tree lives
in a temp dir. No real account (tester/bluefox/stan/pcagent), no sudo, no
service restart, no /etc, /opt, or system tree is touched.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import budgets
import store
from test_lock_regression import (_AdminSessionMixin, _BedtimeFileMixin, KID)

T0 = datetime(2026, 10, 7, 15, 0)          # a Wednesday, no bedtime
ACTIVE = '2026-10-07'
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


def _tick(entry, overrides, t0=T0, procs=None, act=False, summary_ret=None,
          **kw):
    procs = procs or []
    return budgets.account_and_enforce(
        KID, entry, procs, None, t0, prefs={}, index={}, act=act,
        overrides=overrides, **kw)


def _steam(pid=9101, cpu=100.0):
    return {'pid': pid, 'comm': 'steam',
            'cmdline': '/home/kid/.local/share/Steam/ubuntu12_32/steam',
            'args': '', 'etime': '01:00:00', 'cpu_seconds': cpu}


def _reaper(pid=9102, cpu=100.0):
    return {'pid': pid, 'comm': 'reaper',
            'cmdline': '/home/kid/.local/share/Steam/ubuntu12_32/reaper '
                       'SteamLaunch AppId=1',
            'args': '', 'etime': '01:00:00', 'cpu_seconds': cpu}


def _active_pair(procs):
    return [dict(p, cpu_seconds=p['cpu_seconds'] + 30.0) for p in procs]


def _category_window(target='games', allowance=60, days=None, start='00:00',
                     end='23:59', enabled=True):
    return {'target_type': 'category', 'target': target,
            'days': list(ALL_DAYS if days is None else days),
            'start': start, 'end': end,
            'allowance_minutes': allowance, 'enabled': enabled}


class _OverridesFileMixin:
    """Temp budgets + overrides stores for the duration of a test."""

    def setUp(self):
        super().setUp()
        self._ov_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._ov_tmp.cleanup)
        home = self._ov_tmp.name
        self._overrides_path = os.path.join(home, 'overrides.json')
        self._budgets_path = os.path.join(home, 'budgets.json')
        p1 = mock.patch.object(budgets, 'OVERRIDES_FILE', self._overrides_path)
        p2 = mock.patch.object(budgets, 'BUDGETS_FILE', self._budgets_path)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)


# ---------------------------------------------------------------------------
# Unit — precedence & lifecycle (budgets.py)
# ---------------------------------------------------------------------------

class TestOverridePrecedence(unittest.TestCase):
    def test_precedence_exempt_over_app_allowance(self):
        """#1: an exempt comm never bills, even as a category's active member."""
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = ACTIVE
        ov = {'app_overrides': {'steam': {'mode': 'exempt'}}, 'windows': {}}
        _tick(entry, ov, procs=[_steam()])
        _tick(entry, ov, t0=T0 + timedelta(seconds=60),
              procs=_active_pair([_steam()]))
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)
        self.assertEqual(entry.get('app_used', {}), {})
        self.assertFalse(entry['paused'].get('games'))

    def test_precedence_app_allowance_over_category_window(self):
        """#2: an app daily allowance bills its own counter, not window_used."""
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = ACTIVE
        ov = {'app_overrides': {'steam': {'mode': 'limit', 'minutes': 10}},
              'windows': {'w-category-games': _category_window(allowance=120)}}
        _tick(entry, ov, procs=[_steam()])
        _tick(entry, ov, t0=T0 + timedelta(seconds=60),
              procs=_active_pair([_steam()]))
        self.assertAlmostEqual(entry['app_used']['steam'], 60.0)
        self.assertEqual(entry['window_used'].get('w-category-games', 0.0), 0.0)
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)

    def test_precedence_category_window_over_daily_limit(self):
        """#3: an active category window governs; used_seconds stays untouched."""
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = ACTIVE
        ov = {'app_overrides': {},
              'windows': {'w-category-games': _category_window(allowance=5)}}
        _tick(entry, ov, procs=[_steam(), _reaper()])
        _tick(entry, ov, t0=T0 + timedelta(seconds=60),
              procs=_active_pair([_steam(), _reaper()]))
        self.assertAlmostEqual(entry['window_used']['w-category-games'], 60.0)
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)

    def test_precedence_daily_limit_when_no_window(self):
        """#4: no override/window -> unchanged daily billing (regression)."""
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = ACTIVE
        _tick(entry, None, procs=[_steam(), _reaper()])
        _tick(entry, None, t0=T0 + timedelta(seconds=60),
              procs=_active_pair([_steam(), _reaper()]))
        self.assertAlmostEqual(entry['used_seconds']['games'], 60.0)


class TestWindowLifecycle(unittest.TestCase):
    def test_window_open_resets_and_clean(self):
        entry = budgets.blank_entry()
        entry['window_active'] = {'w-category-games': False}
        entry['window_used'] = {'w-category-games': 999.0}
        entry['paused'] = {'games': True}
        ov = {'app_overrides': {},
              'windows': {'w-category-games': _category_window()}}
        _tick(entry, ov, procs=[])
        self.assertEqual(entry['window_used']['w-category-games'], 0.0)
        self.assertNotIn('games', entry['paused'])
        self.assertTrue(entry['window_active']['w-category-games'])

    def test_window_close_auto_resumes_and_thaws(self):
        # A Monday window, viewed on a Wednesday: it just closed.
        entry = budgets.blank_entry()
        entry['window_active'] = {'w-category-games': True}
        entry['window_used'] = {'w-category-games': 999.0}
        entry['paused'] = {'games': True}
        ov = {'app_overrides': {},
              'windows': {'w-category-games': _category_window(days=[0])}}
        with mock.patch.object(budgets, 'set_frozen',
                               return_value=(True, '')) as frozen:
            _tick(entry, ov, procs=[], act=True)
        frozen.assert_any_call(KID, 'games', False)
        self.assertEqual(entry['window_used']['w-category-games'], 0.0)
        self.assertFalse(entry['window_active']['w-category-games'])

    def test_window_spans_midnight_accumulates(self):
        """Sat-night window (Sunday 22:00-06:00) bills across the day reset."""
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = '2026-10-11'                      # Sunday
        ov = {'app_overrides': {},
              'windows': {'w-category-games': _category_window(
                  days=[6], start='22:00', end='06:00', allowance=120)}}
        sun = datetime(2026, 10, 11, 23, 0)
        _tick(entry, ov, t0=sun, procs=[_steam()])
        _tick(entry, ov, t0=sun + timedelta(minutes=1),
              procs=_active_pair([_steam()]))
        self.assertAlmostEqual(entry['window_used']['w-category-games'], 60.0)
        # Cross midnight into Monday: the daily reset fires but the active
        # window's counter survives and keeps accumulating.
        mon = datetime(2026, 10, 12, 0, 30)
        _tick(entry, ov, t0=mon, procs=_active_pair([_steam()]))
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)
        self.assertTrue(entry['window_active']['w-category-games'])
        self.assertGreaterEqual(entry['window_used']['w-category-games'], 60.0)

    def test_daily_reset_skips_active_window(self):
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = '2026-10-11'
        ov = {'app_overrides': {},
              'windows': {'w-category-games': _category_window(
                  days=[6], start='22:00', end='06:00', allowance=120)}}
        mon = datetime(2026, 10, 12, 0, 30)
        with mock.patch.object(budgets, 'unfreeze_all_user',
                               return_value=(True, [])) as unfrz:
            _tick(entry, ov, t0=mon, procs=[], act=True)
        self.assertTrue(unfrz.called)
        _args, kwargs = unfrz.call_args
        self.assertIn('games', kwargs.get('keep_categories', set()))


class TestAppCgroups(unittest.TestCase):
    def test_app_allowance_exhaust_freezes_app_group_only(self):
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = ACTIVE
        ov = {'app_overrides': {'steam': {'mode': 'limit', 'minutes': 0.05}},
              'windows': {}}
        procs = [_steam(), _reaper()]
        _tick(entry, ov, procs=procs)
        summary = _tick(entry, ov, t0=T0 + timedelta(seconds=60),
                        procs=_active_pair(procs))
        self.assertTrue(entry['app_paused'].get('steam'))
        self.assertFalse(entry['paused'].get('games'))
        self.assertIn('app:steam (dry-run)', summary['froze'])

    def test_app_exempt_never_placed_in_category_cgroup(self):
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 0.05}          # exhausts fast
        entry['day'] = ACTIVE
        ov = {'app_overrides': {'steam': {'mode': 'exempt'}}, 'windows': {}}
        procs = [_steam(), _reaper()]
        migrated = []

        def fake_migrate(user, cat, pid):
            migrated.append(pid)
            return True, ''

        with mock.patch.object(budgets, 'ensure_cgroup',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'migrate_pid',
                               side_effect=fake_migrate), \
             mock.patch.object(budgets, 'cgroup_members',
                               return_value=[_reaper()['pid']]), \
             mock.patch.object(budgets, 'set_frozen',
                               return_value=(True, '')):
            _tick(entry, ov, procs=procs, act=True)
            _tick(entry, ov, t0=T0 + timedelta(seconds=60),
                  procs=_active_pair(procs), act=True)
        self.assertIn(_reaper()['pid'], migrated)
        self.assertNotIn(_steam()['pid'], migrated)

    def test_override_corrupt_fails_closed(self):
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = ACTIVE
        ov = {'app_overrides': {
                  'steam': {'mode': 'bogus'},
                  'vlc': {'mode': 'limit'},          # missing minutes
                  'mpv': {'mode': 'limit', 'minutes': -5}},
              'windows': {}}
        procs = [_steam()]
        _tick(entry, ov, procs=procs)
        _tick(entry, ov, t0=T0 + timedelta(seconds=60),
              procs=_active_pair(procs))
        # Garbage overrides are absent -> the category governs.
        self.assertAlmostEqual(entry['used_seconds']['games'], 60.0)
        self.assertEqual(entry.get('app_used', {}), {})
        self.assertEqual(budgets.override_map(ov), {})

    def test_regression_no_overrides_no_behavior_change(self):
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = ACTIVE
        summary = _tick(entry, None, procs=[_steam()])
        self.assertFalse(summary['reset'])
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)


class TestValidationHelpers(unittest.TestCase):
    def test_valid_comm_gate(self):
        for good in ('steam', 'firefox', 'a', 'a.b', 'x_y', 'A-1', 'fd'):
            self.assertTrue(budgets.valid_comm(good), good)
        for bad in ('', '-x', '.', '..', 'a/b', 'a b', 'a;b', 'a$b',
                    'x' * 16, None, 'a\nb'):
            self.assertFalse(budgets.valid_comm(bad), repr(bad))

    def test_window_invalid_fields_fail_closed(self):
        bad_windows = [
            _category_window(days=[]),                       # empty days
            _category_window(days=[7]),                      # out of range
            _category_window(start='10:00', end='10:00'),    # start == end
            _category_window(start='bogus'),                 # unparseable
            _category_window(allowance=1),                   # below floor
            dict(_category_window(), target_type='nope'),
            dict(_category_window(), target='not-a-category'),
        ]
        for win in bad_windows:
            self.assertIsNone(
                budgets.normalize_window(win), win)
        # A tick with only invalid windows matches no-window behavior.
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = ACTIVE
        ov = {'app_overrides': {}, 'windows': {'bad': bad_windows[0]}}
        _tick(entry, ov, procs=[_steam()])
        _tick(entry, ov, t0=T0 + timedelta(seconds=60),
              procs=_active_pair([_steam()]))
        self.assertAlmostEqual(entry['used_seconds']['games'], 60.0)
        self.assertEqual(entry.get('window_active', {}), {})


class TestSetClearHelpers(_OverridesFileMixin, unittest.TestCase):
    def test_set_clear_override_roundtrip(self):
        ok, _ = budgets.set_app_override(KID, 'steam', 'limit', 30,
                                         actor='qaadmin')
        self.assertTrue(ok)
        ov = budgets.load_overrides()[KID]['app_overrides']['steam']
        self.assertEqual(ov['mode'], 'limit')
        self.assertEqual(ov['minutes'], 30)
        self.assertEqual(ov['set_by'], 'qaadmin')
        with mock.patch.object(budgets, 'set_frozen_app',
                               return_value=(True, '')):
            ok, _ = budgets.set_app_override(KID, 'steam', 'none',
                                             actor='qaadmin')
        self.assertTrue(ok)
        self.assertEqual(
            budgets.load_overrides()[KID]['app_overrides'], {})

    def test_negative_minutes_rejected(self):
        ok, msg = budgets.set_app_override(KID, 'steam', 'limit', -5)
        self.assertFalse(ok)

    def test_exempt_override(self):
        ok, _ = budgets.set_app_override(KID, 'firefox', 'exempt')
        self.assertTrue(ok)
        self.assertEqual(
            budgets.load_overrides()[KID]['app_overrides']['firefox']['mode'],
            'exempt')


# ---------------------------------------------------------------------------
# Endpoint — auth/validation (app.py test client)
# ---------------------------------------------------------------------------

class _EndpointBase(_AdminSessionMixin, _OverridesFileMixin,
                    _BedtimeFileMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # Clearing an override/window can issue an unfreeze write.
        for name in ('set_frozen', 'set_frozen_app'):
            patcher = mock.patch.object(budgets, name,
                                        return_value=(True, ''))
            patcher.start()
            self.addCleanup(patcher.stop)


class TestOverrideEndpoints(_EndpointBase):
    def test_api_auth_csrf_matrix(self):
        paths = (('/api/budgets/overrides',
                  {'username': KID, 'comm': 'steam', 'mode': 'exempt'}),
                 ('/api/budgets/windows',
                  {'username': KID, 'target_type': 'category',
                   'target': 'games', 'days': '0', 'start': '10:00',
                   'end': '11:00', 'allowance_minutes': '30'}))
        anon = app.app.test_client()
        for path, data in paths:
            with self.subTest(path=path):
                self.assertEqual(anon.post(path, data=data).status_code, 401)
                # Authenticated but no CSRF header -> 403.
                self.assertEqual(self.client.post(path, data=data).status_code,
                                 403)

    def test_api_override_validation(self):
        base = {'username': KID, 'comm': 'steam', 'mode': 'limit'}
        # Bad username / comm / mode.
        for data in (
            dict(base, username='bad;user', minutes='30'),
            dict(base, username='-x', minutes='30'),
            dict(base, comm='-x', minutes='30'),
            dict(base, comm='a b', minutes='30'),
            dict(base, mode='bogus', minutes='30'),
            dict(base, minutes='abc'),
            dict(base, minutes='-5'),
        ):
            with self.subTest(data=data):
                resp = self.authed_post('/api/budgets/overrides', data=data)
                self.assertEqual(resp.status_code, 400)
        # No state written by the rejected calls.
        self.assertEqual(budgets.load_overrides(), {})
        # Wheel target refused (no state write).
        resp = self.authed_post('/api/budgets/overrides',
                                data={'username': self.ADMIN_USER,
                                      'comm': 'steam', 'mode': 'exempt'})
        self.assertEqual(resp.status_code, 403)
        # Valid set + clear round-trip.
        resp = self.authed_post('/api/budgets/overrides',
                                data=dict(base, minutes='30'))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertEqual(
            budgets.load_overrides()[KID]['app_overrides']['steam']['minutes'],
            30)
        resp = self.authed_post('/api/budgets/overrides',
                                data={'username': KID, 'comm': 'steam',
                                      'mode': 'none'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            budgets.load_overrides()[KID]['app_overrides'], {})

    def test_api_window_validation(self):
        good = {'username': KID, 'target_type': 'category',
                'target': 'games', 'days': '0,1,2', 'start': '10:00',
                'end': '11:00', 'allowance_minutes': '30', 'enabled': '1'}
        for bad in (
            dict(good, target_type='nope'),
            dict(good, target='not-a-category'),
            dict(good, days=''),
            dict(good, days='7'),
            dict(good, start='bogus'),
            dict(good, start='10:00', end='10:00'),
            dict(good, allowance_minutes='1'),
        ):
            with self.subTest(bad=bad):
                resp = self.authed_post('/api/budgets/windows', data=bad)
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(budgets.load_overrides(), {})
        # Create, then disable, then clear (round-trip).
        resp = self.authed_post('/api/budgets/windows', data=good)
        self.assertEqual(resp.status_code, 200)
        wid = 'w-category-games'
        self.assertIn(wid, budgets.load_overrides()[KID]['windows'])
        resp = self.authed_post('/api/budgets/windows',
                                data={'username': KID, 'action': 'update',
                                      'window_id': wid, 'enabled': '0'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            budgets.load_overrides()[KID]['windows'][wid]['enabled'])
        resp = self.authed_post('/api/budgets/windows',
                                data={'username': KID, 'action': 'clear',
                                      'window_id': wid})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(budgets.load_overrides()[KID]['windows'], {})

    def test_api_window_collision_rejected(self):
        data = {'username': KID, 'target_type': 'category', 'target': 'games',
                'days': '0', 'start': '10:00', 'end': '11:00',
                'allowance_minutes': '30'}
        self.assertEqual(
            self.authed_post('/api/budgets/windows', data=data).status_code,
            200)
        resp = self.authed_post('/api/budgets/windows', data=data)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already exists', resp.get_json()['error'])

    def test_audit_entries(self):
        with mock.patch.object(budgets.audit, 'safe_log') as slog:
            self.authed_post('/api/budgets/overrides',
                             data={'username': KID, 'comm': 'steam',
                                   'mode': 'limit', 'minutes': '30'})
            self.authed_post('/api/budgets/overrides',
                             data={'username': KID, 'comm': 'steam',
                                   'mode': 'none'})
            self.authed_post('/api/budgets/windows',
                             data={'username': KID, 'target_type': 'category',
                                   'target': 'games', 'days': '0',
                                   'start': '10:00', 'end': '11:00',
                                   'allowance_minutes': '30'})
            self.authed_post('/api/budgets/windows',
                             data={'username': KID, 'action': 'clear',
                                   'window_id': 'w-category-games'})
        actions = [c.args[1] for c in slog.call_args_list]
        for expected in ('budget.override_set', 'budget.override_cleared',
                         'budget.window_set', 'budget.window_cleared'):
            self.assertIn(expected, actions)
        self.assertTrue(all(c.args[0] == 'qaadmin'
                            for c in slog.call_args_list))


class TestLivePayload(_AdminSessionMixin, _OverridesFileMixin,
                      unittest.TestCase):
    def setUp(self):
        super().setUp()
        budgets.set_app_override(KID, 'steam', 'limit', 30, actor='qaadmin')
        budgets.set_window(KID, 'category', 'games', '0,1,2,3,4,5,6',
                           '00:00', '23:59', 60, actor='qaadmin')

    def _patches(self):
        return [
            mock.patch.object(app, 'get_human_users', return_value=[
                {'username': KID, 'uid': 30000, 'home': '/home/kid',
                 'shell': '/bin/bash'}]),
            mock.patch.object(app, 'get_lock_status',
                              return_value=(False, 'Unlocked')),
            mock.patch.object(app.budgets, 'list_user_processes',
                              return_value=[]),
            mock.patch.object(app.budgets, 'load_prefs', return_value={}),
            mock.patch.object(app.budgets, 'build_desktop_index',
                              return_value={}),
            mock.patch.object(app.budgets, 'build_desktop_names',
                              return_value={}),
        ]

    def test_live_payload_overrides_and_app_budgets(self):
        patchers = self._patches()
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        payload = self.client.get(f'/api/user/{KID}/live').get_json()
        self.assertTrue(payload['success'])
        self.assertIn('overrides', payload)
        self.assertEqual(
            payload['overrides']['app_overrides']['steam']['mode'], 'limit')
        self.assertEqual(len(payload['overrides']['windows']), 1)
        self.assertIn('app_budgets', payload['budget'])
        self.assertIn('steam', payload['budget']['app_budgets'])
        games = payload['budget']['games']
        self.assertIn('window_active', games)
        self.assertIn('effective_source', games)
        # Stable across polls.
        payload2 = self.client.get(f'/api/user/{KID}/live').get_json()
        self.assertEqual(sorted(payload2['budget']), sorted(payload['budget']))
        self.assertEqual(payload2['overrides'], payload['overrides'])


# ---------------------------------------------------------------------------
# Template smoke / UI
# ---------------------------------------------------------------------------

def _live_with_overrides():
    state = budgets.budget_status(KID, {KID: budgets.blank_entry()})
    groups = {c: [] for c in budgets.CATEGORIES}
    groups['games'] = [{'comm': 'steam', 'display_name': 'Steam', 'count': 1,
                        'max_elapsed': '01:00:00', 'members': []}]
    return {
        'user': {'username': KID, 'uid': 1001, 'home': '/home/kid',
                 'shell': '/bin/bash', 'locked': False,
                 'status_detail': 'Unlocked', 'bedtime': {},
                 'in_bedtime': False, 'bonus': None},
        'processes': [], 'visible': [{'comm': 'steam'}], 'hidden': [],
        'groups': groups, 'budget_state': state,
        'overrides': {
            'app_overrides': {'steam': {'mode': 'limit', 'minutes': 30}},
            'windows': [{'id': 'w-category-games', 'target_type': 'category',
                         'target': 'games', 'days': [0, 1], 'start': '14:00',
                         'end': '17:00', 'allowance_minutes': 60,
                         'enabled': True}],
        },
        'app_names': ['steam', 'firefox'],
    }


class TestUserPageTemplate(_AdminSessionMixin, unittest.TestCase):
    def test_user_page_renders_with_overrides(self):
        with mock.patch.object(app, 'get_user_live',
                               return_value=_live_with_overrides()):
            resp = self.client.get(f'/user/{KID}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('Weekly windows', html)
        self.assertIn('w-category-games', html)
        self.assertIn('data-window-id="w-category-games"', html)
        self.assertIn('class="override-mode"', html)
        self.assertIn('applyOverride(this)', html)
        self.assertIn('installed-apps', html)
        self.assertIn('addAppOverride', html)

    def test_user_page_renders_with_no_overrides(self):
        live = _live_with_overrides()
        live['overrides'] = None
        live['app_names'] = []
        with mock.patch.object(app, 'get_user_live', return_value=live):
            resp = self.client.get(f'/user/{KID}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('No weekly windows configured.', html)
        self.assertIn('No app overrides configured.', html)

    def test_render_budgets_preserves_legacy_cells(self):
        """The 10s poll guard: legacy category status markup is unchanged,
        and renderBudgets only appends the separate window-badge update."""
        with open(os.path.join(os.path.dirname(__file__), '..', 'templates',
                               'user.html'), encoding='utf-8') as f:
            src = f.read()
        self.assertIn(
            "statusEl.innerHTML = `OK <button type=\"button\" "
            "class=\"btn-freeze\" title=\"Freeze this category now\" "
            "onclick=\"freezeCategory('${PAGE_USERNAME}', '${cat}')\">"
            "Freeze</button>`;", src)
        self.assertIn("document.getElementById('budget-window-' + cat)", src)
        # Server-rendered legacy cells for a plain limited category.
        state = budgets.budget_status(
            KID, {KID: dict(budgets.blank_entry(), limits={'games': 60})})
        live = _live_with_overrides()
        live['overrides'] = None
        live['app_names'] = []
        live['budget_state'] = state
        with mock.patch.object(app, 'get_user_live', return_value=live):
            html = self.client.get(f'/user/{KID}').get_data(as_text=True)
        self.assertIn('<span title="Set a limit first">OK</span>', html)


# ---------------------------------------------------------------------------
# SQLite v2->v3 migration + mirror round-trip (store.py)
# ---------------------------------------------------------------------------

class TestSchemaV3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = self.tmp.name
        self.db = os.path.join(home, 'state.db')
        self.budgets_json = os.path.join(home, 'budgets.json')
        self.overrides_json = os.path.join(home, 'overrides.json')

    def _version(self):
        conn = sqlite3.connect(self.db)
        try:
            return int(conn.execute(
                'SELECT version FROM schema_version').fetchone()[0])
        finally:
            conn.close()

    def _columns(self, table):
        conn = sqlite3.connect(self.db)
        try:
            return [r[1] for r in conn.execute(
                'PRAGMA table_info(%s)' % table)]
        finally:
            conn.close()

    def _make_v2_db(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute('CREATE TABLE schema_version '
                         '(version INTEGER NOT NULL)')
            conn.execute('INSERT INTO schema_version VALUES (2)')
            for table in store.TABLES:
                conn.execute(
                    'CREATE TABLE %s (username TEXT NOT NULL, '
                    'field TEXT NOT NULL, value TEXT NOT NULL, '
                    'PRIMARY KEY (username, field))' % table)
            conn.execute("INSERT INTO budgets (username, field, value) "
                         "VALUES ('kid', 'limits', '{\"games\": 60}')")
            conn.execute("INSERT INTO budgets (username, field, value) "
                         "VALUES ('kid', 'day', '\"2026-10-07\"')")
            conn.commit()
        finally:
            conn.close()

    def _make_v1_db(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute('CREATE TABLE schema_version '
                         '(version INTEGER NOT NULL)')
            conn.execute('INSERT INTO schema_version VALUES (1)')
            for table in store.TABLES:
                conn.execute('CREATE TABLE %s (username TEXT PRIMARY KEY, '
                             'doc TEXT NOT NULL)' % table)
            conn.execute(
                "INSERT INTO budgets (username, doc) VALUES "
                "('kid', '{\"limits\": {\"games\": 60}, "
                "\"day\": \"2026-10-07\"}')")
            conn.commit()
        finally:
            conn.close()

    def test_schema_v1_migration_preserves_rows(self):
        self._make_v1_db()
        loaded = store.load_table(self.db, 'budgets')
        self.assertEqual(loaded['kid']['limits'], {'games': 60})
        self.assertEqual(self._version(), store.SCHEMA_VERSION)
        self.assertTrue(os.path.isfile(self.db + '.bak.1'))
        self.assertEqual(self._columns('overrides'), ['username', 'doc'])

    def test_schema_v2_migration_adds_overrides_table(self):
        self._make_v2_db()
        loaded = store.load_table(self.db, 'budgets')
        self.assertEqual(loaded['kid'], {'limits': {'games': 60},
                                         'day': '2026-10-07'})
        self.assertEqual(self._version(), store.SCHEMA_VERSION)
        self.assertTrue(os.path.isfile(self.db + '.bak.2'))
        self.assertEqual(self._columns('overrides'), ['username', 'doc'])
        # The pre-migration backup is the untouched v2 snapshot.
        conn = sqlite3.connect(self.db + '.bak.2')
        try:
            self.assertEqual(int(conn.execute(
                'SELECT version FROM schema_version').fetchone()[0]), 2)
            self.assertEqual([r[1] for r in conn.execute(
                'PRAGMA table_info(budgets)')],
                ['username', 'field', 'value'])
        finally:
            conn.close()

    def test_overrides_table_roundtrip_and_mirror(self):
        config = {KID: {'app_overrides': {'steam': {'mode': 'limit',
                                                    'minutes': 30}},
                        'windows': {'w-category-games': {
                            'target_type': 'category', 'target': 'games',
                            'days': [0], 'start': '10:00', 'end': '11:00',
                            'allowance_minutes': 30, 'enabled': True}}}}
        store.save_table(self.db, 'overrides', config, self.overrides_json)
        self.assertEqual(store.load_table(self.db, 'overrides'), config)
        with open(self.overrides_json) as f:
            self.assertEqual(json.load(f), config)
        # Per-row garbage is skipped; the rest survives.
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO overrides (username, doc) "
                         "VALUES ('broken', '{nope')")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(store.load_table(self.db, 'overrides'), config)
        # Whole-store failure -> {} (existing fail-closed contract).
        with open(self.db, 'wb') as f:
            f.write(os.urandom(256))
        self.assertEqual(store.load_table(self.db, 'overrides'), {})


# ---------------------------------------------------------------------------
# Watchdog app-group awareness
# ---------------------------------------------------------------------------

def _make_cgroup(root, name, freeze='1', events_frozen='1'):
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'cgroup.freeze'), 'w') as f:
        f.write(freeze)
    with open(os.path.join(path, 'cgroup.events'), 'w') as f:
        f.write('populated 1\nfrozen %s\n' % events_frozen)
    return path


def _read_freeze(root, name):
    with open(os.path.join(root, name, 'cgroup.freeze')) as f:
        return f.read().strip()


class TestWatchdogAppGroups(unittest.TestCase):
    def test_parse_managed_cgroup_app_group(self):
        self.assertEqual(budgets.parse_managed_cgroup('parental-kid-app-firefox'),
                         ('kid', 'app-firefox'))
        self.assertEqual(budgets.split_bucket('app-firefox'), ('app', 'firefox'))
        self.assertEqual(budgets.split_bucket('games'), ('category', 'games'))
        # Foreign schemes stay report-only.
        self.assertIsNone(
            budgets.parse_managed_cgroup('parental-tester-quarantine'))

    def test_stuck_app_group_without_state_is_thawed(self):
        with tempfile.TemporaryDirectory() as root:
            _make_cgroup(root, 'parental-%s-app-firefox' % KID)
            summary = budgets.watchdog_tick({}, cgroup_root=root)
            self.assertIn('parental-%s-app-firefox' % KID, summary['thawed'])
            self.assertEqual(
                _read_freeze(root, 'parental-%s-app-firefox' % KID), '0')

    def test_paused_app_group_is_legit(self):
        with tempfile.TemporaryDirectory() as root:
            _make_cgroup(root, 'parental-%s-app-firefox' % KID)
            entry = budgets.blank_entry()
            entry['app_paused'] = {'firefox': True}
            summary = budgets.watchdog_tick({KID: entry}, cgroup_root=root)
            self.assertIn('parental-%s-app-firefox' % KID, summary['legit'])
            self.assertEqual(
                _read_freeze(root, 'parental-%s-app-firefox' % KID), '1')

    def test_unfreeze_all_user_covers_app_and_category(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'parental-%s-games' % KID))
            os.makedirs(os.path.join(root, 'parental-%s-app-firefox' % KID))
            with mock.patch.object(budgets, 'CGROUP_ROOT', root), \
                 mock.patch.object(budgets, 'set_frozen',
                                   return_value=(True, '')) as cat_frz, \
                 mock.patch.object(budgets, 'set_frozen_app',
                                   return_value=(True, '')) as app_frz:
                ok, _res = budgets.unfreeze_all_user(KID)
            self.assertTrue(ok)
            cat_frz.assert_called_once_with(KID, 'games', False,
                                            actor='system')
            app_frz.assert_called_once_with(KID, 'firefox', False,
                                            actor='system')


if __name__ == '__main__':
    unittest.main()
