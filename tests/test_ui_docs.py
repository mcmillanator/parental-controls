#!/usr/bin/env python3
"""Documentation guardrail for the mobile-first UI redesign (UI_UX_PLAN.md P6).

The redesign moved Bootstrap from a CDN into ``static/`` and replaced the
desktop-only skin with a token-based, mobile-first design system. The docs are
part of that promise, so this module keeps ``README.md``, ``PLAN.md`` and
``UI_UX_PLAN.md`` truthful about what shipped:

* README/PLAN describe the offline, vendored assets (no CDN), the mobile-first
  layout, the dark/light theme and that ``static/`` ships with the app,
* README/PLAN do not contradict the shipped UI (no stale "loaded from a CDN",
  no stale test counts),
* the two recorded test counts (tests + skips) agree between README and PLAN,
* UI_UX_PLAN.md marks the phases landed instead of "PLAN ONLY".

Stdlib only (``re`` + ``unittest``); no Flask, no network, no browser.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / 'README.md'
PLAN = ROOT / 'PLAN.md'
UI_PLAN = ROOT / 'UI_UX_PLAN.md'

TESTS_RE = re.compile(r'(\d+)\s+tests')
SKIPS_RE = re.compile(r'(\d+)\s+environmental skips')


def read(path):
    return path.read_text(encoding='utf-8')


class ReadmeTest(unittest.TestCase):
    """README.md describes the shipped mobile-first/offline UI."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(README)
        cls.flat = ' '.join(cls.text.split())
        cls.lower = cls.flat.lower()

    def test_documents_offline_vendored_assets(self):
        self.assertIn('vendored', self.lower,
                      'README must say Bootstrap is vendored, not a CDN')
        self.assertIn('static/', self.flat,
                      'README must point at the local static/ assets')
        self.assertIn('bootstrap.min.css', self.lower,
                      'README must name the vendored Bootstrap file')
        # "no CDN" / "no external request" — the offline promise.
        self.assertIn('no cdn', self.lower)

    def test_documents_the_app_ships_its_own_assets(self):
        self.assertIn('install.sh', self.lower,
                      'README must tie static/ to the installer staging it')
        self.assertIn('stage', self.lower)

    def test_documents_mobile_first_and_theme(self):
        self.assertIn('mobile-first', self.lower)
        self.assertIn('light theme', self.lower)
        self.assertIn('prefers-color-scheme', self.lower)

    def test_has_no_stale_cdn_or_test_count_claims(self):
        self.assertNotIn('cdn.jsdelivr.net', self.lower,
                         'README still references the retired CDN')
        self.assertNotIn('loaded from a cdn', self.lower,
                         'README still claims a CDN Bootstrap')
        self.assertNotIn('526 tests', self.lower,
                         'README still carries the stale test count')


class PlanTest(unittest.TestCase):
    """PLAN.md tracks the redesign as landed and does not contradict it."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(PLAN)
        cls.flat = ' '.join(cls.text.split())
        cls.lower = cls.flat.lower()

    def test_references_the_ui_ux_plan_and_lists_it(self):
        self.assertIn('UI_UX_PLAN.md', self.text,
                      'PLAN must reference the UI/UX redesign document')
        self.assertRegex(self.text, r'\|\s*`UI_UX_PLAN\.md`\s*\|',
                         'PLAN Files table needs a UI_UX_PLAN.md row')

    def test_marks_the_redesign_complete(self):
        # The mobile-first redesign should be recorded as shipped with the
        # selector contract intact.
        self.assertIn('mobile-first', self.lower)
        self.assertIn('zero selector', self.lower)

    def test_has_no_stale_cdn_or_test_count_claims(self):
        self.assertNotIn('cdn.jsdelivr.net', self.lower,
                         'PLAN still references the retired CDN')
        self.assertNotIn('loaded from a cdn', self.lower,
                         'PLAN still claims a CDN Bootstrap')
        self.assertNotIn('526 tests', self.lower,
                         'PLAN still carries the stale test count')


class RecordedCountsTest(unittest.TestCase):
    """README and PLAN must agree on the recorded suite size."""

    def _counts(self, text, pattern, label):
        match = pattern.search(' '.join(text.split()))
        self.assertIsNotNone(match, 'no %s recorded' % label)
        return match.group(1)

    def test_test_and_skip_counts_agree(self):
        readme, plan = read(README), read(PLAN)
        self.assertEqual(
            self._counts(readme, TESTS_RE, 'test count'),
            self._counts(plan, TESTS_RE, 'test count'),
            'README and PLAN disagree on the test count')
        self.assertEqual(
            self._counts(readme, SKIPS_RE, 'skip count'),
            self._counts(plan, SKIPS_RE, 'skip count'),
            'README and PLAN disagree on the skip count')


class UiPlanStatusTest(unittest.TestCase):
    """UI_UX_PLAN.md records P0-P6 as landed, not "PLAN ONLY"."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(UI_PLAN)

    def test_header_is_no_longer_plan_only(self):
        self.assertNotIn('Status: **PLAN ONLY**', self.text,
                         'UI_UX_PLAN.md still claims to be plan-only')
        self.assertIn('IMPLEMENTED', self.text)

    def test_every_phase_is_marked(self):
        for phase in range(0, 7):
            with self.subTest(phase=phase):
                self.assertRegex(self.text, r'\*\*Phase %d\b' % phase,
                                 'phase %d heading missing' % phase)
        self.assertGreaterEqual(
            self.text.count('\u2705'), 6,
            'phases 0-5 should each carry a merged marker')

    def test_phase_6_recorded_zero_selector_changes(self):
        self.assertIn('zero selector changes', self.text)


if __name__ == '__main__':
    unittest.main()
