#!/usr/bin/env python3
"""Overview payload parity: what the 10s JS rebuild consumes vs what we send.

Two invariants guard one bug class. The `/` first paint is server-rendered
from the rows `index()` builds, while the 10-second refresh rebuilds
`#accounts` from `/api/users` through the page script's `accountHtml()`.
When that payload drifts from the fields the script actually reads — `wheel`
was missing from `api_users()` while `index()` had it — the rebuild silently
rendered wheel admins as lockable children (no Protected pill, "child"
label, live Lock/Unlock forms).

The fix has two halves (owner decision, 2026-10-18):

* ADMIN ACCOUNTS ARE HIDDEN FROM THE OVERVIEW. An account is an admin when
  it is a live wheel member OR has an admin-store row (the exact predicate
  `_is_admin_account()` implements, shared by `index()` and `api_users()` —
  parity of the *exclusion set* is now the contract). The agent keeps
  REPORTING its admin accounts (never changed what a device says about the
  world); its `/users` rows now carry `wheel` AND `admin`, and the hub drops
  flagged rows consumer-side (server-side for the first paint, in
  refreshTable() for every rebuild). accountHtml()'s wheel branch stays as
  defence.
* the field contract stays pinned: parse `templates/index.html` and extract
  the `u.*` fields `accountHtml()` and `updateSummary()` read, then require
  every one of them as a key on every `/api/users` row (plus the `cd.*`
  keys of the `countdown` object); the `wheel` + `admin` filter flags are
  required too.

All system reads are mocked; nothing touches a real account, the service,
``/etc``, ``/opt`` or the checkout. Flask test client + raw WSGI calls only.
"""

import os
import re
import sys
import tempfile
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
    {'username': 'kidadmin', 'uid': 1002, 'home': '/home/kidadmin',
     'shell': '/bin/bash'},
    {'username': 'kidplain', 'uid': 1003, 'home': '/home/kidplain',
     'shell': '/bin/bash'},
    {'username': 'kidnosched', 'uid': 1004, 'home': '/home/kidnosched',
     'shell': '/bin/bash'},
]
USERS_NAMES = sorted(u['username'] for u in USERS)
LOCK = {'kidwheel': (True, 'Locked'), 'kidadmin': (False, 'Unlocked'),
        'kidplain': (False, 'Unlocked'), 'kidnosched': (False, 'Unlocked')}
# kidplain carries a schedule so `countdown` is non-null on at least one row.
BEDTIME = {'kidplain': {'start': '21:30', 'end': '07:00',
                        'days': [0, 1, 2, 3, 4]}}

# The per-row fields accountHtml()/updateSummary() read today. The contract
# test extracts the live set from the script; this floor only detects an
# extraction that silently matched nothing (or a JS edit that dropped a
# field — drop it here deliberately, in review, if the script changes).
#
# `uid` LEFT this set on 2026-10-18 (owner request): the Overview card no
# longer shows the UID — `accountHtml()` stopped reading `u.uid` when the
# meta line lost its "UID <n>" segment. The payload still carries the key
# (the per-user Details page needs it); it is simply no longer part of the
# JS rebuild contract. See ChildAccountHeadingAndMetaTest.
JS_CONSUMED_FLOOR = {'username', 'locked', 'status_detail', 'bedtime',
                     'in_bedtime', 'countdown', 'wheel'}

