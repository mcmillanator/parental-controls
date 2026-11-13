#!/usr/bin/env python3
"""Calendar schedule — budgets-side glue (store v4, migration, shims).

Covers CALENDAR_DESIGN.md's backend half WITHOUT Flask: the 'schedule'
table lifecycle, the idempotent hard-cutover migration, the enforcement
read (legacy windows vs migrated docs — behaviorally identical), the
permitted-block gating, the window write-through, and the single write
funnel (upsert/remove/set_bedtime_block) with its windows mirror sync.

Stdlib only. Temp dirs via TMPDIR (.scratch); no real accounts, no sudo,
no cgroups (act=False / mocked set_frozen), no service state touched.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import audit
import budgets
import schedule
import store
from test_lock_regression import KID

T0 = datetime(2026, 10, 7, 15, 0)          # a Wednesday
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
SCHOOL_DAYS = [0, 1, 2, 3, 4]


def _category_window(target='games', allowance=60, days=None, start='00:00',
                     end='23:59', enabled=True):
    return {'target_type': 'category', 'target': target,
            'days': list(ALL_DAYS if days is None else days),
            'start': start, 'end': end,
            'allowance_minutes': allowance, 'enabled': enabled}


def _steam(pid=9101, cpu=100.0):
    return {'pid': pid, 'comm': 'steam',
            'cmdline': '/home/kid/.local/share/Steam/ubuntu12_32/steam',
            'args': '', 'etime': '01:00:00', 'cpu_seconds': cpu}


def _active_pair(procs):
    return [dict(p, cpu_seconds=p['cpu_seconds'] + 30.0) for p in procs]


class _StateMixin:
    """Temp budgets/overrides/schedule stores (one shared db per dir)."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = self._tmp.name
        patches = [
            mock.patch.object(budgets, 'OVERRIDES_FILE',
                              os.path.join(home, 'overrides.json')),
            mock.patch.object(budgets, 'BUDGETS_FILE',
                              os.path.join(home, 'budgets.json')),
            mock.patch.object(budgets, 'SCHEDULE_FILE',
                              os.path.join(home, 'schedule.json')),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


def _tick(entry, overrides, t0=T0, procs=None, act=False, bedtime=None,
          **kw):
    return budgets.account_and_enforce(
        KID, entry, procs or [], bedtime, t0, prefs={}, index={}, act=act,
        overrides=overrides, **kw)


# ---------------------------------------------------------------------------
# store.py: the 'schedule' table (schema v4)
# ---------------------------------------------------------------------------

class ScheduleTableTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name
        self.json = os.path.join(self.home, 'schedule.json')
        self.db = store.db_path_for(self.json)

    def test_schedule_is_a_doc_table(self):
        self.assertIn('schedule', store.DOC_TABLES)
        self.assertIn('schedule', store.ALL_TABLES)
        self.assertEqual(store.LEGACY_FILES['schedule'], 'schedule.json')

    def test_round_trip(self):
        data = {KID: {'events': {'b-1': {'id': 'b-1', 'target': 'games'}},
                      'migrated': '2026-11-13T08:00:00'}}
        store.save_table(self.db, 'schedule', data, self.json)
        self.assertEqual(store.load_table(self.db, 'schedule'), data)
        with open(self.json) as fh:
            self.assertEqual(json.load(fh), data)   # mirror written

    def test_fresh_db_has_the_table(self):
        conn = store.open_db(self.db)      # materialize + migrate
        conn.close()
        conn = sqlite3.connect(self.db)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn('schedule', names)

    def test_v3_db_upgrades_to_v4(self):
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE bedtime (username TEXT NOT NULL, '
                     'field TEXT NOT NULL, value TEXT NOT NULL, '
                     'PRIMARY KEY (username, field))')
        conn.execute('CREATE TABLE overrides '
                     '(username TEXT PRIMARY KEY, doc TEXT NOT NULL)')
        conn.execute('CREATE TABLE schema_version (version INTEGER)')
        conn.execute('INSERT INTO schema_version VALUES (3)')
        conn.execute("INSERT INTO overrides VALUES ('kid', '{}')")
        conn.commit()
        conn.close()
        data = store.load_table(self.db, 'schedule')
        self.assertEqual(data, {})                   # empty, not crashed
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute('SELECT version FROM schema_version').
                fetchone()[0], store.SCHEMA_VERSION)
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('schedule', names)
            self.assertIn('kid', conn.execute(
                'SELECT username FROM overrides').fetchall()[0])
        finally:
            conn.close()
        # backup-before-migrate ran
        self.assertTrue(os.path.exists(self.db + '.bak.3'))

    def test_legacy_json_absorbed_once(self):
        doc = {KID: {'events': {}, 'migrated': '2026-11-13T08:00:00'}}
        with open(self.json, 'w') as fh:
            json.dump(doc, fh)
        self.assertEqual(store.load_table(self.db, 'schedule'), doc)


