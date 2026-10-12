#!/usr/bin/env python3
"""Accessibility, theming and state-polish guardrail (UI_UX_PLAN.md Phase 5).

Covers the P5 commitments:

* **WCAG 2.2 AA contrast** — parses the token pairs out of
  ``static/css/app.css`` and computes the real contrast ratio for every status
  chip and every text-safe token, in *both* the dark default and the light
  override. Fails if a pair drops below 4.5:1 (the four audited failures plus
  the ones light mode would otherwise reintroduce).
* **Focus / motion / targets** — a ``:focus-visible`` ring, a
  ``prefers-reduced-motion`` kill-switch, 44px targets (32px for the small
  variant per 2.5.8).
* **Light theme** — dark default + ``prefers-color-scheme: light`` and an
  explicit ``[data-theme]`` hook.
* **Auth shell** — the app bar / tab bar are hidden by a shared
  ``.pc-body--auth`` rule, not by an ad-hoc inline ``<style>`` on the pages.
* **Legacy control hooks** — the ``.btn-*`` / ``.badge-*`` / process-group
  classes the renderers still emit are styled without renaming them.
* **Shell a11y** — every rendered page has ``<html lang>``, the viewport, a
  skip link, an ``aria-live`` toast region, no external asset, and stacked
  tables keep a real ``<thead>``/``th scope``.
* **States** — the skeleton / empty / stale patterns exist and are wired.

Stdlib only (``re`` + ``unittest``); the render checks reuse the same mocked
admin-session harness as the other UI tests (no live account, no service).
"""

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / 'templates'
PARTIAL_DIR = TEMPLATE_DIR / '_partials'
APP_CSS = ROOT / 'static' / 'css' / 'app.css'
BASE_TEMPLATE = TEMPLATE_DIR / 'base.html'

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''', re.IGNORECASE)

try:  # Flask render harness — the static tests run even if this fails.
    import app
    from test_lock_regression import _AdminSessionMixin
    _HAVE_FLASK = True
except Exception:  # pragma: no cover - surfaces as a skip
    app = None
    _AdminSessionMixin = None
    _HAVE_FLASK = False

# Legacy control hooks the renderers still emit and the selector contract pins.
LEGACY_BUTTON_CLASSES = (
    'btn-freeze', 'btn-unlock', 'btn-hide', 'btn-show-hidden',
    'btn-group-toggle', 'btn-bonus', 'btn-bedtime-save', 'btn-bedtime-clear',
    'btn-lock', 'btn-logout', 'btn-details',
)
LEGACY_BADGE_CLASSES = (
    'badge-bonus', 'badge-bedtime', 'badge-override', 'badge-count',
    'badge-paused', 'badge-frozen', 'badge-window', 'badge-protected',
)
PROCESS_HOOK_CLASSES = (
    'process-name', 'group-chevron', 'group-row', 'group-detail',
    'actions', 'override-mode',
)


def read(path):
    return Path(path).read_text(encoding='utf-8')


def _lum(hex_color):
    h = hex_color.strip().lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = f(r), f(g), f(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg):
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _parse_token_blocks(css):
    """Return (dark, light) dicts of custom-property declarations.

    ``app.css`` has a single bare ``:root { ... }`` block (dark) plus a
    ``[data-theme='light']`` block and a ``prefers-color-scheme: light`` block
    (both light). Only blocks that declare ``--bg`` are treated as tokens.
    """
    dark, light = {}, {}
    for match in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        selector = match.group(1).strip()
        body = match.group(2)
        if '--bg' not in body:
            continue
        decls = {k: v.strip() for k, v in
                 re.findall(r'(--[\w-]+)\s*:\s*([^;]+);', body)}
        if 'light' in selector or 'not(' in selector or 'data-theme' in selector:
            light.update(decls)
        else:
            dark.update(decls)
    return dark, light


def _resolve(tokens, name, seen=None):
    """Resolve one level of ``var(--x)`` indirection."""
    seen = seen or set()
    value = tokens.get(name, '')
    m = re.match(r'var\(\s*(--[\w-]+)\s*\)', value)
    if m and m.group(1) not in seen:
        return _resolve(tokens, m.group(1), seen | {m.group(1)})
    return value


class AppCssThemeTest(unittest.TestCase):
    """Dark default + light override, focus, motion and touch tokens."""

    @classmethod
    def setUpClass(cls):
        cls.css = read(APP_CSS)

    def test_dark_is_the_default_and_light_is_available(self):
        self.assertIn('prefers-color-scheme: light', self.css)
        self.assertIn("[data-theme='light']", self.css)
        self.assertIn('color-scheme: dark', self.css)
        self.assertIn('color-scheme: light', self.css)
        # The light override actually changes the page/surface tokens.
        self.assertIn('--bg: #f4f5f8', self.css)
        self.assertIn('--surface: #ffffff', self.css)

    def test_focus_visible_ring_is_defined_globally(self):
        self.assertIn(':focus-visible', self.css)
        self.assertIn('outline: 2px solid var(--focus-ring)', self.css)
        self.assertIn('outline-offset: 2px', self.css)
        # ...and only suppressed for the pointer case (never removed outright).
        self.assertIn(':focus:not(:focus-visible)', self.css)

    def test_reduced_motion_switch(self):
        self.assertIn('prefers-reduced-motion: reduce', self.css)
        match = re.search(
            r'@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}',
            self.css, re.DOTALL)
        self.assertIsNotNone(match, 'reduced-motion block missing')
        self.assertIn('transition: none !important', match.group(1))
        self.assertIn('animation: none !important', match.group(1))

    def test_touch_targets(self):
        self.assertIn('--touch: 44px', self.css)
        self.assertIn('min-height: var(--touch)', self.css)
        self.assertRegex(self.css, r'\.pc-btn--sm\s*\{[^}]*min-height:\s*32px')

    def test_auth_body_hides_the_chrome_from_the_stylesheet(self):
        self.assertIn('.pc-body--auth', self.css)
        self.assertRegex(
            self.css,
            r'\.pc-body--auth\s+\.pc-appbar\s*,\s*\.pc-body--auth\s+\.pc-tabbar\s*\{[^}]*display:\s*none')
        for page in ('login.html', 'change_password.html'):
            src = read(TEMPLATE_DIR / page)
            with self.subTest(page=page):
                self.assertIn('pc-body--auth', src)
                self.assertNotIn('<style>', src)

    def test_scroll_padding_keeps_focus_clear_of_the_appbar(self):
        # WCAG 2.4.11 — anchored/focused controls must not sit under the bar.
        self.assertIn('scroll-padding-top', self.css)
        self.assertIn('scroll-margin-top', self.css)


class LegacyControlStylingTest(unittest.TestCase):
    """The renderers' legacy classes are styled without being renamed."""

    @classmethod
    def setUpClass(cls):
        cls.css = read(APP_CSS)

    def test_every_legacy_button_class_has_a_rule(self):
        for cls in LEGACY_BUTTON_CLASSES:
            with self.subTest(cls=cls):
                self.assertRegex(
                    self.css, r'\.%s(:[^,{\s]*)?\s*[,{]' % re.escape(cls),
                    '%s has no styling rule' % cls)

    def test_legacy_buttons_are_scoped_to_the_bare_form(self):
        # They must not fight the `.pc-btn` variant when both are present.
        self.assertIsNotNone(
            re.search(r'\.btn-lock:not\(\.pc-btn\)[^{]*\{', self.css),
            'legacy button base rule missing')
        self.assertIn(':not(.pc-btn)', self.css)

    def test_every_legacy_badge_class_has_a_rule(self):
        for cls in LEGACY_BADGE_CLASSES:
            with self.subTest(cls=cls):
                self.assertRegex(self.css, r'\.%s\b' % re.escape(cls),
                                 '%s has no styling rule' % cls)
        self.assertIn(':not(.pc-chip)', self.css)

    def test_process_group_hooks_are_styled(self):
        for cls in PROCESS_HOOK_CLASSES:
            with self.subTest(cls=cls):
                self.assertRegex(self.css, r'\.%s\b[^{}]*\{' % re.escape(cls),
                                 '%s has no rule' % cls)

    def test_nested_card_inside_disclosure_is_flattened(self):
        self.assertRegex(
            self.css,
            r'\.pc-disclosure__body\s*>\s*\.pc-card\s*\{[^}]*border:\s*0')

    def test_duration_unit_token_and_class(self):
        self.assertIn('--unit-fg:', self.css)
        self.assertRegex(self.css, r'\.pc-duration__unit\s*,\s*\.pc-unit\s*\{')


class ContrastTokenTest(unittest.TestCase):
    """Every status pair and text token passes WCAG AA in both themes."""

    @classmethod
    def setUpClass(cls):
        dark, light_overrides = _parse_token_blocks(read(APP_CSS))
        cls.dark = dark
        # A light theme inherits every token it does not override, so merge.
        cls.light = {**dark, **light_overrides}

    def _check(self, tokens, fg, bg):
        fgv = _resolve(tokens, fg)
        bgv = _resolve(tokens, bg)
        self.assertTrue(re.match(r'#[0-9a-fA-F]{3,6}$', fgv),
                        '%s did not resolve to a hex colour (%r)' % (fg, fgv))
        self.assertTrue(re.match(r'#[0-9a-fA-F]{3,6}$', bgv),
                        '%s did not resolve to a hex colour (%r)' % (bg, bgv))
        return contrast(fgv, bgv)

    CHIP_PAIRS = {
        'ok': ('--ok-fg', '--ok-bg'),
        'danger': ('--danger-fg', '--danger-bg'),
        'warn': ('--warn-fg', '--warn-bg'),
        'info': ('--info-fg', '--info-bg'),
        'override': ('--override-fg', '--override-bg'),
        'bedtime': ('--bedtime-fg', '--bedtime-bg'),
        'bonus': ('--bonus-fg', '--bonus-bg'),
        'neutral': ('--neutral-fg', '--neutral-bg'),
    }

    def test_chip_foregrounds_pass_aa_on_their_fill(self):
        for theme, tokens in (('dark', self.dark), ('light', self.light)):
            for name, (fg, bg) in self.CHIP_PAIRS.items():
                with self.subTest(theme=theme, chip=name):
                    self.assertGreaterEqual(
                        self._check(tokens, fg, bg), 4.5,
                        '%s chip fails AA in %s' % (name, theme))

    def test_text_tokens_pass_aa_on_the_surface_both_themes(self):
        for theme, tokens in (('dark', self.dark), ('light', self.light)):
            for token in ('--ok-text', '--warn-text', '--danger-text',
                          '--info-text', '--text', '--text-muted',
                          '--text-faint', '--link'):
                with self.subTest(theme=theme, token=token):
                    ratio = self._check(tokens, token, '--surface')
                    self.assertGreaterEqual(
                        ratio, 4.5,
                        '%s on --surface fails AA in %s (%.2f)'
                        % (token, theme, ratio))

    def test_muted_text_passes_on_the_second_surface_too(self):
        # zebra rows / nested panels use --surface-2.
        for theme, tokens in (('dark', self.dark), ('light', self.light)):
            for token in ('--text-muted', '--text-faint'):
                with self.subTest(theme=theme, token=token):
                    self.assertGreaterEqual(
                        self._check(tokens, token, '--surface-2'), 4.5, token)


class ShellSourceA11yTest(unittest.TestCase):
    """The shared shell and partials carry the a11y scaffolding."""

    @classmethod
    def setUpClass(cls):
        cls.base = read(BASE_TEMPLATE)

    def test_lang_and_viewport(self):
        self.assertRegex(self.base, r'<html lang="en">')
        self.assertIn('name="viewport"', self.base)
        self.assertIn('width=device-width', self.base)

    def test_skip_link_is_the_first_focusable_control(self):
        self.assertIn('class="pc-skip-link"', self.base)
        self.assertIn('href="#pc-main"', self.base)
        self.assertIn('Skip to content', self.base)
        self.assertIn('id="pc-main"', self.base)
        self.assertLess(self.base.index('pc-skip-link'),
                        self.base.index('_partials/appbar.html'))

    def test_toast_is_a_live_region_and_refresh_has_a_polite_channel(self):
        toast = read(PARTIAL_DIR / 'toast.html')
        self.assertIn('id="toast"', toast)
        self.assertIn('role="status"', toast)
        self.assertIn('aria-live="polite"', toast)
        self.assertIn('role="alert"', toast)
        self.assertIn('aria-live="assertive"', toast)
        self.assertIn('pc-aria-live', toast)
        self.assertIn('pcAnnounce', toast)

    def test_no_external_assets_in_shell_or_partials(self):
        for path in [BASE_TEMPLATE] + sorted(PARTIAL_DIR.glob('*.html')):
            with self.subTest(path=path.name):
                self.assertIsNone(EXTERNAL_ASSET.search(read(path)))

    def test_auth_pages_are_offline_and_keep_their_ids(self):
        for page, ids in (('login.html', ('username', 'password')),
                          ('change_password.html', ('current', 'new', 'confirm'))):
            src = read(TEMPLATE_DIR / page)
            with self.subTest(page=page):
                self.assertIsNone(EXTERNAL_ASSET.search(src))
                self.assertNotIn('<style>', src)
                self.assertIn('pc-body--auth', src)
                for field in ids:
                    self.assertIn('id="%s"' % field, src)
                    self.assertIn('for="%s"' % field, src)


class StatePatternTest(unittest.TestCase):
    """Skeleton / empty / stale are present and wired into the shell."""

    @classmethod
    def setUpClass(cls):
        cls.css = read(APP_CSS)
        cls.flash = read(PARTIAL_DIR / 'flash.html')

    def test_empty_state_macro_and_style(self):
        self.assertIn('macro pc_empty', self.flash)
        self.assertIn('class="pc-empty"', self.flash)
        for sel in ('.pc-empty', '.pc-empty__title', '.pc-empty__body'):
            self.assertIn(sel, self.css)

    def test_skeleton_macro_and_style(self):
        self.assertIn('macro pc_skeleton', self.flash)
        self.assertIn('pc-skeleton', self.flash)
        self.assertIn('.pc-skeleton', self.css)
        self.assertIn('.pc-skeleton--row', self.css)
        # announced once, politely; the bars themselves are decorative.
        self.assertIn('aria-live="polite"', self.flash)
        self.assertIn('aria-hidden="true"', self.flash)

    def test_stale_chip_style(self):
        self.assertIn('.pc-chip--stale', self.css)

    def test_loading_button_state(self):
        self.assertIn("aria-busy='true'", self.css)
        self.assertIn('pc-spin', self.css)


if _HAVE_FLASK:
    _RenderBase = _AdminSessionMixin
else:  # pragma: no cover - only when the app cannot be imported
    _RenderBase = object


@unittest.skipUnless(_HAVE_FLASK, 'Flask app harness unavailable')
class RenderedA11yTest(_RenderBase, unittest.TestCase):
    """Minimal Flask render: the converted pages ship the a11y scaffolding."""

    def _render_login(self):
        resp = app.app.test_client().get('/login')
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def _render_change_password(self):
        resp = self.client.get('/change-password')
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def _render_home(self):
        with mock.patch.object(app, 'get_human_users', return_value=[]), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             mock.patch.object(app, 'is_in_bedtime', return_value=False), \
             mock.patch.object(app, 'bonus_state', return_value={'active': False}):
            resp = self.client.get('/')
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def _assert_common_scaffolding(self, html, auth=False):
        self.assertIn('<html lang="en">', html)
        self.assertIn('name="viewport"', html)
        self.assertIn('class="pc-skip-link"', html)
        self.assertIn('href="#pc-main"', html)
        self.assertIn('id="pc-main"', html)
        self.assertIn('id="toast"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('pc-aria-live', html)
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)
        if auth:
            self.assertIn('pc-body--auth', html)
            self.assertNotIn('<style>', html)
        else:
            self.assertNotIn('pc-body--auth', html)

    def test_login_render(self):
        html = self._render_login()
        self._assert_common_scaffolding(html, auth=True)
        self.assertIn('id="username"', html)
        self.assertIn('id="password"', html)

    def test_change_password_render(self):
        html = self._render_change_password()
        self._assert_common_scaffolding(html, auth=True)
        for field in ('current', 'new', 'confirm'):
            self.assertIn('id="%s"' % field, html)

    def test_home_render_keeps_thead_and_scope_in_the_stack(self):
        html = self._render_home()
        self._assert_common_scaffolding(html)
        self.assertIn('pc-table--stack', html)
        self.assertIn('<thead>', html)
        self.assertIn('scope="col"', html)


if __name__ == '__main__':
    unittest.main()


class TableHeaderRenderingTest(unittest.TestCase):
    """Regression: a sticky `thead th` under `border-collapse: collapse`
    renders the header one row low (blank first row). The table must use
    separate borders and no sticky header positioning."""

    def setUp(self):
        import re as _re
        css = read(APP_CSS)
        self.css = _re.sub(r'/\*.*?\*/', '', css, flags=_re.S)

    def test_table_uses_separate_borders(self):
        block = self.css[self.css.index('.pc-table {'):]
        block = block[:block.index('}')]
        self.assertIn('border-collapse: separate', block)
        self.assertIn('border-spacing: 0', block)
        self.assertNotIn('border-collapse: collapse', block)

    def test_thead_th_is_not_sticky(self):
        block = self.css[self.css.index('.pc-table thead th {'):]
        block = block[:block.index('}')]
        self.assertNotIn('position: sticky', block)
