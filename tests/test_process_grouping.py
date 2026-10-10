#!/usr/bin/env python3
"""Tests for comm-based process grouping (feature/process-grouping).

Covers the pure budgets.group_by_comm() contract, the get_user_live /
api_user_live shaping, and the grouped-rows template. No live sessions:
every process snapshot is synthetic (STEAM-like witness rows with
duplicate steamwebhelper PIDs), and Flask paths use a mocked
get_user_live / admin session.
"""

import json
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import budgets
from budgets import (etime_to_minutes, format_minutes, group_by_comm,
                     blank_entry)

try:
    import flask  # noqa: F401
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False


def proc(pid, comm, etime='01:00:00', category='games', cpu=1.0):
    return {'pid': pid, 'comm': comm, 'etime': etime,
            'cpu_seconds': cpu, 'args': '', 'cmdline': '' + comm,
            'category': category}


def steam_multi():
    """Witness rows: three steamwebhelper PIDs (the flood this feature
    fixes), one each of steam / vlc. etime values deliberately chosen so
    a string comparison would pick the wrong max ('1-02:03:04' vs
    '59:00' vs '01:00:00')."""
    return [
        proc(9001, 'steam', etime='01:02:00'),
        proc(9002, 'steamwebhelper', etime='01:00:00'),
        proc(9003, 'steamwebhelper', etime='59:00'),
        proc(9005, 'steamwebhelper', etime='1-02:03:04'),
        proc(9006, 'vlc', etime='00:00:05', category='media', cpu=30.0),
    ]


def full_procs():
    """Full list_user_processes-shaped rows (with cmdline, no category)."""
    rows = [
        {'pid': 9001, 'comm': 'steam', 'etime': '01:02:00',
         'cpu_seconds': 100.0,
         'args': '/home/tester/.local/share/Steam/ubuntu12_32/steam',
         'cmdline': '/home/tester/.local/share/Steam/ubuntu12_32/steam'},
        {'pid': 9002, 'comm': 'steamwebhelper', 'etime': '01:00:00',
         'cpu_seconds': 50.0, 'args': '',
         'cmdline': ('/home/tester/.local/share/Steam/ubuntu12_64/'
                     'steamwebhelper --type=renderer')},
        {'pid': 9003, 'comm': 'steamwebhelper', 'etime': '59:00',
         'cpu_seconds': 40.0, 'args': '',
         'cmdline': ('/home/tester/.local/share/Steam/ubuntu12_64/'
                     'steamwebhelper --type=gpu')},
        {'pid': 9005, 'comm': 'steamwebhelper', 'etime': '1-02:03:04',
         'cpu_seconds': 60.0, 'args': '',
         'cmdline': ('/home/tester/.local/share/Steam/ubuntu12_64/'
                     'steamwebhelper --type=audio')},
        {'pid': 9006, 'comm': 'vlc', 'etime': '00:00:05',
         'cpu_seconds': 30.0, 'args': '/usr/bin/vlc movie.mkv',
         'cmdline': '/usr/bin/vlc movie.mkv'},
    ]
    return rows