# ---------------------------------------------------------------------------
# ensure_migrated — idempotent, deterministic, audit-once
# ---------------------------------------------------------------------------

class EnsureMigratedTest(_StateMixin, unittest.TestCase):
    def test_migrates_bedtime_and_enabled_windows(self):
        bedtime = {KID: {'start': '21:00', 'end': '08:00',
                         'days': SCHOOL_DAYS, 'locked_by_bedtime': True}}
        overrides = {KID: {'app_overrides': {},
                           'windows': {'w-category-games':
                                       _category_window(days=ALL_DAYS),
                                       'w-category-media':
                                       _category_window(target='media',
                                                        enabled=False)}}}
        with mock.patch.object(audit, 'safe_log') as log:
            doc_map, migrated = budgets.ensure_migrated(
                bedtime, overrides, now=T0)
        self.assertEqual(migrated, [KID])
        doc = doc_map[KID]
        self.assertTrue(schedule.doc_is_migrated(doc))
        self.assertEqual(sorted(doc['events']),
                         ['b-bedtime-1', 'w-category-games'])
        bt = doc['events']['b-bedtime-1']
        self.assertEqual(bt['target'], 'bedtime')
        self.assertEqual(bt['preset'], 'SCHOOL')
        self.assertEqual((bt['start'], bt['end']), ('21:00', '08:00'))
        games = doc['events']['w-category-games']
        self.assertEqual(games['mode'], 'capped')
        self.assertEqual(games['allowance_minutes'], 60)
        self.assertEqual(games['preset'], 'DAILY')
        actions = [c.args[1] for c in log.call_args_list]
        self.assertIn('schedule.migrate', actions)
        # disabled window NOT migrated (and the media id is absent)
        self.assertNotIn('w-category-media', doc['events'])

    def test_idempotent_and_persisted(self):
        bedtime = {KID: {'start': '21:00', 'end': '08:00', 'days': ALL_DAYS}}
        overrides = {KID: {'app_overrides': {}, 'windows': {
            'w-category-games': _category_window()}}}
        with mock.patch.object(audit, 'safe_log') as log:
            first, migrated = budgets.ensure_migrated(
                bedtime, overrides, now=T0)
            self.assertEqual(migrated, [KID])
            stored = budgets.load_schedule()
            self.assertEqual(stored, first)
            second, again = budgets.ensure_migrated(
                bedtime, overrides, now=T0 + timedelta(hours=1))
            self.assertEqual(again, [])
            self.assertEqual(second, first)     # byte-identical doc
        self.assertEqual(
            sum(1 for c in log.call_args_list
                if c.args[1] == 'schedule.migrate'), 1)

    def test_marker_for_fully_dead_config_never_resurrects(self):
        overrides = {KID: {'app_overrides': {}, 'windows': {
            'w-category-games': _category_window(enabled=False),
            'bad': {'target_type': 'nope'}}}}
        doc_map, migrated = budgets.ensure_migrated({}, overrides, now=T0)
        self.assertEqual(migrated, [KID])
        self.assertEqual(doc_map[KID]['events'], {})
        self.assertTrue(schedule.doc_is_migrated(doc_map[KID]))

    def test_users_without_legacy_config_get_no_doc(self):
        doc_map, migrated = budgets.ensure_migrated(
            {'': {}, 'bad/../user': {}}, {'good': {'app_overrides': {},
                                                   'windows': {}}}, now=T0)
        self.assertEqual(migrated, [])
        self.assertEqual(doc_map, {})

    def test_migration_preserves_live_bedtime_state(self):
        # The bedtime TABLE row is untouched by migration (mirror/backup).
        entry = {'start': '21:00', 'end': '08:00', 'days': ALL_DAYS,
                 'locked_by_bedtime': True, 'manual_override': True,
                 'bonus_until': '2026-10-07T23:00:00'}
        budgets.ensure_migrated({KID: dict(entry)}, {}, now=T0)
        self.assertEqual(entry['locked_by_bedtime'], True)
        self.assertEqual(entry['bonus_until'], '2026-10-07T23:00:00')


