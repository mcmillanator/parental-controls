#!/usr/bin/env python3
"""Unit tests for budgets.py (stdlib only — run with system python3).

Filtering/categorization tests run against a captured read-only `ps`
snapshot of the tester session (tests/fixtures/tester_ps_snapshot.txt),
plus synthetic Steam-chain processes.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import budgets
from budgets import (
    ACTIVE_THRESHOLD_SECONDS,
    account_and_enforce,
    apply_filter,
    blank_entry,
    budget_reset_needed,
    budget_status,
    build_desktop_index,
    categorize,
    compute_deltas,
    freezable_members,
    group_by_category,
    is_default_hidden,
    is_never_pause,
    parse_cpu_seconds,
    parse_desktop_file,
)

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures',
                       'tester_ps_snapshot.txt')


def parse_snapshot(path):
    """Parse a captured `ps -o pid,comm,etime,time,args` snapshot."""
    procs = []
    with open(path) as f:
        lines = f.read().strip().split('\n')
    for line in lines[1:]:
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        args = parts[4] if len(parts) > 4 else ''
        procs.append({
            'pid': int(parts[0]),
            'comm': parts[1],
            'etime': parts[2],
            'cpu_seconds': parse_cpu_seconds(parts[3]),
            'args': args,
            'cmdline': args,  # snapshot has no /proc enrichment; ps args do
        })
    return procs


STEAM_PROCS = [
    {'pid': 9001, 'comm': 'steam',
     'cmdline': '/home/tester/.local/share/Steam/ubuntu12_32/steam',
     'args': '', 'etime': '01:00:00', 'cpu_seconds': 100.0},
    {'pid': 9002, 'comm': 'steamwebhelper',
     'cmdline': '/home/tester/.local/share/Steam/ubuntu12_64/steamwebhelper --type=renderer',
     'args': '', 'etime': '01:00:00', 'cpu_seconds': 50.0},
    {'pid': 9003, 'comm': 'reaper',
     'cmdline': '/home/tester/.local/share/Steam/ubuntu12_32/reaper SteamLaunch AppId=123',
     'args': '', 'etime': '01:00:00', 'cpu_seconds': 200.0},
    {'pid': 9004, 'comm': 'firefox',
     'cmdline': '/usr/lib64/firefox/firefox --new-window',
     'args': '', 'etime': '01:00:00', 'cpu_seconds': 60.0},
    {'pid': 9005, 'comm': 'vlc',
     'cmdline': '/usr/bin/vlc movie.mkv', 'args': '',
     'etime': '01:00:00', 'cpu_seconds': 30.0},
    {'pid': 9006, 'comm': 'discord',
     'cmdline': '/usr/bin/discord --ozone-platform=wayland',
     'args': '', 'etime': '01:00:00', 'cpu_seconds': 20.0},
]


class TestSnapshotFixture(unittest.TestCase):
    def test_snapshot_parses(self):
        procs = parse_snapshot(FIXTURE)
        self.assertGreater(len(procs), 10)
        for p in procs:
            self.assertIsNotNone(p['cpu_seconds'])

    def test_plumbing_is_default_hidden(self):
        """~95% of the tester session is plumbing: it must filter out."""
        procs = parse_snapshot(FIXTURE)
        visible, hidden = apply_filter(procs, 'tester', prefs={})
        self.assertGreater(len(hidden), 0)
        # Nearly everything in the captured plumbing session hides.
        self.assertLessEqual(len(visible), max(2, len(procs) // 10))

    def test_steam_chain_stays_visible(self):
        visible, hidden = apply_filter(STEAM_PROCS, 'tester', prefs={})
        comms = {p['comm'] for p in visible}
        for expected in ('steam', 'steamwebhelper', 'reaper', 'firefox',
                         'vlc', 'discord'):
            self.assertIn(expected, comms, f'{expected} wrongly hidden')
        self.assertEqual(hidden, [])

    def test_user_hide_and_show(self):
        prefs = {'tester': {'hidden_comm': ['steam'], 'shown_comm': []}}
        visible, hidden = apply_filter(STEAM_PROCS, 'tester', prefs=prefs)
        self.assertNotIn('steam', {p['comm'] for p in visible})
        self.assertIn('steam', {p['comm'] for p in hidden})
        # shown_comm re-shows a default-hidden entry
        procs = parse_snapshot(FIXTURE)
        hidden_comms = {p['comm'] for p in
                        apply_filter(procs, 'tester', prefs={})[1]}
        self.assertTrue(hidden_comms)
        some = sorted(hidden_comms)[0]
        prefs2 = {'tester': {'hidden_comm': [], 'shown_comm': [some]}}
        visible2, _ = apply_filter(procs, 'tester', prefs=prefs2)
        self.assertIn(some, {p['comm'] for p in visible2})

    def test_edge_does_not_match_knowledge(self):
        proc = {'comm': 'python3',
                'cmdline': '/usr/bin/python3 /opt/knowledge/app.py'}
        self.assertNotEqual(categorize(proc, {}), 'browsers')


class TestCategorize(unittest.TestCase):
    def test_overrides(self):
        cases = [
            ('steam', '/home/tester/.local/share/Steam/ubuntu12_32/steam', 'games'),
            ('reaper', 'reaper SteamLaunch AppId=730', 'games'),
            ('firefox', '/usr/lib64/firefox/firefox', 'browsers'),
            ('brave', '/opt/brave.com/brave/brave', 'browsers'),
            ('vlc', '/usr/bin/vlc', 'media'),
            ('discord', '/usr/bin/discord', 'chat'),
            ('vesktop', '/usr/bin/vesktop', 'chat'),
            ('kwrite', '/usr/bin/kwrite', 'other'),
        ]
        for comm, cmdline, expected in cases:
            with self.subTest(comm=comm):
                self.assertEqual(
                    categorize({'comm': comm, 'cmdline': cmdline}, {}),
                    expected)

    def test_desktop_categories_mapping(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'mygame.desktop')
            with open(p, 'w') as f:
                f.write('[Desktop Entry]\nName=MyGame\n'
                        'Exec=/opt/mygame/mygame.bin %U\n'
                        'Categories=Game;\n')
            parsed = parse_desktop_file(p)
            self.assertEqual(parsed, (['mygame.bin'], 'games', 'MyGame'))
            idx = {'mygame.bin': 'games'}
            self.assertEqual(
                categorize({'comm': 'mygame.bin',
                            'cmdline': '/opt/mygame/mygame.bin'}, idx),
                'games')

    def test_flatpak_exec_mapping(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'chat.desktop')
            with open(p, 'w') as f:
                f.write('[Desktop Entry]\nName=Chat\n'
                        'Exec=/usr/bin/flatpak run --branch=stable org.example.ChatApp\n'
                        'Categories=Chat;\n')
            keys, cat, name = parse_desktop_file(p)
            self.assertEqual(cat, 'chat')
            self.assertEqual(name, 'Chat')
            self.assertIn('chatapp', keys)

    def test_real_desktop_index_has_firefox(self):
        idx = build_desktop_index()
        self.assertEqual(idx.get('firefox'), 'browsers')

    def test_group_by_category(self):
        groups = group_by_category(list(STEAM_PROCS), {})
        self.assertEqual(len(groups['games']), 3)
        self.assertEqual(len(groups['browsers']), 1)
        self.assertEqual(len(groups['media']), 1)
        self.assertEqual(len(groups['chat']), 1)
        self.assertEqual(len(groups['other']), 0)


class TestCpuParsing(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_cpu_seconds('00:00:00'), 0.0)
        self.assertEqual(parse_cpu_seconds('00:00:01'), 1.0)
        self.assertEqual(parse_cpu_seconds('05:23'), 323.0)
        self.assertEqual(parse_cpu_seconds('21:47:19'), 21 * 3600 + 47 * 60 + 19)
        self.assertEqual(parse_cpu_seconds('1-02:03:04'),
                         86400 + 2 * 3600 + 3 * 60 + 4)
        self.assertIsNone(parse_cpu_seconds('bogus'))

    def test_deltas(self):
        procs = [{'pid': 1, 'comm': 'steam', 'cpu_seconds': 100.0},
                 {'pid': 2, 'comm': 'steam', 'cpu_seconds': 50.0}]
        last = {'1': {'comm': 'steam', 'cpu': 90.0}}
        deltas, new_last = compute_deltas(last, procs)
        self.assertEqual(deltas[1], 10.0)
        self.assertEqual(deltas[2], 0.0)  # first sighting bills nothing
        # PID reuse with a different comm bills nothing
        procs2 = [{'pid': 1, 'comm': 'bash', 'cpu_seconds': 999.0}]
        deltas2, _ = compute_deltas(new_last, procs2)
        self.assertEqual(deltas2[1], 0.0)


class TestResetRule(unittest.TestCase):
    def test_bedtime_end_transition(self):
        self.assertTrue(budget_reset_needed('2026-10-06', True, True, False,
                                            '2026-10-07'))
        # Overnight: past midnight but still in bedtime -> no reset
        self.assertFalse(budget_reset_needed('2026-10-06', True, True, True,
                                             '2026-10-07'))

    def test_midnight_fallback_no_bedtime(self):
        self.assertTrue(budget_reset_needed('2026-10-06', False, False, False,
                                            '2026-10-07'))
        self.assertFalse(budget_reset_needed('2026-10-07', False, False, False,
                                             '2026-10-07'))

    def test_stale_day_outside_bedtime(self):
        self.assertTrue(budget_reset_needed('2026-10-01', True, False, False,
                                            '2026-10-07'))

    def test_same_day_bedtime_end(self):
        # Afternoon nap window ending same day still resets
        self.assertTrue(budget_reset_needed('2026-10-07', True, True, False,
                                            '2026-10-07'))


class TestSafetyGates(unittest.TestCase):
    def test_never_pause_list(self):
        for comm in ('kwin_wayland', 'pipewire', 'wireplumber', 'dbus-broker',
                     'dbus-broker-lau', 'systemd', '(sd-pam)', 'xdg-desktop-por',
                     'bash', 'sshd', 'pipewire-pulse'):
            with self.subTest(comm=comm):
                self.assertTrue(is_never_pause({'comm': comm, 'cmdline': comm}))
        self.assertFalse(is_never_pause({'comm': 'steam',
                                         'cmdline': '/x/Steam/steam'}))

    def test_freezable_requires_limit_and_visibility(self):
        limits = {'games': 60}
        members = freezable_members(STEAM_PROCS, 'tester', 'games', limits,
                                    prefs={}, index={})
        self.assertEqual({p['comm'] for p in members},
                         {'steam', 'steamwebhelper', 'reaper'})
        # No limit -> nothing freezable
        self.assertEqual(freezable_members(STEAM_PROCS, 'tester', 'games',
                                           {}, prefs={}, index={}), [])
        # NEVER-pause comms are excluded even if categorized as games
        sneaky = [{'pid': 42, 'comm': 'bash',
                   'cmdline': 'bash /home/tester/.local/share/Steam/steam.sh',
                   'cpu_seconds': 5.0}]
        self.assertEqual(freezable_members(sneaky, 'tester', 'games', limits,
                                           prefs={}, index={}), [])

    def test_default_hidden(self):
        self.assertTrue(is_default_hidden({'comm': 'dbus-broker',
                                           'cmdline': 'dbus-broker --log 1'}))
        self.assertTrue(is_default_hidden({'comm': 'foo',
                                           'cmdline': '/usr/libexec/foo'}))
        self.assertFalse(is_default_hidden({'comm': 'steam',
                                            'cmdline': '/x/steam'}))


class TestAccounting(unittest.TestCase):
    def _entry(self):
        e = blank_entry()
        e['limits'] = {'games': 60}
        e['day'] = '2026-10-07'
        return e

    def test_idle_does_not_burn_budget(self):
        """Sub-threshold CPU deltas must not accumulate, even as wall time passes."""
        entry = self._entry()
        procs = [dict(p, cpu_seconds=100.0) for p in STEAM_PROCS[:3]]
        now = datetime(2026, 10, 7, 15, 0)
        account_and_enforce('tester', entry, procs, None, now,
                            prefs={}, index={}, act=False)
        # First tick only sets baselines.
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)
        procs2 = [dict(p, cpu_seconds=p['cpu_seconds'] + 0.2)
                  for p in procs]  # 0.2s each: idle noise
        account_and_enforce('tester', entry, procs2, None,
                            now + timedelta(seconds=60),
                            prefs={}, index={}, act=False)
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)

    def test_active_burns_wall_clock(self):
        """An active tick bills the tick's wall-clock duration, not CPU sum."""
        entry = self._entry()
        procs = [dict(p, cpu_seconds=100.0) for p in STEAM_PROCS[:3]]
        now = datetime(2026, 10, 7, 15, 0)
        account_and_enforce('tester', entry, procs, None, now,
                            prefs={}, index={}, act=False)
        procs2 = [dict(p, cpu_seconds=p['cpu_seconds'] + 30.0) for p in procs]
        account_and_enforce('tester', entry, procs2, None,
                            now + timedelta(seconds=60),
                            prefs={}, index={}, act=False)
        # 30 CPU-s each proves activity, but only 60 wall seconds are billed.
        self.assertAlmostEqual(entry['used_seconds']['games'], 60.0)

    def test_wall_clock_capped_after_gap(self):
        """Sleep/downtime gaps cannot bill phantom hours — capped at 5 min."""
        entry = self._entry()
        procs = [dict(p, cpu_seconds=100.0) for p in STEAM_PROCS[:3]]
        now = datetime(2026, 10, 7, 15, 0)
        account_and_enforce('tester', entry, procs, None, now,
                            prefs={}, index={}, act=False)
        procs2 = [dict(p, cpu_seconds=p['cpu_seconds'] + 5000.0)
                  for p in procs]
        account_and_enforce('tester', entry, procs2, None,
                            now + timedelta(days=1),
                            prefs={}, index={}, act=False)
        self.assertAlmostEqual(entry['used_seconds']['games'], 300.0)

    def test_exhaust_marks_paused_dry_run(self):
        entry = self._entry()
        entry['limits'] = {'games': 0.05}  # 3 seconds
        procs = [dict(p, cpu_seconds=100.0) for p in STEAM_PROCS[:1]]
        now = datetime(2026, 10, 7, 15, 0)
        account_and_enforce('tester', entry, procs, None, now,
                            prefs={}, index={}, act=False)
        procs2 = [dict(p, cpu_seconds=p['cpu_seconds'] + 30.0) for p in procs]
        summary = account_and_enforce('tester', entry, procs2, None,
                                      now + timedelta(seconds=60),
                                      prefs={}, index={}, act=False)
        self.assertTrue(entry['paused'].get('games'))
        self.assertTrue(summary['froze'])
        status = budget_status('tester', {'tester': entry})
        self.assertTrue(status['games']['exhausted'])

    def test_status_reports_whole_minutes(self):
        """Displays never show seconds: used/remaining are whole minutes."""
        entry = self._entry()
        entry['limits'] = {'games': 10}
        entry['used_seconds'] = {'games': 150.0}  # 2.5 min
        status = budget_status('tester', {'tester': entry})
        self.assertEqual(status['games']['used_minutes'], 2)
        self.assertEqual(status['games']['remaining_minutes'], 8)
        self.assertIsInstance(status['games']['used_minutes'], int)

    def test_threshold_value_documented(self):
        self.assertEqual(ACTIVE_THRESHOLD_SECONDS, 1.0)


if __name__ == '__main__':
    unittest.main()
