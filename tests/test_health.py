#!/usr/bin/env python3
"""Health-check deployment + behaviour tests.

Covers the silent-breakage classes the probe exists for:

1. A wedged panel is invisible: `Restart=always` restarts a crash but nothing
   notices a process that stays up and stops serving. A timer must probe it.
2. Deployment artifacts that do not render leave the box with no monitoring at
   all: an unsubstituted `@APP_DIR@`/`@BIND_PORT@` makes ExecStart fail and the
   timer quietly probe nothing (mirrors tests/test_deployment.py style).
3. `systemctl --failed` is the whole alerting mechanism, so a probe that exits
   0 while the unit is down (or that probes the wrong port/status) is worse
   than no probe: it silences the alarm.
4. The probe must never touch accounts or the protected service: no restart,
   no stop, no notification path of any kind.

Static assertions plus a real run of healthcheck.sh against throwaway HTTP
servers and a stand-in `systemctl` on PATH. Nothing installed, no real unit
touched: the live parental-controls.service is never queried (every run passes
--unit pc-test.service plus a shim), no account is touched, nothing is written
outside the test temp dir.
"""

import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, 'healthcheck.sh')
SERVICE_UNIT = 'systemd/parental-controls-healthcheck.service'
TIMER_UNIT = 'systemd/parental-controls-healthcheck.timer'

# What install.sh substitutes (kept in step with its sed line).
SAMPLE_RENDER = {
    '@APP_DIR@': '/opt/parental-controls',
    '@SERVICE_USER@': 'pcsuser',
    '@BIND_HOST@': '127.0.0.1',
    '@BIND_PORT@': '5051',
}

NOTIFY_TOKENS = ('notify-send', 'systemd-run', 'zenity', 'wall ', 'wall\t',
                 'dbus-send', 'logger ')


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def render(text):
    for placeholder, value in SAMPLE_RENDER.items():
        text = text.replace(placeholder, value)
    return text


def code_only(text):
    """Drop comment and blank lines (notes may name forbidden things)."""
    return '\n'.join(l for l in text.splitlines()
                     if l.strip() and not l.strip().startswith('#'))


def free_port():
    """A port nothing is listening on (best effort; must stay free)."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers every GET with a fixed (code, Location) pair.

    No timeout on the socket on purpose: the hung-service test needs a server
    that accepts the connection and never finishes its response, which is what
    a wedged WSGI worker looks like from the probe's side.
    """

    protocol_version = 'HTTP/1.0'
    code = 302
    location = '/login?next=/'
    delay = 0.0

    def do_GET(self):
        if self.delay:
            time.sleep(self.delay)
        self.send_response(self.code)
        if self.location:
            self.send_header('Location', self.location)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


class ProbeServer:
    """Context manager running _Handler on a loopback port."""

    def __init__(self, code=302, location='/login?next=/', delay=0.0):
        handler = type('BoundHandler', (_Handler,),
                       {'code': code, 'location': location, 'delay': delay})
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def systemctl_shim(directory, state, rc=0, unqueryable=False):
    """A fake `systemctl` that only answers `is-active`.

    Put its directory first on PATH. Lets the probe be tested without any real
    unit being created, started, stopped or restarted. `unqueryable` mimics a
    manager the probe cannot talk to at all (no stdout state, error on stderr).
    """
    bindir = os.path.join(directory, 'shimbin')
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, 'systemctl')
    with open(path, 'w', encoding='utf-8') as fh:
        if unqueryable:
            fh.write('#!/bin/sh\necho "Failed to connect to bus: '
                     'No such file or directory" >&2\nexit 1\n')
        else:
            fh.write('#!/bin/sh\nprintf \'%s\\n\' "' + state + '"\nexit '
                     + str(rc) + '\n')
    os.chmod(path, 0o755)
    return bindir


