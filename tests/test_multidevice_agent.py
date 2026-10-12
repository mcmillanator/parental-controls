#!/usr/bin/env python3
"""Phase-1 multi-device: agent (managed box) tests.

MULTIDEVICE_PLAN.md §4.4/§4.5. Every test mocks subprocess and the local
user/lock readers — no live account is touched, no command runs, no port is
bound beyond ephemeral loopback in the one HTTP integration test.

The cross-box wheel-refusal suite is the heart of Phase 1: the agent must
refuse to lock/freeze a wheel member of ITS OWN box even when the caller
claims to be a wheel admin on the hub.
"""

import io
import json
import os
import tempfile
import unittest
import urllib.parse
import urllib.request
from unittest import mock

import agent
import audit
import auth
import budgets
import device_policy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = os.path.join(ROOT, '.scratch')

KID = 'kid'
ADMIN = 'busyadmin'


def _env(path, method='GET', body=b'', headers=None, query=''):
    environ = {
        'REQUEST_METHOD': method,
        'PATH_INFO': path,
        'QUERY_STRING': query,
        'CONTENT_LENGTH': str(len(body)),
        'wsgi.input': io.BytesIO(body),
        'wsgi.url_scheme': 'http',
        'SERVER_NAME': 'test',
        'SERVER_PORT': '0',
    }
    environ.update(headers or {})
    return environ


def call(app, path, method='GET', body=b'', headers=None, query=''):
    out = {}

    def start_response(status, hdrs):
        out['status'] = status
        out['headers'] = dict(hdrs)

    chunks = app(_env(path, method, body, headers, query), start_response)
    payload = json.loads(b''.join(chunks))
    out['payload'] = payload
    out['code'] = int(out['status'].split()[0])
    return out


def auth_header(token):
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


def form(fields):
    return urllib.parse.urlencode(fields).encode('utf-8')


class BaseAgentTest(unittest.TestCase):
    TOKEN = 'unit-test-token-0123456789abcdef'

    def setUp(self):
        os.makedirs(SCRATCH, exist_ok=True)
        self.tmp = tempfile.mkdtemp(dir=SCRATCH)
        os.environ['PARENTAL_CONTROLS_AUDIT'] = os.path.join(self.tmp,
                                                             'audit.jsonl')
        self.app = agent.AgentApp(token_loader=lambda: self.TOKEN)

    def tearDown(self):
        os.environ.pop('PARENTAL_CONTROLS_AUDIT', None)


# ---------------------------------------------------------------------------
# Token auth
# ---------------------------------------------------------------------------

