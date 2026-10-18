#!/usr/bin/env python3
"""Overview payload parity: what the 10s JS rebuild consumes vs what we send.

Bug class this guards: the `/` first paint is server-rendered from the rows
`index()` builds, while the 10-second refresh rebuilds `#accounts` from
`/api/users` through the page script's `accountHtml()`. When that payload
drifts from the fields the script actually reads — `wheel` was missing from
`api_users()` while `index()` had it — the rebuild silently renders wheel
admins as lockable children (no Protected pill, "child" label, live
Lock/Unlock forms).

These tests pin the contract, not one snapshot of it:

* parse `templates/index.html` and extract the `u.*` fields `accountHtml()`
  and `updateSummary()` read, then require every one of them as a key on
  every `/api/users` row (plus the `cd.*` keys of the `countdown` object);
* the `/` first paint and `/api/users` must AGREE per username on the wheel
  flag (server-vs-JS parity for the exact hazard above);
* the agent's `/api/v1/users` payload — which the Overview's remote-device
  poll feeds through the SAME `accountHtml()` — carries `wheel` too. Its
  missing `countdown` stays an ACCEPTED degradation (TODO in agent.py) and is
  listed explicitly as such here.

All system reads are mocked; nothing touches a real account, the service,
``/etc``, ``/opt`` or the checkout. Flask test client + raw WSGI calls only.
"""

import os
import re
import sys
import unittest
from contextlib import ExitStack
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import agent
import app
import auth
from test_lock_regression import _AdminSessionMixin
from test_multidevice_agent import BaseAgentTest, auth_header, call

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, 'templates')

USERS = [
    {'username': 'kidwheel', 'uid': 1001, 'home': '/home/kidwheel',
     'shell': '/bin/bash'},
    {'username': 'kidplain', 'uid': 1002, 'home': '/home/kidplain',
     'shell': '/bin/bash'},
]
LOCK = {'kidwheel': (True, 'Locked'), 'kidplain': (False, 'Unlocked')}
# kidplain carries a schedule so `countdown` is non-null on at least one row.
BEDTIME = {'kidplain': {'start': '21:30', 'end': '07:00',
                        'days': [0, 1, 2, 3, 4]}}

# The per-row fields accountHtml()/updateSummary() read today. The contract
# test extracts the live set from the script; this floor only detects an
# extraction that silently matched nothing (or a JS edit that dropped a
# field — drop it here deliberately, in review, if the script changes).
JS_CONSUMED_FLOOR = {'username', 'uid', 'locked', 'status_detail', 'bedtime',
                     'in_bedtime', 'countdown', 'wheel'}

# Keys the remote agent payload may legitimately omit. `countdown` is the
# documented degradation: accountHtml() renders no chip when the key is
# absent (see app.index() and the TODO(multidevice) note in agent.read_users).
# Anything else missing is drift and must fail.
AGENT_ACCEPTED_DEGRADATIONS = {'countdown'}


def read_index_script():
    with open(os.path.join(TEMPLATE_DIR, 'index.html'),
              encoding='utf-8') as fh:
        return fh.read()


def _slice(src, start_marker, end_marker, name):
    """The JS source of one page-script function, by its two anchors."""
    if start_marker not in src or end_marker not in src:
        raise AssertionError(
            'index.html script layout changed: %r anchors %r..%r not found — '
            're-pin this extractor against the renamed function'
            % (name, start_marker, end_marker))
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def js_fields_read_by_account_html():
    """Every `u.<field>` the per-card rebuild reads."""
    body = _slice(read_index_script(), 'function accountHtml(u)',
                  'function setStale', 'accountHtml')
    return set(re.findall(r'\bu\.([A-Za-z_]\w*)', body))


def js_fields_read_by_update_summary():
    """Every `u.<field>` the summary counters read."""
    body = _slice(read_index_script(), 'function updateSummary(users)',
                  'function confirmThenSubmit', 'updateSummary')
    return set(re.findall(r'\bu\.([A-Za-z_]\w*)', body))


