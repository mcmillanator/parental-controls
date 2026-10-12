#!/usr/bin/env python3
"""Tests for the v2 per-field SQLite schema (store.py SCHEMA_VERSION 2).

The v2 store keeps the same table names but stores each user's entry as
one row per named field ``(username, field, value)`` with a single
stable value type (TEXT holding the JSON encoding of that field);
``load_*`` reconstructs the identical whole-document dict shape so
callers are untouched.

Covered here:
* migration backward-fidelity: a genuine v1 database (whole-document
  rows) upgraded to v2 yields byte-identical ``load_*`` output for all
  three stores, including bonus keys, ``manual_frozen``, empty-dict and
  scalar entries;
* field-granular persistence: changing one field changes only that
  field's row on disk; untouched users are untouched; removed users are
  dropped;
* schema_version bump + pre-migration backup for the v1->v2 step;
* rollback mirror still written atomically and re-importable;
* fail-closed corruption behaviour unchanged;
* caller-facing round-trips (app/budgets load_*/save_*) unchanged.

Stdlib only; every database lives under a temp dir.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import budgets
import store


def version_of(db_path):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            'SELECT version FROM schema_version LIMIT 1').fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


class V2Case(unittest.TestCase):
    """Fresh temp state home per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.db = os.path.join(self.home, 'state.db')
        self.bedtime_json = os.path.join(self.home, 'bedtime.json')
        self.budgets_json = os.path.join(self.home, 'budgets.json')
        self.prefs_json = os.path.join(self.home, 'process_prefs.json')

    def field_rows(self, table):
        """On-disk (username, field, value) rows as a set (JSON-raw)."""
        conn = sqlite3.connect(self.db)
        try:
            return set(conn.execute(
                'SELECT username, field, value FROM %s' % table))
        finally:
            conn.close()

    def column_names(self, table):
        conn = sqlite3.connect(self.db)
        try:
            return [row[1] for row in conn.execute(
                'PRAGMA table_info(%s)' % table)]
        finally:
            conn.close()

    def _make_v1_db(self, data_by_table):
        """Authentic v1 database: all three whole-document tables present.

        (A real v1 schema always creates every store table, so the test
        builder does too; tables without data are left empty.)
        """
        conn = sqlite3.connect(self.db)
        try:
            conn.execute('CREATE TABLE schema_version '
                         '(version INTEGER NOT NULL)')
            conn.execute('INSERT INTO schema_version (version) VALUES (1)')
            for table in store.TABLES:
                conn.execute(
                    'CREATE TABLE %s '
                    '(username TEXT PRIMARY KEY, doc TEXT NOT NULL)' % table)
                for username, doc in data_by_table.get(table, {}).items():
                    conn.execute(
                        'INSERT INTO %s (username, doc) VALUES (?, ?)'
                        % table, (username, json.dumps(doc)))
            conn.commit()
        finally:
            conn.close()


# Full-featured fixtures: bonus keys, manual_frozen*, every budgets
# shape, and prefs. Whether saved through v2 directly or written into a
# v1 database and migrated, load_* must reproduce these exactly.
BEDTIME = {'kid': {
    'start': '21:00', 'end': '07:00', 'days': [0, 1, 2, 3, 4, 5, 6],
    'locked_by_bedtime': False, 'manual_override': True,
    'bonus_until': '2026-10-10T03:45:00', 'bonus_minutes': 30,
    'bonus_granted_by': 'parent',
    'bonus_history': [
        {'kind': 'grant', 'minutes': 30, 'by': 'parent',
         'until': '2026-10-10T03:45:00', 'at': '2026-10-09T21:00:00'}],
    'warned_for': '2026-10-09-21:00-2026-10-10-07:00',
}}
BUDGETS = {'kid': {
    'limits': {'games': 60, 'chat': 45},
    'day': '2026-10-07',
    'used_seconds': {'games': 150.5, 'chat': 0.0},
    'last_cpu': {'steam': 12.25},
    'paused': {'games': True},
    'resumed': {'chat': True},
    'manual_frozen': {'games': True},
    'manual_frozen_at': {'games': '2026-10-07T18:00:00'},
    'warned_low': {'games': '2026-10-07'},
    'warned_paused': {'chat': '2026-10-07'},
    'was_in_bedtime': False,
    'last_tick': '2026-10-07T17:59:00',
}}
PREFS = {'kid': {'hidden_comm': ['steam', 'discord'],
                 'shown_comm': ['krita']}}


