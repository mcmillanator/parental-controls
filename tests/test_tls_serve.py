#!/usr/bin/env python3
"""tls_serve.py --http-port redirector tests (install.sh --deploy-webserver).

Hermetic by construction: the TLS server is never started (build_server
stubbed), the redirector binds 127.0.0.1 on ephemeral ports only (never
80/443), and the auth store is a temp file so importing tls_serve -> wsgi
never reads the live store (same dance as tests/test_tls.py). No /etc
reads, no live services.
"""

import ast
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()

try:
    import cheroot  # noqa: F401
    HAS_CHEROOT = True
except ImportError:
    HAS_CHEROOT = False

_MODULE = None
_STORE_DIR = None


def tls_serve_module():
    """Import tls_serve against a throwaway auth store (never the real one).

    Importing pulls wsgi, whose module-level auth init reads
    PARENTAL_CONTROLS_AUTH at import time; purge the cached modules and
    re-import under the patched env, exactly like tests/test_tls.py does.
    """
    global _MODULE, _STORE_DIR
    if _MODULE is not None:
        return _MODULE
    _STORE_DIR = tempfile.mkdtemp(prefix='pc-tls-serve-')
    store = os.path.join(_STORE_DIR, 'auth.json')
    with open(store, 'w') as fh:
        json.dump({'users': {}}, fh)
    with mock.patch.dict(os.environ, {'PARENTAL_CONTROLS_AUTH': store}):
        for mod in ('tls_serve', 'wsgi', 'auth'):
            sys.modules.pop(mod, None)
        import tls_serve
        _MODULE = tls_serve
    return _MODULE


def tearDownModule():
    if _STORE_DIR is not None:
        shutil.rmtree(_STORE_DIR, ignore_errors=True)


