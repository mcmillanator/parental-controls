#!/usr/bin/env python3
"""App-overrides area polish on the user detail page.

Three focused changes to the weekly-windows / app-overrides part of
``templates/user.html``:

* the app fields are labelled **"App"** (not the developer-speak
  "App comm:") for both ``#override-comm`` and ``#window-app-target``,
  with a friendly "Search installed apps…" placeholder,
* both app fields become a searchable, accessible **combobox** backed by
  the existing ``<datalist id="installed-apps">`` (which the server renders
  from ``app_names``) — one reusable vanilla-JS implementation, no build
  step, and the committed ``.value`` is still the raw comm so the
  ``/api/budgets`` POSTs are unchanged,
* **App overrides** moves out of the Time-budgets disclosure into its own
  ``<details class="pc-disclosure" id="app-overrides">`` so it can be
  collapsed independently.

Renders go through the mocked admin session harness (temp auth store, mocked
``get_user_live``). Stdlib only: nothing here touches a real account, the
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
CSS = os.path.join(ROOT, 'static', 'css', 'app.css')

# The friendly placeholder both app fields share.
PLACEHOLDER = 'Search installed apps…'

# The ids the selector contract + stdlib suite pin in this area. The rename
# and the combobox must not disturb any of them.
PINNED_IDS = (
    'override-comm', 'override-add-mode', 'override-add-hours',
    'override-add-minutes', 'app-override-list',
    'window-app-target', 'installed-apps',
)


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def details_slice(html, marker):
    """The slice of ``html`` from the ``<details>`` containing ``marker`` to
    its first closing tag (the disclosures here hold no nested details)."""
    i = html.index(marker)
    start = html.rindex('<details', 0, i)
    end = html.index('</details>', start)
    return html[start:end]


def _entry():
    entry = budgets.blank_entry()
    entry['limits'] = {'games': 90}
    return entry


def live_dict(with_overrides=True):
    live = {
        'user': {
            'username': KID, 'uid': 1001, 'home': '/home/kid',
            'shell': '/bin/bash', 'locked': False,
            'status_detail': 'Unlocked',
            'bedtime': {'start': '21:00', 'end': '07:00',
                        'days': [0, 1, 2, 3, 4], 'manual_override': False},
            'in_bedtime': False,
            'bonus': {'active': False},
        },
        'processes': [], 'visible': [], 'hidden': [],
        'groups': {c: [] for c in budgets.CATEGORIES},
        'budget_state': budgets.budget_status(KID, {KID: _entry()}),
        'overrides': {'app_overrides': {}, 'windows': []},
        'app_names': ['firefox', 'steam', 'minecraft'],
    }
    if with_overrides:
        live['overrides'] = {
            'app_overrides': {'firefox': {'mode': 'limit', 'minutes': 60}},
            'windows': [],
        }
    return live


class _Render(_AdminSessionMixin, unittest.TestCase):
    def render(self, live=None):
        live = live_dict() if live is None else live
        with mock.patch.object(app, 'get_user_live', return_value=live):
            resp = self.client.get('/user/' + KID)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Static source guards (no Flask).
# ---------------------------------------------------------------------------

class SourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read(TEMPLATE)
        cls.css = read(CSS)

    # ---------------------------------------------------------- label rename
    def test_labels_read_app_not_app_comm(self):
        self.assertNotIn('App comm', self.src)
        self.assertIn('for="override-comm">App</label>', self.src)
        self.assertIn('for="window-app-target">App</label>', self.src)

    def test_both_fields_share_the_friendly_placeholder(self):
        self.assertEqual(2, self.src.count('placeholder="%s"' % PLACEHOLDER))
        self.assertNotIn('placeholder="comm"', self.src)
        self.assertNotIn('placeholder="app comm"', self.src)

    # --------------------------------------------------------- combobox markup
    def test_both_inputs_are_comboboxes(self):
        # Two <input> tags carry the enhancement hook (the JS also mentions
        # the selector, so match the tags themselves).
        self.assertEqual(
            2, len(re.findall(r'<input[^>]*data-app-picker', self.src)))
        self.assertEqual(2, self.src.count('role="combobox"'))
        self.assertEqual(2, self.src.count('aria-autocomplete="list"'))
        self.assertEqual(2, self.src.count('aria-haspopup="listbox"'))

    def test_combobox_wires_the_aria_contract(self):
        self.assertIn('aria-expanded="false"', self.src)
        self.assertIn('aria-controls="override-comm-listbox"', self.src)
        self.assertIn('aria-controls="window-app-target-listbox"', self.src)

    def test_listbox_panels_are_role_listbox(self):
        self.assertIn(
            'id="override-comm-listbox" class="pc-combobox__list" '
            'role="listbox"', self.src)
        self.assertIn(
            'id="window-app-target-listbox" class="pc-combobox__list" '
            'role="listbox"', self.src)

    def test_fallback_datalist_is_kept(self):
        # The datalist is the data source + the no-JS fallback.
        self.assertIn('<datalist id="installed-apps">', self.src)
        self.assertIn('{% for comm in app_names %}<option value="{{ comm }}">',
                      self.src)
        # The inputs keep `list=` so the no-JS path still autocompletes.
        self.assertEqual(2, self.src.count('list="installed-apps"'))

    # -------------------------------------------------------------- the JS
    def test_reusable_picker_reads_the_datalist(self):
        self.assertIn('function pcAppOptions()', self.src)
        self.assertIn("document.getElementById('installed-apps')", self.src)
        self.assertIn('function pcEnhanceAppPicker(input)', self.src)
        self.assertIn('function pcInitAppPickers()', self.src)
        self.assertIn('input[data-app-picker]', self.src)
        # Enhanced at load, after the DOM exists.
        self.assertIn('pcInitAppPickers();', self.src)

    def test_picker_filters_and_keeps_the_raw_comm_value(self):
        # Options carry the raw comm; selecting commits it unchanged.
        self.assertIn('opt.dataset.value = name;', self.src)
        self.assertIn('input.value = name;', self.src)
        # Typing filters the option list.
        self.assertIn('all.filter(n => n.toLowerCase().includes(needle))',
                      self.src)

    def test_picker_keyboard_and_a11y_behaviour(self):
        self.assertIn("e.key === 'ArrowDown'", self.src)
        self.assertIn("e.key === 'ArrowUp'", self.src)
        self.assertIn("e.key === 'Enter'", self.src)
        self.assertIn("e.key === 'Escape'", self.src)
        self.assertIn("opt.setAttribute('role', 'option')", self.src)
        self.assertIn("input.setAttribute('aria-activedescendant'", self.src)
        self.assertIn("input.setAttribute('aria-expanded'", self.src)
        # The native datalist popup is removed once the styled panel is live;
        # the <datalist> itself stays in the DOM as the data source.
        self.assertIn("input.removeAttribute('list')", self.src)

    # ------------------------------------------------- the details section
    def test_app_overrides_is_its_own_disclosure(self):
        self.assertIn('<details class="pc-disclosure" id="app-overrides">',
                      self.src)
        self.assertIn('<span>App overrides</span>', self.src)
        # The old in-section heading is gone.
        self.assertNotIn('<h3>App overrides</h3>', self.src)

    def test_weekly_windows_no_longer_owns_app_overrides(self):
        self.assertIn('<span>Weekly windows</span>', self.src)
        self.assertNotIn('Weekly windows &amp; app overrides', self.src)

    # ---------------------------------------------------------------- the CSS
    def test_combobox_panel_css_uses_existing_tokens(self):
        for selector in ('.pc-combobox', '.pc-combobox__list',
                         '.pc-combobox__option'):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.css)
        self.assertRegex(
            self.css,
            r'\.pc-combobox__list\s*\{[^}]*position:\s*absolute')
        self.assertRegex(
            self.css,
            r'\.pc-combobox__list\s*\{[^}]*box-shadow:\s*var\(--el-2\)')
        self.assertRegex(
            self.css,
            r'\.pc-combobox__option\.is-active[^{]*\{[^}]*var\(--info-bg\)')


# ---------------------------------------------------------------------------
# Rendered page guards.
# ---------------------------------------------------------------------------

class AppOverridesRenderTest(_Render):
    def test_labels_and_placeholders_render(self):
        html = self.render()
        self.assertNotIn('App comm', html)
        self.assertIn('<label class="pc-field__label" for="override-comm">'
                      'App</label>', html)
        self.assertIn('<label class="pc-field__label" for="window-app-target">'
                      'App</label>', html)
        self.assertEqual(2, html.count('placeholder="%s"' % PLACEHOLDER))

    def test_combobox_roles_render(self):
        html = self.render()
        self.assertGreaterEqual(html.count('role="combobox"'), 2)
        self.assertIn('aria-controls="override-comm-listbox"', html)
        self.assertIn('aria-controls="window-app-target-listbox"', html)
        self.assertIn('role="listbox"', html)
        self.assertIn('data-app-picker', html)

    def test_pickers_are_wired_to_app_names(self):
        # app_names -> the datalist options the picker reads.
        html = self.render()
        for comm in ('firefox', 'steam', 'minecraft'):
            with self.subTest(comm=comm):
                self.assertIn('<option value="%s">' % comm, html)
        self.assertIn('id="installed-apps"', html)

    def test_app_overrides_is_a_separate_disclosure(self):
        html = self.render()
        self.assertIn('<details class="pc-disclosure" id="app-overrides">',
                      html)
        self.assertIn('<span>App overrides</span>', html)
        self.assertIn('configured', html)  # summary value: "1 configured"

    def test_app_overrides_left_the_weekly_windows_disclosure(self):
        html = self.render()
        weekly = details_slice(html, 'id="weekly-windows"')
        apps = details_slice(html, 'id="app-overrides"')
        # The window picker stays with the windows ...
        self.assertIn('id="window-app-target"', weekly)
        self.assertNotIn('id="override-comm"', weekly)
        self.assertNotIn('id="app-override-list"', weekly)
        # ... while the override list/add-row/datalist moved to their own.
        self.assertIn('id="override-comm"', apps)
        self.assertIn('id="app-override-list"', apps)
        self.assertIn('id="installed-apps"', apps)

    def test_pinned_ids_intact(self):
        html = self.render()
        for pinned in PINNED_IDS:
            with self.subTest(id=pinned):
                self.assertIn('id="%s"' % pinned, html)

    def test_empty_state_still_renders(self):
        live = live_dict(with_overrides=False)
        live['app_names'] = []
        html = self.render(live)
        self.assertIn('id="app-overrides-empty"', html)
        self.assertIn('No app overrides configured.', html)
        self.assertIn('None configured', html)
        self.assertIn('<datalist id="installed-apps">', html)

    def test_posts_are_unchanged(self):
        # The picker only changes how a comm is chosen, never the payload.
        src = read(TEMPLATE)
        self.assertIn("comm: comm, mode: mode,", src)
        self.assertIn("minutes: mode === 'limit' ? minutes : ''", src)
        self.assertIn("target_type: type, target: target,", src)


if __name__ == '__main__':
    unittest.main()