class TestV2Schema(V2Case):
    def test_fresh_db_uses_per_field_layout(self):
        conn = store.open_db(self.db)
        conn.close()
        for table in store.TABLES:
            self.assertEqual(self.column_names(table),
                             ['username', 'field', 'value'])
        self.assertEqual(version_of(self.db), store.SCHEMA_VERSION)

    def test_value_column_is_one_stable_type(self):
        store.save_table(self.db, 'bedtime', {'kid': {'start': '21:00'}},
                         self.bedtime_json)
        conn = sqlite3.connect(self.db)
        try:
            info = {row[1]: row for row in conn.execute(
                'PRAGMA table_info(bedtime)')}
        finally:
            conn.close()
        self.assertEqual(info['field'][1], 'field')
        self.assertEqual(info['field'][2], 'TEXT')
        self.assertEqual(info['value'][2], 'TEXT')
        self.assertTrue(info['value'][3])  # NOT NULL

    def test_per_field_rows_in_all_three_tables(self):
        store.save_table(self.db, 'budgets', BUDGETS, self.budgets_json)
        rows = self.field_rows('budgets')
        self.assertIn(('kid', 'limits', json.dumps(BUDGETS['kid']['limits'])),
                      rows)
        self.assertIn(('kid', 'manual_frozen',
                       json.dumps(BUDGETS['kid']['manual_frozen'])), rows)
        self.assertIn(('kid', 'last_tick', '"2026-10-07T17:59:00"'), rows)
        self.assertIn(('kid', 'was_in_bedtime', 'false'), rows)


class TestMigrationFidelity(V2Case):
    """A real v1 database (whole-document rows) upgraded to v2 must read
    back identically through the unchanged load_* contract."""

    def test_v1_to_v2_identical_reads_for_all_stores(self):
        self._make_v1_db({'bedtime': BEDTIME, 'budgets': BUDGETS,
                          'process_prefs': PREFS})
        self.assertEqual(store.load_table(self.db, 'bedtime'), BEDTIME)
        self.assertEqual(store.load_table(self.db, 'budgets'), BUDGETS)
        self.assertEqual(store.load_table(self.db, 'process_prefs'), PREFS)
        # The store is now on v2 and stores per-field rows.
        self.assertEqual(version_of(self.db), store.SCHEMA_VERSION)
        self.assertEqual(self.column_names('bedtime'),
                         ['username', 'field', 'value'])
        self.assertNotIn(('kid', 'start', '21:00'), self.field_rows('bedtime'))
        self.assertIn(('kid', 'start', '"21:00"'), self.field_rows('bedtime'))

    def test_v1_migration_preserves_empty_and_scalar_entries(self):
        data = {'empty_user': {}, 'scalar_user': 'just-a-string'}
        self._make_v1_db({'bedtime': data})
        self.assertEqual(store.load_table(self.db, 'bedtime'), data)
        # The empty dict is held whole under the reserved field.
        self.assertIn(('empty_user', store.WHOLE_DOC_FIELD, '{}'),
                      self.field_rows('bedtime'))
        self.assertIn(('scalar_user', store.WHOLE_DOC_FIELD,
                       '"just-a-string"'), self.field_rows('bedtime'))

    def test_v1_garbage_doc_row_skipped_in_migration(self):
        self._make_v1_db({'bedtime': BEDTIME})
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO bedtime (username, doc) "
                         "VALUES ('garbage', '{nope')")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(store.load_table(self.db, 'bedtime'), BEDTIME)

    def test_v1_bonus_keys_and_manual_frozen_intact(self):
        # Explicit spot-check on the values the task called out.
        self._make_v1_db({'bedtime': BEDTIME, 'budgets': BUDGETS})
        loaded_bedtime = store.load_table(self.db, 'bedtime')
        loaded_budgets = store.load_table(self.db, 'budgets')
        self.assertEqual(loaded_bedtime['kid']['bonus_until'],
                         BEDTIME['kid']['bonus_until'])
        self.assertEqual(loaded_bedtime['kid']['bonus_history'],
                         BEDTIME['kid']['bonus_history'])
        self.assertEqual(loaded_bedtime['kid']['warned_for'],
                         BEDTIME['kid']['warned_for'])
        self.assertEqual(loaded_budgets['kid']['manual_frozen'],
                         {'games': True})
        self.assertEqual(loaded_budgets['kid']['manual_frozen_at'],
                         {'games': '2026-10-07T18:00:00'})
        self.assertEqual(loaded_budgets['kid']['last_tick'],
                         '2026-10-07T17:59:00')