def grouped_live():
    """A get_user_live()-shaped result with duplicates already grouped."""
    user = {'username': 'kid', 'uid': 1001, 'home': '/home/kid',
            'shell': '/bin/bash', 'locked': False,
            'status_detail': 'Unlocked', 'bedtime': {}, 'in_bedtime': False,
            'bonus': {'active': False, 'until': None,
                      'remaining_minutes': None, 'granted_by': None,
                      'last_minutes': None}}
    groups = {c: [] for c in budgets.CATEGORIES}
    groups['games'] = [
        {'comm': 'steam', 'count': 1, 'max_minutes': 62,
         'max_elapsed': '1h 2m', 'categories': ['games'],
         'uniform_category': True,
         'members': [{'pid': 9001, 'comm': 'steam', 'category': 'games',
                      'elapsed': '1h 2m', 'etime': '01:02:00'}]},
        {'comm': 'steamwebhelper', 'count': 3, 'max_minutes': 1563,
         'max_elapsed': '1d 2h 3m', 'categories': ['games'],
         'uniform_category': True,
         'members': [
             {'pid': 9002, 'comm': 'steamwebhelper', 'category': 'games',
              'elapsed': '1h', 'etime': '01:00:00'},
             {'pid': 9003, 'comm': 'steamwebhelper', 'category': 'games',
              'elapsed': '59m', 'etime': '59:00'},
             {'pid': 9005, 'comm': 'steamwebhelper', 'category': 'games',
              'elapsed': '1d 2h 3m', 'etime': '1-02:03:04'}]},
    ]
    hidden = [
        {'comm': 'vlc', 'count': 1, 'max_minutes': 0, 'max_elapsed': '0m',
         'categories': ['media'], 'uniform_category': True,
         'members': [{'pid': 9006, 'comm': 'vlc', 'category': 'media',
                      'elapsed': '0m', 'etime': '00:00:05'}]},
    ]
    return {
        'user': user,
        'processes': full_procs(),
        'visible': full_procs()[:4],  # flat gate list (mirrors get_user_live)
        'hidden': hidden,
        'groups': groups,
        'budget_state': budgets.budget_status('kid',
                                              {'kid': blank_entry()}),
    }


class TestEtimeToMinutes(unittest.TestCase):
    def test_parses_ps_forms(self):
        self.assertEqual(etime_to_minutes('21:47:19'),
                         21 * 60 + 47)  # seconds floored
        self.assertEqual(etime_to_minutes('05:23'), 5)
        self.assertEqual(etime_to_minutes('1-02:03:04'), 1563)
        self.assertEqual(etime_to_minutes('00:00:00'), 0)
        self.assertEqual(etime_to_minutes('00:00:05'), 0)  # whole minutes

    def test_invalid_returns_none(self):
        self.assertIsNone(etime_to_minutes('bogus'))
        self.assertIsNone(etime_to_minutes(''))
        self.assertIsNone(etime_to_minutes('12:34:56:78'))
        self.assertIsNone(etime_to_minutes(123))


class TestFormatMinutes(unittest.TestCase):
    def test_whole_minutes_only(self):
        self.assertEqual(format_minutes(0), '0m')
        self.assertEqual(format_minutes(5), '5m')
        self.assertEqual(format_minutes(60), '1h')
        self.assertEqual(format_minutes(90), '1h 30m')
        self.assertEqual(format_minutes(1307), '21h 47m')
        self.assertEqual(format_minutes(1563), '1d 2h 3m')

    def test_no_seconds_tokens(self):
        for mins in (0, 1, 59, 60, 1563):
            self.assertNotIn('s', format_minutes(mins).rstrip('m'))