def js_keys_read_by_countdown_html():
    """Every `cd.<key>` the countdown chip reads off the payload's `countdown`."""
    body = _slice(read_index_script(), 'function countdownHtml(cd)',
                  'function accountHtml', 'countdownHtml')
    return set(re.findall(r'\bcd\.([A-Za-z_]\w*)', body))


class _PanelStateMixin:
    """Deterministic local state for the Overview endpoints (all mocked)."""

    ADMIN_WHEEL = ('kidwheel',)

    def _fakes(self, wheel=ADMIN_WHEEL):
        return [
            mock.patch.object(app, 'get_human_users', return_value=USERS),
            mock.patch.object(app, 'load_bedtime', return_value=BEDTIME),
            mock.patch.object(app, 'get_lock_status',
                              side_effect=lambda u: LOCK[u]),
            mock.patch.object(app, 'is_in_bedtime', return_value=False),
            mock.patch.object(app, 'bonus_state',
                              return_value={'active': False}),
            mock.patch.object(auth, 'is_wheel_member',
                              side_effect=lambda u: (
                                  u == self.ADMIN_USER or u in wheel)),
        ]

    def _get_both(self, wheel=ADMIN_WHEEL):
        """Return (first-paint html, api rows) built from ONE fake state."""
        with ExitStack() as stack:
            for patcher in self._fakes(wheel):
                stack.enter_context(patcher)
            html = self.client.get('/').get_data(as_text=True)
            rows = self.client.get('/api/users').get_json()
        return html, {row['username']: row for row in rows}

    @staticmethod
    def _protected_in_first_paint(html, username):
        """True if the SERVER-rendered card shows the Protected pill.

        Scoped to the #accounts region: the page script legitimately contains
        the same class name as a template literal.
        """
        region = html[html.index('id="accounts"'):
                      html.index('class="refresh-info"')]
        idx = region.index('id="row-%s"' % username)
        card = region[idx:region.index('</article>', idx)]
        return 'badge-protected' in card


class OverviewPayloadContractTest(_PanelStateMixin, _AdminSessionMixin,
                                  unittest.TestCase):
    """/api/users rows carry every field the JS rebuild reads from them."""

    def test_extraction_finds_the_known_consumed_fields(self):
        # Guard against a vacuous pass: if the regexes find nothing (script
        # refactored), the subset check below would silently succeed.
        consumed = js_fields_read_by_account_html() | \
            js_fields_read_by_update_summary()
        self.assertTrue(JS_CONSUMED_FLOOR <= consumed,
                        'JS rebuild no longer reads %s (extracted %s) — if the '
                        'script genuinely dropped a field, update the floor '
                        'in this test with review' % (
                            sorted(JS_CONSUMED_FLOOR - consumed),
                            sorted(consumed)))

    def test_api_users_rows_carry_every_js_consumed_field(self):
        consumed = js_fields_read_by_account_html() | \
            js_fields_read_by_update_summary()
        _, by_user = self._get_both()
        self.assertEqual(sorted(row['username'] for row in by_user.values()),
                         sorted(u['username'] for u in USERS))
        for username, row in sorted(by_user.items()):
            missing = consumed - set(row)
            with self.subTest(row=username):
                self.assertEqual(set(), missing,
                                 '/api/users row %r is missing %s that '
                                 'accountHtml()/updateSummary() read — the '
                                 '10s rebuild would render this card wrong'
                                 % (username, sorted(missing)))

    def test_summary_counter_fields_are_present(self):
        # updateSummary() today counts locked/in_bedtime across all accounts
        # (it does NOT distinguish admins from children). Whatever it reads
        # per row must be on the payload or the counters drift on rebuild.
        summary_fields = js_fields_read_by_update_summary()
        _, by_user = self._get_both()
        for username, row in sorted(by_user.items()):
            with self.subTest(row=username):
                self.assertTrue(summary_fields <= set(row),
                                'updateSummary() reads %s but the row lacks it'
                                % sorted(summary_fields - set(row)))

    def test_countdown_object_carries_the_keys_the_chip_reads(self):
        cd_keys = js_keys_read_by_countdown_html()
        _, by_user = self._get_both()
        scheduled = by_user['kidplain']
        self.assertIsNotNone(scheduled['countdown'],
                             'kidplain has a bedtime start: countdown must '
                             'be an object for this test to mean anything')
        self.assertTrue(cd_keys <= set(scheduled['countdown']))
        # Nothing to count down to -> explicit null, not a partial object.
        self.assertIsNone(by_user['kidwheel']['countdown'])


