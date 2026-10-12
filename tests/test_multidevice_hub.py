#!/usr/bin/env python3
"""Phase-1 multi-device: hub-side tests.

Covers the devices.json store, the stdlib hub client (offline/timeout/
malformed/401), the hub device routes (session + CSRF on the browser side,
bearer token to the agent), and the feature gate proving single-box output
is byte-identical when devices.json is absent.

Stdlib only; the only network is an ephemeral loopback bind inside the
process. No live account or service is touched.
"""

import io
import json
import os
import socket
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

import agent
import audit
import budgets
import remote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = os.path.join(ROOT, '.scratch')

TOKEN = 'hub-test-token-0123456789abcdef'


class BaseHubTest(unittest.TestCase):
    def setUp(self):
        os.makedirs(SCRATCH, exist_ok=True)
        self.tmp = tempfile.mkdtemp(dir=SCRATCH)
        self.devices = os.path.join(self.tmp, 'devices.json')
        os.environ['PARENTAL_CONTROLS_DEVICES'] = self.devices
        os.environ['PARENTAL_CONTROLS_AUDIT'] = os.path.join(self.tmp,
                                                             'audit.jsonl')

    def tearDown(self):
        os.environ.pop('PARENTAL_CONTROLS_DEVICES', None)
        os.environ.pop('PARENTAL_CONTROLS_AUDIT', None)

    def _admin_patches(self):
        import app
        store = {'users': {'boss': {'hash': 'x', 'must_change': False,
                                    'locked': False}}}
        sess = {'username': 'boss', 'csrf': 'test-csrf', 'created': 0,
                'last_seen': 0}
        return (
            mock.patch.object(app.auth, 'get_session',
                              return_value=dict(sess)),
            mock.patch.object(app.auth, 'load_store', return_value=store),
            mock.patch.object(app.auth, 'is_wheel_member', return_value=True),
            mock.patch.object(app.auth, 'is_system_user', return_value=True),
        )

    def _admin_client(self):
        import app
        client = app.app.test_client()
        client.set_cookie('pc_sid', 'fake-test-session')
        return client


# ---------------------------------------------------------------------------
# devices.json store
# ---------------------------------------------------------------------------

class TestDevicesStore(BaseHubTest):
    def test_absent_is_single_box(self):
        data = remote.load_devices(self.devices)
        self.assertEqual(data['devices'], {})
        self.assertFalse(remote.devices_enabled(self.devices))

    def test_add_saves_and_is_0600(self):
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          hostname='kidsbox', path=self.devices)
        mode = os.stat(self.devices).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        entry = remote.get_device('boxb', self.devices)
        self.assertEqual(entry['url'], 'http://localhost:5001')
        self.assertEqual(entry['hostname'], 'kidsbox')
        self.assertTrue(remote.devices_enabled(self.devices))

    def test_add_then_load_roundtrip(self):
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        entries = remote.list_devices(self.devices)
        self.assertEqual([e['name'] for e in entries], ['boxb'])

    def test_add_refuses_bad_name(self):
        with self.assertRaises(remote.DevicesError):
            remote.add_device('../etc', 'http://localhost:5001', TOKEN,
                              path=self.devices)

    def test_add_refuses_short_token(self):
        with self.assertRaises(remote.DevicesError):
            remote.add_device('boxb', 'http://localhost:5001', 'short',
                              path=self.devices)

    def test_add_refuses_plain_http_lan(self):
        with self.assertRaises(remote.DevicesError):
            remote.add_device('boxb', 'http://10.0.0.5:5001', TOKEN,
                              path=self.devices)

    def test_add_allows_plain_http_lan_when_flagged(self):
        entry = remote.add_device('boxb', 'http://10.0.0.5:5001', TOKEN,
                                  allow_insecure=True, path=self.devices)
        self.assertEqual(entry['url'], 'http://10.0.0.5:5001')

    def test_add_allows_https(self):
        entry = remote.add_device('boxb', 'https://10.0.0.5:5001', TOKEN,
                                  path=self.devices)
        self.assertEqual(entry['url'], 'https://10.0.0.5:5001')

    def test_add_refuses_bad_scheme(self):
        with self.assertRaises(remote.DevicesError):
            remote.add_device('boxb', 'ftp://host', TOKEN, path=self.devices)

    def test_remove(self):
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        self.assertTrue(remote.remove_device('boxb', self.devices))
        self.assertFalse(remote.remove_device('boxb', self.devices))

    def test_update_existing_device(self):
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        remote.add_device('boxb', 'http://localhost:5002', TOKEN,
                          path=self.devices)
        self.assertEqual(remote.get_device('boxb', self.devices)['url'],
                         'http://localhost:5002')
        self.assertEqual(len(remote.list_devices(self.devices)), 1)

    def test_corrupt_store_fails_closed(self):
        with open(self.devices, 'w') as fh:
            fh.write('{not json')
        with self.assertRaises(remote.DevicesError):
            remote.load_devices(self.devices)
        # The gate stays on (the file exists) but the error is surfaced.
        self.assertTrue(remote.devices_enabled(self.devices))

    def test_describe_device_never_exposes_token(self):
        entry = remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                                  path=self.devices)
        info = remote.describe_device(entry)
        self.assertNotIn('token', info)
        self.assertNotIn(TOKEN, json.dumps(info))
        self.assertTrue(info['token_set'])
        self.assertTrue(info['token_fingerprint'])
        self.assertNotEqual(info['token_fingerprint'], TOKEN)

    def test_token_fingerprint_is_stable_and_nonreversible(self):
        self.assertEqual(remote.token_fingerprint(TOKEN),
                         remote.token_fingerprint(TOKEN))
        self.assertNotIn(TOKEN, remote.token_fingerprint(TOKEN))

    def test_save_leaves_no_temp_files(self):
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        leftovers = [n for n in os.listdir(self.tmp)
                     if n.startswith('.devices-')]
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# Remote client (injected transport)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, body=b'', status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body


