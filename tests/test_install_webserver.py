#!/usr/bin/env python3
"""install.sh --deploy-webserver tests.

Hermetic by construction: tls_lib.sh helpers run sourced in bash (the same
functions the installer uses), unit rendering replays install.sh's exact
pipeline (sed + tls_apply_variant + tls_apply_webserver) in a temp dir,
and install.sh itself is pinned by text unless --dry-run support is
probed — which never changes anything on the box (every write goes
through run()/as_user()). firewall-cmd/systemctl are never executed; the
privileged-port binding itself is a live-box checklist item, not a test.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TLS_LIB = os.path.join(ROOT, 'tls_lib.sh')

PANEL_UNIT = os.path.join(ROOT, 'systemd', 'parental-controls.service')
HEALTH_UNIT = os.path.join(ROOT, 'systemd',
                           'parental-controls-healthcheck.service')
CERT = '/etc/parental-controls/tls/server.crt'
KEY = '/etc/parental-controls/tls/server.key'
APP_DIR = '/opt/parental-controls'
USER = 'pcverify'

HAS_SYSTEMCTL = shutil.which('systemctl') is not None
HAS_VISUDO = shutil.which('visudo') is not None
IS_ROOT = os.geteuid() == 0


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def lib(func, *args):
    """Run a tls_lib.sh helper: lib('tls_webserver_conflicts', ...)."""
    return subprocess.run(
        ['bash', '-c', 'source "$0"; "%s" "${@:1}"' % func, TLS_LIB, *args],
        capture_output=True, text=True, timeout=60)


def render_unit(template, host, port, mode, health_tls='',
                redirect_port=0, need_caps=0, webserver_pass=True):
    """install.sh's render pipeline for a unit file, returned as text.

    webserver_pass=False mirrors units other than parental-controls.service
    (install.sh never runs tls_apply_webserver on those).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, 'unit')
        chain = (
            'source "$0"; sed'
            ' -e "s|@APP_DIR@|$2|g" -e "s|@SERVICE_USER@|$3|g"'
            ' -e "s|@BIND_HOST@|$4|g" -e "s|@BIND_PORT@|$5|g"'
            ' "$6" > "$7"'
            ' && tls_apply_variant "$7" "$8" "$2" "$4" "$5" "$9" "${10}"'
            ' "${11}"')
        if webserver_pass:
            chain += ' && tls_apply_webserver "$7" "${12}" "${13}"'
        proc = subprocess.run(
            ['bash', '-c', chain + ' && cat "$7"', TLS_LIB, TLS_LIB,
             APP_DIR, USER, host, port, template, out, mode, CERT, KEY,
             health_tls, str(redirect_port), str(need_caps)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout


def golden_render(template_rel, host, port, mode='loopback'):
    """The pre-webserver rendering for the PLACEHOLDER-DELETION modes
    (loopback; insecure via its env line): value substitution plus dropping
    @TLS_EXECSTART@ (and @TLS_ENV_LINE@ for loopback), built from the
    template text independently of tls_lib.sh."""
    template = read(template_rel)
    lines = []
    for line in template.splitlines():
        if line.strip() == '@TLS_EXECSTART@':
            continue
        if line.strip() == '@TLS_ENV_LINE@':
            if mode == 'loopback':
                continue
            lines.append('Environment=PARENTAL_INSECURE_LAN=1')
            continue
        lines.append(line)
    text = '\n'.join(lines)
    for placeholder, value in (('@APP_DIR@', APP_DIR),
                               ('@SERVICE_USER@', USER),
                               ('@BIND_HOST@', host),
                               ('@BIND_PORT@', port)):
        text = text.replace(placeholder, value)
    return text


class TestWebserverConflicts(unittest.TestCase):
    """tls_webserver_conflicts: the hard-error matrix (functional)."""

    def _expect(self, rc, args, needle):
        proc = lib('tls_webserver_conflicts', *args)
        self.assertEqual(proc.returncode, rc,
                         'args=%r stderr=%s' % (args, proc.stderr))
        if needle:
            self.assertIn(needle, proc.stderr)

    def test_all_implicit_passes(self):
        self._expect(0, ('0', '127.0.0.1', '0', '5000', '0'), '')

    def test_matching_explicit_bind_and_port_pass(self):
        self._expect(0, ('1', '0.0.0.0', '1', '443', '0'), '')

    def test_conflicting_explicit_bind_rejected(self):
        self._expect(1, ('1', '127.0.0.1', '0', '5000', '0'),
                     'implies --bind 0.0.0.0')

    def test_conflicting_explicit_port_rejected(self):
        self._expect(1, ('0', '127.0.0.1', '1', '8443', '0'),
                     'implies --port 443')

    def test_insecure_lan_rejected(self):
        self._expect(1, ('0', '127.0.0.1', '0', '5000', '1'),
                     'conflicts with --insecure-lan')

    def test_tls_and_open_firewall_alongside_are_no_ops(self):
        # No separate flags exist for those in the gate's inputs: the mode
        # simply proceeds (TLS_MODE=1 / OPEN_FIREWALL set twice), pinned
        # textually in TestInstallerWiring below.
        self._expect(0, ('0', '127.0.0.1', '0', '5000', '0'), '')


class TestWebserverUnitRender(unittest.TestCase):
    """The rendered panel unit per mode (full install.sh pipeline)."""

    WEB_EXECSTART = (
        "ExecStart=/bin/bash -c 'cd %s && . .venv/bin/activate && exec "
        "python %s/tls_serve.py --host=0.0.0.0 --port=443 --cert=%s "
        "--key=%s --http-port 80'" % (APP_DIR, APP_DIR, CERT, KEY))

    def test_webserver_mode_full_render(self):
        rendered = render_unit(PANEL_UNIT, '0.0.0.0', '443', 'tls',
                               health_tls=' --tls',
                               redirect_port=80, need_caps=1)
        self.assertIn(self.WEB_EXECSTART, rendered)
        self.assertIn('AmbientCapabilities=CAP_NET_BIND_SERVICE', rendered)
        self.assertIn('CapabilityBoundingSet=', rendered)
        # The bounding set must stay wide enough for the passwordless-sudo
        # enforcement children: dropping CAP_SETUID would break usermod,
        # dropping CAP_SYS_ADMIN would break cgroup writes (AGENTS.md).
        bounding = re.search(r'(?m)^CapabilityBoundingSet=(.*)$',
                             rendered).group(1)
        for cap in ('CAP_NET_BIND_SERVICE', 'CAP_SETUID', 'CAP_SETGID',
                    'CAP_SETPCAP', 'CAP_DAC_OVERRIDE', 'CAP_KILL',
                    'CAP_SYS_ADMIN'):
            self.assertIn(cap, bounding)
        self.assertIn('Environment=PARENTAL_TLS=1', rendered)
        self.assertNotRegex(rendered, r'@[A-Z_]+@')
        self.assertNotIn('waitress-serve', rendered)
        # capabilities land in the hardening block, after LockPersonality
        self.assertLess(rendered.index('LockPersonality=true'),
                        rendered.index('AmbientCapabilities='))

    def test_privileged_port_without_the_flag_gets_caps_only(self):
        # --tls --port 443 (no --deploy-webserver): binds 443, so it needs
        # the capability, but it never gets the redirector argv.
        rendered = render_unit(PANEL_UNIT, '0.0.0.0', '443', 'tls',
                               health_tls=' --tls',
                               redirect_port=0, need_caps=1)
        self.assertIn('AmbientCapabilities=CAP_NET_BIND_SERVICE', rendered)
        self.assertNotIn('--http-port', rendered)
        self.assertIn("--key=%s'" % KEY, rendered)

    def test_unprivileged_tls_port_renders_today_execstart(self):
        # --tls --port 5000/8443: exactly today's ExecStart, no caps, no
        # redirector — the byte-for-byte pre-webserver TLS rendering.
        for port in ('5000', '8443'):
            with self.subTest(port=port):
                rendered = render_unit(PANEL_UNIT, '0.0.0.0', port, 'tls',
                                       health_tls=' --tls',
                                       redirect_port=0, need_caps=0)
                self.assertIn(
                    "ExecStart=/bin/bash -c 'cd %s && . .venv/bin/activate"
                    " && exec python %s/tls_serve.py --host=0.0.0.0 "
                    "--port=%s --cert=%s --key=%s'" % (
                        APP_DIR, APP_DIR, port, CERT, KEY), rendered)
                self.assertIn('Environment=PARENTAL_TLS=1', rendered)
                self.assertNotIn('CAP_NET_BIND_SERVICE', rendered)
                self.assertNotIn('--http-port', rendered)
                self.assertNotRegex(rendered, r'@[A-Z_]+@')

    def test_default_loopback_unit_is_byte_identical(self):
        # The golden spec: a plain (127.0.0.1:5000) install renders exactly
        # the unit install.sh rendered before --deploy-webserver existed.
        rendered = render_unit(PANEL_UNIT, '127.0.0.1', '5000', 'loopback',
                               redirect_port=0, need_caps=0)
        self.assertEqual(rendered.strip(),
                         golden_render('systemd/parental-controls.service',
                                       '127.0.0.1', '5000').strip())
        self.assertNotRegex(
            rendered, r'(AmbientCapabilities|CapabilityBoundingSet|'
            r'CAP_NET_BIND_SERVICE|--http-port)')
        self.assertIn('waitress-serve', rendered)

    def test_insecure_unit_is_byte_identical(self):
        # insecure (plain-LAN) installs also carry no webserver lines even
        # when the bind port is privileged (the pass is simply not asked).
        rendered = render_unit(PANEL_UNIT, '0.0.0.0', '80', 'insecure',
                               redirect_port=0, need_caps=0)
        self.assertEqual(rendered.strip(),
                         golden_render('systemd/parental-controls.service',
                                       '0.0.0.0', '80',
                                       mode='insecure').strip())

    def test_healthcheck_unit_never_gets_the_webserver_pass(self):
        # install.sh scopes tls_apply_webserver to parental-controls.service;
        # the probe keeps --tls and carries no capability lines.
        script = read('install.sh')
        self.assertIn('if [ "$unit" = "parental-controls.service" ]; then',
                      script)
        pass_idx = script.index('tls_apply_webserver "$TMPD/$unit"')
        guard_idx = script.index('if [ "$unit" = "parental-controls.service" ]')
        self.assertLess(guard_idx, pass_idx)
        # and functionally: rendering it through the variant pass only
        rendered = render_unit(HEALTH_UNIT, '0.0.0.0', '443', 'tls',
                               health_tls=' --tls', webserver_pass=False)
        self.assertIn('healthcheck.sh --host 0.0.0.0 --port 443 --tls',
                      rendered)
        self.assertNotIn('CAP_NET_BIND_SERVICE', rendered)
        self.assertNotIn('--http-port', rendered)


class TestInstallerWiring(unittest.TestCase):
    """install.sh text pins: the flag's semantics are documented and wired.

    install.sh is never executed here beyond --dry-run (separate class).
    """

    def test_flag_is_parsed(self):
        self.assertIn('--deploy-webserver) DEPLOY_WEBSERVER=1; shift ;;',
                      read('install.sh'))

    def test_explicit_bind_and_port_are_tracked(self):
        script = read('install.sh')
        self.assertIn('--bind) BIND_HOST="$2"; BIND_EXPLICIT=1; shift 2 ;;',
                      script)
        self.assertIn('--port) BIND_PORT="$2"; PORT_EXPLICIT=1; shift 2 ;;',
                      script)

    def test_implies_the_full_webserver_set(self):
        script = read('install.sh')
        start = script.index('if [ "$DEPLOY_WEBSERVER" -eq 1 ]; then')
        end = re.search(r'(?m)^fi$', script[start:]).start() + start
        block = script[start:end]
        self.assertIn('tls_webserver_conflicts', block)
        self.assertIn('BIND_HOST="0.0.0.0"', block)
        self.assertIn('BIND_PORT="443"', block)
        self.assertIn('TLS_MODE=1', block)
        self.assertIn('HTTP_REDIRECT_PORT=80', block)
        self.assertIn('OPEN_FIREWALL="1"', block)
        # the conflicts gate runs BEFORE any of the implied assignments
        self.assertLess(block.index('tls_webserver_conflicts'),
                        block.index('BIND_HOST="0.0.0.0"'))
        # ...and before the root check, so the failure reason is reachable
        # unprivileged (exit 2, not "Run as root").
        self.assertLess(start, script.index('if [ "$EUID" -ne 0 ]'))

    def test_webserver_still_keeps_tls_from_implying_firewall(self):
        # The test_tls.py `--tls never implies --open-firewall` pin counts
        # the UNQUOTED assignment exactly once; the webserver block uses
        # the quoted assignment, so the pin must hold.
        self.assertEqual(read('install.sh').count('OPEN_FIREWALL=1'), 1)

    def test_never_overrides_cert_reuse_or_tls_name(self):
        # The mode only sets TLS_MODE=1; the existing cert-reuse/--tls-name
        # logic in step 2b stays the sole path for certificate decisions.
        script = read('install.sh')
        start = script.index('if [ "$DEPLOY_WEBSERVER" -eq 1 ]; then')
        end = re.search(r'(?m)^fi$', script[start:]).start() + start
        for line in script[start:end].splitlines():
            self.assertNotIn('REGEN', line)
            self.assertNotIn('TLS_NAME', line)
            self.assertNotIn('TLS_CERT', line)

    def test_capabilities_gate_covers_both_ports(self):
        script = read('install.sh')
        start = script.index('NEED_NET_CAPS=0')
        end = script.index('done', start)
        region = script[start:end]
        self.assertIn('"$BIND_PORT" "$HTTP_REDIRECT_PORT"', region)
        self.assertIn('-lt 1024', region)

    def test_webserver_pass_lands_after_the_variant_pass(self):
        script = read('install.sh')
        self.assertLess(script.index('tls_apply_variant "$TMPD/$unit"'),
                        script.index('tls_apply_webserver "$TMPD/$unit"'))

    def test_rendered_summary_shows_capability_lines(self):
        # so a --dry-run can be asserted on the unit lines that matter
        script = read('install.sh')
        self.assertIn('^(User|WorkingDirectory|ExecStart='
                      '|AmbientCapabilities=|CapabilityBoundingSet=)', script)

    def test_firewall_opens_both_webserver_ports(self):
        script = read('install.sh')
        start = script.index('FIREWALL_PORTS=')
        end = script.index('echo "== 7/8', start)
        region = script[start:end]
        self.assertIn('FIREWALL_PORTS=("$BIND_PORT")', region)
        self.assertIn('FIREWALL_PORTS+=("$HTTP_REDIRECT_PORT")', region)
        # duplicate guard (BIND_PORT 80 + redirect 80 cannot double-open)
        self.assertIn('[ "$HTTP_REDIRECT_PORT" != "$BIND_PORT" ]', region)
        self.assertIn('firewall-cmd --permanent --add-port="${fw_port}/tcp"',
                      region)
        self.assertIn('[dry-run] firewall-cmd '
                      '--permanent --add-port=${fw_port}/tcp', region)
        # the opt-in gate still precedes any firewall-cmd invocation
        gate = script.index('if [ "$OPEN_FIREWALL" -eq 1 ]')
        self.assertLess(gate, script.index(
            'firewall-cmd --permanent --add-port="${fw_port}/tcp"'))

    def test_verify_probes_the_redirector(self):
        script = read('install.sh')
        verify = script.split('== 8/8 verify ==', 1)[1].split('Next steps:')[0]
        self.assertIn('http://127.0.0.1:${HTTP_REDIRECT_PORT}/', verify)
        self.assertIn('301 https://', verify)
        self.assertIn('%{http_code} %{redirect_url}', verify)
        self.assertIn('GET http://127.0.0.1:${HTTP_REDIRECT_PORT}/', verify)
        # gated on redirect-port-in-play + tls, never fires on default runs
        self.assertIn('if [ "$HTTP_REDIRECT_PORT" -gt 0 ] && '
                      '[ "$MODE" = "tls" ]; then', verify)
        # and the captured curl stays failure-swallowed (set -e safety)
        self.assertRegex(verify,
                         r'redir_probe="\$\(curl[\s\S]*\|\| true\)"')

    def test_help_block_documents_the_flag(self):
        head = '\n'.join(read('install.sh').splitlines()[:60])
        self.assertIn('--deploy-webserver', head)
        for phrase in ('0.0.0.0', '443', '301', '80/tcp + 443/tcp',
                       'INTENTIONALLY implies', 'trust it on first use',
                       'byte-identical'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, head)

    def test_next_steps_mentions_the_redirect(self):
        script = read('install.sh')
        self.assertIn('301-redirects every request to https', script)


@unittest.skipUnless(HAS_SYSTEMCTL and HAS_VISUDO and IS_ROOT,
                     'full installer dry-run needs root + systemctl + visudo')
class TestInstallerDryRun(unittest.TestCase):
    """`install.sh --dry-run` end-to-end (prints only; run() guards every
    write). Skips in containers without systemd (the authoritative VM box
    executes this)."""

    def _run(self, *flags):
        env = dict(os.environ, TMPDIR=os.path.join(ROOT, '.scratch'))
        os.makedirs(env['TMPDIR'], exist_ok=True)
        return subprocess.run(['./install.sh', '--dry-run', *flags],
                              cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=300)

    def _unit_section(self, out):
        start = out.index('--- rendered parental-controls.service ---')
        end = out.index('--- rendered', start + 10)
        return out[start:end]

    def test_webserver_dry_run_renders_the_new_unit(self):
        proc = self._run('--deploy-webserver', '--tls-name', '10.0.0.1',
                         '--install-dir',
                         os.path.join(ROOT, '.scratch', 'dryrun-install'))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        section = self._unit_section(proc.stdout)
        self.assertIn('--http-port 80', section)
        self.assertIn('AmbientCapabilities=CAP_NET_BIND_SERVICE', section)
        self.assertIn('--port=443', section)
        # firewall dry-run lines name both ports, once each
        self.assertIn('add-port=443/tcp', proc.stdout)
        self.assertIn('add-port=80/tcp', proc.stdout)
        self.assertEqual(proc.stdout.count('add-port=80/tcp'), 1)

    def test_default_dry_run_is_free_of_the_webserver_lines(self):
        proc = self._run('--install-dir',
                         os.path.join(ROOT, '.scratch', 'dryrun-install'))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        section = self._unit_section(proc.stdout)
        self.assertNotIn('--http-port', section)
        self.assertNotIn('CAP_NET_BIND_SERVICE', section)
        self.assertIn('--host=127.0.0.1 --port=5000', section)
        self.assertIn('(skipped — pass --open-firewall', proc.stdout)


class TestConflictsRunEarly(unittest.TestCase):
    """The conflict gate runs on any box, any privileges: it sits before
    the root check, so exit 2 + reason is always observable — and with
    --dry-run even a broken gate could only ever print."""

    def _run(self, *flags):
        env = dict(os.environ, TMPDIR=os.path.join(ROOT, '.scratch'))
        os.makedirs(env['TMPDIR'], exist_ok=True)
        return subprocess.run(['./install.sh', '--dry-run', *flags],
                              cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=120)

    def test_conflict_exits_2_with_reason(self):
        for flags, needle in (
                (('--deploy-webserver', '--insecure-lan'),
                 'conflicts with --insecure-lan'),
                (('--deploy-webserver', '--bind', '127.0.0.1'),
                 'implies --bind 0.0.0.0'),
                (('--deploy-webserver', '--port', '8443'),
                 'implies --port 443')):
            with self.subTest(flags=flags):
                proc = self._run(*flags)
                self.assertEqual(proc.returncode, 2,
                                 proc.stdout + proc.stderr)
                self.assertIn(needle, proc.stderr)
                self.assertNotIn('systemctl', proc.stderr)


if __name__ == '__main__':
    unittest.main()
