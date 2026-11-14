#!/usr/bin/env python3
"""Calendar page (/calendar) — UI slice tests (CALENDAR_DESIGN.md §10).

Three layers:

1. **Asset/offline guardrails** (stdlib only, no Flask): the vendored
   FullCalendar bundle is the pinned version with its banner + license,
   nothing under templates/ or static/ may reference a CDN, and the
   calendar page links only local assets.
2. **Render pins** (Flask test client, mocked users + live payload): the
   page requires the admin session, carries the child selector, the grid
   mount, the editor modal, the fail-closed error banner and the config
   block; the Time-rules surface it replaced is gone.
3. **API e2e** (the exact form fields the modal posts): create bedtime +
   capped-game blocks, edit, disable, delete, the overlap-rejection path
   and the immediate bedtime-lock path — all through /api/schedule with
   temp state homes.

No real accounts, no locks beyond mocked seams, no /etc, no service.
"""

import hashlib
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import app
import budgets
from test_lock_regression import _AdminSessionMixin, KID

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / 'templates'
CALENDAR_TEMPLATE = TEMPLATE_DIR / 'calendar.html'
CALENDAR_JS = ROOT / 'static' / 'js' / 'calendar.js'
FC_DIR = ROOT / 'static' / 'vendor' / 'fullcalendar'
FC_BUNDLE = FC_DIR / 'index.global.min.js'
FC_LICENSE = FC_DIR / 'LICENSE.md'
FC_NOTICE = FC_DIR / 'NOTICE.txt'

# Pinned exactly (static/vendor/fullcalendar/NOTICE.txt). A deliberate bump
# updates the NOTICE, these constants and CALENDAR_DESIGN.md §10 together.
FC_VERSION = '6.1.22'
FC_UPSTREAM_SHA256 = ('65f927f1802cb35428b9c5e2a8d2276633880a89ee5ea5f9a7'
                      '727f79892786ea')
FC_COMMITTED_SHA256 = ('bfb83ae68fd2dc4612a2c86cfb64c22753e4bc44a2d8149afe'
                       '6a6b8bfbb96aad')
FC_BANNER = 'VENDORING: FullCalendar v6 Standard Bundle'

EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''',
                            re.IGNORECASE)
URL_LITERAL = re.compile(r'''https?://\S''', re.IGNORECASE)

USERS = [{'username': KID, 'uid': 1001, 'home': '/home/kid',
          'shell': '/bin/bash'}]
LIVE = {
    'user': {'username': KID, 'uid': 1001, 'home': '/home/kid',
             'shell': '/bin/bash', 'locked': False,
             'status_detail': 'Unlocked', 'bedtime': {},
             'in_bedtime': False, 'bonus': {'active': False}},
    'processes': [], 'visible': [], 'hidden': [],
    'groups': {c: [] for c in budgets.CATEGORIES},
    'budget_state': budgets.budget_status(KID, {KID: budgets.blank_entry()}),
    'overrides': {'app_overrides': {}, 'windows': []},
    'app_names': ['steam', 'discord'],
}


def _render_calendar(client, users=None, live=None):
    users = USERS if users is None else users
    live = LIVE if live is None else live
    with mock.patch.object(app, 'get_human_users', return_value=users), \
         mock.patch.object(app, 'get_user_live', return_value=live):
        return client.get('/calendar')


# ---------------------------------------------------------------------------
# 1. Vendored artifact + offline promise (stdlib only).
# ---------------------------------------------------------------------------

