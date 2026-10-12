#!/usr/bin/env python3
"""Processes table shows budget USAGE, not process uptime (ps ETIME).

Owner report: "Processes have elapsed time greater than 1 day. Elapsed
time should reset when the budget resets. I don't care how long a
process has been running. I care how long it's been used."

So the Processes table's "Used" column (and its expanded per-PID
detail rows) shows the billable time accrued since the last reset --
the same counter the budget rows show -- never ``ps -o etime``
wall-clock age:

* a comm with an app override shows the override's ``used_minutes``,
* otherwise the owning category's ``used_minutes`` applies,
* both reset in lockstep with the daily/window reset (drive via mocks),
* ``ps`` ETIME never reaches the Processes markup or the /live payload.

Stdlib + a mocked get_user_live / admin session only: no live account,
service or system command is touched.
"""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import budgets
from test_lock_regression import _AdminSessionMixin, KID

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, 'templates', 'user.html')

# A distinctive ETIME that must never surface in the Processes UI.
BIG_ETIME = '3-04:05:06'          # 3d 4h 5m if ever formatted
BIG_ELAPSED = '3d 4h 5m'


def proc(pid, comm, etime=BIG_ETIME, category='games', cpu=1.0):
    return {'pid': pid, 'comm': comm, 'etime': etime,
            'cpu_seconds': cpu, 'args': '', 'cmdline': '/' + comm,
            'category': category}


def _live_user():
    return {'username': KID, 'uid': 1001, 'home': '/home/kid',
            'shell': '/bin/bash', 'locked': False,
            'status_detail': 'Unlocked', 'bedtime': {}, 'in_bedtime': False,
            'bonus': {'active': False, 'until': None, 'remaining_minutes': None,
                      'granted_by': None, 'last_minutes': None}}


def _category_state(used_minutes):
    return {'minutes': 0, 'used_minutes': used_minutes,
            'remaining_minutes': None, 'paused': False, 'exhausted': False}


def _get_user_live(procs, budget_state):
    """Run the real get_user_live with a synthetic process snapshot."""
    with mock.patch.object(app, 'get_human_users',
                           return_value=[{'username': KID, 'uid': 1001,
                                          'home': '/home/kid',
                                          'shell': '/bin/bash'}]), \
         mock.patch.object(app, 'get_lock_status',
                           return_value=(False, 'Unlocked')), \
         mock.patch.object(app, 'load_bedtime', return_value={}), \
         mock.patch.object(app.budgets, 'list_user_processes',
                           return_value=procs), \
         mock.patch.object(app.budgets, 'load_prefs', return_value={}), \
         mock.patch.object(app.budgets, 'build_desktop_index',
                           return_value={}), \
         mock.patch.object(app.budgets, 'build_desktop_names',
                           return_value={}), \
         mock.patch.object(app.budgets, 'load_overrides', return_value={}), \
         mock.patch.object(app.budgets, 'budget_status',
                           return_value=budget_state):
        return app.get_user_live(KID)


class TestGroupUsageMinutes(unittest.TestCase):
    """The pure derivation rule behind the Used column."""

    def test_app_override_used_minutes_wins(self):
        state = {c: _category_state(95) for c in budgets.CATEGORIES}
        state['app_budgets'] = {'steam': {'mode': 'limit', 'used_minutes': 12,
                                          'minutes': 60}}
        self.assertEqual(budgets.group_usage_minutes('games', 'steam', state),
                         12)

    def test_category_used_minutes_by_default(self):
        state = {c: _category_state(95) for c in budgets.CATEGORIES}
        state['app_budgets'] = {}
        self.assertEqual(
            budgets.group_usage_minutes('games', 'steamwebhelper', state), 95)

    def test_exempt_override_shows_zero_billed_minutes(self):
        # An exempt override is never billed; its own counter is 0.
        state = {c: _category_state(95) for c in budgets.CATEGORIES}
        state['app_budgets'] = {'steam': {'mode': 'exempt',
                                          'used_minutes': 0}}
        self.assertEqual(budgets.group_usage_minutes('games', 'steam', state),
                         0)

    def test_unknown_category_and_payload_return_none(self):
        self.assertIsNone(budgets.group_usage_minutes('games', 'x', None))
        self.assertIsNone(budgets.group_usage_minutes('games', 'x', {}))
        self.assertIsNone(
            budgets.group_usage_minutes('games', 'x', {'app_budgets': {}}))
        # A legacy payload without app_budgets still resolves the category.
        self.assertEqual(
            budgets.group_usage_minutes('games', 'x',
                                        {'games': {'used_minutes': 7}}), 7)


