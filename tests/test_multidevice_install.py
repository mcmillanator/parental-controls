#!/usr/bin/env python3
"""Phase-1 multi-device: installer + deployment-artifact tests.

Pure file-content assertions (no installer run, no systemd, no privileged
tools). Proves the `--enable-agent` wiring exists, mints a 0600 token, uses
the loopback-first default, never restarts the panel service, and that the
agent's privileged argv is covered by the existing scoped sudoers templates.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import test_deployment as dep  # noqa: E402

ROOT = os.path.dirname(HERE)

AGENT_UNIT = 'systemd/parental-controls-agent.service'


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def agent_section():
    """The install.sh `== 5c/8 remote agent ==` section text."""
    text = read('install.sh')
    start = text.index('== 5c/8 remote agent')
    end = text.index('== 6/8 firewall', start)
    return text[start:end]


class TestInstallerAgentWiring(unittest.TestCase):
    def test_flag_parsed(self):
        self.assertIn('--enable-agent) ENABLE_AGENT=1', read('install.sh'))

    def test_agent_bind_and_port_flags(self):
        text = read('install.sh')
        self.assertIn('--agent-bind)', text)
        self.assertIn('--agent-port)', text)
        self.assertIn('--agent-insecure-lan)', text)

    def test_defaults_are_loopback(self):
        text = read('install.sh')
        self.assertIn('AGENT_BIND_HOST="127.0.0.1"', text)
        self.assertIn('AGENT_BIND_PORT="5001"', text)

    def test_preflights_agent_sources(self):
        text = read('install.sh')
        for name in ('agent.py', 'wsgi_agent.py', 'device_policy.py',
                     'remote.py'):
            self.assertIn(name, text)

    def test_refuses_plain_lan_agent_bind_without_flag(self):
        text = read('install.sh')
        self.assertIn(
            'refusing a non-loopback --agent-bind without --agent-insecure-lan',
            text)

    def test_mints_token_with_strong_entropy(self):
        section = agent_section()
        self.assertIn('secrets.token_urlsafe(32)', section)

    def test_token_file_is_0600(self):
        section = agent_section()
        self.assertIn('AGENT_TOKEN_FILE="/etc/parental-controls/agent_token"',
                      read('install.sh'))
        self.assertIn('install -m 0600 -o "$SERVICE_USER" '
                      '-g "$SERVICE_USER"', section)
        self.assertIn('"$AGENT_TOKEN_FILE"', section)

    def test_token_write_stays_inside_temp_dir(self):
        # The installer only ever redirects into $TMPD (test_service_user
        # invariant); the token is minted to $TMPD then installed.
        self.assertNotIn('> "$AGENT_TOKEN_FILE"', read('install.sh'))

    def test_token_never_rotates_on_rerun(self):
        section = agent_section()
        self.assertIn('already present', section)

    def test_renders_agent_unit(self):
        section = agent_section()
        self.assertIn('@AGENT_BIND_HOST@', section)
        self.assertIn('@AGENT_BIND_PORT@', section)
        self.assertIn('parental-controls-agent.service', section)

    def test_enables_only_the_agent(self):
        section = agent_section()
        self.assertIn('systemctl enable --now "$AGENT_UNIT"', section)

    def test_agent_section_does_not_restart_panel(self):
        # Hard rule: never restart parental-controls.service.
        self.assertNotIn('restart parental-controls.service', agent_section())


class TestAgentUnitTemplate(unittest.TestCase):
    def test_uses_render_placeholders(self):
        text = read(AGENT_UNIT)
        for token in ('@APP_DIR@', '@SERVICE_USER@', '@AGENT_BIND_HOST@',
                      '@AGENT_BIND_PORT@'):
            self.assertIn(token, text)

    def test_no_checkout_path_hardcoded(self):
        text = read(AGENT_UNIT)
        self.assertNotIn('Documents/Projects', text)
        self.assertNotIn('/home/stan', text)

    def test_runs_agent_wsgi_and_restarts(self):
        text = read(AGENT_UNIT)
        self.assertIn('wsgi_agent:application', text)
        self.assertIn('waitress-serve', text)
        self.assertIn('Restart=always', text)

    def test_token_env_points_at_0600_store(self):
        self.assertIn(
            'Environment=PARENTAL_CONTROLS_AGENT_TOKEN_FILE='
            '/etc/parental-controls/agent_token', read(AGENT_UNIT))

    def test_hardening_present(self):
        text = read(AGENT_UNIT)
        self.assertIn('ProtectSystem=strict', text)
        self.assertIn('PrivateTmp=true', text)

    def test_no_new_sudoers_surface(self):
        # The agent reuses the existing scoped grants; no new sudoers file.
        self.assertFalse(os.path.exists(
            os.path.join(ROOT, 'sudoers-parental-controls-agent')))


class TestAgentPrivilegedArgvCovered(unittest.TestCase):
    """Every `sudo` argv in agent.py must be authorized by the templates.

    Mirrors test_deployment.TestSudoersCoversTheCode for the new module so a
    privileged command the agent runs cannot escape the scoped allowlist.
    """

    def test_every_agent_sudo_call_is_authorized(self):
        argvs = dep.code_argvs('agent.py')
        self.assertTrue(argvs, 'scanner found no sudo argv in agent.py')
        for lineno, argv in argvs:
            with self.subTest(line=lineno, argv=' '.join(argv)):
                self.assertTrue(
                    dep.authorized_by_template(argv)
                    or dep._unreachable_degenerate(argv),
                    f'agent.py:{lineno} runs `sudo {" ".join(argv[1:])}` '
                    'which no sudoers template authorizes')

    def test_new_modules_have_no_checkout_path(self):
        for rel in ('agent.py', 'wsgi_agent.py', 'remote.py',
                    'device_policy.py'):
            with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
                self.assertNotIn('Documents/Projects', fh.read())


if __name__ == '__main__':
    unittest.main()