class VendoredFullCalendarTest(unittest.TestCase):
    def test_bundle_present_with_pinned_hash(self):
        data = FC_BUNDLE.read_bytes()
        self.assertEqual(FC_COMMITTED_SHA256, hashlib.sha256(data).hexdigest(),
                         'vendored bundle differs from NOTICE.txt; bump '
                         'deliberately (NOTICE + §10 + these constants)')

    def test_banner_and_version_pinned(self):
        head = FC_BUNDLE.read_text(encoding='utf-8')[:600]
        self.assertIn(FC_BANNER, head)
        self.assertIn('Do not edit by hand', head)
        self.assertIn('tests/test_ui_calendar.py', head)
        self.assertIn('FullCalendar Standard Bundle v%s' % FC_VERSION, head)
        self.assertIn('(c) 2024 Adam Shaw', head)

    def test_css_is_injected_from_the_bundle(self):
        # v6 injects its stylesheets from the JS (no separate CSS artifact):
        # the inline fc-* rules + the style-element creation must stay.
        src = FC_BUNDLE.read_text(encoding='utf-8')
        self.assertIn('.fc-daygrid-body', src)
        self.assertIn('createElement("style")', src)

    def test_license_and_notice_vendored_next_to_the_bundle(self):
        self.assertIn('MIT License', FC_LICENSE.read_text(encoding='utf-8'))
        notice = FC_NOTICE.read_text(encoding='utf-8')
        self.assertIn(FC_UPSTREAM_SHA256, notice)
        self.assertIn(FC_COMMITTED_SHA256, notice)
        self.assertIn(FC_VERSION, notice)

    def test_page_links_only_local_assets(self):
        src = CALENDAR_TEMPLATE.read_text(encoding='utf-8')
        self.assertIsNone(EXTERNAL_ASSET.search(src),
                          'calendar.html must not reference a CDN')
        self.assertIn("url_for('static', filename='vendor/fullcalendar/"
                      "index.global.min.js')", src)
        self.assertIn("url_for('static', filename='js/calendar.js')", src)

    def test_calendar_js_has_no_url_literals(self):
        src = CALENDAR_JS.read_text(encoding='utf-8')
        self.assertIsNone(URL_LITERAL.search(src),
                          'calendar.js must talk same-origin only')
        # ...and the API paths it uses are the schedule API.
        for path in ("/api/schedule?username=", "'/api/schedule/events?'",
                     "'/api/schedule'", "'/api/schedule/' +"):
            with self.subTest(path=path):
                self.assertIn(path, src)

    def test_no_retired_writer_strings_anywhere_in_the_ui(self):
        for f in (CALENDAR_TEMPLATE, CALENDAR_JS,
                  TEMPLATE_DIR / 'budgets.html', TEMPLATE_DIR / 'user.html'):
            with self.subTest(file=f.name):
                src = f.read_text(encoding='utf-8')
                self.assertNotIn('/api/budgets/windows', src)
                self.assertNotIn("action=\"/api/bedtime\"", src)


# ---------------------------------------------------------------------------
# 2. Rendered page pins.
# ---------------------------------------------------------------------------

class CalendarRenderTest(_AdminSessionMixin, unittest.TestCase):
    def test_anonymous_redirected_to_login(self):
        anon = app.app.test_client()
        resp = anon.get('/calendar')
        self.assertEqual(302, resp.status_code)
        self.assertIn('/login', resp.headers['Location'])

    def test_page_renders_key_elements(self):
        resp = _render_calendar(self.client)
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        self.assertIn('Parental Controls — Calendar', html)
        self.assertIn('data-pc-calendar', html)          # grid mount
        self.assertIn('id="pc-block-editor"', html)      # editor modal
        self.assertIn('id="calendar-error"', html)       # fail-closed banner
        self.assertIn('id="calendar-user"', html)        # child selector
        self.assertIn(KID, html)
        # the JS config block carries the labels + app tint map
        self.assertIn('id="calendar-config"', html)
        self.assertIn('dayCodes', html)
        self.assertIn('appCategories', html)

    def test_week_month_views_and_now_indicator_in_js(self):
        src = CALENDAR_JS.read_text(encoding='utf-8')
        self.assertIn("initialView: 'timeGridWeek'", src)
        self.assertIn("'timeGridWeek,dayGridMonth'", src)
        self.assertIn('nowIndicator: true', src)
        # polling contract: gentle 60 s + refetch on interaction
        self.assertIn('REFRESH_MS = 60000', src)
        self.assertIn('calendar.refetchEvents()', src)

    def test_bonus_band_is_derived_and_read_only(self):
        src = CALENDAR_JS.read_text(encoding='utf-8')
        self.assertIn('editable: !row.derived', src)
        self.assertIn('showBonusHint', src)
        # the hint points at the page carrying the shared bonus dialog
        self.assertIn('/user/', src)

    def test_child_selector_switches_user_param(self):
        resp = _render_calendar(self.client)
        html = resp.get_data(as_text=True)
        self.assertIn('switchCalendarUser', html)
        src = CALENDAR_JS.read_text(encoding='utf-8')
        self.assertIn("u.searchParams.set('user', name)", src)

    def test_empty_account_list_renders_empty_state(self):
        resp = _render_calendar(self.client, users=[], live=None)
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        self.assertIn('No child accounts found', html)
        self.assertNotIn('data-pc-calendar', html)

    def test_no_external_assets_rendered(self):
        html = _render_calendar(self.client).get_data(as_text=True)
        self.assertIsNone(EXTERNAL_ASSET.search(html))

    def test_time_rules_page_is_gone_and_nav_says_calendar(self):
        resp = self.client.get('/time-rules')
        self.assertEqual(404, resp.status_code)
        html = _render_calendar(self.client).get_data(as_text=True)
        sidebar = html[html.index('class="pc-sidebar"'):]
        sidebar = sidebar[:sidebar.index('</aside>')]
        self.assertIn('aria-current="page"', sidebar)
        self.assertIn('Calendar', sidebar)


