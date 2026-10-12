#!/usr/bin/env python3
"""Shared per-app override dialog (feature/override-dialog).

The per-process Override editor moved behind ONE shared ``<dialog>`` in the
shell. This module pins the new model:

* a process-group row carries only a small state chip (when an override is
  set) plus a compact "Override" button — no always-visible mode select /
  h/m inputs / Set button,
* exactly one ``#pc-override`` dialog exists, and it lives OUTSIDE the
  10s-refreshed ``#process-section`` (so a live poll can never wipe a
  half-typed allowance),
* the open handler (``applyOverride``) reads the row's ``data-comm`` and the
  current override from the live payload, then populates mode + h/m
  (limit/exempt/none) in the shared dialog,
* Set composes whole minutes client-side for the unchanged
  ``/api/budgets/overrides`` POST; Cancel/Esc resolves null and posts nothing.

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
PARTIAL = os.path.join(ROOT, 'templates', '_partials', 'override_dialog.html')
BASE = os.path.join(ROOT, 'templates', 'base.html')

EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''', re.I)


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


def live_dict(overrides=None):
    """A live payload with one running browser group and one override."""
    entry = budgets.blank_entry()
    entry['limits'] = {'games': 90}
    live = {
        'user': {
            'username': KID, 'uid': 1001, 'home': '/home/kid',
            'shell': '/bin/bash', 'locked': False,
            'status_detail': 'Unlocked',
            'bedtime': {'start': '21:00', 'end': '07:00',
                        'days': [0, 1, 2, 3, 4, 5, 6], 'manual_override': False},
            'in_bedtime': False,
            'bonus': {'active': False},
        },
        'processes': [], 'visible': [{'comm': 'firefox'}], 'hidden': [],
        'groups': {c: [] for c in budgets.CATEGORIES},
        'budget_state': budgets.budget_status(KID, {KID: entry}),
        'overrides': {
            'app_overrides': ({'firefox': {'mode': 'limit', 'minutes': 90}}
                              if overrides is None else overrides),
            'windows': [],
        },
        'app_names': ['firefox', 'steam'],
    }
    live['groups']['browsers'] = [
        {'comm': 'firefox', 'display_name': 'Firefox Web Browser',
         'count': 1, 'elapsed': '12m', 'max_elapsed': '12m',
         'members': [{'pid': 9001, 'comm': 'firefox',
                      'display_name': 'Firefox Web Browser', 'elapsed': '12m'}]},
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
# The row: chip + Override button only.
# ---------------------------------------------------------------------------

class RowModelTest(_Render):
    def test_row_has_only_the_chip_and_the_override_button(self):
        row = group_row(self.render(), 'firefox')
        self.assertIn('class="btn-override', row)
        self.assertIn('data-comm="firefox"', row)
        self.assertIn('onclick="applyOverride(this)"', row)
        self.assertIn('>Override<', row)
        # The stored Limit override shows as a compact state chip.
        self.assertIn('1h 30m/day', row)
        # No always-visible editor is left in the row.
        self.assertNotIn('override-mode', row)
        self.assertNotIn('override-hours', row)
        self.assertNotIn('override-minutes', row)
        self.assertNotIn('<select', row)
        self.assertNotIn('<input', row)

    def test_row_has_no_chip_when_no_override_is_set(self):
        row = group_row(self.render(live_dict(overrides={})), 'firefox')
        self.assertNotIn('pc-chip', row)
        self.assertIn('class="btn-override', row)

    def test_exempt_override_shows_unlimited_chip(self):
        live = live_dict(overrides={'firefox': {'mode': 'exempt',
                                                'minutes': None}})
        row = group_row(self.render(live), 'firefox')
        self.assertIn('Unlimited', row)
        self.assertIn('class="btn-override', row)


# ---------------------------------------------------------------------------
# One shared dialog, outside the refreshed process region.
# ---------------------------------------------------------------------------

class SharedDialogTest(_Render):
    def test_exactly_one_dialog_and_outside_the_process_region(self):
        html = self.render()
        self.assertEqual(1, html.count('id="pc-override"'))
        start = html.index('id="process-section"')
        end = html.index('</section>', start)
        # The dialog is not inside #process-section (which renderProcesses
        # replaces wholesale on every 10s poll).
        self.assertNotIn('id="pc-override"', html[start:end])
        self.assertIn('id="pc-override"', html[end:])

    def test_dialog_is_accessible_and_has_the_editor(self):
        html = self.render()
        self.assertRegex(
            html,
            r'<dialog id="pc-override" class="pc-dialog[^"]*" '
            r'aria-labelledby="pc-override-title"')
        self.assertIn('id="pc-override-title"', html)
        # The app name (comm) travels via the dialog body.
        self.assertIn('id="pc-override-comm"', html)
        # Mode select: Limit / Unlimited / Clear.
        self.assertIn('id="pc-override-mode"', html)
        self.assertIn('class="override-mode"', html)
        self.assertIn('for="pc-override-mode"', html)
        for value, label in (('limit', 'Limit'), ('exempt', 'Unlimited'),
                             ('none', 'Clear')):
            self.assertRegex(
                html,
                r'<option value="%s">%s</option>' % (value, label))
        # h/m allowance pair, labelled.
        self.assertIn('id="pc-override-hours"', html)
        self.assertIn('id="pc-override-minutes"', html)
        self.assertIn('for="pc-override-hours"', html)
        self.assertIn('for="pc-override-minutes"', html)
        # Set + Cancel wired like the shell's confirm dialog.
        self.assertIn('data-pc-dialog-confirm', html)
        self.assertIn('data-pc-dialog-cancel', html)
        self.assertIn('>Set<', html)
        self.assertIn('>Cancel<', html)

    def test_base_shell_includes_the_shared_dialog_partial(self):
        self.assertIn("_partials/override_dialog.html", read(BASE))


# ---------------------------------------------------------------------------
# Static source guards on the shared partial (no JS engine: source-level).
# ---------------------------------------------------------------------------

class PartialSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read(PARTIAL)

    def test_defines_the_one_shared_helper(self):
        self.assertIn('function pcOverrideDialog(opts)', self.src)
        self.assertIn('window.pcOverrideDialog = pcOverrideDialog;', self.src)
        self.assertIn('dialog.showModal()', self.src)
        self.assertIn("document.getElementById('pc-override')", self.src)

    def test_open_populates_mode_and_allowance_from_the_current_override(self):
        # comm shown, mode reflected (limit/exempt/none), h/m prefilled from
        # the stored whole minutes for a Limit override.
        self.assertIn('commEl.textContent = opts.comm ||', self.src)
        self.assertIn(
            "modeEl.value = (opts.mode === 'exempt' || opts.mode === 'none')",
            self.src)
        self.assertIn('hoursEl.value = String(Math.floor(stored / 60));',
                      self.src)
        self.assertIn('minsEl.value = String(stored % 60);', self.src)

    def test_set_composes_whole_minutes_for_the_unchanged_api(self):
        self.assertIn('function composeMinutes(hoursEl, minsEl)', self.src)
        self.assertIn('return h * 60 + m;', self.src)
        self.assertIn('h < 0 || h > 99 || m < 0 || m > 59', self.src)
        self.assertIn("if (hStr === '' && mStr === '') { return ''; }",
                      self.src)
        self.assertIn('finish({ mode: mode, minutes: minutes });', self.src)
        # A Limit override with no allowance is refused in-dialog.
        self.assertIn('if (minutes === null || minutes < 1)', self.src)

    def test_cancel_and_escape_resolve_null_without_posting(self):
        self.assertIn('function onCancel() { finish(null); }', self.src)
        self.assertIn('function onClose() { finish(null); }', self.src)
        # The dialog never talks to the network itself; the page posts.
        self.assertNotIn('fetch(', self.src)
        self.assertNotIn('postForm', self.src)

    def test_partial_is_offline_and_has_no_raw_confirm(self):
        self.assertIsNone(EXTERNAL_ASSET.search(self.src))
        self.assertNotIn('window.confirm(', self.src)


# ---------------------------------------------------------------------------
# Static source guards on the page handler.
# ---------------------------------------------------------------------------

class PageHandlerSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read(TEMPLATE)

    def test_open_reads_data_comm_and_the_current_override(self):
        self.assertIn("button.getAttribute('data-comm')", self.src)
        self.assertIn('window.pcOverrideDialog({', self.src)
        self.assertIn('comm: comm, mode: mode, minutes: minutes', self.src)
        # limit/exempt/none mapping from the live payload.
        self.assertIn("if (ov && ov.mode === 'exempt')", self.src)
        self.assertIn("} else if (ov && ov.mode === 'limit') {", self.src)
        self.assertIn("let mode = 'none';", self.src)

    def test_set_posts_minutes_then_refreshes(self):
        self.assertIn("if (!choice) { return; }", self.src)
        self.assertIn("postForm('/api/budgets/overrides'", self.src)
        self.assertIn('mode: choice.mode,', self.src)
        self.assertIn('minutes: choice.minutes', self.src)
        self.assertIn('refreshUserLive();', self.src)

    def test_row_renderer_emits_chip_plus_button_only(self):
        self.assertIn('function overrideControlHtml(comm)', self.src)
        self.assertIn(
            'class="btn-override pc-btn pc-btn--sm pc-btn--primary"',
            self.src)
        self.assertIn('onclick="applyOverride(this)"', self.src)
        # The live re-render no longer emits the inline editor.
        self.assertNotIn('override-mode', self.src)
        self.assertNotIn("wrap.querySelector('.override-hours')", self.src)

    def test_add_app_override_form_is_untouched(self):
        # The separate #app-overrides add-row card keeps its own inputs.
        self.assertIn('id="override-add-mode"', self.src)
        self.assertIn('id="override-add-hours"', self.src)
        self.assertIn('id="override-add-minutes"', self.src)
        self.assertIn('onclick="addAppOverride()"', self.src)


# ---------------------------------------------------------------------------
# The selectors the rest of the contract pins still resolve.
# ---------------------------------------------------------------------------

class PinnedSelectorTest(_Render):
    def test_relevant_pinned_selectors_still_resolve(self):
        html = self.render()
        for token in ('class="override-mode"', 'data-comm="firefox"',
                      'applyOverride(', 'id="override-add-minutes"',
                      'id="app-override-list"', 'installed-apps'):
            with self.subTest(token=token):
                self.assertIn(token, html)

    def test_dialog_and_row_share_the_data_comm_plumbing(self):
        # The row button carries data-comm; the dialog takes the comm as an
        # argument (never a JS string literal).
        html = self.render()
        self.assertRegex(html, r'btn-override[^>]*data-comm="firefox"')
        self.assertNotIn("pcOverrideDialog('", html)


if __name__ == '__main__':
    unittest.main()
