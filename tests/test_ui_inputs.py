#!/usr/bin/env python3
"""Hour+minute entry for the calendar/app-override allowances, and the
day-picker row (UI_UX_PLAN.md §5.4).

* the calendar modal's "Allowance" entry is an hours + minutes pair that
  composes the same whole-minute value for the ``POST /api/schedule``
  contract (``allowance_minutes``) — the weekly-window editor it replaced
  retired with the calendar cutover (CALENDAR_DESIGN.md §10),
* the app-override "Allowance" entry is the same h/m pair for the unchanged
  ``/api/budgets/overrides`` contract (still whole ``minutes``) — the
  add-row form on the user page, and the per-row editor in the shared
  #pc-override dialog (see ``test_ui_override_dialog.py``),
* the calendar modal's repeat-days checkboxes share one row (the old
  bedtime "Apply to all days" row retired with the bedtime form).

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
CALENDAR_TEMPLATE = os.path.join(ROOT, 'templates', 'calendar.html')
CALENDAR_JS = os.path.join(ROOT, 'static', 'js', 'calendar.js')
USERS = [{'username': KID, 'uid': 1001, 'home': '/home/kid',
          'shell': '/bin/bash'}]


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

    def test_user_page_has_no_window_editor_left(self):
        # Calendar cutover: the weekly-window writer + its JS left the page.
        self.assertNotIn('window-allowance', self.src)
        self.assertNotIn('/api/budgets/windows', self.src)

    def test_calendar_modal_composes_minutes_for_the_schedule_api(self):
        js = read(CALENDAR_JS)
        self.assertIn("$('block-allowance-hours')", js)
        self.assertIn("$('block-allowance')", js)
        # The POST field is the whole-minute allowance (API contract).
        self.assertIn("body.set('allowance_minutes', allowance)", js)

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

class CalendarAllowanceTest(unittest.TestCase):
    """The /calendar modal carries the h/m allowance pair (the window
    editor's contract, moved to the schedule API). Source-level: stdlib
    only, no Flask, no live payload probing."""

    def test_allowance_is_an_hours_and_minutes_pair(self):
        html = read(CALENDAR_TEMPLATE)
        self.assertRegex(
            html,
            r'<input[^>]*id="block-allowance-hours"[^>]*max="99"')
        self.assertRegex(
            html,
            r'<input[^>]*id="block-allowance"[^>]*max="59"')
        for field in ('block-allowance-hours', 'block-allowance'):
            m = re.search(r'<input[^>]*id="%s"[^>]*>' % field, html)
            self.assertIsNotNone(m, field)
            self.assertIn('type="number"', m.group(0))

    def test_allowance_preserves_the_pinned_minutes_id(self):
        # UI_UX_PLAN.md §7.4 pinned `window-allowance` for the minutes
        # field; the calendar successor keeps the `-allowance` suffix
        # pattern on the minutes input (the hours field is the -hours twin).
        self.assertIn('id="block-allowance"', read(CALENDAR_TEMPLATE))


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


class CalendarDayRowTest(unittest.TestCase):
    """The old bedtime all-days row retired with the form; the calendar
    modal's day picker keeps the same row contract (source-level Jinja:
    one <span class="pc-row"> wrapper inside the days field, holding the
    seven loop-rendered checkboxes)."""

    def setUp(self):
        self.html = read(CALENDAR_TEMPLATE)

    def test_day_checkboxes_share_a_row(self):
        field = self.html.index('id="block-days-field"')
        row = self.html.index('<span class="pc-row">', field)
        day = self.html.index('class="block-day"', row)
        self.assertLess(field, row, 'the row must sit inside the days field')
        self.assertLess(row, day, 'the checkboxes must sit in the shared row')

    def test_day_inputs_carry_the_weekday_values(self):
        # Source-level (Jinja): value binds loop.index0 == date.weekday()
        # (MO..SU) per the schedule grammar; the seven day_labels render the
        # seven boxes (the render path is covered by test_ui_calendar).
        self.assertIn('<input type="checkbox" class="block-day" '
                      'value="{{ loop.index0 }}">', self.html)
        self.assertIn('{% for label in day_labels %}', self.html)
        self.assertIn("'dayCodes': ['MO','TU','WE','TH','FR','SA','SU']",
                      self.html)
        self.assertNotIn('all_days_checkbox', self.html)


if __name__ == '__main__':
    unittest.main()