# ---------------------------------------------------------------------------
# Enforcement read: migrated doc vs legacy windows are equivalent
# ---------------------------------------------------------------------------

class TickEquivalenceTest(_StateMixin, unittest.TestCase):
    def test_same_behavior_with_and_without_doc(self):
        windows = {'w-category-games': _category_window(
            days=SCHOOL_DAYS, start='14:00', end='20:00', allowance=30)}
        overrides = {'app_overrides': {}, 'windows': windows}
        bedtime_entry = {'start': '21:00', 'end': '08:00',
                         'days': SCHOOL_DAYS}
        doc = schedule.migrate(bedtime_entry,
                               [budgets.normalize_window(
                                   windows['w-category-games'])], T0)
        entry_a = budgets.blank_entry()
        entry_a['limits'] = {'games': 60}
        entry_b = budgets.blank_entry()
        entry_b['limits'] = {'games': 60}
        procs = [_steam()]
        timeline = [T0, T0 + timedelta(minutes=1),
                    T0 + timedelta(hours=2),        # inside the window
                    T0 + timedelta(hours=6),        # 21:00 bedtime start
                    T0 + timedelta(hours=8, minutes=1)]
        sum_a, sum_b = [], []
        for i, t in enumerate(timeline):
            pair = _active_pair(procs) if i else procs
            sum_a.append(_tick(entry_a, overrides, t0=t, procs=pair,
                               bedtime=bedtime_entry))
            sum_b.append(_tick(entry_b, None, t0=t, procs=pair,
                               bedtime=bedtime_entry,
                               schedule_doc=doc))
        self.assertEqual(sum_a, sum_b)
        self.assertEqual(entry_a, entry_b)
        # and the windows really governed something (not a vacuous pass)
        self.assertTrue(any(s['reset'] or s['froze'] or s['notified']
                            for s in sum_a) or entry_a['window_used'])

    def test_tick_without_doc_stays_hermetic(self):
        # The tick NEVER migrates (mixed-store homes would leak docs into
        # unpatched dirs — see budgets_tick comment): it enforces from the
        # derived blocks and leaves the schedule table untouched.
        budgets.save_budgets({KID: {'limits': {'games': 60},
                                    'used_seconds': {}, 'paused': {},
                                    'resumed': {}, 'manual_frozen': {},
                                    'manual_frozen_at': {},
                                    'warned_low': {}, 'warned_paused': {},
                                    'app_used': {}, 'app_paused': {},
                                    'app_resumed': {},
                                    'app_manual_frozen': {},
                                    'app_manual_frozen_at': {},
                                    'app_warned_low': {},
                                    'app_warned_paused': {},
                                    'window_used': {}, 'window_active': {},
                                    'last_cpu': {}, 'day': '2026-10-07'}})
        budgets.save_overrides({KID: {'app_overrides': {}, 'windows': {
            'w-category-games': _category_window(days=ALL_DAYS)}}})
        bedtime = {KID: {'start': '21:00', 'end': '08:00',
                         'days': SCHOOL_DAYS}}
        with mock.patch.object(audit, 'safe_log'):
            budgets.tick_budgets(bedtime, T0, procs_provider=lambda u: [],
                                 prefs={}, act=False, run_watchdog=False,
                                 index={})
        self.assertEqual(budgets.load_schedule(), {})   # no side effects
        entry = budgets.load_budgets()[KID]
        self.assertTrue(entry['window_active']['w-category-games'])

    def test_tick_reads_migrated_doc(self):
        # Once the panel cut the user over, the tick enforces the doc —
        # including blocks that no longer exist in the legacy mirror.
        budgets.save_budgets({KID: {'limits': {'games': 60},
                                    'used_seconds': {}, 'paused': {},
                                    'resumed': {}, 'manual_frozen': {},
                                    'manual_frozen_at': {},
                                    'warned_low': {}, 'warned_paused': {},
                                    'app_used': {}, 'app_paused': {},
                                    'app_resumed': {},
                                    'app_manual_frozen': {},
                                    'app_manual_frozen_at': {},
                                    'app_warned_low': {},
                                    'app_warned_paused': {},
                                    'window_used': {}, 'window_active': {},
                                    'last_cpu': {}, 'day': '2026-10-07'}})
        with mock.patch.object(audit, 'safe_log'):
            budgets.upsert_block(KID, {
                'id': 'b-cal-1', 'target': 'games', 'mode': 'capped',
                'allowance_minutes': 25, 'preset': 'DAILY',
                'dtstart': '2026-10-06', 'start': '14:00', 'end': '18:00'})
        with mock.patch.object(audit, 'safe_log'):
            budgets.tick_budgets({}, T0, procs_provider=lambda u: [],
                                 prefs={}, act=False, run_watchdog=False,
                                 index={})
        entry = budgets.load_budgets()[KID]
        self.assertTrue(entry['window_active']['b-cal-1'])

    def test_legacy_window_ids_survive_into_runtime(self):
        entry = budgets.blank_entry()
        doc = schedule.migrate(None, [budgets.normalize_window(
            _category_window(days=SCHOOL_DAYS))], T0)
        _tick(entry, None, schedule_doc=doc)
        self.assertIn('w-category-games', entry['window_active'])