def run_probe(*args, state='active', env=None, timeout=60,
              unqueryable=False):
    """Run healthcheck.sh with a shimmed systemctl -> CompletedProcess.

    --unit defaults to a stand-in name so no test can ever query (let alone
    act on) the live parental-controls.service; pass --unit yourself to
    override it.
    """
    if '--unit' not in args:
        args = ('--unit', 'pc-test.service', *args)
    tmpdir = tempfile.mkdtemp(prefix='pc-health-')
    try:
        bindir = systemctl_shim(tmpdir, state, unqueryable=unqueryable)
        environ = dict(os.environ)
        if env:
            environ.update(env)
        environ['PATH'] = bindir + os.pathsep + environ['PATH']
        return subprocess.run(
            ['/bin/bash', SCRIPT, *args], capture_output=True, text=True,
            timeout=timeout, env=environ, cwd=tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestUnitsRender(unittest.TestCase):
    """The new units must render exactly like the ones already deployed."""

    def test_unit_files_exist(self):
        for rel in (SERVICE_UNIT, TIMER_UNIT):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)

    def test_render_leaves_no_placeholder(self):
        for rel in (SERVICE_UNIT, TIMER_UNIT):
            body = render(read(rel))
            self.assertNotRegex(body, r'@[A-Z_]+@',
                                f'{rel} still has a placeholder after render')

    def test_service_probes_the_rendered_bind(self):
        template = read(SERVICE_UNIT)
        body = render(template)
        self.assertIn(f'ExecStart=/bin/bash {SAMPLE_RENDER["@APP_DIR@"]}'
                      f'/healthcheck.sh --host {SAMPLE_RENDER["@BIND_HOST@"]}'
                      f' --port {SAMPLE_RENDER["@BIND_PORT@"]}', body)
        # The bind comes from placeholders, never a literal (the installer owns
        # it) -- same rule tests/test_deployment.py pins for the panel unit.
        execstart = next(l for l in template.splitlines()
                         if l.startswith('ExecStart='))
        self.assertIn('@BIND_HOST@', execstart)
        self.assertIn('@BIND_PORT@', execstart)
        self.assertNotIn('127.0.0.1', execstart)
        self.assertNotIn('5000', execstart)

    def test_service_runs_as_the_service_user_oneshot(self):
        body = render(read(SERVICE_UNIT))
        self.assertIn(f'User={SAMPLE_RENDER["@SERVICE_USER@"]}', body)
        self.assertIn(f'WorkingDirectory={SAMPLE_RENDER["@APP_DIR@"]}', body)
        self.assertIn('Type=oneshot', body)
        # Started by its timer only — an [Install] section here would make it
        # run at boot as well.
        self.assertNotRegex(body, r'(?m)^\[Install\]')

    def test_service_keeps_the_hardening_style(self):
        body = render(read(SERVICE_UNIT))
        for directive in ('ProtectSystem=strict', 'PrivateTmp=true',
                          'ProtectKernelTunables=true',
                          'ProtectKernelModules=true',
                          'ProtectHostname=true', 'ProtectClock=true',
                          'LockPersonality=true', 'NoNewPrivileges=true'):
            self.assertIn(directive, body)

    def test_service_has_a_timeout_so_a_wedged_probe_still_fails(self):
        body = render(read(SERVICE_UNIT))
        m = re.search(r'^TimeoutStartSec=(\d+)$', body, re.M)
        self.assertIsNotNone(m, 'TimeoutStartSec pins the probe runtime')
        self.assertGreaterEqual(int(m.group(1)), 15)

    def test_timer_is_enabled_and_points_at_the_probe_unit(self):
        timer = read(TIMER_UNIT)
        self.assertIn('Unit=parental-controls-healthcheck.service', timer)
        self.assertIn('WantedBy=timers.target', timer)
        self.assertIn('OnBootSec=', timer)
        self.assertIn('OnUnitActiveSec=5min', timer)

    def test_timer_interval_is_not_more_than_five_minutes(self):
        timer = read(TIMER_UNIT)
        seconds = {'s': 1, 'min': 60, 'h': 3600}
        interval = re.search(r'^OnUnitActiveSec=(\d+)([a-z]+)$', timer, re.M)
        self.assertIsNotNone(interval)
        span = int(interval.group(1)) * seconds[interval.group(2)]
        self.assertLessEqual(span, 300)

    def test_execstart_binaries_exist_on_this_box(self):
        body = render(read(SERVICE_UNIT))
        # ExecStart=/bin/bash <install dir>/healthcheck.sh --host H --port P
        parts = next(l for l in body.splitlines()
                     if l.startswith('ExecStart=')).split()
        interpreter = parts[0].split('=', 1)[1]
        script = parts[1]
        self.assertTrue(os.path.exists(interpreter),
                        f'{interpreter} does not exist')
        # The script ships from the checkout root; the install dir only has it
        # after install.sh has run.
        self.assertEqual(script, os.path.join(SAMPLE_RENDER['@APP_DIR@'],
                                              'healthcheck.sh'))
        self.assertTrue(os.path.isfile(SCRIPT),
                        'healthcheck.sh is missing from the checkout')

    def test_referenced_script_ships_to_the_install_dir(self):
        """The unit runs @APP_DIR@/healthcheck.sh, so the script must be
        staged by the installer's sync (which copies the checkout root)."""
        self.assertTrue(os.path.isfile(SCRIPT))
        self.assertIn('"$SOURCE_DIR/" "$INSTALL_DIR/"', read('install.sh'))

    def test_systemd_analyze_accepts_the_rendered_units(self):
        if not shutil.which('systemd-analyze'):
            self.skipTest('systemd-analyze not available')
        tmpdir = tempfile.mkdtemp(prefix='pc-health-units-')
        try:
            paths = []
            for rel in (SERVICE_UNIT, TIMER_UNIT):
                path = os.path.join(tmpdir, os.path.basename(rel))
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write(render(read(rel)).replace(
                        SAMPLE_RENDER['@APP_DIR@'], ROOT))
                paths.append(path)
            proc = subprocess.run(['systemd-analyze', 'verify', *paths],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0,
                             f'{proc.stdout}{proc.stderr}')
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestNoHardcodedPaths(unittest.TestCase):
    def test_no_checkout_home_paths(self):
        bad = re.compile(r'(/home/|Documents/Projects)')
        for rel in ('healthcheck.sh', SERVICE_UNIT, TIMER_UNIT):
            self.assertNotRegex(read(rel), bad, f'{rel} names a checkout path')