class TestTokenAuth(BaseAgentTest):
    def test_health_requires_no_token(self):
        res = call(self.app, '/api/v1/health')
        self.assertEqual(res['code'], 200)
        self.assertTrue(res['payload']['success'])
        self.assertEqual(res['payload']['agent'], agent.AGENT_NAME)

    def test_missing_token_is_401(self):
        res = call(self.app, '/api/v1/users')
        self.assertEqual(res['code'], 401)
        self.assertFalse(res['payload']['success'])

    def test_wrong_token_is_401(self):
        res = call(self.app, '/api/v1/users',
                   headers=auth_header('not-the-token'))
        self.assertEqual(res['code'], 401)

    def test_malformed_authorization_is_401(self):
        res = call(self.app, '/api/v1/users',
                   headers={'HTTP_AUTHORIZATION': self.TOKEN})
        self.assertEqual(res['code'], 401)

    def test_correct_token_allows(self):
        with mock.patch.object(agent, 'get_human_users', return_value=[]), \
             mock.patch.object(agent, 'load_bedtime', return_value={}):
            res = call(self.app, '/api/v1/users',
                       headers=auth_header(self.TOKEN))
        self.assertEqual(res['code'], 200)
        self.assertEqual(res['payload'], [])

    def test_rotated_token_is_401(self):
        old = agent.AgentApp(token_loader=lambda: 'rotated-new-token-xyz')
        res = call(old, '/api/v1/users', headers=auth_header(self.TOKEN))
        self.assertEqual(res['code'], 401)

    def test_token_ok_constant_time_semantics(self):
        self.assertTrue(agent.token_ok('abc', 'abc'))
        self.assertFalse(agent.token_ok('abc', 'abd'))
        self.assertFalse(agent.token_ok('', 'abc'))
        self.assertFalse(agent.token_ok('abc', ''))
        self.assertFalse(agent.token_ok('abc', None))

    def test_token_ok_uses_compare_digest(self):
        with mock.patch.object(agent.secrets, 'compare_digest',
                               return_value=True) as cmp:
            self.assertTrue(agent.token_ok('a', 'b'))
        cmp.assert_called_once()

    def test_missing_provisioned_token_is_503(self):
        unprovisioned = agent.AgentApp(token_loader=lambda: None)
        res = call(unprovisioned, '/api/v1/users',
                   headers=auth_header(self.TOKEN))
        self.assertEqual(res['code'], 503)
        # Health still works so the hub can see the box is up but unregistered.
        self.assertEqual(call(unprovisioned, '/api/v1/health')['code'], 200)

    def test_load_token_env_override(self):
        old = os.environ.get(agent.TOKEN_ENV)
        os.environ[agent.TOKEN_ENV] = 'env-token'
        try:
            self.assertEqual(agent.load_token(), 'env-token')
        finally:
            if old is None:
                os.environ.pop(agent.TOKEN_ENV, None)
            else:
                os.environ[agent.TOKEN_ENV] = old

    def test_load_token_file(self):
        path = os.path.join(self.tmp, 'agent_token')
        with open(path, 'w') as fh:
            fh.write('file-token\n')
        self.assertEqual(agent.load_token(path), 'file-token')

    def test_missing_token_file_is_none(self):
        self.assertIsNone(agent.load_token(os.path.join(self.tmp, 'nope')))

    def test_authorization_header_is_redacted(self):
        headers = agent.redacted_headers(
            {'HTTP_AUTHORIZATION': f'Bearer {self.TOKEN}',
             'HTTP_X_PANEL_ACTOR': 'bosshub'})
        self.assertEqual(headers['Authorization'], '<redacted>')
        self.assertEqual(headers['X-Panel-Actor'], 'bosshub')

    def test_token_never_appears_in_error_payloads(self):
        res = call(self.app, '/api/v1/users',
                   headers=auth_header('wrong-' + self.TOKEN))
        self.assertNotIn(self.TOKEN, json.dumps(res['payload']))


# ---------------------------------------------------------------------------
# Actor / header hygiene
# ---------------------------------------------------------------------------

class TestRemoteActor(BaseAgentTest):
    def test_actor_format_with_panel(self):
        actor = agent.remote_actor({'HTTP_X_PANEL_ACTOR': 'boss',
                                    'HTTP_X_PANEL_ID': 'hubA'})
        self.assertEqual(actor, 'remote:boss@hubA')

    def test_actor_format_without_panel(self):
        self.assertEqual(agent.remote_actor({}), 'remote:unknown')

    def test_actor_is_sanitized(self):
        actor = agent.remote_actor(
            {'HTTP_X_PANEL_ACTOR': 'bo ss\n<x>\t'})
        self.assertNotIn('\n', actor)
        self.assertTrue(actor.startswith('remote:'))

    def test_actor_cannot_override_wheel_refusal(self):
        # A hub admin claiming wheel on the hub is still refused for THIS
        # box's wheel members (actor is audit-only, never authorization).
        with mock.patch.object(auth, 'is_wheel_member', return_value=True):
            res = call(self.app, '/api/v1/lock',
                       method='POST', body=form({'username': ADMIN}),
                       headers={**auth_header(self.TOKEN),
                                'HTTP_X_PANEL_ACTOR': 'wheelboss',
                                'HTTP_X_PANEL_ID': 'hubA'})
        self.assertEqual(res['code'], 403)


# ---------------------------------------------------------------------------
# Verbs (subprocess mocked)
# ---------------------------------------------------------------------------

