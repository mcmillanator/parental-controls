#!/usr/bin/env python3
"""5-section IA shell tests (design-exports/SPEC.md "Shared shell").

Covers the shell commit of the design translation:

* source guards on ``templates/base.html`` and the ``_partials/`` sidebar /
  app bar / tab bar: the 5-section nav (Overview / Time rules / Activity /
  Admin / Settings), the "Selected machine" card, the user chip + theme toggle
  + Help, and the phone bottom tab bar;
* server-rendered checks (mocked admin session) that every section renders
  through the shell with the right ``aria-current`` and stays offline;
* the new routes (``/time-rules``, ``/budgets``, ``/processes``,
  ``/settings``) are reachable and do not disturb the existing endpoints.

Nothing here touches a real account, the service, /etc, /opt or the checkout;
renders run against a temp auth store with mocked live data.
"""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import app
from test_lock_regression import _AdminSessionMixin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, 'templates')
PARTIAL_DIR = os.path.join(TEMPLATE_DIR, '_partials')
APP_CSS = os.path.join(ROOT, 'static', 'css', 'app.css')

EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''', re.I)

SECTIONS = ('Overview', 'Time rules', 'Activity', 'Admin', 'Settings')

USERS = [
    {'username': 'kidlocked', 'uid': 1001, 'home': '/home/kidlocked',
     'shell': '/bin/bash'},
    {'username': 'kidfree', 'uid': 1002, 'home': '/home/kidfree',
     'shell': '/bin/bash'},
]
LOCK = {'kidlocked': (True, 'Locked'), 'kidfree': (False, 'Unlocked')}


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _live(username):
    return {
        'user': {'username': username, 'uid': 1001, 'home': '/home/' + username,
                 'shell': '/bin/bash', 'locked': False, 'status_detail': 'Unlocked',
                 'bedtime': {}, 'in_bedtime': False, 'bonus': {'active': False}},
        'processes': [], 'visible': [], 'hidden': [],
        'groups': {c: [] for c in app.budgets.CATEGORIES},
        'budget_state': {}, 'overrides': None, 'app_names': [],
    }


class ShellRenderTest(_AdminSessionMixin, unittest.TestCase):
    """Every section renders inside the shared 5-section shell."""

    def _render(self, path):
        with mock.patch.object(app, 'get_human_users', return_value=USERS), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             mock.patch.object(app, 'get_lock_status',
                               side_effect=lambda u: LOCK[u]), \
             mock.patch.object(app, 'is_in_bedtime', return_value=False), \
             mock.patch.object(app, 'bonus_state',
                               return_value={'active': False}), \
             mock.patch.object(app, 'get_user_live', side_effect=_live):
            resp = self.client.get(path)
        self.assertEqual(200, resp.status_code, path)
        return resp.get_data(as_text=True)

    def test_every_section_is_reachable_and_renders_the_shell(self):
        for path in ('/', '/time-rules', '/budgets', '/processes',
                     '/admin/audit', '/settings'):
            with self.subTest(path=path):
                html = self._render(path)
                self.assertIn('class="pc-tabbar"', html)
                self.assertIn('id="pc-main"', html)
                if path == '/':
                    # REVISED-SPEC.md Screen 1: Overview is phone-first and
                    # deliberately has no desktop sidebar.
                    self.assertNotIn('class="pc-sidebar"', html)
                    self.assertIn('class="pc-phoneheader"', html)
                else:
                    self.assertIn('class="pc-sidebar"', html)

    def test_sidebar_lists_all_five_sections(self):
        html = self._render('/processes')
        sidebar = html[html.index('class="pc-sidebar"'):]
        sidebar = sidebar[:sidebar.index('</aside>')]
        for label in SECTIONS:
            with self.subTest(label=label):
                self.assertIn(label, sidebar)

    def test_tabbar_lists_four_tabs_plus_account_and_settings(self):
        # REVISED-SPEC.md Screen 1/3: the phone bar is Home/Time/Activity/Admin;
        # Settings lives in the account popover on phones.
        html = self._render('/')
        tabbar = html[html.index('class="pc-tabbar"'):]
        tabbar = tabbar[:tabbar.index('</nav>')]
        for label in ('Home', 'Time', 'Activity', 'Admin', 'Account',
                      'Settings'):
            with self.subTest(label=label):
                self.assertIn(label, tabbar)

    def test_active_section_is_marked_current(self):
        cases = {
            '/time-rules': 'Time rules',
            '/budgets': 'Time rules',
            '/processes': 'Activity',
            '/admin/audit': 'Admin',
            '/settings': 'Settings',
        }
        for path, label in cases.items():
            with self.subTest(path=path):
                html = self._render(path)
                sidebar = html[html.index('class="pc-sidebar"'):]
                sidebar = sidebar[:sidebar.index('</aside>')]
                # the active link is the one carrying aria-current + the label
                current = re.findall(
                    r'<a class="pc-navlink"[^>]*aria-current="page"[^>]*>\s*'
                    r'<svg[^>]*>.*?</svg>\s*<span>([^<]+)</span>', sidebar,
                    re.S)
                self.assertEqual([label], current)

    def test_overview_marks_home_current_in_the_tabbar(self):
        html = self._render('/')
        tabbar = html[html.index('class="pc-tabbar"'):]
        tabbar = tabbar[:tabbar.index('</nav>')]
        current = re.findall(
            r'<a class="pc-tab"[^>]*aria-current="page"[^>]*>\s*'
            r'<svg[^>]*>.*?</svg>\s*<span>([^<]+)</span>', tabbar, re.S)
        self.assertEqual(['Home'], current)

    def test_machine_card_is_scheme_less_and_online(self):
        html = self._render('/processes')
        self.assertIn('Selected machine', html)
        self.assertIn('Local / Online', html)
        self.assertIn('pc-dot--online', html)
        # offline promise: no http(s) literal anywhere in a rendered page
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)

    def test_user_chip_and_theme_and_help_are_present(self):
        html = self._render('/processes')
        self.assertIn('id="pc-theme-toggle"', html)
        self.assertIn('aria-label="Toggle light or dark theme"', html)
        self.assertIn('aria-label="Help"', html)
        self.assertIn('aria-label="Account menu"', html)

    def test_no_external_assets_on_any_section(self):
        for path in ('/', '/time-rules', '/budgets', '/processes', '/settings'):
            with self.subTest(path=path):
                html = self._render(path)
                self.assertIsNone(EXTERNAL_ASSET.search(html))

    def test_existing_endpoints_still_work(self):
        # The shell must not disturb the pre-existing pages/endpoints.
        self.assertEqual(200, self.client.get('/api/users').status_code)
        self.assertEqual(200, self.client.get('/admin/audit').status_code)


class ShellSourceTest(unittest.TestCase):
    """Static guards on the shell partials + stylesheet."""

    @classmethod
    def setUpClass(cls):
        cls.base = read(os.path.join(TEMPLATE_DIR, 'base.html'))
        cls.sidebar = read(os.path.join(PARTIAL_DIR, 'sidebar.html'))
        cls.appbar = read(os.path.join(PARTIAL_DIR, 'appbar.html'))
        cls.tabbar = read(os.path.join(PARTIAL_DIR, 'tabbar.html'))
        cls.css = read(APP_CSS)

    def test_base_includes_the_sidebar(self):
        self.assertIn("_partials/sidebar.html", self.base)
        self.assertIn("_partials/appbar.html", self.base)
        self.assertIn("_partials/tabbar.html", self.base)

    def test_base_links_the_progressive_enhancement_script(self):
        # REVISED-SPEC.md: static/js/app.js is progressive enhancement only.
        self.assertIn("js/app.js", self.base)

    def test_css_defines_the_revised_pills_and_phone_shell(self):
        for sel in ('.pc-pill--online', '.pc-pill--protected',
                    '.pc-layout--phone', '.pc-phoneheader', '.pc-catcard__dl',
                    '.pc-annotation'):
            with self.subTest(sel=sel):
                self.assertIn(sel, self.css)
        # explicit Online/Unlocked mockup colours
        self.assertIn('#d7ebd7', self.css)
        self.assertIn('#003a05', self.css)

    def test_sidebar_has_brand_machine_and_nav(self):
        self.assertIn('pc-sidebar__brand', self.sidebar)
        self.assertIn('pc-machine', self.sidebar)
        self.assertIn('pc-sidebar__nav', self.sidebar)
        for label in SECTIONS:
            with self.subTest(label=label):
                self.assertIn(label, self.sidebar)
        # the nav links (with their aria-current marker) come from the shared
        # pc_nav_link macro
        self.assertIn('pc_nav_link(', self.sidebar)

    def test_tabbar_has_four_tabs_and_settings_in_the_menu(self):
        for label in ('Home', 'Time', 'Activity', 'Admin'):
            with self.subTest(label=label):
                self.assertIn("'%s'" % label, self.tabbar)
        # Settings is sidebar-only / account-menu on phones, not a tab.
        self.assertIn('Settings', self.tabbar)
        self.assertIn('pc_nav_link(', self.tabbar)

    def test_appbar_has_theme_help_and_account(self):
        self.assertIn('pc-theme-toggle', self.appbar)
        self.assertIn('#pc-i-sun', self.appbar)
        self.assertIn('#pc-i-help', self.appbar)
        self.assertIn('pc_nav_link(', self.appbar)
        self.assertIn('pcToggleTheme', self.appbar)

    def test_css_defines_the_shell(self):
        for sel in ('.pc-layout', '.pc-sidebar', '.pc-navlink', '.pc-machine',
                    '.pc-dot--online', '.pc-theme-toggle'):
            with self.subTest(sel=sel):
                self.assertIn(sel, self.css)
        self.assertIn('@media (min-width: 960px)', self.css)
        # auth pages hide the sidebar too (no ad-hoc <style> on the page)
        self.assertIn('.pc-body--auth .pc-sidebar', self.css)

    def test_css_defines_the_new_components(self):
        for sel in ('.pc-switch', '.pc-tabs', '.pc-tabbtn', '.pc-toolbar',
                    '.pc-grid--2', '.pc-timer', '.pc-fieldset',
                    '.pc-section-head', '.pc-stat', '.pc-placeholder',
                    '.pc-kbd', '.pc-inline-form'):
            with self.subTest(sel=sel):
                self.assertIn(sel, self.css)

    def test_shell_partials_are_offline(self):
        for name in ('base.html', '_partials/sidebar.html',
                     '_partials/appbar.html', '_partials/tabbar.html'):
            with self.subTest(name=name):
                path = os.path.join(TEMPLATE_DIR, name)
                self.assertIsNone(EXTERNAL_ASSET.search(read(path)))


if __name__ == '__main__':
    unittest.main()
