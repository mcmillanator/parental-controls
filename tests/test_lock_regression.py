#!/usr/bin/env python3
"""Regression tests for the core lock/unlock mechanisms.

These cover the two owner reports:
  1. "None of the buttons are working right now"
  2. "If I log out of a 'locked' account I can log back in without an
      issue."  <-- a Locked account must NOT be able to authenticate

Root cause of (2) was stale bedtime ownership: the manual-lock
endpoints cleared `manual_override` but left `locked_by_bedtime` set,
so when the bedtime window ended enforce_bedtime() ran `usermod -U` and
undid the parent's deliberate lock.

Design notes
------------
Standard library only, matching the existing tests/ style. All
subprocess work is mocked -- these tests never lock, unlock, terminate
or signal any real account, and never depend on a real user existing.

Where a real `usermod` is unavoidable for the shadow-semantics
questions, tests/test_lock_usermod_real.py drives a *disposable* test
user and skips itself when it cannot create one.
"""

import json
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import auth

Result = namedtuple('Result', ['returncode', 'stdout', 'stderr'])


def _result(rc=0, stdout='', stderr=''):
    return Result(rc, stdout, stderr)


def _run_side_effect(results):
    """Build a subprocess.run replacement yielding the given results."""
    it = iter(results)

    def fake(*args, **kwargs):
        return next(it)

    return fake


# A bedtime window that is NOT active at the times we inject, so
# enforce_bedtime() is deterministic.
WINDOW = {'start': '03:00', 'end': '04:00', 'days': [0, 1, 2, 3, 4, 5, 6]}
IN_WINDOW = datetime(2026, 10, 10, 3, 30)
OUT_WINDOW = datetime(2026, 10, 10, 12, 0)


class _AdminSessionMixin:
    """Log in a mocked wheel admin so HTTP tests pass @require_admin + CSRF.

    Mirrors tests/test_auth.py's harness: temp auth store, mocked wheel
    membership, real login over the test client. Argon2 hashing happens
    once per class (deliberately slow). Cooperative setUp/tearDown chain
    with mixins such as _BedtimeFileMixin via super().
    """

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

KID = 'kidaccount'


class _BedtimeFileMixin:
    """Point app.BEDTIME_FILE at a temp dir for the duration of a test.

    Bedtime state lives in a SQLite database next to BEDTIME_FILE, so
    the whole directory is temp (the db travels with the file). Helpers
    go through the app's load/save API: the JSON file itself is only a
    rollback mirror and is never read back.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self._bedtime = os.path.join(self._tmp.name, 'bedtime.json')
        patcher = mock.patch.object(app, 'BEDTIME_FILE', self._bedtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def write_bedtime(self, data):
        app.save_bedtime(data)

    def read_bedtime(self):
        return app.load_bedtime()


# ---------------------------------------------------------------------------
# usermod -L / -U shadow semantics
# ---------------------------------------------------------------------------
#
# These are the documented, observed semantics of shadow-utils (verified
# against a real account; see test_lock_usermod_real.py for the live
# equivalent). They are pinned here as fakes so the expectations the app
# relies on cannot drift silently if `usermod` is ever swapped out.

FAKE_HASH = '$y$j9T$PLACEHOLDER$HASH'          # placeholder, never a real hash


class FakeShadow:
    """A minimal in-memory model of one /etc/shadow password field.

    Models the observed shadow-utils behaviour:
      * `-L` prepends exactly one '!' and is idempotent (a second -L is
        a no-op, it does not produce '!!').
      * `-U` removes exactly one leading '!' and is a no-op when the
        field is not '!'-prefixed -- it never over-strips.
    """

    def __init__(self, field=FAKE_HASH):
        self.field = field

    def lock(self):
        if not self.field.startswith('!'):
            self.field = '!' + self.field
        return 0

    def unlock(self):
        if self.field.startswith('!'):
            self.field = self.field[1:]
        return 0

    @property
    def locked(self):
        return self.field.startswith('!')


class TestUsermodShadowSemantics(unittest.TestCase):
    """Pins the -L/-U contract the app's correctness depends on."""

    def test_lock_prepends_exactly_one_bang(self):
        s = FakeShadow()
        s.lock()
        self.assertEqual(s.field, '!' + FAKE_HASH)
        self.assertTrue(s.locked)

    def test_double_lock_is_idempotent(self):
        """A second -L must not produce '!!' (which -U would only half
        undo, leaving the account still locked)."""
        s = FakeShadow()
        s.lock()
        s.lock()
        self.assertEqual(s.field, '!' + FAKE_HASH)

    def test_triple_lock_is_idempotent(self):
        s = FakeShadow()
        for _ in range(3):
            s.lock()
        self.assertEqual(s.field, '!' + FAKE_HASH)

    def test_unlock_removes_exactly_one_bang(self):
        s = FakeShadow('!' + FAKE_HASH)
        s.unlock()
        self.assertEqual(s.field, FAKE_HASH)
        self.assertFalse(s.locked)

    def test_unlock_does_not_over_strip(self):
        """Unlocking an already-unlocked account must be a no-op and must
        not eat characters out of the hash itself."""
        s = FakeShadow()
        before = s.field
        s.unlock()
        s.unlock()
        s.unlock()
        self.assertEqual(s.field, before)
        self.assertEqual(s.field, FAKE_HASH)

    def test_double_unlock_then_relock_round_trips(self):
        s = FakeShadow()
        s.lock()
        s.unlock()
        s.unlock()
        s.lock()
        self.assertEqual(s.field, '!' + FAKE_HASH)

    def test_hash_never_corrupted_by_any_sequence(self):
        """Any lock/unlock sequence must leave the hash recoverable."""
        import itertools
        for seq in itertools.product('LU', repeat=6):
            s = FakeShadow()
            for op in seq:
                (s.lock if op == 'L' else s.unlock)()
            self.assertIn(FAKE_HASH.lstrip('!'), s.field, f'sequence {seq}')
            # and exactly zero or one leading '!'
            self.assertLessEqual(len(s.field) - len(s.field.lstrip('!')), 1,
                                 f'sequence {seq}')


