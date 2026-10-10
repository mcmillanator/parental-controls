#!/usr/bin/env python3
"""Unit tests for run_lock_command ownership semantics (stdlib only).

Lock ownership (usermod -L) decides success; a terminate-user failure for
an offline user is a warning note, not a failure — so bedtime bookkeeping
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

Result = namedtuple('Result', ['returncode', 'stderr'])


def _run_side_effect(results):
    """Build a subprocess.run replacement yielding the given results."""
    it = iter(results)

    def fake(*args, **kwargs):
        return next(it)

    return fake


class TestLockOwnership(unittest.TestCase):
    def test_lock_offline_user_succeeds_with_note(self):
        """usermod ok + terminate fails (not logged in) -> success True."""
        with mock.patch.object(
            app.subprocess, 'run',
            side_effect=_run_side_effect([
                Result(0, ''),
                Result(1, 'Could not terminate user: not logged in'),
            ]),
        ):
            success, message = app.run_lock_command('bluefox', 'lock')
        self.assertTrue(success)
        self.assertIn('terminate', message)

    def test_lock_online_user_clean_success(self):
        with mock.patch.object(
            app.subprocess, 'run',
            side_effect=_run_side_effect([Result(0, ''), Result(0, '')]),
        ):
            success, message = app.run_lock_command('bluefox', 'lock')
        self.assertTrue(success)
        self.assertEqual(message, 'OK')

    def test_lock_usermod_failure_fails(self):
        """usermod failure -> success False (terminate never attempted)."""
        calls = []

        def fake(*args, **kwargs):
            calls.append(args[0])
            return Result(1, 'usermod: user does not exist')

        with mock.patch.object(app.subprocess, 'run', side_effect=fake):
            success, message = app.run_lock_command('ghost', 'lock')
        self.assertFalse(success)
        self.assertIn('usermod', message)
        self.assertEqual(len(calls), 1)  # terminate not attempted

    def test_unlock_unchanged(self):
        with mock.patch.object(
            app.subprocess, 'run',
            side_effect=_run_side_effect([Result(0, '')]),
        ):
            success, message = app.run_lock_command('bluefox', 'unlock')
        self.assertTrue(success)
        self.assertEqual(message, 'OK')

    def test_unknown_action(self):
        success, message = app.run_lock_command('bluefox', 'nuke')
        self.assertFalse(success)


if __name__ == '__main__':
    unittest.main()