def opener_returning(body, status=200, captured=None):
    def _opener(request, timeout):
        if captured is not None:
            captured.append(request)
        return FakeResponse(body, status)
    return _opener


def opener_raising(exc):
    def _opener(request, timeout):
        raise exc
    return _opener


class TestRemoteClient(unittest.TestCase):
    def _client(self, opener, **kw):
        return remote.RemoteClient('boxb', 'http://localhost:5001', TOKEN,
                                   opener=opener, **kw)

    def test_get_success(self):
        client = self._client(opener_returning(b'{"success": true}'))
        status, payload = client.get('/health')
        self.assertEqual(status, 200)
        self.assertTrue(payload['success'])

    def test_get_sends_bearer_and_actor_headers(self):
        captured = []
        client = self._client(opener_returning(b'{}', captured=captured),
                              panel_actor='boss', panel_id='hubA')
        client.get('/users')
        req = captured[0]
        self.assertEqual(req.get_header('Authorization'), f'Bearer {TOKEN}')
        self.assertEqual(req.get_header('X-panel-actor'), 'boss')
        self.assertEqual(req.get_header('X-panel-id'), 'hubA')

    def test_post_sends_form_body(self):
        captured = []
        client = self._client(opener_returning(b'{"success": true}',
                                               captured=captured))
        client.post('/lock', {'username': 'kid'})
        self.assertEqual(captured[0].data, b'username=kid')

    def test_http_401_passes_through(self):
        err = urllib.error.HTTPError(
            'http://x/api/v1/users', 401, 'Unauthorized', {},
            io.BytesIO(b'{"success": false, "error": "bad token"}'))
        client = self._client(opener_raising(err))
        status, payload = client.get('/users')
        self.assertEqual(status, 401)
        self.assertFalse(payload['success'])

    def test_http_500_json_passes_through(self):
        err = urllib.error.HTTPError(
            'http://x', 500, 'Server Error', {},
            io.BytesIO(b'{"success": false, "error": "boom"}'))
        client = self._client(opener_raising(err))
        status, payload = client.get('/users')
        self.assertEqual(status, 500)
        self.assertEqual(payload['error'], 'boom')

    def test_unreachable_raises(self):
        client = self._client(
            opener_raising(urllib.error.URLError('Connection refused')))
        with self.assertRaises(remote.RemoteError) as cm:
            client.get('/health')
        self.assertEqual(cm.exception.kind, 'unreachable')
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_timeout_raises(self):
        client = self._client(
            opener_raising(urllib.error.URLError(socket.timeout())))
        with self.assertRaises(remote.RemoteError) as cm:
            client.get('/health')
        self.assertEqual(cm.exception.kind, 'timeout')

    def test_malformed_json_raises(self):
        client = self._client(opener_returning(b'not json'))
        with self.assertRaises(remote.RemoteError) as cm:
            client.get('/health')
        self.assertEqual(cm.exception.kind, 'malformed')

    def test_non_dict_json_raises(self):
        client = self._client(opener_returning(b'42'))
        with self.assertRaises(remote.RemoteError) as cm:
            client.get('/health')
        self.assertEqual(cm.exception.kind, 'malformed')

    def test_json_list_is_accepted_for_reads(self):
        client = self._client(opener_returning(b'[]'))
        status, payload = client.get('/users')
        self.assertEqual(payload, [])

    def test_get_query_string(self):
        captured = []
        client = self._client(opener_returning(b'{}', captured=captured))
        client.get('/audit', {'limit': '5'})
        self.assertIn('limit=5', captured[0].full_url)

    def test_proxy_returns_token_rejected_flag(self):
        entry = {'name': 'boxb', 'url': 'http://localhost:5001',
                 'token': TOKEN}
        err = urllib.error.HTTPError('http://x', 401, 'no', {},
                                     io.BytesIO(b'{"success": false}'))
        status, payload, rejected = remote.proxy(
            entry, 'GET', '/users', opener=opener_raising(err))
        self.assertEqual(status, 401)
        self.assertTrue(rejected)