class TestInstallerWiring(unittest.TestCase):
    """install.sh must install and enable the self-monitoring units."""

    UNITS = ('parental-controls.service', 'parental-controls-enforce.service',
             'parental-controls-enforce.timer',
             'parental-controls-healthcheck.service',
             'parental-controls-healthcheck.timer')

    def test_render_loop_installs_every_unit(self):
        script = read('install.sh')
        loop = re.search(r'^for unit in (.*?); do$', script, re.M | re.S)
        self.assertIsNotNone(loop, 'installer must keep a unit render loop')
        for unit in self.UNITS:
            self.assertIn(unit, loop.group(1),
                          f'{unit} is not installed by install.sh')

    def test_bind_placeholders_are_substituted_for_all_units(self):
        script = read('install.sh')
        for placeholder in ('@APP_DIR@', '@SERVICE_USER@', '@BIND_HOST@',
                            '@BIND_PORT@'):
            self.assertIn(f's|{placeholder}|', script)

    def test_timer_is_enabled_at_install_time(self):
        script = read('install.sh')
        self.assertIn('systemctl enable --now '
                      'parental-controls-healthcheck.timer', script)

    def test_verify_step_reports_the_timer_and_runs_a_probe(self):
        script = read('install.sh')
        verify = script.split('== 8/8 verify ==', 1)[-1]
        self.assertIn('systemctl is-active -q '
                      'parental-controls-healthcheck.timer', verify)
        self.assertIn('systemctl start --wait --quiet '
                      'parental-controls-healthcheck.service', verify)

    def test_summary_tells_the_parent_where_failures_surface(self):
        script = read('install.sh')
        summary = script.split('Installed into', 1)[-1]
        self.assertIn('journalctl -u parental-controls-healthcheck.service',
                      summary)
        self.assertIn('systemctl --failed', summary)


