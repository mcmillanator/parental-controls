#!/usr/bin/env python3
"""The Overview's "Child Accounts" section: heading copy and card meta.

Owner request (2026-10-18): "The Accounts section on the Overview page
should be renamed Child Accounts", and the per-account UID line is admin
plumbing a parent never reads, so it comes off the Overview cards. The UID
STAYS on the per-user Details page (`UID: … · Shell: … · Home: …`) — the
owner asked for it there explicitly.

What this pins:
  * the heading reads "Child Accounts" and the old "Accounts" copy is gone,
    with the pinned `#accounts` / `.pc-accounts` / `.pc-accounts__heading`
    selectors untouched (UI_UX_PLAN.md §7.4);
  * the rendered Overview contains no "UID" at all, and the rebuild script
    no longer reads `u.uid`;
  * the rest of the card meta (the Bedtime range + days) still renders,
    identically in the first paint and the 10s rebuild;
  * the payload keeps its `uid` key — only the Overview stopped using it;
  * the Details page still shows UID/Shell/Home.

Everything is mocked: Flask test client + template rendering only. No real
account, no service, no /etc, /run, /opt or checkout path is read.
"""

import os
import sys
import unittest
from contextlib import ExitStack
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import app
import auth
from test_lock_regression import _AdminSessionMixin
from test_ui_overview_parity import js_fields_read_by_account_html

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'templates', 'index.html')

KID = 'metakid'
SCHEDULED = 'metakid2'
LOCK = {KID: (False, 'Unlocked'), SCHEDULED: (True, 'Locked')}
USERS = [
    {'username': KID, 'uid': 1001, 'home': '/home/' + KID, 'shell': '/bin/bash'},
    {'username': SCHEDULED, 'uid': 1002, 'home': '/home/' + SCHEDULED,
     'shell': '/bin/bash'},
]
BEDTIME = {SCHEDULED: {'start': '21:30', 'end': '07:00',
                       'days': [0, 1, 2, 3, 4]}}


def read_template():
    with open(TEMPLATE, encoding='utf-8') as fh:
        return fh.read()


def accounts_region(html):
    """The server-rendered #accounts card list (heading excluded)."""
    return html[html.index('id="accounts"'):html.index('class="refresh-info"')]


class ChildAccountsHeadingTest(_AdminSessionMixin, unittest.TestCase):
    def _render(self):
        store = {'users': {self.ADMIN_USER: {'hash': 'x', 'must_change':
                                             False, 'locked': False}}}
        with ExitStack() as stack:
            for patcher in (
                    mock.patch.object(app, 'get_human_users',
                                      return_value=USERS),
                    mock.patch.object(app, 'load_bedtime', return_value=BEDTIME),
                    mock.patch.object(app, 'get_lock_status',
                                      side_effect=lambda u: LOCK[u]),
                    mock.patch.object(app, 'is_in_bedtime', return_value=False),
                    mock.patch.object(app, 'bonus_state',
                                      return_value={'active': False}),
                    mock.patch.object(app, 'active_session_count',
                                      return_value=0),
                    mock.patch.object(auth, 'is_wheel_member', side_effect=(
                        lambda u: u == self.ADMIN_USER)),
                    mock.patch.object(auth, 'load_store', return_value=store)):
                stack.enter_context(patcher)
            return self.client.get('/').get_data(as_text=True)

    def test_the_section_heading_says_child_accounts(self):
        html = self._render()
        self.assertIn(
            '<h2 class="pc-card__title pc-accounts__heading">Child Accounts</h2>',
            html)

    def test_the_old_accounts_heading_is_gone(self):
        html = self._render()
        self.assertNotIn('pc-accounts__heading">Accounts<', html)
        self.assertNotIn('>Accounts</h2>', html)

    def test_the_pinned_selectors_survive_the_rename(self):
        # §7.4 pins the container id and the two classes; only the visible
        # copy changed.
        html = self._render()
        self.assertIn('id="accounts"', html)
        self.assertIn('class="pc-stack pc-accounts"', html)
        self.assertIn('pc-accounts__heading', html)

    def test_the_template_source_names_the_new_heading_once(self):
        src = read_template()
        self.assertIn('>Child Accounts</h2>', src)
        self.assertNotIn('pc-accounts__heading">Accounts</h2>', src)