class TestAgentVerbs(BaseAgentTest):
    def _post(self, path, fields, **headers):
        return call(self.app, path, method='POST', body=form(fields),
                    headers={**auth_header(self.TOKEN), **headers})

    def test_lock_runs_usermod_and_audits_remote(self):
        with mock.patch.object(agent, 'run_lock_command',
                               return_value=(True, 'OK (1 session(s) locked)')) \
                as run, \
             mock.patch.object(agent, 'note_manual_lock') as note:
            res = self._post('/api/v1/lock', {'username': KID},
                             HTTP_X_PANEL_ACTOR='boss',
                             HTTP_X_PANEL_ID='hubA')
        self.assertEqual(res['code'], 200)
        self.assertTrue(res['payload']['success'])
        run.assert_called_once()
        self.assertEqual(run.call_args[0][1], 'lock')
        # The hub actor travels into the local audit trail, not authz.
        self.assertEqual(run.call_args[1]['actor'], 'remote:boss@hubA')
        note.assert_called_once()

    def test_lock_screen_route(self):
        with mock.patch.object(agent, 'run_lock_command',
                               return_value=(True, 'OK (no active session)')), \
             mock.patch.object(agent, 'note_manual_lock'):
            res = self._post('/api/v1/lock-screen', {'username': KID})
        self.assertEqual(res['code'], 200)

    def test_lock_invalid_username_400(self):
        res = self._post('/api/v1/lock', {'username': '-x'})
        self.assertEqual(res['code'], 400)

    def test_lock_wheel_refused(self):
        with mock.patch.object(auth, 'is_wheel_member', return_value=True), \
             mock.patch.object(agent, 'run_lock_command') as run:
            res = self._post('/api/v1/lock', {'username': ADMIN})
        self.assertEqual(res['code'], 403)
        run.assert_not_called()

    def test_lock_failure_400(self):
        with mock.patch.object(agent, 'run_lock_command',
                               return_value=(False, 'sudo usermod -L: nope')):
            res = self._post('/api/v1/lock', {'username': KID})
        self.assertEqual(res['code'], 400)

    def test_unlock_runs_usermod(self):
        with mock.patch.object(agent, 'run_lock_command',
                               return_value=(True, 'OK')) as run, \
             mock.patch.object(agent, 'note_manual_unlock') as note:
            res = self._post('/api/v1/unlock', {'username': KID})
        self.assertEqual(res['code'], 200)
        self.assertEqual(run.call_args[0][1], 'unlock')
        note.assert_called_once()

    def test_logout_locks_and_terminates(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch.object(agent.subprocess, 'run', side_effect=fake_run), \
             mock.patch.object(agent, 'note_manual_lock'):
            res = self._post('/api/v1/logout', {'username': KID})
        self.assertEqual(res['code'], 200)
        self.assertIn(['sudo', 'usermod', '-L', KID], calls)
        self.assertIn(['sudo', 'loginctl', 'terminate-user', KID], calls)

    def test_logout_refuses_wheel(self):
        with mock.patch.object(auth, 'is_wheel_member', return_value=True), \
             mock.patch.object(agent.subprocess, 'run') as run:
            res = self._post('/api/v1/logout', {'username': ADMIN})
        self.assertEqual(res['code'], 403)
        run.assert_not_called()

    def test_bonus_grant_with_schedule(self):
        entry = {'start': '21:00', 'end': '08:00', 'days': [0]}
        with mock.patch.object(agent, 'load_bedtime',
                               return_value={KID: dict(entry)}), \
             mock.patch.object(agent, 'save_bedtime'), \
             mock.patch.object(agent, 'run_lock_command',
                               return_value=(True, 'OK')), \
             mock.patch.object(agent, '_safe_notify'):
            res = self._post('/api/v1/bonus', {'username': KID,
                                               'minutes': '30'})
        self.assertEqual(res['code'], 200)
        self.assertTrue(res['payload']['success'])
        self.assertIn('bonus_until', res['payload'])

    def test_bonus_grant_without_schedule_400(self):
        with mock.patch.object(agent, 'load_bedtime', return_value={}):
            res = self._post('/api/v1/bonus', {'username': KID,
                                               'minutes': '30'})
        self.assertEqual(res['code'], 400)

    def test_bonus_grant_below_minimum_400(self):
        res = self._post('/api/v1/bonus', {'username': KID, 'minutes': '1'})
        self.assertEqual(res['code'], 400)

    def test_bonus_revoke_without_active_400(self):
        with mock.patch.object(agent, 'load_bedtime', return_value={}):
            res = self._post('/api/v1/bonus/revoke', {'username': KID})
        self.assertEqual(res['code'], 400)

    def test_bedtime_set_saves_and_audits(self):
        saved = {}
        with mock.patch.object(agent, 'load_bedtime', return_value={}), \
             mock.patch.object(agent, 'save_bedtime',
                               side_effect=lambda d: saved.update(d)), \
             mock.patch.object(agent, 'is_in_bedtime', return_value=False), \
             mock.patch.object(agent.audit, 'safe_log'):
            res = self._post('/api/v1/bedtime',
                             {'username': KID, 'start': '21:00',
                              'end': '08:00', 'all_days': 'on'})
        self.assertEqual(res['code'], 200)
        self.assertIn(KID, saved)

    def test_bedtime_invalid_time_400(self):
        res = self._post('/api/v1/bedtime',
                         {'username': KID, 'start': '99:99',
                          'end': '08:00', 'all_days': 'on'})
        self.assertEqual(res['code'], 400)

    def test_bedtime_clear(self):
        with mock.patch.object(agent, 'load_bedtime',
                               return_value={KID: {'start': '21:00'}}), \
             mock.patch.object(agent, 'save_bedtime') as save:
            res = self._post('/api/v1/bedtime',
                             {'username': KID, 'clear': '1'})
        self.assertEqual(res['code'], 200)
        save.assert_called_once()

    def test_process_prefs_delegates_to_budgets(self):
        with mock.patch.object(agent.budgets, 'set_pref',
                               return_value=(True, 'hidden')) as sp:
            res = self._post('/api/v1/process-prefs',
                             {'username': KID, 'comm': 'xterm',
                              'action': 'hide'})
        self.assertEqual(res['code'], 200)
        sp.assert_called_once_with(KID, 'xterm', 'hide')

    def test_freeze_delegates_to_budgets(self):
        with mock.patch.object(budgets, 'freeze_category',
                               return_value=(True, 'frozen')) as fz:
            res = self._post('/api/v1/budgets/freeze',
                             {'username': KID, 'category': 'games'})
        self.assertEqual(res['code'], 200)
        fz.assert_called_once()

    def test_freeze_unknown_category_400(self):
        res = self._post('/api/v1/budgets/freeze',
                         {'username': KID, 'category': 'bogus'})
        self.assertEqual(res['code'], 400)

    def test_unfreeze_delegates_to_budgets(self):
        with mock.patch.object(budgets, 'unfreeze_category',
                               return_value=(True, 'thawed')) as uf:
            res = self._post('/api/v1/budgets/unfreeze',
                             {'username': KID, 'category': 'games'})
        self.assertEqual(res['code'], 200)
        uf.assert_called_once()


# ---------------------------------------------------------------------------
# The critical one: cross-box wheel / admin refusal
# ---------------------------------------------------------------------------

class TestCrossBoxRefusal(BaseAgentTest):
    def test_agent_refuses_own_box_wheel_member_even_for_hub_wheel_admin(self):
        with mock.patch.object(auth, 'is_wheel_member',
                               return_value=True), \
             mock.patch.object(agent, 'run_lock_command') as run:
            res = call(self.app, '/api/v1/lock', method='POST',
                       body=form({'username': ADMIN}),
                       headers={**auth_header(self.TOKEN),
                                'HTTP_X_PANEL_ACTOR': 'hubwheeladmin'})
        self.assertEqual(res['code'], 403)
        run.assert_not_called()

    def test_agent_allows_non_wheel_lock_regardless_of_actor(self):
        with mock.patch.object(auth, 'is_wheel_member', return_value=False), \
             mock.patch.object(agent, 'run_lock_command',
                               return_value=(True, 'OK (none)')), \
             mock.patch.object(agent, 'note_manual_lock'):
            res = call(self.app, '/api/v1/lock', method='POST',
                       body=form({'username': KID}),
                       headers={**auth_header(self.TOKEN),
                                'HTTP_X_PANEL_ACTOR': 'hubwheeladmin'})
        self.assertEqual(res['code'], 200)

    def test_agent_refuses_freeze_of_admin_protected(self):
        with mock.patch.object(auth, 'is_wheel_member', return_value=False), \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=True), \
             mock.patch.object(budgets, 'freeze_category') as fz:
            res = call(self.app, '/api/v1/budgets/freeze', method='POST',
                       body=form({'username': ADMIN, 'category': 'games'}),
                       headers=auth_header(self.TOKEN))
        self.assertEqual(res['code'], 403)
        fz.assert_not_called()

    def test_policy_uses_same_wheel_source_as_panel(self):
        import app as panel
        with mock.patch.object(auth, 'is_wheel_member', return_value=True), \
                panel.app.test_request_context():
            self.assertIsNotNone(device_policy.wheel_refusal(ADMIN))
            refused = panel._wheel_target_refused(ADMIN)
            self.assertIsNotNone(refused)
            self.assertEqual(refused[1], 403)

    def test_freeze_refusal_message_matches_panel(self):
        with mock.patch.object(auth, 'is_wheel_member', return_value=False), \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=True):
            self.assertEqual(
                device_policy.freeze_refusal(ADMIN),
                f'Refused: {ADMIN} is an admin and cannot be frozen')

    def test_wheel_refusal_message_matches_panel(self):
        with mock.patch.object(auth, 'is_wheel_member', return_value=True):
            self.assertEqual(
                device_policy.wheel_refusal(ADMIN),
                f'Refused: {ADMIN} is a wheel admin and can never be '
                'locked, terminated, or removed.')