# ---------------------------------------------------------------------------
# run_lock_command contract
# ---------------------------------------------------------------------------

class TestRunLockCommandContract(unittest.TestCase):
    GRAPHICAL = 'Type=wayland\nClass=user\n'

    def _fake_full_stack(self, calls, sessions=''):
        """Fake subprocess.run for the whole lock path: usermod -L,
        show-user -> sessions, show-session -> graphical, lock-session ok."""
        def fake(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ['sudo', 'usermod', '-L']:
                return _result(0)
            if 'show-user' in cmd:
                return _result(0, stdout=sessions)
            if 'show-session' in cmd:
                return _result(0, stdout=self.GRAPHICAL)
            return _result(0)
        return fake

    def test_lock_preserves_session_never_terminates(self):
        """Lock = usermod -L + screen-lock; terminate-user never runs."""
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=self._fake_full_stack(
                                   calls, sessions='21\n')):
            success, message = app.run_lock_command(KID, 'lock')
        self.assertTrue(success)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        self.assertTrue([c for c in calls if 'lock-session' in c])
        self.assertFalse([c for c in calls if 'terminate-user' in c])

    def test_lock_offline_user_still_succeeds(self):
        """Offline user: usermod succeeds, no session to screen-lock.
        Lock ownership is usermod, so this must still be success."""
        calls = []
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=self._fake_full_stack(calls)):
            success, message = app.run_lock_command(KID, 'lock')
        self.assertTrue(success)
        self.assertIn('no active session', message)
        self.assertFalse([c for c in calls if 'terminate-user' in c])

    def test_lock_screen_lock_failure_still_succeeds(self):
        with mock.patch.object(
            app.subprocess, 'run',
            side_effect=_run_side_effect([
                _result(0), _result(1, stderr='no sessions'),
            ]),
        ):
            success, message = app.run_lock_command(KID, 'lock')
        self.assertTrue(success)
        self.assertIn('no active session', message)

    def test_lock_usermod_failure_fails_and_skips_terminate(self):
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            return _result(1, stderr='user does not exist')

        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            success, message = app.run_lock_command('ghost', 'lock')
        self.assertFalse(success)
        self.assertIn('usermod', message)
        self.assertEqual(len(calls), 1)

    def test_lock_usermod_timeout_fails(self):
        with mock.patch.object(
            app.subprocess, 'run',
            side_effect=app.subprocess.TimeoutExpired('usermod', 15),
        ):
            success, message = app.run_lock_command(KID, 'lock')
        self.assertFalse(success)
        self.assertIn('timed out', message)

    def test_unlock_success(self):
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=_run_side_effect([_result(0)])):
            success, message = app.run_lock_command(KID, 'unlock')
        self.assertTrue(success)
        self.assertEqual(message, 'OK')

    def test_unlock_runs_exactly_one_usermod_u(self):
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            return _result(0)

        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            app.run_lock_command(KID, 'unlock')
        self.assertEqual(len(calls), 1)
        self.assertIn('-U', calls[0])

    def test_unknown_action_rejected(self):
        success, message = app.run_lock_command(KID, 'nuke')
        self.assertFalse(success)
        self.assertIn('Unknown action', message)

    def test_lock_and_unlock_use_sudo(self):
        """All lock/unlock subprocess calls must go through sudo."""
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            if 'show-user' in cmd:
                return _result(0, stdout='')
            return _result(0)

        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            app.run_lock_command(KID, 'lock')
        self.assertTrue(calls)
        self.assertEqual(calls[0][:2], ['sudo', 'usermod'])
        self.assertIn('-L', calls[0])
        for cmd in calls:
            self.assertEqual(cmd[0], 'sudo')
        calls.clear()
        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            app.run_lock_command(KID, 'unlock')
        self.assertEqual(calls[0][0], 'sudo')
        self.assertIn('-U', calls[0])