# ---------------------------------------------------------------------------
# Remote client against a real (ephemeral loopback) agent
# ---------------------------------------------------------------------------

class TestClientAgainstAgent(unittest.TestCase):
    TOKEN = 'loopback-token-0123456789abcdef'

    @classmethod
    def setUpClass(cls):
        from wsgiref.simple_server import make_server, WSGIRequestHandler

        class Quiet(WSGIRequestHandler):
            def log_message(self, *a):
                pass

        cls.server = make_server('127.0.0.1', 0,
                                 agent.AgentApp(token_loader=lambda:
                                                cls.TOKEN),
                                 handler_class=Quiet)
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

    def _client(self):
        return remote.RemoteClient('boxb', f'http://127.0.0.1:{self.port}',
                                   self.TOKEN)

    def test_health(self):
        status, payload = self._client().get('/health')
        self.assertEqual(status, 200)
        self.assertTrue(payload['success'])

    def test_users_with_good_token(self):
        with mock.patch.object(agent, 'get_human_users', return_value=[]), \
             mock.patch.object(agent, 'load_bedtime', return_value={}):
            status, payload = self._client().get('/users')
        self.assertEqual(status, 200)
        self.assertEqual(payload, [])

    def test_users_with_wrong_token_401(self):
        bad = remote.RemoteClient('boxb', f'http://127.0.0.1:{self.port}',
                                  'wrong-token')
        status, payload = bad.get('/users')
        self.assertEqual(status, 401)

    def test_unreachable_port_raises(self):
        # Nothing is listening here; fail visible, never a silent success.
        client = remote.RemoteClient('boxb', 'http://127.0.0.1:1', 'x')
        with self.assertRaises(remote.RemoteError):
            client.get('/health')


# ---------------------------------------------------------------------------
# Hub routes
# ---------------------------------------------------------------------------