# ---------------------------------------------------------------------------
# Permitted-block gating (owner rule #2) — dry-run state machine
# ---------------------------------------------------------------------------

class PermittedGatingTest(unittest.TestCase):
    def _permitted(self, start, end, target='games', preset='DAILY'):
        block = schedule.normalize_block({
            'id': 'p-1', 'target': target, 'mode': 'permitted',
            'preset': preset, 'dtstart': (T0 - timedelta(days=1)).date(),
            'start': start, 'end': end, 'enabled': True})
        assert block is not None
        return {'events': {'p-1': block}, 'migrated': 'x'}

    def _gov(self):
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = '2026-10-07'
        return entry

    def test_inside_permitted_no_billing_and_thaws(self):
        entry = self._gov()
        entry['paused'] = {'games': True}
        doc = self._permitted('14:00', '16:00')
        with mock.patch.object(budgets, 'set_frozen',
                               return_value=(True, '')) as frozen:
            summary = _tick(entry, None, t0=T0.replace(hour=15),
                            procs=_active_pair([_steam()]),
                            schedule_doc=doc, act=True)
        frozen.assert_any_call(KID, 'games', False)
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)
        self.assertNotIn('games', entry['paused'])
        self.assertNotIn('games (dry-run)', summary['froze'])

    def test_outside_permitted_freezes(self):
        entry = self._gov()
        doc = self._permitted('17:00', '19:00')
        summary = _tick(entry, None, t0=T0, procs=[],
                        schedule_doc=doc, act=False)
        self.assertIn('games (dry-run)', summary['froze'])
        self.assertTrue(entry['paused']['games'])

    def test_permitted_beats_capped_while_active(self):
        entry = self._gov()
        capped = schedule.normalize_block({
            'id': 'w-category-games', 'target': 'games', 'mode': 'capped',
            'allowance_minutes': 30, 'preset': 'DAILY',
            'dtstart': (T0 - timedelta(days=1)).date(),
            'start': '00:00', 'end': '23:59', 'enabled': True})
        permitted = schedule.normalize_block({
            'id': 'p-1', 'target': 'games', 'mode': 'permitted',
            'preset': 'DAILY', 'dtstart': (T0 - timedelta(days=1)).date(),
            'start': '14:00', 'end': '16:00', 'enabled': True})
        doc = {'events': {b['id']: b for b in (capped, permitted)},
               'migrated': 'x'}
        _tick(entry, None, t0=T0.replace(hour=15),
              procs=_active_pair([_steam()]), schedule_doc=doc)
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)
        self.assertEqual(entry['window_used'].get('w-category-games',
                                                  0.0), 0.0)

    def test_gated_app_override_target(self):
        entry = self._gov()
        entry['app_used'] = {}
        overrides = {'app_overrides': {'steam': {'mode': 'limit',
                                                 'minutes': 10}},
                     'windows': {}}
        doc = self._permitted('17:00', '19:00', target='app:steam')
        summary = _tick(entry, overrides, t0=T0, procs=[],
                        schedule_doc=doc, act=False)
        self.assertIn('app:steam (dry-run)', summary['froze'])
        self.assertTrue(entry['app_paused']['steam'])

    def test_no_blocks_no_gating(self):
        entry = self._gov()
        doc = {'events': {}, 'migrated': 'x'}
        _tick(entry, None, t0=T0, procs=[_steam()],
              schedule_doc=doc, act=False)
        summary = _tick(entry, None, t0=T0 + timedelta(minutes=1),
                        procs=_active_pair([_steam()]),
                        schedule_doc=doc, act=False)
        self.assertEqual(summary['froze'], [])
        self.assertAlmostEqual(entry['used_seconds']['games'], 60.0)