class TestGetUserLiveUsage(unittest.TestCase):
    """get_user_live annotates every visible group with budget usage."""

    def test_category_usage_by_default(self):
        procs = [proc(1, 'steamwebhelper'), proc(2, 'steamwebhelper')]
        state = {c: _category_state(95) for c in budgets.CATEGORIES}
        state['app_budgets'] = {}
        data = _get_user_live(procs, state)
        group = data['groups']['games'][0]
        self.assertEqual(group['comm'], 'steamwebhelper')
        self.assertEqual(group['used_minutes'], 95)
        self.assertEqual(group['used'], '1h 35m')
        # ps ETIME is still collected internally (group_by_comm aggregate)...
        self.assertEqual(group['max_minutes'], 3 * 1440 + 4 * 60 + 5)
        # ...but it is never what the Processes table renders.

    def test_app_override_usage_wins(self):
        procs = [proc(1, 'steam'),
                 proc(2, 'steamwebhelper'), proc(3, 'steamwebhelper')]
        state = {c: _category_state(95) for c in budgets.CATEGORIES}
        state['app_budgets'] = {'steam': {'mode': 'limit', 'used_minutes': 12}}
        data = _get_user_live(procs, state)
        by_comm = {g['comm']: g for g in data['groups']['games']}
        self.assertEqual(by_comm['steam']['used'], '12m')
        self.assertEqual(by_comm['steamwebhelper']['used'], '1h 35m')

    def test_usage_resets_with_the_budget_day(self):
        """The Used value is read from budget_status, so it zeroes exactly
        when the daily reset zeroes ``used_seconds``."""
        procs = [proc(1, 'steamwebhelper')]
        before = {c: _category_state(0) for c in budgets.CATEGORIES}
        after = {c: _category_state(0) for c in budgets.CATEGORIES}
        used_data = {KID: dict(budgets.blank_entry(),
                               used_seconds={'games': 95 * 60.0})}
        reset_data = {KID: budgets.blank_entry()}   # post daily reset
        with mock.patch.object(app.budgets, 'load_budgets',
                               return_value=used_data), \
             mock.patch.object(app.budgets, 'load_overrides', return_value={}), \
             mock.patch.object(app, 'get_human_users',
                               return_value=[{'username': KID, 'uid': 1001,
                                              'home': '/home/kid',
                                              'shell': '/bin/bash'}]), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             mock.patch.object(app.budgets, 'list_user_processes',
                               return_value=procs), \
             mock.patch.object(app.budgets, 'load_prefs', return_value={}), \
             mock.patch.object(app.budgets, 'build_desktop_index',
                               return_value={}), \
             mock.patch.object(app.budgets, 'build_desktop_names',
                               return_value={}):
            live = app.get_user_live(KID)
            self.assertEqual(live['groups']['games'][0]['used'], '1h 35m')
            # Simulate the daily reset: used_seconds is zeroed.
            with mock.patch.object(app.budgets, 'load_budgets',
                                   return_value=reset_data):
                live2 = app.get_user_live(KID)
        self.assertEqual(live2['groups']['games'][0]['used'], '0m')

    def test_hidden_groups_have_no_single_usage_value(self):
        procs = [proc(1, 'vlc', category='media')]
        state = {c: _category_state(10) for c in budgets.CATEGORIES}
        with mock.patch.object(app, 'get_human_users',
                               return_value=[{'username': KID, 'uid': 1001,
                                              'home': '/home/kid',
                                              'shell': '/bin/bash'}]), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             mock.patch.object(app.budgets, 'list_user_processes',
                               return_value=procs), \
             mock.patch.object(app.budgets, 'load_prefs',
                               return_value={KID: {'hidden_comm': ['vlc'],
                                                   'shown_comm': []}}), \
             mock.patch.object(app.budgets, 'build_desktop_index',
                               return_value={}), \
             mock.patch.object(app.budgets, 'build_desktop_names',
                               return_value={}), \
             mock.patch.object(app.budgets, 'load_overrides',
                               return_value={}), \
             mock.patch.object(app.budgets, 'budget_status',
                               return_value=state):
            data = app.get_user_live(KID)
        self.assertEqual(data['groups']['media'], [])
        self.assertEqual(data['hidden'][0]['used'], '')