# ---------------------------------------------------------------------------
# Lock status read-back
# ---------------------------------------------------------------------------

class TestLockStatusReadBack(unittest.TestCase):
    def _status(self, stdout, rc=0):
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_result(rc, stdout=stdout)):
            return app.get_lock_status(KID)

    def test_locked_reports_locked(self):
        self.assertEqual(self._status(f'{KID} L 2026-10-06 0 99999 7 -1\n'),
                         (True, 'Locked'))

    def test_unlocked_reports_unlocked(self):
        self.assertEqual(self._status(f'{KID} P 2026-10-06 0 99999 7 -1\n'),
                         (False, 'Unlocked'))

    def test_no_password_reports_locked(self):
        self.assertEqual(self._status(f'{KID} NP 2026-10-06 0 99999 7 -1\n'),
                         (True, 'No password'))

    def test_unknown_status_field_is_not_silently_unlocked(self):
        """An unrecognised status must NOT be reported as unlocked --
        that would show the parent a green 'Unlocked' for a locked
        account."""
        locked, detail = self._status(f'{KID} LK 2026-10-06\n')
        self.assertIsNone(locked)
        self.assertIn('Unknown status', detail)

    def test_command_failure_is_not_silently_unlocked(self):
        locked, detail = self._status('', rc=1)
        self.assertIsNone(locked)
        self.assertTrue(detail)

    def test_timeout_is_not_silently_unlocked(self):
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=app.subprocess.TimeoutExpired('x', 10)):
            locked, _ = app.get_lock_status(KID)
        self.assertIsNone(locked)

    def test_truncated_output_is_not_silently_unlocked(self):
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_result(0, stdout=KID)):
            locked, detail = app.get_lock_status(KID)
        self.assertIsNone(locked)
        self.assertIn('Unexpected', detail)

    def test_status_matches_reality_after_each_operation(self):
        """Lock -> read-back Locked; Unlock -> read-back Unlocked."""
        shadow = FakeShadow()
        responses = {}

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ['sudo', 'usermod', '-L']:
                shadow.lock()
            elif cmd[:3] == ['sudo', 'usermod', '-U']:
                shadow.unlock()
            elif cmd[:2] == ['sudo', 'passwd']:
                status = 'L' if shadow.locked else 'P'
                return _result(0, stdout=f'{KID} {status} 2026-10-06\n')
            elif 'show-user' in cmd:
                return _result(0, stdout='')
            elif 'terminate-user' in cmd:
                raise AssertionError(
                    'terminate-user must never run on a lock path')
            elif 'lock-session' in cmd or 'show-session' in cmd:
                return _result(0, stdout='')
            return _result(0)

        with mock.patch.object(app.subprocess, 'run', side_effect=fake_run):
            app.run_lock_command(KID, 'lock')
            self.assertEqual(app.get_lock_status(KID)[0], True)
            app.run_lock_command(KID, 'unlock')
            self.assertEqual(app.get_lock_status(KID)[0], False)