# ---------------------------------------------------------------------------
# Window write-through + single write funnel (upsert/remove/bedtime)
# ---------------------------------------------------------------------------

class WriteThroughTest(_StateMixin, unittest.TestCase):
    def _migrate_first(self):
        budgets.ensure_migrated({KID: {'start': '21:00', 'end': '08:00',
                                       'days': ALL_DAYS}}, {}, now=T0)

    def test_unmigrated_window_set_does_not_create_doc(self):
        with mock.patch.object(audit, 'safe_log'):
            ok, _msg = budgets.set_window(KID, 'category', 'games',
                                          ALL_DAYS, '16:00', '18:00', 60)
        self.assertTrue(ok)
        self.assertEqual(budgets.load_schedule(), {})

    def test_migrated_window_writes_sync_blocks(self):
        self._migrate_first()
        with mock.patch.object(audit, 'safe_log'):
            ok, _msg = budgets.set_window(KID, 'category', 'games',
                                          ALL_DAYS, '16:00', '18:00', 60)
            self.assertTrue(ok)
            doc = budgets.load_schedule()[KID]
            self.assertIn('w-category-games', doc['events'])
            self.assertEqual(doc['events']['w-category-games']
                             ['allowance_minutes'], 60)
            ok, _msg = budgets.update_window(KID, 'w-category-games',
                                             allowance_minutes=90)
            self.assertTrue(ok)
            doc = budgets.load_schedule()[KID]
            self.assertEqual(doc['events']['w-category-games']
                             ['allowance_minutes'], 90)
            ok, _msg = budgets.clear_window(KID, window_id='w-category-games')
            self.assertTrue(ok)
            doc = budgets.load_schedule()[KID]
            self.assertNotIn('w-category-games', doc['events'])
            self.assertIn('b-bedtime-1', doc['events'])   # untouched

    def test_upsert_block_stamps_marker_and_syncs_mirror(self):
        with mock.patch.object(audit, 'safe_log'):
            ok, _msg = budgets.upsert_block(KID, {
                'id': 'b-1', 'target': 'games', 'mode': 'capped',
                'allowance_minutes': 45, 'preset': 'SCHOOL',
                'dtstart': '2026-11-13', 'start': '16:00', 'end': '18:00'})
        self.assertTrue(ok)
        doc = budgets.load_schedule()[KID]
        self.assertTrue(schedule.doc_is_migrated(doc))
        mirrored = budgets.load_overrides()[KID]['windows']['b-1']
        self.assertEqual(mirrored['days'], SCHOOL_DAYS)
        self.assertEqual(mirrored['allowance_minutes'], 45)

    def test_permitted_blocks_are_not_in_the_windows_mirror(self):
        with mock.patch.object(audit, 'safe_log'):
            budgets.upsert_block(KID, {
                'id': 'p-1', 'target': 'media', 'mode': 'permitted',
                'preset': 'DAILY', 'dtstart': '2026-11-13',
                'start': '16:00', 'end': '18:00'})
        entry = budgets.load_overrides().get(KID, {})
        self.assertEqual(entry.get('windows'), {})

    def test_upsert_rejects_same_target_overlap(self):
        block = {'target': 'games', 'mode': 'capped',
                 'allowance_minutes': 30, 'preset': 'DAILY',
                 'dtstart': '2026-11-13', 'start': '16:00', 'end': '18:00'}
        with mock.patch.object(audit, 'safe_log'):
            ok, _msg = budgets.upsert_block(KID, dict(block, id='b-1'))
            self.assertTrue(ok)
            ok, msg = budgets.upsert_block(KID, dict(block, id='b-2',
                                                     start='17:00'))
            self.assertFalse(ok)
            self.assertIn('b-1', msg)
            # different target overlaps freely
            ok, _msg = budgets.upsert_block(KID, dict(block, id='b-3',
                                                      target='media'))
            self.assertTrue(ok)
            # adjacent is fine
            ok, _msg = budgets.upsert_block(KID, dict(block, id='b-4',
                                                      start='18:00',
                                                      end='20:00'))
            self.assertTrue(ok)

    def test_upsert_rejects_malformed_blocks(self):
        with mock.patch.object(audit, 'safe_log'):
            for bad in ({'id': 'x'},
                        {'id': 'x', 'target': 'nope', 'mode': 'permitted',
                         'preset': 'DAILY', 'dtstart': '2026-11-13',
                         'start': '16:00', 'end': '18:00'},
                        {'id': 'x', 'target': 'games', 'mode': 'capped',
                         'allowance_minutes': 1, 'preset': 'DAILY',
                         'dtstart': '2026-11-13', 'start': '16:00',
                         'end': '18:00'}):
                ok, _msg = budgets.upsert_block(KID, bad)
                self.assertFalse(ok)

    def test_set_bedtime_block_replaces_only_bedtime(self):
        with mock.patch.object(audit, 'safe_log'):
            budgets.upsert_block(KID, {
                'id': 'b-1', 'target': 'games', 'mode': 'capped',
                'allowance_minutes': 30, 'preset': 'DAILY',
                'dtstart': '2026-11-13', 'start': '16:00', 'end': '18:00'})
            ok, _msg = budgets.set_bedtime_block(KID, SCHOOL_DAYS,
                                                 '20:00', '06:00')
            self.assertTrue(ok)
        doc = budgets.load_schedule()[KID]
        self.assertEqual(sorted(doc['events']), ['b-1', 'b-bedtime-1'])
        self.assertEqual(doc['events']['b-bedtime-1']['preset'], 'SCHOOL')
        self.assertEqual(doc['events']['b-bedtime-1']['target'], 'bedtime')
        # a second write REPLACES the bedtime block, never adds a second
        with mock.patch.object(audit, 'safe_log'):
            budgets.set_bedtime_block(KID, [5, 6], '21:00', '07:00')
        doc = budgets.load_schedule()[KID]
        self.assertEqual(doc['events']['b-bedtime-1']['preset'],
                         'WEEKLY:BYDAY=SA,SU')
        # clearing the old editor removes ALL bedtime blocks
        with mock.patch.object(audit, 'safe_log'):
            ok, _msg = budgets.set_bedtime_block(KID, [], '', '')
        self.assertTrue(ok)
        doc = budgets.load_schedule()[KID]
        self.assertNotIn('b-bedtime-1', doc['events'])
        self.assertIn('b-1', doc['events'])

    def test_remove_block(self):
        with mock.patch.object(audit, 'safe_log'):
            budgets.upsert_block(KID, {
                'id': 'b-1', 'target': 'games', 'mode': 'capped',
                'allowance_minutes': 30, 'preset': 'DAILY',
                'dtstart': '2026-11-13', 'start': '16:00', 'end': '18:00'})
            ok, _msg = budgets.remove_block(KID, 'b-1')
            self.assertTrue(ok)
            ok, _msg = budgets.remove_block(KID, 'b-1')
            self.assertFalse(ok)
        self.assertEqual(budgets.load_schedule()[KID]['events'], {})
        self.assertEqual(budgets.load_overrides()[KID]['windows'], {})


