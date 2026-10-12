#!/usr/bin/env python3
"""TLS-for-LAN tests (TLS_PLAN.md section 4).

Covers the plan's 14 numbered tests: the waitress-has-no-TLS pin, the
installer guard + rendering variants (via the sourced/testable tls_lib.sh
helpers plus wiring assertions on install.sh, which is never executed),
Secure cookies, the insecure-LAN banner, owner-cert validation, the
healthcheck --tls flag (+ the project-wide `-k` pin), expiry reporting,
the firewall opt-in, and a real TLS round-trip on an ephemeral loopback
port. No live users, no ports below 1024, no firewall changes, no LAN
binds; socket tests bind 127.0.0.1 on an ephemeral port.
"""

import http.client
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import auth
import app

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TLS_LIB = os.path.join(ROOT, 'tls_lib.sh')
INSTALLER = os.path.join(ROOT, 'install.sh')
HEALTHCHECK = os.path.join(ROOT, 'healthcheck.sh')

HAS_OPENSSL = shutil.which('openssl') is not None

try:
    import cheroot  # noqa: F401
    HAS_CHEROOT = True
except ImportError:
    HAS_CHEROOT = False

BANNER_TEXT = 'Insecure LAN mode'


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def lib(*argv):
    """Run a tls_lib.sh helper: lib('tls_decide_mode', 'a', '0', '0')."""
    return subprocess.run(
        ['bash', '-c', 'source "$0"; "%s" "${@:1}"' % argv[0],
         TLS_LIB, *argv[1:]],
        capture_output=True, text=True, timeout=60)


def render_unit_via_shell(template_rel, host, port, mode,
                          app_dir='/opt/parental-controls', user='tlsuser',
                          health_tls='',
                          cert='/etc/parental-controls/tls/server.crt',
                          key='/etc/parental-controls/tls/server.key'):
    template = os.path.join(ROOT, template_rel)
    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, 'unit')
        proc = subprocess.run(
            ['bash', '-c',
             'source "$0"; sed'
             ' -e "s|@APP_DIR@|$2|g" -e "s|@SERVICE_USER@|$3|g"'
             ' -e "s|@BIND_HOST@|$4|g" -e "s|@BIND_PORT@|$5|g"'
             ' "$6" > "$7"'
             ' && tls_apply_variant "$7" "$8" "$2" "$4" "$5" "$9" "${10}"'
             ' "${11}" && cat "$7"',
             TLS_LIB, TLS_LIB, app_dir, user, host, port, template, out,
             mode, cert, key, health_tls],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout


def execstart_python_argv(rendered):
    """The python argv inside a rendered TLS ExecStart, as a list.

    e.g. ['--host=0.0.0.0', '--port=5000', '--cert=/c.crt',
          '--key=/c.key'] — the exact list tls_serve.main() must accept.
    """
    match = re.search(r"tls_serve\.py ([^']+)'", rendered)
    assert match, 'no tls_serve python argv found in render'
    return shlex.split(match.group(1))


def make_cert(tmpdir, name='test-tls-name'):
    """Generate a pair with the installer's exact argv (via tls_lib.sh)."""
    cert = os.path.join(tmpdir, 'server.crt')
    key = os.path.join(tmpdir, 'server.key')
    proc = subprocess.run(
        ['bash', '-c',
         'source "$0"; tls_generate_selfsigned "$1" "$2" "$3"'
         ' "DNS:localhost,IP:127.0.0.1,IP:::1,$(tls_san_entry "$3")"',
         TLS_LIB, cert, key, name],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return cert, key


class TestWaitressHasNoNativeTLS(unittest.TestCase):
    """Plan test 1: pins TLS_PLAN.md section 0.

    If a future waitress gains TLS knobs this fails on purpose, forcing a
    re-evaluation of the cheroot decision.
    """

    def test_adjustments_reject_tls_arguments(self):
        from waitress.adjustments import Adjustments
        for name in ('ssl', 'tls', 'cert', 'key', 'ssl_context',
                     'certfile', 'keyfile'):
            with self.subTest(arg=name):
                with self.assertRaises((TypeError, ValueError)):
                    Adjustments(**{name: 'x'})

    def test_waitress_sources_mention_no_tls_listener(self):
        import waitress.adjustments
        import waitress.runner
        for module in (waitress.adjustments, waitress.runner):
            with open(module.__file__, encoding='utf-8') as fh:
                source = fh.read()
            self.assertNotIn('ssl_context', source)
            self.assertNotIn('certfile', source)
            self.assertNotIn('keyfile', source)


class TestInstallerRefusesPlainLanBind(unittest.TestCase):
    """Plan test 2: non-loopback --bind without --tls/--insecure-lan."""

    def test_decide_mode_refuses_plain_lan(self):
        for host in ('0.0.0.0', '192.168.1.10', '10.0.0.2',
                     '192.168.178.23'):
            with self.subTest(host=host):
                proc = lib('tls_decide_mode', host, '0', '0')
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), 'refuse-plain-lan')

    def test_tls_and_insecure_lan_escape_the_refusal(self):
        self.assertEqual(
            lib('tls_decide_mode', '0.0.0.0', '1', '0').stdout.strip(),
            'tls')
        self.assertEqual(
            lib('tls_decide_mode', '192.168.1.10', '0', '1').stdout.strip(),
            'insecure')

    def test_installer_wires_the_refusal(self):
        script = read('install.sh')
        self.assertIn('tls_decide_mode', script)
        self.assertIn('refusing plain-HTTP', script)
        idx = script.index('refusing plain-HTTP')
        self.assertIn('exit 2', script[idx:idx + 500])