class TestProbeIsSafe(unittest.TestCase):
    """The probe reports; it must never act on an account or the panel."""

    FORBIDDEN = re.compile(
        r'\bsystemctl\s+(restart|stop|disable|mask|kill|start)\b')

    def test_never_restarts_or_stops_anything(self):
        for rel in ('healthcheck.sh', SERVICE_UNIT, TIMER_UNIT):
            self.assertNotRegex(code_only(read(rel)), self.FORBIDDEN,
                                f'{rel} tries to act on a unit')

    def test_never_notifies_anyone(self):
        for rel in ('healthcheck.sh', SERVICE_UNIT, TIMER_UNIT):
            body = code_only(read(rel))
            for token in NOTIFY_TOKENS:
                self.assertNotIn(token, body,
                                 f'{rel} has a notification path ({token})')

    def test_probe_only_queries_the_unit_state(self):
        """The only systemctl verb the probe may use is read-only."""
        code = code_only(read('healthcheck.sh'))
        # `command -v systemctl` is an existence check, not a call.
        code = '\n'.join(l for l in code.splitlines()
                         if 'command -v systemctl' not in l)
        calls = re.findall(r'systemctl\s+([a-z-]+)', code)
        self.assertTrue(calls, 'the probe must check the unit somehow')
        for verb in calls:
            self.assertIn(verb, ('is-active', 'is-enabled'),
                          f'probe runs `systemctl {verb}`')

    def test_probe_script_is_syntactically_valid(self):
        proc = subprocess.run(['/bin/bash', '-n', SCRIPT],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_unknown_arguments_exit_two(self):
        proc = run_probe('--bogus')
        self.assertEqual(proc.returncode, 2)
        self.assertIn('unknown argument', proc.stderr)

    def test_invalid_port_exits_two(self):
        proc = run_probe('--port', 'not-a-port')
        self.assertEqual(proc.returncode, 2)
        self.assertIn('invalid --port', proc.stderr)


class TestProbeReportsHealthy(unittest.TestCase):
    def test_login_redirect_is_healthy(self):
        with ProbeServer(code=302, location='/login?next=/') as srv:
            proc = run_probe('--host', '127.0.0.1', '--port', str(srv.port))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('healthcheck OK', proc.stdout)
        self.assertIn('302', proc.stdout)
        self.assertIn('pc-test.service active', proc.stdout)

    def test_unqueryable_systemd_degrades_to_the_http_probe(self):
        """A manager the probe cannot query must not cry wolf forever."""
        with ProbeServer() as srv:
            proc = run_probe('--host', '127.0.0.1', '--port', str(srv.port),
                             unqueryable=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('could not query the state of pc-test.service',
                      proc.stderr)
        self.assertNotIn('HEALTHCHECK FAILED', proc.stderr)

    def test_plain_200_is_healthy(self):
        with ProbeServer(code=200, location=None) as srv:
            proc = run_probe('--host', '127.0.0.1', '--port', str(srv.port))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_wildcard_bind_is_probed_over_loopback(self):
        # 0.0.0.0 is not a usable probe target (healthcheck.sh maps it).
        with ProbeServer() as srv:
            proc = run_probe('--host', '0.0.0.0', '--port', str(srv.port))
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestProbeReportsUnhealthy(unittest.TestCase):
    """Every silent-breakage class exits non-zero with a reason."""

    def assertFailed(self, proc, needle):
        self.assertEqual(proc.returncode, 1, f'{proc.stdout}{proc.stderr}')
        self.assertIn('HEALTHCHECK FAILED', proc.stderr)
        self.assertIn(needle, proc.stderr)

    def test_stopped_service(self):
        proc = run_probe(state='inactive')
        self.assertFailed(proc, "pc-test.service is 'inactive'")

    def test_failed_service(self):
        proc = run_probe(state='failed')
        self.assertFailed(proc, 'failed')

    def test_crash_loop_parked_in_activating(self):
        proc = run_probe(state='activating')
        self.assertFailed(proc, 'activating')

    def test_port_with_nothing_listening(self):
        proc = run_probe('--host', '127.0.0.1', '--port', str(free_port()))
        self.assertFailed(proc, 'nothing is listening')

    def test_hung_process_times_out_instead_of_blocking_forever(self):
        with ProbeServer(delay=30) as srv:
            started = time.monotonic()
            proc = run_probe('--host', '127.0.0.1', '--port', str(srv.port),
                             '--timeout', '2')
            elapsed = time.monotonic() - started
        self.assertFailed(proc, 'within 2s')
        self.assertLess(elapsed, 30, 'probe hung on an unresponsive panel')

    def test_server_error_status_is_unhealthy(self):
        # What a panel with a broken auth store returns to a real request.
        with ProbeServer(code=500, location=None) as srv:
            proc = run_probe('--host', '127.0.0.1', '--port', str(srv.port))
        self.assertFailed(proc, 'HTTP 500')

    def test_redirect_somewhere_other_than_login_is_unhealthy(self):
        with ProbeServer(code=302, location='/captive-portal') as srv:
            proc = run_probe('--host', '127.0.0.1', '--port', str(srv.port))
        self.assertFailed(proc, 'expected the login redirect')

    def test_unqueryable_systemd_still_fails_when_the_port_is_dead(self):
        proc = run_probe('--host', '127.0.0.1', '--port', str(free_port()),
                         unqueryable=True)
        self.assertFailed(proc, 'nothing is listening')


class TestProbeAuthStore(unittest.TestCase):
    """The one thing the HTTP probe cannot see: the auth store's health."""

    def _store(self, body, tmpdir, name='auth.json'):
        path = os.path.join(tmpdir, name)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(body)
        return path

    def _run(self, path):
        with ProbeServer() as srv:
            return run_probe('--host', '127.0.0.1', '--port', str(srv.port),
                             '--auth-store', path)

    def test_corrupt_store_is_unhealthy_even_though_the_port_answers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = self._run(self._store('{"users": {', tmpdir))
        self.assertEqual(proc.returncode, 1, f'{proc.stdout}{proc.stderr}')
        self.assertIn('HEALTHCHECK FAILED', proc.stderr)
        self.assertIn('auth store', proc.stderr)

    def test_unreadable_store_is_unhealthy(self):
        if os.geteuid() == 0:
            self.skipTest('root ignores file permissions')
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._store(json.dumps({'users': {}}), tmpdir)
            os.chmod(path, 0o000)
            proc = self._run(path)
        self.assertEqual(proc.returncode, 1, f'{proc.stdout}{proc.stderr}')
        self.assertIn('not readable', proc.stderr)

    def test_valid_store_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._store(json.dumps(
                {'users': {'stan': {'hash': 'x', 'must_change': True}}}),
                tmpdir)
            proc = self._run(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_store_warns_but_does_not_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = self._run(os.path.join(tmpdir, 'absent.json'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('WARNING', proc.stderr)
        self.assertNotIn('HEALTHCHECK FAILED', proc.stderr)


if __name__ == '__main__':
    unittest.main()