# ---------------------------------------------------------------------------
# THE regression: manual lock must survive the bedtime timer
# ---------------------------------------------------------------------------

class TestManualLockOwnership(_AdminSessionMixin, _BedtimeFileMixin,
                              unittest.TestCase):
    """A manual lock transfers ownership away from bedtime.

    Before the fix these failed: the manual-lock endpoints left
    `locked_by_bedtime` set, so enforce_bedtime() unlocked the account
    at the end of the window -- and a Locked account could authenticate
    again.
    """

    def _bedtime_locked_entry(self):
        return {KID: dict(WINDOW, locked_by_bedtime=True, manual_override=False)}

    def test_note_manual_lock_clears_bedtime_ownership(self):
        self.write_bedtime(self._bedtime_locked_entry())
        app.note_manual_lock(KID)
        self.assertFalse(self.read_bedtime()[KID]['locked_by_bedtime'])

    def test_note_manual_lock_clears_manual_override(self):
        self.write_bedtime({KID: dict(WINDOW, locked_by_bedtime=False,
                                      manual_override=True)})
        app.note_manual_lock(KID)
        self.assertFalse(self.read_bedtime()[KID]['manual_override'])

    def test_note_manual_lock_clears_both_flags(self):
        self.write_bedtime({KID: dict(WINDOW, locked_by_bedtime=True,
                                      manual_override=True)})
        app.note_manual_lock(KID)
        entry = self.read_bedtime()[KID]
        self.assertFalse(entry['locked_by_bedtime'])
        self.assertFalse(entry['manual_override'])

    def test_manual_lock_survives_bedtime_window_ending(self):
        """THE regression test for the owner's critical report."""
        self.write_bedtime(self._bedtime_locked_entry())
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock_cmd:
            app.enforce_bedtime(now=IN_WINDOW)
            app.note_manual_lock(KID)      # parent locks manually too
            app.enforce_bedtime(now=OUT_WINDOW)   # window ends
        unlocked = [c.args for c in lock_cmd.call_args_list
                    if len(c.args) > 1 and c.args[1] == 'unlock']
        self.assertEqual(unlocked, [],
                         'bedtime timer unlocked an account the parent '
                         'had manually locked')

    def test_manual_lock_survives_schedule_being_cleared(self):
        """Clearing the bedtime schedule must not unlock a manual lock."""
        self.write_bedtime(self._bedtime_locked_entry())
        app.note_manual_lock(KID)
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock_cmd:
            resp = self.authed_post('/api/bedtime',
                                    data={'username': KID, 'clear': '1'})
        self.assertEqual(resp.status_code, 200)
        unlocked = [c.args for c in lock_cmd.call_args_list
                    if len(c.args) > 1 and c.args[1] == 'unlock']
        self.assertEqual(unlocked, [])

    def test_bedtime_still_unlocks_what_it_locked(self):
        """The fix must not break bedtime's own unlock: if bedtime did
        the locking and nobody manually intervened, it still unlocks."""
        self.write_bedtime({KID: dict(WINDOW, locked_by_bedtime=True,
                                      manual_override=False)})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock_cmd:
            app.enforce_bedtime(now=OUT_WINDOW)
        actions = [c.args[1] for c in lock_cmd.call_args_list if len(c.args) > 1]
        self.assertIn('unlock', actions)
        self.assertFalse(self.read_bedtime()[KID]['locked_by_bedtime'])

    def test_stale_snapshot_does_not_unlock(self):
        """The timer runs in a separate process from the web UI, so a
        manual lock can land between snapshot and unlock. The unlock
        must re-read ownership from disk rather than trust the snapshot.
        """
        self.write_bedtime(self._bedtime_locked_entry())
        calls = {'n': 0}

        def load():
            calls['n'] += 1
            if calls['n'] == 1:
                # Snapshot taken before the parent's manual lock.
                return {KID: dict(WINDOW, locked_by_bedtime=True,
                                  manual_override=False)}
            # By re-read time the manual lock has cleared ownership.
            return {KID: dict(WINDOW, locked_by_bedtime=False,
                              manual_override=False)}

        with mock.patch.object(app, 'load_bedtime', side_effect=load), \
             mock.patch.object(app, 'save_bedtime'), \
             mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock_cmd, \
             mock.patch.object(app.budgets, 'is_admin_protected',
                               return_value=False):
            app.enforce_bedtime(now=OUT_WINDOW)
        unlocked = [c.args for c in lock_cmd.call_args_list
                    if len(c.args) > 1 and c.args[1] == 'unlock']
        self.assertEqual(unlocked, [],
                         'unlocked from a stale in-memory snapshot')

    def test_note_manual_lock_unknown_user_is_safe(self):
        self.write_bedtime({})
        app.note_manual_lock('ghost')  # must not raise
        app.note_manual_lock('')


