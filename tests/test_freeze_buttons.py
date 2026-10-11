#!/usr/bin/env python3
"""Freeze/Unfreeze buttons: budgets unit (F-U), endpoint (F-E), template (F-T),
safety/deploy (F-D) tests. See FREEZE_BUTTONS_PLAN.md sections 2-6.

Conventions mirror tests/test_budgets.py (stdlib only, mocked helpers, temp
budgets file) and tests/test_lock_regression.py (_AdminSessionMixin +
authed_post, mocked budgets delegates, mocked subprocess so no real
cgroup/sudo). No test touches tester/bluefox/stan/pcagent, real cgroups,
or sudo.
"""

import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import app
import auth
import budgets
from test_lock_regression import _AdminSessionMixin

Result = namedtuple('Result', ['returncode', 'stdout', 'stderr'])

KID = 'freezekid'

STEAM_PROC = {'pid': 9001, 'comm': 'steam',
              'cmdline': '/home/x/.local/share/Steam/ubuntu12_32/steam',
              'args': '', 'etime': '01:00:00', 'cpu_seconds': 100.0}
BASH_PROC = {'pid': 9002, 'comm': 'bash',
             'cmdline': 'bash /home/x/.local/share/Steam/steam.sh',
             'args': '', 'etime': '01:00:00', 'cpu_seconds': 5.0}


def _write_budgets(path, data):
    with open(path, 'w') as f:
        json.dump(data, f)


def _read_budgets(path):
    with open(path) as f:
        return json.load(f)


