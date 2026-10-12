"""Foundation guardrail for the mobile-first UI redesign (UI_UX_PLAN.md §7.1).

This is the **Phase 0/P1** version of the asset check. It covers exactly what
the foundation commit adds and nothing else:

* the vendored Bootstrap 5.3.3 CSS (offline promise — §6.3),
* the design-system stylesheet ``static/css/app.css`` (§3),
* the shared shell ``templates/base.html`` and its ``_partials/`` (§7.1),
* the converted pages (``index.html``, ``login.html``,
  ``change_password.html``) extend ``base.html``, carry no external
  ``http(s)://`` asset and keep the §7.4 pinned selectors,
* the Katalon selector contract is still intact, reusing the real validator in
  ``tests/test_katalon_objects.py`` rather than a weaker copy (§7.4).

Deliberately **not** asserted here: the *rendered* behaviour of each page
(that needs a Flask test client and lives in the per-page modules, e.g.
``tests/test_ui_home.py`` and ``tests/test_ui_user.py``). This module is the
stdlib-only, no-Flask guardrail: it checks the source of the converted pages
is offline and carries the design-system structure. ``admin_audit.html``
conversion lands in its own phase.

Stdlib only (``hashlib`` + ``re`` + ``unittest``), no Flask, no network.
"""

import hashlib
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / 'templates'
PARTIAL_DIR = TEMPLATE_DIR / '_partials'
CSS_DIR = ROOT / 'static' / 'css'
VENDOR_DIR = ROOT / 'static' / 'vendor' / 'bootstrap'
APP_CSS = CSS_DIR / 'app.css'
BOOTSTRAP_CSS = VENDOR_DIR / 'bootstrap.min.css'
BASE_TEMPLATE = TEMPLATE_DIR / 'base.html'

# Pinned in the candidate this foundation ships (see
# static/vendor/bootstrap/NOTICE.txt). A deliberate Bootstrap bump updates the
# NOTICE, this constant and the size window together.
BOOTSTRAP_SHA256 = ('3c8f27e6009ccfd710a905e6dcf12d0ee3c6f2ac7da05b0572d3e0d12e736fc8')
BOOTSTRAP_BYTES = 232803

EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''', re.IGNORECASE)

# Assets the shell must pull from /static (via url_for), never a CDN.
LOCAL_ASSETS = (
    'vendor/bootstrap/bootstrap.min.css',
    'css/app.css',
)

# ---------------------------------------------------------------------------
# Design-system inventory the pages will lean on (§3.1/§3.2). Names are the
# stable contract; the phase-conversion tests will assert them against real
# markup, this one asserts the stylesheet actually defines them.
# ---------------------------------------------------------------------------
CORE_TOKENS = (
    # tier 1 (a representative slice — the whole palette is documented in §3.1)
    '--pc-ink-900', '--pc-ink-800', '--pc-ink-700', '--pc-ink-600',
    '--pc-ink-500', '--pc-ink-400', '--pc-navy-700', '--pc-navy-800',
    '--pc-green-500', '--pc-green-600', '--pc-red-500', '--pc-red-700',
    '--pc-amber-400', '--pc-violet-500', '--pc-slate-100', '--pc-slate-400',
    '--pc-slate-500',
    # tier 2 semantic
    '--bg', '--surface', '--surface-2', '--surface-3', '--surface-raised',
    '--border', '--border-strong', '--text', '--text-muted', '--text-faint',
    '--link', '--focus-ring',
    # status pairs (the four contrast fixes)
    '--ok-fg', '--ok-bg', '--danger-fg', '--danger-bg', '--warn-fg', '--warn-bg',
    '--info-fg', '--info-bg', '--override-fg', '--override-bg',
    '--bedtime-fg', '--bedtime-bg', '--neutral-fg', '--neutral-bg',
    # text-safe status foregrounds + the duration-unit token (Phase 5)
    '--ok-text', '--warn-text', '--danger-text', '--info-text', '--unit-fg',
    # type / spacing / radius / elevation / motion / layout
    '--fs-xs', '--fs-sm', '--fs-base', '--fs-lg', '--fs-xl', '--fs-2xl',
    '--sp-1', '--sp-2', '--sp-3', '--sp-4', '--sp-5', '--sp-6', '--sp-8',
    '--r-sm', '--r-md', '--r-lg', '--r-pill',
    '--el-0', '--el-1', '--el-2',
    '--dur', '--ease',
    '--bp-sm', '--bp-md', '--bp-lg', '--bp-xl',
    '--nav-h', '--tabbar-h', '--touch', '--content', '--content-wide',
    # tier 3 component
    '--btn-bg', '--chip-bg', '--card-radius', '--field-bg', '--appbar-bg',
    '--tabbar-bg',
)

COMPONENT_CLASSES = (
    '.pc-btn', '.pc-btn--primary', '.pc-btn--danger', '.pc-btn--warn',
    '.pc-btn--neutral', '.pc-btn--ghost', '.pc-btn--sm', '.pc-btn--icon',
    '.pc-chip', '.pc-chip--ok', '.pc-chip--danger', '.pc-chip--warn',
    '.pc-chip--info', '.pc-chip--override', '.pc-chip--bedtime',
    '.pc-status', '.pc-status--locked', '.pc-card', '.pc-summary',
    '.pc-table', '.pc-table--stack', '.pc-disclosure', '.pc-field',
    '.pc-input', '.pc-duration', '.pc-time', '.pc-toast', '.pc-dialog',
    '.pc-empty', '.pc-skeleton', '.pc-meter', '.pc-banner', '.pc-banner--danger',
    '.pc-appbar', '.pc-tabbar', '.pc-tab', '.pc-skip-link', '.pc-auth',
    '.pc-list', '.pc-visually-hidden',
    # Phase 5 additions: auth body, light-theme chips, legacy hooks, states.
    '.pc-body--auth', '.pc-chip--stale', '.pc-chip--bonus', '.pc-skeleton-rows',
    '.pc-unit', '.override-mode', '.group-detail', '.actions',
)

PARTIALS = (
    'appbar.html',
    'tabbar.html',
    'toast.html',
    'dialog.html',
    'icons.html',
    'flash.html',
    'insecure_banner.html',
)


def read(path):
    return path.read_text(encoding='utf-8')


def load_katalon_validator():
    """Import the real Katalon validator so this guardrail cannot drift from it."""
    spec = importlib.util.spec_from_file_location(
        'pc_katalon_objects', ROOT / 'tests' / 'test_katalon_objects.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VendoredBootstrapTest(unittest.TestCase):
    """The offline promise rests on a complete, local Bootstrap CSS."""

    @classmethod
    def setUpClass(cls):
        cls.css = read(BOOTSTRAP_CSS)

    def test_exists_at_the_pinned_path_with_the_pinned_size(self):
        self.assertTrue(BOOTSTRAP_CSS.is_file(),
                        'vendored %s is missing' % BOOTSTRAP_CSS)
        size = BOOTSTRAP_CSS.stat().st_size
        self.assertEqual(BOOTSTRAP_BYTES, size,
                         'vendored Bootstrap size changed; update NOTICE.txt '
                         'and BOOTSTRAP_BYTES together')
        self.assertGreater(size, 200_000,
                           'a Bootstrap dist this small is truncated')

    def test_sha256_matches_the_recorded_dist(self):
        digest = hashlib.sha256(BOOTSTRAP_CSS.read_bytes()).hexdigest()
        self.assertEqual(BOOTSTRAP_SHA256, digest,
                         'vendored Bootstrap differs from the recorded 5.3.3 '
                         'dist (update NOTICE.txt + BOOTSTRAP_SHA256)')

    def test_carries_the_version_banner(self):
        self.assertIn('Bootstrap', self.css)
        self.assertRegex(self.css, r'Bootstrap\s+v5\.3\.3',
                         'version banner missing from the dist')
        self.assertIn('The Bootstrap Authors', self.css)
        self.assertIn('Licensed under MIT', self.css)

    def test_looks_complete(self):
        # Opening charset, a real chunk of rules, the grid, utilities and a
        # clean close — a truncated download would fail one of these.
        self.assertTrue(self.css.lstrip().startswith('@charset "UTF-8";'))
        self.assertGreater(self.css.count('{'), 2_000)
        for needle in ('.container-fluid', '.col-md-6', '.d-none',
                       '.btn-primary', '.table-responsive'):
            self.assertIn(needle, self.css,
                          'Bootstrap dist is missing %s' % needle)
        self.assertTrue(self.css.rstrip().endswith('*/'),
                        'dist does not end with a complete comment/rule')

    def test_has_no_external_urls(self):
        self.assertNotRegex(self.css, r'url\(\s*["\']?https?://',
                            'vendored CSS must not fetch anything off-box')

    def test_no_javascript_bundle_is_vendored(self):
        # The redesign uses native <dialog>/<details>, so bootstrap JS stays out
        # (smaller surface, no jQuery/Popper).
        stray = sorted(p.name for p in VENDOR_DIR.glob('*.js'))
        self.assertEqual([], stray, 'do not vendor Bootstrap JS: %s' % stray)


class AppCssTest(unittest.TestCase):
    """The design system exists and defines the agreed contract."""

    @classmethod
    def setUpClass(cls):
        cls.css = read(APP_CSS)

    def test_is_a_non_trivial_stylesheet(self):
        self.assertGreater(len(self.css), 8_000,
                           'app.css looks like a stub')

    def test_defines_the_core_tokens(self):
        for token in CORE_TOKENS:
            self.assertIn(token + ':', self.css, 'missing token %s' % token)

    def test_defines_the_component_classes(self):
        for selector in COMPONENT_CLASSES:
            self.assertIn(selector, self.css,
                          'missing component selector %s' % selector)

    def test_defines_the_stack_table_pattern(self):
        # the phone fix: semantic table kept, thead clipped (not display:none),
        # labels injected from data-label.
        self.assertIn('content: attr(data-label)', self.css)
        self.assertIn('pc-table--stack thead', self.css)
        self.assertIn('clip:rect', self.css.replace(' ', ''))
        self.assertIn('@media (max-width: 767.98px)', self.css)

    def test_defines_accessibility_states(self):
        self.assertIn(':focus-visible', self.css)
        self.assertIn('outline: 2px solid var(--focus-ring)', self.css)
        self.assertIn('prefers-reduced-motion: reduce', self.css)
        self.assertIn('prefers-color-scheme: light', self.css)
        self.assertIn('.pc-toast-region', self.css)   # toast live-region shell
        self.assertIn('.pc-toast--error', self.css)

    def test_enforces_touch_targets(self):
        self.assertIn('--touch: 44px', self.css)
        self.assertIn('min-height: var(--touch)', self.css)
        # the small button keeps WCAG 2.2 2.5.8 headroom
        self.assertRegex(self.css, r'\.pc-btn--sm\s*\{[^}]*min-height:\s*32px')

    # --------------------------------------------------- Phase 5 (a11y/theme)
    def test_light_theme_is_an_override_not_a_rewrite(self):
        # dark is the bare :root default; light only overrides tokens.
        self.assertIn("prefers-color-scheme: light", self.css)
        self.assertIn("[data-theme='light']", self.css)
        self.assertIn('--bg: #f4f5f8', self.css)
        self.assertIn('--surface: #ffffff', self.css)
        # the two chips that fail AA on white are re-tuned for the light theme
        self.assertIn('--info-bg: #2368a8', self.css)
        self.assertIn('--warn-bg: #8a5a00', self.css)

    def test_auth_shell_hides_the_chrome_from_the_stylesheet(self):
        self.assertIn('.pc-body--auth', self.css)
        self.assertRegex(
            self.css,
            r'\.pc-body--auth\s+\.pc-appbar\s*,\s*\.pc-body--auth\s+\.pc-tabbar\s*\{[^}]*display:\s*none')

    def test_legacy_control_hooks_are_styled(self):
        for cls in ('btn-freeze', 'btn-unlock', 'btn-hide', 'btn-show-hidden',
                    'btn-group-toggle', 'btn-bonus', 'btn-bedtime-save',
                    'btn-bedtime-clear', 'btn-lock', 'btn-logout',
                    'process-name', 'group-chevron', 'group-row',
                    'group-detail', 'badge-bonus', 'badge-override',
                    'override-mode'):
            with self.subTest(cls=cls):
                self.assertIn(cls, self.css, 'unstyled legacy hook %s' % cls)
        self.assertIn(':not(.pc-btn)', self.css)
        self.assertIn(':not(.pc-chip)', self.css)

    def test_skeleton_and_stale_states(self):
        self.assertIn('.pc-skeleton-rows', self.css)
        self.assertIn('.pc-chip--stale', self.css)
        self.assertIn('aria-busy', self.css)


class BaseTemplateTest(unittest.TestCase):
    """The shared shell is self-contained and stays offline."""

    @classmethod
    def setUpClass(cls):
        cls.html = read(BASE_TEMPLATE)

    def test_has_a_doctype_lang_and_viewport(self):
        # a leading Jinja comment is fine; the doctype must still open the doc
        self.assertRegex(self.html, r'(?is)<!doctype html>\s*<html lang="en">')
        self.assertIn('name="viewport"', self.html)
        self.assertIn('width=device-width', self.html)

    def test_links_the_local_assets(self):
        for asset in LOCAL_ASSETS:
            self.assertIn(asset, self.html,
                          'base.html must link the local %s' % asset)
        self.assertIn("url_for('static'", self.html)
        self.assertIn('bootstrap.min.css', self.html)
        self.assertIn('app.css', self.html)

    def test_has_no_external_asset_urls(self):
        self.assertIsNone(EXTERNAL_ASSET.search(self.html),
                          'base.html must not reference a CDN')

    def test_includes_every_partial(self):
        for name in ('appbar', 'tabbar', 'toast', 'dialog', 'icons', 'flash',
                     'insecure_banner'):
            self.assertIn("_partials/%s.html" % name, self.html,
                          'base.html must include the %s partial' % name)

    def test_exposes_the_page_blocks(self):
        for block in ('title', 'head', 'content', 'scripts'):
            self.assertIn('{%% block %s %%}' % block, self.html,
                          'base.html must expose a %s block' % block)

    def test_has_no_type_equals_time_input(self):
        # contract preserved from test_time12h (12h selects only)
        self.assertNotIn('type="time"', self.html)

    def test_carries_the_a11y_scaffolding(self):
        # skip link, main landmark, polite live region for refresh + toasts (P5)
        self.assertIn('class="pc-skip-link"', self.html)
        self.assertIn('href="#pc-main"', self.html)
        self.assertIn('id="pc-main"', self.html)
        self.assertIn('tabindex="-1"', self.html)


class UserPageConversionTest(unittest.TestCase):
    """`user.html` extends the shell and uses the shared design system.

    Source-level (no Flask): the rendered page is covered by
    tests/test_ui_user.py. This is the guardrail that the busiest screen
    left the CDN/inline-CSS world and adopted the shell + stack/disclosure
    markup the plan requires (UI_UX_PLAN.md §5.4, Phase 3).
    """

    @classmethod
    def setUpClass(cls):
        cls.html = read(TEMPLATE_DIR / 'user.html')

    def test_extends_the_shared_shell(self):
        self.assertTrue(self.html.lstrip().startswith('{% extends "base.html" %}'),
                        'user.html must extend templates/base.html')
        self.assertIn('{% block content %}', self.html)
        self.assertIn('{% block scripts %}', self.html)

    def test_has_no_doctype_head_or_cdn(self):
        self.assertNotIn('<!DOCTYPE html>', self.html)
        self.assertNotIn('<head>', self.html)
        self.assertIsNone(EXTERNAL_ASSET.search(self.html),
                          'user.html must not reference a CDN')

    def test_uses_the_stacked_table_pattern_with_labels(self):
        self.assertIn('pc-table--stack', self.html)
        for label in ('Category', 'Limit (per day)', 'Used', 'Left', 'Status',
                      'Usage', 'Command', 'Actions', 'Allowance'):
            with self.subTest(label=label):
                self.assertIn('data-label="%s"' % label, self.html)
        # semantic theads stay in the DOM (clipped on phones, not removed)
        self.assertGreaterEqual(self.html.count('<thead>'), 4)

    def test_uses_details_disclosures(self):
        self.assertIn('class="pc-disclosure"', self.html)
        self.assertIn('<details', self.html)
        for section_id in ('bedtime-section', 'bonus-section',
                           'budgets-section', 'weekly-windows'):
            with self.subTest(section=section_id):
                self.assertIn('id="%s"' % section_id, self.html)

    def test_destructive_actions_use_the_dialog_helper(self):
        self.assertIn('pcConfirm(', self.html)
        self.assertNotIn('window.confirm(', self.html)
        self.assertNotIn('!confirm(', self.html)
        # Emergency logout keeps its deliberate type-to-confirm barrier.
        self.assertIn('requireText', self.html)

    def test_budget_meter_is_present(self):
        self.assertIn('pc-meter', self.html)
        self.assertIn('pc-meter__fill', self.html)

    def test_pinned_ids_and_classes_still_render(self):
        pinned_ids = (
            'account-status', 'account-status-text', 'account-badges',
            'account-actions', 'bonus-status', 'bonus-hours', 'bonus-minutes',
            'budget-{{ cat }}-hours', 'budget-{{ cat }}-minutes',
            'budget-used-{{ cat }}', 'budget-left-{{ cat }}',
            'budget-status-{{ cat }}', 'budget-window-{{ cat }}',
            'budget-meter-{{ cat }}', 'process-section', 'hidden-procs',
        )
        pinned_classes = (
            'back-link', 'subtitle', 'badge-bonus', 'btn-lock', 'btn-unlock',
            'btn-logout', 'btn-group-toggle', 'btn-hide', 'btn-show-hidden',
            'group-chevron', 'process-name', 'group-row', 'group-detail',
            'refresh-info', 'override-mode', 'window-row', 'day-checkbox',
        )
        for token in pinned_ids:
            with self.subTest(kind='id', token=token):
                self.assertIn('id="%s"' % token, self.html)
        for token in pinned_classes:
            with self.subTest(kind='class', token=token):
                self.assertIn(token, self.html)
        for attr in ('aria-expanded', 'aria-controls', 'data-window-id',
                     'data-group-cat', 'data-group-comm', 'data-comm',
                     'data-label'):
            with self.subTest(kind='attr', attr=attr):
                self.assertIn(attr, self.html)

    def test_refresh_info_and_param_names_are_kept(self):
        self.assertIn('Status updates automatically every 10 seconds',
                      self.html)
        for name in ('start_hour', 'end_hour', 'window_start_hour',
                     'window_end_hour', 'all_days', 'day_0'):
            with self.subTest(name=name):
                self.assertIn(name, self.html)

    def test_router_needs_for_the_shell_are_satisfied(self):
        # The shell's app bar/tab bar link these; the page must keep the
        # back link (Katalon `a.back-link`) and the shared toast container
        # comes from base (not duplicated here).
        self.assertIn('class="back-link">← Back to Parental Controls</a>',
                      self.html)
        self.assertNotIn('id="toast"', self.html)

    def test_insecure_banner_is_owned_by_the_shell(self):
        # base.html includes _partials/insecure_banner.html; the page keeps
        # the legacy partial reference discoverable for the TLS guardrail.
        self.assertIn('_insecure_banner.html', self.html)


class AdminConsoleConversionTest(unittest.TestCase):
    """`admin_audit.html` extends the shell and adopts the design system.

    Source-level (no Flask): the rendered page is covered by
    tests/test_ui_admin.py. Phase 4 (UI_UX_PLAN.md §5.5) converts the last
    desktop-shaped page; this is the guardrail that it left the CDN/inline-CSS
    world and carries the stack/banner/dialog markup the plan requires.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = read(TEMPLATE_DIR / 'admin_audit.html')

    def test_extends_the_shared_shell(self):
        self.assertTrue(
            self.html.lstrip().startswith('{% extends "base.html" %}'),
            'admin_audit.html must extend templates/base.html')
        self.assertIn('{% block content %}', self.html)
        self.assertIn('{% block scripts %}', self.html)

    def test_has_no_doctype_head_cdn_or_inline_style(self):
        self.assertNotIn('<!DOCTYPE html>', self.html)
        self.assertNotIn('<head>', self.html)
        self.assertNotIn('<style', self.html)
        self.assertIsNone(EXTERNAL_ASSET.search(self.html),
                          'admin_audit.html must not reference a CDN')

    def test_uses_the_stacked_table_pattern_with_labels(self):
        self.assertIn('pc-table--stack', self.html)
        for label in ('Seq', 'Time (local)', 'Actor', 'Action', 'Target',
                      'Detail', 'Username', 'Tier', 'Status', 'Actions'):
            with self.subTest(label=label):
                self.assertIn('data-label="%s"' % label, self.html)
        # semantic theads stay in the DOM (clipped on phones, not removed)
        self.assertGreaterEqual(self.html.count('<thead>'), 2)

    def test_chain_status_is_a_banner_keeping_legacy_classes(self):
        self.assertIn('pc-banner--', self.html)
        self.assertIn('chain-intact', self.html)
        self.assertIn('chain-broken', self.html)

    def test_remove_uses_the_dialog_helper(self):
        self.assertIn('pcConfirm(', self.html)
        self.assertNotIn('window.confirm(', self.html)
        self.assertNotIn('!confirm(', self.html)

    def test_pinned_selectors_still_render(self):
        pinned_ids = ('audit-section', 'chain-status', 'audit-count',
                      'audit-limit', 'audit-table', 'load-more',
                      'admin-section', 'admin-table', 'onetime-password',
                      'otp-user', 'otp-value', 'new-admin-username',
                      'admin-add')
        pinned_classes = ('audit-row', 'audit-time', 'audit-detail',
                          'admin-row', 'badge-protected', 'btn-lock',
                          'btn-unlock', 'refresh-info')
        for token in pinned_ids:
            with self.subTest(kind='id', token=token):
                self.assertIn('id="%s"' % token, self.html)
        for token in pinned_classes:
            with self.subTest(kind='class', token=token):
                self.assertIn(token, self.html)
        for attr in ('data-seq', 'data-at', 'data-username', 'data-label'):
            with self.subTest(kind='attr', attr=attr):
                self.assertIn(attr, self.html)


class PartialTest(unittest.TestCase):
    """The reusable pieces are present, offline and wired for accessibility."""

    def test_every_partial_exists_and_is_non_trivial(self):
        for name in PARTIALS:
            path = PARTIAL_DIR / name
            with self.subTest(partial=name):
                self.assertTrue(path.is_file(), '%s is missing' % name)
                self.assertGreater(len(read(path).strip()), 100,
                                   '%s is an empty stub' % name)

    def test_partials_have_no_external_urls(self):
        for name in PARTIALS:
            body = read(PARTIAL_DIR / name)
            with self.subTest(partial=name):
                self.assertIsNone(EXTERNAL_ASSET.search(body),
                                  '%s references a CDN' % name)

    def test_toast_is_a_live_region_with_the_pinned_id(self):
        body = read(PARTIAL_DIR / 'toast.html')
        self.assertIn('id="toast"', body)
        self.assertIn('role="status"', body)
        self.assertIn('aria-live="polite"', body)
        self.assertIn('role="alert"', body)

    def test_flash_partial_offers_the_state_macros(self):
        body = read(PARTIAL_DIR / 'flash.html')
        self.assertIn('macro pc_empty', body)
        self.assertIn('macro pc_skeleton', body)
        self.assertIn('class="pc-empty"', body)

    def test_dialog_uses_native_dialog_and_showmodal(self):
        body = read(PARTIAL_DIR / 'dialog.html')
        self.assertIn('<dialog', body)
        self.assertIn('class="pc-dialog', body)
        self.assertIn('showModal()', body)
        self.assertIn('pc-dialog--danger', body)
        self.assertIn('{% macro pc_confirm_dialog(', body)

    def test_appbar_and_tabbar_mark_the_active_destination(self):
        for name in ('appbar.html', 'tabbar.html'):
            body = read(PARTIAL_DIR / name)
            with self.subTest(partial=name):
                self.assertIn('aria-current="page"', body)

    def test_icons_are_an_inline_svg_sprite(self):
        body = read(PARTIAL_DIR / 'icons.html')
        self.assertIn('<symbol id="pc-i-', body)
        self.assertNotIn('url(', body.lower().replace('url_for', ''))


class ConvertedPageTest(unittest.TestCase):
    """The pages converted in the Home/auth phase extend the shell offline and
    keep the §7.4 pinned id/class/text contract."""

    # Per-page pinned selectors/text that must stay literally present.
    PINNED = {
        'index.html': (
            '{% extends "base.html" %}', 'id="row-', 'class="pc-btn pc-btn--ghost btn-details"',
            'btn-lock', 'btn-unlock', 'id="admin-panel"', 'id="admin-list"',
            'id="new-admin-username"', 'class="refresh-info"',
            'Status updates automatically every 10 seconds',
            'id="device-selector"', 'switchDevice',
            'pc-summary', 'pc-table--stack', 'data-label="Username"',
            'Parental Controls', 'Logged in as',
        ),
        'login.html': (
            '{% extends "base.html" %}', 'class="pc-auth"', 'id="username"',
            'name="username"', 'id="password"', 'name="password"',
            '>Log in<', 'Parental Controls',
        ),
        'change_password.html': (
            '{% extends "base.html" %}', 'class="pc-auth"', 'id="current"',
            'name="current"', 'id="new"', 'name="new"', 'id="confirm"',
            'name="confirm"', '>Change password<', 'pc-banner--info info',
            'pc-field__error error',
        ),
    }

    def test_converted_pages_extend_base_and_are_offline(self):
        for name in self.PINNED:
            body = read(TEMPLATE_DIR / name)
            with self.subTest(page=name):
                self.assertIn('{% extends "base.html" %}', body)
                self.assertIsNone(EXTERNAL_ASSET.search(body),
                                  '%s references an external asset' % name)
                self.assertNotIn('cdn.jsdelivr.net', body)
                self.assertNotIn('http://', body)
                self.assertNotIn('https://', body)

    def test_converted_pages_keep_their_pinned_selectors(self):
        for name, needles in self.PINNED.items():
            body = read(TEMPLATE_DIR / name)
            for needle in needles:
                with self.subTest(page=name, needle=needle):
                    self.assertIn(needle, body,
                                  '%s lost pinned literal %r' % (name, needle))

    def test_converted_scripts_drop_window_confirm(self):
        for name in self.PINNED:
            body = read(TEMPLATE_DIR / name)
            with self.subTest(page=name):
                self.assertNotIn('window.confirm', body)
                self.assertNotIn('confirm(', body)


class KatalonContractTest(unittest.TestCase):
    """The selector/expected-text contract is untouched by adding the shell."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.katalon = load_katalon_validator()
        except Exception as exc:  # pragma: no cover - surfaces as a skip
            raise unittest.SkipTest('katalon validator unavailable: %s' % exc)

    def test_every_katalon_selector_still_resolves(self):
        mod = self.katalon
        if not (mod.OBJECT_DIR.is_dir() and mod.CASE_DIR.is_dir()):
            self.skipTest('katalon objects/test-cases absent')
        checked = 0
        for name, data in list(mod.load_dir(mod.OBJECT_DIR).items()) + \
                list(mod.load_dir(mod.CASE_DIR).items()):
            for step in mod.walk(data['steps']):
                if step.get('type') not in ('click', 'input', 'verifyText',
                                            'verifyElementVisible',
                                            'dragAndDrop'):
                    continue
                for selector in (step.get('selector'), step.get('target')):
                    if not selector:
                        continue
                    checked += 1
                    with self.subTest(object=name, selector=selector):
                        self.assertEqual([], mod.selector_problems(selector),
                                         'foundation must not break selector '
                                         '%r' % selector)
        self.assertGreater(checked, 60, 'suspiciously few selectors checked')

    def test_pinned_selectors_that_have_not_moved_yet_still_exist(self):
        """The §7.4 inventory that still lives in the un-converted pages."""
        mod = self.katalon
        pinned_ids = (
            'username', 'password', 'current', 'new', 'confirm', 'row-',
            'admin-panel', 'admin-list', 'new-admin-username', 'toast',
            'account-status', 'account-status-text', 'account-badges',
            'account-actions', 'bonus-status', 'bonus-hours', 'bonus-minutes',
            'budget-', 'hidden-procs', 'process-section',
        )
        pinned_classes = (
            'back-link', 'subtitle', 'error', 'info', 'badge-bonus',
            'btn-lock', 'btn-unlock', 'btn-logout', 'status-locked',
            'status-unlocked', 'status-unknown', 'btn-group-toggle',
            'btn-hide', 'btn-show-hidden', 'group-chevron', 'process-name',
            'group-row', 'group-detail', 'refresh-info',
        )
        pinned_attrs = ('data-at', 'data-window-id', 'data-group-cat',
                        'data-group-comm', 'data-comm', 'aria-expanded',
                        'aria-controls')
        pinned_text = ('Parental Controls', 'Log in', 'Change password',
                       'Locked', 'Unlocked', 'Invalid username or password.',
                       'Current password is incorrect.', 'Grant custom',
                       'Status updates automatically every 10 seconds')
        for token in pinned_ids:
            with self.subTest(kind='id', token=token):
                self.assertTrue(mod.tokens_exist(token, mod.IDS),
                                'pinned id %r vanished' % token)
        for token in pinned_classes:
            with self.subTest(kind='class', token=token):
                self.assertTrue(mod.tokens_exist(token, mod.CLASSES),
                                'pinned class %r vanished' % token)
        for token in pinned_attrs:
            with self.subTest(kind='attr', token=token):
                self.assertIn(token, mod.TEMPLATE_CORPUS,
                              'pinned attribute %r vanished' % token)
        for text in pinned_text:
            with self.subTest(kind='text', text=text):
                self.assertIn(text, mod.TEXT_CORPUS,
                              'pinned text %r vanished' % text)


if __name__ == '__main__':
    unittest.main()