class TestGroupByComm(unittest.TestCase):
    def test_duplicate_comms_group(self):
        groups = group_by_comm(steam_multi())
        by_comm = {g['comm']: g for g in groups}
        self.assertEqual(by_comm['steamwebhelper']['count'], 3)
        members = by_comm['steamwebhelper']['members']
        self.assertEqual([m['pid'] for m in members], [9002, 9003, 9005])

    def test_singleton_counts(self):
        groups = group_by_comm(steam_multi())
        by_comm = {g['comm']: g for g in groups}
        self.assertEqual(by_comm['steam']['count'], 1)
        self.assertEqual(by_comm['vlc']['count'], 1)

    def test_empty_input(self):
        self.assertEqual(group_by_comm([]), [])

    def test_deterministic_order_under_shuffle(self):
        rows = list(steam_multi())
        expected = [g['comm'] for g in group_by_comm(rows)]
        random.Random(7).shuffle(rows)
        self.assertEqual([g['comm'] for g in group_by_comm(rows)], expected)

    def test_max_minutes_not_string_comparison(self):
        """Oldest wins by raw etime minutes, not by formatted-string."""
        groups = group_by_comm(steam_multi())
        swh = next(g for g in groups if g['comm'] == 'steamwebhelper')
        # '1-02:03:04' (1563m) > '01:00:00' (60m) > '59:00' (59m). A string
        # compare would pick '01:00:00'/'59:00' wrong.
        self.assertEqual(swh['max_minutes'], 1563)
        self.assertEqual(swh['max_elapsed'], '1d 2h 3m')

    def test_elapsed_whole_minutes_no_seconds(self):
        groups = group_by_comm(steam_multi())
        for g in groups:
            self.assertNotIn('s', (g['max_elapsed'] or '').rstrip('m'))
        vlc = next(g for g in groups if g['comm'] == 'vlc')
        self.assertEqual(vlc['max_elapsed'], '0m')

    def test_members_sorted_by_pid(self):
        groups = group_by_comm(steam_multi())
        for g in groups:
            pids = [m['pid'] for m in g['members']]
            self.assertEqual(pids, sorted(pids))

    def test_mixed_categories_flag(self):
        rows = [
            proc(1, 'python3', category='games'),
            proc(2, 'python3', category='media'),
            proc(3, 'firefox', category='browsers'),
        ]
        groups = {g['comm']: g for g in group_by_comm(rows)}
        self.assertFalse(groups['python3']['uniform_category'])
        self.assertEqual(groups['python3']['categories'], ['games', 'media'])
        self.assertTrue(groups['firefox']['uniform_category'])
        self.assertEqual(groups['firefox']['categories'], ['browsers'])

    def test_category_composition_scoped_per_bucket(self):
        """Same comm under two categories -> one group per bucket."""
        visible = {c: [] for c in budgets.CATEGORIES}
        visible['games'] = group_by_comm(
            [proc(1, 'python3', category='games'),
             proc(2, 'python3', category='games')])
        visible['media'] = group_by_comm(
            [proc(3, 'python3', category='media')])
        self.assertEqual(len(visible['games']), 1)
        self.assertEqual(visible['games'][0]['count'], 2)
        self.assertEqual(len(visible['media']), 1)
        self.assertEqual(visible['media'][0]['count'], 1)


