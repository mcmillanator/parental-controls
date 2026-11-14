#!/usr/bin/env python3
"""12-hour (AM/PM) input/display sweep.

Storage and the /api/bedtime contract stay 24h 'HH:MM'; the UI now
edits/renders clock times 12-hour everywhere the user has to read one:

* app.hhmm_12h_parts: HH:MM -> (hour 1-12, minute, AM/PM) — the server
  twin of the JS compose/display helpers, used to prefill the bedtime
  selects (12 AM/PM edges pinned here).
* templates/user.html: no type="time" inputs remain; both bedtime forms
  (prefilled and no-bedtime-set) render hour/minute/AM-PM selects;
  saveBedtime() composes HH:MM back from them before POSTing (static
  assert on the conversion logic, which mirrors app.hhmm_12h_parts).
* templates/admin_audit.html: audit timestamps format to LOCAL 12-hour
  time client-side in BOTH render paths (server rows via data-at +
  an in-place upgrade pass; loadMore rows in auditRowHtml). The raw UTC
  ISO `at` stays in the API and in data-at — no server-side locale
  string is ever shipped.

Stdlib only. Page renders go through the test_lock_regression admin
session harness (temp auth store, mocked wheel/system) with a mocked
get_user_live / temp audit log. Nothing touches a real account, the
service, /etc, /opt, or the checkout. The live-PAM class in
test_lock_usermod_real may self-skip (no pexpect) — unrelated to this
module.
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import audit
import budgets
from test_lock_regression import _AdminSessionMixin, KID

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), '..', 'templates')


def read_template(name):
    with open(os.path.join(TEMPLATE_DIR, name), encoding='utf-8') as f:
        return f.read()


def selected_values(html, name):
    """Values of the selected <option>s inside <select name="...">.</select>."""
    m = re.search(r'<select name="%s"[^>]*>(.*?)</select>' % re.escape(name),
                  html, re.S)
    if not m:
        raise AssertionError(f'<select name="{name}"> not found in page')
    opts = re.findall(r'<option value="([^"]*)"([^>]*)>', m.group(1))
    return [value for value, attrs in opts if 'selected' in attrs]


# ---------------------------------------------------------------------------
# Server twin of the JS compose/display helpers (unit-testable)
# ---------------------------------------------------------------------------

class TestHhmm12hParts(unittest.TestCase):
    """HH:MM -> 12-hour select parts, incl. the 12 AM/PM edges."""

    def test_mapping(self):
        for hhmm, expected in [
            ('00:00', (12, 0, 'AM')),
            ('00:30', (12, 30, 'AM')),
            ('09:00', (9, 0, 'AM')),
            ('11:59', (11, 59, 'AM')),
            ('12:00', (12, 0, 'PM')),
            ('12:30', (12, 30, 'PM')),
            ('13:05', (1, 5, 'PM')),
            ('21:00', (9, 0, 'PM')),
            ('23:59', (11, 59, 'PM')),
        ]:
            with self.subTest(hhmm=hhmm):
                self.assertEqual(app.hhmm_12h_parts(hhmm), expected)

    def test_empty_and_garbage_never_crash(self):
        for bad in ('', None, 'bogus', '25:00', 'xx:yy'):
            with self.subTest(bad=bad):
                self.assertEqual(app.hhmm_12h_parts(bad), (12, 0, 'AM'))

    def test_compose_round_trip(self):
        """The selects a form would prefill compose back to the stored
        HH:MM (same arithmetic the JS toHHMM uses)."""
        for hhmm in ('00:00', '00:30', '09:00', '11:59', '12:00', '12:30',
                     '13:05', '21:00', '23:59'):
            hour, minute, meridiem = app.hhmm_12h_parts(hhmm)
            h24 = hour % 12
            if meridiem == 'PM':
                h24 += 12
            self.assertEqual(f'{h24:02d}:{minute:02d}', hhmm)

    def test_twelve_parts_filter_shape(self):
        parts = app._twelve_parts('13:05')
        self.assertEqual(parts, {'hour': 1, 'minute': 5, 'meridiem': 'PM'})


# ---------------------------------------------------------------------------
# user.html: 12-hour selects + HH:MM compose
# ---------------------------------------------------------------------------

def live_dict(bedtime):
    return {
        'user': {'username': KID, 'uid': 1001, 'home': '/home/kid',
                 'shell': '/bin/bash', 'locked': False,
                 'status_detail': 'Unlocked', 'bedtime': bedtime,
                 'in_bedtime': False, 'bonus': None},
        'processes': [], 'visible': [], 'hidden': [],
        'groups': {c: [] for c in budgets.CATEGORIES},
        'budget_state': budgets.budget_status(
            KID, {KID: budgets.blank_entry()}),
    }


class TestBedtimeSelects(unittest.TestCase):
    """Calendar cutover re-pin: the bedtime form left templates/user.html;
    the 12-hour select contract now lives in the /calendar modal
    (templates/calendar.html + static/js/calendar.js). No native clock
    picker anywhere; compose back to 24h HH:MM for the API."""

    def _calendar_html(self):
        return read_template('calendar.html')

    def test_static_template_has_no_time_inputs(self):
        for tpl in ('user.html', 'calendar.html'):
            with self.subTest(template=tpl):
                src = read_template(tpl)
                self.assertNotIn('<input type="time"', src)
                self.assertNotIn('type="time"', src)

    def test_rendered_page_has_no_time_inputs(self):
        # The bedtime forms retired from /user; the modal renders selects.
        src = self._calendar_html()
        self.assertIn('{% macro cal_time_select(prefix, label) -%}', src)
        self.assertIn("cal_time_select('block_start', 'Block start')", src)
        self.assertIn("cal_time_select('block_end', 'Block end')", src)

    def test_modal_selects_open_on_a_required_placeholder(self):
        """Both select triples start on an empty disabled '--' option so a
        missing pick blocks the save (calendar.js null-checks the parts)."""
        src = self._calendar_html()
        self.assertIn('<option value="" selected disabled>--</option>', src)
        # The triple is emitted by the macro (name="{{ prefix }}_hour" etc.)
        # and invoked for both fields:
        self.assertIn('name="{{ prefix }}_hour"', src)
        self.assertIn('name="{{ prefix }}_meridiem"', src)

    def test_calendar_js_composes_hhmm_from_selects(self):
        """Static assert: readTimeSelect() turns the selects back into 24h
        HH:MM (the /api/schedule contract), with the 12 AM/PM edges and a
        missing-selection null, mirroring the retired saveBedtime()."""
        js = Path(__file__).resolve().parents[1] / 'static' / 'js' / 'calendar.js'
        src = js.read_text(encoding='utf-8')
        self.assertIn('function readTimeSelect(prefix)', src)
        self.assertIn("if (hour === '' || minute === '' || meridiem === '')",
                      src)
        self.assertIn('let h24 = parseInt(hour, 10) % 12;', src)
        self.assertIn("if (meridiem === 'PM') { h24 += 12; }", src)
        self.assertIn("return pad2(h24) + ':' + pad2(parseInt(minute, 10));",
                      src)

    def test_js_helpers_mirror_server_side(self):
        """fmt12 (display) remains the user page's mirror of format_12h();
        the compose twin moved to calendar.js readTimeSelect (the toHHMM
        helper retired with the bedtime form)."""
        src = read_template('user.html')
        self.assertIn('function fmt12(hhmm)', src)
        self.assertNotIn('function toHHMM(', src)
        self.assertNotIn('<input type="time"', src)

    def test_prefill_mapping_is_client_side_now(self):
        # The modal prefills from the stored block (HH:MM -> hour/minute/
        # meridiem selects) in setTimeSelect(); the server-side twelve_parts
        # filter keeps its unit coverage above for any future consumer.
        js = Path(__file__).resolve().parents[1] / 'static' / 'js' / 'calendar.js'
        src = js.read_text(encoding='utf-8')
        self.assertIn('function setTimeSelect(prefix, hhmmStr)', src)
        self.assertIn("const mer = h >= 12 ? 'PM' : 'AM';", src)
        self.assertIn('h = h % 12; if (h === 0) { h = 12; }', src)


# ---------------------------------------------------------------------------
# admin_audit.html: client-side LOCAL 12-hour audit timestamps
# ---------------------------------------------------------------------------

class _AuditPageBase(_AdminSessionMixin, unittest.TestCase):
    """Logged-in admin + a temp audit log."""

    def setUp(self):
        super().setUp()
        self._audit_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._audit_tmp.cleanup)
        self._audit_path = os.path.join(self._audit_tmp.name, 'audit.jsonl')
        os.environ['PARENTAL_CONTROLS_AUDIT'] = self._audit_path
        self.addCleanup(lambda: os.environ.pop('PARENTAL_CONTROLS_AUDIT',
                                               None))

    def _log_at(self, at_iso):
        """Append an entry whose `at` is a fixed UTC ISO string."""

        class FakeDateTime:
            @staticmethod
            def now(tz=None):
                return datetime.fromisoformat(at_iso)

        with mock.patch.object(audit, 'datetime', FakeDateTime):
            audit.log('qaadmin', 'account.lock', KID)

    def page_text(self, query=''):
        return self.client.get('/admin/audit' + query).get_data(as_text=True)


class TestAuditPage12h(_AuditPageBase):
    def test_server_rows_carry_raw_at_for_client_side_formatting(self):
        self._log_at('2026-10-11T02:50:43+00:00')
        text = self.page_text()
        # The API/store value stays intact in the server-rendered markup
        # (UI-only change) ...
        self.assertIn('data-at="2026-10-11T02:50:43+00:00"', text)
        self.assertIn('02:50:43', text)
        # ... and no server-side locale string is shipped: the raw at is
        # rendered verbatim; conversion happens in the page JS.
        self.assertNotIn('format_12h(e.at', text)
        self.assertNotIn('e.at|twelve', text)

    def test_header_is_local_not_utc(self):
        self._log_at('2026-10-11T02:50:43+00:00')
        text = self.page_text()
        self.assertIn('<th>Time (local)</th>', text)
        self.assertNotIn('Time (UTC)', text)

    def test_js_formatter_wired_into_both_render_paths(self):
        src = read_template('admin_audit.html')
        # Shared client-side helper: JS Date + local getters + 12h + AM/PM.
        self.assertIn('function formatAuditAt(at)', src)
        self.assertIn('const d = new Date(at);', src)
        self.assertIn('let h = d.getHours();', src)
        self.assertIn('h = h % 12 || 12;', src)
        self.assertIn("padStart(2, '0')", src)
        self.assertIn("AUDIT_MONTHS[d.getMonth()]", src)
        # Month table is complete (deterministic text, no toLocale*).
        self.assertIn("'Jan','Feb','Mar','Apr','May','Jun','Jul'", src)
        self.assertIn("'Aug','Sep','Oct','Nov','Dec'", src)
        # JS render path (loadMore rows via auditRowHtml).
        self.assertIn("formatAuditAt(e.at || '')", src)
        # Server render path: data-at + the in-place upgrade pass.
        self.assertIn('data-at="{{ e.at or \'\' }}"', src)
        self.assertIn('td.audit-time[data-at]', src)
        self.assertIn('cell.textContent = formatAuditAt', src)
        # Unparseable/empty never crashes the page.
        self.assertIn("if (isNaN(d.getTime())) return String(at);", src)


# ---------------------------------------------------------------------------
# Sweep: every clock-time input/display across templates is 12-hour
# ---------------------------------------------------------------------------

class TestTwelveHourSweep(unittest.TestCase):
    def test_no_time_input_anywhere(self):
        for name in sorted(os.listdir(TEMPLATE_DIR)):
            if not name.endswith('.html'):
                continue
            src = read_template(name)
            with self.subTest(template=name):
                self.assertNotIn('type="time"', src)

    def test_bedtime_displays_stay_12h(self):
        # index.html (server + JS refresh paths) and user.html header.
        self.assertIn('user.bedtime.start|twelve', read_template('index.html'))
        self.assertIn('fmt12(u.bedtime.start)', read_template('index.html'))
        self.assertIn('bedtime.start|twelve', read_template('user.html'))
        # Bonus "until" displays are 12h in both render paths.
        self.assertIn('until[11:16]|twelve', read_template('user.html'))
        self.assertIn('fmt12(b.until.slice(11, 16))',
                      read_template('user.html'))

    def test_no_python_locale_time_in_admin_audit(self):
        # The audit page must not pre-format times server-side
        # (deterministic client-side Date rendering only).
        src = read_template('admin_audit.html')
        self.assertNotIn('|twelve', src)
        self.assertNotIn('format_12h', src)


if __name__ == '__main__':
    unittest.main()