# ---------------------------------------------------------------------------
# NEVER-pause / admin safeguards are not bypassed by the lock paths
# ---------------------------------------------------------------------------

class TestSafeguardsNotBypassedByLockPaths(_BedtimeFileMixin, unittest.TestCase):
    def test_bedtime_never_locks_wheel_admin(self):
        bedtime = {
            'wheeladmin': dict(WINDOW, locked_by_bedtime=False,
                               manual_override=False),
            KID: dict(WINDOW, locked_by_bedtime=False, manual_override=False),
        }
        with mock.patch.object(app, 'load_bedtime',
                               return_value={k: dict(v) for k, v in
                                             bedtime.items()}), \
             mock.patch.object(app, 'save_bedtime'), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock, \
             mock.patch.object(app.budgets, 'is_admin_protected',
                               side_effect=lambda u: u == 'wheeladmin'):
            app.enforce_bedtime(now=IN_WINDOW)
        locked = [c.args[0] for c in lock.call_args_list if len(c.args) > 1]
        self.assertNotIn('wheeladmin', locked)
        self.assertIn(KID, locked)

    def test_bedtime_auto_lock_keeps_its_ownership(self):
        """A bedtime lock must SET locked_by_bedtime -- otherwise it
        would never auto-unlock at the end of the window."""
        self.write_bedtime({KID: dict(WINDOW, locked_by_bedtime=False,
                                      manual_override=False)})
        with mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')), \
             mock.patch.object(app, '_safe_notify'), \
             mock.patch.object(app.budgets, 'is_admin_protected',
                               return_value=False):
            app.enforce_bedtime(now=IN_WINDOW)
        self.assertTrue(self.read_bedtime()[KID]['locked_by_bedtime'])

    def test_manual_lock_paths_do_not_freeze_processes(self):
        """Locking an account must never invoke the cgroup freezer --
        the retired /opt/parental app froze whole desktops that way."""
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')), \
             mock.patch.object(app.budgets, 'account_and_enforce') as freeze:
            app.run_lock_command(KID, 'lock')
        freeze.assert_not_called()

    def test_lock_endpoints_do_not_touch_budgets(self):
        self.write_bedtime({})
        client = app.app.test_client()
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')), \
             mock.patch.object(app.subprocess, 'run',
                               return_value=_result(0, stdout='')), \
             mock.patch.object(app.budgets, 'tick_budgets') as tick:
            for path in ('/api/lock', '/api/lock-screen'):
                client.post(path, data={'username': KID})
        tick.assert_not_called()


# ---------------------------------------------------------------------------
# Screen lock targets the right session
# ---------------------------------------------------------------------------

