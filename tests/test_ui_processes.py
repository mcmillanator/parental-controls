#!/usr/bin/env python3
"""Processes screen tests (design-exports/SPEC.md Screen 4).

Server-render checks for ``/processes``: grouped rows with per-PID detail, the
App | Category | Time used today | State | Override columns, the Visible /
Hidden / All tabs, the reduced-motion switch, the override + visibility cards,
the state cards and the "paused not terminated" footnote.

Mocks only: temp auth store, mocked human users + live payload.
"""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import app
import budgets
from test_lock_regression import _AdminSessionMixin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, 'templates', 'processes.html')
EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''', re.I)

KID = 'kidproc'
USERS = [{'username': KID, 'uid': 1001, 'home': '/home/kidproc',
          'shell': '/bin/bash'}]


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def live_dict():
    state = budgets.budget_status(KID, {KID: budgets.blank_entry()})
    live = {
        'user': {'username': KID, 'uid': 1001, 'home': '/home/kidproc',
                 'shell': '/bin/bash', 'locked': False,
                 'status_detail': 'Unlocked', 'bedtime': {},
                 'in_bedtime': False, 'bonus': {'active': False}},
        'processes': [], 'visible': [{'comm': 'steam'}], 'hidden': [],
        'groups': {c: [] for c in budgets.CATEGORIES},
        'budget_state': state,
        'overrides': {'app_overrides': {'steam': {'mode': 'exempt', 'minutes': None},
                                        'firefox': {'mode': 'limit', 'minutes': 45}},
                      'windows': []},
        'app_names': ['steam', 'firefox'],
        'children': [KID],
    }
    live['groups']['games'] = [
        {'comm': 'steam', 'display_name': 'Steam', 'count': 3, 'used': '1h 18m',
         'used_minutes': 78,
         'members': [
             {'pid': 4102, 'comm': 'steam', 'display_name': 'Steam'},
             {'pid': 4124, 'comm': 'steam', 'display_name': 'Steam'},
             {'pid': 4160, 'comm': 'steam', 'display_name': 'Steam'},
         ]},
    ]
    live['groups']['browsers'] = [
        {'comm': 'firefox', 'display_name': 'Firefox', 'count': 2, 'used': '42m',
         'used_minutes': 42,
         'members': [
             {'pid': 9001, 'comm': 'firefox', 'display_name': 'Firefox'},
             {'pid': 9002, 'comm': 'firefox', 'display_name': 'Firefox'},
         ]},
    ]
    live['hidden'] = [
        {'comm': 'mydaemon', 'display_name': 'mydaemon', 'count': 1,
         'uniform_category': True, 'categories': ['other'], 'used': '',
         'members': [{'pid': 8000, 'comm': 'mydaemon',
                      'display_name': 'mydaemon'}]},
    ]
    return live


class ProcessesRenderTest(_AdminSessionMixin, unittest.TestCase):
    def render(self, live=None):
        live = live_dict() if live is None else live
        with mock.patch.object(app, 'get_human_users', return_value=USERS), \
             mock.patch.object(app, 'get_user_live', return_value=live):
            resp = self.client.get('/processes')
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def test_header_and_toolbar(self):
        html = self.render()
        self.assertIn('>Activity</p>', html)
        self.assertIn('>Processes</h1>', html)
        self.assertIn('id="proc-user"', html)
        self.assertIn('id="proc-machine"', html)
        self.assertIn('Refresh now', html)

    def test_active_section_is_activity(self):
        html = self.render()
        sidebar = html[html.index('class="pc-sidebar"'):]
        sidebar = sidebar[:sidebar.index('</aside>')]
        self.assertIn('Activity', sidebar)
        self.assertIn('aria-current="page"', sidebar)

    def test_tabs_and_reduced_motion_switch(self):
        html = self.render()
        for label in ('Visible', 'Hidden', 'All'):
            with self.subTest(label=label):
                self.assertIn('>%s</button>' % label, html)
        self.assertIn('id="reduced-motion"', html)
        self.assertIn('Reduced motion', html)

    def test_group_table_columns_and_labels(self):
        html = self.render()
        self.assertIn('id="processes-table"', html)
        for label in ('App', 'Category', 'Time used today', 'State', 'Override'):
            with self.subTest(label=label):
                self.assertIn('data-label="%s"' % label, html)

    def test_visible_group_and_pid_detail(self):
        html = self.render()
        self.assertIn('data-group-comm="steam"', html)
        self.assertIn('data-group-cat="games"', html)
        self.assertIn('× 3', html)
        self.assertIn('1h 18m', html)
        # per-PID detail expansion carries every member PID
        for pid in ('4102', '4124', '4160'):
            with self.subTest(pid=pid):
                self.assertIn('>%s<' % pid, html)
        self.assertIn('aria-controls="detail-games-steam"', html)

    def test_hidden_row_and_show_app(self):
        html = self.render()
        self.assertIn('data-visibility="hidden"', html)
        self.assertIn('data-group-comm="mydaemon"', html)
        self.assertIn('Show app', html)

    def test_override_chips_and_buttons(self):
        html = self.render()
        self.assertIn('Unlimited', html)
        self.assertIn('45m', html)
        self.assertIn('btn-override', html)
        self.assertIn('id="pc-override"', html)

    def test_override_and_visibility_cards(self):
        html = self.render()
        self.assertIn('Override type', html)
        self.assertIn('Default category budget', html)
        self.assertIn('Custom allowance', html)
        self.assertIn('Save override', html)
        self.assertIn('App visibility', html)
        self.assertIn('Hide app', html)
        self.assertIn('Show app', html)

    def test_state_cards_and_footnote(self):
        html = self.render()
        self.assertIn('Cannot reach machine', html)
        self.assertIn('No visible apps', html)
        self.assertIn('Offline mode hides live controls.', html)
        self.assertIn('Out-of-time apps are paused, not terminated.', html)

    def test_offline_and_no_external_assets(self):
        html = self.render()
        self.assertIsNone(EXTERNAL_ASSET.search(html))
        self.assertNotIn('http://', html)


class ProcessesSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read(TEMPLATE)

    def test_extends_the_shell(self):
        self.assertTrue(self.src.lstrip().startswith('{% extends "base.html" %}'))

    def test_wires_the_real_endpoints(self):
        for path in ("'/api/budgets/overrides'", "'/api/process-prefs'",
                     "'/api/budgets/'"):
            with self.subTest(path=path):
                self.assertIn(path, self.src)

    def test_uses_the_shared_override_dialog(self):
        self.assertIn('pcOverrideDialog', self.src)
        self.assertNotIn('window.confirm(', self.src)


if __name__ == '__main__':
    unittest.main()
