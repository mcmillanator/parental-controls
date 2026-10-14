#!/usr/bin/env python3
"""No-JS Overview action forms (UI_UX_PLAN.md progressive enhancement).

Every Overview action — Lock / Unlock / Lock screen / Emergency logout and
admin add/remove — is a plain ``<form method="post">`` to ``/overview/*``.
Those routes run the *same* core as their ``/api/*`` twins, flash the outcome
and redirect back to the Overview (POST/Redirect/GET). The page script only
adds the shared confirmation dialog on top; with JavaScript disabled the forms
still submit and the server still acts and audits.

These tests exercise the form endpoints directly with the ``csrf_token`` form
field (the genuine no-JS path), the server-rendered markup, the wheel-admin
protections and the ``/api/*`` JSON contract's untouched-ness. ``usermod`` /
``loginctl`` are always mocked; no real account, the service, ``/etc``,
``/opt`` or the checkout is touched.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import app
import auth
from test_lock_regression import _AdminSessionMixin, _result, KID

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates')

USERS = [
    {'username': 'kidlocked', 'uid': 1001, 'home': '/home/kidlocked',
     'shell': '/bin/bash'},
    {'username': 'kidfree', 'uid': 1002, 'home': '/home/kidfree',
     'shell': '/bin/bash'},
]
LOCK = {'kidlocked': (True, 'Locked'), 'kidfree': (False, 'Unlocked')}


class _OverviewFormsBase(_AdminSessionMixin, unittest.TestCase):
    """Logged-in wheel admin (qaadmin) + temp auth/audit stores."""

    APP_ONLY_USER = 'apponly'
    APP_ONLY_PW = 'App-Only-Test-Pass-456'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._apponly_hash = auth.hash_password(cls.APP_ONLY_PW)

    def setUp(self):
        super().setUp()
        self._audit_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._audit_tmp.cleanup)
        os.environ['PARENTAL_CONTROLS_AUDIT'] = os.path.join(
            self._audit_tmp.name, 'audit.jsonl')
        self.addCleanup(lambda: os.environ.pop('PARENTAL_CONTROLS_AUDIT',
                                               None))
        self.csrf = self.csrf_headers['X-CSRF-Token']

    # -- helpers ----------------------------------------------------------

    def _home_mocks(self, users=(), lock=None, wheel=()):
        lock = lock or {}
        return (
            mock.patch.object(app, 'get_human_users', return_value=list(users)),
            mock.patch.object(app, 'load_bedtime', return_value={}),
            mock.patch.object(app, 'get_lock_status',
                              side_effect=lambda u: lock.get(u, (False, 'Unlocked'))),
            mock.patch.object(app, 'is_in_bedtime', return_value=False),
            mock.patch.object(app, 'bonus_state', return_value={'active': False}),
            mock.patch.object(app.auth, 'is_wheel_member',
                              side_effect=lambda u: u == self.ADMIN_USER
                              or u in wheel),
        )

    def _render_home(self, users=USERS, lock=None, wheel=(), device=''):
        """GET / with mocked live data; returns the rendered HTML."""
        mocks = self._home_mocks(users, lock, wheel)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5]:
            resp = self.client.get('/' + (('?device=' + device) if device else ''))
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def _post_raw(self, path, data):
        """POST with the CSRF token in the form field, no redirect follow."""
        payload = dict(data)
        payload.setdefault('csrf_token', self.csrf)
        return self.client.post(path, data=payload)

    def _post_follow(self, path, data):
        """POST the no-JS form and follow the PRG redirect back to /."""
        mocks = self._home_mocks(users=[])
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            return self.client.post(path, data=dict(
                data, csrf_token=self.csrf), follow_redirects=True)

    def _fake_runner(self, calls, rc=0, stderr=''):
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _result(rc, stderr=stderr)
        return fake_run

    def _add_row(self, username, must_change=False, locked=False):
        store = auth.load_store()
        store['users'][username] = {'hash': self._apponly_hash,
                                    'must_change': must_change,
                                    'locked': locked}
        auth.save_store(store)

    def _login_app_only(self):
        """Log the test client in as a fresh app-only (non-wheel) admin.

        Returns the session's CSRF headers. After this the client's session
        is the app-only one (the wheel harness session is replaced).
        """
        self._add_row(self.APP_ONLY_USER)
        resp = self.client.post('/login',
                                data={'username': self.APP_ONLY_USER,
                                      'password': self.APP_ONLY_PW})
        self.assertEqual(resp.status_code, 302, 'app-only login failed')
        sid = resp.headers.get('Set-Cookie', '').split('pc_sid=')[1].split(
            ';')[0]
        return {'X-CSRF-Token': auth._sessions[sid]['csrf']}


class TestAccountActionForms(_OverviewFormsBase):
    """The four account actions work as real POST forms (PRG + flash)."""

    def test_lock_form_redirects_and_locks(self):
        calls = []
        with mock.patch.object(app, 'note_manual_lock') as nml, \
             mock.patch.object(app.subprocess, 'run',
                               side_effect=self._fake_runner(calls)):
            resp = self._post_raw('/overview/lock', {'username': KID})
        self.assertEqual(302, resp.status_code)
        self.assertTrue(resp.headers['Location'].endswith('/'))
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        # Session-preserving: only /overview/logout terminates.
        self.assertFalse([c for c in calls if 'terminate-user' in c])
        nml.assert_called_once_with(KID)

    def test_lock_form_flashes_a_success_banner(self):
        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run',
                               side_effect=self._fake_runner([])):
            resp = self._post_follow('/overview/lock', {'username': KID})
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        self.assertIn('pc-banner pc-banner--ok', html)
        self.assertIn('Locked %s' % KID, html)

    def test_lock_form_refused_without_csrf(self):
        with mock.patch.object(app.subprocess, 'run') as run:
            resp = self.client.post('/overview/lock', data={'username': KID})
        self.assertEqual(403, resp.status_code)
        run.assert_not_called()

    def test_lock_form_failure_flashes_danger_and_does_not_transfer(self):
        with mock.patch.object(app, 'note_manual_lock') as nml, \
             mock.patch.object(app.subprocess, 'run',
                               side_effect=self._fake_runner(
                                   [], rc=1, stderr='usermod: nope')):
            resp = self._post_follow('/overview/lock', {'username': KID})
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        self.assertIn('pc-banner pc-banner--danger', html)
        self.assertIn('usermod -L:', html)
        nml.assert_not_called()

    def test_wheel_target_refused_by_the_form_path(self):
        with mock.patch.object(app.device_policy, 'wheel_refusal',
                               return_value='Refused: kid is a wheel admin'), \
             mock.patch.object(app.subprocess, 'run') as run:
            resp = self._post_follow('/overview/lock', {'username': 'kid'})
        self.assertEqual(200, resp.status_code)
        self.assertIn('can never be', resp.get_data(as_text=True))
        run.assert_not_called()

    def test_invalid_username_never_reaches_a_command(self):
        for bad in ('bad;user', '-x', 'a b', ''):
            with self.subTest(username=bad), \
                 mock.patch.object(app.subprocess, 'run') as run:
                resp = self._post_follow('/overview/lock',
                                         {'username': bad})
            self.assertEqual(200, resp.status_code)
            run.assert_not_called()

    def test_lock_screen_form_locks_without_terminating(self):
        calls = []
        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run',
                               side_effect=self._fake_runner(calls)):
            resp = self._post_follow('/overview/lock-screen',
                                     {'username': KID})
        self.assertEqual(200, resp.status_code)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        self.assertFalse([c for c in calls if 'terminate-user' in c])
        self.assertIn('Locked the screen for %s' % KID,
                      resp.get_data(as_text=True))

    def test_unlock_form_unlocks_and_hands_off_ownership(self):
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock, \
             mock.patch.object(app, 'note_manual_unlock') as nmu:
            resp = self._post_follow('/overview/unlock', {'username': KID})
        self.assertEqual(200, resp.status_code)
        self.assertEqual(lock.call_args[0], (KID, 'unlock'))
        nmu.assert_called_once_with(KID)
        self.assertIn('Unlocked %s' % KID, resp.get_data(as_text=True))

    def test_logout_form_locks_and_terminates(self):
        calls = []
        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run',
                               side_effect=self._fake_runner(calls)):
            resp = self._post_follow('/overview/logout', {'username': KID})
        self.assertEqual(200, resp.status_code)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        self.assertTrue([c for c in calls if 'terminate-user' in c])
        self.assertIn('logged out', resp.get_data(as_text=True))


class TestAdminForms(_OverviewFormsBase):
    """Admin add/remove work as real POST forms; wheel-only, wheel-safe."""

    def test_add_form_creates_admin_and_flashes_one_time_password(self):
        resp = self._post_follow('/overview/admins', {'username': 'helper'})
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        self.assertIn('One-time password:', html)
        self.assertIn('pc-banner pc-banner--ok', html)
        row = auth.load_store()['users']['helper']
        self.assertTrue(row['must_change'])
        self.assertFalse(row['locked'])

    def test_add_form_refuses_wheel_target(self):
        with mock.patch.object(app.auth, 'is_wheel_member',
                              side_effect=lambda u: u in (self.ADMIN_USER,
                                                          'boss')):
            resp = self._post_follow('/overview/admins', {'username': 'boss'})
        self.assertIn('pc-banner pc-banner--danger',
                      resp.get_data(as_text=True))
        self.assertNotIn('boss', auth.load_store()['users'])

    def test_remove_form_deletes_admin(self):
        self._post_follow('/overview/admins', {'username': 'helper'})
        self.assertIn('helper', auth.load_store()['users'])
        resp = self._post_follow('/overview/admins/remove',
                                 {'username': 'helper'})
        self.assertEqual(200, resp.status_code)
        self.assertIn('pc-banner pc-banner--ok', resp.get_data(as_text=True))
        self.assertNotIn('helper', auth.load_store()['users'])

    def test_remove_form_refuses_wheel_target(self):
        resp = self._post_follow('/overview/admins/remove',
                                 {'username': self.ADMIN_USER})
        self.assertIn('can never be', resp.get_data(as_text=True))
        self.assertIn(self.ADMIN_USER, auth.load_store()['users'])

    def test_non_wheel_actor_cannot_add_or_remove(self):
        headers = self._login_app_only()
        token = headers['X-CSRF-Token']
        resp = self.client.post('/overview/admins',
                                data={'username': 'helper',
                                      'csrf_token': token})
        self.assertEqual(302, resp.status_code)
        self.assertNotIn('helper', auth.load_store()['users'])

    def test_admin_panel_markup_present_for_wheel_actor(self):
        wheel_html = self._render_home()
        self.assertIn('id="admin-add"', wheel_html)
        self.assertIn('id="admin-list"', wheel_html)
        self.assertIn('id="admin-panel"', wheel_html)

    def test_admin_add_visible_for_non_wheel_actor_but_post_refused(self):
        self._login_app_only()
        mocks = self._home_mocks(users=[])
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            resp = self.client.get('/')
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        # Katalon asserts these on the index page for every admin (04/09);
        # they must stay visible even though the endpoint refuses non-wheel
        # actors. The refusal is covered by
        # test_non_wheel_actor_cannot_add_or_remove above.
        self.assertIn('id="admin-add"', html)
        self.assertIn('id="new-admin-username"', html)
        self.assertIn('id="admin-panel"', html)
        self.assertIn('id="admin-list"', html)
        # ...and the add form is a real POST form.
        self.assertIn('action="/overview/admins"', html)
        self.assertIn('name="csrf_token"', html)


class TestServerRenderedForms(_OverviewFormsBase):
    """The account cards are real forms with a CSRF token + username."""

    def test_every_action_is_a_real_post_form(self):
        html = self._render_home(users=USERS, lock=LOCK)
        for action in ('/overview/lock', '/overview/unlock',
                       '/overview/lock-screen', '/overview/logout'):
            with self.subTest(action=action):
                self.assertIn('action="%s"' % action, html)
        self.assertIn('name="csrf_token"', html)
        # The lock/unlock buttons are submit buttons bound to a form.
        self.assertIn('class="pc-btn pc-btn--primary btn-lock"', html)
        self.assertIn('class="pc-btn pc-btn--primary btn-unlock"', html)
        self.assertIn('class="pc-btn pc-btn--neutral btn-lockscreen"', html)
        self.assertIn('class="pc-btn pc-btn--danger btn-logout"', html)

    def test_refresh_is_skipped_while_a_confirm_dialog_is_open(self):
        with open(os.path.join(TEMPLATE_DIR, 'index.html'), encoding='utf-8') as fh:
            src = fh.read()
        # The guard must exist inside refreshTable and run before the account
        # list is rebuilt, so an open #pc-confirm dialog never has its form
        # node swapped out (the whole reason actions carry their own form).
        fn = src.index('async function refreshTable')
        self.assertIn("document.querySelector('dialog[open]')", src[fn:])
        guard = src.index("document.querySelector('dialog[open]')", fn)
        rebuild = src.index('list.innerHTML = users.map(accountHtml)', fn)
        self.assertLess(guard, rebuild)

    def test_wheel_card_offers_no_action_forms(self):
        html = self._render_home(users=USERS, lock=LOCK, wheel=('kidfree',))
        idx = html.index('id="row-kidfree"')
        card = html[idx:html.index('</article>', idx)]
        self.assertIn('badge-protected', card)
        for cls in ('btn-lock', 'btn-unlock', 'btn-lockscreen', 'btn-logout'):
            self.assertNotIn(cls, card)
        self.assertNotIn('<form', card)

    def test_remote_device_forms_carry_the_device_field(self):
        entry = {'name': 'boxb', 'url': 'http://localhost:5001'}
        remote_users = [dict(USERS[1], locked=False, status_detail='Unlocked',
                             bedtime={}, in_bedtime=False, wheel=False,
                             bonus={'active': False})]
        with mock.patch.object(app.remote, 'get_device', return_value=entry), \
             mock.patch.object(app, '_remote_users',
                               return_value=(remote_users, None)):
            html = self._render_home(users=[], device='boxb')
        self.assertIn('name="device" value="boxb"', html)
        self.assertIn('action="/overview/lock"', html)

    def test_remote_device_post_proxies_instead_of_running_locally(self):
        entry = {'name': 'boxb', 'url': 'http://localhost:5001'}
        with mock.patch.object(app.remote, 'get_device', return_value=entry), \
             mock.patch.object(app, '_remote_proxy',
                               return_value=(200, {'success': True,
                                                   'message': 'Locked'},
                                             False)) as proxied, \
             mock.patch.object(app.subprocess, 'run') as run:
            resp = self._post_raw('/overview/lock',
                                  {'username': KID, 'device': 'boxb'})
        self.assertEqual(302, resp.status_code)
        self.assertTrue(resp.headers['Location'].endswith('device=boxb'))
        run.assert_not_called()
        self.assertEqual('/lock', proxied.call_args[0][2])


class TestApiContractUnchanged(_OverviewFormsBase):
    """The form work must not disturb the JSON /api/* contract."""

    def test_api_lock_still_returns_json(self):
        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run',
                               side_effect=self._fake_runner([])):
            resp = self.authed_post('/api/lock', data={'username': KID})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.get_json()['success'])

    def test_api_lock_wheel_refusal_still_403_json(self):
        with mock.patch.object(app.device_policy, 'wheel_refusal',
                               return_value='Refused: nope'):
            resp = self.authed_post('/api/lock', data={'username': KID})
        self.assertEqual(403, resp.status_code)
        self.assertFalse(resp.get_json()['success'])

    def test_api_admins_add_still_returns_the_password(self):
        resp = self.authed_post('/api/admins', data={'username': 'helper'})
        self.assertEqual(200, resp.status_code)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertGreaterEqual(len(payload['password']), 12)


if __name__ == '__main__':
    unittest.main()
