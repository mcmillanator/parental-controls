#!/usr/bin/env python3
"""Calendar schedule — agent-side shims (agent.py).

The remote agent never imports app/flask; the calendar swap must keep that
discipline and stay hermetic. Covers: the is_in_bedtime shim, the
_bedtime_flag resolver, the do_bedtime write-through for users that
already have a migrated doc (and the guarantee that a doc is NEVER
created by the agent verbs), the clear path, and the users/live payloads
reading bedtime from blocks.

The agent's schedule home is pinned to the state dir of agent.BEDTIME_FILE
(the box's one shared state.db in production), so patching that constant
redirects the whole calendar surface.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import agent
import budgets
import schedule

KID = 'agentkid'
T0 = datetime(2026, 10, 12, 8, 0)
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


def _bedtime_block(start='00:00', end='23:59', bid='b-bedtime-1',
                   enabled=True):
    block = schedule.normalize_block({
        'id': bid, 'target': 'bedtime', 'mode': None, 'preset': 'DAILY',
        'dtstart': (T0 - timedelta(days=1)).date().isoformat(),
        'start': start, 'end': end, 'enabled': enabled})
    assert block is not None
    return block


class _AgentHomeMixin:
    """One temp state home for the agent's store (BEDTIME_FILE pins it)."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        for target, attr, name in (
                (agent, 'BEDTIME_FILE', os.path.join(home, 'bedtime.json')),
                (budgets, 'OVERRIDES_FILE',
                 os.path.join(home, 'overrides.json')),
                (budgets, 'BUDGETS_FILE', os.path.join(home, 'budgets.json')),
                (budgets, 'SCHEDULE_FILE',
                 os.path.join(home, 'schedule.json'))):
            patcher = mock.patch.object(target, attr,
                                        os.path.join(home, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self._audit = mock.patch.object(agent.audit, 'safe_log')
        self._audit.start()
        self.addCleanup(self._audit.stop)

    def seed_doc(self, doc):
        budgets.save_schedule({KID: doc}, home_file=agent.BEDTIME_FILE)

    def doc(self):
        return budgets.load_schedule(home_file=agent.BEDTIME_FILE).get(KID)

    def seed_entry(self, entry):
        agent.save_bedtime({KID: entry})


class IsInBedtimeShimTest(unittest.TestCase):
    def test_legacy_shape_verbatim(self):
        entry = {'start': '21:00', 'end': '08:00', 'days': ALL_DAYS}
        self.assertTrue(agent.is_in_bedtime(entry, T0.replace(hour=22)))
        self.assertTrue(agent.is_in_bedtime(entry, T0.replace(hour=7)))
        self.assertFalse(agent.is_in_bedtime(entry, T0.replace(hour=9)))
        self.assertFalse(agent.is_in_bedtime(None, T0))
        self.assertFalse(agent.is_in_bedtime({}, T0))

    def test_migrated_doc_wins(self):
        doc = {'events': {'b-bedtime-1': _bedtime_block()},
               'migrated': 'x'}
        self.assertTrue(agent.is_in_bedtime({}, T0, schedule_doc=doc))
        empty = {'events': {}, 'migrated': 'x'}
        stale = {'start': '07:00', 'end': '23:00', 'days': ALL_DAYS}
        self.assertFalse(agent.is_in_bedtime(stale, T0,
                                             schedule_doc=empty))

    def test_flag_resolver(self):
        doc = {'events': {}, 'migrated': 'x'}
        self.assertFalse(agent._bedtime_flag(
            {'start': '07:00', 'end': '23:00', 'days': ALL_DAYS}, doc, T0))
        legacy = {'start': '07:00', 'end': '23:00', 'days': ALL_DAYS}
        self.assertTrue(agent._bedtime_flag(legacy, None, T0))


class AgentBedtimeVerbTest(_AgentHomeMixin, unittest.TestCase):
    def _form(self, **over):
        form = {'start': '20:00', 'end': '06:00',
                'day_0': 'on', 'day_1': 'on', 'day_2': 'on',
                'day_3': 'on', 'day_4': 'on'}
        form.update(over)
        return form

    def _call(self, form):
        with mock.patch.object(agent, 'get_lock_status',
                               return_value=(False, '')), \
             mock.patch.object(agent, 'run_lock_command',
                               return_value=(True, 'ok')) as lock, \
             mock.patch.object(agent, '_safe_notify', return_value=True), \
             mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False):
            code, payload = agent.do_bedtime(KID, form, 'remote:test@hub')
        return code, payload, lock

    def test_write_through_updates_existing_doc(self):
        self.seed_doc({'events': {'b-bedtime-1': _bedtime_block(
            start='21:00', end='08:00')}, 'migrated': 'y'})
        code, _payload, _lock = self._call(self._form())
        self.assertEqual(code, 200)
        block = self.doc()['events']['b-bedtime-1']
        self.assertEqual((block['start'], block['end']), ('20:00', '06:00'))
        self.assertEqual(block['preset'], 'SCHOOL')
        # entry mirror updated by the verb itself
        self.assertEqual(agent.load_bedtime()[KID]['start'], '20:00')

    def test_clear_removes_bedtime_block_keeps_others(self):
        other = schedule.normalize_block({
            'id': 'b-1', 'target': 'games', 'mode': 'capped',
            'allowance_minutes': 30, 'preset': 'DAILY',
            'dtstart': '2026-10-01', 'start': '16:00', 'end': '18:00'})
        self.seed_doc({'events': {'b-bedtime-1': _bedtime_block(),
                                  'b-1': other}, 'migrated': 'y'})
        code, _payload, _lock = self._call({'clear': '1'})
        self.assertEqual(code, 200)
        doc = self.doc()
        self.assertNotIn('b-bedtime-1', doc['events'])
        self.assertIn('b-1', doc['events'])   # non-bedtime survives

    def test_verb_never_creates_a_doc(self):
        # No doc exists: the agent must NOT migrate (the panel's
        # /api/schedule owns the cutover) — the legacy entry governs.
        code, _payload, _lock = self._call(self._form())
        self.assertEqual(code, 200)
        self.assertIsNone(self.doc())
        entry = agent.load_bedtime()[KID]
        self.assertEqual(entry['start'], '20:00')

    def test_migrated_doc_without_block_governs_unlock(self):
        # Stale mirror says 'now is bedtime'; the doc says no blocks. The
        # post-save inline enforcement must NOT lock.
        self.seed_doc({'events': {}, 'migrated': 'y'})
        self.seed_entry({'start': '07:00', 'end': '23:00',
                         'days': ALL_DAYS, 'locked_by_bedtime': False})
        # Bedtime form outside "now" keeps it simple: assert the resolver
        # the verb uses reads the doc, not the stale mirror.
        flag = agent._bedtime_flag(agent.load_bedtime()[KID],
                                   self.doc(), T0)
        self.assertFalse(flag)

    def test_bonus_revoke_relocks_only_per_doc(self):
        # Migrated doc with an ACTIVE block: revoke re-locks the account.
        self.seed_doc({'events': {'b-bedtime-1': _bedtime_block()},
                       'migrated': 'y'})
        self.seed_entry({'start': '21:00', 'end': '08:00',
                         'days': ALL_DAYS,
                         'bonus_until': (datetime.now()
                                         + timedelta(minutes=30)
                                         ).isoformat(timespec='seconds'),
                         'bonus_minutes': 30,
                         'bonus_history': []})
        with mock.patch.object(agent, 'get_lock_status',
                               return_value=(False, '')), \
             mock.patch.object(agent, 'run_lock_command',
                               return_value=(True, 'ok')) as lock, \
             mock.patch.object(agent, '_safe_notify', return_value=True):
            code, payload = agent.do_bonus_revoke(KID, 'remote:test@hub')
        self.assertEqual(code, 200)
        self.assertTrue(payload['locked'])
        lock.assert_any_call(KID, 'lock', actor='remote:test@hub')


class AgentReadPayloadTest(_AgentHomeMixin, unittest.TestCase):
    def _users(self):
        with mock.patch.object(agent, 'get_human_users',
                               return_value=[
                                   {'username': KID, 'uid': 1001,
                                    'home': '/home/' + KID,
                                    'shell': '/bin/bash'}]), \
             mock.patch.object(agent, 'status_row',
                               return_value=(False, 'ok', True)), \
             mock.patch.object(agent, 'active_session_count',
                               return_value=0), \
             mock.patch.object(agent, '_admin_store_names',
                               return_value=set()), \
             mock.patch.object(agent.auth, 'is_wheel_member',
                               return_value=False):
            return agent.read_users()

    def test_in_bedtime_from_blocks(self):
        self.seed_doc({'events': {'b-bedtime-1': _bedtime_block()},
                       'migrated': 'y'})
        self.seed_entry({'locked_by_bedtime': False})   # no config fields
        rows = self._users()
        self.assertTrue(rows[0]['in_bedtime'])
        self.assertEqual(rows[0]['bedtime'],
                         agent.load_bedtime()[KID])

    def test_migrated_empty_doc_overrides_stale_mirror(self):
        self.seed_doc({'events': {}, 'migrated': 'y'})
        self.seed_entry({'start': '00:00', 'end': '23:59',
                         'days': ALL_DAYS})
        rows = self._users()
        self.assertFalse(rows[0]['in_bedtime'])

    def test_legacy_entry_without_doc_still_reports(self):
        self.seed_entry({'start': '00:00', 'end': '23:59',
                         'days': ALL_DAYS})
        rows = self._users()
        self.assertTrue(rows[0]['in_bedtime'])


if __name__ == '__main__':
    unittest.main()
