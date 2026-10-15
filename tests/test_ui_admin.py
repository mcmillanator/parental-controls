#!/usr/bin/env python3
"""Mobile-first Admin Console conversion (UI_UX_PLAN.md §5.5 / Phase 4).

The last desktop-shaped page now extends the shared shell and lays itself out
as two labeled card stacks: the audit log (`.pc-table--stack` + `data-label`s,
local-day grouping + a busy "Load more") and the admin accounts list (chips
with a "Protected" wheel pill). Destructive "Remove" goes through the native
`<dialog>` helper (`window.pcConfirm`), and the one-time password is a
`<dialog>` too.

This module pins what the redesign must *not* lose, in two layers:

* source-level guards on ``templates/admin_audit.html`` (extends base, offline,
  no inline CSS, no ``window.confirm``),
* server-rendered checks through the mocked admin-session harness (temp audit
  store, mocked wheel membership): stacked tables + labels, the chain-status
  banner (ok + broken), every §7.4 pinned id/class/attr/text, the load-more
  control with its busy state, and the OTP dialog.

Nothing here touches a real account, the service, ``/etc``, ``/opt`` or the
checkout; renders run against temp stores only.
"""

import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import audit
from test_lock_regression import _AdminSessionMixin, KID

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, 'templates')
TEMPLATE = os.path.join(TEMPLATE_DIR, 'admin_audit.html')

