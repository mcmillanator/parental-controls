#!/usr/bin/env python3
"""Deployment-artifact regression tests.

Guards against two silent-breakage classes seen in production:

1. ProtectSystem=strict makes /etc read-only for the service, so `sudo
   usermod` cannot write /etc/passwd, /etc/shadow or /etc/.pwd.lock and
   every Lock/Unlock fails with "cannot lock /etc/passwd; try again later"
   (observed 2026-10-10). The unit must stay on ProtectSystem=full.
2. Checkout/home paths (~/Documents/Projects/...) must never be baked into
   committed deployment files; the installer renders absolute paths at
   install time from @APP_DIR@.

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


if __name__ == '__main__':
    unittest.main()