# Payload-contract fields beyond what the renderers read: the admin filter
# flags. `wheel` also feeds the JS consumer-side filter and accountHtml()'s
# defensive branch; `admin` feeds the filter for BOTH paints (local rows
# pre-filtered server-side, remote rows filtered in refreshTable() and by
# index() for the first paint).
ADMIN_FILTER_FIELDS = {'wheel', 'admin'}

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
    """Deterministic local state for the Overview endpoints (all mocked).

    The fake box has one wheel member (kidwheel), one app-only admin-store
    row (kidadmin — the store half of the predicate; a system account with a
    store row is listed by _admin_rows() exactly like the console shows it),
    one plain child with a schedule (kidplain) and one child without
    (kidnosched). ADMIN_WHEEL + STORE_ROWS are the exclusion sources;
    SURVIVORS is what both Overview paths must render.
    """

    ADMIN_WHEEL = ('kidwheel',)
    STORE_ROWS = ('kidadmin',)

    def _fakes(self, wheel=ADMIN_WHEEL, store_rows=STORE_ROWS):
        # The fake store must keep the logged-in harness admin (qaadmin):
        # require_admin() re-classifies every request against load_store().
        users = {self.ADMIN_USER: {'hash': 'x', 'must_change': False,
                                   'locked': False}}
        for name in store_rows:
            users.setdefault(name, {'hash': 'x', 'must_change': False,
                                    'locked': False})
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
            mock.patch.object(auth, 'load_store', return_value={'users': users}),
        ]

    def _get_both(self, wheel=ADMIN_WHEEL, store_rows=STORE_ROWS):
        """Return (first-paint html, api rows) built from ONE fake state."""
        with ExitStack() as stack:
            for patcher in self._fakes(wheel, store_rows):
                stack.enter_context(patcher)
            html = self.client.get('/').get_data(as_text=True)
            rows = self.client.get('/api/users').get_json()
        return html, {row['username']: row for row in rows}

    def _survivors(self, wheel=ADMIN_WHEEL, store_rows=STORE_ROWS):
        return {u['username'] for u in USERS} - set(wheel) - set(store_rows)

    @staticmethod
    def _card_in_first_paint(html, username):
        """True when the SERVER-rendered #accounts region has this card."""
        region = html[html.index('id="accounts"'):
                      html.index('class="refresh-info"')]
        return ('id="row-%s"' % username) in region


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
        # Only children survive the admin filter — exactly the SURVIVORS set.
        self.assertEqual(sorted(by_user), sorted(self._survivors()))
        for username, row in sorted(by_user.items()):
            missing = (consumed | ADMIN_FILTER_FIELDS) - set(row)
            with self.subTest(row=username):
                self.assertEqual(set(), missing,
                                 '/api/users row %r is missing %s that '
                                 'accountHtml()/updateSummary()/the admin '
                                 'filter read — the 10s rebuild would render '
                                 'this card wrong' % (username,
                                                      sorted(missing)))

    def test_summary_counter_fields_are_present(self):
        # updateSummary() counts locked/in_bedtime across the rows it gets —
        # after the 2026-10-18 filter those are children only, and the
        # payload field set it reads must not drift.
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
        self.assertIsNone(by_user['kidnosched']['countdown'])


