#!/usr/bin/env python3
"""Unit tests for pre-lock warnings (bedtime + budget notifications).

Scheduling/warned-state logic runs against fake times with mocked lock
and notify functions. Nothing here locks, freezes, or notifies tester.

The only live-system test uses bluefox (which has no active session) to
verify notify_user fails silently — and it skips itself if bluefox ever
grows a session.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import budgets
from budgets import account_and_enforce, blank_entry


def next_monday():
    """A Monday datetime (2026-10-12 is a Monday; computed, not assumed)."""
    base = datetime(2026, 10, 12)
    return base - timedelta(days=base.weekday())


MONDAY = next_monday()
assert MONDAY.weekday() == 0


def bluefox_has_session():
    """True if bluefox currently owns a logind session (then skip live test)."""
    try:
        result = subprocess.run(
            ['loginctl', 'list-sessions', '--no-legend'],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    return 'bluefox' in result.stdout


class Recorder:
    """Callable notify mock recording (username, title, body) tuples."""

    def __init__(self):
        self.calls = []

    def __call__(self, username, title, body):
        self.calls.append((username, title, body))
        return True

    def titles(self):
        return [t for _, t, _ in self.calls]


class TestNotifyUser(unittest.TestCase):
    def test_command_shape(self):
        """notify-send is delivered via sudo systemd-run --machine=<user>@.host.

        The sudo prefix is load-bearing: without it the service user gets
        "Permission denied" on the system bus and every notification
        silently fails (sudoers authorizes the sudo'd command only).
        """
        seen = {}

        def fake_run(argv, **kwargs):
            seen['argv'] = argv

            class R:
                returncode = 0
                stderr = ''
            return R()

        with mock.patch.object(app.subprocess, 'run', fake_run):
            self.assertTrue(app.notify_user('testchild', 'T', 'B'))
        self.assertEqual(seen['argv'],
                         ['sudo', 'systemd-run', '--machine=testchild@.host',
                          '--user', 'notify-send', '-u', 'critical', 'T', 'B'])

    def test_failure_returns_false_without_raising(self):
        def fake_run(argv, **kwargs):
            class R:
                returncode = 1
                stderr = 'Failed to get machine PTY'
            return R()

        with mock.patch.object(app.subprocess, 'run', fake_run):
            self.assertFalse(app.notify_user('testchild', 'T', 'B'))

    def test_invalid_username_never_runs(self):
        with mock.patch.object(app.subprocess, 'run') as m:
            self.assertFalse(app.notify_user('bad;user', 'T', 'B'))
            m.assert_not_called()

    def test_no_session_fails_silently_live(self):
        """bluefox has no session: must log-and-return-False, never raise."""
        if bluefox_has_session():
            self.skipTest('bluefox unexpectedly has a live session')
        self.assertFalse(app.notify_user('bluefox', 'Test', 'graceful check'))


class TestMinutesUntilBedtime(unittest.TestCase):
    def setUp(self):
        self.overnight = {'start': '21:00', 'end': '08:00',
                          'days': [MONDAY.weekday()]}  # Monday evening

    def test_soon(self):
        now = MONDAY.replace(hour=20, minute=50)
        mins = app.minutes_until_bedtime(self.overnight, now)
        self.assertGreater(mins, 0)
        self.assertLessEqual(mins, 15)

    def test_too_far(self):
        now = MONDAY.replace(hour=20, minute=44)
        self.assertGreater(
            app.minutes_until_bedtime(self.overnight, now), 15)

    def test_in_window_returns_zero(self):
        self.assertEqual(
            app.minutes_until_bedtime(
                self.overnight, MONDAY.replace(hour=21, minute=30)), 0)
        # Overnight tail (early Tuesday still belongs to Monday's window).
        tuesday = MONDAY + timedelta(days=1)
        self.assertEqual(
            app.minutes_until_bedtime(
                self.overnight, tuesday.replace(hour=7, minute=0)), 0)

    def test_after_window_is_far(self):
        tuesday = MONDAY + timedelta(days=1)
        mins = app.minutes_until_bedtime(
            self.overnight, tuesday.replace(hour=9, minute=0))
        self.assertGreater(mins, 15)

    def test_same_day_window(self):
        entry = {'start': '12:00', 'end': '14:00',
                 'days': [MONDAY.weekday()]}
        mins = app.minutes_until_bedtime(
            entry, MONDAY.replace(hour=11, minute=50))
        self.assertGreater(mins, 0)
        self.assertLessEqual(mins, 15)

    def test_invalid_entries(self):
        self.assertIsNone(app.minutes_until_bedtime({}, MONDAY))
        self.assertIsNone(app.minutes_until_bedtime(None, MONDAY))
        self.assertIsNone(app.minutes_until_bedtime(
            {'start': 'bogus', 'end': '08:00', 'days': [0]}, MONDAY))
        self.assertIsNone(app.minutes_until_bedtime(
            {'start': '21:00', 'end': '21:00', 'days': [0]}, MONDAY))
        self.assertIsNone(app.minutes_until_bedtime(
            {'start': '21:00', 'end': '08:00', 'days': []}, MONDAY))

    def test_window_id_format(self):
        self.assertEqual(app.bedtime_window_id(self.overnight),
                         '21:00-08:00')


class TestFormat12h(unittest.TestCase):
    """Clock times display 12-hour; storage stays 24h/ISO."""

    def test_evening(self):
        self.assertEqual(app.format_12h('21:00'), '9:00 PM')
        self.assertEqual(app.format_12h('23:59'), '11:59 PM')

    def test_morning(self):
        self.assertEqual(app.format_12h('07:00'), '7:00 AM')
        self.assertEqual(app.format_12h('00:30'), '12:30 AM')

    def test_noon_midnight_edges(self):
        self.assertEqual(app.format_12h('12:00'), '12:00 PM')
        self.assertEqual(app.format_12h('12:30'), '12:30 PM')
        self.assertEqual(app.format_12h('00:00'), '12:00 AM')

    def test_datetime_input(self):
        self.assertEqual(app.format_12h(datetime(2026, 10, 10, 21, 5)),
                         '9:05 PM')

    def test_empty_and_garbage_never_crash(self):
        self.assertEqual(app.format_12h(''), '')
        self.assertEqual(app.format_12h(None), '')
        self.assertEqual(app.format_12h('bogus'), 'bogus')
        self.assertEqual(app.format_12h('25:00'), '25:00')


class TestBedtimeWarnedState(unittest.TestCase):
    """Full enforce_bedtime loop with fake now and a temp bedtime.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = os.path.join(self.tmp.name, 'bedtime.json')
        self.old_file = app.BEDTIME_FILE
        app.BEDTIME_FILE = self.file
        self.state = {'locked': False}
        self.locks = []
        self.recorder = Recorder()

        def fake_status(username):
            return self.state['locked'], 'fake'

        def fake_lock(username, action):
            self.locks.append(action)
            self.state['locked'] = (action == 'lock')
            return True, 'OK'

        self.patches = [
            mock.patch.object(app, 'get_lock_status', fake_status),
            mock.patch.object(app, 'run_lock_command', fake_lock),
            mock.patch.object(app, 'notify_user', self.recorder),
        ]
        for p in self.patches:
            p.start()
        with open(self.file, 'w') as f:
            json.dump({'testchild': {
                'start': '21:00', 'end': '22:00',
                'days': [MONDAY.weekday()],
                'locked_by_bedtime': False}}, f)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        app.BEDTIME_FILE = self.old_file
        self.tmp.cleanup()

    def _entry(self):
        with open(self.file) as f:
            return json.load(f)['testchild']

    def test_warn_once_then_lock_then_clear(self):
        # 10 min before: exactly one pre-warning, no lock.
        app.enforce_bedtime(MONDAY.replace(hour=20, minute=50))
        self.assertEqual(self.recorder.titles(), ['Bedtime in 15 minutes'])
        self.assertEqual(self.locks, [])
        self.assertEqual(self._entry()['warned_for'], '21:00-22:00')
        bodies = [b for _, _, b in self.recorder.calls]
        self.assertTrue(any('9:00 PM' in b for b in bodies), bodies)
        self.assertFalse(any('21:00' in b for b in bodies), bodies)

        # A minute later: no repeat warning (60s loop must not spam).
        app.enforce_bedtime(MONDAY.replace(hour=20, minute=51))
        self.assertEqual(self.recorder.titles(), ['Bedtime in 15 minutes'])
        self.assertEqual(self.locks, [])

        # Inside the window: lock happens + lock-time notification.
        app.enforce_bedtime(MONDAY.replace(hour=21, minute=30))
        self.assertEqual(self.locks, ['lock'])
        self.assertEqual(self.recorder.titles(),
                         ['Bedtime in 15 minutes', 'Locked for bedtime'])
        self.assertTrue(self._entry()['locked_by_bedtime'])
        bodies = [b for _, _, b in self.recorder.calls]
        self.assertTrue(any('10:00 PM' in b for b in bodies), bodies)
        self.assertFalse(any('22:00' in b for b in bodies), bodies)

        # Still inside: no repeat lock, no repeat notification.
        app.enforce_bedtime(MONDAY.replace(hour=21, minute=31))
        self.assertEqual(self.locks, ['lock'])
        self.assertEqual(len(self.recorder.calls), 2)

        # Window ended: unlock (was bedtime-locked) + warned flag cleared.
        app.enforce_bedtime(MONDAY.replace(hour=23, minute=0))
        self.assertEqual(self.locks, ['lock', 'unlock'])
        self.assertNotIn('warned_for', self._entry())
        self.assertFalse(self._entry()['locked_by_bedtime'])

    def test_notify_failure_never_blocks_lock(self):
        with mock.patch.object(app, 'notify_user',
                               side_effect=RuntimeError('no session')):
            app.enforce_bedtime(MONDAY.replace(hour=21, minute=30))
        self.assertEqual(self.locks, ['lock'])
        self.assertTrue(self._entry()['locked_by_bedtime'])


def steam_proc(pid=9001, cpu=100.0):
    return {'pid': pid, 'comm': 'steam',
            'cmdline': '/home/testchild/.local/share/Steam/steam',
            'args': '', 'etime': '01:00:00', 'cpu_seconds': cpu}


class TestBudgetWarnings(unittest.TestCase):
    def setUp(self):
        self.recorder = Recorder()

    def _entry(self, limit_min=60.0, used_sec=0.0, day='2026-10-12'):
        entry = blank_entry()
        entry['limits'] = {'games': limit_min}
        entry['day'] = day
        entry['used_seconds'] = {'games': used_sec}
        return entry

    def test_low_warning_once_per_day(self):
        entry = self._entry(used_sec=56 * 60.0)  # 4 min left of 60
        now = datetime(2026, 10, 12, 15, 0)
        summary = account_and_enforce(
            'testchild', entry, [steam_proc()], None, now,
            prefs={}, index={}, act=False, notify=self.recorder)
        self.assertEqual(self.recorder.titles(), ['Games: 5 minutes left'])
        self.assertIn('games (low)', summary['notified'])
        self.assertEqual(entry['warned_low']['games'], '2026-10-12')
        # Second tick same day: no repeat.
        account_and_enforce('testchild', entry, [steam_proc()], None,
                            now + timedelta(seconds=60),
                            prefs={}, index={}, act=False,
                            notify=self.recorder)
        self.assertEqual(len(self.recorder.calls), 1)

    def test_low_warning_rearms_next_day(self):
        entry = self._entry(used_sec=56 * 60.0)
        entry['warned_low'] = {'games': '2026-10-11'}  # warned yesterday
        now = datetime(2026, 10, 12, 15, 0)
        account_and_enforce('testchild', entry, [steam_proc()], None, now,
                            prefs={}, index={}, act=False,
                            notify=self.recorder)
        self.assertEqual(self.recorder.titles(), ['Games: 5 minutes left'])
        self.assertEqual(entry['warned_low']['games'], '2026-10-12')

    def test_no_warning_when_plenty_left(self):
        entry = self._entry(used_sec=10 * 60.0)  # 50 min left
        account_and_enforce('testchild', entry, [steam_proc()], None,
                            datetime(2026, 10, 12, 15, 0),
                            prefs={}, index={}, act=False,
                            notify=self.recorder)
        self.assertEqual(self.recorder.calls, [])

    def test_pause_warning_once(self):
        entry = self._entry(limit_min=0.05)  # 3 seconds total
        now = datetime(2026, 10, 12, 15, 0)
        account_and_enforce('testchild', entry, [steam_proc()], None, now,
                            prefs={}, index={}, act=False,
                            notify=self.recorder)
        procs2 = [steam_proc(cpu=130.0)]  # active: +30 CPU-s proves use
        summary = account_and_enforce(
            'testchild', entry, procs2, None, now + timedelta(seconds=60),
            prefs={}, index={}, act=False, notify=self.recorder)
        self.assertTrue(entry['paused'].get('games'))
        pause_notes = [t for t in self.recorder.titles()
                       if 'out of time' in t]
        self.assertEqual(len(pause_notes), 1)
        self.assertIn('games (paused)', summary['notified'])
        # Next tick: already paused, no repeat notification.
        account_and_enforce('testchild', entry, procs2, None,
                            now + timedelta(seconds=120),
                            prefs={}, index={}, act=False,
                            notify=self.recorder)
        self.assertEqual(
            len([t for t in self.recorder.titles() if 'out of time' in t]),
            1)

    def test_reset_clears_warned_state(self):
        entry = self._entry()
        entry['day'] = '2026-10-11'  # stale: midnight rule triggers reset
        entry['warned_low'] = {'games': '2026-10-11'}
        entry['warned_paused'] = {'games': '2026-10-11'}
        account_and_enforce('testchild', entry, [], None,
                            datetime(2026, 10, 12, 15, 0),
                            prefs={}, index={}, act=False,
                            notify=self.recorder)
        self.assertEqual(entry['warned_low'], {})
        self.assertEqual(entry['warned_paused'], {})

    def test_notify_none_and_notify_failure_are_safe(self):
        entry = self._entry(limit_min=0.05)
        now = datetime(2026, 10, 12, 15, 0)
        account_and_enforce('testchild', entry, [steam_proc()], None, now,
                            prefs={}, index={}, act=False, notify=None)
        procs2 = [steam_proc(cpu=130.0)]

        def boom(username, title, body):
            raise RuntimeError('no session')

        # A throwing notifier must not break accounting/enforcement.
        summary = account_and_enforce(
            'testchild', entry, procs2, None, now + timedelta(seconds=60),
            prefs={}, index={}, act=False, notify=boom)
        self.assertTrue(entry['paused'].get('games'))
        self.assertTrue(summary['froze'])

    def test_tick_budgets_forwards_notify(self):
        """tick_budgets(notify=...) reaches account_and_enforce (loop path)."""
        with tempfile.TemporaryDirectory() as d:
            budgets_file = os.path.join(d, 'budgets.json')
            entry = self._entry(used_sec=56 * 60.0)
            with open(budgets_file, 'w') as f:
                json.dump({'testchild': entry}, f)
            with mock.patch.object(budgets, 'BUDGETS_FILE', budgets_file):
                summaries = budgets.tick_budgets(
                    {}, datetime(2026, 10, 12, 15, 0),
                    procs_provider=lambda u: [steam_proc()],
                    prefs={}, index={}, act=False, notify=self.recorder)
        self.assertEqual(len([s for s in summaries
                              if s.get('user') != '_watchdog']), 1)
        self.assertIn('games (low)', summaries[0]['notified'])
        self.assertEqual(self.recorder.titles(), ['Games: 5 minutes left'])


if __name__ == '__main__':
    unittest.main()
