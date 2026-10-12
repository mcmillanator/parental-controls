#!/usr/bin/env python3
"""Time budgets screen tests (design-exports/SPEC.md Screen 3).

Server-render checks for ``/budgets``: the app-budgets table (Category | Used
today | Daily limit | State | Action) with the stack/card pattern + progress
meter, the weekly-windows card, the hours+minutes specimen, the state row and
the app-override modal specimen — all through the shared shell with the "Time
rules" section active.

Mocks only: temp auth store, mocked human users + live payload. Nothing here
touches a real account, the service, /etc, /opt or the checkout.
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
TEMPLATE = os.path.join(ROOT, 'templates', 'budgets.html')
EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''', re.I)

KID = 'kidbudget'
USERS = [{'username': KID, 'uid': 1001, 'home': '/home/kidbudget',
          'shell': '/bin/bash'}]


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _entry():
    entry = budgets.blank_entry()
    entry['limits'] = {'games': 120, 'browsers': 60}
    entry['used_seconds'] = {'games': 78 * 60, 'browsers': 42 * 60}
    return entry


def live_dict():
    return {
        'user': {'username': KID, 'uid': 1001, 'home': '/home/kidbudget',
                 'shell': '/bin/bash', 'locked': False,
                 'status_detail': 'Unlocked', 'bedtime': {},
                 'in_bedtime': False, 'bonus': {'active': False}},
        'processes': [], 'visible': [], 'hidden': [],
        'groups': {c: [] for c in budgets.CATEGORIES},
        'budget_state': budgets.budget_status(KID, {KID: _entry()}),
        'overrides': {
            'app_overrides': {'steam': {'mode': 'exempt', 'minutes': None}},
            'windows': [
                {'id': 'w-category-games', 'target_type': 'category',
                 'target': 'games', 'days': [0, 1, 2, 3], 'start': '16:00',
                 'end': '20:00', 'allowance_minutes': 90, 'enabled': True,
                 'window_active': False},
            ],
        },
        'app_names': ['steam', 'firefox'],
        'children': [KID],
    }


class BudgetsRenderTest(_AdminSessionMixin, unittest.TestCase):
    def render(self, live=None, path='/budgets'):
        live = live_dict() if live is None else live
        with mock.patch.object(app, 'get_human_users', return_value=USERS), \
             mock.patch.object(app, 'get_user_live', return_value=live):
            resp = self.client.get(path)
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def test_page_header_and_save_all(self):
        html = self.render()
        self.assertIn('>Time rules</p>', html)
        self.assertIn('>Time budgets</h1>', html)
        self.assertIn('Save all changes', html)

    def test_active_section_is_time_rules(self):
        html = self.render()
        sidebar = html[html.index('class="pc-sidebar"'):]
        sidebar = sidebar[:sidebar.index('</aside>')]
        self.assertIn('aria-current="page"', sidebar)
        self.assertIn('Time rules', sidebar)

    def test_filter_row_has_user_picker(self):
        html = self.render()
        self.assertIn('id="budget-user"', html)
        self.assertIn('id="budget-date"', html)
        self.assertIn('Last refreshed', html)

    def test_app_budgets_table_columns_and_labels(self):
        html = self.render()
        self.assertIn('id="budgets-table"', html)
        self.assertIn('pc-table pc-table--stack', html)
        for label in ('Category', 'Used today', 'Daily limit', 'State', 'Action'):
            with self.subTest(label=label):
                self.assertIn('data-label="%s"' % label, html)

    def test_every_category_row_renders(self):
        html = self.render()
        for cat in budgets.CATEGORIES:
            with self.subTest(cat=cat):
                self.assertIn('id="budget-%s-hours"' % cat, html)
                self.assertIn('id="budget-%s-minutes"' % cat, html)
                self.assertIn('id="budget-used-%s"' % cat, html)
                self.assertIn('id="budget-status-%s"' % cat, html)

    def test_progress_meter_and_override_link(self):
        html = self.render()
        self.assertIn('pc-meter', html)
        self.assertIn('pc-meter__fill', html)
        self.assertIn('App override', html)
        self.assertIn('href="/processes?user=%s"' % KID, html)

    def test_weekly_windows_card(self):
        html = self.render()
        self.assertIn('Weekly windows', html)
        self.assertIn('Add weekly window', html)
        self.assertIn('Remove window', html)
        self.assertIn('data-window-id="w-category-games"', html)
        self.assertIn('4:00 PM', html)
        self.assertIn('8:00 PM', html)

    def test_hours_minutes_specimen(self):
        html = self.render()
        self.assertIn('Hours + minutes input', html)
        self.assertIn('Hours (0–23)', html)
        self.assertIn('Minutes (0–59)', html)

    def test_states_row(self):
        html = self.render()
        self.assertIn('No app budgets yet', html)
        self.assertIn('Unable to load budgets', html)
        self.assertIn('Last refreshed 4 min ago', html)
        self.assertIn('Offline — edits unavailable', html)

    def test_app_override_modal_specimen(self):
        html = self.render()
        self.assertIn('Steam override', html)
        self.assertIn('45m allowance', html)
        self.assertIn('Unlimited', html)

    def test_empty_state_when_no_user(self):
        with mock.patch.object(app, 'get_human_users', return_value=[]), \
             mock.patch.object(app, 'get_user_live', return_value=None):
            html = self.client.get('/budgets').get_data(as_text=True)
        self.assertIn('No app budgets yet', html)

    def test_offline_and_no_external_assets(self):
        html = self.render()
        self.assertIsNone(EXTERNAL_ASSET.search(html))
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)


class BudgetsSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read(TEMPLATE)

    def test_extends_the_shell_and_has_no_time_input(self):
        self.assertTrue(self.src.lstrip().startswith('{% extends "base.html" %}'))
        self.assertNotIn('type="time"', self.src)

    def test_wires_the_real_endpoints(self):
        for path in ("'/api/budgets'", "'/api/budgets/freeze'",
                     "'/api/budgets/unfreeze'", "'/api/budgets/windows'"):
            with self.subTest(path=path):
                self.assertIn(path, self.src)

    def test_no_raw_window_confirm(self):
        self.assertNotIn('window.confirm(', self.src)
        self.assertIn('pcConfirm(', self.src)


if __name__ == '__main__':
    unittest.main()