@unittest.skipUnless(HAS_FLASK, 'Flask not installed')
class TestGroupingShapes(unittest.TestCase):
    def _admin_patches(self):
        """Fake a logged-in wheel admin session (same as test_safeguards)."""
        import app
        store = {'users': {'boss': {'hash': 'x', 'must_change': False,
                                    'locked': False}}}
        sess = {'username': 'boss', 'csrf': 'test-csrf', 'created': 0,
                'last_seen': 0}
        return (
            mock.patch.object(app.auth, 'get_session',
                              return_value=dict(sess)),
            mock.patch.object(app.auth, 'load_store', return_value=store),
            mock.patch.object(app.auth, 'is_wheel_member',
                              return_value=True),
            mock.patch.object(app.auth, 'is_system_user',
                              return_value=True),
        )

    def test_get_user_live_groups_duplicate_comms(self):
        import app
        procs = full_procs()
        with mock.patch.object(app, 'get_human_users',
                               return_value=[{'username': 'kid', 'uid': 1001,
                                              'home': '/home/kid',
                                              'shell': '/bin/bash'}]), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             mock.patch.object(app.budgets, 'list_user_processes',
                               return_value=procs), \
             mock.patch.object(app.budgets, 'load_prefs',
                               return_value={}), \
             mock.patch.object(app.budgets, 'build_desktop_index',
                               return_value={}), \
             mock.patch.object(app.budgets, 'budget_status',
                               return_value={c: {'minutes': 0,
                                                 'used_minutes': 0,
                                                 'remaining_minutes': None,
                                                 'paused': False,
                                                 'exhausted': False}
                                             for c in budgets.CATEGORIES}):
            data = app.get_user_live('kid')
        groups = {g['comm']: g for g in data['groups']['games']}
        self.assertEqual(groups['steamwebhelper']['count'], 3)
        self.assertEqual([m['pid'] for m in groups['steamwebhelper']['members']],
                         [9002, 9003, 9005])
        self.assertEqual(groups['steamwebhelper']['max_minutes'], 1563)
        self.assertEqual(data['hidden'], [])

    def test_get_user_live_groups_hidden(self):
        import app
        procs = full_procs()
        with mock.patch.object(app, 'get_human_users',
                               return_value=[{'username': 'kid', 'uid': 1001,
                                              'home': '/home/kid',
                                              'shell': '/bin/bash'}]), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             mock.patch.object(app.budgets, 'list_user_processes',
                               return_value=procs), \
             mock.patch.object(app.budgets, 'load_prefs',
                               return_value={'kid': {'hidden_comm': ['vlc'],
                                                     'shown_comm': []}}), \
             mock.patch.object(app.budgets, 'build_desktop_index',
                               return_value={}), \
             mock.patch.object(app.budgets, 'budget_status',
                               return_value={}):
            data = app.get_user_live('kid')
        hidden = {g['comm']: g for g in data['hidden']}
        self.assertEqual(hidden['vlc']['count'], 1)
        self.assertEqual(hidden['vlc']['categories'], ['media'])
        self.assertTrue(hidden['vlc']['uniform_category'])
        # Hidden comm is gone from the visible groups.
        visible_comms = {g['comm'] for g in data['groups']['media']}
        self.assertNotIn('vlc', visible_comms)

    def test_api_live_payload_group_shape(self):
        import app
        live = grouped_live()
        with mock.patch.object(app, 'get_user_live', return_value=live), \
             self._admin_patches()[0], self._admin_patches()[1], \
             self._admin_patches()[2], self._admin_patches()[3]:
            client = app.app.test_client()
            client.set_cookie('pc_sid', 'fake-test-session')
            resp = client.get('/api/user/kid/live')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertIn('budget', payload)          # drives the paused badge
        self.assertIn('games', payload['budget'])
        games = payload['groups']['games']
        by_comm = {g['comm']: g for g in games}
        swh = by_comm['steamwebhelper']
        self.assertEqual(swh['count'], 3)
        self.assertEqual(swh['elapsed'], '1d 2h 3m')  # max member elapsed
        self.assertEqual([m['pid'] for m in swh['members']],
                         [9002, 9003, 9005])
        # Every visible PID is preserved inside members — no flat per-PID
        # duplicate rows remain at the top level.
        all_pids = sorted(m['pid'] for g in games for m in g['members'])
        self.assertEqual(all_pids, [9001, 9002, 9003, 9005])
        self.assertTrue(all(isinstance(g, dict) and 'members' in g
                            and g['count'] == len(g['members'])
                            for g in games))
        # Single-instance groups keep count == 1.
        self.assertEqual(by_comm['steam']['count'], 1)
        # Hidden table is grouped too, members keep their category.
        hidden = payload['hidden']
        self.assertEqual([g['comm'] for g in hidden], ['vlc'])
        self.assertEqual(hidden[0]['members'][0]['category'], 'media')

    def test_user_page_template_grouped(self):
        import app
        live = grouped_live()
        with mock.patch.object(app, 'get_user_live', return_value=live), \
             self._admin_patches()[0], self._admin_patches()[1], \
             self._admin_patches()[2], self._admin_patches()[3]:
            client = app.app.test_client()
            client.set_cookie('pc_sid', 'fake-test-session')
            resp = client.get('/user/kid')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # Accessibility + toggle plumbing exists.
        self.assertIn('aria-expanded', html)
        self.assertIn('aria-controls', html)
        self.assertIn('btn-group-toggle', html)
        # Count badge for the duplicate group.
        self.assertIn('× 3', html)
        # Exactly one top-level group row per comm per category.
        self.assertEqual(
            html.count('<tr class="group-row" data-group-cat="games" '
                       'data-group-comm="steamwebhelper"'), 1)
        self.assertEqual(
            html.count('<tr class="group-row" data-group-cat="games" '
                       'data-group-comm="steam"'), 1)
        # No flat per-PID rows built from the old shape.
        self.assertNotIn('(PID: 9002)', html)
        # Hidden table groups by comm: one server-rendered group row + one
        # Show button (JS source also contains the literal, so match the
        # data-comm-bound server row exactly).
        self.assertIn('data-group-cat="hidden"', html)
        self.assertEqual(
            html.count('data-comm="vlc" onclick="showGroup(this)">'
                       'Show</button>'), 1)
        self.assertIn('Show hidden (1 groups, 1 processes)', html)
        # Category header counts groups, not processes.
        self.assertIn('Games (2)', html)


if __name__ == '__main__':
    unittest.main()