# ---------------------------------------------------------------------------
# budget_status view over the calendar
# ---------------------------------------------------------------------------

class BudgetStatusCalendarTest(_StateMixin, unittest.TestCase):
    def test_capped_block_reports_as_window(self):
        budgets.save_budgets({KID: {
            'limits': {'games': 60}, 'used_seconds': {'games': 120.0},
            'paused': {}, 'resumed': {}, 'manual_frozen': {},
            'manual_frozen_at': {}, 'warned_low': {}, 'warned_paused': {},
            'app_used': {}, 'app_paused': {}, 'app_resumed': {},
            'app_manual_frozen': {}, 'app_manual_frozen_at': {},
            'app_warned_low': {}, 'app_warned_paused': {},
            'window_used': {'b-1': 600.0}, 'window_active': {},
            'last_cpu': {}, 'day': '2026-10-07'}})
        with mock.patch.object(audit, 'safe_log'):
            budgets.upsert_block(KID, {
                'id': 'b-1', 'target': 'games', 'mode': 'capped',
                'allowance_minutes': 30, 'preset': 'DAILY',
                'dtstart': '2026-11-13', 'start': '16:00', 'end': '18:00'})
        doc = budgets.load_schedule()[KID]
        when_in = datetime(2026, 11, 13, 17, 0)   # Fri, inside 16:00-18:00
        when_out = datetime(2026, 11, 13, 20, 0)
        status = budgets.budget_status(KID, overrides={}, now=when_in)
        games = status['games']
        self.assertEqual(games['effective_source'], 'window')
        self.assertTrue(games['window_active'])
        self.assertEqual(games['window_allowance_minutes'], 30)
        # outside the block: daily governs
        status = budgets.budget_status(KID, overrides={}, now=when_out)
        self.assertEqual(status['games']['effective_source'], 'daily')
        self.assertFalse(status['games']['window_active'])
        del doc

    def test_dead_blocks_in_the_doc_fail_closed(self):
        budgets.save_budgets({KID: {
            'limits': {}, 'used_seconds': {}, 'paused': {}, 'resumed': {},
            'manual_frozen': {}, 'manual_frozen_at': {}, 'warned_low': {},
            'warned_paused': {}, 'app_used': {}, 'app_paused': {},
            'app_resumed': {}, 'app_manual_frozen': {},
            'app_manual_frozen_at': {}, 'app_warned_low': {},
            'app_warned_paused': {}, 'window_used': {},
            'window_active': {}, 'last_cpu': {}}})
        db = store.db_path_for(budgets.OVERRIDES_FILE)
        store.save_table(db, 'schedule', {KID: {
            'events': {'junk': {'target': 'nope'},
                       'w-ok': _category_window(days=SCHOOL_DAYS,
                                                allowance=30,
                                                start='16:00',
                                                end='18:00')},
            'migrated': 'x'}}, budgets.SCHEDULE_FILE)
        # the junk block is absent; enforcement never sees it
        blocks = budgets.user_windows(KID, {},
                                      budgets.load_schedule()[KID])
        self.assertEqual([b['id'] for b in blocks], [])
        status = budgets.budget_status(KID, overrides={},
                                       now=datetime(2026, 11, 13, 17, 0))
        self.assertEqual(status['games']['effective_source'], 'none')


if __name__ == '__main__':
    unittest.main()
