#!/usr/bin/env python3
"""Tests for the admin console: audit-log viewer page + admin management.

Covers:
- Auth gating: anonymous 401/302, CSRF 403 on the mutating endpoints.
- Add admin: one-time generated password + must_change row; wheel targets,
  non-wheel actors, non-admin system accounts and duplicates all refused.
- Remove admin: deletes the row, refuses the (self-)removal of the last
  wheel admin and any wheel target, 404 on an unknown admin.
- Audit page: newest-first rows, chain status (intact/broken + first bad
  seq), limit/cap/load-more state, no delete controls, empty-log render.
- Shared mutations: the CLI still works through adminstore with identical
  behavior (CLI may still mint a wheel row; the web console refuses).
- The existing GET /api/audit contract is preserved.

Conventions mirror tests/test_audit.py + test_lock_regression.py: stdlib
only, temp auth/audit stores, mocked wheel/system membership. These tests
never touch a real account, never run usermod/loginctl, and never write
outside temp stores.
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import adminstore
import app
import audit
import auth
import cli
from test_lock_regression import _AdminSessionMixin, KID


class _AuditEnvMixin:
    """Point the audit log at a temp file for each test."""

    def setUp(self):
        super().setUp()
        self._audit_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._audit_tmp.cleanup)
        self._audit_path = os.path.join(self._audit_tmp.name, 'audit.jsonl')
        os.environ['PARENTAL_CONTROLS_AUDIT'] = self._audit_path
        self.addCleanup(lambda: os.environ.pop('PARENTAL_CONTROLS_AUDIT',
                                               None))


class _AdminConsoleBase(_AdminSessionMixin, _AuditEnvMixin,
                        unittest.TestCase):
    """Logged-in wheel admin (qaadmin) + temp auth/audit stores."""

    APP_ONLY_USER = 'apponly'
    APP_ONLY_PW = 'App-Only-Test-Pass-456'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._apponly_hash = auth.hash_password(cls.APP_ONLY_PW)

    def _add_row(self, username, must_change=False, locked=False):
        """Insert a store row directly (no API round-trip)."""
        store = auth.load_store()
        store['users'][username] = {'hash': self._apponly_hash,
                                    'must_change': must_change,
                                    'locked': locked}
        auth.save_store(store)

    def _add_admin_via_api(self, username):
        return self.authed_post('/api/admins', data={'username': username})

    def _remove_admin_via_api(self, username):
        return self.authed_post('/api/admins/remove',
                                data={'username': username})

    def _login_app_only(self):
        """Return CSRF headers for a fresh app-only admin session."""
        self._add_row(self.APP_ONLY_USER)
        resp = self.client.post('/login',
                                data={'username': self.APP_ONLY_USER,
                                      'password': self.APP_ONLY_PW})
        self.assertEqual(resp.status_code, 302,
                         f'app-only login failed: {resp.status_code}')
        sid = resp.headers.get('Set-Cookie', '').split('pc_sid=')[1].split(
            ';')[0]
        return {'X-CSRF-Token': auth._sessions[sid]['csrf']}

    # -- helpers ----------------------------------------------------------

    def get_page(self, query=''):
        return self.client.get('/admin/audit' + query)

    def page_text(self, query=''):
        return self.get_page(query).get_data(as_text=True)

    def _row_slice(self, text, username):
        start = text.index(f'data-username="{username}"')
        return text[start:text.index('</tr>', start)]


class TestConsoleGate(_AdminConsoleBase):
    def test_anon_cannot_read_page(self):
        anon = app.app.test_client()
        resp = anon.get('/admin/audit')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_anon_api_401(self):
        anon = app.app.test_client()
        self.assertEqual(anon.get('/api/audit').status_code, 401)
        self.assertEqual(anon.post('/api/admins',
                                   data={'username': 'helper'}).status_code,
                         401)
        self.assertEqual(anon.post('/api/admins/remove',
                                   data={'username': 'helper'}).status_code,
                         401)

    def test_mutating_endpoints_require_csrf(self):
        # Session cookie present (mixin logged in), but no CSRF token.
        resp = self.client.post('/api/admins', data={'username': 'helper'})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post('/api/admins/remove',
                                data={'username': 'helper'})
        self.assertEqual(resp.status_code, 403)


class TestAddAdmin(_AdminConsoleBase):
    def test_add_generates_onetime_password_and_must_change(self):
        resp = self._add_admin_via_api('helper')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertTrue(len(payload['password']) >= 12)
        row = auth.load_store()['users']['helper']
        self.assertTrue(row['must_change'])
        self.assertFalse(row['locked'])
        self.assertTrue(auth.verify_password(row['hash'],
                                             payload['password']))
        # Audited with the web actor.
        self.assertIn(('qaadmin', 'admin.add', 'helper'),
                      [(e['actor'], e['action'], e['target'])
                       for e in audit.read_entries(limit=1000)])

    def test_add_refuses_wheel_target(self):
        # A wheel member WITHOUT a store row is refused outright: wheel
        # admins are never addable from the web console (bootstrap/CLI
        # territory only). A wheel member who already has a row gets the
        # earlier "already exists" 400 instead.
        with mock.patch.object(auth, 'is_wheel_member',
                               side_effect=lambda u: u in
                               (self.ADMIN_USER, 'boss')):
            resp = self._add_admin_via_api('boss')
        self.assertEqual(resp.status_code, 403)
        self.assertIn('wheel', resp.get_json()['error'].lower())
        self.assertNotIn('boss', auth.load_store()['users'])
        # Wheel self-add is blocked too (row untouched).
        resp = self._add_admin_via_api(self.ADMIN_USER)
        self.assertIn(resp.status_code, (400, 403))
        self.assertIn(self.ADMIN_USER, auth.load_store()['users'])

    def test_add_refuses_apponly_actor(self):
        headers = self._login_app_only()
        resp = self.client.post('/api/admins',
                                data={'username': 'helper'},
                                headers=headers)
        self.assertEqual(resp.status_code, 403)
        self.assertIn('Only wheel admins', resp.get_json()['error'])
        self.assertNotIn('helper', auth.load_store()['users'])

    def test_add_refuses_system_nonwheel(self):
        with mock.patch.object(auth, 'is_system_user',
                               side_effect=lambda u: u == 'localsys'):
            resp = self._add_admin_via_api('localsys')
        self.assertEqual(resp.status_code, 403)
        self.assertIn('system account', resp.get_json()['error'].lower())
        self.assertNotIn('localsys', auth.load_store()['users'])

    def test_add_duplicate_400(self):
        self._add_admin_via_api('helper')
        resp = self._add_admin_via_api('helper')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already exists', resp.get_json()['error'].lower())

    def test_add_invalid_username_400(self):
        resp = self._add_admin_via_api('bad name!')
        self.assertEqual(resp.status_code, 400)


class TestRemoveAdmin(_AdminConsoleBase):
    def test_remove_deletes_and_logs(self):
        self._add_admin_via_api('helper')
        resp = self._remove_admin_via_api('helper')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertNotIn('helper', auth.load_store()['users'])
        self.assertIn(('qaadmin', 'admin.remove', 'helper'),
                      [(e['actor'], e['action'], e['target'])
                       for e in audit.read_entries(limit=1000)])

    def test_remove_refuses_self_removal_of_last_wheel_admin(self):
        # qaadmin is the only (and last) wheel admin: self-removal refused.
        resp = self._remove_admin_via_api(self.ADMIN_USER)
        self.assertEqual(resp.status_code, 403)
        self.assertIn('wheel', resp.get_json()['error'].lower())
        self.assertIn(self.ADMIN_USER, auth.load_store()['users'])

    def test_remove_refuses_any_wheel_target(self):
        # A second wheel member with no store row is still unremovable.
        with mock.patch.object(auth, 'is_wheel_member',
                               side_effect=lambda u: u in
                               (self.ADMIN_USER, 'boss')):
            resp = self._remove_admin_via_api('boss')
        self.assertEqual(resp.status_code, 403)
        self.assertIn('wheel', resp.get_json()['error'].lower())

    def test_remove_missing_404(self):
        resp = self._remove_admin_via_api('ghost')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('no such admin', resp.get_json()['error'].lower())

    def test_remove_refuses_apponly_actor(self):
        self._add_admin_via_api('helper')
        headers = self._login_app_only()
        resp = self.client.post('/api/admins/remove',
                                data={'username': 'helper'},
                                headers=headers)
        self.assertEqual(resp.status_code, 403)
        self.assertIn('Only wheel admins', resp.get_json()['error'])
        self.assertIn('helper', auth.load_store()['users'])


class TestAuditConsolePage(_AdminConsoleBase):
    def test_page_renders_rows_newest_first_with_chain_status(self):
        audit.log('qaadmin', 'bedtime.set', KID)    # seq 1 (oldest)
        audit.log('qaadmin', 'account.unlock', KID)  # seq 2
        audit.log('qaadmin', 'account.lock', KID)    # seq 3 (newest)
        resp = self.get_page()
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        self.assertIn('Audit chain intact', text)
        # Newest first: seq 3 renders above seq 2 above seq 1.
        self.assertLess(text.index('account.lock'),
                        text.index('account.unlock'))
        self.assertLess(text.index('account.unlock'),
                        text.index('bedtime.set'))
        self.assertIn(KID, text)

    def test_page_limit_cap_and_load_more_state(self):
        for i in range(5):
            audit.log('qaadmin', f'action.{i}', KID, {'i': i})
        # limit=2 renders 2 rows and enables "load more". Count only
        # server-rendered rows (data-seq="<digits>">), not the JS builder.
        text = self.page_text('?limit=2')
        self.assertIn('entries (limit 2)', text)
        self.assertEqual(len(re.findall(r'data-seq="\d+">', text)), 2)
        idx = text.index('id="load-more"')
        self.assertNotIn('disabled', text[idx:idx + 120])
        # A huge limit caps at 1000 and disables the button (all loaded).
        text = self.page_text('?limit=5000')
        self.assertIn('entries (limit 1000)', text)
        self.assertEqual(len(re.findall(r'data-seq="\d+">', text)), 5)
        idx = text.index('id="load-more"')
        self.assertIn('disabled', text[idx:idx + 120])

    def test_page_reports_broken_chain_with_seq(self):
        audit.log('qaadmin', 'account.lock', KID)
        audit.log('qaadmin', 'account.unlock', KID)
        with open(self._audit_path) as f:
            lines = f.readlines()
        obj = json.loads(lines[0])
        obj['target'] = 'mallory'
        lines[0] = json.dumps(obj) + '\n'
        with open(self._audit_path, 'w') as f:
            f.write(''.join(lines))
        text = self.page_text()
        self.assertIn('Audit chain broken', text)
        self.assertIn('first bad entry: seq 1', text)

    def test_page_has_no_delete_controls(self):
        audit.log('qaadmin', 'account.lock', KID)
        text = self.page_text()
        audit_section = text.split('<section id="admin-section"')[0]
        self.assertNotIn('delete', audit_section.lower())
        self.assertNotIn('Delete', audit_section)
        # Removal controls (if any) belong to the admin section only.
        self.assertNotIn('removeAdmin', audit_section)

    def test_page_empty_audit_no_crash(self):
        resp = self.get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertIn('No audit entries yet.',
                      resp.get_data(as_text=True))

    def test_page_admin_rows_wheel_protected_apponly_removable(self):
        self._add_row('helper')
        text = self.page_text()
        qa_row = self._row_slice(text, self.ADMIN_USER)
        self.assertIn('Protected', qa_row)
        self.assertNotIn('Remove', qa_row)
        helper_row = self._row_slice(text, 'helper')
        self.assertIn('app-only', helper_row)
        self.assertIn('Remove', helper_row)

    def test_page_apponly_actor_sees_no_manage_controls(self):
        self._add_row('helper')
        headers = self._login_app_only()
        resp = self.client.get('/admin/audit', headers=headers)
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        # The add-form container and button are wheel-actor only. The
        # strings 'new-admin-username' / 'removeAdmin' / 'onclick="removeAdmin('
        # also appear in the inert JS function bodies, so assert on the
        # rendered *markup* only (everything before <script>).
        markup = text.split('<script>')[0]
        self.assertNotIn('id="admin-add"', markup)
        self.assertNotIn('>Add admin<', markup)
        self.assertNotIn('onclick="removeAdmin(', markup)
        qa_row = self._row_slice(text, self.ADMIN_USER)
        self.assertIn('Protected', qa_row)
        helper_row = self._row_slice(text, 'helper')
        self.assertNotIn('Remove', helper_row)


class TestApiAuditContract(_AdminConsoleBase):
    def test_api_audit_contract_unchanged(self):
        # The page must not weaken the existing /api/audit contract.
        audit.log('qaadmin', 'account.lock', KID)
        audit.log('qaadmin', 'account.unlock', KID)
        resp = self.client.get('/api/audit')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['chain_ok'])
        self.assertIsNone(payload['bad_seq'])
        self.assertEqual([e['seq'] for e in payload['entries']], [2, 1])
        self.assertEqual(resp.get_json()['limit'], 100)


class TestSharedMutations(_AdminConsoleBase):
    """CLI parity through adminstore (behavior historically identical)."""

    def _ns(self, value, field='username'):
        return type('NS', (), {field: value})()

    def test_cli_add_and_remove_flow(self):
        with redirect_stdout(io.StringIO()):
            cli.cmd_add_admin(self._ns('cliworker'))
        store = auth.load_store()
        self.assertIn('cliworker', store['users'])
        self.assertTrue(store['users']['cliworker']['must_change'])
        with redirect_stdout(io.StringIO()):
            cli.cmd_remove_admin(self._ns('cliworker'))
        self.assertNotIn('cliworker', auth.load_store()['users'])
        acts = [(e['actor'], e['action']) for e in audit.read_entries(
            limit=1000)]
        self.assertIn(('cli:add-admin', 'admin.add'), acts)
        self.assertIn(('cli:remove-admin', 'admin.remove'), acts)

    def test_cli_add_wheel_target_still_allowed(self):
        # The CLI keeps its historical ability to mint a wheel row, while
        # the web console refuses (see TestAddAdmin.test_add_refuses_...).
        buf = io.StringIO()
        with mock.patch.object(auth, 'is_wheel_member',
                               side_effect=lambda u: u in
                               (self.ADMIN_USER, 'boss')), \
             mock.patch.object(auth, 'is_system_user',
                               side_effect=lambda u: u in
                               (self.ADMIN_USER, 'boss')):
            with redirect_stdout(buf):
                cli.cmd_add_admin(self._ns('boss'))
        self.assertIn('tier: wheel', buf.getvalue())
        self.assertIn('boss', auth.load_store()['users'])

    def test_adminstore_remove_wheel_refused(self):
        with mock.patch.object(auth, 'is_wheel_member', return_value=True):
            with self.assertRaises(adminstore.RefusedError):
                adminstore.remove_admin(self.ADMIN_USER)


if __name__ == '__main__':
    unittest.main()