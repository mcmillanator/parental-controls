"""Static smoke validator for the Katalon Recorder QA suite in ``katalon/``.

The browser suite is the parent's hands-on regression check; this module is the
cheap CI guard that keeps the JSON honest without a browser, a running panel,
or any third-party dependency:

* every file in ``katalon/objects/`` and ``katalon/test-cases/`` is valid JSON
  with the shape the Recorder exports (``name`` / ``steps`` / ``uuid``),
* every step is a documented Recorder step with its required fields,
* **every selector and every expected string still exists in the current
  templates** — that is what makes the suite self-updating: rename an id in
  ``templates/user.html`` and this fails loudly instead of the parent's run
  failing at 11pm on the desktop,
* every test case references objects that exist, and every object is used,
* nothing here hardcodes a real account, password or checkout path.

Stdlib only (``json`` + ``re`` + ``unittest``), no Flask import, no network,
no Katalon install.
"""

import json
import re
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KATALON_DIR = ROOT / 'katalon'
OBJECT_DIR = KATALON_DIR / 'objects'
CASE_DIR = KATALON_DIR / 'test-cases'
README = KATALON_DIR / 'README.md'
TEMPLATE_DIR = ROOT / 'templates'
APP_PY = ROOT / 'app.py'
BUDGETS_PY = ROOT / 'budgets.py'

# Recorder step types this suite is allowed to use, and the fields each one
# must carry. Mirrors the documented Katalon Recorder step vocabulary.
STEP_FIELDS = {
    'navigate': ('url',),
    'goToUrl': ('url',),
    'click': ('selector',),
    'input': ('selector', 'value'),
    'verifyText': ('selector', 'value'),
    'verifyElementVisible': ('selector',),
    'waitFor': ('value',),
    'closeAlert': (),
    'if': ('condition', 'children'),
    'abortTest': ('message',),
    'deleteCookies': (),
    'setContext': ('key', 'value'),
    'getContext': ('key',),
    'dragAndDrop': ('selector', 'target'),
    'objectReference': ('name', 'uuid'),
}

# Objects that are deliberately not wired into a test case.
UNREFERENCED_OK = {
    '01b_set_admin_credentials',   # opt-in helper, documented in the README
    '90_example_drag_and_drop',    # recording template for a future UI
}

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

# Accounts that must never be named anywhere in the QA suite (hard rule).
FORBIDDEN_ACCOUNTS = ('tester', 'bluefox', 'pcagent', 'stan', 'root')

JINJA = re.compile(r'\{\{.*?\}\}|\{%.*?%\}', re.S)
VARIABLE = re.compile(r'\$\{[^}]*\}')
ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


def read(path):
    return path.read_text(encoding='utf-8')


def load_dir(path):
    return {p.stem: json.loads(read(p)) for p in sorted(path.glob('*.json'))}


TEMPLATES = {p.name: read(p) for p in sorted(TEMPLATE_DIR.glob('*.html'))}
TEMPLATE_CORPUS = '\n'.join(TEMPLATES.values())
# Server-rendered strings (login/change-password errors, status details) live
# in app.py, so expected-text checks look there too.
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


CSS_TOKEN = re.compile(
    r'\#(?P<id>[-\w${}.]+)'
    r'|\.(?P<cls>[-\w${}.]+)'
    r'|\[(?P<attr>[^\]]*)\]'
    r'|(?:(?<=^)|(?<=[\s>+~]))(?P<tag>[a-zA-Z][-\w]*)')

IDS = attr_candidates('id')
CLASSES = attr_candidates('class')
ATTR_NAMES = {name for text in TEMPLATES.values()
              for name, _ in ATTR.findall(text)}
TAGS = {t.lower() for text in TEMPLATES.values()
        for t in re.findall(r'<([a-zA-Z][-\w]*)', text)}


