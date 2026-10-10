#!/usr/bin/env python3
"""Unit tests for run_lock_command ownership semantics (stdlib only).

Lock ownership (usermod -L) decides success, and every lock path
preserves the session: `run_lock_command('lock')` runs `usermod -L`
followed by a best-effort screen-lock (lock_user_sessions) and NEVER
calls `loginctl terminate-user` — session termination lives ONLY in
the /api/logout emergency-logout endpoint. A screen-lock failure for
an offline user is a note, not a failure — so bedtime bookkeeping
(locked_by_bedtime) is still recorded. All subprocess calls are mocked;
no real users are touched.
"""

import os
import sys
import unittest
from collections import namedtuple
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app

Result = namedtuple('Result', ['returncode', 'stdout', 'stderr'])


def _result(rc=0, stdout='', stderr=''):
    return Result(rc, stdout, stderr)


def _run_side_effect(results):
    """Build a subprocess.run replacement yielding the given results."""
    it = iter(results)

    def fake(*args, **kwargs):
        return next(it)

    return fake


GRAPHICAL = 'Type=wayland\nClass=user\n'


class TestLockOwnership(unittest.TestCase):
    def test_lock_offline_user_succeeds_with_note(self):
        """usermod ok + no active session -> success True, no terminate."""
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ['sudo', 'usermod', '-L']:
                return _result(0)
            if 'show-user' in cmd:
                return _result(0, stdout='')
            return _result(0)

        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            success, message = app.run_lock_command('bluefox', 'lock')
        self.assertTrue(success)
        self.assertIn('no active session', message)
        self.assertFalse([c for c in calls if 'terminate-user' in c])

    def test_lock_online_user_screen_locks_without_terminate(self):
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ['sudo', 'usermod', '-L']:
                return _result(0)
            if 'show-user' in cmd:
                return _result(0, stdout='21\n')
            if 'show-session' in cmd:
                return _result(0, stdout=GRAPHICAL)
            if 'lock-session' in cmd:
                return _result(0)
            return _result(0)

        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            success, message = app.run_lock_command('bluefox', 'lock')
        self.assertTrue(success)
        self.assertIn('session(s) locked', message)
        self.assertIn(['sudo', 'usermod', '-L', 'bluefox'], calls)
        self.assertTrue([c for c in calls if 'lock-session' in c])
        self.assertFalse([c for c in calls if 'terminate-user' in c])

    def test_lock_screen_lock_failure_still_succeeds(self):
        """usermod ok + screen-lock failure -> success True (best-effort)."""
        with mock.patch.object(
            app.subprocess, 'run',
            side_effect=_run_side_effect([
                _result(0),                       # usermod -L
                _result(1, stderr='no sessions'),  # show-user fails
            ]),
        ):
            success, message = app.run_lock_command('bluefox', 'lock')
        self.assertTrue(success)
        self.assertIn('no active session', message)

    def test_lock_never_terminates(self):
        """terminate-user must never appear in any lock call sequence."""
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            return _result(0, stdout='')

        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            success, _ = app.run_lock_command('bluefox', 'lock')
        self.assertTrue(success)
        for cmd in calls:
            self.assertNotIn('terminate-user', cmd)

    def test_lock_usermod_failure_fails(self):
        """usermod failure -> success False (screen lock never attempted)."""
        calls = []

        def fake(*args, **kwargs):
            calls.append(args[0])
            return _result(1, stderr='usermod: user does not exist')

        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            success, message = app.run_lock_command('ghost', 'lock')
        self.assertFalse(success)
        self.assertIn('usermod', message)
        self.assertEqual(len(calls), 1)  # screen lock not attempted

    def test_unlock_unchanged(self):
        with mock.patch.object(
            app.subprocess, 'run',
            side_effect=_run_side_effect([_result(0)]),
        ):
            success, message = app.run_lock_command('bluefox', 'unlock')
        self.assertTrue(success)
        self.assertEqual(message, 'OK')

    def test_unlock_never_terminates(self):
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            return _result(0)

        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            app.run_lock_command('bluefox', 'unlock')
        for cmd in calls:
            self.assertNotIn('terminate-user', cmd)

    def test_unknown_action(self):
        success, message = app.run_lock_command('bluefox', 'nuke')
        self.assertFalse(success)


if __name__ == '__main__':
    unittest.main()