# ---------------------------------------------------------------------------
# 3. E2E over /api/schedule — exactly the modal's payloads.
# ---------------------------------------------------------------------------

class _CalHomeMixin:
    """All four state homes in one temp dir (production layout)."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        for target, attr, name in (
                (app, 'BEDTIME_FILE', 'bedtime.json'),
                (budgets, 'OVERRIDES_FILE', 'overrides.json'),
                (budgets, 'BUDGETS_FILE', 'budgets.json'),
                (budgets, 'SCHEDULE_FILE', 'schedule.json')):
            patcher = mock.patch.object(target, attr,
                                        os.path.join(home, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        # The bedtime tail may lock/unlock: pin the command seams (the one
        # argv-level test wraps the REAL run_lock_command captured here).
        self._real_run_lock_command = app.run_lock_command
        for name, ret in (('run_lock_command', (True, 'ok')),
                          ('get_lock_status', (False, '')),
                          ('_safe_notify', None)):
            patcher = mock.patch.object(app, name, return_value=ret)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(budgets, 'is_admin_protected',
                                    return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def doc(self, username=KID):
        return budgets.load_schedule().get(username, {})

    def entry(self, username=KID):
        return app.load_bedtime().get(username, {})

    def post_block(self, **over):
        now = datetime.now()
        data = {
            'username': KID,
            'target': 'games',
            'mode': 'capped',
            'allowance_minutes': '45',
            'preset': 'DAILY',
            'dtstart': (now - timedelta(days=1)).strftime('%Y-%m-%d'),
            'start': '16:00',
            'end': '18:00',
            'enabled': '1',
        }
        data.update(over)
        return self.authed_post('/api/schedule', data=data)

    def events(self, days_ahead=3):
        now = datetime.now()
        return self.client.get(
            '/api/schedule/events?username=' + KID
            + '&start=' + now.strftime('%Y-%m-%d')
            + '&end=' + (now + timedelta(days=days_ahead)).strftime('%Y-%m-%d')
        ).get_json()


class ScheduleEditorE2ETest(_AdminSessionMixin, _CalHomeMixin,
                            unittest.TestCase):
    def test_create_edit_disable_delete_games_block(self):
        resp = self.post_block(id='b-game')
        self.assertEqual(200, resp.status_code)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual('b-game', payload['block']['id'])
        # the derived windows mirror exists (legacy shape, config truth is
        # the schedule doc)
        self.assertIn('b-game', budgets.load_overrides()[KID]['windows'])

        # events expand the DAILY series once per day in the 3-day range
        rows = self.events()['events']
        self.assertEqual(3, len(rows))
        self.assertEqual({'b-game'}, {r['block_id'] for r in rows})
        self.assertTrue(all(r['derived'] is False for r in rows))

        # cap-edit (resize in the UI posts the same field set with the id)
        resp = self.post_block(id='b-game', allowance_minutes='60')
        self.assertEqual(200, resp.status_code)
        self.assertEqual(60, self.doc()['events']['b-game']
                         ['allowance_minutes'])

        # disable via the modal's Enabled toggle
        resp = self.post_block(id='b-game', enabled='0')
        self.assertEqual(200, resp.status_code)
        self.assertFalse(self.doc()['events']['b-game']['enabled'])
        # disabled blocks stop expanding for the grid
        self.assertEqual([], self.events()['events'])

        # delete-series (the modal's confirmed DELETE)
        resp = self.client.delete('/api/schedule/b-game?username=' + KID,
                                  headers=self.csrf_headers)
        self.assertEqual(200, resp.status_code)
        self.assertEqual({}, self.doc().get('events', {}))
        self.assertEqual([], self.events()['events'])

    def test_create_bedtime_series_via_modal_fields(self):
        # "create Bedtime 21:00-08:00 DAILY" — the VERIFY-ON-VM flow, minus
        # the wall-clock lottery: the flag is driven through the real
        # predicate with a NOW-anchored window instead of fixed times.
        now = datetime.now()
        start = (now - timedelta(hours=1)).strftime('%H:%M')
        end = (now + timedelta(hours=1)).strftime('%H:%M')
        resp = self.post_block(target='bedtime', mode='',
                               allowance_minutes='', id='b-bed',
                               start=start, end=end)
        self.assertEqual(200, resp.status_code)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        # the old writer's immediate-enforcement tail rode this write:
        self.assertTrue(payload['locked'])
        block = self.doc()['events']['b-bed']
        self.assertIsNone(block['mode'])
        # bedtime writes the entry mirror (reads for the live chips)
        self.assertEqual((block['start'], block['end']),
                         (self.entry()['start'], self.entry()['end']))

    def test_overlap_rejection_surfaces_the_conflicting_id(self):
        self.assertEqual(200, self.post_block(id='b-a').status_code)
        resp = self.post_block(id='b-b', start='17:00', end='19:00')
        self.assertEqual(400, resp.status_code)
        # The modal renders THIS string inline (no vanishing alert).
        self.assertIn('b-a', resp.get_json()['error'])

    def test_validation_errors_return_400_not_a_state_write(self):
        resp = self.post_block(preset='WEEKLY:BYDAY=')
        self.assertEqual(400, resp.status_code)
        resp = self.post_block(start='25:00')
        self.assertEqual(400, resp.status_code)
        resp = self.post_block(target='bogus-target')
        self.assertEqual(400, resp.status_code)
        self.assertEqual({}, self.doc().get('events', {}))

    def test_derived_bonus_band_reported_read_only(self):
        # Seed a bedtime row with a live bonus (state stays live-state —
        # the events endpoint only reports it, CALENDAR_DESIGN.md §5).
        now = datetime.now()
        app.save_bedtime({KID: {
            'start': '21:00', 'end': '08:00', 'days': [0, 1, 2, 3, 4, 5, 6],
            'locked_by_bedtime': False, 'manual_override': False,
            'bonus_until': (now + timedelta(minutes=30)).isoformat(),
            'bonus_minutes': 30, 'bonus_granted_by': 'qaadmin',
            'bonus_granted_at': now.isoformat(),
        }})
        rows = self.events()['events']
        derived = [r for r in rows if r.get('derived')]
        self.assertEqual(1, len(derived))
        self.assertEqual('bonus', derived[0]['block_id'])
        self.assertEqual('bedtime', derived[0]['target'])

    def test_events_range_validation_errors(self):
        resp = self.client.get('/api/schedule/events?username=' + KID
                               + '&start=nonsense')
        self.assertEqual(400, resp.status_code)
        resp = self.client.get('/api/schedule/events?username=' + KID
                               + '&start=2026-01-02&end=2026-01-01')
        self.assertEqual(400, resp.status_code)

    def test_bedtime_lock_path_surfaces_locked_true(self):
        """The retired POST /api/bedtime auto-locked an in-window save;
        the same command argv is asserted on the schedule API now."""
        calls = []

        def recorder(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args') or []
            calls.append(list(cmd))

            class R:
                returncode = 0
                stdout = ''
                stderr = ''
            return R()

        now = datetime.now()
        start = (now - timedelta(hours=1)).strftime('%H:%M')
        end = (now + timedelta(hours=1)).strftime('%H:%M')
        with mock.patch.object(app, 'run_lock_command',
                               wraps=self._real_run_lock_command), \
             mock.patch.object(app.subprocess, 'run', side_effect=recorder), \
             mock.patch.object(app, '_safe_notify'):
            resp = self.post_block(target='bedtime', mode='',
                                   allowance_minutes='',
                                   start=start, end=end)
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.get_json()['locked'])
        self.assertTrue(any(c[:2] == ['sudo', 'usermod'] and '-L' in c
                            for c in calls), calls)
        self.assertTrue(self.entry()['locked_by_bedtime'])


if __name__ == '__main__':
    unittest.main()
