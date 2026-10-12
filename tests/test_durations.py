#!/usr/bin/env python3
"""Durations displayed as hours+minutes everywhere the parent reads one.

The single canonical formatter is ``budgets.format_hm`` (server) with a JS
twin ``fmtHM`` in templates/user.html used by the live-refresh render
paths. Budget "used"/"left", bonus remaining, app-override allowances and
weekly-window allowances all render through them, and the budget *limit*
is now entered as hours + minutes (composed back to whole MINUTES for the
unchanged /api/budgets contract).

Clock times stay 12-hour (see test_time12h) — this module is only about
elapsed/allowance durations. Stdlib only; nothing touches a real account,
the service, /etc, /opt, or the checkout.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import budgets
from test_lock_regression import _AdminSessionMixin, KID

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), '..', 'templates')


def read_template(name):
    with open(os.path.join(TEMPLATE_DIR, name), encoding='utf-8') as f:
        return f.read()


# ---------------------------------------------------------------------------
# budgets.format_hm — the canonical server formatter
# ---------------------------------------------------------------------------

class TestFormatHm(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(budgets.format_hm(0), '0m')

    def test_under_an_hour(self):
        self.assertEqual(budgets.format_hm(5), '5m')
        self.assertEqual(budgets.format_hm(45), '45m')
        self.assertEqual(budgets.format_hm(59), '59m')

    def test_exact_hours(self):
        self.assertEqual(budgets.format_hm(60), '1h')
        self.assertEqual(budgets.format_hm(120), '2h')

    def test_hours_and_minutes(self):
        self.assertEqual(budgets.format_hm(90), '1h 30m')
        self.assertEqual(budgets.format_hm(605), '10h 5m')

    def test_large(self):
        self.assertEqual(budgets.format_hm(1440), '24h')
        self.assertEqual(budgets.format_hm(1500), '25h')

    def test_accepts_numeric_strings_and_floats(self):
        self.assertEqual(budgets.format_hm('90'), '1h 30m')
        self.assertEqual(budgets.format_hm(90.0), '1h 30m')

    def test_none_negative_and_garbage_return_empty(self):
        # Call sites choose their own placeholder ('—'); the formatter
        # itself never invents one and never raises.
        for bad in (None, -1, -90, 'bogus', ''):
            with self.subTest(bad=bad):
                self.assertEqual(budgets.format_hm(bad), '')

    def test_no_seconds_token(self):
        for mins in (0, 1, 59, 60, 90, 1440):
            self.assertNotIn('s', budgets.format_hm(mins))


# ---------------------------------------------------------------------------
# templates/user.html — server render paths use the |hm filter
# ---------------------------------------------------------------------------

class TestTemplateUsesHmFilter(unittest.TestCase):
    def test_budget_used_and_left_cells(self):
        src = read_template('user.html')
        self.assertIn('{{ b.used_minutes|hm }}', src)
        self.assertIn('{{ b.remaining_minutes|hm }}', src)

    def test_bonus_displays(self):
        src = read_template('user.html')
        self.assertIn('user.bonus.remaining_minutes|hm', src)

    def test_override_and_window_allowances(self):
        src = read_template('user.html')
        self.assertIn('{{ ov.minutes|hm }}', src)
        self.assertIn('{{ w.allowance_minutes|hm }}', src)

    def test_no_bare_minute_duration_left(self):
        """The converted spots never emit a bare 'N min' duration again."""
        src = read_template('user.html')
        for leftover in ('{{ b.used_minutes }} min',
                         '{{ b.remaining_minutes }} min',
                         'remaining_minutes }} min left',
                         'remaining_minutes }}m',
                         '{{ ov.minutes }} min/day',
                         '{{ w.allowance_minutes }} min'):
            with self.subTest(leftover=leftover):
                self.assertNotIn(leftover, src)

    def test_helper_text_matches_hours_and_minutes_entry(self):
        src = read_template('user.html')
        self.assertIn('Daily limit per category', src)
        self.assertNotIn('Daily minutes per category', src)


# ---------------------------------------------------------------------------
# fmtHM — the JS twin, wired into every live-refresh render path
# ---------------------------------------------------------------------------

class TestJsFmtHm(unittest.TestCase):
    def setUp(self):
        self.src = read_template('user.html')

    def test_helper_exists_and_mirrors_server(self):
        self.assertIn('function fmtHM(min)', self.src)
        self.assertIn('const h = Math.floor(n / 60);', self.src)
        self.assertIn('const m = n % 60;', self.src)
        self.assertIn('return `${h}h ${m}m`;', self.src)
        self.assertIn('return `${h}h`;', self.src)
        self.assertIn('return `${m}m`;', self.src)

    def test_wired_into_render_budgets(self):
        self.assertIn('fmtHM(b.used_minutes)', self.src)
        self.assertIn('fmtHM(b.remaining_minutes)', self.src)

    def test_wired_into_render_bonus_and_account(self):
        # renderBonus status line.
        self.assertIn('esc(fmtHM(b.remaining_minutes))', self.src)
        # renderAccount badge.
        self.assertIn('fmtHM(data.bonus.remaining_minutes)', self.src)

    def test_wired_into_override_renderers(self):
        self.assertIn('fmtHM(info.used_minutes)', self.src)
        self.assertIn('fmtHM(info.minutes)', self.src)
        self.assertIn('esc(fmtHM(ov.minutes))', self.src)

    def test_no_bare_minute_duration_left_in_js(self):
        for leftover in ('`${b.used_minutes} min`',
                         '`${b.remaining_minutes} min`',
                         "esc(ov.minutes) + ' min/day'",
                         "info.minutes + ' min'"):
            with self.subTest(leftover=leftover):
                self.assertNotIn(leftover, self.src)


# ---------------------------------------------------------------------------
# Budget limit INPUT: hours + minutes, composed back to whole minutes
# ---------------------------------------------------------------------------

class TestBudgetLimitInput(unittest.TestCase):
    def setUp(self):
        self.src = read_template('user.html')

    def test_hour_and_minute_controls_present(self):
        self.assertIn('id="budget-{{ cat }}-hours"', self.src)
        self.assertIn('id="budget-{{ cat }}-minutes"', self.src)
        # The old single minutes box is gone.
        self.assertNotIn('id="budget-{{ cat }}"', self.src)

    def test_minute_control_is_capped_at_59(self):
        self.assertIn('id="budget-{{ cat }}-minutes"', self.src)
        # minutes 0..59 (the hour control carries the rollover).
        self.assertIn('min="0" max="59" step="1" id="budget-{{ cat }}-minutes"',
                      self.src)

    def test_save_budget_composes_total_minutes(self):
        self.assertIn('hours * 60 + mins', self.src)
        self.assertIn('budget-' + "' + category + '-hours'", self.src)
        self.assertIn('budget-' + "' + category + '-minutes'", self.src)
        # Still POSTs the single minutes field (contract unchanged).
        self.assertIn("'&minutes=' + encodeURIComponent(total)", self.src)

    def test_blank_pair_clears(self):
        self.assertIn('hoursStr === \'\' && minsStr === \'\'', self.src)
        self.assertIn('total = \'\';', self.src)


# ---------------------------------------------------------------------------
# /api/budgets contract: still takes MINUTES (the JS composes them)
# ---------------------------------------------------------------------------

class TestBudgetsApiContract(_AdminSessionMixin, unittest.TestCase):
    def test_post_forwards_minutes_to_set_limit(self):
        with mock.patch.object(budgets, 'set_limit',
                               return_value=(True, 'OK')) as m:
            resp = self.authed_post('/api/budgets', {
                'username': KID, 'category': 'games', 'minutes': '90'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertEqual(m.call_args.args[:3], (KID, 'games', '90'))
        self.assertEqual(m.call_args.kwargs.get('actor'), self.ADMIN_USER)

    def test_blank_minutes_clears(self):
        with mock.patch.object(budgets, 'set_limit',
                               return_value=(True, 'Limit cleared')) as m:
            resp = self.authed_post('/api/budgets', {
                'username': KID, 'category': 'games', 'minutes': ''})
        self.assertEqual(resp.status_code, 200)
        # Blank collapses to 0, which set_limit treats as "clear".
        self.assertEqual(m.call_args.args[:3], (KID, 'games', 0))


# ---------------------------------------------------------------------------
# Rendered page: server-side formatter output + prefilled hour/minute boxes
# ---------------------------------------------------------------------------

def live_dict():
    entry = budgets.blank_entry()
    entry['limits'] = {'games': 90}
    entry['used_seconds'] = {'games': 45 * 60}
    state = budgets.budget_status(KID, {KID: entry})
    return {
        'user': {'username': KID, 'uid': 1001, 'home': '/home/kid',
                 'shell': '/bin/bash', 'locked': False,
                 'status_detail': 'Unlocked',
                 'bedtime': {'start': '21:00', 'end': '07:00', 'days': [0]},
                 'in_bedtime': False,
                 'bonus': {'active': True, 'until': '2026-10-12T23:00:00',
                           'remaining_minutes': 90, 'granted_by': 'qaadmin',
                           'last_minutes': 90}},
        'processes': [], 'visible': [], 'hidden': [],
        'groups': {c: [] for c in budgets.CATEGORIES},
        'budget_state': state,
        'overrides': {'app_overrides': {}, 'windows': []},
        'app_names': [],
    }


class TestRenderedDurations(_AdminSessionMixin, unittest.TestCase):
    def _render(self):
        with mock.patch.object(app, 'get_user_live',
                               return_value=live_dict()):
            resp = self.client.get(f'/user/{KID}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_budget_cells_render_hours_and_minutes(self):
        html = self._render()
        # games: limit 90m -> hours box 1 / minutes box 30; used 45m; left 45m.
        self.assertIn('id="budget-games-hours" value="1"', html)
        self.assertIn('id="budget-games-minutes" value="30"', html)
        self.assertIn('>45m<', html)
        self.assertNotIn('>45 min<', html)

    def test_bonus_renders_hours_and_minutes(self):
        html = self._render()
        self.assertIn('1h 30m left', html)
        self.assertIn('Bonus (1h 30m)', html)
        self.assertNotIn('90 min left', html)

    def test_jinja_hm_filter_registered(self):
        self.assertIs(app.app.jinja_env.filters['hm'], budgets.format_hm)


if __name__ == '__main__':
    unittest.main()