# ---------------------------------------------------------------------------
# Reads + routing
# ---------------------------------------------------------------------------

class TestAgentReads(BaseAgentTest):
    def test_read_users_shape(self):
        users = [{'username': KID, 'uid': 1001, 'home': '/home/kid',
                  'shell': '/bin/bash'}]
        with mock.patch.object(agent, 'get_human_users', return_value=users), \
             mock.patch.object(agent, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(agent, 'load_bedtime', return_value={}), \
             mock.patch.object(agent, 'is_in_bedtime', return_value=False):
            res = call(self.app, '/api/v1/users',
                       headers=auth_header(self.TOKEN))
        self.assertEqual(res['code'], 200)
        self.assertEqual(res['payload'][0]['username'], KID)
        self.assertFalse(res['payload'][0]['locked'])

    def test_read_audit_reports_chain_ok(self):
        with mock.patch.object(agent.audit, 'read_entries',
                               return_value=[{'seq': 1}]), \
             mock.patch.object(agent.audit, 'verify_chain',
                               return_value=(True, None)):
            res = call(self.app, '/api/v1/audit', query='limit=5',
                       headers=auth_header(self.TOKEN))
        self.assertEqual(res['code'], 200)
        self.assertTrue(res['payload']['chain_ok'])
        self.assertEqual(res['payload']['limit'], 5)

    def test_unknown_path_404(self):
        res = call(self.app, '/api/v1/nope', headers=auth_header(self.TOKEN))
        self.assertEqual(res['code'], 404)

    def test_outside_prefix_404(self):
        res = call(self.app, '/admin/audit', headers=auth_header(self.TOKEN))
        self.assertEqual(res['code'], 404)

    def test_wrong_method_405(self):
        res = call(self.app, '/api/v1/lock', headers=auth_header(self.TOKEN))
        self.assertEqual(res['code'], 405)

    def test_json_body_is_parsed(self):
        body = json.dumps({'username': KID}).encode()
        with mock.patch.object(agent, 'run_lock_command',
                               return_value=(True, 'OK (none)')), \
             mock.patch.object(agent, 'note_manual_lock'):
            res = call(self.app, '/api/v1/lock', method='POST', body=body,
                       headers={**auth_header(self.TOKEN),
                                'CONTENT_TYPE': 'application/json'})
        self.assertEqual(res['code'], 200)


# ---------------------------------------------------------------------------
# Real HTTP over ephemeral loopback (self-contained integration)
# ---------------------------------------------------------------------------

class TestAgentOverHTTP(unittest.TestCase):
    TOKEN = 'integration-token-abcdef0123456789'

    @classmethod
    def setUpClass(cls):
        from wsgiref.simple_server import make_server
        cls.server = make_server(
            '127.0.0.1', 0, agent.AgentApp(token_loader=lambda: cls.TOKEN))
        cls.port = cls.server.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def _get(self, path, token=None):
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}')
        if token:
            req.add_header('Authorization', f'Bearer {token}')
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def test_health_over_http(self):
        status, payload = self._get('/api/v1/health')
        self.assertEqual(status, 200)
        self.assertTrue(payload['success'])

    def test_users_requires_token_over_http(self):
        status, _ = self._get('/api/v1/users')
        self.assertEqual(status, 401)

    def test_users_with_token_over_http(self):
        with mock.patch.object(agent, 'get_human_users', return_value=[]), \
             mock.patch.object(agent, 'load_bedtime', return_value={}):
            status, payload = self._get('/api/v1/users', token=self.TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload, [])


if __name__ == '__main__':
    unittest.main()
