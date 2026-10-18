#!/usr/bin/env python3
"""The lock-status probe cache: 30s TTL, invalidation, audit dedup.

Live finding (owner, 2026-10-18): the Overview polls `/api/users` every
10s and each poll ran `sudo passwd -S <user>` per account -- ~24 sudo PAM
sessions a minute on a four-account box, on top of the enforce timer's own
per-minute probes. systemd-logind then refused new sessions with
`TooManySessions`, so `sudo passwd -S` exited non-zero, `get_lock_status`
returned `(None, stderr)` and every child card raised the warning box.

`app.get_lock_status` therefore memoizes the RAW probe result per username
for `app._LOCK_STATUS_TTL_SECONDS`. What these tests pin:

  * the probe runs once per user inside the TTL and again once it expires
    (a poll burst must not open a sudo session per account per tick);
  * every lock/unlock core the app performs drops the entry, so the UI
    reflects the parent's own action on the very next render;
  * the cache holds RAW text, so `status_row`'s classification and the
    once-per-change `account.status_probe` audit keep behaving exactly as
    before -- a cached repeat emits nothing, a NEW failure re-emits.

Nothing here touches a real account: `subprocess.run` is mocked, no
account is locked, unlocked or signalled, and no /etc, /run, /opt or
checkout path is read.
"""

import os
import sys
import unittest
from collections import namedtuple
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app

Result = namedtuple('Result', ['returncode', 'stdout', 'stderr'])

KID = 'cachekid'
OTHER = 'cachekid2'

# The exact text the owner's box produced when logind refused the session.
TOO_MANY = ('sudo: pam_open_session: Permission denied\n'
            'sudo: policy plugin failed session initialization')


def _passwd(status):
    """stdout of a healthy `passwd -S` for `status` ('P' / 'L')."""
    return f'{KID} {status} 2026-10-18 0 99999 7 -1\n'


