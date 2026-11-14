#!/usr/bin/env python3
"""Calendar schedule — panel API + enforcement shims (app.py side).

Covers CALENDAR_DESIGN.md's HTTP surface with the same auth matrix as
/api/budgets/windows (401 anon / 403 no-CSRF / 403 protected target /
400 validation), the first-access migration via GET /api/schedule, the
retirement of the old bedtime/windows writers (the calendar API is the
only writer since the UI slice), the entry mirror sync (incl. live-state
preservation + the immediate lock/unlock tail), the events expansion with
the DERIVED bonus band, and the calendar-aware _bedtime_flag/enforce
path.

All four state homes (bedtime/overrides/budgets/schedule) point at ONE
temp dir — the production layout — so the shared SQLite db + mirrors
behave exactly as on the live box. No real accounts, no locks beyond
mocked run_lock_command, no /etc, no service state.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import budgets
import schedule
from test_lock_regression import _AdminSessionMixin, KID

ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
SCHOOL_DAYS = [0, 1, 2, 3, 4]
T0 = datetime(2026, 10, 12, 8, 0)


def _cat_window(target='games', days=None, start='10:00', end='11:00',
                allowance=30, enabled=True):
    return {'target_type': 'category', 'target': target,
            'days': list(ALL_DAYS if days is None else days),
            'start': start, 'end': end,
            'allowance_minutes': allowance, 'enabled': enabled}


def _block(target='games', mode='capped', allowance=30, preset='DAILY',
           start='10:00', end='11:00', bid='b-1', enabled=True,
           dtstart='2026-10-01', until=None):
    out = {'id': bid, 'target': target,
           'mode': None if mode == 'bedtime' else mode,
           'preset': preset, 'dtstart': dtstart,
           'start': start, 'end': end, 'enabled': enabled}
    if mode == 'capped' and allowance is not None:
        out['allowance_minutes'] = allowance
    if until:
        out['until'] = until
    return out


class _CalendarHomeMixin:
    """One temp state home shared by app + budgets (production layout)."""

    def setUp(self):
        super().setUp()
        # Bedtime writes through /api/schedule carry the retired old writer's
        # immediate lock/unlock tail (_enforce_bedtime_now) — pin the command
        # seams so no test can touch the box, whatever the wall clock says
        # (the file docstring's "no locks beyond mocked run_lock_command"
        # now covers the schedule API too).
        for name, ret in (('run_lock_command', (True, 'ok')),
                          ('get_lock_status', (False, '')),
                          ('_safe_notify', None)):
            patcher = mock.patch.object(app, name, return_value=ret)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(budgets, 'is_admin_protected',
                                    return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp = tempfile.TemporaryDirectory()
        home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        for target, attr, name in (
                (app, 'BEDTIME_FILE', 'bedtime.json'),
                (budgets, 'OVERRIDES_FILE', 'overrides.json'),
                (budgets, 'BUDGETS_FILE', 'budgets.json'),
                (budgets, 'SCHEDULE_FILE', 'schedule.json')):
            patcher = mock.patch.object(target, attr,
                                        os.path.join(home, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- helpers ----------------------------------------------------------

    def seed_bedtime(self, data):
        app.save_bedtime(data)

    def seed_schedule(self, docs):
        budgets.save_schedule(docs)

    def doc(self, username=KID):
        return budgets.load_schedule().get(username)

    def entry(self, username=KID):
        return app.load_bedtime().get(username, {})


# ---------------------------------------------------------------------------
# Auth / validation matrix
# ---------------------------------------------------------------------------

class ScheduleApiAuthTest(_AdminSessionMixin, _CalendarHomeMixin,
                          unittest.TestCase):
    def test_anonymous_and_csrf_matrix(self):
        anon = app.app.test_client()
        self.assertEqual(anon.get('/api/schedule').status_code, 401)
        self.assertEqual(anon.post('/api/schedule',
                                   data=_block()).status_code, 401)
        self.assertEqual(anon.delete('/api/schedule/b-1').status_code, 401)
        self.assertEqual(anon.get('/api/schedule/events').status_code, 401)
        # Authenticated but CSRF-less writes -> 403 (GET is fine).
        self.assertEqual(self.client.post('/api/schedule',
                                          data=_block()).status_code, 403)
        self.assertEqual(self.client.delete('/api/schedule/b-1',
                                            data={'username': KID},
                                            ).status_code, 403)
        self.assertEqual(self.client.get('/api/schedule').status_code, 200)

    def test_wheel_target_refused(self):
        resp = self.authed_post('/api/schedule',
                                data=dict(_block(), username=self.ADMIN_USER))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.doc(), None)

    def test_validation_rejects_bad_blocks(self):
        base = {'username': KID}
        bad = [
            dict(base, **_block(target='nope')),
            dict(base, **_block(target='app:bad comm!')),
            dict(base, **_block(preset='MONTHLY')),
            dict(base, **_block(preset='WEEKLY:BYDAY=XX')),
            dict(base, **_block(start='10:00', end='10:00')),
            dict(base, **_block(start='bogus')),
            dict(base, **_block(mode='capped', allowance=1)),
            dict(base, **_block(mode='capped', allowance=None)),
            dict(base, **_block(mode='')),
            dict(base, **_block(dtstart='nope')),
            dict(base, **_block(dtstart='')),
            dict(base, **_block(preset='ONE', dtstart='')),
            dict(base, **_block(target='bedtime', mode='permitted')),
        ]
        for data in bad:
            with self.subTest(target=data['target'],
                              preset=data['preset']):
                resp = self.authed_post('/api/schedule', data=data)
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(budgets.load_schedule(), {})


# ---------------------------------------------------------------------------
# CRUD + mirrors
# ---------------------------------------------------------------------------

class ScheduleCrudTest(_AdminSessionMixin, _CalendarHomeMixin,
                       unittest.TestCase):
    def test_create_capped_block_and_mirror(self):
        resp = self.authed_post('/api/schedule', data={
            'username': KID, 'target': 'games', 'mode': 'capped',
            'allowance_minutes': '45', 'preset': 'SCHOOL',
            'dtstart': '2026-10-01', 'start': '16:00', 'end': '18:00'})
        self.assertEqual(resp.status_code, 200)
        doc = self.doc()
        self.assertTrue(schedule.doc_is_migrated(doc))
        block = doc['events'][resp.get_json()['block']['id']]
        self.assertEqual(block['allowance_minutes'], 45)
        self.assertEqual(block['preset'], 'SCHOOL')
        # windows mirror regenerated (capped + expressible preset)
        mirrored = budgets.load_overrides()[KID]['windows'][block['id']]
        self.assertEqual(mirrored['days'], SCHOOL_DAYS)
        self.assertEqual(mirrored['allowance_minutes'], 45)
        # GET returns the view
        view = self.client.get('/api/schedule?username=' + KID
                               ).get_json()['schedule'][KID]
        self.assertEqual([b['id'] for b in view['events']], [block['id']])
        self.assertEqual(view['invalid'], [])

    def test_permitted_block_absent_from_mirror(self):
        resp = self.authed_post('/api/schedule', data={
            'username': KID, 'target': 'media', 'mode': 'permitted',
            'preset': 'WEEKLY:BYDAY=SA,SU', 'dtstart': '2026-10-01',
            'start': '10:00', 'end': '20:00'})
        self.assertEqual(resp.status_code, 200)
        entry = budgets.load_overrides().get(KID, {})
        self.assertEqual(entry.get('windows'), {})

    def test_bedtime_block_syncs_entry_mirror(self):
        # The block is UNTIL-bounded in the past, so the immediate-enforcement
        # tail (ported from the retired old writer) has nothing to do at any
        # wall clock, and the live keys below must survive untouched.
        self.seed_bedtime({KID: {'start': '21:00', 'end': '08:00',
                                 'days': ALL_DAYS,
                                 'locked_by_bedtime': False,
                                 'bonus_until': '2026-10-12T09:00:00'}})
        resp = self.authed_post('/api/schedule', data={
            'username': KID, 'target': 'bedtime',
            'preset': 'WEEKLY:BYDAY=TU,TH', 'dtstart': '2026-10-01',
            'start': '20:30', 'end': '06:30', 'until': '2026-10-11'})
        self.assertEqual(resp.status_code, 200)
        entry = self.entry()
        self.assertEqual((entry['start'], entry['end']), ('20:30', '06:30'))
        self.assertEqual(entry['days'], [1, 3])
        self.assertFalse(entry['locked_by_bedtime'])       # live keys kept
        self.assertEqual(entry['bonus_until'], '2026-10-12T09:00:00')

    def test_overlap_rejected_with_clear_error(self):
        ok1 = self.authed_post('/api/schedule', data={
            'username': KID, 'target': 'games', 'mode': 'capped',
            'allowance_minutes': '30', 'preset': 'DAILY',
            'dtstart': '2026-10-01', 'start': '16:00', 'end': '18:00',
            'id': 'b-a'})
        self.assertEqual(ok1.status_code, 200)
        resp = self.authed_post('/api/schedule', data={
            'username': KID, 'target': 'games', 'mode': 'capped',
            'allowance_minutes': '30', 'preset': 'DAILY',
            'dtstart': '2026-10-01', 'start': '17:00', 'end': '19:00',
            'id': 'b-b'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('b-a', resp.get_json()['error'])
        # different target overlaps freely
        resp = self.authed_post('/api/schedule', data={
            'username': KID, 'target': 'media', 'mode': 'capped',
            'allowance_minutes': '30', 'preset': 'DAILY',
            'dtstart': '2026-10-01', 'start': '17:00', 'end': '19:00',
            'id': 'b-c'})
        self.assertEqual(resp.status_code, 200)
        # disabled overlaps do not conflict
        resp = self.authed_post('/api/schedule', data={
            'username': KID, 'target': 'games', 'mode': 'capped',
            'allowance_minutes': '30', 'preset': 'DAILY',
            'dtstart': '2026-10-01', 'start': '17:00', 'end': '19:00',
            'id': 'b-d', 'enabled': '0'})
        self.assertEqual(resp.status_code, 200)

    def test_edit_and_delete(self):
        self.authed_post('/api/schedule', data={
            'username': KID, 'target': 'games', 'mode': 'capped',
            'allowance_minutes': '30', 'preset': 'DAILY',
            'dtstart': '2026-10-01', 'start': '16:00', 'end': '18:00',
            'id': 'b-a'})
        resp = self.authed_post('/api/schedule', data={
            'username': KID, 'target': 'games', 'mode': 'capped',
            'allowance_minutes': '60', 'preset': 'DAILY',
            'dtstart': '2026-10-01', 'start': '16:00', 'end': '18:00',
            'id': 'b-a'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.doc()['events']['b-a']['allowance_minutes'],
                         60)
        resp = self.client.delete('/api/schedule/b-a',
                                  data={'username': KID},
                                  headers=self.csrf_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.doc()['events'], {})
        self.assertEqual(budgets.load_overrides()[KID]['windows'], {})
        resp = self.client.delete('/api/schedule/zz',
                                  data={'username': KID},
                                  headers=self.csrf_headers)
        self.assertEqual(resp.status_code, 400)

    def test_delete_bedtime_block_unlocks_bedtime_lock(self):
        # The retired old writer's CLEAR unlocked what BEDTIME had locked;
        # the tail rides DELETE /api/schedule/<id> now. The series is built
        # around the real wall clock (DAILY anchored yesterday, now-1h to
        # now+1h) so the window is deterministically ACTIVE at create
        # ('already locked' -> the flag survives) and deterministically OVER
        # at delete (the lock is released) at any hour.
        now = datetime.now()
        start = (now - timedelta(hours=1)).strftime('%H:%M')
        end = (now + timedelta(hours=1)).strftime('%H:%M')
        dtstart = (now - timedelta(days=1)).strftime('%Y-%m-%d')
        self.seed_bedtime({KID: {'start': '21:00', 'end': '08:00',
                                 'days': ALL_DAYS,
                                 'locked_by_bedtime': True,
                                 'bonus_until': '2026-10-12T09:00:00'}})
        resp = self.authed_post('/api/schedule', data={
            'username': KID, 'target': 'bedtime', 'preset': 'DAILY',
            'dtstart': dtstart, 'start': start, 'end': end})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['locked'])
        self.assertIn('Already locked', payload['message'])
        self.assertTrue(self.entry()['locked_by_bedtime'])
        bid = payload['block']['id']
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'ok')) as lock_cmd:
            resp = self.client.delete('/api/schedule/' + bid,
                                      data={'username': KID},
                                      headers=self.csrf_headers)
        self.assertEqual(resp.status_code, 200)
        actions = [c.args[1] for c in lock_cmd.call_args_list
                   if len(c.args) > 1]
        self.assertIn('unlock', actions)
        self.assertFalse(resp.get_json()['locked'])
        self.assertEqual(self.doc()['events'], {})
        entry = self.entry()
        self.assertNotIn('start', entry)
        self.assertNotIn('days', entry)
        self.assertFalse(entry['locked_by_bedtime'])  # bedtime released its lock
        self.assertEqual(entry['bonus_until'], '2026-10-12T09:00:00')  # others kept


# ---------------------------------------------------------------------------
# First-access migration via GET /api/schedule
# ---------------------------------------------------------------------------

class MigrationAccessTest(_AdminSessionMixin, _CalendarHomeMixin,
                          unittest.TestCase):
    def test_get_cuts_over_legacy_config(self):
        self.seed_bedtime({KID: {'start': '21:00', 'end': '08:00',
                                 'days': SCHOOL_DAYS}})
        budgets.save_overrides({KID: {
            'app_overrides': {'steam': {'mode': 'limit', 'minutes': 20}},
            'windows': {'w-category-games': _cat_window(days=SCHOOL_DAYS,
                                                        start='16:00',
                                                        end='18:00'),
                        'w-category-media': _cat_window(target='media',
                                                        enabled=False)}}})
        payload = self.client.get('/api/schedule').get_json()
        self.assertTrue(payload['success'])
        view = payload['schedule'][KID]
        ids = [b['id'] for b in view['events']]
        self.assertEqual(sorted(ids), ['b-bedtime-1', 'w-category-games'])
        doc = self.doc()
        self.assertTrue(schedule.doc_is_migrated(doc))
        # legacy sources untouched (backup/mirror)
        self.assertEqual(self.entry()['start'], '21:00')
        self.assertIn('w-category-games',
                      budgets.load_overrides()[KID]['windows'])
        # idempotent: second GET keeps the same migrated stamp
        stamp = doc['migrated']
        self.client.get('/api/schedule')
        self.assertEqual(self.doc()['migrated'], stamp)

    def test_dead_windows_get_marker_but_no_blocks(self):
        budgets.save_overrides({KID: {'app_overrides': {},
                                      'windows': {'x': {'target_type': 'nope',
                                                        'target': 'x'}}}})
        payload = self.client.get('/api/schedule').get_json()
        view = payload['schedule'][KID]
        self.assertEqual(view['events'], [])
        doc = self.doc()
        self.assertTrue(schedule.doc_is_migrated(doc))
        # enforcement now sees ZERO windows, not the dead legacy row
        self.assertEqual(budgets.user_windows(KID, None, doc), [])


# ---------------------------------------------------------------------------
# Retired legacy editor surfaces (calendar cutover, CALENDAR_DESIGN.md §10)
# ---------------------------------------------------------------------------

class RetiredLegacyWritersTest(_AdminSessionMixin, _CalendarHomeMixin,
                               unittest.TestCase):
    """The old editors are gone; the calendar API is the only writer.

    The write-through these tests pinned (old /api/bedtime and
    /api/budgets/windows editing the schedule doc for migrated users)
    existed only while the old pages could write. With the hard cutover
    the UI slice retired those writers, so what is pinned now is the
    retirement itself + the surviving read contracts:
      * POST /api/bedtime -> 405 (the rule exists GET-only for the live
        chips/countdown readers);
      * POST /api/budgets/windows -> 405 (only the /api/budgets/<username>
        GET rule matches the path; no method it accepts);
      * GET /api/bedtime keeps answering from the mirror (status rows).
    """

    def test_post_bedtime_retired(self):
        resp = self.authed_post('/api/bedtime', data={
            'username': KID, 'start': '20:00', 'end': '06:00',
            'all_days': '1'})
        self.assertEqual(resp.status_code, 405)
        # Nothing was written: no doc, no mirror config.
        self.assertEqual(budgets.load_schedule(), {})
        self.assertEqual(app.load_bedtime(), {})

    def test_post_windows_retired(self):
        resp = self.authed_post('/api/budgets/windows', data={
            'username': KID, 'action': 'set', 'target_type': 'category',
            'target': 'games', 'days': '0,1,2', 'start': '16:00',
            'end': '18:00', 'allowance_minutes': '45'})
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(budgets.load_overrides(), {})

    def test_get_bedtime_still_serves_mirrors(self):
        self.seed_bedtime({KID: {'start': '21:00', 'end': '08:00',
                                 'days': ALL_DAYS}})
        resp = self.client.get('/api/bedtime')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()[KID]['start'], '21:00')

    def test_time_rules_page_gone(self):
        self.assertEqual(self.client.get('/time-rules').status_code, 404)


# ---------------------------------------------------------------------------
# events endpoint (expand + derived bonus band)
# ---------------------------------------------------------------------------

class ScheduleEventsTest(_AdminSessionMixin, _CalendarHomeMixin,
                         unittest.TestCase):
    def test_events_expand_and_bonus_band(self):
        self.seed_schedule({KID: {'events': {
            'b-1': schedule.normalize_block(_block(
                preset='WEEKLY:BYDAY=TU', start='16:00', end='18:00')),
            'b-2': schedule.normalize_block(_block(
                bid='b-2', target='bedtime', mode='bedtime',
                preset='DAILY', start='21:00', end='08:00')),
        }, 'migrated': '2026-10-12T08:00:00'}})
        self.seed_bedtime({KID: {'start': '21:00', 'end': '08:00',
                                 'days': ALL_DAYS,
                                 'bonus_until': (
                                     datetime.now()
                                     + timedelta(minutes=30)
                                 ).isoformat(timespec='seconds')}})
        payload = self.client.get(
            '/api/schedule/events?username=' + KID
            + '&start=2026-10-12&end=2026-10-19').get_json()
        self.assertTrue(payload['success'])
        by_id = {}
        for row in payload['events']:
            by_id.setdefault(row['block_id'], []).append(row)
        self.assertEqual(len(by_id['b-1']), 1)        # Tuesday only
        # DAILY overnight: the Oct-11 anchor bleeds in + 7 daily anchors.
        self.assertEqual(len(by_id['b-2']), 8)
        self.assertEqual(by_id['b-1'][0]['start'], '2026-10-13T16:00:00')
        # midnight-crossing occurrence runs into the next day
        self.assertEqual(by_id['b-2'][0]['start'], '2026-10-11T21:00:00')
        self.assertEqual(by_id['b-2'][1]['end'], '2026-10-13T08:00:00')
        derived = [r for r in payload['events'] if r.get('derived')]
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]['source'], 'bonus_state')
        self.assertTrue(derived[0]['end'] > derived[0]['start'])
        # NOT stored in the doc:
        self.assertNotIn('bonus', self.doc()['events'])

    def test_events_validation(self):
        self.assertEqual(self.client.get(
            '/api/schedule/events').status_code, 400)       # no username
        self.assertEqual(self.client.get(
            '/api/schedule/events?username=bad;user').status_code, 400)
        self.assertEqual(self.client.get(
            '/api/schedule/events?username=' + KID
            + '&start=nonsense').status_code, 400)
        self.assertEqual(self.client.get(
            '/api/schedule/events?username=' + KID
            + '&start=2026-10-12&end=2026-10-11').status_code, 400)
        self.assertEqual(self.client.get(
            '/api/schedule/events?username=' + KID
            + '&start=2026-01-01&end=2027-06-01').status_code, 400)
        # no doc yet -> empty expansion, 200
        resp = self.client.get('/api/schedule/events?username=' + KID)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['events'], [])


# ---------------------------------------------------------------------------
# Enforcement shims on the app side
# ---------------------------------------------------------------------------

class BedtimeFlagShimTest(_CalendarHomeMixin, unittest.TestCase):
    def test_migrated_doc_wins_over_entry(self):
        doc = {'events': {'b-1': schedule.normalize_block(_block(
            bid='b-1', target='bedtime', mode='bedtime', preset='DAILY',
            start='00:00', end='23:59'))}, 'migrated': 'x'}
        self.assertTrue(app._bedtime_flag({}, doc, T0))
        # legacy shape still governs without a doc
        legacy = {'start': '07:00', 'end': '09:00', 'days': [0, 1, 2, 3, 4]}
        self.assertTrue(app._bedtime_flag(legacy, None, T0))
        # a non-migrated doc is not a cutover -> legacy governs
        self.assertTrue(app._bedtime_flag(legacy, {}, T0))
        # migrated doc with NO bedtime block: entry mirror is NOT an input
        empty_doc = {'events': {}, 'migrated': 'x'}
        self.assertFalse(app._bedtime_flag(
            {'start': '07:00', 'end': '09:00', 'days': ALL_DAYS},
            empty_doc, T0))

    def test_shim_signature_back_compat(self):
        legacy = {'start': '07:00', 'end': '09:00', 'days': [0, 1, 2, 3, 4]}
        self.assertTrue(app.is_in_bedtime(legacy, T0))
        self.assertFalse(app.is_in_bedtime({}, T0))
        self.assertFalse(app.is_in_bedtime(None, T0))

    def test_minutes_until_bedtime_migrated(self):
        doc = {'events': {'b-1': schedule.normalize_block(_block(
            bid='b-1', target='bedtime', mode='bedtime', preset='DAILY',
            start='21:00', end='08:00'))}, 'migrated': 'x'}
        now = datetime(2026, 10, 12, 19, 0)
        self.assertAlmostEqual(
            app.minutes_until_bedtime({}, now, schedule_doc=doc), 120.0)
        self.assertEqual(
            app.minutes_until_bedtime({}, datetime(2026, 10, 12, 22, 0),
                                      schedule_doc=doc), 0)
        # legacy path unchanged
        legacy = {'start': '21:00', 'end': '08:00', 'days': ALL_DAYS}
        self.assertAlmostEqual(
            app.minutes_until_bedtime(legacy, now), 120.0)

    def test_window_id_migrated(self):
        doc = {'events': {'b-1': schedule.normalize_block(_block(
            bid='b-1', target='bedtime', mode='bedtime', preset='DAILY',
            start='21:00', end='08:00'))}, 'migrated': 'x'}
        self.assertEqual(
            app.bedtime_window_id({}, datetime(2026, 10, 12, 19, 0),
                                  schedule_doc=doc), '21:00-08:00')
        self.assertEqual(app.bedtime_window_id(
            {'start': '21:00', 'end': '08:00'}), '21:00-08:00')


class EnforceBedtimeCalendarTest(_CalendarHomeMixin, unittest.TestCase):
    def _active_bedtime_doc(self):
        return {'events': {'b-1': schedule.normalize_block(_block(
            bid='b-1', target='bedtime', mode='bedtime', preset='DAILY',
            start='00:00', end='23:59'))}, 'migrated': 'x'}

    def test_locks_from_block_even_without_entry_config(self):
        # Entry carries NO start/end (mirror emptied); the doc locks.
        self.seed_bedtime({KID: {'locked_by_bedtime': False}})
        budgets.save_schedule({KID: self._active_bedtime_doc()})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'ok')) as lock, \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, '')), \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False), \
             mock.patch.object(app, '_safe_notify', return_value=True):
            app.enforce_bedtime(datetime(2026, 10, 12, 12, 0))
        lock.assert_any_call(KID, 'lock')
        self.assertTrue(app.load_bedtime()[KID]['locked_by_bedtime'])

    def test_unmigrated_legacy_entry_still_locks(self):
        self.seed_bedtime({KID: {'start': '07:00', 'end': '23:00',
                                 'days': [0, 1, 2, 3, 4, 5, 6],
                                 'locked_by_bedtime': False}})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'ok')) as lock, \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(False, '')), \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False), \
             mock.patch.object(app, '_safe_notify', return_value=True):
            app.enforce_bedtime(datetime(2026, 10, 12, 12, 0))
        lock.assert_any_call(KID, 'lock')

    def test_migrated_without_blocks_unlocks_bedtime_locked(self):
        self.seed_bedtime({KID: {'start': '07:00', 'end': '23:00',
                                 'days': ALL_DAYS,
                                 'locked_by_bedtime': True}})
        budgets.save_schedule({KID: {'events': {}, 'migrated': 'x'}})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'ok')) as lock, \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(True, '')), \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False):
            app.enforce_bedtime(datetime(2026, 10, 12, 12, 0))
        lock.assert_any_call(KID, 'unlock')
        self.assertFalse(app.load_bedtime()[KID]['locked_by_bedtime'])


if __name__ == '__main__':
    unittest.main()
