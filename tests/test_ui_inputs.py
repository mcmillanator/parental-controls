#!/usr/bin/env python3
"""Hour+minute entry for the window/app-override allowances, and the
apply-to-all-days row (UI_UX_PLAN.md §5.4).

Three focused UI fixes on the user detail page:

* the weekly-window "Allowance" entry is now an hours + minutes pair that
  composes the same whole-minute value for the unchanged ``/api/budgets/windows``
  contract (the server still takes ``allowance_minutes``),
* the app-override "Allowance" entry is the same h/m pair for the unchanged
  ``/api/budgets/overrides`` contract (still whole ``minutes``) — the
  add-row form here, and the per-row editor in the shared #pc-override
  dialog (see ``test_ui_override_dialog.py``),
* the bedtime "Apply to all days:" label and checkbox share one row.

Renders go through the mocked admin session harness (temp auth store, mocked
``get_user_live``). Stdlib only; nothing here touches a real account, the
service, ``/etc``, ``/opt`` or a checkout path.
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


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def group_row(html, comm):
    """Slice the visible process-group <tr> for ``comm`` out of the page."""
    marker = 'data-group-comm="%s"' % comm
    i = html.index(marker)
    start = html.rindex('<tr class="group-row"', 0, i)
    end = html.index('</tr>', i)
    return html[start:end]


def live_dict():
    """A live payload with one window and one Limit app override."""
    entry = budgets.blank_entry()
    entry['limits'] = {'games': 90}
    live = {
        'user': {
            'username': KID, 'uid': 1001, 'home': '/home/kid',
            'shell': '/bin/bash', 'locked': False,
            'status_detail': 'Unlocked',
            'bedtime': {'start': '21:00', 'end': '07:00',
                        'days': [0, 1, 2, 3, 4, 5, 6], 'manual_override': False},
            'in_bedtime': True,
            'bonus': {'active': False},
        },
        'processes': [], 'visible': [{'comm': 'firefox'}], 'hidden': [],
        'groups': {c: [] for c in budgets.CATEGORIES},
        'budget_state': budgets.budget_status(KID, {KID: entry}),
        'overrides': {
            'app_overrides': {'firefox': {'mode': 'limit', 'minutes': 90}},
            'windows': [
                {'id': 'w-category-games', 'target_type': 'category',
                 'target': 'games', 'days': [0, 1, 2], 'start': '13:00',
                 'end': '15:00', 'allowance_minutes': 60, 'enabled': True,
                 'window_active': False},
            ],
        },
        'app_names': ['firefox', 'steam'],
    }
    live['groups']['browsers'] = [
        {'comm': 'firefox', 'display_name': 'Firefox Web Browser',
         'count': 1, 'used': '12m', 'used_minutes': 12,
         'members': [{'pid': 9001, 'comm': 'firefox',
                      'display_name': 'Firefox Web Browser'}]},
    ]
    return live


class _Render(_AdminSessionMixin, unittest.TestCase):
    def render(self, live=None):
        live = live_dict() if live is None else live
        with mock.patch.object(app, 'get_user_live', return_value=live):
            resp = self.client.get('/user/' + KID)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Static source guards (no Flask) — the JS composes minutes for the POST.
# ---------------------------------------------------------------------------

class SourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read(TEMPLATE)

    def test_compose_duration_helper_validates_hours_and_minutes(self):
        self.assertIn('function composeDuration(hoursEl, minsEl)', self.src)
        # minutes 0-59, hours 0-99, blank pair -> clear ('').
        self.assertIn('h < 0 || h > 99 || m < 0 || m > 59', self.src)
        self.assertIn("if (hStr === '' && mStr === '') return '';", self.src)

    def test_save_window_composes_minutes_for_the_unchanged_api(self):
        self.assertIn("document.getElementById('window-allowance-hours')",
                      self.src)
        self.assertIn("document.getElementById('window-allowance')", self.src)
        self.assertIn('composeDuration(', self.src)
        # The POST field is still the whole-minute allowance.
        self.assertIn('allowance_minutes: allowance', self.src)

    def test_override_add_composes_minutes_for_the_unchanged_api(self):
        self.assertIn("document.getElementById('override-add-hours')", self.src)
        self.assertIn("document.getElementById('override-add-minutes')",
                      self.src)
        self.assertIn('minutes: mode === \'limit\' ? minutes : \'\'', self.src)

    def test_apply_override_opens_the_shared_dialog(self):
        # The per-row editor is gone: the row button opens the one shared
        # #pc-override dialog (which composes the minutes itself).
        self.assertIn('window.pcOverrideDialog(', self.src)
        self.assertNotIn("wrap.querySelector('.override-hours')", self.src)
        self.assertNotIn("wrap.querySelector('.override-minutes')", self.src)
        self.assertNotIn('override-mode', self.src)

    def test_no_type_time_input(self):
        self.assertNotIn('type="time"', self.src)


# ---------------------------------------------------------------------------
# Rendered page guards.
# ---------------------------------------------------------------------------

class WindowAllowanceTest(_Render):
    def test_window_allowance_is_an_hours_and_minutes_pair(self):
        html = self.render()
        self.assertRegex(
            html,
            r'<input[^>]*id="window-allowance-hours"[^>]*max="99"')
        self.assertRegex(
            html,
            r'<input[^>]*id="window-allowance"[^>]*max="59"')
        # Both are number inputs.
        for field in ('window-allowance-hours', 'window-allowance'):
            m = re.search(r'<input[^>]*id="%s"[^>]*>' % field, html)
            self.assertIsNotNone(m, field)
            self.assertIn('type="number"', m.group(0))

    def test_window_allowance_preserves_the_pinned_id(self):
        # Pinned by UI_UX_PLAN.md §7.4 + test_ui_user.PINNED_IDS.
        self.assertIn('id="window-allowance"', self.render())


class OverrideAllowanceTest(_Render):
    def test_override_add_is_an_hours_and_minutes_pair(self):
        html = self.render()
        self.assertRegex(
            html,
            r'<input[^>]*id="override-add-hours"[^>]*max="99"')
        self.assertRegex(
            html,
            r'<input[^>]*id="override-add-minutes"[^>]*max="59"')
        for field in ('override-add-hours', 'override-add-minutes'):
            m = re.search(r'<input[^>]*id="%s"[^>]*>' % field, html)
            self.assertIsNotNone(m, field)
            self.assertIn('type="number"', m.group(0))

    def test_shared_dialog_has_an_hours_and_minutes_pair(self):
        # The h/m pair now lives in the one shared #pc-override dialog
        # (rendered from the shell partial), not in every process row.
        html = self.render()
        self.assertIn('class="override-hours pc-input"', html)
        # The pinned `.override-minutes` token is preserved on the minutes input.
        self.assertRegex(
            html,
            r'<input[^>]*class="override-minutes pc-input"[^>]*max="59"')

    def test_row_shows_the_state_chip_without_inputs(self):
        # 90 minutes -> a "1h 30m/day" chip in the row, and no inline editor.
        row = group_row(self.render(), 'firefox')
        self.assertIn('1h 30m/day', row)
        self.assertNotIn('override-mode', row)
        self.assertNotIn('override-hours', row)
        self.assertNotIn('override-minutes', row)
        self.assertNotIn('<select', row)
        self.assertNotIn('<input', row)

    def test_exempt_override_shows_unlimited_chip(self):
        live = live_dict()
        live['overrides']['app_overrides'] = {
            'firefox': {'mode': 'exempt', 'minutes': None}}
        html = self.render(live)
        row = group_row(html, 'firefox')
        self.assertIn('Unlimited', row)
        self.assertNotIn('override-mode', row)
        # The dialog's allowance inputs render blank; they are populated
        # from the stored override when the row button opens the dialog.
        self.assertIn('id="pc-override-hours"', html)
        self.assertNotIn('id="pc-override-hours" value=', html)

    def test_pinned_override_minutes_id_preserved(self):
        self.assertIn('id="override-add-minutes"', self.render())


class ApplyToAllDaysRowTest(_Render):
    def _assert_same_row(self, html, a, b):
        ia, ib = html.find(a), html.find(b)
        self.assertNotEqual(-1, ia, a)
        self.assertNotEqual(-1, ib, b)
        lo, hi = sorted((ia, ib))
        between = html[lo:hi]
        # No block boundary between the two -> they share one row.
        self.assertNotIn('</div>', between)
        self.assertNotIn('</span>', between)
        # The shared row is the `.pc-row` wrapper inside the field.
        field_start = html.rfind('<div class="pc-field">', 0, lo)
        row_start = html.rfind('<span class="pc-row">', 0, lo)
        self.assertGreater(row_start, field_start,
                           'label + checkbox must sit inside one .pc-row')

    def test_label_and_checkbox_share_a_row(self):
        html = self.render()
        self._assert_same_row(html, 'for="all_days_checkbox"',
                              'id="all_days_checkbox"')

    def test_checkbox_id_and_name_unchanged(self):
        html = self.render()
        self.assertRegex(html, r'<input[^>]*name="all_days"[^>]*id="all_days_checkbox"')
        self.assertIn('for="all_days_checkbox"', html)


if __name__ == '__main__':
    unittest.main()
