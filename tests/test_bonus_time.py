#!/usr/bin/env python3
"""Tests for parent-granted bonus time (keep unlocked past bedtime).

Covers the tick/expiry logic inside enforce_bedtime(), the grant/revoke
HTTP endpoints, stacking + caps, manual-lock cancellation, schedule save
preservation, and the live-payload shape. All subprocess/work is mocked;
no real users are touched.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import auth
from test_lock_regression import (_AdminSessionMixin, _BedtimeFileMixin,
                                  KID, WINDOW, IN_WINDOW, OUT_WINDOW)

# Bonus-overlapping fixture times: IN_WINDOW (03:30) is inside the
# 03:00-04:00 window, OUT_WINDOW (12:00) is outside it.
BONUS = {'start': '03:00', 'end': '04:00', 'days': [0, 1, 2, 3, 4, 5, 6]}


def _active_bonus_entry(minutes=30, until=None):
    """A bedtime entry with a live bonus expiring `minutes` past `until`.

    Returns JSON-safe state (bonus_until as an ISO string).
    """
    base = until or datetime.now()
    return dict(BONUS, bonus_until=(base + timedelta(minutes=minutes)).isoformat(),
                bonus_minutes=minutes, bonus_granted_by='qaadmin',
                bonus_granted_at=base.isoformat())


class TestBonusHelpers(_BedtimeFileMixin, unittest.TestCase):
    """Pure helper logic (no HTTP, no enforcement loop)."""

    def test_parse_bonus_until_roundtrip(self):
        entry = dict(BONUS, bonus_until='2026-10-10T03:45:00')
        self.assertEqual(app._parse_bonus_until(entry),
                         datetime(2026, 10, 10, 3, 45))

    def test_parse_bonus_until_absent_is_none(self):
        self.assertIsNone(app._parse_bonus_until(dict(BONUS)))
        self.assertIsNone(app._parse_bonus_until({}))

    def test_parse_bonus_until_corrupt_is_none(self):
        # Fail closed: garbage timestamp means no bonus, not a crash.
        self.assertIsNone(app._parse_bonus_until(
            dict(BONUS, bonus_until='not-a-time')))
        self.assertIsNone(app._parse_bonus_until(
            dict(BONUS, bonus_until=12345)))

    def test_bonus_state_shapes(self):
        now = datetime(2026, 10, 10, 3, 30)
        entry = _active_bonus_entry(minutes=30, until=now)
        state = app.bonus_state(entry, now)
        self.assertTrue(state['active'])
        self.assertEqual(state['remaining_minutes'], 30)
        self.assertEqual(state['granted_by'], 'qaadmin')
        self.assertEqual(state['last_minutes'], 30)
        # Expired / absent
        entry['bonus_until'] = (now - timedelta(minutes=1)).isoformat()
        self.assertFalse(app.bonus_state(entry, now)['active'])
        self.assertFalse(app.bonus_state(dict(BONUS), now)['active'])

    def test_compute_bonus_until_fresh_grant(self):
        now = datetime(2026, 10, 10, 3, 30)
        until, kind = app.compute_bonus_until(dict(BONUS), 45, now)
        self.assertEqual(until, now + timedelta(minutes=45))
        self.assertEqual(kind, 'grant')

    def test_compute_bonus_until_stacks_onto_active(self):
        now = datetime(2026, 10, 10, 3, 30)
        entry = _active_bonus_entry(minutes=30, until=now)
        until, kind = app.compute_bonus_until(entry, 45, now)
        # Stacks from the existing expiry, not from now.
        self.assertEqual(until, datetime(2026, 10, 10, 3, 30) + timedelta(minutes=75))
        self.assertEqual(kind, 'extend')

    def test_compute_bonus_until_cap_from_now(self):
        now = datetime(2026, 10, 10, 3, 30)
        # 181 min from now exceeds BONUS_MAX_STACK_MINUTES (180).
        self.assertIsNone(app.compute_bonus_until(dict(BONUS), 181, now))
        # Stacking a valid amount onto an active bonus past the cap fails too.
        entry = _active_bonus_entry(minutes=170, until=now)
        self.assertIsNone(app.compute_bonus_until(entry, 60, now))

    def test_note_bonus_grant_records_and_clears_override(self):
        self.write_bedtime({KID: dict(BONUS, manual_override=True)})
        until = app.note_bonus_grant(KID, 30, grantor='qaadmin',
                                     now=datetime(2026, 10, 10, 3, 30))
        self.assertIsNotNone(until)
        entry = self.read_bedtime()[KID]
        self.assertFalse(entry['manual_override'], 'grant must replace override')
        self.assertFalse(entry['locked_by_bedtime'])
        self.assertEqual(entry['bonus_minutes'], 30)
        self.assertEqual(entry['bonus_granted_by'], 'qaadmin')
        hist = entry['bonus_history']
        self.assertEqual(hist[-1]['kind'], 'grant')
        self.assertEqual(hist[-1]['minutes'], 30)

    def test_note_bonus_grant_no_entry_is_none(self):
        self.write_bedtime({})
        self.assertIsNone(app.note_bonus_grant(KID, 30))

    def test_note_bonus_revoke_removes_only_bonus(self):
        self.write_bedtime({KID: _active_bonus_entry()})
        self.assertTrue(app.note_bonus_revoke(KID, by='qaadmin'))
        entry = self.read_bedtime()[KID]
        self.assertNotIn('bonus_until', entry)
        self.assertEqual(entry['bonus_history'][-1]['kind'], 'revoke')
        # Second revoke finds nothing to cancel.
        self.assertFalse(app.note_bonus_revoke(KID, by='qaadmin'))

    def test_history_capped_at_20(self):
        data = {KID: dict(BONUS)}
        for i in range(25):
            app._append_bonus_history(data[KID], 'grant', 15,
                                      '2026-10-10T04:00:00', 'qaadmin')
        self.assertEqual(len(data[KID]['bonus_history']), app.BONUS_HISTORY_CAP)
        # The oldest grants rolled off; all remaining entries are from the
        # last BONUS_HISTORY_CAP appends.
        self.assertEqual(data[KID]['bonus_history'][-1]['kind'], 'grant')
        self.assertEqual(data[KID]['bonus_history'][0]['by'], 'qaadmin')


class TestBonusEnforcement(_BedtimeFileMixin, unittest.TestCase):
    """enforce_bedtime() ticking: active bonus suppresses, expiry re-locks."""

    def _enforce(self, now, **entry_kwargs):
        self.write_bedtime({KID: dict(BONUS, **entry_kwargs)})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock_cmd, \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'unlocked')), \
             mock.patch.object(app.budgets, 'is_admin_protected',
                               return_value=False), \
             mock.patch.object(app, '_safe_notify') as notify:
            app.enforce_bedtime(now=now)
        return lock_cmd, notify

    def test_active_bonus_suppresses_lock_in_window(self):
        lock_cmd, notify = self._enforce(
            IN_WINDOW,
            bonus_until=(IN_WINDOW + timedelta(minutes=30)).isoformat(),
            bonus_minutes=30, bonus_granted_by='qaadmin')
        self.assertEqual(lock_cmd.call_count, 0,
                         'active bonus must suppress the bedtime lock')
        entry = self.read_bedtime()[KID]
        self.assertFalse(entry.get('locked_by_bedtime'))

    def test_active_bonus_keeps_pre_warning(self):
        # A bonus suppresses the lock only — the 15-min bedtime pre-warning
        # still fires while the bonus runs (BONUS_TIME_PLAN.md §2).
        soon = datetime(2026, 10, 10, 2, 50)  # 10 min before 03:00 window
        entry = dict(BONUS,
                     bonus_until=(soon + timedelta(minutes=30)).isoformat(),
                     bonus_minutes=30, bonus_granted_by='qaadmin')
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock_cmd, \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'unlocked')), \
             mock.patch.object(app.budgets, 'is_admin_protected',
                               return_value=False), \
             mock.patch.object(app, '_safe_notify') as notify:
            app.enforce_bedtime(now=soon)
            self.write_bedtime({KID: entry})
            app.enforce_bedtime(now=soon)
        actions = [c.args[1] for c in lock_cmd.call_args_list if len(c.args) > 1]
        self.assertNotIn('lock', actions)
        self.assertNotIn('unlock', actions)
        titles = [c.args[1] for c in notify.call_args_list]
        self.assertIn('Bedtime in 15 minutes', titles,
                      'pre-warning must still fire during an active bonus')

    def test_expired_bonus_relocks_in_window(self):
        lock_cmd, notify = self._enforce(
            IN_WINDOW,
            bonus_until=(IN_WINDOW - timedelta(minutes=1)).isoformat(),
            bonus_minutes=30, bonus_granted_by='qaadmin')
        actions = [c.args[1] for c in lock_cmd.call_args_list if len(c.args) > 1]
        self.assertIn('lock', actions)
        entry = self.read_bedtime()[KID]
        self.assertTrue(entry.get('locked_by_bedtime'))
        self.assertNotIn('bonus_until', entry, 'expired bonus must be cleared')
        self.assertEqual(entry['bonus_history'][-1]['kind'], 'expire')
        # The expiry lock notification mentions the bonus ended.
        titles = [c.args[1] for c in notify.call_args_list]
        self.assertIn('Bonus time ended', titles)

    def test_expired_bonus_out_of_window_stays_unlocked(self):
        lock_cmd, notify = self._enforce(
            OUT_WINDOW,
            bonus_until=(OUT_WINDOW - timedelta(minutes=1)).isoformat(),
            bonus_minutes=30, bonus_granted_by='qaadmin')
        actions = [c.args[1] for c in lock_cmd.call_args_list if len(c.args) > 1]
        self.assertNotIn('lock', actions)
        self.assertNotIn('bonus_until', self.read_bedtime()[KID])

    def test_corrupt_bonus_until_means_no_bonus(self):
        # Fail closed: unparseable bonus_until → enforcement proceeds.
        lock_cmd, _ = self._enforce(IN_WINDOW, bonus_until='garbage')
        actions = [c.args[1] for c in lock_cmd.call_args_list if len(c.args) > 1]
        self.assertIn('lock', actions)

    def test_no_bonus_keeps_existing_behavior(self):
        lock_cmd, _ = self._enforce(IN_WINDOW)
        actions = [c.args[1] for c in lock_cmd.call_args_list if len(c.args) > 1]
        self.assertIn('lock', actions)


class TestBonusGrantAPI(_AdminSessionMixin, _BedtimeFileMixin,
                        unittest.TestCase):
    """/api/bonus and /api/bonus/revoke endpoints."""

    def setUp(self):
        super().setUp()
        # Never fire real systemd-run notifications from tests.
        patcher = mock.patch.object(app, '_safe_notify')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_grant_unlocks_bedtime_locked_user(self):
        self.write_bedtime({KID: dict(BONUS, locked_by_bedtime=True)})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock_cmd:
            resp = self.authed_post('/api/bonus',
                                    data={'username': KID, 'minutes': '30'})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertFalse(payload['locked'])
        actions = [c.args[1] for c in lock_cmd.call_args_list if len(c.args) > 1]
        self.assertIn('unlock', actions,
                      'granting bonus past bedtime must unlock the account')
        entry = self.read_bedtime()[KID]
        self.assertFalse(entry['locked_by_bedtime'])
        self.assertTrue(app._parse_bonus_until(entry) is not None)

    def test_grant_requires_schedule(self):
        self.write_bedtime({})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')):
            resp = self.authed_post('/api/bonus',
                                    data={'username': KID, 'minutes': '30'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('schedule', resp.get_json()['error'].lower())

    def test_grant_validation(self):
        self.write_bedtime({KID: dict(BONUS)})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')):
            for bad_minutes in ('0', '-15', 'abc', '4', '481'):
                resp = self.authed_post('/api/bonus',
                                        data={'username': KID,
                                              'minutes': bad_minutes})
                self.assertEqual(resp.status_code, 400,
                                 f'minutes={bad_minutes!r} must be rejected')
            resp = self.authed_post('/api/bonus', data={'minutes': '15'})
            self.assertEqual(resp.status_code, 400)
            resp = self.authed_post('/api/bonus',
                                    data={'username': '-kid', 'minutes': '15'})
            self.assertEqual(resp.status_code, 400)
            # 480 is the custom-entry ceiling and passes field validation,
            # but a single grant can never exceed the stacked cap (180).
            resp = self.authed_post('/api/bonus',
                                    data={'username': KID, 'minutes': '480'})
            self.assertEqual(resp.status_code, 400)
            resp = self.authed_post('/api/bonus',
                                    data={'username': KID, 'minutes': '180'})
            self.assertEqual(resp.status_code, 200)

    def test_grant_refused_for_wheel(self):
        self.write_bedtime({self.ADMIN_USER: dict(BONUS)})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')):
            resp = self.authed_post('/api/bonus',
                                    data={'username': self.ADMIN_USER,
                                          'minutes': '15'})
        self.assertEqual(resp.status_code, 403)

    def test_grant_cap_rejected(self):
        self.write_bedtime({KID: dict(BONUS)})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')):
            resp = self.authed_post('/api/bonus',
                                    data={'username': KID, 'minutes': '181'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('total', resp.get_json()['error'].lower())

    def test_grant_requires_auth_and_csrf(self):
        self.write_bedtime({KID: dict(BONUS)})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')):
            # No session at all → 401 on the API.
            anon = app.app.test_client()
            resp = anon.post('/api/bonus',
                             data={'username': KID, 'minutes': '15'})
            self.assertEqual(resp.status_code, 401)
            # Authenticated (setup login cookie) but no CSRF token → 403.
            resp = self.client.post('/api/bonus',
                                    data={'username': KID, 'minutes': '15'})
            self.assertEqual(resp.status_code, 403)

    def test_revoke_relocks_immediately(self):
        self.write_bedtime({KID: _active_bonus_entry(minutes=30,
                                                     until=IN_WINDOW)})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock_cmd, \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, 'unlocked')), \
             mock.patch.object(app, 'is_in_bedtime',
                               return_value=True):
            resp = self.authed_post('/api/bonus/revoke',
                                    data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['locked'], 'revoke must re-lock in-window')
        actions = [c.args[1] for c in lock_cmd.call_args_list if len(c.args) > 1]
        self.assertIn('lock', actions, 'revoke must lock immediately, not wait 60s')
        entry = self.read_bedtime()[KID]
        self.assertTrue(entry.get('locked_by_bedtime'))
        self.assertNotIn('bonus_until', entry)

    def test_revoke_with_no_bonus_is_400(self):
        self.write_bedtime({KID: dict(BONUS)})
        resp = self.authed_post('/api/bonus/revoke', data={'username': KID})
        self.assertEqual(resp.status_code, 400)

    def test_manual_lock_cancels_bonus(self):
        self.write_bedtime({KID: _active_bonus_entry(minutes=30,
                                                     until=IN_WINDOW)})
        app.note_manual_lock(KID)
        entry = self.read_bedtime()[KID]
        self.assertNotIn('bonus_until', entry)
        self.assertEqual(entry['bonus_history'][-1]['kind'], 'lock_cancelled')
        # And a cancelled bonus must not be auto-unlocked at window end
        # (the manual lock owns the account now).
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')) as lock_cmd, \
             mock.patch.object(app.budgets, 'is_admin_protected',
                               return_value=False):
            app.enforce_bedtime(now=OUT_WINDOW)
        actions = [c.args[1] for c in lock_cmd.call_args_list if len(c.args) > 1]
        self.assertNotIn('unlock', actions)

    def test_save_bedtime_preserves_bonus(self):
        self.write_bedtime({KID: _active_bonus_entry(minutes=30,
                                                     until=IN_WINDOW)})
        resp = self.authed_post('/api/bedtime',
                                data={'username': KID, 'start': '22:00',
                                      'end': '07:00', 'all_days': 'on'})
        self.assertEqual(resp.status_code, 200)
        entry = self.read_bedtime()[KID]
        self.assertIn('bonus_until', entry, 'schedule edit must keep the bonus')
        self.assertEqual(entry['start'], '22:00')

    def test_clear_bedtime_drops_bonus(self):
        self.write_bedtime({KID: _active_bonus_entry(minutes=30,
                                                     until=IN_WINDOW)})
        resp = self.authed_post('/api/bedtime',
                                data={'username': KID, 'clear': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(KID, self.read_bedtime())


class TestBonusLivePayload(_AdminSessionMixin, _BedtimeFileMixin,
                           unittest.TestCase):
    """The bonus block reaches the UI: live payload + users list."""

    def _patch_user_source(self):
        users = [{'username': KID, 'uid': 30000,
                  'home': '/home/kid', 'shell': '/bin/bash'}]
        return mock.patch.object(
            app, 'get_human_users', return_value=users), \
            mock.patch.object(app, 'get_lock_status',
                              return_value=(False, 'unlocked')), \
            mock.patch.object(app.budgets, 'list_user_processes',
                              return_value=[]), \
            mock.patch.object(app.budgets, 'load_prefs',
                              return_value={}), \
            mock.patch.object(app.budgets, 'build_desktop_index',
                              return_value={}), \
            mock.patch.object(app.budgets, 'budget_status',
                              return_value={})

    def test_live_payload_includes_bonus(self):
        self.write_bedtime({KID: _active_bonus_entry(minutes=30,
                                                     until=datetime.now())})
        patchers = self._patch_user_source()
        with mock.patch.object(app.budgets, 'group_by_category',
                               return_value={c: [] for c in app.budgets.CATEGORIES}), \
             mock.patch.object(app.budgets, 'categorize',
                               return_value='system'):
            for p in patchers:
                p.start()
            for p in patchers:
                self.addCleanup(p.stop)
            resp = self.client.get(f'/api/user/{KID}/live')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn('bonus', payload)
        self.assertTrue(payload['bonus']['active'])
        self.assertEqual(payload['bonus']['granted_by'], 'qaadmin')

    def test_users_list_includes_bonus(self):
        self.write_bedtime({KID: _active_bonus_entry(minutes=30,
                                                     until=datetime.now())})
        patchers = self._patch_user_source()
        for p in patchers:
            p.start()
        for p in patchers:
            self.addCleanup(p.stop)
        resp = self.client.get('/api/users')
        self.assertEqual(resp.status_code, 200)
        row = next(u for u in resp.get_json() if u['username'] == KID)
        self.assertTrue(row['bonus']['active'])


if __name__ == '__main__':
    unittest.main()