class _Clock:
    """A frozen `time.monotonic` the test advances by hand."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _ProbeHarness(unittest.TestCase):
    """Shared setup: empty cache, frozen clock, one scripted subprocess.

    `subprocess.run` is patched ONCE for the whole test: `sudo passwd -S`
    walks `responses` (recording every probe), and every other command
    (usermod / loginctl) is answered by `self.other`, which defaults to
    success and can be swapped for a failure.
    """

    def setUp(self):
        app.invalidate_lock_status()
        self.addCleanup(app.invalidate_lock_status)
        self.clock = _Clock()
        self.patcher = mock.patch.object(app.time, 'monotonic', self.clock)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.responses = []
        self.passwd_calls = []
        self.other = lambda cmd: Result(0, '', '')
        run = mock.patch.object(app.subprocess, 'run', side_effect=self._run)
        run.start()
        self.addCleanup(run.stop)

    def _probe(self, responses):
        """Queue the `sudo passwd -S` results the next probes will see."""
        self.responses = list(responses)

    def _run(self, cmd, **kwargs):
        if cmd[:3] == ['sudo', 'passwd', '-S']:
            self.passwd_calls.append(cmd[3])
            if not self.responses:
                raise AssertionError(
                    'unexpected extra lock-status probe for %r' % (cmd[3],))
            rc, stdout, stderr = self.responses.pop(0)
            return Result(rc, stdout, stderr)
        return self.other(cmd)

    def _probe_all_ok(self):
        """Every non-passwd command (usermod, loginctl) succeeds."""
        self.other = lambda cmd: Result(0, '', '')

    def _probe_all_fail(self, stderr='usermod: operation not permitted'):
        """Every non-passwd command fails."""
        self.other = lambda cmd: Result(1, '', stderr)


# ---------------------------------------------------------------------------
# 1. Cache hit / miss / TTL expiry
# ---------------------------------------------------------------------------

class LockStatusCacheTest(_ProbeHarness):
    def test_first_call_probes_and_later_calls_inside_the_ttl_do_not(self):
        self._probe([(0, _passwd('P'), '')])
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self.assertEqual(self.passwd_calls, [KID],
                         'the Overview polls every 10s -- inside the TTL the '
                         'probe must not run again (that was the live bug)')

    def test_the_cache_is_per_username(self):
        self._probe([(0, _passwd('P'), ''), (0, _passwd('P'), '')])
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self.assertEqual(app.get_lock_status(OTHER), (False, 'Unlocked'))
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self.assertEqual(self.passwd_calls, [KID, OTHER])

    def test_a_probe_inside_the_ttl_reprobes_after_it_expires(self):
        self._probe([(0, _passwd('P'), ''), (0, _passwd('L'), '')])
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self.clock.advance(app._LOCK_STATUS_TTL_SECONDS - 0.01)
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self.assertEqual(len(self.passwd_calls), 1)
        # ...and one tick past the TTL the account is read again, so a
        # change made outside the app surfaces within a few polls.
        self.clock.advance(0.02)
        self.assertEqual(app.get_lock_status(KID), (True, 'Locked'))
        self.assertEqual(len(self.passwd_calls), 2)

    def test_ttl_is_30_seconds_and_uses_the_monotonic_clock(self):
        # The exact number is part of the fix: 30s outlives the 10s poll
        # and stays inside a parent's attention span.
        self.assertEqual(30.0, app._LOCK_STATUS_TTL_SECONDS)

    def test_a_failure_result_is_cached_too(self):
        # Caching only the success path would leave the failure storms
        # (the very thing that raised the warning box) untouched.
        self._probe([(1, '', TOO_MANY)])
        self.assertEqual(app.get_lock_status(KID), (None, TOO_MANY))
        self.assertEqual(app.get_lock_status(KID), (None, TOO_MANY))
        self.assertEqual(len(self.passwd_calls), 1)

    def test_cached_value_is_the_raw_probe_tuple(self):
        """The RAW text is what is memoized -- status_row() classifies it."""
        self._probe([(1, '', TOO_MANY)])
        self.assertEqual(app.get_lock_status(KID), (None, TOO_MANY))
        raw = app._LOCK_STATUS_CACHE[KID][1]
        self.assertIsNone(raw[0])
        self.assertEqual(TOO_MANY, raw[1],
                         'the cache must not pre-classify: the admin audit '
                         'log needs the verbatim probe stderr')

    def test_invalidate_lock_status_clears_one_user_or_all_of_them(self):
        self._probe([(0, _passwd('P'), ''), (0, _passwd('P'), '')])
        app.get_lock_status(KID)
        app.get_lock_status(OTHER)
        app.invalidate_lock_status(KID)
        self.assertNotIn(KID, app._LOCK_STATUS_CACHE)
        self.assertIn(OTHER, app._LOCK_STATUS_CACHE)
        app.invalidate_lock_status()
        self.assertEqual({}, app._LOCK_STATUS_CACHE)


# ---------------------------------------------------------------------------
# 2. Invalidation after a lock/unlock the app performs
# ---------------------------------------------------------------------------

class LockStatusCacheInvalidationTest(_ProbeHarness):
    """Every lock/unlock core must drop the cached probe.

    All /api/* twins, the /overview/* form twins and the bedtime/bonus
    enforcement passes funnel through these three functions, so the cache
    cannot go stale after a parent action.
    """

    def test_run_lock_command_lock_invalidates_the_cached_probe(self):
        self._probe_all_ok()
        self._probe([(0, _passwd('P'), ''), (0, _passwd('L'), '')])
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        with mock.patch.object(app, 'lock_user_sessions',
                               return_value='no active session'), \
             mock.patch.object(app, '_safe_audit'):
            ok, _msg = app.run_lock_command(KID, 'lock')
        self.assertTrue(ok)
        # The card must not still say "Unlocked" on the next render.
        self.assertEqual(app.get_lock_status(KID), (True, 'Locked'))
        self.assertEqual(len(self.passwd_calls), 2)

    def test_run_lock_command_unlock_invalidates_the_cached_probe(self):
        self._probe_all_ok()
        self._probe([(0, _passwd('L'), ''), (0, _passwd('P'), '')])
        self.assertEqual(app.get_lock_status(KID), (True, 'Locked'))
        with mock.patch.object(app, '_safe_audit'):
            ok, _msg = app.run_lock_command(KID, 'unlock')
        self.assertTrue(ok)
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self.assertEqual(len(self.passwd_calls), 2)

    def test_lock_account_and_screen_invalidates_the_cached_probe(self):
        # /api/lock, /api/lock-screen and POST /overview/lock.
        self._probe_all_ok()
        self._probe([(0, _passwd('P'), ''), (0, _passwd('L'), '')])
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app, 'lock_user_sessions',
                               return_value='1 session(s) locked'), \
             mock.patch.object(app, '_safe_audit'):
            ok, _msg = app._lock_account_and_screen(KID, 'test')
        self.assertTrue(ok)
        self.assertEqual(app.get_lock_status(KID), (True, 'Locked'))
        self.assertEqual(len(self.passwd_calls), 2)

    def test_unlock_account_invalidates_the_cached_probe(self):
        # /api/unlock and POST /overview/unlock (via run_lock_command).
        self._probe_all_ok()
        self._probe([(0, _passwd('L'), ''), (0, _passwd('P'), '')])
        self.assertEqual(app.get_lock_status(KID), (True, 'Locked'))
        with mock.patch.object(app, 'note_manual_unlock'), \
             mock.patch.object(app, '_safe_audit'):
            ok, _msg = app._unlock_account(KID)
        self.assertTrue(ok)
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self.assertEqual(len(self.passwd_calls), 2)

    def test_emergency_logout_invalidates_the_cached_probe(self):
        # /api/logout: the account is locked even when terminate-user
        # fails, so the cache is dropped as soon as usermod -L lands.
        self._probe_all_ok()
        self._probe([(0, _passwd('P'), ''), (0, _passwd('L'), '')])
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app, '_safe_audit'):
            ok, _msg = app._emergency_logout(KID)
        self.assertTrue(ok)
        self.assertEqual(app.get_lock_status(KID), (True, 'Locked'))
        self.assertEqual(len(self.passwd_calls), 2)

    def test_a_failed_usermod_leaves_the_cache_alone(self):
        # A refused lock changed nothing, so the cached read stays valid
        # and the UI keeps showing the last known truth.
        self._probe_all_ok()
        self._probe([(0, _passwd('P'), '')])
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self._probe_all_fail()
        with mock.patch.object(app, '_safe_audit'):
            ok, _msg = app.run_lock_command(KID, 'lock')
        self.assertFalse(ok)
        self.assertIn(KID, app._LOCK_STATUS_CACHE)
        self.assertEqual(app.get_lock_status(KID), (False, 'Unlocked'))
        self.assertEqual(len(self.passwd_calls), 1)

    def test_invalidation_leaves_other_accounts_cached(self):
        self._probe([(0, _passwd('P'), ''), (0, _passwd('P'), '')])
        app.get_lock_status(KID)
        app.get_lock_status(OTHER)
        self.assertEqual(set(app._LOCK_STATUS_CACHE), {KID, OTHER})
        app.invalidate_lock_status(KID)
        self.assertEqual(set(app._LOCK_STATUS_CACHE), {OTHER})


# ---------------------------------------------------------------------------
# 3. The audit dedup is unchanged by the cache
# ---------------------------------------------------------------------------

class LockStatusCacheAuditDedupTest(_ProbeHarness):
    """`status_row` still classifies every call; the audit still dedups.

    The cache memoizes the RAW probe tuple, so `status_row` re-runs
    `status_display()`/`audit_status_probe()` on cached values. The dedup
    signature is (class, raw): a cached repeat is byte-identical and emits
    nothing; a genuinely new failure re-emits.
    """

    def _status_rows(self, responses):
        """Feed `responses` to the probe, then poll 2 more times (cached)."""
        self._probe(responses)
        return [app.status_row(KID) for _ in range(len(responses) + 2)]

    def test_a_cached_repeat_of_the_same_failure_emits_once(self):
        with mock.patch.object(app, '_LAST_STATUS_PROBE_SIGS', {}) as sigs, \
             mock.patch.object(app, '_safe_audit') as logged:
            rows = self._status_rows([(1, '', TOO_MANY)])
        self.assertEqual([(None, app.STATUS_UNAVAILABLE_COPY, False)] * 3,
                         rows)
        self.assertEqual(len(self.passwd_calls), 1)
        self.assertEqual(logged.call_count, 1,
                         'three polls, one admin line: the 10s poll must not '
                         're-log an unchanged failure')
        self.assertEqual(TOO_MANY, logged.call_args[0][3]['raw'])
        self.assertEqual(sigs[KID], (app.STATUS_UNAVAILABLE, TOO_MANY))

    def test_a_healthy_probe_stays_quiet_and_reads_unlocked(self):
        with mock.patch.object(app, '_LAST_STATUS_PROBE_SIGS', {}), \
             mock.patch.object(app, '_safe_audit') as logged:
            rows = self._status_rows([(0, _passwd('P'), '')])
        self.assertEqual([(False, 'Unlocked', True)] * 3, rows)
        logged.assert_not_called()

    def test_a_new_failure_after_the_ttl_expires_emits_again(self):
        second = 'sudo: passwd: command failed'
        with mock.patch.object(app, '_LAST_STATUS_PROBE_SIGS', {}), \
             mock.patch.object(app, '_safe_audit') as logged:
            self._status_rows([(1, '', TOO_MANY)])
            self.clock.advance(app._LOCK_STATUS_TTL_SECONDS + 1)
            self._status_rows([(1, '', second)])
        self.assertEqual(logged.call_count, 2,
                         'a DIFFERENT failure is a new (class, raw) signature '
                         'and must reach the admin log again')
        self.assertEqual([TOO_MANY, second],
                         [call[0][3]['raw'] for call in logged.call_args_list])

    def test_the_same_failure_after_the_ttl_is_not_re_logged(self):
        # The dedup is "log when the text CHANGES", unchanged by the cache:
        # re-probing the same broken account every 30s stays one line, just
        # like budgets' own dedup maps.
        with mock.patch.object(app, '_LAST_STATUS_PROBE_SIGS', {}), \
             mock.patch.object(app, '_safe_audit') as logged:
            self._status_rows([(1, '', TOO_MANY)])
            self.clock.advance(app._LOCK_STATUS_TTL_SECONDS + 1)
            self._status_rows([(1, '', TOO_MANY)])
        self.assertEqual(len(self.passwd_calls), 2, 'the probe re-ran')
        self.assertEqual(logged.call_count, 1, '...but the audit did not')

    def test_a_recovery_after_a_failure_is_visible_immediately(self):
        """Cached raw text must never mask a real state change."""
        with mock.patch.object(app, '_LAST_STATUS_PROBE_SIGS', {}), \
             mock.patch.object(app, '_safe_audit'):
            failed = self._status_rows([(1, '', TOO_MANY)])
            self.assertEqual((None, app.STATUS_UNAVAILABLE_COPY, False),
                             failed[0])
            app.invalidate_lock_status(KID)
            healthy = self._status_rows([(0, _passwd('P'), '')])
        self.assertEqual((False, 'Unlocked', True), healthy[0])


if __name__ == '__main__':
    unittest.main()
