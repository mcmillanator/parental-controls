#!/usr/bin/env python3
"""MVP auth tests: every denial path (AUTH_DESIGN.md build order item 9).

Uses the Flask test client with a temp auth store (PARENTAL_CONTROLS_AUTH)
and mocked wheel membership — no real users, locks, or sessions touched.
The only real crypto is argon2 hashing (placeholders, never real secrets).
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import auth
import app


WHEEL_PW = 'Wheel-Test-Pass-123'
APP_PW = 'App-Test-Pass-12345'
CHILD_PW = 'Child-Test-Pass-123'
LOCKED_PW = 'Locked-Test-Pass-12'
FIRST_PW = 'First-Test-Pass-123'


def _row(pw_hash, must_change=False, locked=False):
    return {'hash': pw_hash, 'must_change': must_change, 'locked': locked}


class AuthTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Hash once (argon2 is deliberately slow); rewrite rows per test.
        cls.hashes = {
            'testwheel': auth.hash_password(WHEEL_PW),
            'testapp': auth.hash_password(APP_PW),
            'testchild': auth.hash_password(CHILD_PW),
            'testlocked': auth.hash_password(LOCKED_PW),
            'testfirst': auth.hash_password(FIRST_PW),
        }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth_path = os.path.join(self.tmp.name, 'auth.json')
        self.wheel_members = {'testwheel'}
        # System accounts known to the (mocked) OS layer. testapp,
        # testlocked and testfirst are app-only admins (no system
        # account); testchild is a regular local user that must never
        # authenticate despite holding a store row (defense in depth).
        self.system_users = {'testwheel', 'testchild'}
        self._write_store()
        os.environ['PARENTAL_CONTROLS_AUTH'] = self.auth_path
        auth.clear_sessions()
        auth.clear_login_failures()
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()
        patcher_w = mock.patch.object(auth, 'is_wheel_member',
                                      side_effect=lambda u: u in self.wheel_members)
        patcher_s = mock.patch.object(auth, 'is_system_user',
                                      side_effect=lambda u: u in self.system_users)
        self.addCleanup(patcher_w.stop)
        self.addCleanup(patcher_s.stop)
        patcher_w.start()
        patcher_s.start()

    def tearDown(self):
        os.environ.pop('PARENTAL_CONTROLS_AUTH', None)
        auth.clear_sessions()
        auth.clear_login_failures()
        self.tmp.cleanup()

    def _write_store(self, locked=(), must_change=('testfirst',)):
        users = {}
        for name, h in self.hashes.items():
            users[name] = _row(h, must_change=(name in must_change),
                               locked=(name in locked))
        with open(self.auth_path, 'w') as f:
            json.dump({'users': users}, f)
        os.chmod(self.auth_path, 0o600)

    def login(self, username, password):
        return self.client.post('/login',
                                data={'username': username,
                                      'password': password})

    def login_ok(self, username, password):
        resp = self.login(username, password)
        self.assertIn(resp.status_code, (302,),
                      f'login failed for {username}: {resp.status_code}')
        return resp

    def csrf_for(self, login_resp):
        sid = login_resp.headers.get('Set-Cookie', '').split(
            'pc_sid=')[1].split(';')[0]
        return sid, auth._sessions[sid]['csrf']

    def authed_client(self, username, password):
        resp = self.login_ok(username, password)
        sid, csrf = self.csrf_for(resp)
        return resp, {'X-CSRF-Token': csrf}


class TestUnauthenticated(AuthTestBase):
    def test_pages_redirect_to_login(self):
        for path in ('/', '/user/bluefox'):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 302, path)
            self.assertIn('/login', resp.headers['Location'])

    def test_api_returns_401(self):
        self.assertEqual(self.client.get('/api/users').status_code, 401)
        resp = self.client.post('/api/unlock', data={'username': 'bluefox'})
        self.assertEqual(resp.status_code, 401)


class TestLogin(AuthTestBase):
    def test_wheel_login_ok(self):
        resp = self.login_ok('testwheel', WHEEL_PW)
        self.assertIn('pc_sid=', resp.headers.get('Set-Cookie', ''))

    def test_app_only_login_ok(self):
        self.login_ok('testapp', APP_PW)

    def test_child_account_refused(self):
        """A non-wheel system account never authenticates — generic 401."""
        resp = self.login('testchild', CHILD_PW)
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn('pc_sid=', resp.headers.get('Set-Cookie', ''))
        # No session was minted: still unauthenticated.
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_unknown_user_refused(self):
        resp = self.login('ghost', 'whatever-password-123')
        self.assertEqual(resp.status_code, 401)

    def test_wrong_password_refused(self):
        resp = self.login('testwheel', 'wrong-password-12345')
        self.assertEqual(resp.status_code, 401)

    def test_locked_admin_refused_with_recovery_hint(self):
        self._write_store(locked=('testlocked',))
        resp = self.login('testlocked', LOCKED_PW)
        self.assertEqual(resp.status_code, 403)
        self.assertIn('reset-password', resp.get_data(as_text=True))

    def test_backoff_kicks_in(self):
        self.login('testwheel', 'wrong-password-12345')
        resp = self.login('testwheel', 'wrong-password-12345')
        self.assertEqual(resp.status_code, 429)
        self.assertIn('Retry-After', resp.headers)

    def test_must_change_forced(self):
        resp = self.login_ok('testfirst', FIRST_PW)
        self.assertIn('/change-password', resp.headers['Location'])
        # Every other route redirects until the password changes.
        page = self.client.get('/')
        self.assertEqual(page.status_code, 302)
        self.assertIn('/change-password', page.headers['Location'])
        api = self.client.get('/api/users')
        self.assertEqual(api.status_code, 403)
        self.assertTrue(api.get_json().get('must_change'))
        # Wrong current password cannot clear the flag.
        _sid, csrf = self.csrf_for(resp)
        bad = self.client.post(
            '/change-password',
            data={'current': 'nope-nope-nope-no', 'new': 'New-Test-Pass-12345',
                  'confirm': 'New-Test-Pass-12345',
                  'csrf_token': csrf})
        self.assertEqual(bad.status_code, 403)
        # Correct change clears the flag and lands on the index.
        with mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')):
            good = self.client.post(
                '/change-password',
                data={'current': FIRST_PW, 'new': 'New-Test-Pass-12345',
                      'confirm': 'New-Test-Pass-12345', 'csrf_token': csrf})
            self.assertEqual(good.status_code, 302)
            home = self.client.get('/')
            self.assertEqual(home.status_code, 200)

    def test_change_password_short_rejected(self):
        resp = self.login_ok('testfirst', FIRST_PW)
        _sid, csrf = self.csrf_for(resp)
        bad = self.client.post(
            '/change-password',
            data={'current': FIRST_PW, 'new': 'short',
                  'confirm': 'short', 'csrf_token': csrf})
        self.assertEqual(bad.status_code, 400)


class TestSessions(AuthTestBase):
    def test_tampered_session_rejected(self):
        self.client.set_cookie('pc_sid', 'forged-session-id')
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertEqual(self.client.get('/api/users').status_code, 401)

    def test_logout_destroys_session(self):
        resp = self.login_ok('testwheel', WHEEL_PW)
        sid = resp.headers['Set-Cookie'].split('pc_sid=')[1].split(';')[0]
        self.client.post('/logout')
        self.assertEqual(self.client.get('/').status_code, 302)
        # The old cookie is dead even if replayed.
        self.client.set_cookie('pc_sid', sid)
        self.assertEqual(self.client.get('/api/users').status_code, 401)

    def test_idle_timeout_enforced(self):
        resp = self.login_ok('testwheel', WHEEL_PW)
        sid = resp.headers['Set-Cookie'].split('pc_sid=')[1].split(';')[0]
        auth._sessions[sid]['last_seen'] -= (auth.IDLE_TIMEOUT_SECONDS + 1)
        self.assertEqual(self.client.get('/api/users').status_code, 401)

    def test_absolute_timeout_enforced(self):
        resp = self.login_ok('testwheel', WHEEL_PW)
        sid = resp.headers['Set-Cookie'].split('pc_sid=')[1].split(';')[0]
        auth._sessions[sid]['created'] -= (auth.ABSOLUTE_LIFETIME_SECONDS + 1)
        self.assertEqual(self.client.get('/api/users').status_code, 401)

    def test_revoked_wheel_member_refused(self):
        """Removing someone from wheel ends access on the next request."""
        self.login_ok('testwheel', WHEEL_PW)
        with mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')):
            self.assertEqual(self.client.get('/').status_code, 200)
        self.wheel_members.discard('testwheel')
        # Still a system account, just no longer a wheel member: the
        # realistic revocation scenario -> no tier at all.
        resp = self.client.get('/api/users')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_locked_mid_session_refused(self):
        self.login_ok('testwheel', WHEEL_PW)
        self._write_store(locked=('testwheel',))
        resp = self.client.get('/api/users')
        self.assertEqual(resp.status_code, 403)
        self.assertIn('reset-password', resp.get_json()['error'])

    def test_removed_admin_session_killed(self):
        _resp, headers = self.authed_client('testapp', APP_PW)
        store = auth.load_store()
        del store['users']['testapp']
        auth.save_store(store)
        api = self.client.get('/api/users', headers=headers)
        self.assertEqual(api.status_code, 403)


class TestCsrf(AuthTestBase):
    def test_missing_csrf_rejected(self):
        self.login_ok('testwheel', WHEEL_PW)
        with mock.patch.object(app, 'run_lock_command') as m:
            resp = self.client.post('/api/unlock',
                                    data={'username': 'bluefox'})
            self.assertEqual(resp.status_code, 403)
            self.assertIn('CSRF', resp.get_json()['error'])
            m.assert_not_called()

    def test_valid_header_csrf_accepted(self):
        _resp, headers = self.authed_client('testwheel', WHEEL_PW)
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')):
            resp = self.client.post('/api/unlock', data={'username': 'bluefox'},
                                    headers=headers)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()['success'])

    def test_valid_form_csrf_accepted(self):
        resp = self.login_ok('testwheel', WHEEL_PW)
        _sid, csrf = self.csrf_for(resp)
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')):
            resp = self.client.post('/api/unlock',
                                    data={'username': 'bluefox',
                                          'csrf_token': csrf})
            self.assertEqual(resp.status_code, 200)

    def test_wrong_csrf_rejected(self):
        self.login_ok('testwheel', WHEEL_PW)
        resp = self.client.post('/api/unlock', data={'username': 'bluefox'},
                                headers={'X-CSRF-Token': 'wrong'})
        self.assertEqual(resp.status_code, 403)


class TestWheelInvariants(AuthTestBase):
    def test_wheel_cannot_lock_wheel(self):
        _resp, headers = self.authed_client('testwheel', WHEEL_PW)
        with mock.patch.object(app.subprocess, 'run') as m:
            m.side_effect = AssertionError('must not reach sudo')
            for path in ('/api/lock', '/api/lock-screen', '/api/logout'):
                resp = self.client.post(path, data={'username': 'testwheel'},
                                        headers=headers)
                self.assertEqual(resp.status_code, 403, path)
                self.assertIn('wheel', resp.get_json()['error'].lower())

    def test_app_only_cannot_lock_wheel(self):
        _resp, headers = self.authed_client('testapp', APP_PW)
        with mock.patch.object(app.subprocess, 'run') as m:
            m.side_effect = AssertionError('must not reach sudo')
            resp = self.client.post('/api/lock', data={'username': 'testwheel'},
                                    headers=headers)
            self.assertEqual(resp.status_code, 403)

    def test_wheel_cannot_remove_wheel(self):
        _resp, headers = self.authed_client('testwheel', WHEEL_PW)
        resp = self.client.post('/api/admins/remove',
                                data={'username': 'testwheel'}, headers=headers)
        self.assertEqual(resp.status_code, 403)

    def test_app_only_cannot_add_or_remove(self):
        """App-only admins cannot manage anyone (overreach)."""
        _resp, headers = self.authed_client('testapp', APP_PW)
        resp = self.client.post('/api/admins', data={'username': 'newapp'},
                                headers=headers)
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post('/api/admins/remove',
                                data={'username': 'testapp'}, headers=headers)
        self.assertEqual(resp.status_code, 403)

    def test_wheel_can_add_and_remove_app_only(self):
        _resp, headers = self.authed_client('testwheel', WHEEL_PW)
        resp = self.client.post('/api/admins', data={'username': 'newapp'},
                                headers=headers)
        self.assertEqual(resp.status_code, 200)
        password = resp.get_json()['password']
        self.assertTrue(len(password) >= 12)
        # New admin must change password at first login.
        login = self.login('newapp', password)
        self.assertIn('/change-password', login.headers['Location'])
        # Restore the wheel session (login replaced the client cookie)
        # and re-derive its CSRF token — the old token belongs to the
        # superseded session.
        resp2 = self.login_ok('testwheel', WHEEL_PW)
        _sid2, csrf2 = self.csrf_for(resp2)
        headers = {'X-CSRF-Token': csrf2}
        resp = self.client.post('/api/admins/remove',
                                data={'username': 'newapp'}, headers=headers)
        self.assertEqual(resp.status_code, 200)

    def test_add_system_nonwheel_refused(self):
        """Handing a login to a regular local user (child) is refused."""
        _resp, headers = self.authed_client('testwheel', WHEEL_PW)
        # A system account with no admin row at all.
        self.system_users.add('localsystemuser')
        resp = self.client.post('/api/admins',
                                data={'username': 'localsystemuser'},
                                headers=headers)
        self.assertEqual(resp.status_code, 403)


class TestStoreFailClosed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        AuthTestBase.setUpClass()
    def test_corrupt_store_refuses_startup(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'auth.json')
        with open(path, 'w') as f:
            f.write('{not json')
        with self.assertRaises(auth.AuthStoreError):
            auth.load_store(path)
        with mock.patch.dict(os.environ, {'PARENTAL_CONTROLS_AUTH': path}):
            with self.assertRaises(auth.AuthStoreError):
                auth.init_store()

    def test_bad_shape_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'auth.json')
        with open(path, 'w') as f:
            json.dump({'users': ['not', 'a', 'dict']}, f)
        with self.assertRaises(auth.AuthStoreError):
            auth.load_store(path)

    def test_corrupt_store_per_request_500(self):
        base = AuthTestBase()
        base.setUp()
        self.addCleanup(base.tearDown)
        base.login_ok('testwheel', WHEEL_PW)
        with open(base.auth_path, 'w') as f:
            f.write('garbage')
        resp = base.client.get('/api/users')
        self.assertEqual(resp.status_code, 500)

    def test_no_hash_backend_fails_closed(self):
        with mock.patch.object(auth, '_HASHER', None):
            with self.assertRaises(SystemExit):
                auth.ensure_hash_backend()
            self.assertFalse(auth.verify_password('anything', 'anything'))

    def test_classify_never_authenticates_child_row(self):
        """Even a store row for a non-wheel system user yields no tier."""
        store = {'users': {'kid': _row('hash', False, False)}}
        with mock.patch.object(auth, 'is_wheel_member', return_value=False):
            with mock.patch.object(auth, 'is_system_user', return_value=True):
                self.assertIsNone(auth.classify_admin('kid', store))

    def test_store_saved_0600(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'auth.json')
        auth.save_store(auth.blank_store(), path)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), '0o600')


# ---------------------------------------------------------------------------
# Ownership across the atomic replace.
#
# Regression (live, 2026-10-14): the root-only recovery CLI rewrote the store
# and left /etc/parental-controls/auth.json as root:root 0600. The service
# (user `parental-controls`) then could not read it and answered *every*
# request with HTTP 500 "Authentication unavailable" -- nobody could log in
# until the file was chowned back by hand.
# ---------------------------------------------------------------------------

# Unprivileged account borrowed by the chown-based tests. `nobody` exists on
# the target box and in the container and owns nothing of ours: only files
# inside a test-private temp dir are ever re-owned.
_FOREIGN_UID = 65534
_FOREIGN_GID = 65534


def _owner_of(path):
    st = os.stat(path)
    return (st.st_uid, st.st_gid)


class TestStoreOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'auth.json')

    def test_existing_owner_is_kept(self):
        """The atomic replace must not change who owns the file."""
        auth.save_store(auth.blank_store(), self.path)
        before = _owner_of(self.path)
        auth.save_store({'users': {}}, self.path)
        self.assertEqual(_owner_of(self.path), before)
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), '0o600')

    def test_failed_chown_warns_but_never_loses_the_write(self):
        """Best effort: a chown we cannot make must not block the write (the
        recovery CLI is the escape hatch), but it must not be silent either
        -- a store the service cannot read fails closed on every request."""
        err = io.StringIO()
        with mock.patch.object(auth.os, 'chown',
                               side_effect=PermissionError(1, 'not permitted')):
            with redirect_stderr(err):
                auth.save_store(auth.blank_store(), self.path,
                                owner=(_FOREIGN_UID, _FOREIGN_GID))
        with open(self.path) as f:
            self.assertEqual(json.load(f), {'users': {}})
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), '0o600')
        self.assertIn('AUTH WARNING', err.getvalue())
        self.assertIn(self.path, err.getvalue())

    @unittest.skipUnless(os.geteuid() == 0, 'needs root to chown')
    def test_foreign_owner_survives_the_replace(self):
        """A store already owned by someone else keeps that owner."""
        auth.save_store(auth.blank_store(), self.path)
        os.chown(self.path, _FOREIGN_UID, _FOREIGN_GID)
        auth.save_store({'users': {'boss': _row('h')}}, self.path)
        self.assertEqual(_owner_of(self.path), (_FOREIGN_UID, _FOREIGN_GID))

    @unittest.skipUnless(os.geteuid() == 0, 'needs root to chown')
    def test_new_store_inherits_state_dir_owner(self):
        """A root-created store (first `add-admin` on a fresh box) lands
        owned by the state directory's owner -- the service user, as
        install.sh chowns /etc/parental-controls -- not by root."""
        os.chown(self.tmp.name, _FOREIGN_UID, _FOREIGN_GID)
        auth.save_store(auth.blank_store(), self.path)
        self.assertEqual(_owner_of(self.path), (_FOREIGN_UID, _FOREIGN_GID))
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), '0o600')

    @unittest.skipUnless(os.geteuid() == 0, 'needs root to chown')
    def test_explicit_owner_wins(self):
        """A caller that knows the intended owner (the root-only CLI) can
        hand the store over instead of preserving a mis-owner."""
        auth.save_store(auth.blank_store(), self.path)
        auth.save_store(auth.blank_store(), self.path,
                        owner=(_FOREIGN_UID, _FOREIGN_GID))
        self.assertEqual(_owner_of(self.path), (_FOREIGN_UID, _FOREIGN_GID))

    @unittest.skipUnless(os.geteuid() == 0, 'needs root to chown')
    def test_root_owned_state_dir_does_not_guess(self):
        """No install.sh ownership migration: don't invent an owner."""
        auth.save_store(auth.blank_store(), self.path)
        self.assertEqual(_owner_of(self.path), (0, 0))
        os.chown(self.path, _FOREIGN_UID, _FOREIGN_GID)
        auth.save_store(auth.blank_store(), self.path)
        self.assertEqual(_owner_of(self.path), (_FOREIGN_UID, _FOREIGN_GID))


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth_path = os.path.join(self.tmp.name, 'auth.json')
        os.environ['PARENTAL_CONTROLS_AUTH'] = self.auth_path
        self.addCleanup(os.environ.pop, 'PARENTAL_CONTROLS_AUTH', None)
        import cli
        self.cli = cli
        store = {'users': {
            'boss': _row(auth.hash_password('Boss-Test-Pass-123')),
            'helper': _row(auth.hash_password('Helper-Test-Pass-12')),
        }}
        auth.save_store(store, self.auth_path)

    def test_remove_wheel_refused(self):
        with mock.patch.object(auth, 'is_wheel_member',
                               side_effect=lambda u: u == 'boss'):
            with self.assertRaises(SystemExit) as ctx:
                self.cli.cmd_remove_admin(argparse_ns('boss'))
            self.assertNotEqual(ctx.exception.code, 0)
        store = auth.load_store(self.auth_path)
        self.assertIn('boss', store['users'])

    def test_lock_wheel_refused(self):
        with mock.patch.object(auth, 'is_wheel_member',
                               side_effect=lambda u: u == 'boss'):
            with self.assertRaises(SystemExit):
                self.cli.cmd_lock_admin(argparse_ns('boss', field='admin'))

    def test_remove_app_only_ok(self):
        with mock.patch.object(auth, 'is_wheel_member', return_value=False):
            with mock.patch.object(auth, 'is_system_user', return_value=False):
                self.cli.cmd_remove_admin(argparse_ns('helper'))
        store = auth.load_store(self.auth_path)
        self.assertNotIn('helper', store['users'])

    def test_reset_password_mints_must_change(self):
        with mock.patch.object(auth, 'is_wheel_member', return_value=True):
            with mock.patch.object(auth, 'is_system_user', return_value=True):
                self.cli.cmd_reset_password(argparse_ns('boss', field='admin'))
        pw = _last_printed_password(self)  # resets once more, returns latest
        store = auth.load_store(self.auth_path)
        self.assertTrue(store['users']['boss']['must_change'])
        self.assertTrue(auth.verify_password(
            store['users']['boss']['hash'], pw))

    def test_add_system_nonwheel_refused(self):
        with mock.patch.object(auth, 'is_wheel_member', return_value=False):
            with mock.patch.object(auth, 'is_system_user', return_value=True):
                with self.assertRaises(SystemExit):
                    self.cli.cmd_add_admin(argparse_ns('kid'))

    def test_add_admin_bootstraps_missing_store(self):
        import io
        from contextlib import redirect_stdout
        os.unlink(self.auth_path)
        with mock.patch.object(auth, 'is_wheel_member', return_value=False):
            with mock.patch.object(auth, 'is_system_user', return_value=False):
                with redirect_stdout(io.StringIO()):
                    self.cli.cmd_add_admin(argparse_ns('fresh'))
        store = auth.load_store(self.auth_path)
        self.assertIn('fresh', store['users'])
        self.assertTrue(store['users']['fresh']['must_change'])

    def test_add_admin_corrupt_store_never_overwritten(self):
        with open(self.auth_path, 'w') as f:
            f.write('garbage')
        with mock.patch.object(auth, 'is_wheel_member', return_value=False):
            with mock.patch.object(auth, 'is_system_user', return_value=False):
                with self.assertRaises(SystemExit):
                    self.cli.cmd_add_admin(argparse_ns('fresh'))
        with open(self.auth_path) as f:
            self.assertEqual(f.read(), 'garbage')