class OverviewCardMetaTest(_AdminSessionMixin, unittest.TestCase):
    def _render_all(self):
        store = {'users': {self.ADMIN_USER: {'hash': 'x', 'must_change':
                                             False, 'locked': False}}}
        with ExitStack() as stack:
            for patcher in (
                    mock.patch.object(app, 'get_human_users',
                                      return_value=USERS),
                    mock.patch.object(app, 'load_bedtime', return_value=BEDTIME),
                    mock.patch.object(app, 'get_lock_status',
                                      side_effect=lambda u: LOCK[u]),
                    mock.patch.object(app, 'is_in_bedtime', return_value=False),
                    mock.patch.object(app, 'bonus_state',
                                      return_value={'active': False}),
                    mock.patch.object(app, 'active_session_count',
                                      return_value=0),
                    mock.patch.object(auth, 'is_wheel_member', side_effect=(
                        lambda u: u == self.ADMIN_USER)),
                    mock.patch.object(auth, 'load_store', return_value=store)):
                stack.enter_context(patcher)
            html = self.client.get('/').get_data(as_text=True)
            rows = self.client.get('/api/users').get_json()
            user_html = self.client.get('/user/' + KID).get_data(as_text=True)
        return html, {r['username']: r for r in rows}, user_html

    def test_the_rendered_overview_shows_no_uid(self):
        html, _rows, _user = self._render_all()
        self.assertNotIn('UID', html,
                         'the Overview card must not show the numeric account '
                         'id anywhere — not in a card, not in a badge')

    def test_the_card_region_shows_no_uid_number(self):
        html, _rows, _user = self._render_all()
        region = accounts_region(html)
        self.assertIn('id="row-%s"' % KID, region)
        self.assertNotIn('1001', region)
        self.assertNotIn('1002', region)

    def test_the_bedtime_segment_of_the_meta_survives(self):
        html, _rows, _user = self._render_all()
        region = accounts_region(html)
        self.assertIn('Bedtime 9:30 PM – 7:00 AM (Mon, Tue, Wed, Thu, Fri)',
                      region)

    def test_an_account_without_a_schedule_still_renders_a_meta_line(self):
        # The <p> stays (same DOM in both paints); it is just empty for an
        # account with no bedtime window, exactly as the rebuild renders it.
        html, _rows, _user = self._render_all()
        region = accounts_region(html)
        self.assertEqual(2, region.count('class="pc-meta pc-account__meta"'),
                         'one meta line per account card, scheduled or not')

    def test_the_rebuild_script_no_longer_reads_the_uid(self):
        src = read_template()
        start = src.index('function accountHtml(u)')
        body = src[start:src.index('function setStale', start)]
        self.assertNotIn('u.uid', body)
        self.assertNotIn('UID', body)
        self.assertNotIn('uid', js_fields_read_by_account_html(),
                         'uid left the JS rebuild contract on 2026-10-18')

    def test_the_payload_still_carries_the_uid_key(self):
        # Only the Overview stopped using it; the Details page still does.
        _html, rows, _user = self._render_all()
        for username, row in sorted(rows.items()):
            with self.subTest(row=username):
                self.assertIn('uid', row)

    def test_the_details_page_still_shows_uid_shell_home(self):
        _html, _rows, user_html = self._render_all()
        self.assertIn('UID: 1001', user_html)
        self.assertIn('Shell: /bin/bash', user_html)
        self.assertIn('Home: /home/' + KID, user_html)


if __name__ == '__main__':
    unittest.main()