class TestLockUserSessions(unittest.TestCase):
    """`loginctl lock-session` with no ID locks the CALLER's session.

    The app used to call it with no ID, so Lock/Lock Screen locked the
    parent's own screen and left the child alone.
    """

    GRAPHICAL = 'Type=wayland\nClass=user\n'
    MANAGER = 'Type=unspecified\nClass=manager\n'
    TTY_USER = 'Type=tty\nClass=user\n'

    def _fake_ps(self, sessions, props_by_id=None, calls=None):
        """Fake subprocess.run: show-user -> sessions; show-session ->
        per-ID props; everything else records and succeeds."""
        props_by_id = props_by_id or {}

        def fake_run(cmd, **kwargs):
            if calls is not None:
                calls.append(cmd)
            if 'show-user' in cmd:
                return _result(0, stdout=sessions)
            if 'show-session' in cmd:
                sid = cmd[cmd.index('show-session') + 1]
                return _result(0, stdout=props_by_id.get(sid, ''))
            return _result(0)

        return fake_run

    def test_resolves_target_sessions_first(self):
        calls = []
        fake = self._fake_ps('21 22\n', {'21': self.GRAPHICAL,
                                         '22': self.GRAPHICAL}, calls)
        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            msg = app.lock_user_sessions(KID)
        self.assertIn('show-user', calls[0])
        self.assertEqual(calls[0][:2], ['sudo', 'loginctl'])
        lock_call = calls[-1]
        self.assertIn('lock-session', lock_call)
        self.assertIn('21', lock_call)
        self.assertIn('22', lock_call)
        self.assertIn('2 session', msg)

    def test_never_calls_bare_lock_session(self):
        """Regression guard: a bare 'lock-session' locks the caller."""
        calls = []
        fake = self._fake_ps('21\n', {'21': self.GRAPHICAL}, calls)
        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            app.lock_user_sessions(KID)
        for cmd in calls:
            if 'lock-session' in cmd:
                self.assertGreater(len(cmd), 3,
                                   'lock-session called without a session id')

    def test_manager_sessions_are_skipped(self):
        """Manager/greeter sessions reject lock-session ('does not support
        lock screen') — they must never reach the lock call."""
        calls = []
        fake = self._fake_ps('31 30\n', {'31': self.MANAGER,
                                         '30': self.MANAGER}, calls)
        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            msg = app.lock_user_sessions(KID)
        self.assertIn('no lockable', msg)
        self.assertFalse(any('lock-session' in cmd for cmd in calls))

    def test_tty_user_sessions_are_skipped(self):
        calls = []
        fake = self._fake_ps('30\n', {'30': self.TTY_USER}, calls)
        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            msg = app.lock_user_sessions(KID)
        self.assertIn('no lockable', msg)
        self.assertFalse(any('lock-session' in cmd for cmd in calls))

    def test_mixed_sessions_lock_only_graphical(self):
        """Manager + graphical: only the graphical ID is locked."""
        calls = []
        fake = self._fake_ps('31 30\n', {'31': self.MANAGER,
                                         '30': self.GRAPHICAL}, calls)
        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            msg = app.lock_user_sessions(KID)
        lock_calls = [cmd for cmd in calls if 'lock-session' in cmd]
        self.assertEqual(len(lock_calls), 1)
        self.assertIn('30', lock_calls[0])
        self.assertNotIn('31', lock_calls[0])
        self.assertIn('1 session', msg)

    def test_vanished_session_is_skipped(self):
        """A session that disappears mid-loop (show-session fails) is
        skipped, not fatal."""
        calls = []
        fake = self._fake_ps('30\n', {}, calls)  # props lookup misses
        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            msg = app.lock_user_sessions(KID)
        self.assertIn('no lockable', msg)

    def test_unparsable_props_are_skipped(self):
        calls = []
        fake = self._fake_ps('30\n', {'30': 'garbage without equals'},
                             calls)
        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            msg = app.lock_user_sessions(KID)
        self.assertIn('no lockable', msg)
        self.assertFalse(any('lock-session' in cmd for cmd in calls))

    def test_no_active_session_is_reported_not_fatal(self):
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_result(0, stdout='')):
            self.assertIn('no active session',
                          app.lock_user_sessions(KID))

    def test_show_user_failure_is_not_fatal(self):
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_result(1, stderr='nope')):
            msg = app.lock_user_sessions(KID)
        self.assertIn('no active session', msg)

    def test_never_raises(self):
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=Exception('boom')):
            self.assertTrue(app.lock_user_sessions(KID))
        with mock.patch.object(
            app.subprocess, 'run',
            side_effect=app.subprocess.TimeoutExpired('x', 15)):
            self.assertTrue(app.lock_user_sessions(KID))


# ---------------------------------------------------------------------------
# The lock endpoints call usermod with the right flags and username
# ---------------------------------------------------------------------------