def selector_problems(selector):
    """Reasons a CSS selector cannot match the current templates ([] = fine)."""
    problems = []
    selector = VARIABLE.sub('.*', selector)
    if selector.startswith('xpath='):
        # XPath: every quoted literal in the expression must be real UI text.
        literals = re.findall(r"'([^']*)'|\"([^\"]*)\"", selector)
        for one, two in literals:
            text = one or two
            if text and text not in TEXT_CORPUS:
                problems.append(f'xpath literal {text!r} not found in the '
                                f'templates/app.py')
        return problems

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
                # Bare flag (:not([disabled])); the JS builds it as
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
        problems.append(f'no checkable token in selector {selector!r}')
    return problems


def text_problems(value):
    """Reasons an expected string cannot match anything the app renders."""
    expected = value
    if expected.startswith('regex:'):
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


def walk(steps):
    """Yield every step, descending into if/children/elseSteps."""
    for step in steps:
        yield step
        if step.get('type') == 'if':
            yield from walk(step.get('children') or [])
            yield from walk(step.get('elseSteps') or [])


class KatalonSuiteTest(unittest.TestCase):
    """Static validation of katalon/objects + katalon/test-cases."""

    @classmethod
    def setUpClass(cls):
        if not OBJECT_DIR.is_dir() or not CASE_DIR.is_dir():
            raise unittest.SkipTest('katalon/ objects or test-cases missing')
        cls.objects = load_dir(OBJECT_DIR)
        cls.cases = load_dir(CASE_DIR)

    # ---------------------------------------------------------------- files
    def test_every_file_is_valid_json(self):
        self.assertTrue(self.objects, 'no objects found in katalon/objects/')
        self.assertTrue(self.cases, 'no cases found in katalon/test-cases/')

    def test_names_match_filenames(self):
        for kind, collection in (('object', self.objects), ('case', self.cases)):
            numbers = {}
            for name, data in collection.items():
                where = data.get('name', '<no name>')
                with self.subTest(name=where):
                    self.assertRegex(where, r'^[a-z0-9][a-z0-9_]*$',
                                     f'{where!r} must be a lower snake_case '
                                     f'name')
                    self.assertEqual(where, name)
                    number = re.match(r'^(\d+[a-z]?)', where)
                    self.assertIsNotNone(number, f'{where!r} needs a number '
                                                 f'prefix so the Recorder list '
                                                 f'sorts in run order')
                    self.assertNotIn(number.group(1), numbers,
                                     f'{where!r} reuses number '
                                     f'{number.group(1)} of {numbers.get(number.group(1))}')
                    numbers[number.group(1)] = where

    def test_uuids_are_unique_and_valid(self):
        seen = {}
        for kind, collection in (('object', self.objects),
                                 ('case', self.cases)):
            for name, data in collection.items():
                value = data.get('uuid', '')
                with self.subTest(name=name):
                    self.assertIsInstance(value, str)
                    uuid.UUID(value)  # raises if malformed
                    self.assertNotIn(value, seen,
                                     f'uuid {value} already used by '
                                     f'{seen.get(value)}')
                    seen[value] = f'{kind}:{name}'

    def test_top_level_shape(self):
        for kind, collection in (('object', self.objects), ('case', self.cases)):
            for name, data in collection.items():
                with self.subTest(name=name):
                    self.assertEqual({'name', 'steps', 'uuid'}, set(data),
                                     f'{kind} {name} must have exactly '
                                     f'name/steps/uuid')
                    self.assertEqual(name, data['name'])
                    self.assertIsInstance(data['steps'], list)
                    self.assertTrue(data['steps'],
                                    f'{kind} {name} has no steps')

    # ---------------------------------------------------------------- steps
    def test_step_shapes(self):
        for name, data in list(self.objects.items()) + list(self.cases.items()):
            for step in walk(data['steps']):
                with self.subTest(object=name, type=step.get('type')):
                    stype = step.get('type')
                    self.assertIn(stype, STEP_FIELDS,
                                  f'unknown step type {stype!r} in {name}')
                    for field in STEP_FIELDS[stype]:
                        self.assertIn(field, step,
                                      f'{stype} step in {name} needs '
                                      f'{field!r}')

    def test_element_steps_carry_parent_selectors(self):
        for name, data in list(self.objects.items()) + list(self.cases.items()):
            for step in walk(data['steps']):
                if step.get('type') in ('click', 'input', 'verifyText',
                                        'verifyElementVisible'):
                    with self.subTest(object=name, selector=step['selector']):
                        self.assertEqual([], step.get('parentSelectors'),
                                         'Recorder records a parentSelectors '
                                         'list (empty when not nested)')

    def test_waits_are_millisecond_numbers(self):
        for name, data in list(self.objects.items()) + list(self.cases.items()):
            for step in walk(data['steps']):
                if step.get('type') == 'waitFor':
                    with self.subTest(object=name):
                        self.assertRegex(step['value'], r'^\d+$')

    def test_urls_come_from_the_base_url_variable(self):
        for name, data in list(self.objects.items()) + list(self.cases.items()):
            for step in walk(data['steps']):
                if step.get('type') in ('navigate', 'goToUrl'):
                    with self.subTest(object=name, url=step['url']):
                        self.assertTrue(step['url'].startswith('${BASE_URL}'),
                                        f'{name} hardcodes a host: '
                                        f'{step["url"]}')

    def test_credentials_are_variables_or_placeholders(self):
        for name, data in list(self.objects.items()) + list(self.cases.items()):
            for step in walk(data['steps']):
                if step.get('type') != 'input':
                    continue
                if step['selector'] in ('#password', '#current', '#new',
                                        '#confirm'):
                    with self.subTest(object=name, selector=step['selector']):
                        value = step['value']
                        self.assertTrue(
                            value.startswith('${') or
                            value.startswith('<'),
                            f'{name} types a literal into {step["selector"]}')

    # ------------------------------------------------------------ selectors
    def test_every_selector_exists_in_templates(self):
        checked = 0
        for name, data in list(self.objects.items()) + list(self.cases.items()):
            for step in walk(data['steps']):
                stype = step.get('type')
                if stype not in ('click', 'input', 'verifyText',
                                 'verifyElementVisible', 'dragAndDrop'):
                    continue
                for selector in (step.get('selector'),
                                 step.get('target')):
                    if not selector:
                        continue
                    checked += 1
                    with self.subTest(object=name, selector=selector):
                        self.assertEqual([], selector_problems(selector),
                                         f'selector {selector!r} in {name}')
        self.assertGreater(checked, 60, 'suspiciously few selectors checked')

    def test_every_expected_text_exists_in_the_ui(self):
        checked = 0
        for name, data in list(self.objects.items()) + list(self.cases.items()):
            for step in walk(data['steps']):
                if step.get('type') != 'verifyText':
                    continue
                checked += 1
                with self.subTest(object=name, value=step['value']):
                    self.assertEqual([], text_problems(step['value']),
                                     f'expected text in {name}')
        self.assertGreater(checked, 25, 'suspiciously few texts checked')

    def test_budget_categories_match_budgets_module(self):
        source = read(BUDGETS_PY)
        match = re.search(r'^CATEGORIES\s*=\s*\((.*?)\)', source, re.S | re.M)
        self.assertIsNotNone(match, 'CATEGORIES not found in budgets.py')
        categories = set(re.findall(r"'([^']+)'", match.group(1)))
        for name, data in self.objects.items():
            for step in walk(data['steps']):
                selector = step.get('selector', '') or ''
                for cat in re.findall(
                        r'#budget-(?:used-|left-|status-)?([a-z_]+)(?![\w-])',
                        selector):
                    if cat.endswith('-') or '${' in cat:
                        continue
                    with self.subTest(object=name, category=cat):
                        self.assertIn(cat, categories,
                                      f'{name} checks unknown budget '
                                      f'category {cat!r}')

    # ------------------------------------------------------------- wiring
    def test_cases_reference_existing_objects(self):
        for name, data in self.cases.items():
            for step in walk(data['steps']):
                if step.get('type') != 'objectReference':
                    continue
                with self.subTest(case=name, ref=step['name']):
                    self.assertTrue(
                        step['name'] in self.objects,
                        f'{name} calls a missing object {step["name"]!r}')
                    self.assertEqual(
                        self.objects[step['name']]['uuid'], step['uuid'],
                        f'{name} calls {step["name"]} with a stale uuid')

    def test_every_object_is_used(self):
        used = set()
        for data in self.cases.values():
            for step in walk(data['steps']):
                if step.get('type') == 'objectReference':
                    used.add(step['name'])
        self.assertEqual(set(self.objects) - used, UNREFERENCED_OK,
                         'objects are either unreferenced (add them to a test '
                         'case) or missing from UNREFERENCED_OK')

    def test_cases_are_self_contained(self):
        for name, data in self.cases.items():
            types = [s.get('type') for s in data['steps']]
            with self.subTest(case=name):
                self.assertEqual('objectReference', types[0])
                self.assertEqual('00_setup_reset_session',
                                 data['steps'][0]['name'],
                                 f'{name} must start with the reset/setup '
                                 f'object')
                self.assertEqual('objectReference', types[-1])
                self.assertEqual('05_logout', data['steps'][-1]['name'],
                                 f'{name} must end with the logout object')
                logins = [s['name'] for s in data['steps']
                          if s.get('type') == 'objectReference'
                          and s['name'] in ('04_login_good_password',
                                            '04b_login_tolerate_must_change')]
                self.assertEqual(1, len(logins),
                                 f'{name} must log in exactly once (found '
                                 f'{logins})')

    def test_required_cases_present(self):
        self.assertEqual(REQUIRED_CASES, set(self.cases))

    def test_no_case_chains_state_changing_objects_by_default(self):
        """A smoke case must never lock/freeze/grant; each state change
        lives in its own dedicated case (and undoes itself there)."""
        allowed_anywhere = set().union(*STATE_CHANGING.values())
        for name, data in self.cases.items():
            for step in walk(data['steps']):
                if step.get('type') != 'objectReference':
                    continue
                ref = step['name']
                with self.subTest(case=name, ref=ref):
                    if ref not in allowed_anywhere:
                        continue
                    self.assertIn(
                        ref, STATE_CHANGING.get(name, set()),
                        f'{name} runs {ref}, a state-changing object; that '
                        f'belongs in {sorted(k for k, v in STATE_CHANGING.items() if ref in v)}')

    # ------------------------------------------------------------- safety
    def test_no_real_accounts_or_paths(self):
        for path in sorted(OBJECT_DIR.glob('*.json')) + \
                sorted(CASE_DIR.glob('*.json')):
            blob = read(path)
            with self.subTest(file=path.name):
                for account in FORBIDDEN_ACCOUNTS:
                    self.assertIsNone(
                        re.search(rf'\b{account}\b', blob),
                        f'{path.name} mentions the protected account '
                        f'{account!r}')
                for needle in ('/var/home', '/home/', 'Documents/Projects'):
                    self.assertFalse(needle in blob,
                                     f'{path.name} hardcodes a checkout path '
                                     f'({needle})')

    def test_placeholders_are_present(self):
        blob = '\n'.join(read(p) for p in sorted(OBJECT_DIR.glob('*.json')))
        for placeholder in ('<ADMIN_USER>', '<ADMIN_PASSWORD>',
                             '<TEST_USER>', '<WRONG_PASSWORD>'):
            self.assertIn(placeholder, blob,
                          f'expected the {placeholder} placeholder in the '
                          f'objects')
        self.assertIn('${ADMIN_USER}', blob)
        self.assertIn('${ADMIN_PASSWORD}', blob)

    def test_readme_documents_the_suite(self):
        self.assertTrue(README.is_file(), 'katalon/README.md is missing')
        # Collapse line wrapping: a prose phrase may straddle a newline.
        blob = ' '.join(read(README).split())
        for needle in ('chromewebstore.google.com',
                       'addons.mozilla.org',
                       'microsoftedge.microsoft.com',
                       'Object Repository', 'Import', 'Export',
                       'localhost:5000', '${BASE_URL}', '<ADMIN_PASSWORD>'):
            with self.subTest(needle=needle):
                self.assertTrue(needle in blob,
                                f'katalon/README.md must mention {needle!r}')
        for name in REQUIRED_CASES:
            with self.subTest(case=name):
                self.assertTrue(name in blob,
                                f'katalon/README.md must document {name}')


if __name__ == '__main__':
    unittest.main()