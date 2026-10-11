#!/usr/bin/env python3
"""Unit tests for the freezer watchdog (stdlib only — system python3).

All cgroup I/O goes to fake trees under temp dirs; the real
/sys/fs/cgroup hierarchy is never written by these tests.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import budgets
from budgets import parse_managed_cgroup, watchdog_tick


def make_cgroup(root, name, freeze='0', events_frozen='0',
                with_events=True, with_freeze=True):
    """Create a fake cgroup dir; returns its path."""
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    if with_freeze:
        with open(os.path.join(path, 'cgroup.freeze'), 'w') as f:
            f.write(freeze)
    if with_events:
        with open(os.path.join(path, 'cgroup.events'), 'w') as f:
            f.write(f'populated 1\nfrozen {events_frozen}\n')
    return path


def read_freeze(root, name):
    with open(os.path.join(root, name, 'cgroup.freeze')) as f:
        return f.read().strip()


class TestParseManagedCgroup(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_managed_cgroup('parental-bluefox-games'),
                         ('bluefox', 'games'))

    def test_hyphenated_user(self):
        # Split on the LAST hyphen so hyphenated usernames still parse.
        self.assertEqual(parse_managed_cgroup('parental-mary-jane-media'),
                         ('mary-jane', 'media'))

    def test_unknown_scheme(self):
        # Future quarantine-style groups are NOT ours: report-only.
        self.assertIsNone(
            parse_managed_cgroup('parental-tester-quarantine'))
        self.assertIsNone(parse_managed_cgroup('parental-bluefox-nonsense'))
        self.assertIsNone(parse_managed_cgroup('parental-nodash'))
        self.assertIsNone(parse_managed_cgroup('notparental-x-games'))

    def test_non_parental_ignored(self):
        self.assertIsNone(parse_managed_cgroup('user.slice'))


class TestWatchdog(unittest.TestCase):
    def test_stuck_frozen_without_state_is_thawed(self):
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-bluefox-games',
                        freeze='1', events_frozen='1')
            data = {'bluefox': budgets.blank_entry()}
            summary = watchdog_tick(data, cgroup_root=root)
            self.assertIn('parental-bluefox-games', summary['thawed'])
            self.assertEqual(read_freeze(root, 'parental-bluefox-games'),
                             '0')

    def test_frozen_without_any_user_entry_is_orphan_thawed(self):
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-bluefox-chat',
                        freeze='1', events_frozen='1')
            summary = watchdog_tick({}, cgroup_root=root)
            self.assertIn('parental-bluefox-chat', summary['thawed'])
            self.assertEqual(read_freeze(root, 'parental-bluefox-chat'),
                             '0')

    def test_legitimately_paused_is_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-bluefox-games',
                        freeze='1', events_frozen='1')
            entry = budgets.blank_entry()
            entry['paused'] = {'games': True}
            summary = watchdog_tick({'bluefox': entry}, cgroup_root=root)
            self.assertIn('parental-bluefox-games', summary['legit'])
            self.assertNotIn('parental-bluefox-games', summary['thawed'])
            # Freeze state must be exactly as found.
            self.assertEqual(read_freeze(root, 'parental-bluefox-games'),
                             '1')

    def test_unknown_scheme_reported_not_touched(self):
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-tester-quarantine',
                        freeze='1', events_frozen='1')
            summary = watchdog_tick({}, cgroup_root=root)
            self.assertIn('parental-tester-quarantine', summary['unknown'])
            self.assertEqual(summary['thawed'], [])
            # Membership/freeze state untouched.
            self.assertEqual(
                read_freeze(root, 'parental-tester-quarantine'), '1')

    def test_mid_freeze_transition_not_thawed(self):
        # freeze=1 written but events still `frozen 0`: settling, skip.
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-bluefox-media',
                        freeze='1', events_frozen='0')
            summary = watchdog_tick({}, cgroup_root=root)
            self.assertEqual(summary['thawed'], [])
            self.assertEqual(read_freeze(root, 'parental-bluefox-media'),
                             '1')
            self.assertTrue(any('transition' in s
                                for s in summary['skipped']))

    def test_unfrozen_left_alone(self):
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-bluefox-other',
                        freeze='0', events_frozen='0')
            summary = watchdog_tick({}, cgroup_root=root)
            self.assertIn('parental-bluefox-other', summary['clean'])
            self.assertEqual(summary['thawed'], [])

    def test_unreadable_state_skipped_never_thawed(self):
        with tempfile.TemporaryDirectory() as root:
            # Missing cgroup.freeze file: ambiguous, must skip.
            make_cgroup(root, 'parental-bluefox-games',
                        with_freeze=False, events_frozen='1')
            summary = watchdog_tick({}, cgroup_root=root)
            self.assertEqual(summary['thawed'], [])
            self.assertTrue(summary['skipped'])
            # Missing events file: ambiguous, must skip even at freeze=1.
            make_cgroup(root, 'parental-bluefox-chat', freeze='1',
                        with_events=False)
            summary = watchdog_tick({}, cgroup_root=root)
            self.assertEqual(summary['thawed'], [])
            self.assertTrue(summary['skipped'])

    def test_garbage_freeze_content_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-bluefox-games',
                        freeze='bogus', events_frozen='1')
            summary = watchdog_tick({}, cgroup_root=root)
            self.assertEqual(summary['thawed'], [])

    def test_non_parental_dirs_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'user.slice'))
            os.makedirs(os.path.join(root, 'init.scope'))
            summary = watchdog_tick({}, cgroup_root=root)
            self.assertEqual(summary, {'thawed': [], 'legit': [],
                                       'clean': [], 'unknown': [],
                                       'skipped': [], 'note': ''})

    def test_thaw_failure_recorded_not_raised(self):
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-bluefox-games',
                        freeze='1', events_frozen='1')

            def boom(user, cat):
                raise RuntimeError('no sudo here')

            summary = watchdog_tick({}, cgroup_root=root, thaw_fn=boom)
            self.assertEqual(summary['thawed'], [])
            self.assertTrue(summary['skipped'])

    def test_only_unfreeze_writes(self):
        """The watchdog must never freeze: thaw_fn sees only managed
        stuck groups, and the default alt-root thaw only writes '0'."""
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-bluefox-games',
                        freeze='0', events_frozen='0')
            calls = []

            def spy(user, cat):
                calls.append((user, cat))
                return True, ''

            watchdog_tick({}, cgroup_root=root, thaw_fn=spy)
            self.assertEqual(calls, [])


class TestTickWiring(unittest.TestCase):
    def test_tick_runs_watchdog_after_enforcement(self):
        """A stuck cgroup for a user with no paused state is thawed by
        tick_budgets; a legitimately-paused one is left frozen."""
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-bluefox-games',
                        freeze='1', events_frozen='1')
            data = {'bluefox': budgets.blank_entry()}
            data['bluefox']['day'] = datetime.now().date().isoformat()
            # Patch persistence + cgroup root to stay inside temp dirs.
            old_file = budgets.BUDGETS_FILE
            budgets.BUDGETS_FILE = os.path.join(root, 'budgets.json')
            try:
                budgets.save_budgets(data)
                summaries = budgets.tick_budgets(
                    {}, datetime.now(),
                    procs_provider=lambda u: [],
                    prefs={}, index={}, act=False,
                    cgroup_root=root)
            finally:
                budgets.BUDGETS_FILE = old_file
            watch = [s for s in summaries if s.get('user') == '_watchdog']
            self.assertEqual(len(watch), 1)
            self.assertIn('parental-bluefox-games', watch[0]['thawed'])

    def test_tick_does_not_thaw_currently_frozen_legit(self):
        """Enforcement-first ordering: a category the current tick froze
        (dry-run paused=True) with a frozen cgroup reads as legitimate."""
        with tempfile.TemporaryDirectory() as root:
            make_cgroup(root, 'parental-bluefox-games',
                        freeze='1', events_frozen='1')
            entry = budgets.blank_entry()
            entry['day'] = datetime.now().date().isoformat()
            entry['paused'] = {'games': True}
            data = {'bluefox': entry}
            old_file = budgets.BUDGETS_FILE
            budgets.BUDGETS_FILE = os.path.join(root, 'budgets.json')
            try:
                budgets.save_budgets(data)
                summaries = budgets.tick_budgets(
                    {}, datetime.now(),
                    procs_provider=lambda u: [],
                    prefs={}, index={}, act=False,
                    cgroup_root=root)
            finally:
                budgets.BUDGETS_FILE = old_file
            watch = [s for s in summaries if s.get('user') == '_watchdog']
            self.assertEqual(len(watch), 1)
            self.assertEqual(watch[0]['thawed'], [])
            self.assertIn('parental-bluefox-games', watch[0]['legit'])

    def test_tick_watchdog_opt_out(self):
        old_file = budgets.BUDGETS_FILE
        with tempfile.TemporaryDirectory() as root:
            budgets.BUDGETS_FILE = os.path.join(root, 'budgets.json')
            try:
                budgets.save_budgets({})
                summaries = budgets.tick_budgets(
                    {}, datetime.now(), procs_provider=lambda u: [],
                    prefs={}, run_watchdog=False)
            finally:
                budgets.BUDGETS_FILE = old_file
            self.assertFalse(any(s.get('user') == '_watchdog'
                                 for s in summaries))


if __name__ == '__main__':
    unittest.main()
