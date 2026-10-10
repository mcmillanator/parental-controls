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
5. Notifications never reached the child: the warning path needs
   `systemd-run --machine=<user>@.host --user notify-send ...` to run as root,
   and the template granted no `systemd-run` at all, so every pre-lock warning
   failed with "Failed to connect to system scope bus via machine transport".
   Masked on dev by the stan-nopasswd blanket rule.
6. A sudoers rule can be present but never match the command the app
   actually runs (sudoers matches arguments as one concatenated string, and
   a rule with too few argument slots matches nothing). The grant is pinned
   against the real argv, and every granted binary path must exist.

Pure file-content assertions; no Flask, no real accounts, nothing installed.
"""

import fnmatch
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def rule_lines(rel):
    """Non-comment, non-blank lines of a sudoers template."""
    return [l.strip() for l in read(rel).splitlines()
            if l.strip() and not l.strip().startswith('#')]


def granted_commands(rel):
    """[(binary, argument pattern)] for every entry in a sudoers template.

    sudoers command specifications are comma separated after NOPASSWD:, so
    each entry is split out and the binary separated from its arguments.
    """
    out = []
    for line in rule_lines(rel):
        spec = line.split('NOPASSWD:', 1)[-1]
        for item in spec.split(','):
            item = item.strip()
            if not item:
                continue
            binary, _, args = item.partition(' ')
            out.append((binary, ' '.join(args.split())))
    return out


def authorizes(rel, argv):
    """True if the template would let sudo run `argv` (sudoers(5) semantics).

    Command-line arguments are matched as a single, concatenated string
    against the rule's argument pattern ("* matches any set of zero or more
    characters, including white space"), which is what fnmatch does.
    """
    joined = ' '.join(argv)
    return any(binary and fnmatch.fnmatchcase(joined, args)
               for binary, args in granted_commands(rel))


# The exact argv notify_user() runs — pinned by
# tests/test_notifications.py::TestNotifyUser::test_command_shape.
NOTIFY_ARGV = ['--machine=testchild@.host', '--user', 'notify-send',
               '-u', 'critical', 'Bedtime in 15 minutes', '9:00 PM']


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


class TestBindOptions(unittest.TestCase):
    def test_safe_defaults(self):
        script = read('install.sh')
        self.assertIn('BIND_HOST="127.0.0.1"', script)
        self.assertIn('BIND_PORT="5000"', script)
        self.assertIn('OPEN_FIREWALL=0', script)

    def test_unit_renders_bind_from_placeholders(self):
        unit = read(os.path.join('systemd', 'parental-controls.service'))
        self.assertIn('@BIND_HOST@', unit)
        self.assertIn('@BIND_PORT@', unit)
        for line in unit.splitlines():
            if line.startswith('ExecStart='):
                self.assertNotIn('127.0.0.1', line)
                self.assertNotIn('5000', line)

    def test_installer_substitutes_bind_placeholders(self):
        script = read('install.sh')
        self.assertIn('s|@BIND_HOST@|$BIND_HOST|g', script)
        self.assertIn('s|@BIND_PORT@|$BIND_PORT|g', script)

    def test_firewall_change_is_opt_in(self):
        script = read('install.sh')
        self.assertIn('--open-firewall', script)
        self.assertIn('firewall-cmd', script)
        # The firewall commands only run inside the opt-in branch.
        idx_flag = script.index('--open-firewall) OPEN_FIREWALL=1')
        idx_gate = script.index('if [ "$OPEN_FIREWALL" -eq 1 ]')
        idx_fw = script.index('firewall-cmd --permanent --add-port')
        self.assertLess(idx_flag, idx_gate)
        self.assertLess(idx_gate, idx_fw)

    def test_port_and_bind_are_validated(self):
        script = read('install.sh')
        self.assertIn("Invalid --port", script)
        self.assertIn("Invalid --bind", script)

    def test_service_restarts_only_on_unit_change(self):
        script = read('install.sh')
        self.assertIn('UNITS_CHANGED=0', script)
        self.assertIn('UNITS_CHANGED=1', script)
        self.assertIn('systemctl restart parental-controls.service', script)
        self.assertIn('units unchanged', script.lower())

    def test_stale_process_detected_via_mtime(self):
        script = read('install.sh')
        self.assertIn('ActiveEnterTimestamp', script)
        self.assertIn('NEED_RESTART', script)

    def test_lan_verify_hints_at_firewall(self):
        script = read('install.sh')
        self.assertIn('--open-firewall', script)
        self.assertIn('re-run with --open-firewall', script)


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

    NOTIFY_ARGS = '--machine=*@.host --user notify-send -u critical * *'

    def test_systemd_run_granted_for_notifications(self):
        body = read('sudoers-parental-controls')
        for binary in ('/usr/sbin/systemd-run', '/usr/bin/systemd-run'):
            self.assertIn(f'{binary} {self.NOTIFY_ARGS}', body)

    def test_systemd_run_rule_is_scoped_to_the_machine_transport(self):
        for line in rule_lines('sudoers-parental-controls'):
            if 'systemd-run' not in line:
                continue
            self.assertIn('--machine=*@.host', line)
            self.assertIn('--user notify-send -u critical', line)
            # `--user *` would authorize any command in a child's session
            # (sudoers(5): "Preventing shell escapes").

    def test_systemd_run_rule_matches_the_command_the_app_runs(self):
        self.assertTrue(
            authorizes('sudoers-parental-controls', NOTIFY_ARGV),
            'no granted command authorizes: '
            f'systemd-run {" ".join(NOTIFY_ARGV)}')

    def test_notify_user_uses_the_granted_transport(self):
        src = read('app.py')
        for token in ('--machine={username}@.host', "'--user'",
                      "'notify-send'", "'critical'"):
            self.assertIn(token, src)

    def test_notification_grant_authorizes_nothing_else(self):
        """Least privilege: notify-send only, and only via --machine."""
        for payload in (
                ['--machine=testchild@.host', '--user', '/bin/sh'],
                ['--machine=testchild@.host', '--user', 'sh', '-c', 'id'],
                ['--user', 'notify-send', '-u', 'critical', 'T', 'B']):
            self.assertFalse(
                authorizes('sudoers-parental-controls', payload),
                'notifications grant must not authorize: '
                f'systemd-run {" ".join(payload)}')

    def test_granted_binary_paths_exist_on_this_box(self):
        for rel in ('sudoers-parental-controls',
                    'sudoers-parental-controls-budgets'):
            for binary, _ in granted_commands(rel):
                self.assertTrue(
                    os.path.exists(binary),
                    f'{rel} grants {binary}, which does not exist here')


if __name__ == '__main__':
    unittest.main()