class TestLocalhostNeedsNoTlsFlags(unittest.TestCase):
    """Plan test 3: the default loopback path installs with zero TLS flags."""

    def test_all_loopback_binds_stay_loopback(self):
        for host in ('127.0.0.1', 'localhost', '::1'):
            with self.subTest(host=host):
                proc = lib('tls_decide_mode', host, '0', '0')
                self.assertEqual(proc.stdout.strip(), 'loopback')

    def test_insecure_flag_is_ignored_on_loopback(self):
        for host in ('127.0.0.1', 'localhost', '::1'):
            with self.subTest(host=host):
                proc = lib('tls_decide_mode', host, '0', '1')
                self.assertEqual(proc.stdout.strip(), 'loopback')

    def test_explicit_tls_wins_on_loopback(self):
        proc = lib('tls_decide_mode', '127.0.0.1', '1', '0')
        self.assertEqual(proc.stdout.strip(), 'tls')


@unittest.skipUnless(HAS_OPENSSL, 'openssl CLI not available')
class TestSelfSignedCertParses(unittest.TestCase):
    """Plan test 4: the installer's openssl argv produces a sound cert."""

    def test_cert_parses_with_expected_identity_and_lifetime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chmod(tmpdir, 0o700)
            cert, _key = make_cert(tmpdir, name='192.168.1.50')
            proc = subprocess.run(
                ['openssl', 'x509', '-in', cert, '-noout', '-subject',
                 '-ext', 'subjectAltName', '-text'],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('CN=192.168.1.50', proc.stdout)
            self.assertIn('DNS:localhost', proc.stdout)
            self.assertIn('127.0.0.1', proc.stdout)
            self.assertIn('192.168.1.50', proc.stdout)
            self.assertRegex(proc.stdout, r'Public-Key: \(2048 bit\)')
            days = int(lib('tls_expiry_days', cert).stdout.strip())
            self.assertGreaterEqual(days, 3600)
            self.assertLessEqual(days, 3650)


@unittest.skipUnless(HAS_OPENSSL, 'openssl CLI not available')
class TestCertKeyPermissionsAndOwnership(unittest.TestCase):
    """Plan test 5: 0600 pair in a 0700 dir; re-runs never regenerate."""

    def test_generated_pair_is_0600(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chmod(tmpdir, 0o700)
            cert, key = make_cert(tmpdir)
            for path in (cert, key):
                mode = os.stat(path).st_mode & 0o777
                self.assertEqual(mode, 0o600, path)
            self.assertEqual(os.stat(tmpdir).st_mode & 0o777, 0o700)

    def test_installer_never_regenerates_and_sets_ownership(self):
        script = read('install.sh')
        self.assertIn('REGEN_TLS_CERT" -eq 0', script)
        self.assertIn('never regenerated on re-run', script)
        self.assertIn('chmod 700 "$TLS_DIR"', script)
        self.assertIn('chmod 600 "$TLS_CERT" "$TLS_KEY"', script)
        self.assertIn('chown "$SERVICE_USER:$SERVICE_USER" "$TLS_CERT"',
                      script)
        self.assertIn('.bak-$stamp', script)


class TestLoopbackUnitIsByteIdentical(unittest.TestCase):
    """Plan test 6: loopback rendering matches the pre-TLS rendering."""

    def test_loopback_render(self):
        rendered = render_unit_via_shell('systemd/parental-controls.service',
                                         '127.0.0.1', '5000', 'loopback')
        self.assertIn(
            "ExecStart=/bin/bash -c 'cd /opt/parental-controls && "
            ". .venv/bin/activate && exec waitress-serve "
            "--host=127.0.0.1 --port=5000 wsgi:application'",
            rendered)
        # The waitress/loopback variant legitimately keeps the trailing
        # `wsgi:application` (waitress-serve is a pserve-style runner);
        # only the tls_serve.py variant must not carry it.
        self.assertIn('wsgi:application', rendered)
        self.assertNotIn('PARENTAL_TLS', rendered)
        self.assertNotIn('PARENTAL_INSECURE_LAN', rendered)
        self.assertNotRegex(rendered, r'@[A-Z_]+@')
        # Full-text check: the render step may only substitute the four
        # values and drop the two variant placeholder lines.
        template = read('systemd/parental-controls.service')
        expected = '\n'.join(
            line for line in template.splitlines()
            if line.strip() not in ('@TLS_EXECSTART@', '@TLS_ENV_LINE@'))
        for placeholder, value in (
                ('@APP_DIR@', '/opt/parental-controls'),
                ('@SERVICE_USER@', 'tlsuser'),
                ('@BIND_HOST@', '127.0.0.1'),
                ('@BIND_PORT@', '5000')):
            expected = expected.replace(placeholder, value)
        self.assertEqual(rendered.strip(), expected.strip())


class TestTlsUnitRendersCertPathsAndEnv(unittest.TestCase):
    """Plan test 7: the LAN+TLS variant carries cert paths + Secure env."""

    def test_tls_render(self):
        rendered = render_unit_via_shell('systemd/parental-controls.service',
                                         '0.0.0.0', '5000', 'tls',
                                         health_tls=' --tls')
        self.assertIn('tls_serve.py', rendered)
        self.assertIn('--cert=/etc/parental-controls/tls/server.crt',
                      rendered)
        self.assertIn('--key=/etc/parental-controls/tls/server.key',
                      rendered)
        # Regression: the TLS ExecStart must carry ONLY tls_serve.py's own
        # argv. install.sh used to append the waitress-style trailing
        # `wsgi:application`, which tls_serve.py's argparse rejects
        # ("unrecognized arguments"), crash-looping every --tls deploy.
        self.assertIn(
            "ExecStart=/bin/bash -c 'cd /opt/parental-controls && "
            ". .venv/bin/activate && exec python "
            "/opt/parental-controls/tls_serve.py "
            "--host=0.0.0.0 --port=5000 "
            "--cert=/etc/parental-controls/tls/server.crt "
            "--key=/etc/parental-controls/tls/server.key'",
            rendered)
        self.assertNotIn('wsgi:application', rendered)
        self.assertEqual(rendered.count('Environment=PARENTAL_TLS=1'), 1)
        self.assertNotIn('waitress-serve', rendered)
        self.assertNotIn('PARENTAL_INSECURE_LAN', rendered)
        self.assertNotRegex(rendered, r'@[A-Z_]+@')
        self.assertNotRegex(rendered, r'(/home/|Documents/Projects)')

    def test_insecure_render_has_banner_env_not_tls_env(self):
        rendered = render_unit_via_shell('systemd/parental-controls.service',
                                         '0.0.0.0', '5000', 'insecure')
        self.assertIn('Environment=PARENTAL_INSECURE_LAN=1', rendered)
        self.assertIn('waitress-serve', rendered)
        self.assertNotIn('PARENTAL_TLS', rendered)
        self.assertNotRegex(rendered, r'@[A-Z_]+@')


class TestTlsExecStartArgvParsesCleanly(unittest.TestCase):
    """The rendered TLS ExecStart must be exactly tls_serve.py's own argv.

    Regression: install.sh rendered `... tls_serve.py --host=... --port=...
    --cert=... --key=... wsgi:application`, but tls_serve.py imports the
    WSGI app itself (the trailing app is a waitress runner arg) and its
    argparse has no positional slot. Every --tls deploy crash-looped with
    `unrecognized arguments: wsgi:application`, leaving the unit stuck in
    `activating (auto-restart)`.
    """

    _CERT = '/etc/parental-controls/tls/server.crt'
    _KEY = '/etc/parental-controls/tls/server.key'

    def _store(self):
        tmpdir = tempfile.mkdtemp(prefix='pc-tls-execstart-')
        self.addCleanup(shutil.rmtree, tmpdir, True)
        path = os.path.join(tmpdir, 'auth.json')
        with open(path, 'w') as fh:
            json.dump({'users': {}}, fh)
        return path

    def _parse(self, argv):
        """tls_serve.main(argv) against tls_serve.py's REAL argparse, with
        build_server stubbed so nothing binds. Returns (host, port, cert,
        key) as parsed — SystemExit means argparse rejected the argv.

        The auth store must be a temp file BEFORE the import: importing
        tls_serve pulls in wsgi, whose module-level auth init reads
        auth_file_path() at import time, and the real store at
        /etc/parental-controls/auth.json is outside the verified
        project/config scope (the prior version of this test read the
        live store and triggered a broad permission prompt). Purging the
        cached modules forces the import to run under the patched env.
        """
        store = self._store()
        with mock.patch.dict(os.environ,
                             {'PARENTAL_CONTROLS_AUTH': store}):
            for _mod in ('tls_serve', 'wsgi', 'auth'):
                sys.modules.pop(_mod, None)
            import tls_serve  # noqa: F811  (imports under the temp store)
            with mock.patch.object(
                    tls_serve, 'build_server',
                    return_value=mock.Mock()) as build:
                tls_serve.main(argv)  # SystemExit == argparse rejected argv
                return build.call_args.args

    def test_lan_tls_render_parses_with_tls_serves_parser(self):
        rendered = render_unit_via_shell('systemd/parental-controls.service',
                                         '0.0.0.0', '5000', 'tls',
                                         health_tls=' --tls')
        self.assertNotIn('wsgi:application', rendered)
        args = self._parse(execstart_python_argv(rendered))
        self.assertEqual(args, ('0.0.0.0', 5000, self._CERT, self._KEY))

    def test_loopback_tls_render_parses_with_tls_serves_parser(self):
        # --tls on a loopback bind (TLS-over-tunnel) is a real mode: it must
        # render an ExecStart the same parser accepts.
        rendered = render_unit_via_shell('systemd/parental-controls.service',
                                         '127.0.0.1', '5443', 'tls',
                                         health_tls=' --tls')
        self.assertNotIn('wsgi:application', rendered)
        args = self._parse(execstart_python_argv(rendered))
        self.assertEqual(args, ('127.0.0.1', 5443, self._CERT, self._KEY))

    def test_old_buggy_trailing_wsgi_application_is_rejected(self):
        """The pre-fix argv must FAIL argparse — that failure IS the bug
        this suite pins so it can never regress."""
        store = self._store()
        import tls_serve  # noqa: F811
        with mock.patch.dict(os.environ,
                             {'PARENTAL_CONTROLS_AUTH': store}):
            with mock.patch.object(tls_serve, 'build_server') as build:
                with self.assertRaises(SystemExit):
                    tls_serve.main(['--host=0.0.0.0', '--port=5000',
                                    '--cert=/c.crt', '--key=/c.key',
                                    'wsgi:application'])
                build.assert_not_called()


class TlsClientBase(unittest.TestCase):
    ADMIN = 'tlstestadmin'
    PASSWORD = 'Tls-Test-Pass-12345'

    @classmethod
    def setUpClass(cls):
        cls.hash = auth.hash_password(cls.PASSWORD)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth_path = os.path.join(self.tmp.name, 'auth.json')
        with open(self.auth_path, 'w') as fh:
            json.dump({'users': {
                self.ADMIN: {'hash': self.hash, 'must_change': False,
                             'locked': False}}}, fh)
        os.chmod(self.auth_path, 0o600)
        self._env = mock.patch.dict(
            os.environ, {'PARENTAL_CONTROLS_AUTH': self.auth_path})
        self._env.start()
        self.addCleanup(self._env.stop)
        auth.clear_sessions()
        auth.clear_login_failures()
        self.addCleanup(auth.clear_sessions)
        self.addCleanup(auth.clear_login_failures)
        self._secure = app.app.config['SECURE_COOKIES']
        self._banner = app.app.config.get('INSECURE_LAN', False)
        self.addCleanup(self._restore_config)
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()
        patcher_w = mock.patch.object(
            auth, 'is_wheel_member', side_effect=lambda u: u == self.ADMIN)
        patcher_s = mock.patch.object(
            auth, 'is_system_user', side_effect=lambda u: u == self.ADMIN)
        self.addCleanup(patcher_w.stop)
        self.addCleanup(patcher_s.stop)
        patcher_w.start()
        patcher_s.start()

    def _restore_config(self):
        app.app.config['SECURE_COOKIES'] = self._secure
        app.app.config['INSECURE_LAN'] = self._banner

    def tearDown(self):
        self.tmp.cleanup()

    def login(self):
        return self.client.post('/login', data={'username': self.ADMIN,
                                                'password': self.PASSWORD})


class TestSecureCookieIffTlsEnv(TlsClientBase):
    """Plan test 8: Secure follows PARENTAL_TLS=1 through the real stack."""

    def test_secure_cookie_set_when_tls(self):
        app.app.config['SECURE_COOKIES'] = True
        resp = self.login()
        self.assertEqual(resp.status_code, 302)
        cookie = resp.headers.get('Set-Cookie', '')
        self.assertIn('pc_sid=', cookie)
        self.assertIn('Secure', cookie.split(';', 1)[1])
        self.assertIn('HttpOnly', cookie)

    def test_no_secure_cookie_without_tls(self):
        app.app.config['SECURE_COOKIES'] = False
        resp = self.login()
        self.assertEqual(resp.status_code, 302)
        cookie = resp.headers.get('Set-Cookie', '')
        self.assertIn('pc_sid=', cookie)
        self.assertNotIn('Secure', cookie)


class TestInsecureBannerOnlyInInsecureLan(TlsClientBase):
    """Plan test 9: banner iff insecure-LAN mode; login shows it first."""

    BANNER = b'Insecure LAN mode'

    def login_cookie(self):
        resp = self.login()
        self.assertEqual(resp.status_code, 302)
        sid = resp.headers['Set-Cookie'].split('pc_sid=')[1].split(';')[0]
        return {'Cookie': 'pc_sid=%s' % sid}

    def test_login_shows_banner_only_when_insecure(self):
        app.app.config['INSECURE_LAN'] = True
        resp = self.client.get('/login')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.BANNER, resp.data)
        # Above the credential form, not below it.
        self.assertLess(resp.data.index(self.BANNER),
                        resp.data.index(b'<form'))
        app.app.config['INSECURE_LAN'] = False
        resp = self.client.get('/login')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(self.BANNER, resp.data)

    def test_index_shows_banner_only_when_insecure(self):
        headers = self.login_cookie()
        with mock.patch.object(app, 'get_human_users', return_value=[]):
            app.app.config['INSECURE_LAN'] = True
            resp = self.client.get('/', headers=headers)
            self.assertEqual(resp.status_code, 200)
            self.assertIn(self.BANNER, resp.data)
            app.app.config['INSECURE_LAN'] = False
            resp = self.client.get('/', headers=headers)
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn(self.BANNER, resp.data)

    def test_all_templates_include_the_banner_partial(self):
        """Converted pages inherit the token banner from base.html, which
        includes the `_partials/insecure_banner.html` partial; un-converted
        pages still include the legacy `_insecure_banner.html` directly."""
        base = read('templates/base.html')
        self.assertIn("_partials/insecure_banner.html", base)
        for template in ('login.html', 'index.html', 'user.html',
                         'change_password.html'):
            with self.subTest(template=template):
                body = read('templates/%s' % template)
                self.assertTrue(
                    '{% extends "base.html" %}' in body or
                    "_insecure_banner.html" in body,
                    '%s must extend base.html or include the banner partial'
                    % template)


@unittest.skipUnless(HAS_OPENSSL, 'openssl CLI not available')
class TestOwnerCertValidation(unittest.TestCase):
    """Plan test 10: owner pairs validate or fail closed (exit 2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cert, self.key = make_cert(self.tmp.name, name='owner-box')

    def validate(self, cert, key):
        return subprocess.run(
            ['bash', '-c', 'source "$0"; tls_validate_owner_pair "$1" "$2"',
             TLS_LIB, cert, key],
            capture_output=True, text=True, timeout=60)

    def test_matching_pair_validates(self):
        proc = self.validate(self.cert, self.key)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_mismatched_key_rejected(self):
        other_dir = os.path.join(self.tmp.name, 'other')
        os.makedirs(other_dir)
        _other_cert, other_key = make_cert(other_dir, name='other-box')
        proc = self.validate(self.cert, other_key)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('does not match', proc.stderr)

    def test_missing_and_unparseable_rejected(self):
        proc = self.validate(os.path.join(self.tmp.name, 'absent.crt'),
                             self.key)
        self.assertNotEqual(proc.returncode, 0)
        bad = os.path.join(self.tmp.name, 'bad.crt')
        with open(bad, 'w') as fh:
            fh.write('not a certificate\n')
        proc = self.validate(bad, self.key)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('does not parse', proc.stderr)

    def test_relative_paths_rejected(self):
        proc = self.validate('rel/cert.crt', self.key)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('absolute path', proc.stderr)

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file permissions')
    def test_unreadable_key_rejected(self):
        locked = os.path.join(self.tmp.name, 'locked.key')
        shutil.copy(self.key, locked)
        os.chmod(locked, 0o000)
        proc = self.validate(self.cert, locked)
        self.assertNotEqual(proc.returncode, 0)

    def test_installer_fails_closed_and_copies_verbatim(self):
        script = read('install.sh')
        self.assertIn('tls_validate_owner_pair', script)
        idx = script.index('tls_validate_owner_pair "$TLS_CERT_IN"')
        self.assertIn('exit 2', script[idx:idx + 200])
        self.assertIn('cp "$TLS_CERT_IN" "$TLS_CERT"', script)
        self.assertIn('--tls-cert and --tls-key must both be given', script)


class TestHealthcheckTlsFlag(unittest.TestCase):
    """Plan test 11: --tls flips the probe to https with -k; -k pinned."""

    def test_tls_flag_parses(self):
        script = read('healthcheck.sh')
        self.assertIn('--tls) USE_TLS=1', script)
        self.assertIn('scheme="https"', script)
        self.assertIn('curl_tls=(-k)', script)
        self.assertIn('scheme="http"', script)

    def test_tls_probe_uses_https_and_plain_uses_http(self):
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import test_health
        with test_health.ProbeServer() as srv:
            proc = test_health.run_probe(
                '--host', '127.0.0.1', '--port', str(srv.port),
                '--auth-store', self._store())
            self.assertEqual(proc.returncode, 0, proc.stderr)
            proc = test_health.run_probe(
                '--host', '127.0.0.1', '--port', str(srv.port),
                '--auth-store', self._store(), '--tls')
            self.assertEqual(proc.returncode, 1,
                             f'{proc.stdout}{proc.stderr}')
            self.assertIn('https://127.0.0.1:%d' % srv.port, proc.stderr)

    def test_k_flag_appears_only_in_healthcheck(self):
        pin = re.compile(r'(?<![-\w])-k(?![-\w])')
        self.assertIsNotNone(pin.search(read('healthcheck.sh')))
        for rel in ('install.sh', 'run.sh', 'tls_serve.py', 'wsgi.py',
                    'tls_lib.sh', 'app.py'):
            with self.subTest(rel=rel):
                self.assertIsNone(pin.search(read(rel)),
                                  '%s must not contain a standalone -k' % rel)

    def _store(self):
        tmpdir = tempfile.mkdtemp(prefix='pc-tls-health-')
        self.addCleanup(shutil.rmtree, tmpdir, True)
        path = os.path.join(tmpdir, 'auth.json')
        with open(path, 'w') as fh:
            json.dump({'users': {}}, fh)
        return path


@unittest.skipUnless(HAS_OPENSSL, 'openssl CLI not available')
class TestVerifyReportsExpiry(unittest.TestCase):
    """Plan test 12: verify prints the enddate and warns under 30 days."""

    def test_expiry_days_tracks_cert_lifetime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.addCleanup(shutil.rmtree, tmpdir, True)
            short = os.path.join(tmpdir, 'short.crt')
            key = os.path.join(tmpdir, 'short.key')
            proc = subprocess.run(
                ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                 '-days', '20', '-subj', '/CN=short',
                 '-keyout', key, '-out', short],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            days = int(lib('tls_expiry_days', short).stdout.strip())
            self.assertLessEqual(days, 20)
            self.assertLess(days, 30)
            long_cert, _key = make_cert(tmpdir, name='long-lived')
            days = int(lib('tls_expiry_days', long_cert).stdout.strip())
            self.assertGreaterEqual(days, 3600)

    def test_installer_verify_prints_enddate_and_warns(self):
        script = read('install.sh')
        verify = script.split('== 8/8 verify ==', 1)[-1]
        self.assertIn('-enddate', verify)
        self.assertIn('expires in', verify)
        self.assertIn('--regen-tls-cert', verify)


class TestTlsDoesNotOpenFirewall(unittest.TestCase):
    """Plan test 13: --tls never implies --open-firewall."""

    def test_firewall_stays_opt_in(self):
        script = read('install.sh')
        self.assertEqual(script.count('OPEN_FIREWALL=1'), 1)
        self.assertIn('--open-firewall) OPEN_FIREWALL=1', script)
        tls_arm = re.search(r'--tls\) TLS_MODE=1.*?\n', script)
        self.assertIsNotNone(tls_arm)
        self.assertNotIn('OPEN_FIREWALL', tls_arm.group(0))
        idx_gate = script.index('if [ "$OPEN_FIREWALL" -eq 1 ]')
        idx_fw = script.index('firewall-cmd --permanent --add-port')
        self.assertLess(idx_gate, idx_fw)


@unittest.skipUnless(HAS_CHEROOT and HAS_OPENSSL,
                     'cheroot or openssl not available')
class TestTlsOverLoopbackRoundtrip(unittest.TestCase):
    """Plan test 14: real TLS through tls_serve on an ephemeral port."""

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def test_tls_serves_and_plain_http_does_not(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chmod(tmpdir, 0o700)
            cert, key = make_cert(tmpdir, name='localhost')
            store = os.path.join(tmpdir, 'auth.json')
            with open(store, 'w') as fh:
                json.dump({'users': {}}, fh)
            with mock.patch.dict(os.environ,
                                 {'PARENTAL_CONTROLS_AUTH': store}):
                import tls_serve
                port = self.free_port()
                server = tls_serve.build_server('127.0.0.1', port, cert,
                                                key)
                thread = threading.Thread(target=server.start, daemon=True)
                thread.start()
                self.addCleanup(server.stop)
                self.addCleanup(thread.join, 10)
                ctx = ssl._create_unverified_context()
                status, location = None, None
                for _ in range(200):
                    try:
                        conn = http.client.HTTPSConnection(
                            '127.0.0.1', port, context=ctx, timeout=5)
                        conn.request('GET', '/')
                        resp = conn.getresponse()
                        status = resp.status
                        location = resp.getheader('Location')
                        resp.read()
                        conn.close()
                        break
                    except (OSError, ssl.SSLError):
                        time.sleep(0.05)
                self.assertEqual(status, 302)
                self.assertIn('/login', location or '')
                try:
                    conn = http.client.HTTPConnection(
                        '127.0.0.1', port, timeout=5)
                    conn.request('GET', '/')
                    resp = conn.getresponse()
                    resp.read()
                    conn.close()
                    self.assertNotEqual(resp.status, 302,
                                        'plain HTTP must not serve the app')
                except Exception:
                    pass  # refused/reset: the expected TLS-only outcome


if __name__ == '__main__':
    unittest.main()
