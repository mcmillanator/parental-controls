#!/usr/bin/env python3
"""Lock-must-preserve-session regression pins (mocked only).

Owner's report: pressing Lock logged the child out (session destroyed)
instead of locking the screen. Root cause: `run_lock_command('lock')`
ran `loginctl terminate-user`, and it is called from three
bedtime/bonus paths (enforce_bedtime auto-lock, api_bonus_revoke
re-lock, api_bedtime_set save-enforce) — so even the manual Lock button
paths that avoided terminate directly were re-locked with terminate by
the timer.

Contract pinned here: NO lock path may invoke `terminate-user`.
Session termination lives ONLY in /api/logout (emergency logout, by
design). Every test records the full subprocess argv list and asserts
`terminate-user` is absent (plus a positive control that /api/logout
still terminates).

All subprocess work is mocked — these tests never lock, unlock,
terminate or signal any real account.
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

import app
import auth

Result = namedtuple('Result', ['returncode', 'stdout', 'stderr'])


def _result(rc=0, stdout='', stderr=''):
    return Result(rc, stdout, stderr)


KID = 'kidaccount'

# A bedtime window that is NOT active at the times we inject, so
# enforce_bedtime() is deterministic.
WINDOW = {'start': '03:00', 'end': '04:00', 'days': [0, 1, 2, 3, 4, 5, 6]}
IN_WINDOW = datetime(2026, 10, 10, 3, 30)

GRAPHICAL = 'Type=wayland\nClass=user\n'


def subprocess_recorder(calls, sessions='21\n'):
    """Fake subprocess.run for the whole lock path.

    Appends every argv list to `calls`. usermod always succeeds;
    show-user reports `sessions`; show-session reports a graphical user
    session; everything else succeeds.
    """
    def fake(cmd, **kwargs):
        calls.append(cmd)
        if 'show-user' in cmd:
            return _result(0, stdout=sessions)
        if 'show-session' in cmd:
            return _result(0, stdout=GRAPHICAL)
        return _result(0)
    return fake


def assert_no_terminate(testcase, calls, where):
    bad = [c for c in calls if 'terminate-user' in c]
    testcase.assertEqual(
        bad, [], f'{where} invoked loginctl terminate-user: {bad}')


class _AdminSessionMixin:
    """Log in a mocked wheel admin so HTTP tests pass @require_admin + CSRF."""

    ADMIN_USER = 'qaadmin'
    ADMIN_PW = 'Qa-Admin-Test-Pass-123'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._admin_hash = auth.hash_password(cls.ADMIN_PW)

    def setUp(self):
        super().setUp()
        self._auth_tmp = tempfile.TemporaryDirectory()
        self._auth_path = os.path.join(self._auth_tmp.name, 'auth.json')
        with open(self._auth_path, 'w') as f:
            json.dump({'users': {self.ADMIN_USER: {
                'hash': self._admin_hash, 'must_change': False,
                'locked': False}}}, f)
        os.chmod(self._auth_path, 0o600)
        os.environ['PARENTAL_CONTROLS_AUTH'] = self._auth_path
        auth.clear_sessions()
        auth.clear_login_failures()
        app.app.config['TESTING'] = True
        patcher_w = mock.patch.object(auth, 'is_wheel_member',
                                      side_effect=lambda u: u == self.ADMIN_USER)
        patcher_s = mock.patch.object(auth, 'is_system_user',
                                      side_effect=lambda u: u == self.ADMIN_USER)
        self.addCleanup(patcher_w.stop)
        self.addCleanup(patcher_s.stop)
        patcher_w.start()
        patcher_s.start()
        self.client = app.app.test_client()
        resp = self.client.post('/login', data={'username': self.ADMIN_USER,
                                                'password': self.ADMIN_PW})
        assert resp.status_code == 302, \
            f'setup login failed: {resp.status_code}'
        sid = resp.headers.get('Set-Cookie', '').split('pc_sid=')[1].split(';')[0]
        self.csrf_headers = {'X-CSRF-Token': auth._sessions[sid]['csrf']}

    def tearDown(self):
        os.environ.pop('PARENTAL_CONTROLS_AUTH', None)
        auth.clear_sessions()
        auth.clear_login_failures()
        self._auth_tmp.cleanup()
        super().tearDown()

    def authed_post(self, path, data=None):
        """POST with session cookie + CSRF, like the real UI does."""
        return self.client.post(path, data=data or {},
                                headers=self.csrf_headers)


class _BedtimeFileMixin:
    """Point app.BEDTIME_FILE at a temp file for the duration of a test."""

    def setUp(self):
        super().setUp()
        self._bedtime = tempfile.mktemp(suffix='.json')
        patcher = mock.patch.object(app, 'BEDTIME_FILE', self._bedtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._remove_bedtime)

    def _remove_bedtime(self):
        try:
            os.unlink(self._bedtime)
        except OSError:
            pass

    def write_bedtime(self, data):
        with open(self._bedtime, 'w') as f:
            json.dump(data, f)

    def read_bedtime(self):
        with open(self._bedtime) as f:
            return json.load(f)


class TestLockNeverTerminates(_AdminSessionMixin, _BedtimeFileMixin,
                              unittest.TestCase):
    """Every lock path preserves the session; only /api/logout ends it."""

    def test_run_lock_command_never_terminates(self):
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=subprocess_recorder(calls)):
            success, _ = app.run_lock_command(KID, 'lock')
        self.assertTrue(success)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        assert_no_terminate(self, calls, 'run_lock_command(lock)')

    def test_api_lock_never_terminates(self):
        self.write_bedtime({})
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=subprocess_recorder(calls)):
            resp = self.authed_post('/api/lock', data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        assert_no_terminate(self, calls, '/api/lock')

    def test_api_lock_screen_never_terminates(self):
        self.write_bedtime({})
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=subprocess_recorder(calls)):
            resp = self.authed_post('/api/lock-screen',
                                    data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        assert_no_terminate(self, calls, '/api/lock-screen')

    def test_enforce_bedtime_auto_lock_never_terminates(self):
        self.write_bedtime({KID: dict(WINDOW, locked_by_bedtime=False,
                                      manual_override=False)})
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=subprocess_recorder(calls)), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, '_safe_notify'), \
             mock.patch.object(app.budgets, 'is_admin_protected',
                               return_value=False):
            app.enforce_bedtime(now=IN_WINDOW)
        self.assertTrue(self.read_bedtime()[KID]['locked_by_bedtime'])
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        assert_no_terminate(self, calls, 'enforce_bedtime auto-lock')

    def test_bedtime_save_enforce_never_terminates(self):
        """Saving an in-window schedule auto-locks without terminating."""
        self.write_bedtime({})
        now = datetime.now()
        start = (now - timedelta(minutes=60)).strftime('%H:%M')
        end = (now + timedelta(minutes=60)).strftime('%H:%M')
        days = [0, 1, 2, 3, 4, 5, 6]
        form = {'username': KID, 'start': start, 'end': end,
                'all_days': 'on'}
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=subprocess_recorder(
                                   calls, sessions='')), \
             mock.patch.object(app, '_safe_notify'):
            resp = self.authed_post('/api/bedtime', data=form)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['locked'])
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        assert_no_terminate(self, calls, 'bedtime-save enforce')

    def test_bonus_grant_never_terminates(self):
        """A bonus grant unlocks (usermod -U only), never terminates."""
        self.write_bedtime({KID: dict(WINDOW, locked_by_bedtime=False,
                                      manual_override=False)})
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=subprocess_recorder(calls)), \
             mock.patch.object(app, '_safe_notify'):
            resp = self.authed_post('/api/bonus',
                                    data={'username': KID, 'minutes': '30'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(['sudo', 'usermod', '-U', KID], calls)
        assert_no_terminate(self, calls, 'bonus grant')

    def test_bonus_revoke_relock_never_terminates(self):
        """Revoking a bonus re-locks in-window without terminating."""
        now = datetime.now()
        start = (now - timedelta(minutes=60)).strftime('%H:%M')
        end = (now + timedelta(minutes=60)).strftime('%H:%M')
        until = (now + timedelta(minutes=30)).isoformat()
        self.write_bedtime({KID: {
            'start': start, 'end': end, 'days': [0, 1, 2, 3, 4, 5, 6],
            'locked_by_bedtime': False, 'manual_override': False,
            'bonus_until': until, 'bonus_minutes': 30,
            'bonus_granted_by': 'qaadmin',
            'bonus_granted_at': now.isoformat()}})
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=subprocess_recorder(calls)), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, '_safe_notify'):
            resp = self.authed_post('/api/bonus/revoke',
                                    data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        assert_no_terminate(self, calls, 'bonus revoke re-lock')

    def test_bonus_expiry_relock_never_terminates(self):
        """An expired bonus re-locks on the next enforce pass, in-session."""
        until = (IN_WINDOW - timedelta(minutes=1)).isoformat()
        self.write_bedtime({KID: dict(
            WINDOW, locked_by_bedtime=False, manual_override=False,
            bonus_until=until, bonus_minutes=30,
            bonus_granted_by='qaadmin',
            bonus_granted_at=(IN_WINDOW - timedelta(
                minutes=31)).isoformat())})
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=subprocess_recorder(calls)), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, '_safe_notify'), \
             mock.patch.object(app.budgets, 'is_admin_protected',
                               return_value=False):
            app.enforce_bedtime(now=IN_WINDOW)
        entry = self.read_bedtime()[KID]
        self.assertTrue(entry['locked_by_bedtime'])
        self.assertNotIn('bonus_until', entry)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        assert_no_terminate(self, calls, 'bonus expiry re-lock')

    def test_api_logout_still_terminates(self):
        """Positive control: emergency logout is the ONE path allowed to
        terminate the session."""
        self.write_bedtime({})
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=subprocess_recorder(calls)):
            resp = self.authed_post('/api/logout', data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        self.assertTrue([c for c in calls if 'terminate-user' in c],
                        '/api/logout must still terminate the session')


if __name__ == '__main__':
    unittest.main()
