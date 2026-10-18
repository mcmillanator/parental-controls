#!/usr/bin/env python3
"""No-JS action forms (UI_UX_PLAN.md progressive enhancement).

Every Overview account action — Lock / Unlock — is a plain ``<form
method="post">`` submission to ``/overview/*``. Those routes run the *same*
core as their ``/api/*`` twins, flash the outcome and redirect back to the
Overview (POST/Redirect/GET). The admin add/remove forms moved to the Admin
console (``/admin/audit``) on 2026-10-18 (owner decision: admins are hidden
from the Overview); they keep the pinned ``/overview/admins[/remove]`` PATHS
but redirect back to the console. The page scripts only add the shared
confirmation dialog on top; with JavaScript disabled the forms still submit
and the server still acts and audits.

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
    """The Overview account actions work as real POST forms (PRG + flash)."""

    def test_lock_form_redirects_and_locks(self):
        calls = []
        with mock.patch.object(app, 'note_manual_lock') as nml, \
             mock.patch.object(app.subprocess, 'run',
                               side_effect=self._fake_runner(calls)):
            resp = self._post_raw('/overview/lock', {'username': KID})
        self.assertEqual(302, resp.status_code)
        self.assertTrue(resp.headers['Location'].endswith('/'))
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        # Session-preserving: no Overview form terminates (that is /api/logout).
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

    def test_lock_screen_form_route_is_gone(self):
        # The redundant Overview "Lock screen" button was removed (owner
        # request): its form route no longer exists. Lock already covers it.
        with mock.patch.object(app.subprocess, 'run') as run:
            resp = self._post_raw('/overview/lock-screen', {'username': KID})
        self.assertEqual(404, resp.status_code)
        run.assert_not_called()

    def test_lock_also_locks_the_screen(self):
        # Removing the "Lock screen" button loses no behaviour: the single
        # Overview Lock action still locks the account *and* the child's
        # screen. /api/lock is unchanged and must still invoke
        # `loginctl lock-session` on the resolved session.
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if 'show-user' in cmd:
                return _result(0, stdout='21\n')
            if 'show-session' in cmd:
                return _result(0, stdout='Type=wayland\nClass=user\n')
            return _result(0)

        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app.subprocess, 'run', side_effect=fake_run):
            resp = self._post_raw('/api/lock', {'username': KID})
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.get_json()['success'])
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        self.assertIn(['sudo', 'loginctl', 'lock-session', '21'], calls)
        # session-preserving: /api/lock never terminates
        self.assertFalse([c for c in calls if 'terminate-user' in c])

    def test_unlock_form_unlocks_and_hands_off_ownership(self):
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock, \
             mock.patch.object(app, 'note_manual_unlock') as nmu:
            resp = self._post_follow('/overview/unlock', {'username': KID})
        self.assertEqual(200, resp.status_code)
        self.assertEqual(lock.call_args[0], (KID, 'unlock'))
        nmu.assert_called_once_with(KID)
        self.assertIn('Unlocked %s' % KID, resp.get_data(as_text=True))


class TestAdminForms(_OverviewFormsBase):
    """Admin add/remove work as real POST forms on the ADMIN CONSOLE.

    The forms moved from the Overview to /admin/audit (owner decision,
    2026-10-18): the endpoints keep their pinned /overview/admins[/remove]
    paths but PRG back to the console, where the flash (including the
    one-time password) renders ONCE.
    """

    def _page_mocks(self, users=USERS, lock=None, wheel=()):
        """Mocks for rendering /admin/audit deterministically."""
        return self._home_mocks(users, lock, wheel)

    def _render_audit(self, wheel=()):
        mocks = self._page_mocks(wheel=wheel)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5]:
            resp = self.client.get('/admin/audit')
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def test_add_form_posts_from_the_audit_page_markup(self):
        html = self._render_audit()
        self.assertIn('id="admin-add"', html)
        self.assertIn('action="/overview/admins"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('id="new-admin-username"', html)
        # The audit page's own pinned field copy is the surviving variant
        # (the Overview card's wording died with the card).
        self.assertIn('New app-only admin username:', html)
        self.assertIn('placeholder="new app-only admin username"', html)
        # Per-row Remove is a plain POST form too, gated only by the
        # confirm-dialog handler.
        self._add_row('helper')
        html = self._render_audit()
        idx = html.index('data-username="helper"')
        row = html[idx:html.index('</tr>', idx)]
        self.assertIn('action="/overview/admins/remove"', row)
        self.assertIn('onsubmit="return removeAdmin(this)"', row)
        self.assertIn('name="username" value="helper"', row)

    def test_add_form_redirects_to_the_console(self):
        resp = self._post_raw('/overview/admins', {'username': 'helper'})
        self.assertEqual(302, resp.status_code)
        self.assertTrue(resp.headers['Location'].endswith('/admin/audit'))

    def test_remove_form_redirects_to_the_console(self):
        self._add_row('helper')
        resp = self._post_raw('/overview/admins/remove',
                              {'username': 'helper'})
        self.assertEqual(302, resp.status_code)
        self.assertTrue(resp.headers['Location'].endswith('/admin/audit'))

    def test_add_form_creates_admin_and_flashes_one_time_password(self):
        resp = self._post_follow('/overview/admins', {'username': 'helper'})
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        # The follow landed on the console…
        self.assertIn('id="admin-table"', html)
        # …where the OTP flash renders, ONCE.
        self.assertIn('One-time password:', html)
        self.assertEqual(1, html.count('One-time password:'),
                         'the one-time password must render exactly once')
        row = auth.load_store()['users']['helper']
        self.assertTrue(row['must_change'])
        self.assertFalse(row['locked'])
        # Flashes are consumed: a second GET shows no OTP banner (the audit
        # page's own chain-status banner carries pc-banner--ok too, so the
        # flash must be verified by its text, not by the banner class).
        html2 = self.client.get('/admin/audit',
                                 headers=self.csrf_headers).get_data(
                                     as_text=True)
        self.assertNotIn('One-time password:', html2)

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

    def test_admin_mutating_forms_require_csrf(self):
        for path in ('/overview/admins', '/overview/admins/remove'):
            with self.subTest(path=path):
                resp = self.client.post(path, data={'username': 'helper'})
                self.assertEqual(403, resp.status_code)
        self.assertNotIn('helper', auth.load_store()['users'])

    def test_admin_panel_markup_is_gone_from_the_overview(self):
        # Owner asked to hide this UI: the card died with the move, and the
        # ids must not come back on '/'.
        html = self._render_home()
        for gone in ('id="admin-panel"', 'id="admin-list"',
                     'id="admin-count"', 'id="admin-add"',
                     'id="new-admin-username"', 'action="/overview/admins"'):
            with self.subTest(needle=gone):
                self.assertNotIn(gone, html)

    def test_admin_add_absent_for_non_wheel_actor_on_the_console(self):
        # The console gates the add form by tier (wheel-actor only): the
        # Overview's "visible to every admin, refused server-side" variant
        # did not survive the move — test_admin_console pins the console's
        # conditional, the refusal is covered by
        # test_non_wheel_actor_cannot_add_or_remove above.
        self._login_app_only()
        mocks = self._page_mocks(users=[])
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            resp = self.client.get('/admin/audit')
        self.assertEqual(200, resp.status_code)
        markup = resp.get_data(as_text=True).split('<script>')[0]
        self.assertNotIn('id="admin-add"', markup)


class TestServerRenderedForms(_OverviewFormsBase):
    """The account cards are real forms with a CSRF token + username."""

    def test_every_action_is_a_real_post_form(self):
        html = self._render_home(users=USERS, lock=LOCK)
        for action in ('/overview/lock', '/overview/unlock'):
            with self.subTest(action=action):
                self.assertIn('action="%s"' % action, html)
        self.assertIn('name="csrf_token"', html)
        # The lock/unlock buttons are submit buttons bound to a form.
        self.assertIn('class="pc-btn pc-btn--primary btn-lock"', html)
        self.assertIn('class="pc-btn pc-btn--primary btn-unlock"', html)
        # The redundant "Lock screen" control is gone (Lock covers it).
        self.assertNotIn('btn-lockscreen', html)
        self.assertNotIn('/overview/lock-screen', html)
        # Emergency logout is no longer offered on the Overview.
        self.assertNotIn('btn-logout', html)
        self.assertNotIn('/overview/logout', html)

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

    def test_wheel_account_gets_no_card_at_all(self):
        # Was: "wheel card offers no action forms". Superseded 2026-10-18:
        # the wheel row is filtered out of the Overview entirely — no card,
        # no badges, nothing to disable. The endpoints still refuse wheel
        # targets independently (defence in depth).
        html = self._render_home(users=USERS, lock=LOCK, wheel=('kidfree',))
        self.assertNotIn('id="row-kidfree"', html)
        # the child that is NOT wheel still renders with its real forms
        # (kidlocked carries the Unlock form; kidfree — the Lock carrier —
        # is the wheel admin that vanished).
        self.assertIn('id="row-kidlocked"', html)
        self.assertIn('action="/overview/unlock"', html)
        self.assertNotIn('action="/overview/lock"', html)

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
