"""Static validator for the Katalon Recorder QA suite in ``katalon/``.

The browser suite is the parent's hands-on regression check; this module is the
cheap CI guard that keeps it honest without a browser, a running panel, or any
third-party dependency.

The suite is a single **Selenium-IDE HTML** file,
``katalon/parental-controls.html`` — the format Katalon Recorder itself writes
(``panel/js/IO/save_file.js``) and the only one its loader accepts
(``panel/js/IO/load_file.js`` requires the file name to contain ``.krecorder``
or ``.html``). There is no object repository in Katalon Recorder, so the old
``katalon/objects/*.json`` + ``katalon/test-cases/*.json`` pairs were flattened
into one ``<table>`` per test case, with a ``#`` comment row naming the original
object at every seam.

What this module checks, with no browser and no Katalon install:

* the file is the XHTML suite the Recorder ships (header, one table per case,
  the eight documented case names);
* every command is a real Selenese command the Recorder can run, every
  ``if``/``else``/``endIf`` is balanced, every ``pause`` is milliseconds, and
  every ``open`` comes from ``${BASE_URL}``;
* **every locator and every expected string still exists in the current
  templates** — that is what makes the suite self-updating: rename an id in
  ``templates/user.html`` and this fails loudly instead of the parent's run
  failing at 11pm on the desktop;
* budget categories match ``budgets.py``;
* the destructive actions confirm in the shared native ``<dialog>``
  (``#pc-confirm [data-pc-dialog-confirm]``) and no step still expects a
  browser alert (``closeAlert`` etc. are gone);
* the object seams name the expected objects, each case starts at the session
  reset and ends at logout, logs in exactly once, and a state-changing object
  only runs in the case that exists to exercise it;
* nothing here hardcodes a real account, password or checkout path;
* ``katalon/README.md`` still describes the suite and the current install flow.

Stdlib only (``re`` + ``unittest`` + ``html.parser``), no Flask import, no
network, no Katalon install, no JSON.
"""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KATALON_DIR = ROOT / 'katalon'
SUITE = KATALON_DIR / 'parental-controls.html'
README = KATALON_DIR / 'README.md'
TEMPLATE_DIR = ROOT / 'templates'
APP_PY = ROOT / 'app.py'
BUDGETS_PY = ROOT / 'budgets.py'

# The eight test cases the suite must contain, by exact table title.
REQUIRED_CASES = {
    '01_login_wrong_password_then_success',
    '02_change_password_first_login',
    '03_user_details_page_smoke',
    '04_process_groups_expand_hide_show',
    '05_bonus_time_shortcut_buttons',
    '06_bonus_time_custom_grant_and_revoke',
    '07_time_budget_limit_freeze_toggle',
    '08_manual_unlock_round_trip',
}

# Every original object name. The suite flattens the referenced ones (a `#`
# comment row marks each seam); the two helpers below are deliberately dropped
# because Katalon Recorder has no object repository to keep them in.
ALL_OBJECTS = {
    '00_setup_reset_session',
    '01_set_test_config',
    '01b_set_admin_credentials',
    '02_open_login_page',
    '03_login_wrong_password',
    '04_login_good_password',
    '04b_login_tolerate_must_change',
    '05_logout',
    '06_change_password_verify_form',
    '07_change_password_wrong_current',
    '08_change_password_set_new',
    '09_navigate_to_index',
    '10_open_user_details',
    '11_verify_account_status',
    '12_verify_process_groups',
    '13_expand_group_detail',
    '14_hide_and_show_process',
    '15_verify_budget_rows',
    '16_budget_set_limit',
    '17_budget_freeze_toggle',
    '18_budget_clear_limit',
    '19_bonus_verify_status',
    '20_bonus_grant_shortcuts',
    '21_bonus_grant_custom',
    '22_bonus_revoke',
    '23_auto_refresh_health',
    '24_manual_lock',
    '25_manual_unlock',
    '90_example_drag_and_drop',
}
UNREFERENCED_OK = {
    '01b_set_admin_credentials',   # opt-in helper, documented in the README
    '90_example_drag_and_drop',    # dragAndDrop template for a future UI
}
EXPECTED_USED_OBJECTS = ALL_OBJECTS - UNREFERENCED_OK

LOGIN_SEAMS = {'04_login_good_password', '04b_login_tolerate_must_change'}