class TestFieldGranularPersistence(V2Case):
    def test_single_field_change_differs_only_that_field_on_disk(self):
        entry = {'kid': {'start': '21:00', 'end': '07:00', 'days': [0, 1]}}
        store.save_table(self.db, 'bedtime', entry, self.bedtime_json)
        before = self.field_rows('bedtime')
        changed = {'kid': {'start': '22:00', 'end': '07:00', 'days': [0, 1]}}
        store.save_table(self.db, 'bedtime', changed, self.bedtime_json)
        after = self.field_rows('bedtime')
        self.assertEqual(before - after, {('kid', 'start', '"21:00"')})
        self.assertEqual(after - before, {('kid', 'start', '"22:00"')})
        self.assertEqual(store.load_table(self.db, 'bedtime'), changed)

    def test_untouched_user_rows_untouched(self):
        store.save_table(self.db, 'budgets', {
            'kid': {'limits': {'games': 60}, 'day': None},
            'other': {'limits': {'chat': 30}, 'day': '2026-10-07'},
        }, self.budgets_json)
        before = self.field_rows('budgets')
        store.save_table(self.db, 'budgets', {
            'kid': {'limits': {'games': 90}, 'day': None},
            'other': {'limits': {'chat': 30}, 'day': '2026-10-07'},
        }, self.budgets_json)
        after = self.field_rows('budgets')
        # 'other' is byte-identical; only kid's limits row changed.
        for row in before:
            if row[0] == 'other':
                self.assertIn(row, after)
        self.assertEqual(before - after,
                         {('kid', 'limits', '{"games": 60}')})
        self.assertEqual(after - before,
                         {('kid', 'limits', '{"games": 90}')})

    def test_removed_user_rows_deleted(self):
        store.save_table(self.db, 'process_prefs', {
            'kid': {'hidden_comm': ['steam'], 'shown_comm': []},
            'gone': {'hidden_comm': [], 'shown_comm': ['x']},
        }, self.prefs_json)
        store.save_table(self.db, 'process_prefs', {
            'kid': {'hidden_comm': ['steam'], 'shown_comm': []},
        }, self.prefs_json)
        self.assertEqual(store.load_table(self.db, 'process_prefs'),
                         {'kid': {'hidden_comm': ['steam'],
                                  'shown_comm': []}})
        self.assertFalse(any(row[0] == 'gone'
                             for row in self.field_rows('process_prefs')))

    def test_empty_and_scalar_entries_roundtrip_v2(self):
        data = {'empty_user': {}, 'scalar_user': 'just-a-string'}
        store.save_table(self.db, 'bedtime', data, self.bedtime_json)
        self.assertEqual(store.load_table(self.db, 'bedtime'), data)


class TestMigrationBackupV1(V2Case):
    def test_v1_backup_created_and_version_bumped(self):
        self._make_v1_db({'bedtime': BEDTIME})
        self.assertEqual(store.load_table(self.db, 'bedtime'), BEDTIME)
        backup = self.db + '.bak.1'
        self.assertTrue(os.path.isfile(backup))
        self.assertEqual(version_of(self.db), store.SCHEMA_VERSION)
        conn = sqlite3.connect(backup)
        try:
            # The backup is the untouched v1 snapshot: old schema, old rows.
            self.assertEqual(
                conn.execute('SELECT version FROM schema_version')
                .fetchone(), (1,))
            self.assertEqual(
                [r[1] for r in conn.execute('PRAGMA table_info(bedtime)')],
                ['username', 'doc'])
            self.assertEqual(
                conn.execute("SELECT doc FROM bedtime WHERE username='kid'")
                .fetchone(), (json.dumps(BEDTIME['kid']),))
        finally:
            conn.close()

    def test_no_rebackup_on_clean_v2_open(self):
        self._make_v1_db({'bedtime': BEDTIME})
        store.load_table(self.db, 'bedtime')
        self.assertTrue(os.path.isfile(self.db + '.bak.1'))
        os.unlink(self.db + '.bak.1')
        store.load_table(self.db, 'bedtime')
        store.save_table(self.db, 'bedtime', BEDTIME, self.bedtime_json)
        self.assertFalse(os.path.isfile(self.db + '.bak.1'))
        self.assertFalse(os.path.isfile(self.db + '.bak.2'))