class AdminExclusionParityTest(_PanelStateMixin, _AdminSessionMixin,
                               unittest.TestCase):
    """Admins (wheel + store rows) are hidden from BOTH Overview paints.

    The old bug class was `wheel` drifting between index() and api_users();
    the 2026-10-18 invariant subsumes it: the same predicate
    (app._is_admin_account) must drop the same usernames from the first
    paint and the 10s rebuild, and surviving rows keep the fields the
    renderers read.
    """

    def test_wheel_member_excluded_from_cards_and_api(self):
        html, by_user = self._get_both(wheel=('kidwheel',), store_rows=())
        self.assertFalse(self._card_in_first_paint(html, 'kidwheel'),
                         'a wheel admin rendered on the Overview — the '
                         'exactly-hidden account the owner asked to remove')
        self.assertNotIn('kidwheel', by_user)
        self.assertTrue(self._card_in_first_paint(html, 'kidplain'))

    def test_app_only_store_row_excluded_from_cards_and_api(self):
        html, by_user = self._get_both(wheel=(), store_rows=('kidadmin',))
        self.assertFalse(self._card_in_first_paint(html, 'kidadmin'),
                         'an admin-store row rendered on the Overview')
        self.assertNotIn('kidadmin', by_user)
        # ...and the OTHER users stay: exclusion is per-account, not blanket.
        self.assertTrue(self._card_in_first_paint(html, 'kidwheel'))
        self.assertTrue(self._card_in_first_paint(html, 'kidplain'))

    def test_both_sources_excluded_together(self):
        html, by_user = self._get_both()  # wheel + store, both active
        for name in ('kidwheel', 'kidadmin'):
            with self.subTest(user=name):
                self.assertFalse(self._card_in_first_paint(html, name))
                self.assertNotIn(name, by_user)
        self.assertEqual(sorted(by_user), sorted(self._survivors()))

    def test_first_paint_and_api_exclude_the_same_usernames(self):
        # The parity claim: one predicate, two paths — a username missing
        # from one paint but present on the other is the drift that started
        # this whole guard.
        for wheel, store_rows in ((('kidwheel',), ()), ((), ('kidadmin',)),
                                  (self.ADMIN_WHEEL, self.STORE_ROWS)):
            with self.subTest(wheel=wheel, store=store_rows):
                html, by_user = self._get_both(wheel, store_rows)
                painted = {u['username'] for u in USERS
                           if self._card_in_first_paint(html, u['username'])}
                self.assertEqual(painted, set(by_user),
                                 'index() and /api/users disagree about who '
                                 'is on the Overview — the 10s rebuild would '
                                 're-appear an excluded admin (or vanish a '
                                 'child)')

    def test_no_admin_state_keeps_every_row_unflagged(self):
        # Symmetry: with no wheel members and no store rows, all users paint
        # on both paths and both filter flags are False.
        html, by_user = self._get_both(wheel=(), store_rows=())
        self.assertEqual(sorted(by_user), sorted(USERS_NAMES))
        for username, row in sorted(by_user.items()):
            with self.subTest(user=username):
                self.assertIs(False, row['wheel'])
                self.assertIs(False, row['admin'])
                self.assertTrue(self._card_in_first_paint(html, username))
        # accountHtml()'s defensive wheel branch never fires: no Protected
        # pill anywhere in the server-rendered cards region.
        region = html[html.index('id="accounts"'):
                      html.index('class="refresh-info"')]
        self.assertNotIn('badge-protected', region)

    def test_server_rendered_summary_counts_children_only(self):
        # Item A.2: the first paint's KPI strip is built from the same
        # filtered `users` list, so it must show the survivor count.
        html, by_user = self._get_both()
        self.assertIn('id="summary-total">%d<' % len(by_user), html)
        self.assertIn('%d human accounts' % len(by_user), html)
        locked = sum(1 for r in by_user.values() if r['locked'] is True)
        self.assertIn('id="summary-locked">%d<' % locked, html)

    def test_js_rebuild_filters_admin_rows_consumer_side(self):
        # The 10s rebuild must apply the same predicate to remote rows
        # BEFORE rendering/summarising (local rows arrive pre-filtered; the
        # device-proxy payload does not).
        with open(os.path.join(TEMPLATE_DIR, 'index.html'),
                  encoding='utf-8') as fh:
            src = fh.read()
        fn = src.index('async function refreshTable')
        guard = src.index('u.wheel === true || u.admin === true', fn)
        rebuild = src.index('list.innerHTML = users.map(accountHtml)', fn)
        summary = src.index('updateSummary(users)', fn)
        self.assertLess(guard, rebuild,
                        'refreshTable() must drop admin rows before '
                        'rebuilding the cards')
        self.assertLess(guard, summary,
                        'the summary counters must count children only too')

    def test_hub_remote_first_paint_drops_admin_rows(self):
        # index()?device=… applies the same consumer-side filter to the
        # agent payload: admin rows (wheel===true or admin===true) never
        # render; children — and rows from an old agent without the keys —
        # always do.
        entry = {'name': 'boxb', 'url': 'http://localhost:5001'}
        remote_users = [
            {'username': 'remotekid', 'uid': 1002, 'locked': False,
             'status_detail': 'Unlocked', 'bedtime': {}, 'in_bedtime': False,
             'bonus': {'active': False}, 'wheel': False, 'admin': False},
            {'username': 'remwheel', 'uid': 1003, 'locked': False,
             'status_detail': 'Unlocked', 'bedtime': {}, 'in_bedtime': False,
             'bonus': {'active': False}, 'wheel': True, 'admin': True},
            {'username': 'remadmin', 'uid': 1004, 'locked': False,
             'status_detail': 'Unlocked', 'bedtime': {}, 'in_bedtime': False,
             'bonus': {'active': False}, 'wheel': False, 'admin': True},
            {'username': 'legacykid', 'uid': 1005, 'locked': False,
             'status_detail': 'Unlocked', 'bedtime': {}, 'in_bedtime': False,
             'bonus': {'active': False}},
        ]
        with mock.patch.object(app.remote, 'get_device',
                               return_value=entry), \
             mock.patch.object(app, '_remote_users',
                               return_value=(remote_users, None)), \
             mock.patch.object(app, 'get_human_users', return_value=[]), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             mock.patch.object(auth, 'load_store', return_value={
                 'users': {self.ADMIN_USER: {'hash': 'x', 'must_change':
                                             False, 'locked': False}}}):
            html = self.client.get('/?device=boxb').get_data(as_text=True)
        self.assertTrue(self._card_in_first_paint(html, 'remotekid'))
        self.assertTrue(self._card_in_first_paint(html, 'legacykid'))
        self.assertFalse(self._card_in_first_paint(html, 'remwheel'))
        self.assertFalse(self._card_in_first_paint(html, 'remadmin'))
        self.assertIn('id="summary-total">2<', html)