# Objects that change real state on the target account. Each one may only
# appear in the case that exists to exercise it, so no smoke case can ever
# lock, freeze or grant anything by accident.
STATE_CHANGING = {
    '04_process_groups_expand_hide_show': {'14_hide_and_show_process'},
    '05_bonus_time_shortcut_buttons': {'20_bonus_grant_shortcuts',
                                       '22_bonus_revoke'},
    '06_bonus_time_custom_grant_and_revoke': {'21_bonus_grant_custom',
                                              '22_bonus_revoke'},
    '07_time_budget_limit_freeze_toggle': {'16_budget_set_limit',
                                           '17_budget_freeze_toggle',
                                           '18_budget_clear_limit'},
    '08_manual_unlock_round_trip': {'24_manual_lock', '25_manual_unlock'},
}

# Selenese commands this suite is allowed to use. Commands Katalon Recorder
# supports but this suite has no use for (select/addSelection) are listed so a
# future addition does not fail here for the wrong reason.
ALLOWED_COMMANDS = {
    'open', 'click', 'type', 'store', 'echo', 'pause',
    'deleteAllVisibleCookies',
    'verifyElementPresent', 'verifyText',
    'assertElementPresent', 'assertElementNotPresent',
    'if', 'elseIf', 'else', 'endIf',
    'select', 'addSelection',
    '#',
}
# Browser-alert commands. The redesign replaced every confirm() with the shared
# native <dialog>, so these can never be right again in this suite.
ALERT_COMMANDS = {
    'closeAlert', 'chooseOkOnNextConfirmation', 'chooseCancelOnNextConfirmation',
    'waitForAlert', 'waitForConfirmation', 'storeAlert',
    'assertAlert', 'assertConfirmation', 'verifyAlert', 'verifyConfirmation',
}

# Commands whose target is an element locator.
LOCATOR_COMMANDS = {
    'click', 'type', 'verifyText', 'verifyElementPresent',
    'assertElementPresent', 'assertElementNotPresent', 'select',
}

# Clicks that open the shared confirmation dialog on the current pages.
CONFIRM_TRIGGERS = ('.btn-lock', '.btn-unlock', '.btn-freeze',
                    "text())='Revoke'", "text())='Grant custom'")
CONFIRM_CLICK = 'css=#pc-confirm [data-pc-dialog-confirm]'

# Variables the suite must actually use (never a literal value).
REQUIRED_VARIABLES = {
    'BASE_URL', 'ADMIN_USER', 'ADMIN_PASSWORD', 'TEST_USER', 'WRONG_PASSWORD',
    'NEW_ADMIN_PASSWORD', 'TEST_CATEGORY', 'BUDGET_HOURS', 'BUDGET_MINUTES',
    'BONUS_HOURS', 'BONUS_MINUTES',
}

# Accounts that must never be named anywhere in the QA suite (hard rule).
FORBIDDEN_ACCOUNTS = ('tester', 'bluefox', 'pcagent', 'stan', 'root')

# Classes the shared shell builds at runtime (string concatenation in
# templates/_partials/toast.html: 'pc-toast pc-toast--' + level), so they never
# appear as a literal class="..." attribute.
RUNTIME_CLASSES = {'pc-toast--error', 'pc-toast--success', 'pc-toast--info'}

JINJA = re.compile(r'\{\{.*?\}\}|\{%.*?%\}', re.S)
VARIABLE = re.compile(r'\$\{[^}]*\}')
ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


