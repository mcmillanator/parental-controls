#!/usr/bin/env python3
"""Deployment-artifact regression tests.

Guards against silent-breakage classes seen in production:

1. ProtectSystem=strict makes /etc read-only for the service, so `sudo
   usermod` cannot write /etc/passwd, /etc/shadow or /etc/.pwd.lock and
   every Lock/Unlock fails with "cannot lock /etc/passwd; try again later"
   (observed 2026-10-10). The unit must keep directory-level /etc writable.
2. Checkout/home paths (~/Documents/Projects/...) must never be baked into
   committed deployment files; the installer renders absolute paths at
   install time from @APP_DIR@.
3. Installed sudoers files contained a literal `@SERVICE_USER@` (installer
   replaced only a leading "stan ", never the placeholder), granting nothing
   to the service user — masked on dev by the stan-nopasswd blanket rule,
   fatal on a real machine. The installer must replace the placeholder.
4. Sudoers allowed /usr/bin/usermod while sudo resolves bare `usermod` via
   secure_path to /usr/sbin/usermod (sudo matches on path string), and the
   app's `sudo passwd -S` status check had no rule at all.

Pure file-content assertions; no Flask, no real accounts, nothing installed.
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


class TestUnitSandbox(unittest.TestCase):
    def test_protect_system_is_strict(self):
        unit = read(os.path.join('systemd', 'parental-controls.service'))
        directives = [l for l in unit.splitlines()
                      if l.startswith('ProtectSystem=')]
        self.assertEqual(directives, ['ProtectSystem=strict'],
                         'the only ProtectSystem directive must be "strict"')

    def test_etc_is_writable_for_usermod(self):
        unit = read(os.path.join('systemd', 'parental-controls.service'))
        # /etc (directory level) is required: per-file ReadWritePaths for
        # /etc/shadow and /etc/gshadow (mode 0000) fails namespace setup
        # (SELinux denies mounton on shadow_t; unit exits 226/NAMESPACE).
        self.assertIn('ReadWritePaths=@APP_DIR@ /etc/parental-controls /etc',
                      unit)


class TestNoHardcodedPaths(unittest.TestCase):
    """Deployment files must not name the dev checkout/home location."""

    FILES = [
        'systemd/parental-controls.service',
        'systemd/parental-controls-enforce.service',
        'systemd/parental-controls-enforce.timer',
        'install.sh',
        'enforce_bedtime_timer.py',
        'sudoers-parental-controls',
        'sudoers-parental-controls-budgets',
    ]

    def test_no_checkout_home_paths_in_deployment_files(self):
        bad = re.compile(r'(/home/|Documents/Projects)')
        for rel in self.FILES:
            self.assertNotRegex(
                read(rel), bad,
                f'{rel} references a checkout/home path')


class TestInstallerStaging(unittest.TestCase):
    def test_default_install_dir_is_opt_personal_controls(self):
        script = read('install.sh')
        self.assertIn('INSTALL_DIR="/opt/parental-controls"', script)

    def test_units_use_placeholders_not_absolute_paths(self):
        for rel in ('systemd/parental-controls.service',
                    'systemd/parental-controls-enforce.service'):
            unit = read(rel)
            self.assertIn('@APP_DIR@', unit)
            self.assertNotIn('@SERVICE_USER@=stan', unit)

    def test_installer_replaces_service_user_placeholder(self):
        script = read('install.sh')
        self.assertIn('s|@SERVICE_USER@|${SERVICE_USER}|g', script)


class TestSudoersTemplates(unittest.TestCase):
    def test_templates_use_placeholder_not_a_real_user(self):
        for rel in ('sudoers-parental-controls',
                    'sudoers-parental-controls-budgets'):
            body = read(rel)
            self.assertIn('@SERVICE_USER@', body)
            self.assertNotRegex(body, r'(?m)^stan\s')

    def test_rendered_templates_contain_no_placeholder(self):
        import re as _re
        script = read('install.sh')
        m = _re.search(r's\|@SERVICE_USER@\|\$\{SERVICE_USER\}\|g', script)
        self.assertIsNotNone(m, 'installer must substitute @SERVICE_USER@')
        for rel in ('sudoers-parental-controls',
                    'sudoers-parental-controls-budgets'):
            rendered = read(rel).replace('@SERVICE_USER@', 'stan')
            self.assertNotIn('@SERVICE_USER@', rendered)
            self.assertRegex(rendered, r'(?m)^stan\s')

    def test_account_rules_cover_resolved_sbin_and_bin_paths(self):
        body = read('sudoers-parental-controls')
        for binary in ('usermod -L', 'usermod -U',
                       'loginctl terminate-user', 'loginctl show-user',
                       'loginctl lock-session', 'loginctl show-session'):
            self.assertIn(f'/usr/sbin/{binary}', body)
            self.assertIn(f'/usr/bin/{binary}', body)

    def test_passwd_status_check_is_allowed_and_scoped(self):
        body = read('sudoers-parental-controls')
        self.assertIn('/usr/sbin/passwd -S *', body)
        self.assertIn('/usr/bin/passwd -S *', body)
        # No unscoped passwd rule: every passwd entry must carry the -S flag.
        for line in body.splitlines():
            if 'passwd' in line and not line.strip().startswith('#'):
                self.assertIn('passwd -S', line)

    def test_budget_rules_cover_resolved_sbin_and_bin_paths(self):
        body = read('sudoers-parental-controls-budgets')
        for binary in ('tee', 'cat', 'mkdir'):
            self.assertIn(f'/usr/sbin/{binary}', body)
            self.assertIn(f'/usr/bin/{binary}', body)


if __name__ == '__main__':
    unittest.main()