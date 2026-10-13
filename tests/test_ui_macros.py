#!/usr/bin/env python3
"""Jinja macro library + adoption tests.

The Flowstep canvas prompts require the implementation to use Jinja macros for
status badges, account cards, action buttons, navigation, time inputs, day
toggles, alerts, toggles, device cards and confirmation dialogs. These are
static guards that:

* the shared macro library (``templates/_partials/macros.html``) exists, is
  offline, and defines the named macros;
* the confirmation-dialog macro lives in ``_partials/dialog.html``;
* the shell partials and the screens actually adopt the macros (a page-local
  inline copy of the markup would silently drift from the shared library).

Nothing here renders Flask or touches a real account, the service, /etc, /opt
or the checkout — it is a pure source check, like the other static guards.
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, 'templates')
PARTIAL_DIR = os.path.join(TEMPLATE_DIR, '_partials')

EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''', re.I)

# Macros the design prompts name, defined in the shared library.
LIBRARY_MACROS = (
    'pc_status_pill',   # status badges / pills
    'pc_badge',         # status chips
    'pc_button',        # action buttons
    'pc_toggle',        # enable/disable switches
    'pc_device_card',   # Settings device cards
    'pc_nav_link',      # navigation (sidebar / app bar / tab bar)
)


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class MacroLibraryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read(os.path.join(PARTIAL_DIR, 'macros.html'))

    def test_defines_the_named_macros(self):
        for name in LIBRARY_MACROS:
            with self.subTest(macro=name):
                self.assertIn('macro %s(' % name, self.src)

    def test_library_is_offline(self):
        self.assertIsNone(EXTERNAL_ASSET.search(self.src))

    def test_confirmation_dialog_macro_lives_in_dialog_partial(self):
        dialog = read(os.path.join(PARTIAL_DIR, 'dialog.html'))
        self.assertIn('{% macro pc_confirm_dialog(', dialog)

    def test_no_external_assets_in_any_partial(self):
        for path in sorted(os.listdir(PARTIAL_DIR)):
            if not path.endswith('.html'):
                continue
            with self.subTest(partial=path):
                self.assertIsNone(
                    EXTERNAL_ASSET.search(read(os.path.join(PARTIAL_DIR, path))))


class MacroAdoptionTest(unittest.TestCase):
    """The shell partials and the screens import/use the shared macros."""

    def test_shell_partials_use_the_nav_macro(self):
        for name in ('sidebar.html', 'appbar.html', 'tabbar.html'):
            body = read(os.path.join(PARTIAL_DIR, name))
            with self.subTest(partial=name):
                self.assertIn("_partials/macros.html", body)
                self.assertIn('pc_nav_link(', body)

    def test_screens_adopt_the_macros(self):
        cases = {
            'index.html': ('pc_status_pill(', 'pc_button('),
            'settings.html': ('pc_toggle(', 'pc_device_card(', 'pc_button('),
            'budgets.html': ('pc_badge(',),
        }
        for page, needles in cases.items():
            body = read(os.path.join(TEMPLATE_DIR, page))
            with self.subTest(page=page):
                self.assertIn("_partials/macros.html", body)
                for needle in needles:
                    self.assertIn(needle, body)


if __name__ == '__main__':
    unittest.main()
