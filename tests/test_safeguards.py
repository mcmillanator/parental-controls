#!/usr/bin/env python3
"""Safeguards against accidental lockout (MVP).

- Wheel members (admins, rule-based — no hardcoded names) are never
  auto-locked by bedtime enforcement nor auto-frozen by budget enforcement.
- Destructive UI buttons carry confirm() dialogs, including a clearly-marked
  Emergency logout button on the user details page.

Fake users/groups only (unittest.mock) — never touches real accounts,
sessions, or cgroups. Run with system python3:
    python3 -m unittest discover -s tests
Flask-dependent UI tests skip gracefully when Flask is not installed.
"""

import json
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import budgets
from budgets import account_and_enforce, blank_entry, tick_budgets

try:
    import flask  # noqa: F401
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

FakeGroup = namedtuple('FakeGroup', ['gr_name', 'gr_gid', 'gr_mem'])
FakePasswd = namedtuple('FakePasswd', ['pw_name', 'pw_gid'])

WHEEL = FakeGroup('wheel', 10, ['fakeadmin', 'otheradmin'])


def fake_getgrnam(name):
    if name == 'wheel':
        return WHEEL
    raise KeyError(name)


def fake_getpwnam(name):
    gids = {'fakeadmin': 1000, 'wheelprimary': 10, 'kid': 1001}
    if name in gids:
        return FakePasswd(name, gids[name])
    raise KeyError(name)


class TestWheelRule(unittest.TestCase):
    def test_supplementary_member_is_protected(self):
        with mock.patch('budgets.grp.getgrnam', fake_getgrnam), \
             mock.patch('budgets.pwd.getpwnam', fake_getpwnam):
            self.assertTrue(budgets.is_admin_protected('fakeadmin'))

    def test_primary_gid_member_is_protected(self):
        with mock.patch('budgets.grp.getgrnam', fake_getgrnam), \
             mock.patch('budgets.pwd.getpwnam', fake_getpwnam):
            self.assertTrue(budgets.is_admin_protected('wheelprimary'))

    def test_ordinary_user_not_protected(self):
        with mock.patch('budgets.grp.getgrnam', fake_getgrnam), \
             mock.patch('budgets.pwd.getpwnam', fake_getpwnam):
            self.assertFalse(budgets.is_admin_protected('kid'))

    def test_missing_wheel_group_protects_nothing(self):
        with mock.patch('budgets.grp.getgrnam',
                        side_effect=KeyError('wheel')):
            self.assertFalse(budgets.is_admin_protected('fakeadmin'))

    def test_unknown_user_not_protected(self):
        with mock.patch('budgets.grp.getgrnam', fake_getgrnam), \
             mock.patch('budgets.pwd.getpwnam',
                        side_effect=KeyError('ghost')):
            self.assertFalse(budgets.is_admin_protected('ghost'))

    def test_empty_username_not_protected(self):
        self.assertFalse(budgets.is_admin_protected(''))


class TestBudgetWheelSkip(unittest.TestCase):
    def _exhausted_entry(self):
        e = blank_entry()
        e['limits'] = {'games': 0.05}  # 3 seconds: exhausts on one tick
        e['day'] = '2026-10-07'
        return e

    def test_account_and_enforce_never_freezes_wheel(self):
        entry = self._exhausted_entry()
        procs = [{'pid': 9001, 'comm': 'steam',
                  'cmdline': '/x/steam', 'args': '',
                  'etime': '01:00:00', 'cpu_seconds': 100.0}]
        now = datetime(2026, 10, 7, 15, 0)
        with mock.patch.object(budgets, 'is_admin_protected',
                               return_value=True):
            account_and_enforce('fakeadmin', entry, procs, None, now,
                                prefs={}, index={}, act=False)
            procs2 = [dict(p, cpu_seconds=p['cpu_seconds'] + 30.0)
                      for p in procs]
            summary = account_and_enforce(
                'fakeadmin', entry, procs2, None,
                now + timedelta(seconds=60), prefs={}, index={}, act=False)
        self.assertFalse(entry['paused'].get('games'))
        self.assertEqual(summary['froze'], [])
        self.assertIn('wheel', summary['note'])

    def test_tick_budgets_skips_wheel_without_touching_procs(self):
        with tempfile.TemporaryDirectory() as d:
            budgets_file = os.path.join(d, 'budgets.json')
            entry = self._exhausted_entry()
            with open(budgets_file, 'w') as f:
                json.dump({'fakeadmin': entry, 'kid': self._exhausted_entry()},
                          f)
            provider = mock.Mock(return_value=[])
            now = datetime(2026, 10, 7, 15, 0)
            with mock.patch.object(budgets, 'BUDGETS_FILE', budgets_file), \
                 mock.patch.object(budgets, 'is_admin_protected',
                                   side_effect=lambda u: u == 'fakeadmin'):
                summaries = tick_budgets({}, now, procs_provider=provider,
                                         prefs={}, index={}, act=False)
        by_user = {s['user']: s for s in summaries}
        self.assertIn('wheel', by_user['fakeadmin']['note'])
        self.assertEqual(by_user['fakeadmin']['froze'], [])
        # Live ps snapshot taken only for the non-admin user.
        provider.assert_called_once_with('kid')