# A jsdelivr/Bootstrap CDN <link> is exactly the failure this guards against.
EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''', re.I)

# UI_UX_PLAN.md §7.4 + tests/test_admin_console.py + the stdlib suite.
PINNED_IDS = (
    'audit-section', 'chain-status', 'audit-count', 'audit-limit',
    'audit-table', 'load-more', 'admin-section', 'admin-table',
    'onetime-password', 'otp-user', 'otp-value', 'new-admin-username',
    'admin-add', 'toast',
)

PINNED_CLASSES = (
    'audit-row', 'audit-time', 'audit-detail', 'admin-row', 'chain-intact',
    'chain-broken', 'badge-protected', 'btn-lock', 'btn-unlock', 'subtitle',
    'refresh-info',
)

PINNED_ATTRS = (
    'data-seq', 'data-at', 'data-username', 'data-label', 'aria-busy',
)

PINNED_TEXT = (
    'Admin Console', 'Audit chain intact', 'Audit Log', 'Admin Accounts',
    'Load more entries', 'One-time app password', "I've saved it",
    'No audit entries yet.', 'Up to 1000 entries shown',
    'Protected — wheel admin',
)

# The audit + admin stacked tables must carry these per-cell labels.
AUDIT_LABELS = ('Seq', 'Time (local)', 'Actor', 'Action', 'Target', 'Detail')
ADMIN_LABELS = ('Username', 'Tier', 'Status', 'Actions')


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class _AuditEnvMixin:
    """Point the audit log at a temp file for each test (mirrors
    tests/test_admin_console.py's harness, kept local on purpose)."""

    def setUp(self):
        super().setUp()
        self._audit_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._audit_tmp.cleanup)
        self._audit_path = os.path.join(self._audit_tmp.name, 'audit.jsonl')
        os.environ['PARENTAL_CONTROLS_AUDIT'] = self._audit_path
        self.addCleanup(lambda: os.environ.pop('PARENTAL_CONTROLS_AUDIT',
                                               None))


class AdminConsoleRenderTest(_AdminSessionMixin, _AuditEnvMixin,
                            unittest.TestCase):
    """Logged-in wheel admin (qaadmin) + a temp audit store."""

    def page(self, query=''):
        resp = self.client.get('/admin/audit' + query)
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def log(self, action='account.lock', target=KID, detail=None):
        audit.log(self.ADMIN_USER, action, target, detail)

    def _break_first_entry(self):
        with open(self._audit_path) as fh:
            lines = fh.readlines()
        obj = json.loads(lines[0])
        obj['target'] = 'mallory'
        lines[0] = json.dumps(obj) + '\n'
        with open(self._audit_path, 'w') as fh:
            fh.write(''.join(lines))

    # ------------------------------------------------------------ offline
    def test_no_external_assets_in_the_rendered_page(self):
        self.log()
        html = self.page()
        self.assertIsNone(EXTERNAL_ASSET.search(html),
                          'rendered page references an external asset')
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)
        self.assertIn('/static/css/app.css', html)
        self.assertIn('/static/vendor/bootstrap/bootstrap.min.css', html)

    # -------------------------------------------------------- stacked tables
    def test_audit_table_is_a_labeled_card_stack(self):
        self.log('account.lock')
        html = self.page()
        self.assertIn('pc-table pc-table--stack', html)
        self.assertIn('id="audit-table"', html)
        self.assertIn('<thead>', html)
        for label in AUDIT_LABELS:
            with self.subTest(label=label):
                self.assertIn('data-label="%s"' % label, html,
                              'audit card stack needs a "%s" label' % label)

    def test_admin_table_is_a_labeled_card_stack(self):
        html = self.page()
        self.assertIn('id="admin-table"', html)
        for label in ADMIN_LABELS:
            with self.subTest(label=label):
                self.assertIn('data-label="%s"' % label, html)
        # two semantic theads stay in the DOM (clipped on phones, not removed)
        self.assertGreaterEqual(html.count('<thead>'), 2)

    def test_audit_rows_keep_their_raw_local_time_hook(self):
        self.log()
        html = self.page()
        # The store value is shipped raw in data-at and upgraded client-side.
        self.assertRegex(html, r'class="audit-time" data-at="[^"]+"')
        self.assertIn("td.audit-time[data-at]", read(TEMPLATE))

    # ------------------------------------------------------- chain status
    def test_chain_status_is_an_ok_banner(self):
        self.log()
        html = self.page()
        self.assertIn('id="chain-status"', html)
        self.assertIn('pc-banner pc-banner--ok chain-intact', html)
        self.assertIn('Audit chain intact', html)
        self.assertIn('role="status"', html)

    def test_chain_status_is_a_danger_banner_when_broken(self):
        self.log()
        self.log('account.unlock')
        self._break_first_entry()
        html = self.page()
        self.assertIn('pc-banner pc-banner--danger chain-broken', html)
        self.assertIn('Audit chain broken', html)
        self.assertIn('first bad entry: seq 1', html)

    # ------------------------------------------------------- load more
    def test_load_more_control_and_busy_state(self):
        for i in range(3):
            self.log('action.%d' % i)
        html = self.page('?limit=2')
        self.assertIn('id="load-more"', html)
        self.assertIn('Load more entries', html)
        self.assertIn('entries (limit 2)', html)
        # busy state: a skeleton status region + aria-busy owned by the JS
        self.assertIn('id="load-more-status"', html)
        src = read(TEMPLATE)
        self.assertIn("btn.setAttribute('aria-busy', 'true')", src)
        self.assertIn("btn.setAttribute('aria-busy', 'false')", src)

    def test_load_more_is_disabled_when_everything_is_loaded(self):
        self.log()
        html = self.page()
        idx = html.index('id="load-more"')
        self.assertIn('disabled', html[idx:idx + 120])

    # --------------------------------------------------- one-time password
    def test_onetime_password_is_a_dialog_with_pinned_ids(self):
        html = self.page()
        self.assertIn('id="onetime-password"', html)
        self.assertIn('<dialog', html)
        self.assertIn('pc-dialog', html)
        self.assertIn('otp-box', html)
        self.assertIn('id="otp-user"', html)
        self.assertIn('id="otp-value"', html)
        self.assertIn("I've saved it", html)
        src = read(TEMPLATE)
        self.assertIn('function showOtp(', src)
        self.assertIn('function dismissOtp(', src)
        self.assertIn('showModal()', src)

    # ------------------------------------------------------- dialog wiring
    def test_remove_uses_the_native_dialog_helper(self):
        src = read(TEMPLATE)
        self.assertIn('window.pcConfirm(', src)
        self.assertNotIn('window.confirm(', src)
        self.assertNotIn('!confirm(', src)
        # the shared confirm dialog is on every page that extends base.html
        html = self.page()
        self.assertIn('id="pc-confirm"', html)

    # ------------------------------------------------------ pinned contract
    def test_all_pinned_ids_present(self):
        self.log()
        html = self.page()
        for pinned in PINNED_IDS:
            with self.subTest(id=pinned):
                self.assertIn('id="%s"' % pinned, html)

    def test_all_pinned_classes_present(self):
        self.log()
        html = self.page()
        for cls in PINNED_CLASSES:
            with self.subTest(cls=cls):
                self.assertIn(cls, html)

    def test_pinned_attrs_and_text_present(self):
        self.log()
        html = self.page()
        for attr in PINNED_ATTRS:
            with self.subTest(attr=attr):
                self.assertIn(attr, html)
        for text in PINNED_TEXT:
            with self.subTest(text=text):
                self.assertIn(text, html)

    # --------------------------------------------------------- admin rows
    def test_admin_row_is_a_chip_with_a_protected_wheel_pill(self):
        html = self.page()
        idx = html.index('data-username="%s"' % self.ADMIN_USER)
        row = html[idx:html.index('</tr>', idx)]
        self.assertIn('badge-protected', row)
        self.assertIn('Protected', row)
        self.assertIn('pc-chip', row)
        # The chip must carry `pc-chip--ok` so the overflow guard on
        # `.pc-chip` applies to this long label (mobile scroll regression).
        self.assertIn('pc-chip--ok', row)

    def test_admin_add_form_is_a_wheel_only_disclosure(self):
        html = self.page()
        self.assertIn('id="admin-add"', html)
        self.assertIn('<details class="pc-disclosure" id="admin-add">', html)
        self.assertIn('id="new-admin-username"', html)

    # ------------------------------------------------------- empty states
    def test_empty_audit_list_renders(self):
        # No audit entries logged; only the qaadmin row exists in the store.
        html = self.page()
        self.assertIn('No audit entries yet.', html)

    def test_day_grouping_hook_is_wired(self):
        src = read(TEMPLATE)
        # Local-day group headings are inserted client-side (UTC/local parity
        # cannot be decided server-side), spliced before the first row of each
        # local calendar day.
        self.assertIn('function decorateRows()', src)
        self.assertIn("'<tr class=\"audit-day\" data-day=\"'", src)
        self.assertIn('function auditDayKey(at)', src)


class AdminConsoleSourceTest(unittest.TestCase):
    """Static guards on templates/admin_audit.html itself (no Flask)."""

    @classmethod
    def setUpClass(cls):
        cls.src = read(TEMPLATE)

    def test_extends_the_shared_shell(self):
        self.assertTrue(self.src.lstrip().startswith('{% extends "base.html" %}'),
                        'admin_audit.html must extend templates/base.html')
        self.assertIn('{% block content %}', self.src)
        self.assertIn('{% block scripts %}', self.src)

    def test_has_no_doctype_head_or_inline_style_of_its_own(self):
        self.assertNotIn('<!DOCTYPE html>', self.src)
        self.assertNotIn('<head>', self.src)
        self.assertNotIn('</body>', self.src)
        # the CDN link and the duplicated inline theme block are gone
        self.assertNotIn('<style', self.src)

    def test_has_no_external_asset_urls(self):
        self.assertIsNone(EXTERNAL_ASSET.search(self.src),
                          'admin_audit.html must not reference a CDN')
        self.assertNotIn('cdn.jsdelivr.net', self.src)
        self.assertNotIn('http://', self.src)
        self.assertNotIn('https://', self.src)

    def test_no_raw_window_confirm(self):
        # Destructive actions go through the shell's <dialog> helper.
        self.assertNotIn('window.confirm(', self.src)
        self.assertNotIn('!confirm(', self.src)
        self.assertIn('pcConfirm(', self.src)

    def test_data_at_literal_is_preserved(self):
        # test_time12h pins this exact source string + the JS upgrade pass.
        self.assertIn('data-at="{{ e.at or \'\' }}"', self.src)
        self.assertIn('cell.textContent = formatAuditAt', self.src)

    def test_audit_header_keeps_the_pinned_literal(self):
        self.assertIn('<th>Time (local)</th>', self.src)


class AdminConsoleDesignTest(_AdminSessionMixin, _AuditEnvMixin,
                             unittest.TestCase):
    """design-exports/SPEC.md Screen 5 additions on the admin console."""

    def page(self, query=''):
        resp = self.client.get('/admin/audit' + query)
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def test_export_button_and_toast_specimen(self):
        html = self.page()
        self.assertIn('id="export-audit"', html)
        self.assertIn('Export audit log', html)
        self.assertIn('Audit exported', html)
        self.assertIn('exportAudit', read(TEMPLATE))

    def test_integrity_column_and_verified_chips(self):
        audit.log(self.ADMIN_USER, 'account.lock', KID)
        audit.log(self.ADMIN_USER, 'account.unlock', KID)
        html = self.page()
        self.assertIn('data-label="Integrity"', html)
        self.assertIn('id="integrity-summary"', html)
        self.assertIn('2 of 2 entries verified', html)
        self.assertIn('#pc-i-badge-check', html)
        self.assertIn('>Verified<', html)

    def test_filters_present(self):
        html = self.page()
        self.assertIn('id="audit-filters"', html)
        for fid in ('filter-actor', 'filter-action', 'filter-range'):
            with self.subTest(id=fid):
                self.assertIn('id="%s"' % fid, html)
        self.assertIn('Last 7 days', html)
        self.assertIn('Last 30 days', html)
        self.assertIn('applyAuditFilters', read(TEMPLATE))

    def test_admin_accounts_copy_and_add_button(self):
        html = self.page()
        self.assertIn('Access to this machine', html)
        self.assertIn('Add app-only admin', html)
        self.assertIn('#pc-i-user-plus', html)
        self.assertIn('openAddAdmin', read(TEMPLATE))

    def test_remove_confirmation_requires_typed_remove(self):
        src = read(TEMPLATE)
        self.assertIn('Remove app-only admin?', src)
        self.assertIn("requireText: 'REMOVE'", src)

    def test_specimen_cards(self):
        # Design fixtures: dev-only preview, never production content.
        normal = self.page()
        self.assertNotIn('id="admin-specimens"', normal)
        self.assertNotIn('Theme specimen', normal)
        html = self.page('?preview=1')
        self.assertIn('id="admin-specimens"', html)
        self.assertIn('Irreversible actions', html)
        self.assertIn('Theme specimen', html)
        self.assertIn('Component callouts', html)


if __name__ == '__main__':
    unittest.main()
