#!/usr/bin/env python3
"""Dedicated hardened service user (`parental-controls`) regression tests.

The Flask panel is network-exposed, so it must not run as the shared
human/agent login account (which carries blanket passwordless root sudo on
dev). The installer deploys it as a dedicated system user instead:
nologin shell, no home, locked password, no sudo of its own — only the
scoped sudoers grants.

Pure file-content assertions plus temp-copy validation (`visudo -c -f`,
`systemd-analyze verify`). Nothing here creates a user, writes /etc,
touches the running service, or restarts anything.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SERVICE_USER = 'parental-controls'

STATE_FILES = ('bedtime.json', 'budgets.json', 'process_prefs.json',
               'auth.json', 'audit.jsonl',
               'state.db', 'state.db-wal', 'state.db-shm',
               'state.db.bak.*')

# Accounts no test or installer path may ever manage; the installer only
# ever creates the dedicated service user, and these must not appear as the
# target of an account-management command.
PROTECTED_ACCOUNTS = ('tester', 'bluefox', 'stan', 'pcagent')


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def render_unit(rel, user=SERVICE_USER):
    """A systemd template as the installer would write it to /etc/systemd."""
    body = read(rel)
    return (body.replace('@APP_DIR@', '/opt/parental-controls')
                .replace('@SERVICE_USER@', user)
                .replace('@BIND_HOST@', '127.0.0.1')
                .replace('@BIND_PORT@', '5000'))


class TestServiceUserDefault(unittest.TestCase):
    def test_default_is_the_dedicated_account(self):
        script = read('install.sh')
        self.assertIn('DEFAULT_SERVICE_USER="parental-controls"', script)

    def test_explicit_flag_or_env_wins(self):
        script = read('install.sh')
        self.assertIn('--user) SERVICE_USER="$2"', script)
        self.assertRegex(
            script,
            r'SERVICE_USER="\$\{SERVICE_USER:-\$DEFAULT_SERVICE_USER\}"')

    def test_never_defaults_to_the_shared_login_account(self):
        script = read('install.sh')
        self.assertNotRegex(script, r'(?m)^SERVICE_USER=.*SUDO_USER')

    def test_empty_service_user_is_still_refused(self):
        script = read('install.sh')
        self.assertIn('Could not determine service user', script)


class TestServiceUserCreation(unittest.TestCase):
    def test_creation_is_guarded_by_existence_check(self):
        script = read('install.sh')
        self.assertIn('if ! id "$SERVICE_USER"', script)
        guard = script.index('if ! id "$SERVICE_USER"')
        self.assertLess(guard, script.index('useradd --system'))

    def test_creation_uses_system_account_flags(self):
        script = read('install.sh')
        self.assertIn('useradd --system --no-create-home --shell', script)

    def test_creation_flags_exist_in_this_useradd(self):
        if shutil.which('useradd') is None:
            self.skipTest('useradd not on PATH')
        out = subprocess.run(['useradd', '--help'], capture_output=True,
                             text=True, timeout=30).stdout
        for flag in ('--system', '--no-create-home', '--shell'):
            self.assertIn(flag, out)

    def test_nologin_shell_with_distribution_fallback(self):
        script = read('install.sh')
        self.assertIn('/usr/sbin/nologin', script)
        self.assertIn('/sbin/nologin', script)
        self.assertTrue(os.path.exists('/usr/sbin/nologin')
                        or os.path.exists('/sbin/nologin'),
                        'no nologin shell on this box')

    def test_password_is_locked(self):
        script = read('install.sh')
        self.assertIn('usermod -L "$SERVICE_USER"', script)

    def test_only_the_default_account_is_auto_created(self):
        script = read('install.sh')
        self.assertIn('!= "$DEFAULT_SERVICE_USER"', script)
        self.assertIn('does not exist', script)

    def test_username_is_valid_for_useradd(self):
        self.assertRegex(SERVICE_USER, r'^[a-z_][a-z0-9_-]*$')
        self.assertLessEqual(len(SERVICE_USER), 32)

    def test_no_account_deletion_or_password_setting(self):
        script = read('install.sh')
        self.assertNotIn('userdel', script)
        self.assertNotIn('chpasswd', script)

    def test_protected_accounts_are_never_managed(self):
        script = read('install.sh')
        for name in PROTECTED_ACCOUNTS:
            self.assertNotRegex(
                script,
                r'\b(useradd|usermod|userdel|passwd|chpasswd|chage)\b[^\n]*\b%s\b' % name,
                f'installer must never manage {name}')


class TestUnitsRunAsServiceUser(unittest.TestCase):
    SERVICES = ('systemd/parental-controls.service',
                'systemd/parental-controls-enforce.service',
                'systemd/parental-controls-healthcheck.service')
    TIMERS = ('systemd/parental-controls-enforce.timer',
              'systemd/parental-controls-healthcheck.timer')

    def test_every_service_runs_as_the_placeholder(self):
        for rel in self.SERVICES:
            with self.subTest(unit=rel):
                users = [l for l in read(rel).splitlines()
                         if l.startswith('User=')]
                self.assertEqual(users, ['User=@SERVICE_USER@'])

    def test_no_service_names_a_real_user(self):
        for rel in self.SERVICES:
            with self.subTest(unit=rel):
                self.assertNotRegex(read(rel), r'(?m)^User=(?!@SERVICE_USER@)')

    def test_no_stan_default_in_units(self):
        for rel in self.SERVICES:
            with self.subTest(unit=rel):
                self.assertNotIn('stan', read(rel))

    def test_timers_carry_no_user(self):
        # systemd timers cannot take User=: the oneshot they trigger does.
        for rel in self.TIMERS:
            with self.subTest(unit=rel):
                self.assertNotRegex(read(rel), r'(?m)^User=')

    def test_rendered_units_run_as_the_dedicated_user(self):
        for rel in self.SERVICES:
            with self.subTest(unit=rel):
                rendered = render_unit(rel)
                users = [l for l in rendered.splitlines()
                         if l.startswith('User=')]
                self.assertEqual(users, [f'User={SERVICE_USER}'])
                self.assertNotIn('@SERVICE_USER@', rendered)

    # logind caps a user at MaxSessions (default 5) class=user sessions.
    # Every sudo the status probes run opens a PAM session through
    # pam_systemd; concurrent probe bursts tripped TooManySessions, making
    # sudo die at session init ("pam_open_session: Permission denied" ->
    # the "unavailable" lock status). pam_systemd(8) honours
    # XDG_SESSION_CLASS from sudo's environment, and background sessions
    # do not count against the cap. See AGENT_CONTEXT "Status right now".
    PROBE_UNITS = ('systemd/parental-controls.service',
                   'systemd/parental-controls-enforce.service',
                   'systemd/parental-controls-agent.service')

    def test_probe_units_register_background_logind_sessions(self):
        for rel in self.PROBE_UNITS:
            with self.subTest(unit=rel):
                self.assertIn('Environment=XDG_SESSION_CLASS=background',
                              read(rel))

    def test_healthcheck_unit_stays_plain(self):
        # It only curls the panel; no sudo children, so no need to touch
        # its session class.
        self.assertNotIn('XDG_SESSION_CLASS',
                         read('systemd/parental-controls-healthcheck.service'))

    def test_no_newprivileges_changes(self):
        # Out of scope for this migration: the panel must keep spawning
        # passwordless sudo children, the healthcheck must keep refusing to.
        panel = read('systemd/parental-controls.service')
        self.assertNotRegex(panel, r'(?m)^NoNewPrivileges=')
        health = read('systemd/parental-controls-healthcheck.service')
        self.assertIn('NoNewPrivileges=true', health)

    def test_rendered_units_pass_systemd_analyze_verify(self):
        if shutil.which('systemd-analyze') is None:
            self.skipTest('systemd-analyze not on PATH')
        tmpd = tempfile.mkdtemp(prefix='pc-units-')
        try:
            for rel in self.SERVICES + self.TIMERS:
                name = os.path.basename(rel)
                with open(os.path.join(tmpd, name), 'w',
                          encoding='utf-8') as fh:
                    fh.write(render_unit(rel))
            for name in ('parental-controls.service',
                         'parental-controls-enforce.service',
                         'parental-controls-healthcheck.service'):
                proc = subprocess.run(
                    ['systemd-analyze', 'verify',
                     os.path.join(tmpd, name)],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(
                    proc.returncode, 0,
                    f'systemd-analyze verify {name} failed: {proc.stderr}')
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)


class TestSudoersRenderAsServiceUser(unittest.TestCase):
    TEMPLATES = ('sudoers-parental-controls',
                 'sudoers-parental-controls-budgets')

    # logind's MaxSessions cap (default 5, class=user) is blown by the app's
    # sudo bursts: every sudo opens a PAM session, pam_systemd registers it
    # with logind, TooManySessions makes pam_open_session fail, and sudo
    # dies before running even a read-only probe (`pam_open_session:
    # Permission denied` -> the "needs help" lock status). The fix is the
    # per-invoking-user Default entry `Defaults:<user> !pam_session`
    # (sudoers(5) — `Defaults:` scopes USERS, `Defaults!` scopes COMMANDS;
    # the pam_session flag exists since sudo 1.8.7). It stops sudo opening
    # PAM sessions for the service user only: auth/account phases and
    # sudo's own command logging are untouched. This supersedes the
    # XDG_SESSION_CLASS=background unit env (kept belt-and-braces; the
    # probe tests above still pin those Environment= lines).
    PAM_SESSION_DEFAULTS = re.compile(
        r'(?m)^Defaults:(?P<users>[^ \t]+) !pam_session[ \t]*$')

    def test_every_rule_line_uses_the_placeholder(self):
        for rel in self.TEMPLATES:
            with self.subTest(template=rel):
                for line in read(rel).splitlines():
                    if line.strip() and not line.strip().startswith('#'):
                        self.assertIn('@SERVICE_USER@', line)

    def test_templates_carry_a_pam_session_defaults_entry(self):
        # Unrendered: the Defaults entry must scope through the placeholder,
        # matching the user sudo is invoked as on the live box.
        for rel in self.TEMPLATES:
            with self.subTest(template=rel):
                match = self.PAM_SESSION_DEFAULTS.search(read(rel))
                self.assertIsNotNone(
                    match,
                    f'{rel} must contain `Defaults:@SERVICE_USER@ '
                    '!pam_session` (see the template comment for why)')
                self.assertEqual(match.group('users'), '@SERVICE_USER@')

    def test_rendered_pam_session_defaults_scopes_the_service_user(self):
        # Rendered like install.sh's sed: the entry must name exactly the
        # real service user, with no literal placeholder left behind.
        for rel in self.TEMPLATES:
            with self.subTest(template=rel):
                rendered = read(rel).replace('@SERVICE_USER@', SERVICE_USER)
                self.assertNotIn('@SERVICE_USER@', rendered)
                match = self.PAM_SESSION_DEFAULTS.search(rendered)
                self.assertIsNotNone(match,
                                     f'{rel}: Defaults entry disappeared')
                self.assertEqual(match.group('users'), SERVICE_USER)

    def test_templates_name_no_real_user(self):
        for rel in self.TEMPLATES:
            with self.subTest(template=rel):
                self.assertNotRegex(read(rel), r'(?m)^stan\s')
                self.assertNotIn('stan', read(rel))

    def test_rendered_templates_name_the_dedicated_user(self):
        for rel in self.TEMPLATES:
            with self.subTest(template=rel):
                rendered = read(rel).replace('@SERVICE_USER@', SERVICE_USER)
                self.assertNotIn('@SERVICE_USER@', rendered)
                self.assertRegex(rendered, r'(?m)^parental-controls\s')

    def test_rendered_templates_pass_visudo(self):
        # Covers BOTH templates (the TEMPLATES loop) and every line shape in
        # them, including the `Defaults:<user> !pam_session` entry: visudo
        # rejects unknown Defaults options and bad grammar as parse errors,
        # exactly like install.sh's `visudo -c` gate on the live box.
        # Skips when visudo is absent (e.g. containers without sudo).
        if shutil.which('visudo') is None:
            self.skipTest('visudo not on PATH')
        tmpd = tempfile.mkdtemp(prefix='pc-sudoers-')
        try:
            for rel in self.TEMPLATES:
                target = os.path.join(tmpd, os.path.basename(rel))
                with open(target, 'w', encoding='utf-8') as fh:
                    fh.write(read(rel).replace('@SERVICE_USER@',
                                                SERVICE_USER))
                proc = subprocess.run(['visudo', '-c', '-f', target],
                                      capture_output=True, text=True,
                                      timeout=60)
                self.assertEqual(
                    proc.returncode, 0,
                    f'visudo -c -f {rel} failed: {proc.stderr}')
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)


class TestStatePreservedNotReset(unittest.TestCase):
    def test_install_dir_ownership_migrates_not_resets(self):
        script = read('install.sh')
        self.assertIn('chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"',
                      script)
        self.assertNotIn('rm -rf "$INSTALL_DIR"', script)

    def test_auth_store_ownership_migrates_not_resets(self):
        script = read('install.sh')
        self.assertIn(
            'chown -R "$SERVICE_USER:$SERVICE_USER" /etc/parental-controls',
            script)
        self.assertNotIn('rm -rf /etc/parental-controls', script)

    def test_no_redirect_clobbers_outside_the_temp_dir(self):
        # Every file the installer writes with `>` must land in $TMPD
        # (rendered units, sudoers copies, CLI wrapper) — never into the
        # install dir or the auth store. The wrapper goes through
        # $CLI_WRAPPER, which is defined as a path under $TMPD.
        script = read('install.sh')
        self.assertIn('CLI_WRAPPER="$TMPD/parental-controls"', script)
        targets = re.findall(r'>\s*"(\$[A-Za-z_]+)', script)
        self.assertTrue(targets, 'installer writes no files?')
        for target in targets:
            self.assertIn(target, ('$TMPD', '$CLI_WRAPPER'),
                          f'installer redirect outside temp dir: {target}')

    def test_runtime_state_excluded_from_both_staging_paths(self):
        script = read('install.sh')
        for name in STATE_FILES:
            with self.subTest(state=name):
                self.assertIn(f'--exclude=/{name}', script)
                self.assertIn(f"--exclude='./{name}'", script)


class TestHomelessVenv(unittest.TestCase):
    def test_home_and_cache_are_pinned_under_the_app_dir(self):
        script = read('install.sh')
        self.assertIn('VENV_CACHE="$INSTALL_DIR/.cache"', script)
        self.assertIn('XDG_CACHE_HOME="$VENV_CACHE"', script)
        self.assertIn('HOME="$VENV_HOME"', script)
        self.assertIn('PIP_CACHE_DIR="$VENV_CACHE/pip"', script)

    def test_cache_dir_is_owned_by_the_service_user(self):
        script = read('install.sh')
        self.assertIn('chown "$SERVICE_USER:$SERVICE_USER" "$VENV_CACHE"',
                      script)

    def test_venv_and_pip_run_with_the_pinned_home(self):
        script = read('install.sh')
        self.assertRegex(
            script, r'(?s)as_user env HOME=.*?python3 -m venv',
            'venv creation must run under the pinned HOME')
        self.assertRegex(
            script, r'(?s)as_user env HOME=.*?\.venv/bin/pip" install',
            'pip install must run under the pinned HOME')

    def test_mechanism_works_for_a_homeless_user(self):
        """End-to-end shape of the installer step, as `nobody` (no home).

        Proves HOME/XDG_CACHE_HOME propagate through `sudo -u ... env ...`
        and are enough for venv creation plus a real pip install — the exact
        failure without them is pip's `/.cache/pip ... not owned` fallback.
        Temp dir only; no system state touched.
        """
        if shutil.which('sudo') is None:
            self.skipTest('sudo not on PATH')
        probe = subprocess.run(['sudo', '-n', '-u', 'nobody', 'true'],
                               capture_output=True, timeout=30)
        if probe.returncode != 0:
            self.skipTest('cannot sudo -u nobody non-interactively here')
        # The scratch root must be traversable by the unprivileged account.
        # When TMPDIR lives under a private home (e.g. an in-project .scratch
        # under a 0700 $HOME) `nobody` cannot reach it, and this live probe
        # cannot run — skip rather than fail on an environment limitation.
        root = tempfile.gettempdir()
        if subprocess.run(['sudo', '-n', '-u', 'nobody', 'test', '-x', root],
                          capture_output=True, timeout=30).returncode != 0:
            self.skipTest('scratch dir not traversable by nobody '
                          f'({root}); set TMPDIR to a world-traversable path')
        tmpd = tempfile.mkdtemp(prefix='pc-homeless-')
        try:
            home = os.path.join(tmpd, 'home')
            cache = os.path.join(home, '.cache')
            venv = os.path.join(tmpd, 'venv')
            os.chmod(tmpd, 0o777)

            def as_nobody(*argv):
                return subprocess.run(
                    ['sudo', '-n', '-u', 'nobody', 'env',
                     f'HOME={home}', f'XDG_CACHE_HOME={cache}',
                     'PATH=/usr/bin:/bin'] + list(argv),
                    capture_output=True, text=True, timeout=120)

            proc = as_nobody('python3', '-m', 'venv', venv)
            self.assertEqual(proc.returncode, 0,
                             f'venv as homeless user failed: {proc.stderr}')
            proc = as_nobody(f'{venv}/bin/pip', 'cache', 'dir')
            self.assertEqual(proc.returncode, 0,
                             f'pip cache as homeless user failed: {proc.stderr}')
            self.assertEqual(proc.stdout.strip(), f'{cache}/pip')
        finally:
            # The venv is nobody-owned; remove it the way it was made.
            subprocess.run(['sudo', '-n', '-u', 'nobody', 'rm', '-rf', tmpd],
                           capture_output=True, timeout=60)
            shutil.rmtree(tmpd, ignore_errors=True)


class TestNoHardcodedStanIdentity(unittest.TestCase):
    FILES = ['install.sh',
             'systemd/parental-controls.service',
             'systemd/parental-controls-enforce.service',
             'systemd/parental-controls-healthcheck.service',
             'sudoers-parental-controls',
             'sudoers-parental-controls-budgets']

    def test_no_unit_or_template_runs_as_stan(self):
        for rel in self.FILES[1:]:
            with self.subTest(file=rel):
                self.assertNotIn('stan', read(rel))

    def test_installer_names_stan_only_for_compat(self):
        # `stan` survives in install.sh in exactly two shapes: comments that
        # document why $SUDO_USER is not the default, and the legacy
        # `s|^stan |` render rule for hand-written sudoers files.
        for line in read('install.sh').splitlines():
            if 'stan' not in line or '@SERVICE_USER@' in line:
                continue
            self.assertTrue(
                line.strip().startswith('#') or 's|^stan |' in line,
                f'unexpected stan reference: {line.strip()}')


if __name__ == '__main__':
    unittest.main()