class TestMirrorAndRollback(V2Case):
    def test_mirror_still_written_atomically_and_reimportable(self):
        data = {'kid': {'start': '21:00', 'end': '07:00',
                        'locked_by_bedtime': True}}
        store.save_table(self.db, 'bedtime', data, self.bedtime_json)
        with open(self.bedtime_json) as f:
            self.assertEqual(json.load(f), data)
        os.unlink(self.db)
        self.assertEqual(store.load_table(self.db, 'bedtime'), data)
        # Re-imported through the mirror is per-field on disk again.
        self.assertIn(('kid', 'locked_by_bedtime', 'true'),
                      self.field_rows('bedtime'))

    def test_mirror_matches_every_v2_save(self):
        for data in ({}, {'a': {'x': 1}}, {'a': {'x': 2}, 'b': {}},
                     {'b': {}}):
            store.save_table(self.db, 'budgets', data, self.budgets_json)
            with open(self.budgets_json) as f:
                self.assertEqual(json.load(f), data)
            self.assertEqual(store.load_table(self.db, 'budgets'), data)


class TestFailClosedV2(V2Case):
    def test_corrupt_db_loads_empty_save_raises_oserror(self):
        with open(self.db, 'wb') as f:
            f.write(os.urandom(256))
        self.assertEqual(store.load_table(self.db, 'bedtime'), {})
        self.assertEqual(store.load_table(self.db, 'budgets'), {})
        with self.assertRaises(OSError):
            store.save_table(self.db, 'budgets', {'kid': {}},
                             self.budgets_json)

    def test_unparseable_v2_field_value_skipped(self):
        store.save_table(self.db, 'bedtime',
                         {'kid': {'start': '21:00', 'end': '07:00'}},
                         self.bedtime_json)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO bedtime (username, field, value) "
                         "VALUES ('kid', 'days', '{nope')")
            conn.commit()
        finally:
            conn.close()
        # Only the unparseable field is skipped; the user survives.
        self.assertEqual(store.load_table(self.db, 'bedtime'),
                         {'kid': {'start': '21:00', 'end': '07:00'}})


class TestPublicApiV2(unittest.TestCase):
    """load_*/save_* through app/budgets keep identical shapes on v2."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = self.tmp.name
        self.bedtime_patch = mock.patch.object(
            app, 'BEDTIME_FILE', os.path.join(home, 'bedtime.json'))
        self.budgets_patch = mock.patch.object(
            budgets, 'BUDGETS_FILE', os.path.join(home, 'budgets.json'))
        self.prefs_patch = mock.patch.object(
            budgets, 'PROCESS_PREFS_FILE',
            os.path.join(home, 'process_prefs.json'))
        self.bedtime_patch.start()
        self.budgets_patch.start()
        self.prefs_patch.start()
        self.addCleanup(self.bedtime_patch.stop)
        self.addCleanup(self.budgets_patch.stop)
        self.addCleanup(self.prefs_patch.stop)

    def test_callers_see_identical_shapes_on_v2(self):
        self.assertEqual(app.load_bedtime(), {})
        entry = budgets.blank_entry()
        entry.update({
            'limits': {'games': 60},
            'used_seconds': {'games': 150.5},
            'manual_frozen': {'games': True},
            'manual_frozen_at': {'games': '2026-10-07T18:00:00'},
            'last_tick': None})
        budgets.save_budgets({'kid': entry})
        self.assertEqual(budgets.load_budgets(), {'kid': entry})
        prefs = {'kid': {'hidden_comm': ['steam', 'discord'],
                         'shown_comm': []}}
        budgets.save_prefs(prefs)
        self.assertEqual(budgets.load_prefs(), prefs)
        bedtime = {'kid': {'start': '21:00', 'end': '07:00',
                           'days': [0, 1], 'bonus_until':
                           '2026-10-10T03:45:00',
                           'bonus_history': [{'kind': 'grant',
                                              'minutes': 30,
                                              'by': 'parent'}]}}
        app.save_bedtime(bedtime)
        self.assertEqual(app.load_bedtime(), bedtime)

    def test_v2_store_failure_surfaces_as_oserror(self):
        db = store.db_path_for(app.BEDTIME_FILE)
        store.save_table(db, 'bedtime', {'kid': {'start': '21:00'}}, None)
        with open(db, 'wb') as f:
            f.write(os.urandom(128))
        with self.assertRaises(OSError):
            app.save_bedtime({'kid': {}})
        self.assertEqual(app.load_bedtime(), {})


if __name__ == '__main__':
    unittest.main()