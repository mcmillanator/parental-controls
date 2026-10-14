#!/usr/bin/env python3
"""Server-render checks for the mobile-first Home + auth conversion.

Covers UI_UX_PLAN.md Phase 1b (auth shell) and Phase 2 (Home cards):

* Home renders the `.pc-summary` KPI strip, a `data-label`ed
  `.pc-table--stack`, the status pill + one primary action, and a
  `window.pcConfirm` dialog wiring instead of `window.confirm()`.
* The admin list now lives INSIDE the content container.
* `login.html` / `change_password.html` use the `.pc-auth` shell (no nav)
  and render inline errors.
* No page reaches off-box (no `http(s)://` asset URL) and the auth pages
  keep every id/name the auth tests pin.

Server renders go through the test_lock_regression admin session harness
(temp auth store, mocked wheel/system, mocked live data); nothing touches a
real account, the service, /etc, /opt, or the checkout.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import app
from test_lock_regression import _AdminSessionMixin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, 'templates')

CONVERTED_PAGES = ('index.html', 'login.html', 'change_password.html')

USERS = [
    {'username': 'kidlocked', 'uid': 1001, 'home': '/home/kidlocked',
     'shell': '/bin/bash'},
    {'username': 'kidfree', 'uid': 1002, 'home': '/home/kidfree',
     'shell': '/bin/bash'},
    {'username': 'kidunknown', 'uid': 1003, 'home': '/home/kidunknown',
     'shell': '/bin/bash'},
]
LOCK = {
    'kidlocked': (True, 'Locked'),
    'kidfree': (False, 'Unlocked'),
    'kidunknown': (None, 'Unknown'),
}
# A bedtime entry for kidfree; in_bedtime is mocked deterministically below.
BEDTIME = {
    'kidfree': {'start': '21:30', 'end': '07:00',
                'days': [0, 1, 2, 3, 4], 'manual_override': True},
}


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class HomeRenderTest(_AdminSessionMixin, unittest.TestCase):
    """The converted Home renders the card/stack layout and dialog wiring."""

    def _render(self, query=''):
        with mock.patch.object(app, 'get_human_users', return_value=USERS), \
             mock.patch.object(app, 'load_bedtime', return_value=BEDTIME), \
             mock.patch.object(app, 'get_lock_status',
                               side_effect=lambda u: LOCK[u]), \
             mock.patch.object(app, 'is_in_bedtime',
                               side_effect=lambda e: bool(e and e.get('manual_override'))), \
             mock.patch.object(app, 'bonus_state', return_value={'active': False}):
            resp = self.client.get('/' + query)
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    # ------------------------------------------------------------ layout
    def test_summary_kpi_strip_has_counts(self):
        html = self._render()
        self.assertIn('class="pc-summary"', html)
        self.assertIn('id="summary-total">3<', html)
        self.assertIn('id="summary-locked">1<', html)
        self.assertIn('id="summary-bedtime">1<', html)

    def test_accounts_are_cards_with_pinned_hooks(self):
        # REVISED-SPEC.md Screen 1: accounts render as cards, not a table.
        html = self._render()
        self.assertIn('class="pc-stack pc-accounts" id="accounts"', html)
        self.assertIn('class="pc-card pc-account"', html)
        self.assertIn('data-label="Actions"', html)
        for user in USERS:
            with self.subTest(user=user['username']):
                self.assertIn('id="row-%s"' % user['username'], html)

    def test_every_user_renders_a_card_with_pinned_hooks(self):
        html = self._render()
        for user in USERS:
            self.assertIn('id="row-%s"' % user['username'], html)
        self.assertIn('class="pc-btn pc-btn--ghost btn-details"', html)
        self.assertIn('btn-lock', html)
        self.assertIn('btn-unlock', html)
        # status pill + the preserved status colour class tokens
        self.assertIn('pc-status--locked', html)
        self.assertIn('status-locked', html)

    def test_bedtime_is_rendered_12h(self):
        html = self._render()
        self.assertIn('9:30 PM', html)
        self.assertIn('7:00 AM', html)

    def test_refresh_info_text_is_preserved(self):
        html = self._render()
        self.assertIn('<p class="refresh-info">'
                      'Status updates automatically every 10 seconds</p>', html)

    # ------------------------------------------------------ admin panel
    def test_admin_panel_is_inside_the_page_container(self):
        html = self._render()
        shell = html.index('class="pc-shell')
        admin = html.index('id="admin-panel"')
        main_end = html.index('</main>')
        self.assertLess(shell, admin, 'admin panel must follow the container')
        self.assertLess(admin, main_end,
                        'admin panel must be INSIDE <main>, not full-bleed')
        self.assertIn('id="admin-list"', html)
        self.assertIn('id="new-admin-username"', html)
        self.assertIn('id="admin-panel" open', html,
                      'kept open so a runtime verifyElementVisible resolves')

    def test_empty_state_renders_when_no_users(self):
        with mock.patch.object(app, 'get_human_users', return_value=[]), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             mock.patch.object(app, 'is_in_bedtime', return_value=False), \
             mock.patch.object(app, 'bonus_state', return_value={'active': False}):
            html = self.client.get('/').get_data(as_text=True)
        self.assertIn('pc-empty', html)
        self.assertIn('No users', html)

    # ----------------------------------------------------------- dialog
    def test_destructive_actions_use_the_native_dialog(self):
        html = self._render()
        # the shared confirm dialog + helper are pulled in from base.html
        self.assertIn('id="pc-confirm"', html)
        self.assertIn('window.pcConfirm(', html)
        # Progressive enhancement only: the dialog gates a real POST form,
        # it never replaces the server round-trip.
        self.assertIn('form.submit()', html)
        # The shared dialog is a real form with a CSRF token (the reusable
        # confirmation-dialog pattern the canvas asks for).
        self.assertIn('class="pc-dialog__form" method="post"', html)
        self.assertIn('name="csrf_token"', html)
        # The lock action uses the example's acknowledgement checkbox.
        self.assertIn("checkbox: 'Confirm lock action'", html)

    def test_no_external_asset_urls(self):
        html = self._render()
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)

    # ------------------------------------------------- static source
    def test_converted_scripts_do_not_call_window_confirm(self):
        # base.html's dialog partial keeps a `window.confirm` *fallback* for
        # environments without <dialog>; the converted page scripts must not.
        for page in CONVERTED_PAGES:
            src = read(os.path.join(TEMPLATE_DIR, page))
            with self.subTest(page=page):
                self.assertNotIn('window.confirm', src)
                self.assertNotIn('confirm(', src)


class AuthPagesRenderTest(_AdminSessionMixin, unittest.TestCase):
    """login / change-password use the offline `.pc-auth` shell."""

    def test_login_renders_the_auth_shell(self):
        fresh = app.app.test_client()
        resp = fresh.get('/login')
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        self.assertIn('class="pc-auth"', html)
        self.assertIn('id="username"', html)
        self.assertIn('id="password"', html)
        self.assertIn('>Log in<', html)
        self.assertIn('autocomplete="username"', html)
        self.assertIn('autocomplete="current-password"', html)
        # password-manager friendly: the login form no longer forbids autofill
        # (the shared confirm dialog keeps autocomplete=off on its own field).
        self.assertNotIn('autocomplete="off"',
                         read(os.path.join(TEMPLATE_DIR, 'login.html')))

    def test_login_has_no_nav_bars(self):
        fresh = app.app.test_client()
        html = fresh.get('/login').get_data(as_text=True)
        self.assertIn('pc-body--auth', html)
        # The app bar / tab bar are hidden by the shared app.css rule, not by an
        # ad-hoc inline <style> block on the auth page (UI_UX_PLAN.md Phase 5).
        self.assertNotIn('<style>', html)
        css = read(os.path.join(ROOT, 'static', 'css', 'app.css'))
        self.assertIn('.pc-body--auth .pc-appbar', css)
        self.assertIn('.pc-body--auth .pc-tabbar', css)

    def test_change_password_renders_the_auth_shell(self):
        resp = self.client.get('/change-password')
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        self.assertIn('class="pc-auth"', html)
        for field in ('current', 'new', 'confirm'):
            self.assertIn('id="%s"' % field, html)
            self.assertIn('name="%s"' % field, html)
        self.assertIn('>Change password<', html)
        self.assertIn('name="csrf_token"', html)

    def test_change_password_forced_notice_keeps_info_class(self):
        with mock.patch.object(app.auth, 'is_must_change', return_value=True):
            html = self.client.get('/change-password').get_data(as_text=True)
        self.assertIn('class="pc-banner pc-banner--info info"', html)
        self.assertIn('You must change your password before continuing', html)


class ConvertedPageStaticTest(unittest.TestCase):
    """Cheap static guardrails on the converted templates."""

    def test_pages_extend_base_and_stay_offline(self):
        for page in CONVERTED_PAGES:
            src = read(os.path.join(TEMPLATE_DIR, page))
            with self.subTest(page=page):
                self.assertIn('{% extends "base.html" %}', src)
                self.assertNotIn('http://', src)
                self.assertNotIn('https://', src)

    def test_index_keeps_the_pinned_id_inventory(self):
        src = read(os.path.join(TEMPLATE_DIR, 'index.html'))
        for token in ('id="row-', 'id="admin-panel"', 'id="admin-list"',
                      'id="new-admin-username"', 'class="refresh-info"',
                      '.btn-details', 'btn-lock', 'btn-unlock'):
            self.assertIn(token, src, 'index.html lost %r' % token)

    def test_index_keeps_the_devices_selector_markup(self):
        src = read(os.path.join(TEMPLATE_DIR, 'index.html'))
        for token in ('id="device-selector"', 'id="device-select"',
                      'switchDevice', 'device-error'):
            self.assertIn(token, src)


class OverviewDesignTest(_AdminSessionMixin, unittest.TestCase):
    """design-exports/SPEC.md Screen 1 elements on the Overview page."""

    def _render(self, wheel=(), query=''):
        with mock.patch.object(app, 'get_human_users', return_value=USERS), \
             mock.patch.object(app, 'load_bedtime', return_value=BEDTIME), \
             mock.patch.object(app, 'get_lock_status',
                               side_effect=lambda u: LOCK[u]), \
             mock.patch.object(app, 'is_in_bedtime',
                               side_effect=lambda e: bool(e and e.get('manual_override'))), \
             mock.patch.object(app, 'bonus_state', return_value={'active': False}), \
             mock.patch.object(app.auth, 'is_wheel_member',
                               side_effect=lambda u: u == self.ADMIN_USER or u in wheel):
            resp = self.client.get('/' + query)
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def test_h1_is_overview_and_subtitle_keeps_login_line(self):
        html = self._render()
        self.assertIn('<h1 class="pc-page-title">Overview</h1>', html)
        self.assertIn('Logged in as', html)

    def test_system_summary_card_with_online_pill(self):
        html = self._render()
        self.assertIn('id="summary-heading">System summary<', html)
        self.assertIn('human accounts', html)
        # REVISED-SPEC.md: explicit green Online pill.
        self.assertIn('pc-pill pc-pill--online', html)
        self.assertIn('>Online<', html)

    def test_offline_sync_banner_has_retry(self):
        html = self._render()
        self.assertIn('id="sync-banner"', html)
        self.assertIn('#pc-i-wifi-off', html)
        self.assertIn('Local connection healthy', html)
        self.assertIn('onclick="refreshTable()">Retry<', html)

    def test_wheel_account_is_protected_with_disabled_actions(self):
        html = self._render(wheel=('kidfree',))
        idx = html.index('id="row-kidfree"')
        card = html[idx:html.index('</article>', idx)]
        self.assertIn('badge-protected', card)
        self.assertIn('Protected', card)
        self.assertIn('wheel admins can never be locked or removed', card)
        self.assertNotIn('btn-lock', card)
        self.assertNotIn('btn-unlock', card)
        self.assertNotIn('btn-logout', card)
        # REVISED-SPEC.md: all three actions render disabled for a wheel admin.
        self.assertIn('disabled aria-disabled="true"', card)
        self.assertIn('>Lock<', card)
        self.assertIn('>Unlock<', card)
        # a non-wheel card still carries the pinned actions
        idx2 = html.index('id="row-kidlocked"')
        card2 = html[idx2:html.index('</article>', idx2)]
        self.assertIn('btn-unlock', card2)
        self.assertIn('btn-lockscreen', card2)
        self.assertIn('btn-logout', card2)

    def test_emergency_logout_requires_typed_confirmation(self):
        # Overview actions are plain POST forms (progressive enhancement):
        # the page script only gates them behind the shared confirm dialog.
        src = read(os.path.join(TEMPLATE_DIR, 'index.html'))
        self.assertIn('requireText: username', src)
        self.assertIn("url_for('overview_lock_screen')", src)
        self.assertIn("url_for('overview_logout')", src)
        # ...and every action posts to its own no-JS form endpoint.
        html = self._render()
        for action in ('/overview/lock', '/overview/lock-screen',
                       '/overview/unlock', '/overview/logout'):
            with self.subTest(action=action):
                self.assertIn('action="%s"' % action, html)

    def test_specimen_cards_are_dev_only(self):
        # The "Selected account: Sam" example, the design thesis and the empty
        # card are design fixtures: they render only behind ?preview=1.
        normal = self._render()
        self.assertNotIn('id="overview-specimens"', normal)
        self.assertNotIn('Selected account: Sam', normal)
        self.assertNotIn('Design thesis', normal)
        preview = self._render(query='?preview=1')
        self.assertIn('id="overview-specimens"', preview)
        self.assertIn('Selected account: Sam', preview)
        self.assertIn('Confirm lock action', preview)
        self.assertIn('Design thesis', preview)
        self.assertIn('destructive actions separated and confirmed', preview)
        self.assertIn('No human accounts found', preview)
        self.assertIn('Check the machine connection', preview)


if __name__ == '__main__':
    unittest.main()