class TestCliStoreOwnership(unittest.TestCase):
    """The root-only recovery CLI must leave the store readable by the
    service user even when it finds it mis-owned (2026-10-14 regression)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'auth.json')
        os.environ['PARENTAL_CONTROLS_AUTH'] = self.path
        self.addCleanup(os.environ.pop, 'PARENTAL_CONTROLS_AUTH', None)
        # Keep the audit chain out of the checkout.
        os.environ['PARENTAL_CONTROLS_AUDIT'] = os.path.join(
            self.tmp.name, 'audit.jsonl')
        self.addCleanup(os.environ.pop, 'PARENTAL_CONTROLS_AUDIT', None)
        import cli
        self.cli = cli

    @unittest.skipUnless(os.geteuid() == 0, 'needs root to chown')
    def test_cli_repairs_a_root_owned_store(self):
        """Replay the outage: the state dir belongs to the service user and
        the store is the root-owned wreckage of an earlier CLI run. After
        the CLI writes again the store must belong to the service user."""
        store = {'users': {
            'boss': _row(auth.hash_password('Boss-Test-Pass-123'))}}
        auth.save_store(store, self.path)
        os.chown(self.tmp.name, _FOREIGN_UID, _FOREIGN_GID)
        os.chown(self.path, 0, 0)
        with mock.patch.object(auth, 'is_wheel_member', return_value=False):
            self.cli.cmd_lock_admin(argparse_ns('boss', field='admin'))
        self.assertEqual(_owner_of(self.path), (_FOREIGN_UID, _FOREIGN_GID))
        self.assertTrue(auth.load_store(self.path)['users']['boss']['locked'])
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), '0o600')

    @unittest.skipUnless(os.geteuid() == 0, 'needs root to chown')
    def test_cli_keeps_owner_when_state_dir_is_root_owned(self):
        """A root-owned state dir means install.sh's ownership migration
        never ran, so the service user cannot be derived from it: the CLI
        must keep the store's working owner, not force root onto it."""
        store = {'users': {
            'boss': _row(auth.hash_password('Boss-Test-Pass-123'))}}
        auth.save_store(store, self.path)
        os.chown(self.path, _FOREIGN_UID, _FOREIGN_GID)
        with mock.patch.object(auth, 'is_wheel_member', return_value=False):
            self.cli.cmd_lock_admin(argparse_ns('boss', field='admin'))
        self.assertEqual(_owner_of(self.path), (_FOREIGN_UID, _FOREIGN_GID))


class _NS:
    def __init__(self, value, field='username'):
        setattr(self, field, value)


def argparse_ns(value, field='username'):
    return _NS(value, field)


def _last_printed_password(test):
    # reset-password prints the generated secret; capture it instead.
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with mock.patch.object(auth, 'is_wheel_member', return_value=True):
        with mock.patch.object(auth, 'is_system_user', return_value=True):
            with redirect_stdout(buf):
                test.cli.cmd_reset_password(argparse_ns('boss', field='admin'))
    line = [l for l in buf.getvalue().splitlines() if 'password for' in l][0]
    return line.split(': ', 1)[1].strip()


if __name__ == '__main__':
    unittest.main()