class TestLockEndpoints(_AdminSessionMixin, unittest.TestCase):
    def _post(self, path, data=None, **patches):
        defaults = {
            'run_lock_command': mock.DEFAULT,
            'note_manual_lock': mock.DEFAULT,
            'clear_manual_override': mock.DEFAULT,
            'subprocess.run': mock.DEFAULT,
        }
        defaults.update(patches)
        return self.authed_post(path, data=data or {'username': KID})

    def test_api_lock_locks_the_account(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _result(0, stdout='')

        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run', side_effect=fake_run):
            resp = self.authed_post('/api/lock',
                                      data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        # must NOT terminate the session (only /api/logout terminates)
        self.assertFalse([c for c in calls if 'terminate-user' in c])

    def test_api_lock_screen_locks_the_account(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _result(0, stdout='')

        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run', side_effect=fake_run):
            resp = self.authed_post('/api/lock-screen',
                                              data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        # screen-lock endpoint must also never terminate the session
        self.assertFalse([c for c in calls if 'terminate-user' in c])

    def test_api_logout_locks_and_terminates(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _result(0)

        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run', side_effect=fake_run):
            resp = self.authed_post('/api/logout',
                                              data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        self.assertTrue([c for c in calls if 'terminate-user' in c])

    def test_api_unlock_unlocks(self):
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock, \
             mock.patch.object(app, 'note_manual_unlock'):
            resp = self.authed_post('/api/unlock',
                                              data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertEqual(lock.call_args[0], (KID, 'unlock'))

    def test_all_lock_endpoints_call_note_manual_lock(self):
        """Every manual lock path must take ownership from bedtime."""
        for path in ('/api/lock', '/api/lock-screen', '/api/logout'):
            with self.subTest(path=path), \
                 mock.patch.object(app, 'note_manual_lock') as nml, \
                 mock.patch.object(app, 'run_lock_command',
                                   return_value=(True, 'OK')), \
                 mock.patch.object(app.subprocess, 'run',
                                   return_value=_result(0, stdout='')):
                self.authed_post(path, data={'username': KID})
            nml.assert_called_once_with(KID)

    def test_lock_fails_loudly_when_usermod_fails(self):
        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run',
                               return_value=_result(1,
                                                    stderr='usermod: error')):
            resp = self.authed_post('/api/lock',
                                              data={'username': KID})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['success'])

    def test_lock_fails_loudly_when_usermod_times_out(self):
        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run',
                               side_effect=app.subprocess.TimeoutExpired('x', 15)):
            resp = self.authed_post('/api/lock',
                                              data={'username': KID})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['success'])

    def test_no_bookkeeping_when_usermod_fails(self):
        """Ownership must only transfer on a *successful* lock."""
        with mock.patch.object(app, 'note_manual_lock') as nml, \
             mock.patch.object(app.subprocess, 'run',
                               return_value=_result(1, stderr='error')):
            self.authed_post('/api/lock', data={'username': KID})
        nml.assert_not_called()

    def test_invalid_username_rejected(self):
        # Mocked so a bug here can never run a real root command.
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_result(0, stdout='')), \
             mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')), \
             mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app, 'note_manual_unlock'):
            for bad in ('bad;user', 'a b', '-x', '--lock', 'u$', '', '-', 'a/b'):
                with self.subTest(username=bad):
                    for path in ('/api/lock', '/api/lock-screen',
                                 '/api/unlock', '/api/logout'):
                        resp = self.authed_post(
                            path, data={'username': bad})
                        self.assertEqual(resp.status_code, 400, path)

    def test_leading_dash_never_reaches_usermod(self):
        """Argument-injection guard: '-x' would be parsed by usermod as
        an option, not a login. No real account can start with '-'."""
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_result(0, stdout='')) as run:
            for path in ('/api/lock', '/api/lock-screen', '/api/unlock',
                         '/api/logout'):
                self.authed_post(path, data={'username': '-U'})
        for call in run.call_args_list:
            self.assertNotIn('-U', call.args[0][1:],
                             f'leading-dash name reached {call.args[0]}')

    def test_valid_username_helper(self):
        for good in ('bluefox', 'kid_1', 'a.b', 'tester2', 'x'):
            self.assertTrue(app.valid_username(good), good)
        for bad in ('', '-', '-x', '--lock', 'a b', 'a/b', 'a;b', 'a$b',
                    None, 'a\nb'):
            self.assertFalse(app.valid_username(bad), bad)

    def test_username_is_never_interpolated_into_a_shell(self):
        """Arguments must stay in the argv list (no shell=True)."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return _result(0, stdout='')

        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run', side_effect=fake_run):
            self.authed_post('/api/lock', data={'username': KID})
        for cmd, kwargs in calls:
            self.assertFalse(kwargs.get('shell', False))
            self.assertIsInstance(cmd, list)


if __name__ == '__main__':
    unittest.main()