class TestHubDeviceRoutes(BaseHubTest):
    def test_devices_requires_admin(self):
        import app
        client = app.app.test_client()
        self.assertEqual(client.get('/api/devices').status_code, 401)

    def test_devices_single_box_when_absent(self):
        import app
        patches = self._admin_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/api/devices')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body['multi_device'])
        self.assertEqual(body['devices'], [])

    def test_devices_reports_reachability(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          hostname='kidsbox', path=self.devices)
        patches = self._admin_patches()
        with mock.patch.object(app, '_remote_proxy',
                               return_value=(200, {'success': True,
                                                   'box_id': 'kidsbox',
                                                   'version': '1'}, False)), \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/api/devices')
        body = resp.get_json()
        self.assertTrue(body['multi_device'])
        self.assertTrue(body['devices'][0]['online'])
        self.assertEqual(body['devices'][0]['box_id'], 'kidsbox')
        self.assertNotIn(TOKEN, json.dumps(body))

    def test_devices_corrupt_store_reports_error(self):
        import app
        with open(self.devices, 'w') as fh:
            fh.write('{oops')
        patches = self._admin_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/api/devices')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.get_json()['error'])

    def test_proxy_unknown_device_404(self):
        import app
        patches = self._admin_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/api/device/nope/v1/health')
        self.assertEqual(resp.status_code, 404)

    def test_proxy_get_forwards_and_audits(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        patches = self._admin_patches()
        with mock.patch.object(app, '_remote_proxy',
                               return_value=(200, {'success': True}, False)) \
                as proxy, \
             mock.patch.object(app.audit, 'safe_log') as logged, \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/api/device/boxb/v1/users')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(proxy.call_args[0][1], 'GET')
        self.assertEqual(proxy.call_args[0][2], '/users')
        actions = [c[0][1] for c in logged.call_args_list]
        self.assertIn('device.command', actions)

    def test_proxy_post_requires_csrf(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        patches = self._admin_patches()
        with mock.patch.object(app, '_remote_proxy',
                               return_value=(200, {'success': True}, False)), \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().post('/api/device/boxb/v1/lock',
                                             data={'username': 'kid'})
        self.assertEqual(resp.status_code, 403)

    def test_proxy_post_with_csrf_forwards(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        patches = self._admin_patches()
        with mock.patch.object(app, '_remote_proxy',
                               return_value=(200, {'success': True,
                                                   'message': 'ok'}, False)) \
                as proxy, \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().post(
                '/api/device/boxb/v1/lock', data={'username': 'kid'},
                headers={'X-CSRF-Token': 'test-csrf'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(proxy.call_args[0][1], 'POST')
        self.assertEqual(proxy.call_args[0][3], {'username': 'kid'})

    def test_proxy_unreachable_returns_502_not_success(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        patches = self._admin_patches()
        with mock.patch.object(app, '_remote_proxy',
                               side_effect=remote.RemoteError(
                                   'unreachable', "device 'boxb' unreachable")), \
             mock.patch.object(app.audit, 'safe_log') as logged, \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/api/device/boxb/v1/users')
        self.assertEqual(resp.status_code, 502)
        self.assertIn('unavailable', resp.get_json()['error'])
        details = [c[0][3] for c in logged.call_args_list
                   if len(c[0]) >= 4]
        self.assertIn(False, [d.get('success') for d in details])

    def test_proxy_token_rejected_is_401(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        patches = self._admin_patches()
        with mock.patch.object(app, '_remote_proxy',
                               return_value=(401, {'success': False},
                                             True)), \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/api/device/boxb/v1/users')
        self.assertEqual(resp.status_code, 401)


# ---------------------------------------------------------------------------
# Feature gate: single-box output is byte-identical
# ---------------------------------------------------------------------------

class TestFeatureGate(BaseHubTest):
    def _base_ctx(self):
        import app
        g = types.SimpleNamespace(admin='boss', tier='wheel',
                                  csrf_token='csrf-x')
        users = [{'username': 'kid', 'uid': 1001, 'home': '/home/kid',
                  'shell': '/bin/bash', 'locked': False,
                  'status_detail': 'Unlocked', 'bedtime': {},
                  'in_bedtime': False,
                  'bonus': {'active': False, 'until': None,
                            'remaining_minutes': None, 'granted_by': None,
                            'last_minutes': None}}]
        return app, g, users

    def test_gate_processor_false_without_store(self):
        import app
        with app.app.test_request_context():
            self.assertFalse(app._multidevice_gate()['devices_enabled'])

    def test_gate_processor_true_with_store(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        with app.app.test_request_context():
            self.assertTrue(app._multidevice_gate()['devices_enabled'])

    def test_index_single_box_is_byte_identical(self):
        app, g, users = self._base_ctx()
        env = app.app.jinja_env
        ctx = dict(g=g, users=users, day_labels=app.DAY_LABELS,
                   devices_enabled=False, insecure_lan=False,
                   devices=[], selected_device='', device_error=None)
        original = open(os.path.join(HERE, 'fixtures',
                                     'index_pre_multidevice.html')).read()
        current = open(os.path.join(ROOT, 'templates', 'index.html')).read()
        self.assertEqual(env.from_string(current).render(**ctx),
                         env.from_string(original).render(**ctx))

    def test_user_page_single_box_is_byte_identical(self):
        app, g, users = self._base_ctx()
        env = app.app.jinja_env
        ctx = dict(
            g=g, username='kid',
            user=users[0], processes=[], visible=[], hidden=[],
            groups={c: [] for c in budgets.CATEGORIES},
            budget_state=budgets.budget_status('kid', {}),
            overrides={'app_overrides': {}, 'windows': []},
            app_names=[], category_labels=budgets.CATEGORY_LABELS,
            categories=budgets.CATEGORIES, day_labels=app.DAY_LABELS,
            devices_enabled=False, insecure_lan=False,
            devices=[], selected_device='', device_error=None)
        original = open(os.path.join(HERE, 'fixtures',
                                     'user_pre_multidevice.html')).read()
        current = open(os.path.join(ROOT, 'templates', 'user.html')).read()
        self.assertEqual(env.from_string(current).render(**ctx),
                         env.from_string(original).render(**ctx))

    def test_index_with_devices_shows_selector(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          hostname='kidsbox', path=self.devices)
        fake_users = [{'username': 'kid', 'uid': 1001, 'home': '/home/kid',
                       'shell': '/bin/bash'}]
        patches = self._admin_patches()
        with mock.patch.object(app, 'get_human_users',
                               return_value=fake_users), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/')
        html = resp.get_data(as_text=True)
        self.assertIn('device-selector', html)
        self.assertIn('boxb', html)
        self.assertIn('switchDevice', html)
        self.assertIn('kidsbox', html)

    def test_index_single_box_has_no_selector(self):
        import app
        patches = self._admin_patches()
        with mock.patch.object(app, 'get_human_users', return_value=[]), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/')
        html = resp.get_data(as_text=True)
        self.assertNotIn('device-selector', html)
        self.assertNotIn('switchDevice', html)

    def test_index_remote_device_uses_remote_users(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        remote_users = [{'username': 'remotekid', 'uid': 1002,
                         'locked': True, 'status_detail': 'Locked',
                         'bedtime': {}, 'in_bedtime': False,
                         'bonus': {'active': False}}]
        patches = self._admin_patches()
        with mock.patch.object(app, '_remote_users',
                               return_value=(remote_users, None)), \
             mock.patch.object(app, 'get_human_users',
                               return_value=[]), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/?device=boxb')
        html = resp.get_data(as_text=True)
        self.assertIn('remotekid', html)

    def test_index_remote_unreachable_shows_error(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        patches = self._admin_patches()
        with mock.patch.object(app, '_remote_users',
                               return_value=(None, "device 'boxb' unreachable")), \
             mock.patch.object(app, 'get_human_users', return_value=[]), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/?device=boxb')
        html = resp.get_data(as_text=True)
        self.assertIn('unreachable', html)

    def test_user_page_remote_renders_remote_payload(self):
        import app
        remote.add_device('boxb', 'http://localhost:5001', TOKEN,
                          path=self.devices)
        payload = {
            'success': True, 'locked': False, 'status_detail': 'Unlocked',
            'in_bedtime': False, 'bonus': {'active': False},
            'uid': 1001, 'home': '/home/kid', 'shell': '/bin/bash',
            'bedtime': {}, 'processes': [], 'visible': [], 'hidden': [],
            'groups': {c: [] for c in budgets.CATEGORIES},
            'app_names': [],
            'budget': budgets.budget_status(
                'kid', {}, overrides={'app_overrides': {}, 'windows': []}),
            'overrides': {'app_overrides': {}, 'windows': []},
        }
        patches = self._admin_patches()
        with mock.patch.object(app, '_remote_user_live',
                               return_value=(payload, None)), \
             patches[0], patches[1], patches[2], patches[3]:
            resp = self._admin_client().get('/user/kid?device=boxb')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('device-selector', resp.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