# --------------------------------------------------------------------------
# The suite file -> {case name: [(command, target, value), ...]}
# --------------------------------------------------------------------------
class SuiteParser(HTMLParser):
    """Read the Selenium-IDE tables, ignoring the <datalist> option lists."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []            # [{'name': str, 'rows': [cells, ...]}]
        self._table = None
        self._section = None
        self._cells = None
        self._cell = None
        self._datalist_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self._table = {'name': None, 'rows': [], 'attrs': dict(attrs)}
        elif tag in ('thead', 'tbody'):
            self._section = tag
        elif tag == 'tr':
            self._cells = []
        elif tag == 'td':
            self._cell = []
        elif tag == 'datalist':
            self._datalist_depth += 1

    def handle_data(self, data):
        if self._cell is not None and self._datalist_depth == 0:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'datalist':
            self._datalist_depth = max(0, self._datalist_depth - 1)
        elif tag == 'td':
            if self._cell is not None and self._cells is not None:
                self._cells.append(''.join(self._cell).strip())
            self._cell = None
        elif tag == 'tr':
            if self._cells:
                if self._section == 'thead' and self._table is not None:
                    self._table['name'] = self._cells[0]
                elif self._section == 'tbody' and self._table is not None:
                    cells = (self._cells + ['', '', ''])[:3]
                    self._table['rows'].append(tuple(cells))
            self._cells = None
        elif tag in ('thead', 'tbody'):
            self._section = None
        elif tag == 'table':
            if self._table is not None:
                self.tables.append(self._table)
            self._table = None


SUITE_TEXT = SUITE.read_text(encoding='utf-8') if SUITE.is_file() else ''
_parser = SuiteParser()
_parser.feed(SUITE_TEXT)
CASES = {t['name']: t['rows'] for t in _parser.tables if t['name']}
TABLE_ATTRS = [t.get('attrs', {}) for t in _parser.tables]
TOTAL_STEPS = sum(len(rows) for rows in CASES.values())


def read(path):
    return path.read_text(encoding='utf-8')


# Templates the selector/expected-text contract resolves against. `base.html`
# composes the shared shell from `templates/_partials/*`, so the partials belong
# to the corpus too: a selector that moves from a converted page into the shell
# (e.g. `#toast`, now only in `_partials/toast.html`) must keep resolving
# instead of reading as "vanished". The selector itself is unchanged.
TEMPLATES = {p.name: read(p) for p in sorted(TEMPLATE_DIR.glob('*.html'))}
_PARTIAL_DIR = TEMPLATE_DIR / '_partials'
if _PARTIAL_DIR.is_dir():
    TEMPLATES.update({'_partials/' + p.name: read(p)
                      for p in sorted(_PARTIAL_DIR.glob('*.html'))})
TEMPLATE_CORPUS = '\n'.join(TEMPLATES.values())
# Server-rendered strings (login/change-password errors, flash messages) live in
# app.py, so expected-text checks look there too.
TEXT_CORPUS = TEMPLATE_CORPUS + '\n' + read(APP_PY)


def attr_candidates(kind):
    """Every value templates use for ``kind`` (class/id/...), jinja-aware.

    A jinja-templated attribute (``id="budget-{{ cat }}"``) yields its static
    prefix, so a concrete selector (``#budget-games``) still resolves.

    A ``class`` attribute holds space-separated tokens, and a selector pins a
    *token* (``.btn-lock``), not the whole value — so a button decorated with
    the design system (``class="pc-btn pc-btn--danger btn-lock"``) still
    resolves ``.btn-lock``. A jinja expression inside a token contributes its
    static prefix (``class="status-{{ … }}"`` -> ``status-``). Non-class
    attributes stay whole values.
    """
    out = set()
    for text in TEMPLATES.values():
        for name, value in ATTR.findall(text):
            if name != kind:
                continue
            if kind == 'class':
                # Blank out jinja (which may contain spaces) before splitting,
                # then keep the literal prefix of each token.
                for part in JINJA.sub('\x00', value).split():
                    prefix = part.split('\x00', 1)[0]
                    if prefix:
                        out.add(prefix)
            elif JINJA.search(value):
                out.add(value[:JINJA.search(value).start()])
            else:
                out.add(value)
    return out


def tokens_exist(token, candidates):
    """``status-locked`` matches literal ``status-locked`` or prefix ``status-``."""
    return token in candidates or any(c and token.startswith(c) for c in candidates)


# Ids the shared dialog macros render from an argument instead of a literal
# id="..." attribute: `_partials/dialog.html` does `id="{{ dialog_id }}"` and is
# called as `pc_confirm_dialog('pc-confirm', ...)`. Keep only the names that
# really appear as a quoted literal in the corpus.
MACRO_IDS = {name for name in ('pc-confirm',)
             if f"'{name}'" in TEXT_CORPUS}

IDS = attr_candidates('id') | MACRO_IDS
CLASSES = attr_candidates('class') | RUNTIME_CLASSES
ATTR_NAMES = {name for text in TEMPLATES.values()
              for name, _ in ATTR.findall(text)}
TAGS = {t.lower() for text in TEMPLATES.values()
        for t in re.findall(r'<([a-zA-Z][-\w]*)', text)}

CSS_TOKEN = re.compile(
    r'\#(?P<id>[-\w${}.]+)'
    r'|\.(?P<cls>[-\w${}.]+)'
    r'|\[(?P<attr>[^\]]*)\]'
    r'|(?:(?<=^)|(?<=[\s>+~]))(?P<tag>[a-zA-Z][-\w]*)')


def locator_problems(locator):
    """Reasons a Selenese locator cannot match the current templates ([] = fine)."""
    problems = []
    if locator.startswith('xpath='):
        # XPath: every quoted literal in the expression must be real UI text.
        expression = locator[len('xpath='):]
        for one, two in re.findall(r"'([^']*)'|\"([^\"]*)\"", expression):
            text = one or two
            if text and text not in TEXT_CORPUS:
                problems.append(f'xpath literal {text!r} not found in the '
                                f'templates/app.py')
        if not re.search(r"'[^']*'|\"[^\"]*\"", expression):
            problems.append(f'no checkable literal in xpath {expression!r}')
        return problems

    if not locator.startswith('css='):
        problems.append(f'locator {locator!r} must start with css= or xpath=')
        return problems
    selector = VARIABLE.sub('.*', locator[len('css='):])

    seen = False
    for match in CSS_TOKEN.finditer(selector):
        seen = True
        if match.group('id'):
            token = match.group('id')
            if not tokens_exist(token, IDS):
                problems.append(f'id {token!r} not found in any template')
        elif match.group('cls'):
            token = match.group('cls')
            if not tokens_exist(token, CLASSES):
                problems.append(f'class {token!r} not found in any template')
        elif match.group('attr') is not None:
            attr = match.group('attr').strip()
            name, _, value = attr.partition('=')
            name = name.strip().lstrip(':')
            if name and not value:
                # Bare flag (`:not([disabled])`); the JS builds it as
                # `' disabled'`, so match it as a word in the templates.
                if not re.search(rf'\b{re.escape(name)}\b', TEMPLATE_CORPUS):
                    problems.append(f'attribute {name!r} not found in any '
                                    f'template')
                continue
            if name and name not in ATTR_NAMES and not name.startswith('data-'):
                problems.append(f'attribute {name!r} not found in any template')
            value = value.strip().strip('"\'')
            if value and f'"{value}"' not in TEXT_CORPUS \
                    and f"'{value}'" not in TEXT_CORPUS:
                problems.append(f'attribute value {value!r} not found in any '
                                f'template')
        elif match.group('tag'):
            tag = match.group('tag').lower()
            if tag not in TAGS:
                problems.append(f'tag <{tag}> not found in any template')
    if not seen:
        problems.append(f'no checkable token in locator {locator!r}')
    return problems


def text_problems(value):
    """Reasons an expected string cannot match anything the app renders."""
    expected = value
    if expected.startswith('regex:') or expected.startswith('regexp:'):
        return []
    if expected.startswith('glob:'):
        expected = expected[len('glob:'):]
        fragments = [f.strip() for f in expected.split('*')]
        fragments = [f for f in fragments
                     if len(f) >= 2 and not VARIABLE.search(f)]
        missing = [f for f in fragments if f not in TEXT_CORPUS]
        if missing:
            return [f'expected text {m!r} not found in templates/app.py'
                    for m in missing]
        return []
    if VARIABLE.search(expected):
        return []
    return [] if expected in TEXT_CORPUS else [
        f'expected text {expected!r} not found in templates/app.py']


def seams(rows):
    """The original object names, in order, from the ``#`` seam comments."""
    return [target[3:-3] for command, target, _ in rows
            if command == '#' and target.startswith('== ') and target.endswith(' ==')]


class KatalonSuiteTest(unittest.TestCase):
    """Static validation of katalon/parental-controls.html."""

    def setUp(self):
        if not SUITE.is_file() or not CASES:
            self.skipTest('katalon/parental-controls.html missing or unparsable')

    # ---------------------------------------------------------------- shape
    def test_header_is_the_recorder_suite_header(self):
        self.assertTrue(SUITE_TEXT.startswith('<?xml version="1.0" '
                                              'encoding="UTF-8"?>\n'))
        self.assertIn(
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" '
            '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">',
            SUITE_TEXT)
        self.assertIn('<html xmlns="http://www.w3.org/1999/xhtml"', SUITE_TEXT)
        self.assertEqual(1, SUITE_TEXT.count('<title>'))

    def test_one_table_per_case_with_the_grid_attributes(self):
        self.assertEqual(REQUIRED_CASES, set(CASES))
        self.assertEqual(len(REQUIRED_CASES), len(_parser.tables))
        for attrs in TABLE_ATTRS:
            self.assertEqual('1', attrs.get('cellpadding'),
                             'Recorder tables carry cellpadding="1"')
            self.assertEqual('1', attrs.get('cellspacing'))
            self.assertEqual('1', attrs.get('border'))
        for name, rows in CASES.items():
            with self.subTest(case=name):
                self.assertGreater(len(rows), 30,
                                   f'{name} looks truncated')

    def test_total_step_count(self):
        self.assertGreater(TOTAL_STEPS, 400, 'suspiciously small suite')

    # ---------------------------------------------------------------- steps
    def test_every_command_is_selenese(self):
        for name, rows in CASES.items():
            for command, target, value in rows:
                with self.subTest(case=name, command=command):
                    self.assertIn(command, ALLOWED_COMMANDS,
                                  f'{name} uses an unsupported command '
                                  f'{command!r}')

    def test_no_step_expects_a_browser_alert(self):
        for name, rows in CASES.items():
            for command, target, value in rows:
                with self.subTest(case=name, command=command):
                    self.assertNotIn(command, ALERT_COMMANDS,
                                     f'{name} still expects a browser alert '
                                     f'({command}); destructive actions confirm '
                                     f'in the shared native <dialog> now')

    def test_if_blocks_are_balanced(self):
        for name, rows in CASES.items():
            depth = 0
            for index, (command, target, value) in enumerate(rows):
                with self.subTest(case=name, step=index, command=command):
                    if command in ('if', 'elseIf'):
                        if command == 'elseIf':
                            self.assertGreater(depth, 0,
                                               'elseIf outside an if')
                        depth += 1
                    elif command == 'else':
                        self.assertGreater(depth, 0, 'else outside an if')
                    elif command == 'endIf':
                        depth -= 1
                        self.assertGreaterEqual(depth, 0, 'endIf without if')
            self.assertEqual(0, depth, f'{name} has an unclosed if')

    def test_pauses_are_milliseconds(self):
        for name, rows in CASES.items():
            for command, target, value in rows:
                if command == 'pause':
                    with self.subTest(case=name):
                        self.assertRegex(target, r'^\d+$')
                        self.assertGreaterEqual(int(target), 100)

    def test_opens_come_from_the_base_url_variable(self):
        for name, rows in CASES.items():
            for command, target, value in rows:
                if command == 'open':
                    with self.subTest(case=name, url=target):
                        self.assertTrue(target.startswith('${BASE_URL}'),
                                        f'{name} hardcodes a host: {target}')

    def test_credentials_are_variables_or_placeholders(self):
        for name, rows in CASES.items():
            for command, target, value in rows:
                if command != 'type':
                    continue
                if target in ('css=#password', 'css=#current',
                              'css=#new', 'css=#confirm'):
                    with self.subTest(case=name, target=target):
                        self.assertTrue(value.startswith('${') or
                                        value.startswith('<'),
                                        f'{name} types a literal into {target}')

    def test_confirm_dialog_is_used_where_a_dialog_opens(self):
        for name, rows in CASES.items():
            triggers = [t for c, t, _ in rows
                        if c == 'click' and any(k in t for k in CONFIRM_TRIGGERS)]
            confirms = [t for c, t, _ in rows
                        if c == 'click' and t == CONFIRM_CLICK]
            with self.subTest(case=name):
                if triggers:
                    self.assertTrue(
                        confirms,
                        f'{name} clicks {triggers[0]} but never confirms in '
                        f'{CONFIRM_CLICK}')

    # ------------------------------------------------------------ selectors
    def test_every_locator_exists_in_templates(self):
        checked = 0
        for name, rows in CASES.items():
            for command, target, value in rows:
                if command not in LOCATOR_COMMANDS or not target:
                    continue
                if target.startswith('${'):
                    continue   # a whole target from a variable is not checkable
                checked += 1
                with self.subTest(case=name, target=target):
                    self.assertEqual([], locator_problems(target),
                                     f'locator {target!r} in {name}')
        self.assertGreater(checked, 60, 'suspiciously few locators checked')

    def test_every_expected_text_exists_in_the_ui(self):
        checked = 0
        for name, rows in CASES.items():
            for command, target, value in rows:
                if command != 'verifyText':
                    continue
                checked += 1
                with self.subTest(case=name, value=value):
                    self.assertEqual([], text_problems(value),
                                     f'expected text in {name}')
        self.assertGreater(checked, 25, 'suspiciously few texts checked')

    def test_budget_categories_match_budgets_module(self):
        source = read(BUDGETS_PY)
        match = re.search(r'^CATEGORIES\s*=\s*\((.*?)\)', source, re.S | re.M)
        self.assertIsNotNone(match, 'CATEGORIES not found in budgets.py')
        categories = set(re.findall(r"'([^']+)'", match.group(1)))
        for name, rows in CASES.items():
            for command, target, value in rows:
                for cat in re.findall(
                        r'#budget-(?:used-|left-|status-)?([a-z_]+)(?![\w-])',
                        target):
                    if cat.endswith('-') or '${' in cat:
                        continue
                    with self.subTest(case=name, category=cat):
                        self.assertIn(cat, categories,
                                      f'{name} checks unknown budget category '
                                      f'{cat!r}')

    # ------------------------------------------------------------- wiring
    def test_object_seams_are_complete(self):
        seen = set()
        for name, rows in CASES.items():
            found = seams(rows)
            with self.subTest(case=name):
                for seam in found:
                    self.assertIn(seam, ALL_OBJECTS,
                                  f'{name} names an unknown object {seam!r}')
                self.assertEqual('00_setup_reset_session', found[0],
                                 f'{name} must start with the reset/setup '
                                 f'object')
                self.assertEqual('05_logout', found[-1],
                                 f'{name} must end with the logout object')
                logins = [s for s in found if s in LOGIN_SEAMS]
                self.assertEqual(1, len(logins),
                                 f'{name} must log in exactly once (found '
                                 f'{logins})')
            seen.update(found)
        self.assertEqual(EXPECTED_USED_OBJECTS, seen,
                         'objects are either not flattened into a case or '
                         'missing from ALL_OBJECTS/UNREFERENCED_OK')

    def test_no_case_chains_state_changing_objects_by_default(self):
        """A smoke case must never lock/freeze/grant; each state change
        lives in its own dedicated case (and undoes itself there)."""
        allowed_anywhere = set().union(*STATE_CHANGING.values())
        for name, rows in CASES.items():
            for seam in seams(rows):
                if seam not in allowed_anywhere:
                    continue
                with self.subTest(case=name, object=seam):
                    self.assertIn(
                        seam, STATE_CHANGING.get(name, set()),
                        f'{name} runs {seam}, a state-changing object; that '
                        f'belongs in '
                        f'{sorted(k for k, v in STATE_CHANGING.items() if seam in v)}')

    # ------------------------------------------------------------- safety
    def test_no_real_accounts_or_paths(self):
        for account in FORBIDDEN_ACCOUNTS:
            with self.subTest(account=account):
                self.assertIsNone(
                    re.search(rf'\b{account}\b', SUITE_TEXT),
                    f'the suite mentions the protected account {account!r}')
        for needle in ('/var/home', '/home/', 'Documents/Projects'):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, SUITE_TEXT,
                                 f'the suite hardcodes a checkout path '
                                 f'({needle})')

    def test_every_required_variable_is_used(self):
        for variable in sorted(REQUIRED_VARIABLES):
            with self.subTest(variable=variable):
                self.assertIn(f'${{{variable}}}', SUITE_TEXT,
                              f'the suite must use ${{{variable}}}')

    def test_readme_documents_the_suite(self):
        self.assertTrue(README.is_file(), 'katalon/README.md is missing')
        # Collapse line wrapping: a prose phrase may straddle a newline.
        blob = ' '.join(read(README).split())
        for needle in (
                'chromewebstore.google.com',
                'addons.mozilla.org',
                'microsoftedge.microsoft.com',
                'Katalon account',
                'no longer actively maintained',
                'Profiles (Global Variables)',
                'Use this in a test case',
                'Open Test Suite',
                'drag and drop',
                '.krecorder',
                'parental-controls.html',
                'Katalon Studio',
                'Selenium WebDriver',
                'https://192.168.122.201:5000',
                'self-signed',
                'localhost:5000',
                '${BASE_URL}',
                '${ADMIN_PASSWORD}',
                '<ADMIN_PASSWORD>',
                'verify on the live box',
                'test_katalon_suite.py'):
            with self.subTest(needle=needle):
                self.assertIn(needle, blob,
                              f'katalon/README.md must mention {needle!r}')
        for name in REQUIRED_CASES:
            with self.subTest(case=name):
                self.assertIn(name, blob,
                              f'katalon/README.md must document {name}')
        # Stale UI that no longer exists must not be described.
        for stale in ('Object Repository', 'Variables table', 'Export back'):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, blob,
                                 f'katalon/README.md still describes {stale!r}')


if __name__ == '__main__':
    unittest.main()