class WheelFlagParityTest(_PanelStateMixin, _AdminSessionMixin,
                          unittest.TestCase):
    """The wheel flag: correct per row AND identical across first paint vs API."""

    def test_api_users_flags_wheel_membership(self):
        with ExitStack() as stack:
            for patcher in self._fakes(wheel=('kidwheel',)):
                stack.enter_context(patcher)
            resp = self.client.get('/api/users')
        self.assertEqual(200, resp.status_code)
        by_user = {row['username']: row for row in resp.get_json()}
        self.assertIs(True, by_user['kidwheel']['wheel'],
                      'a wheel member must not be flagged lockable — this is '
                      'exactly the field the 10s rebuild lost')
        self.assertIs(False, by_user['kidplain']['wheel'])

    def test_first_paint_and_api_agree_on_wheel_per_username(self):
        html, by_user = self._get_both(wheel=('kidwheel',))
        for username in sorted(by_user):
            with self.subTest(user=username):
                self.assertEqual(
                    self._protected_in_first_paint(html, username),
                    bool(by_user[username]['wheel']),
                    '%s renders Protected on the first paint but the API '
                    'row disagrees — accountHtml() will flip it every 10s'
                    % username)
        self.assertTrue(self._protected_in_first_paint(html, 'kidwheel'))
        self.assertFalse(self._protected_in_first_paint(html, 'kidplain'))

    def test_no_wheel_means_no_protected_either_way(self):
        # Symmetry: with no remote wheel members, both paints agree flat.
        html, by_user = self._get_both(wheel=())
        for username in sorted(by_user):
            with self.subTest(user=username):
                self.assertIs(False, by_user[username]['wheel'])
                self.assertFalse(
                    self._protected_in_first_paint(html, username))


class AgentUsersPayloadParityTest(BaseAgentTest):
    """The agent /users mirror: remote cards feed the SAME accountHtml()."""

    AGENT_USERS = [
        {'username': 'busywheel', 'uid': 1002, 'home': '/home/busywheel',
         'shell': '/bin/bash'},
        {'username': 'kidremote', 'uid': 1003, 'home': '/home/kidremote',
         'shell': '/bin/bash'},
    ]

    def _read_users(self, wheel=('busywheel',)):
        with mock.patch.object(agent, 'get_human_users',
                               return_value=self.AGENT_USERS), \
             mock.patch.object(agent, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(agent, 'load_bedtime', return_value={}), \
             mock.patch.object(agent, 'is_in_bedtime', return_value=False), \
             mock.patch.object(auth, 'is_wheel_member',
                               side_effect=lambda u: u in wheel):
            return call(self.app, '/api/v1/users',
                        headers=auth_header(self.TOKEN))

    def test_agent_users_rows_carry_wheel(self):
        res = self._read_users()
        self.assertEqual(200, res['code'])
        by_user = {row['username']: row for row in res['payload']}
        self.assertIs(True, by_user['busywheel']['wheel'],
                      'a remote wheel admin rendered as a lockable child is '
                      'the same hazard as the local /api/users drift')
        self.assertIs(False, by_user['kidremote']['wheel'])

    def test_agent_users_rows_cover_js_consumed_fields(self):
        # Same extractor as the panel contract; only the documented
        # degradations may be missing.
        consumed = js_fields_read_by_account_html() | \
            js_fields_read_by_update_summary()
        res = self._read_users()
        for row in res['payload']:
            missing = consumed - AGENT_ACCEPTED_DEGRADATIONS - set(row)
            with self.subTest(row=row['username']):
                self.assertEqual(
                    set(), missing,
                    'agent /api/v1/users row %r is missing %s the JS rebuild '
                    'reads (accepted degradations: %s)'
                    % (row['username'], sorted(missing),
                       sorted(AGENT_ACCEPTED_DEGRADATIONS)))


if __name__ == '__main__':
    unittest.main()