@unittest.skipUnless(HAS_CHEROOT, 'cheroot not available')
class TestImportSafety(unittest.TestCase):
    """Module-level surface: build_server unchanged, nothing binds at import."""

    def test_build_server_signature_unchanged(self):
        import inspect
        tls_serve = tls_serve_module()
        params = list(
            inspect.signature(tls_serve.build_server).parameters)
        self.assertEqual(params, ['host', 'port', 'cert', 'key'])

    def test_import_listens_on_nothing(self):
        """Structural pin: no bind lives at module level.

        Binding 80/443/5000 "to see if they are free" is itself forbidden
        (privileged ports, and :5000 belongs to the live service on the
        authoritative VM box), so prove the invariant from the syntax tree:
        every top-level statement that is not a def/class/__main__-guard
        must not mention starting a server — binds happen only inside
        functions, reached only from main().
        """
        tree = ast.parse(read('tls_serve.py'))
        parts = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            if (isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                continue  # module docstring (mentions the verbs in prose only)
            if isinstance(node, ast.If) and '__main__' in ast.unparse(node.test):
                continue  # the guarded entrypoint call is the intended bind
            parts.append(ast.unparse(node))
        joined = '\n'.join(parts)
        for verb in ('serve_forever', 'build_server(', 'start_redirect_server(',
                     '.start()', '.listen', 'WSGIServer(',
                     'ThreadingHTTPServer('):
            self.assertNotIn(verb, joined,
                             'module-level code must never bind: %r' % verb)


@unittest.skipUnless(HAS_CHEROOT, 'cheroot not available')
class TestRedirectLocation(unittest.TestCase):
    """Pure logic: Location building for every Host/target shape."""

    def setUp(self):
        self.rs = tls_serve_module()

    def loc(self, target, host=None, tls_name=''):
        return self.rs.redirect_location(target, host, tls_name)

    def test_path_and_query_preserved(self):
        self.assertEqual(
            self.loc('/panel?next=%2Fhome&x=1', 'box.lan'),
            'https://box.lan/panel?next=%2Fhome&x=1')

    def test_default_ports_stripped(self):
        self.assertEqual(self.loc('/', 'box:80'), 'https://box/')
        self.assertEqual(self.loc('/a', 'box:443'), 'https://box/a')

    def test_non_default_port_preserved(self):
        self.assertEqual(self.loc('/a', 'box:8443'), 'https://box:8443/a')

    def test_ipv6_literal_kept_bracketed(self):
        self.assertEqual(self.loc('/a', '[::1]:80'), 'https://[::1]/a')
        self.assertEqual(self.loc('/a', '[::1]'), 'https://[::1]/a')

    def test_bad_host_falls_back_to_tls_name(self):
        for bad in ('', None, 'not a host!', 'evil.example.com/x',
                    'user:pass@evil', 'javascript:alert', '::1',
                    'box:99999', 'x' * 300, 'box:'):
            with self.subTest(host=bad):
                self.assertEqual(
                    self.loc('/a', bad, tls_name='pc.example'),
                    'https://pc.example/a')

    def test_no_host_and_no_tls_name_yields_relative_path(self):
        # The simplest safe option: never fabricate an authority.
        self.assertEqual(self.loc('/a/b?q=1', None), '/a/b?q=1')
        self.assertEqual(self.loc('/x', 'bad host!', tls_name=''), '/x')

    def test_double_slash_target_cannot_become_authority(self):
        self.assertEqual(self.loc('//evil.com/x', 'good.example'),
                         'https://good.example/evil.com/x')

    def test_backslash_target_normalized(self):
        self.assertEqual(self.loc('/\\evil.com', 'good.example'),
                         'https://good.example/evil.com')

    def test_abs_uri_target_degrades_to_path(self):
        self.assertEqual(self.loc('http://evil.com/x', 'g.example'),
                         'https://g.example/http://evil.com/x')

    def test_options_star_maps_to_root(self):
        self.assertEqual(self.loc('*', 'g'), 'https://g/')

    def test_tls_name_also_sanitized(self):
        self.assertEqual(self.loc('/a', None, tls_name='bad name!'), '/a')

    def test_control_chars_dropped_from_target(self):
        self.assertEqual(self.loc('/a\x00b', 'g'), 'https://g/ab')


@unittest.skipUnless(HAS_CHEROOT, 'cheroot not available')
class TestMainArgv(unittest.TestCase):
    """main(): --http-port defaults off and nothing binds through it here."""

    def setUp(self):
        self.rs = tls_serve_module()

    def _run(self, argv):
        with mock.patch.object(self.rs, 'build_server',
                               return_value=mock.Mock()) as build:
            with mock.patch.object(self.rs, 'start_redirect_server',
                                   return_value=None) as redir:
                self.rs.main(argv)
        return build, redir

    def test_http_port_default_zero_never_starts_redirector(self):
        base = ['--host=0.0.0.0', '--port=5000', '--cert=/c.crt',
                '--key=/c.key']
        build, redir = self._run(base)
        self.assertEqual(build.call_args.args,
                         ('0.0.0.0', 5000, '/c.crt', '/c.key'))
        redir.assert_not_called()

    def test_http_port_zero_explicit_off(self):
        build, redir = self._run(['--host=h', '--port=443', '--cert=/c',
                                  '--key=/k', '--http-port=0'])
        redir.assert_not_called()

    def test_http_port_positive_starts_redirector_with_same_host(self):
        build, redir = self._run(['--host=0.0.0.0', '--port=443',
                                  '--cert=/c', '--key=/k',
                                  '--http-port=80', '--tls-name=pc.lan'])
        self.assertEqual(build.call_args.args,
                         ('0.0.0.0', 443, '/c', '/k'))
        redir.assert_called_once_with('0.0.0.0', 80, 'pc.lan')

    def test_unknown_flag_still_rejected(self):
        # The historical wsgi:application regression: argparse stays strict.
        with self.assertRaises(SystemExit):
            self._run(['--cert=/c', '--key=/k', 'wsgi:application'])


@unittest.skipUnless(HAS_CHEROOT, 'cheroot not available')
class TestRedirectServerRealSocket(unittest.TestCase):
    """Real ThreadingHTTPServer round-trips on 127.0.0.1 ephemeral ports."""

    def setUp(self):
        self.rs = tls_serve_module()
        self.httpd = self.rs.start_redirect_server('127.0.0.1', 0)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]
        self.assertGreater(self.port, 1024,
                           'test redirector must never sit on a privileged port')

    def request_raw(self, payload, tls_name=''):
        """Send a literal request (full header control), return the response."""
        self.httpd.tls_name = tls_name
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) \
                as sock:
            sock.sendall(payload)
            chunks = []
            while True:
                part = sock.recv(4096)
                if not part:
                    break
                chunks.append(part)
                if b'\r\n\r\n' in b''.join(chunks):
                    break
        return b''.join(chunks).decode('latin-1')

    def header(self, response, name):
        for line in response.split('\r\n'):
            if line.lower().startswith(name.lower() + ':'):
                return line.split(':', 1)[1].strip()
        return None

    def test_get_301_with_https_location(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/bedtime?hour=21')
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 301)
        location = resp.getheader('Location')
        self.assertTrue(location.startswith('https://'), location)
        self.assertIn('/bedtime?hour=21', location)
        self.assertEqual(body, b'')

    def test_every_method_answers_301(self):
        for method in ('GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'OPTIONS',
                       'PATCH', 'TRACE'):
            with self.subTest(method=method):
                conn = http.client.HTTPConnection('127.0.0.1', self.port,
                                                  timeout=5)
                try:
                    conn.request(method, '/x' if method != 'OPTIONS' else '*',
                                 body='payload' if method in
                                 ('POST', 'PUT', 'PATCH') else None)
                    resp = conn.getresponse()
                    resp.read()
                    self.assertEqual(resp.status, 301,
                                     '%s must 301, not proxied to the app'
                                     % method)
                    self.assertTrue(resp.getheader('Location').startswith(
                        'https://127.0.0.1:%d' % self.port))
                finally:
                    conn.close()

    def test_host_header_used_as_authority(self):
        response = self.request_raw(
            b'GET /a HTTP/1.1\r\nHost: panel.example.lan\r\n'
            b'Connection: close\r\n\r\n')
        self.assertTrue(response.startswith('HTTP/1.1 301'), response)
        self.assertEqual(self.header(response, 'Location'),
                         'https://panel.example.lan/a')

    def test_bad_host_falls_back_to_tls_name_argument(self):
        response = self.request_raw(
            b'GET /a HTTP/1.1\r\nHost: evil host"/x\r\n'
            b'Connection: close\r\n\r\n', tls_name='pc.example.lan')
        self.assertEqual(self.header(response, 'Location'),
                         'https://pc.example.lan/a')

    def test_missing_host_http10_falls_back_relative(self):
        response = self.request_raw(b'GET /a HTTP/1.0\r\n\r\n')
        self.assertTrue(response.startswith('HTTP/1.1 301'), response)
        self.assertEqual(self.header(response, 'Location'), '/a')

    def test_thread_is_daemon_named(self):
        names = {t.name: t.daemon for t in threading.enumerate()
                 if t.name == 'parental-http-redirect'}
        self.assertEqual(names, {'parental-http-redirect': True})

    def test_serves_many_requests(self):
        # Smoke: ThreadingHTTPServer keeps answering after repeated closes.
        for _ in range(25):
            response = self.request_raw(
                b'GET / HTTP/1.1\r\nHost: box\r\nConnection: close\r\n\r\n')
            self.assertTrue(response.startswith('HTTP/1.1 301'))

    def test_never_runs_the_wsgi_app(self):
        # The redirector answers without ever touching `application`.
        with mock.patch.object(self.rs, 'application',
                               side_effect=AssertionError('app was served')):
            conn = http.client.HTTPConnection('127.0.0.1', self.port,
                                              timeout=5)
            self.addCleanup(conn.close)
            conn.request('GET', '/')
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 301)


if __name__ == '__main__':
    unittest.main()