class ProcUsagePayloadTest(_AdminSessionMixin, unittest.TestCase):
    """The /live payload + the server-rendered Processes markup."""

    def _live(self):
        groups = {c: [] for c in budgets.CATEGORIES}
        groups['games'] = [
            {'comm': 'steam', 'display_name': 'Steam', 'count': 1,
             'used_minutes': 12, 'used': '12m',
             'categories': ['games'], 'uniform_category': True,
             'members': [{'pid': 1, 'comm': 'steam', 'category': 'games',
                          'display_name': 'Steam', 'etime': BIG_ETIME}]},
            {'comm': 'steamwebhelper', 'display_name': 'steamwebhelper',
             'count': 2, 'used_minutes': 95, 'used': '1h 35m',
             'categories': ['games'], 'uniform_category': True,
             'members': [
                 {'pid': 2, 'comm': 'steamwebhelper', 'category': 'games',
                  'display_name': 'steamwebhelper', 'etime': BIG_ETIME},
                 {'pid': 3, 'comm': 'steamwebhelper', 'category': 'games',
                  'display_name': 'steamwebhelper', 'etime': BIG_ETIME}]},
        ]
        return {
            'user': _live_user(),
            'processes': [], 'visible': [{'comm': 'steam'}],
            'hidden': [],
            'groups': groups,
            'budget_state': {c: _category_state(95)
                             for c in budgets.CATEGORIES},
            'overrides': {'app_overrides': {}, 'windows': []},
            'app_names': ['steam'],
        }

    def test_live_payload_carries_used_not_elapsed(self):
        with mock.patch.object(app, 'get_user_live',
                               return_value=self._live()):
            resp = self.client.get(f'/api/user/{KID}/live')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        games = {g['comm']: g for g in payload['groups']['games']}
        self.assertEqual(games['steamwebhelper']['used'], '1h 35m')
        self.assertNotIn('elapsed', games['steamwebhelper'])
        self.assertNotIn('elapsed', games['steamwebhelper']['members'][0])
        # No ETIME anywhere in the payload the UI consumes.
        self.assertNotIn(BIG_ETIME, resp.get_data(as_text=True))

    def test_server_markup_shows_used_header_and_value(self):
        with mock.patch.object(app, 'get_user_live',
                               return_value=self._live()):
            resp = self.client.get(f'/user/{KID}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # Header + cell label changed from Elapsed to Used.
        self.assertIn('<th scope="col">Used</th>', html)
        self.assertIn('data-label="Used"', html)
        self.assertNotIn('<th scope="col">Elapsed</th>', html)
        self.assertNotIn('data-label="Elapsed"', html)
        # The rendered usage values are the budget counters.
        self.assertIn('1h 35m', html)
        self.assertIn('12m', html)
        # ps ETIME never reaches the Processes UI.
        self.assertNotIn(BIG_ETIME, html)
        self.assertNotIn(BIG_ELAPSED, html)


class TestJsRenderers(unittest.TestCase):
    """Static guards on the live-refresh renderers in user.html."""

    @classmethod
    def setUpClass(cls):
        with open(TEMPLATE, encoding='utf-8') as f:
            cls.src = f.read()

    def test_group_rows_render_used_with_matching_label(self):
        start = self.src.index('function groupRowsHtml')
        end = self.src.index('function updateToggleState')
        body = self.src[start:end]
        self.assertIn("data-label=\"Used\">' + esc(group.used", body)
        self.assertNotIn('data-label="Elapsed"', body)
        self.assertNotIn('group.elapsed', body)
        self.assertNotIn('max_elapsed', body)

    def test_group_detail_rows_render_group_usage(self):
        start = self.src.index('function groupDetailHtml')
        end = self.src.index('function freezeToggleHtml')
        body = self.src[start:end]
        self.assertIn("data-label=\"Used\">' + used", body)
        self.assertIn('<th scope="col">Used</th>', body)
        self.assertNotIn('m.elapsed', body)
        self.assertNotIn('data-label="Elapsed"', body)

    def test_live_render_emits_used_header(self):
        start = self.src.index('function renderProcesses')
        end = self.src.index('function toggleHidden')
        body = self.src[start:end]
        self.assertIn('<th scope="col">Command</th><th scope="col">Used</th>',
                      body)
        self.assertNotIn('<th scope="col">Elapsed</th>', body)

    def test_no_etime_reference_in_processes_markup(self):
        # ETIME stays an internal ps field; the template must not read it.
        self.assertNotIn('etime', self.src)
        self.assertIsNotNone(
            re.search(r'^\s*<td data-label="Used">\{\{ group\.used', self.src,
                      re.M))


if __name__ == '__main__':
    unittest.main()