class AdminConsoleKeepsAdminsTest(_PanelStateMixin, _AdminSessionMixin,
                                  unittest.TestCase):
    """Hiding admins from the Overview must not hide them from the console."""

    def setUp(self):
        super().setUp()
        self._audit_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._audit_tmp.cleanup)
        os.environ['PARENTAL_CONTROLS_AUDIT'] = os.path.join(
            self._audit_tmp.name, 'audit.jsonl')
        self.addCleanup(lambda: os.environ.pop('PARENTAL_CONTROLS_AUDIT',
                                               None))

    def test_admin_console_lists_every_admin_row_with_details(self):
        # The console (built from the store) shows BOTH admins the Overview
        # just excluded: kidwheel (wheel member WITH a store row — on a real
        # box wheel admins get their row at bootstrap) and kidadmin
        # (app-only). System-backed rows keep the Details affordance to the
        # per-user page (spec A.3).
        with ExitStack() as stack:
            for patcher in self._fakes(wheel=('kidwheel',),
                                       store_rows=('kidwheel', 'kidadmin')):
                stack.enter_context(patcher)
            # kidwheel is a real system account (wheel), kidadmin is store-
            # only in this fixture: Details must follow system_user, not
            # tier, or it would link to a 404.
            stack.enter_context(mock.patch.object(
                auth, 'is_system_user',
                side_effect=lambda u: u in (self.ADMIN_USER, 'kidwheel')))
            html = self.client.get('/admin/audit').get_data(as_text=True)
        for name in ('kidwheel', 'kidadmin', self.ADMIN_USER):
            with self.subTest(admin=name):
                self.assertIn('data-username="%s"' % name, html)
        idx = html.index('data-username="kidwheel"')
        wheel_row = html[idx:html.index('</tr>', idx)]
        self.assertIn('badge-protected', wheel_row)
        self.assertIn('href="/user/kidwheel"', wheel_row)
        # App-only admins have no system account: no 404-link is offered.
        idx = html.index('data-username="kidadmin"')
        apponly_row = html[idx:html.index('</tr>', idx)]
        self.assertNotIn('href="/user/kidadmin"', apponly_row)


class AgentUsersPayloadParityTest(BaseAgentTest):
    """The agent /users mirror: remote cards feed the SAME accountHtml().

    The agent REPORTS everything (it never decides what the hub displays):
    admin rows stay in the payload, flagged `wheel`/`admin` so the hub can
    filter them consumer-side.
    """

    AGENT_USERS = [
        {'username': 'busywheel', 'uid': 1002, 'home': '/home/busywheel',
         'shell': '/bin/bash'},
        {'username': 'boxadmin', 'uid': 1003, 'home': '/home/boxadmin',
         'shell': '/bin/bash'},
        {'username': 'kidremote', 'uid': 1004, 'home': '/home/kidremote',
         'shell': '/bin/bash'},
    ]

    def _read_users(self, wheel=('busywheel',), store_rows=('boxadmin',)):
        store = {'users': dict((name, {'hash': 'x', 'must_change': False,
                                       'locked': False})
                               for name in store_rows)}
        with mock.patch.object(agent, 'get_human_users',
                               return_value=self.AGENT_USERS), \
             mock.patch.object(agent, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(agent, 'load_bedtime', return_value={}), \
             mock.patch.object(agent, 'is_in_bedtime', return_value=False), \
             mock.patch.object(auth, 'is_wheel_member',
                               side_effect=lambda u: u in wheel), \
             mock.patch.object(auth, 'load_store', return_value=store):
            return call(self.app, '/api/v1/users',
                        headers=auth_header(self.TOKEN))

    def test_agent_users_rows_carry_wheel_and_admin(self):
        res = self._read_users()
        self.assertEqual(200, res['code'])
        by_user = {row['username']: row for row in res['payload']}
        self.assertIs(True, by_user['busywheel']['wheel'],
                      'a remote wheel admin rendered as a lockable child is '
                      'the same hazard as the local /api/users drift')
        self.assertIs(True, by_user['busywheel']['admin'],
                      'admin must cover wheel too — the hub filter reads it')
        # App-only admin (store member, not wheel): flagged, but reported.
        self.assertIs(False, by_user['boxadmin']['wheel'])
        self.assertIs(True, by_user['boxadmin']['admin'])
        # Children are never flagged.
        self.assertIs(False, by_user['kidremote']['wheel'])
        self.assertIs(False, by_user['kidremote']['admin'])

    def test_agent_never_withholds_rows_from_the_hub(self):
        # The consumer filters; the producer stays complete: admin rows
        # remain reported (this box's agent list is not the hub's board).
        res = self._read_users()
        self.assertEqual(sorted(u['username'] for u in self.AGENT_USERS),
                         sorted(row['username'] for row in res['payload']))

    def test_agent_users_rows_cover_js_consumed_fields(self):
        # Same extractor as the panel contract; only the documented
        # degradations may be missing. The filter fields are contract keys.
        consumed = js_fields_read_by_account_html() | \
            js_fields_read_by_update_summary() | ADMIN_FILTER_FIELDS
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