@unittest.skipUnless(HAS_FLASK, 'Flask not installed')
class TestBedtimeWheelSkip(unittest.TestCase):
    def test_enforce_bedtime_never_locks_wheel(self):
        import app
        bedtime = {
            'fakeadmin': {'start': '00:00', 'end': '23:59',
                          'days': [0, 1, 2, 3, 4, 5, 6]},
            'kid': {'start': '00:00', 'end': '23:59',
                    'days': [0, 1, 2, 3, 4, 5, 6]},
        }
        with mock.patch.object(app, 'load_bedtime',
                               return_value={k: dict(v)
                                             for k, v in bedtime.items()}), \
             mock.patch.object(app, 'save_bedtime'), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock, \
             mock.patch.object(budgets, 'is_admin_protected',
                               side_effect=lambda u: u == 'fakeadmin'), \
             mock.patch.object(app.budgets, 'is_admin_protected',
                               side_effect=lambda u: u == 'fakeadmin'):
            app.enforce_bedtime()
        locked_users = [c.args[0] for c in lock.call_args_list]
        self.assertNotIn('fakeadmin', locked_users)
        self.assertIn('kid', locked_users)


@unittest.skipUnless(HAS_FLASK, 'Flask not installed')
class TestConfirmUI(unittest.TestCase):
    def _admin_patches(self):
        """Fake a logged-in wheel admin session (MVP auth gates pages)."""
        import app
        store = {'users': {'boss': {'hash': 'x', 'must_change': False,
                                   'locked': False}}}
        sess = {'username': 'boss', 'csrf': 'test-csrf', 'created': 0,
                'last_seen': 0}
        return (
            mock.patch.object(app.auth, 'get_session',
                              return_value=dict(sess)),
            mock.patch.object(app.auth, 'load_store', return_value=store),
            mock.patch.object(app.auth, 'is_wheel_member', return_value=True),
            mock.patch.object(app.auth, 'is_system_user', return_value=True),
        )

    def test_index_buttons_carry_confirms(self):
        import app
        fake_users = [{'username': 'kid', 'uid': 1001,
                       'home': '/home/kid', 'shell': '/bin/bash'}]
        patches = self._admin_patches()
        with mock.patch.object(app, 'get_human_users',
                               return_value=fake_users), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             patches[0], patches[1], patches[2], patches[3]:
            client = app.app.test_client()
            client.set_cookie('pc_sid', 'fake-test-session')
            resp = client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        for fn in ('lockUser', 'unlockUser'):
            self.assertIn(fn, html)
        # Lock Screen was deliberately removed: Lock already locks the
        # account and the screen. Its absence is asserted, not incidental.
        self.assertNotIn('lockScreenUser', html)
        self.assertIn('confirm(', html)
        self.assertNotIn('TODO: add confirmation dialog', html)

    def test_user_page_has_emergency_logout_with_strong_confirm(self):
        import app
        live = {
            'user': {'username': 'kid', 'uid': 1001, 'home': '/home/kid',
                     'shell': '/bin/bash', 'locked': False,
                     'status_detail': 'Unlocked', 'bedtime': {},
                     'in_bedtime': False},
            'processes': [], 'visible': [], 'hidden': [],
            'groups': {c: [] for c in budgets.CATEGORIES},
            'budget_state': budgets.budget_status(
                'kid', {'kid': blank_entry()}),
        }
        with mock.patch.object(app, 'get_user_live', return_value=live), \
             self._admin_patches()[0], self._admin_patches()[1], \
             self._admin_patches()[2], self._admin_patches()[3]:
            client = app.app.test_client()
            client.set_cookie('pc_sid', 'fake-test-session')
            resp = client.get('/user/kid')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('Emergency logout', html)
        self.assertIn('/api/logout', html)
        self.assertIn('logoutUser', html)
        self.assertIn('confirm(', html)
        # Lock + Unlock confirms present on details page. Lock Screen was
        # deliberately removed (Lock already locks account + screen).
        for fn in ('lockUser', 'unlockUser'):
            self.assertIn(fn, html)
        self.assertNotIn('lockScreenUser', html)


if __name__ == '__main__':
    unittest.main()
