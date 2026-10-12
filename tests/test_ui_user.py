#!/usr/bin/env python3
"""Mobile-first redesign of the user detail page (UI_UX_PLAN.md §5.4 / Phase 3).

The busiest screen now extends the shared shell and lays itself out as a
status-first hero plus collapsible `.pc-disclosure` sections, with the wide
tables turned into labelled card stacks (`.pc-table--stack` + `data-label`)
under 768px. This module pins the things the redesign must *not* lose:

* the page extends ``base.html`` (offline shell, no CDN),
* no external ``http(s)://`` asset anywhere in the page,
* the stacked-table pattern with per-cell ``data-label``s,
* native ``<details>`` disclosures,
* destructive actions go through ``window.pcConfirm`` (never ``window.confirm``),
* every id/class the Katalon contract + stdlib suite pin still renders,
* the budget spend-vs-limit ``.pc-meter`` exists.

Renders go through the mocked admin session harness (temp auth store, mocked
wheel/system + a mocked ``get_user_live``). Stdlib only: nothing here touches
a real account, the service, ``/etc``, ``/opt``, or the checkout.
"""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import budgets
from test_lock_regression import _AdminSessionMixin, KID

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, 'templates', 'user.html')

# A jsdelivr/Bootstrap CDN <link> is exactly the failure this guards against.
EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''', re.I)

# UI_UX_PLAN.md §7.4 + tests/test_katalon_objects.py + the stdlib suite.
PINNED_IDS = (
    'account-status', 'account-status-text', 'account-badges',
    'account-actions', 'bonus-status', 'bonus-hours', 'bonus-minutes',
    'budget-games-hours', 'budget-games-minutes', 'budget-used-games',
    'budget-left-games', 'budget-status-games', 'budget-window-games',
    'budget-meter-games', 'process-section', 'hidden-procs', 'toast',
    'window-target-type', 'window-category-target', 'window-app-target',
    'window-allowance', 'override-comm', 'override-add-mode',
    'override-add-minutes', 'app-override-list', 'installed-apps',
    'all_days_checkbox',
)

PINNED_CLASSES = (
    'back-link', 'subtitle', 'badge-bonus', 'btn-lock', 'btn-unlock',
    'btn-logout', 'status-unlocked',
    'btn-group-toggle', 'btn-hide', 'btn-show-hidden', 'group-chevron',
    'process-name', 'group-row', 'group-detail', 'refresh-info',
    'override-mode', 'window-row', 'day-checkbox', 'badge-window',
)

PINNED_ATTRS = (
    'aria-expanded', 'aria-controls', 'data-window-id', 'data-group-cat',
    'data-group-comm', 'data-comm', 'data-override-row', 'data-label',
)

PINNED_TEXT = (
    'Weekly windows', 'Grant custom', 'Revoke', '+15m', '+30m', '+1h',
    '+1h30', '+2h', 'Emergency logout', 'Daily limit per category',
    'Status updates automatically every 10 seconds',
    'Frozen — by parent', 'Set a limit first', 'Show hidden',
    'No bonus time active', 'Window active',
)


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _entry():
    entry = budgets.blank_entry()
    entry['limits'] = {'games': 90, 'browsers': 60}
    entry['used_seconds'] = {'games': 45 * 60, 'browsers': 60 * 60}
    return entry


def live_dict(with_extras=True):
    """A live payload shaped like app.get_user_live()'s return value."""
    state = budgets.budget_status(KID, {KID: _entry()})
    live = {
        'user': {
            'username': KID, 'uid': 1001, 'home': '/home/kid',
            'shell': '/bin/bash', 'locked': False,
            'status_detail': 'Unlocked',
            'bedtime': {'start': '21:00', 'end': '07:00',
                        'days': [0, 1, 2, 3, 4], 'manual_override': False},
            'in_bedtime': True,
            'bonus': {'active': True, 'until': '2026-10-12T23:00:00',
                      'remaining_minutes': 90, 'granted_by': 'qaadmin',
                      'last_minutes': 90},
        },
        'processes': [], 'visible': [{'comm': 'firefox'}], 'hidden': [],
        'groups': {c: [] for c in budgets.CATEGORIES},
        'budget_state': state,
        'overrides': {'app_overrides': {}, 'windows': []},
        'app_names': ['firefox', 'steam'],
    }
    live['groups']['browsers'] = [
        {'comm': 'firefox', 'display_name': 'Firefox Web Browser',
         'count': 2, 'used': '12m', 'used_minutes': 12,
         'members': [
             {'pid': 9001, 'comm': 'firefox',
              'display_name': 'Firefox Web Browser'},
             {'pid': 9002, 'comm': 'firefox',
              'display_name': 'Firefox Web Browser'},
         ]},
    ]
    live['hidden'] = [
        {'comm': 'mydaemon', 'display_name': 'mydaemon', 'count': 1,
         'uniform_category': True, 'categories': ['other'],
         'members': [{'pid': 8000, 'comm': 'mydaemon',
                      'display_name': 'mydaemon',
                      'category': 'other'}]},
    ]
    if with_extras:
        live['overrides'] = {
            'app_overrides': {
                'firefox': {'mode': 'limit', 'minutes': 60},
                'steam': {'mode': 'exempt', 'minutes': None},
            },
            'windows': [
                {'id': 'w-category-games', 'target_type': 'category',
                 'target': 'games', 'days': [0, 1, 2], 'start': '13:00',
                 'end': '15:00', 'allowance_minutes': 60, 'enabled': True,
                 'window_active': True},
            ],
        }
    return live


class _UserPageRender(_AdminSessionMixin, unittest.TestCase):
    """Shared render helper: mocked admin session + mocked live payload."""

    def render(self, live=None, path=None):
        live = live_dict() if live is None else live
        with mock.patch.object(app, 'get_user_live', return_value=live):
            resp = self.client.get(path or ('/user/' + KID))
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)


class UserPageSourceTest(unittest.TestCase):
    """Static guards on templates/user.html itself."""

    @classmethod
    def setUpClass(cls):
        cls.src = read(TEMPLATE)

    def test_extends_the_shared_shell(self):
        self.assertTrue(self.src.lstrip().startswith('{% extends "base.html" %}'),
                        'user.html must extend templates/base.html')

    def test_has_no_doctype_or_head_of_its_own(self):
        # The shell owns <html>/<head>/<body>; a stray doctype/head here would
        # mean the lift-and-shift conversion was incomplete.
        self.assertNotIn('<!DOCTYPE html>', self.src)
        self.assertNotIn('<head>', self.src)
        self.assertNotIn('</body>', self.src)

    def test_has_no_external_asset_urls(self):
        self.assertIsNone(EXTERNAL_ASSET.search(self.src),
                          'user.html must not reference a CDN')

    def test_has_no_time_input(self):
        self.assertNotIn('type="time"', self.src)

    def test_no_raw_window_confirm(self):
        # Destructive actions go through the shell's <dialog> helper.
        self.assertNotIn('window.confirm(', self.src)
        self.assertNotIn('!confirm(', self.src)
        self.assertIn('pcConfirm(', self.src)

    def test_defines_the_content_and_scripts_blocks(self):
        self.assertIn('{% block content %}', self.src)
        self.assertIn('{% block scripts %}', self.src)


class UserPageRenderTest(_UserPageRender):
    """The server-rendered page (the first paint, pre-JS)."""

    def test_no_external_assets_in_the_rendered_page(self):
        html = self.render()
        self.assertIsNone(EXTERNAL_ASSET.search(html),
                          'rendered page references an external asset')
        self.assertIn('/static/css/app.css', html)
        self.assertIn('/static/vendor/bootstrap/bootstrap.min.css', html)

    def test_stacked_tables_carry_data_labels(self):
        html = self.render()
        self.assertIn('pc-table--stack', html)
        for label in ('Category', 'Limit (per day)', 'Used', 'Left', 'Status',
                      'Usage', 'Command', 'Actions', 'Allowance'):
            with self.subTest(label=label):
                self.assertIn('data-label="%s"' % label, html,
                              'stacked card needs a "%s" label' % label)
        # Every stacked table keeps its semantic <thead> (clipped, not removed)
        # so screen readers still get row/column relationships.
        self.assertGreaterEqual(html.count('<thead>'), 4)

    def test_disclosure_sections_present(self):
        html = self.render()
        self.assertIn('pc-disclosure', html)
        for section_id in ('bedtime-section', 'bonus-section',
                           'budgets-section', 'weekly-windows',
                           'app-overrides'):
            with self.subTest(section=section_id):
                self.assertIn('id="%s"' % section_id, html)
        # Native <details>, not a JS-only accordion.
        self.assertIn('<details class="pc-disclosure"', html)

    def test_processes_use_details_and_keep_the_group_contract(self):
        html = self.render()
        self.assertIn('<details class="pc-disclosure" open>', html)
        self.assertIn('aria-expanded', html)
        self.assertIn('aria-controls', html)
        self.assertIn('btn-group-toggle', html)
        self.assertIn('data-group-cat="browsers"', html)
        self.assertIn('data-group-comm="firefox"', html)
        self.assertIn('× 2', html)
        self.assertIn('Show hidden (1 groups, 1 processes)', html)
        self.assertIn('Browsers (1)', html)

    def test_budget_meter_present(self):
        html = self.render()
        self.assertIn('pc-meter', html)
        self.assertIn('pc-meter__fill', html)
        self.assertIn('id="budget-meter-games"', html)

    def test_hero_status_pill_and_actions(self):
        html = self.render()
        self.assertIn('id="account-status"', html)
        self.assertIn('class="status-unlocked pc-status pc-status--unlocked"',
                      html)
        self.assertIn('id="account-actions"', html)
        # unlocked -> Lock (destructive); not Unlock.
        self.assertIn('id="account-actions"', html)
        self.assertIn('btn-lock', html)
        self.assertIn('btn-logout', html)

    def test_locked_and_unknown_status_classes(self):
        # Katalon pins #account-status.status-locked / .status-unknown.
        for locked, cls in ((True, 'locked'), (None, 'unknown')):
            live = live_dict(with_extras=False)
            live['overrides'] = None
            live['user']['locked'] = locked
            live['user']['status_detail'] = 'Locked' if locked else 'Unknown'
            html = self.render(live)
            with self.subTest(cls=cls):
                self.assertIn(
                    'class="status-%s pc-status pc-status--%s"' % (cls, cls),
                    html)

    def test_all_pinned_ids_present(self):
        html = self.render()
        for pinned in PINNED_IDS:
            with self.subTest(id=pinned):
                self.assertIn('id="%s"' % pinned, html)

    def test_all_pinned_classes_present(self):
        html = self.render()
        for cls in PINNED_CLASSES:
            with self.subTest(cls=cls):
                self.assertIn(cls, html)

    def test_all_pinned_attrs_and_text_present(self):
        html = self.render()
        for attr in PINNED_ATTRS:
            with self.subTest(attr=attr):
                self.assertIn(attr, html)
        for text in PINNED_TEXT:
            with self.subTest(text=text):
                self.assertIn(text, html)

    def test_refresh_info_text_is_exact(self):
        html = self.render()
        self.assertIn('class="refresh-info">Status updates automatically '
                      'every 10 seconds</p>', html)

    def test_dialogs_are_wired(self):
        html = self.render()
        # The shell's reusable confirm dialog + helper are on the page ...
        self.assertIn('id="pc-confirm"', html)
        self.assertIn('pcConfirm(', html)
        # ... and the page's own destructive paths use it.
        self.assertIn('requireText', html)
        self.assertNotIn('window.confirm(', read(TEMPLATE))

    def test_windows_and_override_empty_states(self):
        live = live_dict(with_extras=False)
        live['overrides'] = None
        live['app_names'] = []
        html = self.render(live)
        self.assertIn('id="windows-empty"', html)
        self.assertIn('No weekly windows configured.', html)
        self.assertIn('id="app-overrides-empty"', html)
        self.assertIn('No app overrides configured.', html)

    def test_device_selector_is_conditional(self):
        html = self.render()
        self.assertNotIn('device-selector', html)


class TimeRulesDesignTest(_UserPageRender):
    """design-exports/SPEC.md Screen 2 elements on the Time rules page."""

    def _live_with(self, **over):
        live = live_dict()
        live['children'] = [KID, 'kidother']
        for key, value in over.items():
            live[key] = value
        return live

    def test_h1_and_eyebrow_and_child_tabs(self):
        html = self.render(self._live_with())
        self.assertIn('>kidaccount time rules</h1>', html)
        self.assertIn('>Time rules</p>', html)
        self.assertIn('class="pc-tabs"', html)
        self.assertIn('href="/user/kidother"', html)
        self.assertIn('aria-current="page"', html)

    def test_override_card_with_revoke(self):
        live = self._live_with()
        live['user']['bedtime']['manual_override'] = True
        html = self.render(live)
        self.assertIn('id="override-card"', html)
        self.assertIn('Manual override active', html)
        self.assertIn('btn-revoke-override', html)
        self.assertIn('Revoke override', html)

    def test_bedtime_enable_switch_and_warning_select(self):
        html = self.render(self._live_with())
        self.assertIn('id="bedtime-enabled"', html)
        self.assertIn('pc-switch', html)
        self.assertIn('id="bedtime-warning"', html)
        self.assertIn('Warning lead-time', html)

    def test_day_buttons_keep_the_day_checkboxes(self):
        html = self.render(self._live_with())
        self.assertIn('class="pc-daybtn"', html)
        for i in range(7):
            with self.subTest(day=i):
                self.assertIn('name="day_%d"' % i, html)
        self.assertIn('Apply to all days', html)

    def test_countdown_card(self):
        html = self.render(self._live_with())
        self.assertIn('id="countdown-card"', html)
        self.assertIn('class="pc-timer"', html)
        self.assertIn('id="bedtime-countdown"', html)

    def test_state_specimen(self):
        html = self.render(self._live_with())
        self.assertIn('id="time-rules-states"', html)
        self.assertIn('Could not save schedule', html)
        self.assertIn('No bedtime schedule', html)
        self.assertIn('Machine offline, controls unavailable.', html)

    def test_revoke_and_enable_scripts_are_wired(self):
        src = read(TEMPLATE)
        self.assertIn('function toggleBedtimeEnabled(', src)
        self.assertIn('function revokeOverride(', src)
        self.assertIn("'/api/lock'", src)


if __name__ == '__main__':
    unittest.main()
