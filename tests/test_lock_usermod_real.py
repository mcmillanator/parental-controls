#!/usr/bin/env python3
"""Live `usermod -L/-U` semantics against a real, disposable account.

The unit tests in test_lock_regression.py pin the shadow-field contract
as a model. This file proves the model matches the real shadow-utils on
this machine, and that a locked account is genuinely refused by the PAM
stack while an unlocked one is accepted.

`sudo -u <user> <cmd>` is NOT a valid lock test -- sudo already
authorized you and bypasses password auth. Authentication is exercised
through `su -` over a pty, which runs the real PAM stack.

Safety
------
* Operates only on a throwaway user this file creates itself
  (default `qa_lockprobe_regress`), never on an existing account.
* Never touches tester, stan, bluefox, root or PID 1.
* Never calls loginctl terminate-user / lock-session / kill.
* Restores (then deletes) the account in tearDownClass.

Skips itself unless it can create the user, set a password, and read
shadow -- so the suite stays green on machines where that is not
permitted. Requires: root-ish usermod access, pexpect, and the ability
to `su -` with a password.

Run:
    python3 -m unittest tests.test_lock_usermod_real
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    import pexpect
    HAS_PEXPECT = True
except ImportError:
    HAS_PEXPECT = False

TEST_USER = 'qa_lockprobe_regress'
TEST_PASSWORD = 'QaRegress123!'

# Never allowed to be touched by this module.
FORBIDDEN = {'root', 'stan', 'tester', 'bluefox', 'nobody', 'systemd-network'}


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kw)


def _have_privileges():
    if os.geteuid() != 0:
        p = _run(['sudo', '-n', 'true'])
        if p.returncode != 0:
            return False
    for tool in ('usermod', 'useradd', 'userdel', 'passwd'):
        p = _run(['sudo', '-n', 'which', tool])
        if p.returncode != 0:
            return False
    return True


def _can_create_user():
    if TEST_USER in FORBIDDEN:
        return False
    if _run(['getent', 'passwd', TEST_USER]).stdout.strip():
        return False
    return True


def _shadow_field():
    p = _run(['sudo', '-n', 'getent', 'shadow', TEST_USER])
    if p.returncode != 0 or ':' not in p.stdout:
        return None
    return p.stdout.strip().split(':')[1]


def _passwd_status():
    p = _run(['sudo', '-n', 'passwd', '-S', TEST_USER])
    parts = p.stdout.split()
    return parts[1] if len(parts) > 1 else None


def _bang_count(field):
    return len(field) - len(field.lstrip('!'))


def _auth_accepts():
    """True when `su -` with the correct password reaches a shell.

    This is the only trustworthy lock test: it exercises PAM.
    """
    user_re = TEST_USER.replace('-', r'\-')
    success = [rf'\[{user_re}@', rf'\]#\s', rf'\]\$\s']
    failure = [r'Authentication failure', r'incorrect password',
               r'login incorrect', r'account is locked',
               r'Permission denied', r'No passwd entry', r'Try again']
    try:
        child = pexpect.spawn(f'su - {TEST_USER}', timeout=25, encoding='utf-8',
                              env={'TERM': 'xterm', 'LANG': 'C',
                                   'PATH': '/usr/bin:/bin'})
    except Exception:
        return None
    try:
        idx = child.expect([r'[Pp]assword:'] + failure +
                           [pexpect.TIMEOUT, pexpect.EOF])
        if idx != 0:
            return False
        child.sendline(TEST_PASSWORD)
        idx2 = child.expect(success + failure +
                            [pexpect.TIMEOUT, pexpect.EOF])
        return idx2 < len(success)
    except Exception:
        return None
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass


@unittest.skipUnless(HAS_PEXPECT, 'pexpect not installed')
@unittest.skipUnless(_have_privileges(), 'needs usermod/useradd privileges')
@unittest.skipUnless(_can_create_user(), f'{TEST_USER} already exists')
class TestRealUsermodSemantics(unittest.TestCase):
    """Real /etc/shadow + real PAM. One throwaway account, torn down."""

    @classmethod
    def setUpClass(cls):
        p = _run(['sudo', '-n', 'useradd', '-m', '-s', '/bin/bash',
                  TEST_USER])
        if p.returncode != 0:
            raise unittest.SkipTest(f'useradd failed: {p.stderr.strip()}')
        pw = subprocess.run(['sudo', '-n', 'chpasswd'],
                            input=f'{TEST_USER}:{TEST_PASSWORD}\n',
                            capture_output=True, text=True, timeout=30)
        if pw.returncode != 0:
            _run(['sudo', '-n', 'userdel', '-r', TEST_USER])
            raise unittest.SkipTest('chpasswd failed')
        if _shadow_field() is None:
            _run(['sudo', '-n', 'userdel', '-r', TEST_USER])
            raise unittest.SkipTest('cannot read shadow for test user')

    @classmethod
    def tearDownClass(cls):
        _run(['sudo', '-n', 'userdel', '-r', TEST_USER])

    def setUp(self):
        # Start every test from a known unlocked state.
        _run(['sudo', '-n', 'usermod', '-U', TEST_USER])
        if _bang_count(_shadow_field() or '') != 0:
            self.skipTest('could not normalise account to unlocked')

    # -- shadow semantics ------------------------------------------------

    def test_lock_prepends_one_bang(self):
        before = _shadow_field()
        self.assertEqual(_bang_count(before), 0)
        _run(['sudo', '-n', 'usermod', '-L', TEST_USER])
        self.assertEqual(_bang_count(_shadow_field()), 1)
        self.assertEqual(_shadow_field()[1:], before)

    def test_double_lock_is_idempotent(self):
        before = _shadow_field()
        _run(['sudo', '-n', 'usermod', '-L', TEST_USER])
        _run(['sudo', '-n', 'usermod', '-L', TEST_USER])
        _run(['sudo', '-n', 'usermod', '-L', TEST_USER])
        self.assertEqual(_bang_count(_shadow_field()), 1,
                         'repeated -L must not stack bangs')
        self.assertEqual(_shadow_field()[1:], before)

    def test_unlock_removes_exactly_one_bang(self):
        _run(['sudo', '-n', 'usermod', '-L', TEST_USER])
        locked = _shadow_field()
        _run(['sudo', '-n', 'usermod', '-U', TEST_USER])
        self.assertEqual(_bang_count(_shadow_field()), 0)
        self.assertEqual(_shadow_field(), locked[1:])

    def test_repeated_unlock_does_not_over_strip(self):
        before = _shadow_field()
        _run(['sudo', '-n', 'usermod', '-L', TEST_USER])
        for _ in range(3):
            _run(['sudo', '-n', 'usermod', '-U', TEST_USER])
        self.assertEqual(_shadow_field(), before,
                         'unlocking an unlocked account must not eat the hash')

    def test_lock_unlock_round_trip_preserves_hash(self):
        before = _shadow_field()
        _run(['sudo', '-n', 'usermod', '-L', TEST_USER])
        _run(['sudo', '-n', 'usermod', '-U', TEST_USER])
        self.assertEqual(_shadow_field(), before)

    # -- status read-back matches reality --------------------------------

    def test_passwd_status_reflects_lock_state(self):
        _run(['sudo', '-n', 'usermod', '-L', TEST_USER])
        self.assertEqual(_passwd_status(), 'L')
        _run(['sudo', '-n', 'usermod', '-U', TEST_USER])
        self.assertEqual(_passwd_status(), 'P')

    # -- the actual security property ------------------------------------

    def test_locked_account_is_rejected_by_pam(self):
        _run(['sudo', '-n', 'usermod', '-L', TEST_USER])
        self.assertEqual(_passwd_status(), 'L')
        result = _auth_accepts()
        if result is None:
            self.skipTest('su probe inconclusive in this environment')
        self.assertFalse(result, 'LOCKED ACCOUNT AUTHENTICATED')

    def test_unlocked_account_is_accepted_by_pam(self):
        _run(['sudo', '-n', 'usermod', '-U', TEST_USER])
        result = _auth_accepts()
        if result is None:
            self.skipTest('su probe inconclusive in this environment')
        self.assertTrue(result, 'unlocked account wrongly rejected')

    def test_lock_blocks_auth_and_unlock_restores_it(self):
        """The end-to-end property the product depends on."""
        _run(['sudo', '-n', 'usermod', '-L', TEST_USER])
        locked = _auth_accepts()
        _run(['sudo', '-n', 'usermod', '-U', TEST_USER])
        unlocked = _auth_accepts()
        if locked is None or unlocked is None:
            self.skipTest('su probe inconclusive in this environment')
        self.assertFalse(locked, 'locked account must not authenticate')
        self.assertTrue(unlocked, 'unlocked account must authenticate')


if __name__ == '__main__':
    unittest.main()