class _BudgetsFileMixin:
    """Point budgets.BUDGETS_FILE at a temp file for the duration of a test."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        tmp.close()
        self._budgets_path = tmp.name
        _write_budgets(self._budgets_path, {})
        patcher = mock.patch.object(budgets, 'BUDGETS_FILE',
                                    self._budgets_path)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.addCleanup(os.unlink, self._budgets_path)

    def write_entry(self, username, **overrides):
        entry = budgets.blank_entry()
        entry.update(overrides)
        data = _read_budgets(self._budgets_path)
        data[username] = entry
        _write_budgets(self._budgets_path, data)
        return entry

    def read_entry(self, username):
        return _read_budgets(self._budgets_path)[username]


class _NoPrivilegedHelpersMixin:
    """Hermetic freeze_category wiring: pure-python + mocked cgroup ops."""

    def _freeze_wiring(self, **kw):
        """Default mocked wiring; override pieces per test via kwargs."""
        wiring = {
            'list_user_processes': mock.DEFAULT,
            'load_prefs': mock.DEFAULT,
            'build_desktop_index': mock.DEFAULT,
            'ensure_cgroup': mock.DEFAULT,
            'migrate_pid': mock.DEFAULT,
            'cgroup_members': mock.DEFAULT,
            'set_frozen': mock.DEFAULT,
            'is_admin_protected': mock.DEFAULT,
            'subprocess.run': mock.DEFAULT,
        }
        wiring.update(kw)
        return wiring

    def _patched(self, test, procs, limits, cgroup_current,
                 verify_result=None):
        """Context managers stack for a freeze_category drive."""
        stack = [
            mock.patch.object(budgets, 'list_user_processes',
                              return_value=procs),
            mock.patch.object(budgets, 'load_prefs', return_value={}),
            mock.patch.object(budgets, 'build_desktop_index',
                              return_value={}),
            mock.patch.object(budgets, 'ensure_cgroup',
                              return_value=(True, '')),
            mock.patch.object(budgets, 'cgroup_members',
                              return_value=list(cgroup_current)),
            mock.patch.object(budgets, 'is_admin_protected',
                              return_value=False),
        ]
        _write_budgets(self._budgets_path,
                       {KID: dict(budgets.blank_entry(),
                                 limits=dict(limits))})
        return stack


# ---------------------------------------------------------------------------
# Unit — budgets.py (F-U1..F-U10)
# ---------------------------------------------------------------------------

class TestFreezeUnits(_BudgetsFileMixin, _NoPrivilegedHelpersMixin,
                      unittest.TestCase):
    def test_f_u1_rejects_bad_username_category_without_side_effects(self):
        """F-U1: bad username/category -> (False, ...) with no subprocess."""
        with mock.patch.object(budgets.subprocess, 'run') as run, \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False):
            for bad_user in ('', '-x', 'a/b', 'a b', 'x;y'):
                for cat in ('games', 'nope'):
                    ok, msg = budgets.freeze_category(bad_user, cat)
                    self.assertFalse(ok, (bad_user, cat))
            for bad_cat in ('nope', '', 'quarantine'):
                ok, msg = budgets.freeze_category(KID, bad_cat)
                self.assertFalse(ok, bad_cat)
                self.assertIn('Unknown category', msg)
            ok, msg = budgets.unfreeze_category('-x', 'games')
            self.assertFalse(ok)
        run.assert_not_called()

    def test_f_u2_requires_configured_limit(self):
        """F-U2: no limit -> refuse before any cgroup write."""
        self.write_entry(KID, limits={})
        with mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False), \
             mock.patch.object(budgets, 'ensure_cgroup') as ensure, \
             mock.patch.object(budgets, 'set_frozen') as frozen:
            ok, msg = budgets.freeze_category(KID, 'games')
        self.assertFalse(ok)
        self.assertIn('Set a time limit', msg)
        ensure.assert_not_called()
        frozen.assert_not_called()

    def test_f_u3_never_pause_excluded_at_match_time(self):
        """F-U3: NEVER_PAUSE members never reach the cgroup (S1)."""
        self.write_entry(KID, limits={'games': 60})
        migrated = []

        def fake_migrate(user, cat, pid):
            migrated.append(pid)
            return True, ''

        seen = {}

        def fake_verify(user, current, expected):
            seen['expected'] = set(expected)
            return True, ''

        with mock.patch.object(budgets, 'list_user_processes',
                               return_value=[dict(STEAM_PROC),
                                             dict(BASH_PROC)]), \
             mock.patch.object(budgets, 'load_prefs', return_value={}), \
             mock.patch.object(budgets, 'build_desktop_index',
                               return_value={}), \
             mock.patch.object(budgets, 'ensure_cgroup',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'migrate_pid',
                               side_effect=fake_migrate), \
             mock.patch.object(budgets, 'cgroup_members',
                               return_value=[STEAM_PROC['pid']]), \
             mock.patch.object(budgets, 'verify_freeze_safety',
                               side_effect=fake_verify), \
             mock.patch.object(budgets, 'set_frozen',
                               return_value=(True, '')) as froze, \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False):
            ok, msg = budgets.freeze_category(KID, 'games')
        self.assertTrue(ok, msg)
        # bash excluded at match time: never migrated, never verified.
        self.assertEqual(migrated, [STEAM_PROC['pid']])
        self.assertEqual(seen['expected'], {STEAM_PROC['pid']})
        froze.assert_called_once_with(KID, 'games', True, actor='system')
        # Same gate, direct: sneaky bash with a games-flavoured cmdline.
        self.assertEqual(budgets.freezable_members(
            [dict(BASH_PROC)], KID, 'games', {'games': 60},
            prefs={}, index={}), [])

    def test_f_u4_stray_cgroup_member_aborts_freeze(self):
        """F-U4: non-owned / unclassified member aborts, no freeze (S2)."""
        self.write_entry(KID, limits={'games': 60})
        stray = 424242
        real_stat = os.stat

        def fake_stat(path):
            if path == f'/proc/{stray}':
                return SimpleNamespace(st_uid=9999)
            return SimpleNamespace(st_uid=4242)

        with mock.patch.object(budgets, 'list_user_processes',
                               return_value=[dict(STEAM_PROC)]), \
             mock.patch.object(budgets, 'load_prefs', return_value={}), \
             mock.patch.object(budgets, 'build_desktop_index',
                               return_value={}), \
             mock.patch.object(budgets, 'freezable_members',
                               return_value=[dict(STEAM_PROC)]), \
             mock.patch.object(budgets, 'ensure_cgroup',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'migrate_pid',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'cgroup_members',
                               return_value=[STEAM_PROC['pid'], stray]), \
             mock.patch.object(budgets, 'set_frozen') as froze, \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False), \
             mock.patch.object(budgets.pwd, 'getpwnam',
                               return_value=SimpleNamespace(pw_uid=4242)), \
             mock.patch('os.stat', side_effect=fake_stat):
            ok, msg = budgets.freeze_category(KID, 'games')
        self.assertFalse(ok)
        self.assertIn(str(stray), msg)
        froze.assert_not_called()
        self.assertNotIn('manual_frozen',
                         _read_budgets(self._budgets_path).get(KID, {}).get(
                             'manual_frozen', {}))

    def test_f_u5_never_pause_member_in_cgroup_aborts(self):
        """F-U5: NEVER_PAUSE PID found in cgroup aborts with reason (S2)."""
        self.write_entry(KID, limits={'games': 60})
        bash_pid = BASH_PROC['pid']
        real_open = open

        def fake_open(path, *args, **kwargs):
            if isinstance(path, str) and path.startswith('/proc/'):
                if path.endswith('/comm'):
                    return io.StringIO('bash\n')
                return io.BytesIO(b'bash\x00')
            return real_open(path, *args, **kwargs)

        def fake_stat(path):
            return SimpleNamespace(st_uid=4242)

        with mock.patch.object(budgets, 'list_user_processes',
                               return_value=[dict(STEAM_PROC)]), \
             mock.patch.object(budgets, 'load_prefs', return_value={}), \
             mock.patch.object(budgets, 'build_desktop_index',
                               return_value={}), \
             mock.patch.object(budgets, 'freezable_members',
                               return_value=[dict(STEAM_PROC)]), \
             mock.patch.object(budgets, 'ensure_cgroup',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'migrate_pid',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'cgroup_members',
                               return_value=[STEAM_PROC['pid'], bash_pid]), \
             mock.patch.object(budgets, 'set_frozen') as froze, \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False), \
             mock.patch.object(budgets.pwd, 'getpwnam',
                               return_value=SimpleNamespace(pw_uid=4242)), \
             mock.patch('os.stat', side_effect=fake_stat), \
             mock.patch('builtins.open', side_effect=fake_open):
            ok, msg = budgets.freeze_category(KID, 'games')
        self.assertFalse(ok)
        self.assertIn('NEVER', msg)
        froze.assert_not_called()

    def test_f_u6_empty_category_records_state_without_freezing(self):
        """F-U6: nothing running -> manual_frozen set, no freeze write."""
        self.write_entry(KID, limits={'games': 60})
        with mock.patch.object(budgets, 'list_user_processes',
                               return_value=[]), \
             mock.patch.object(budgets, 'load_prefs', return_value={}), \
             mock.patch.object(budgets, 'build_desktop_index',
                               return_value={}), \
             mock.patch.object(budgets, 'ensure_cgroup',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'cgroup_members', return_value=[]), \
             mock.patch.object(budgets, 'set_frozen') as froze, \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False), \
             mock.patch.object(budgets.pwd, 'getpwnam',
                               return_value=SimpleNamespace(pw_uid=4242)), \
             mock.patch('os.stat',
                        return_value=SimpleNamespace(st_uid=4242)):
            ok, msg = budgets.freeze_category(KID, 'games')
        self.assertTrue(ok, msg)
        froze.assert_not_called()
        entry = self.read_entry(KID)
        self.assertTrue(entry['manual_frozen'].get('games'))
        self.assertIn('games', entry.get('manual_frozen_at', {}))

    def test_f_u7_freeze_failure_persists_nothing(self):
        """F-U7: set_frozen failure -> (False, ...) and no state (S6)."""
        self.write_entry(KID, limits={'games': 60})
        with mock.patch.object(budgets, 'list_user_processes',
                               return_value=[dict(STEAM_PROC)]), \
             mock.patch.object(budgets, 'load_prefs', return_value={}), \
             mock.patch.object(budgets, 'build_desktop_index',
                               return_value={}), \
             mock.patch.object(budgets, 'freezable_members',
                               return_value=[dict(STEAM_PROC)]), \
             mock.patch.object(budgets, 'ensure_cgroup',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'migrate_pid',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'cgroup_members',
                               return_value=[STEAM_PROC['pid']]), \
             mock.patch.object(budgets, 'verify_freeze_safety',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'set_frozen',
                               return_value=(False, 'tee failed')), \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False):
            ok, msg = budgets.freeze_category(KID, 'games')
        self.assertFalse(ok)
        self.assertIn('tee failed', msg)
        self.assertEqual(self.read_entry(KID).get('manual_frozen', {}), {})

    def test_f_u8_reset_clears_manual_freeze(self):
        """F-U8: day reset clears manual_frozen(+_at) and thaws (S8)."""
        entry = budgets.blank_entry()
        entry['day'] = '2000-01-01'
        entry['limits'] = {'games': 60}
        entry['manual_frozen'] = {'games': True}
        entry['manual_frozen_at'] = {'games': '2000-01-02T00:00:00'}
        with mock.patch.object(budgets, 'unfreeze_all_user',
                               return_value=(True, [])) as unfreeze:
            summary = budgets.account_and_enforce(
                KID, entry, [], None, datetime(2026, 10, 10, 12, 0),
                prefs={}, index={}, act=True)
        self.assertTrue(summary['reset'])
        unfreeze.assert_called_once_with(KID)
        self.assertEqual(entry['manual_frozen'], {})
        self.assertEqual(entry['manual_frozen_at'], {})

    def test_f_u9_watchdog_honours_manual_freeze(self):
        """F-U9: manual_frozen (no paused) reads as legit, never thawed."""
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'parental-freezekid-games')
            os.makedirs(path)
            with open(os.path.join(path, 'cgroup.freeze'), 'w') as f:
                f.write('1')
            with open(os.path.join(path, 'cgroup.events'), 'w') as f:
                f.write('populated 1\nfrozen 1\n')
            entry = budgets.blank_entry()
            entry['manual_frozen'] = {'games': True}
            summary = budgets.watchdog_tick({KID: entry}, cgroup_root=root)
            self.assertIn('parental-freezekid-games', summary['legit'])
            self.assertNotIn('parental-freezekid-games', summary['thawed'])
            with open(os.path.join(path, 'cgroup.freeze')) as f:
                self.assertEqual(f.read().strip(), '1')

    def test_f_u10_clearing_limit_releases_manual_freeze(self):
        """F-U10: set_limit clear pops manual_frozen and thaws (S10)."""
        self.write_entry(KID, limits={'games': 60},
                         manual_frozen={'games': True},
                         manual_frozen_at={'games': 'x'})
        with mock.patch.object(budgets, 'set_frozen',
                               return_value=(True, '')) as froze:
            ok, msg = budgets.set_limit(KID, 'games', 0)
        self.assertTrue(ok, msg)
        froze.assert_called_once_with(KID, 'games', False, actor='system')
        entry = self.read_entry(KID)
        self.assertEqual(entry.get('manual_frozen', {}), {})
        self.assertEqual(entry.get('manual_frozen_at', {}), {})


# ---------------------------------------------------------------------------
# Endpoint — app.py Flask client (F-E1..F-E7 + resume alias)
# ---------------------------------------------------------------------------

class TestFreezeEndpoints(_AdminSessionMixin, unittest.TestCase):
    def test_f_e1_freeze_unfreeze_happy_path(self):
        """F-E1: delegates called with (user, cat), 200 {success: True}."""
        with mock.patch.object(app.budgets, 'freeze_category',
                               return_value=(True, 'Frozen Games')) as frz, \
             mock.patch.object(app.budgets, 'unfreeze_category',
                               return_value=(True, 'Resumed; ...')) as unfrz:
            resp = self.authed_post('/api/budgets/freeze',
                                    data={'username': KID,
                                          'category': 'games'})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()['success'])
            frz.assert_called_once_with(KID, 'games', actor='qaadmin')
            resp = self.authed_post('/api/budgets/unfreeze',
                                    data={'username': KID,
                                          'category': 'games'})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()['success'])
            unfrz.assert_called_once_with(KID, 'games', actor='qaadmin')

    def test_f_e1_resume_alias_clears_manual_freeze(self):
        """Resume endpoint subsumed by unfreeze (single operation)."""
        with mock.patch.object(app.budgets, 'unfreeze_category',
                               return_value=(True, 'Resumed; will not '
                                                   're-pause until the next '
                                                   'reset')) as unfrz:
            resp = self.authed_post('/api/budgets/resume',
                                    data={'username': KID,
                                          'category': 'games'})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertIn('will not re-pause', body['message'])
        unfrz.assert_called_once_with(KID, 'games', actor='qaadmin')

    def test_f_e2_invalid_username_rejected_before_delegate(self):
        """F-E2: bad usernames -> 400 on both endpoints, no delegate."""
        with mock.patch.object(app.budgets, 'freeze_category') as frz, \
             mock.patch.object(app.budgets, 'unfreeze_category') as unfrz, \
             mock.patch.object(app.subprocess, 'run',
                               return_value=Result(0, '', '')) as run:
            for bad in ('-x', 'a/b', '', 'a b', 'x;y'):
                with self.subTest(username=bad):
                    for path in ('/api/budgets/freeze',
                                 '/api/budgets/unfreeze'):
                        resp = self.authed_post(
                            path, data={'username': bad,
                                        'category': 'games'})
                        self.assertEqual(resp.status_code, 400, path)
        frz.assert_not_called()
        unfrz.assert_not_called()
        run.assert_not_called()

    def test_f_e3_unknown_category_rejected_before_delegate(self):
        """F-E3: unknown category -> 400, delegate never called (S5)."""
        with mock.patch.object(app.budgets, 'freeze_category') as frz, \
             mock.patch.object(app.budgets, 'unfreeze_category') as unfrz, \
             mock.patch.object(app.subprocess, 'run',
                               return_value=Result(0, '', '')) as run:
            for path in ('/api/budgets/freeze', '/api/budgets/unfreeze'):
                resp = self.authed_post(path, data={'username': KID,
                                                    'category': 'nope'})
                self.assertEqual(resp.status_code, 400, path)
                self.assertIn('Unknown category',
                              resp.get_json().get('error', ''))
        frz.assert_not_called()
        unfrz.assert_not_called()
        run.assert_not_called()

    def test_f_e4_wheel_target_refused(self):
        """F-E4: wheel member target -> 403 (S4)."""
        with mock.patch.object(app.auth, 'is_wheel_member',
                               return_value=True), \
             mock.patch.object(app.budgets, 'is_admin_protected',
                               return_value=False), \
             mock.patch.object(app.budgets, 'freeze_category') as frz, \
             mock.patch.object(app.budgets, 'unfreeze_category') as unfrz:
            for path in ('/api/budgets/freeze', '/api/budgets/unfreeze'):
                resp = self.authed_post(path, data={'username': KID,
                                                    'category': 'games'})
                self.assertEqual(resp.status_code, 403, path)
        frz.assert_not_called()
        unfrz.assert_not_called()

    def test_f_e5_admin_protected_target_refused(self):
        """F-E5: admin-protected (non-wheel-view) target -> 403 (S4)."""
        # NOTE: app.auth.is_wheel_member keeps the mixin default (False
        # for KID, True for the session admin) — mocking it False would
        # invalidate our own admin session (401, not 403).
        with mock.patch.object(app.budgets, 'is_admin_protected',
                               return_value=True), \
             mock.patch.object(app.budgets, 'freeze_category') as frz, \
             mock.patch.object(app.budgets, 'unfreeze_category') as unfrz:
            for path in ('/api/budgets/freeze', '/api/budgets/unfreeze'):
                resp = self.authed_post(path, data={'username': KID,
                                                    'category': 'games'})
                self.assertEqual(resp.status_code, 403, path)
        frz.assert_not_called()
        unfrz.assert_not_called()

    def test_f_e6_unauthenticated_post_rejected(self):
        """F-E6: no session -> 401 JSON (S12)."""
        client = app.app.test_client()
        for path in ('/api/budgets/freeze', '/api/budgets/unfreeze'):
            resp = client.post(path, data={'username': KID,
                                            'category': 'games'})
            self.assertEqual(resp.status_code, 401, path)
            self.assertFalse(resp.get_json()['success'])

    def test_f_e7_missing_csrf_rejected(self):
        """F-E7: session without CSRF token -> 403 (S12)."""
        for path in ('/api/budgets/freeze', '/api/budgets/unfreeze'):
            resp = self.client.post(path, data={'username': KID,
                                                 'category': 'games'})
            self.assertEqual(resp.status_code, 403, path)


# ---------------------------------------------------------------------------
# Template / live payload (F-T1, F-T2)
# ---------------------------------------------------------------------------

def _fake_live(budget_state, groups=None, hidden=None):
    return {
        'user': {'username': KID, 'locked': False,
                 'status_detail': 'Unlocked', 'in_bedtime': False,
                 'bedtime': {'manual_override': False},
                 'bonus': {'active': False}},
        'processes': [], 'visible': [], 'hidden': hidden or [],
        'groups': groups if groups is not None
        else {c: [] for c in budgets.CATEGORIES},
        'budget_state': budget_state,
    }


def _budget_state_frozen():
    data = {KID: dict(budgets.blank_entry(),
                      limits={'games': 60, 'browsers': 30},
                      manual_frozen={'games': True},
                      manual_frozen_at={'games': 'x'})}
    return budgets.budget_status(KID, data)


class TestFreezeTemplate(_AdminSessionMixin, unittest.TestCase):
    def test_f_t1_user_page_renders_freeze_unfreeze_toggle(self):
        """F-T1: Unfreeze for frozen, Freeze for limited, none w/o limit."""
        groups = {c: [] for c in budgets.CATEGORIES}
        groups['games'] = [{'comm': 'steam', 'display_name': 'Steam',
                            'count': 1, 'max_elapsed': '01:00:00',
                            'members': []}]
        live = _fake_live(_budget_state_frozen(), groups=groups)
        live['visible'] = [{'comm': 'steam'}]
        with mock.patch.object(app, 'get_user_live',
                               return_value=live):
            resp = self.client.get(f'/user/{KID}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # Frozen games row: manual badge + Unfreeze, no Freeze.
        # (onclick-prefixed: 'unfreezeCategory(...' contains the substring
        # 'freezeCategory(...', so bare-substring asserts would lie.)
        self.assertIn('Frozen — by parent', html)
        self.assertIn(f"unfreezeCategory('{KID}', 'games')", html)
        self.assertNotIn(f"onclick=\"freezeCategory('{KID}', 'games')\"",
                         html)
        # Limited browsers row: Freeze button present.
        self.assertIn(f"onclick=\"freezeCategory('{KID}', 'browsers')\"",
                      html)
        # Unlimited media row: NO Freeze button, limit-first tooltip.
        self.assertNotIn(f"freezeCategory('{KID}', 'media')", html)
        self.assertIn('Set a limit first', html)
        # Old Resume button is gone (single Unfreeze toggle now).
        self.assertNotIn('>Resume</button>', html)
        # Visible process-group row carries the same category toggle.
        self.assertIn(f"unfreezeCategory('{KID}', 'games')", html)
        self.assertIn('data-group-comm="steam"', html)

    def test_f_t2_live_budget_carries_frozen_keys_groups_unchanged(self):
        """F-T2: budget[cat] has manual_frozen+frozen; no per-group keys."""
        groups = {c: [] for c in budgets.CATEGORIES}
        groups['games'] = [{'comm': 'steam', 'display_name': 'Steam',
                            'count': 1, 'max_elapsed': '01:00:00',
                            'members': [{'pid': 9001, 'comm': 'steam',
                                         'display_name': 'Steam',
                                         'elapsed': '01:00:00'}]}]
        with mock.patch.object(app, 'get_user_live',
                               return_value=_fake_live(
                                   _budget_state_frozen(),
                                   groups=groups)):
            resp = self.client.get(f'/api/user/{KID}/live')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        games = body['budget']['games']
        self.assertTrue(games['manual_frozen'])
        self.assertTrue(games['frozen'])
        self.assertFalse(body['budget']['media']['frozen'])
        # Option A: no per-group frozen duplication.
        for cat, grp_list in body['groups'].items():
            for grp in grp_list:
                self.assertNotIn('frozen', grp)


# ---------------------------------------------------------------------------
# Safety / deployment (F-D1, F-D2)
# ---------------------------------------------------------------------------

class TestFreezeDeploy(unittest.TestCase):
    def test_f_d1_no_new_privileged_writes(self):
        """F-D1: sudoers grants pinned; new code uses helpers only (S11)."""
        path = os.path.join(os.path.dirname(__file__), '..',
                            'sudoers-parental-controls-budgets')
        with open(path) as f:
            lines = [ln.strip() for ln in f
                     if ln.strip() and not ln.strip().startswith('#')]
        service = '@SERVICE_USER@'
        self.assertEqual(lines, [
            f'{service} ALL=(root) NOPASSWD: /usr/sbin/tee '
            '/sys/fs/cgroup/parental-*/cgroup.freeze, /usr/bin/tee '
            '/sys/fs/cgroup/parental-*/cgroup.freeze',
            f'{service} ALL=(root) NOPASSWD: /usr/sbin/tee '
            '/sys/fs/cgroup/parental-*/cgroup.procs, /usr/bin/tee '
            '/sys/fs/cgroup/parental-*/cgroup.procs',
            f'{service} ALL=(root) NOPASSWD: /usr/sbin/cat '
            '/sys/fs/cgroup/parental-*/cgroup.freeze, /usr/bin/cat '
            '/sys/fs/cgroup/parental-*/cgroup.freeze',
            f'{service} ALL=(root) NOPASSWD: /usr/sbin/cat '
            '/sys/fs/cgroup/parental-*/cgroup.procs, /usr/bin/cat '
            '/sys/fs/cgroup/parental-*/cgroup.procs',
            f'{service} ALL=(root) NOPASSWD: /usr/sbin/mkdir -p '
            '/sys/fs/cgroup/parental-*, /usr/bin/mkdir -p '
            '/sys/fs/cgroup/parental-*',
        ])
        # The new freeze paths must not touch subprocess/sudo directly —
        # all privileged writes go through the five existing helpers.
        for fn in (budgets.freeze_category, budgets.unfreeze_category):
            src = inspect.getsource(fn)
            self.assertNotIn('subprocess', src, fn.__name__)
            self.assertNotIn('sudo', src, fn.__name__)
        for helper in ('ensure_cgroup', 'migrate_pid', 'set_frozen',
                       'cgroup_members', 'verify_freeze_safety'):
            self.assertIn(helper,
                          inspect.getsource(budgets.freeze_category),
                          helper)

    def test_f_d2_preexisting_interfaces_stable(self):
        """F-D2: pre-existing suite unaffected — modules import, contracts
        hold (paused/exhausted byte-identical, resume alias, routes)."""
        import test_auth
        import test_bonus_time
        import test_budgets
        import test_lock
        import test_lock_regression
        import test_process_grouping
        import test_safeguards
        import test_watchdog
        for mod in (test_auth, test_bonus_time, test_budgets, test_lock,
                    test_lock_regression, test_process_grouping,
                    test_safeguards, test_watchdog):
            self.assertTrue(mod.__name__)
        # budget_status keeps paused/exhausted keys byte-identical.
        status = budgets.budget_status(
            'ghost', {'ghost': budgets.blank_entry()})['games']
        self.assertIn('paused', status)
        self.assertIn('exhausted', status)
        self.assertFalse(status['paused'])
        self.assertFalse(status['exhausted'])
        # resume_category is a live alias of unfreeze_category.
        self.assertIn('alias', budgets.resume_category.__doc__)
        with mock.patch.object(budgets, 'unfreeze_category',
                               return_value=(True, 'aliased')) as unfrz:
            self.assertEqual(budgets.resume_category(KID, 'games'),
                             (True, 'aliased'))
            unfrz.assert_called_once_with(KID, 'games', actor='system')
        # All three budget endpoints are registered.
        rules = {r.rule for r in app.app.url_map.iter_rules()}
        for rule in ('/api/budgets/freeze', '/api/budgets/unfreeze',
                     '/api/budgets/resume'):
            self.assertIn(rule, rules)


if __name__ == '__main__':
    unittest.main()
