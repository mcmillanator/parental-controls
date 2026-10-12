#!/usr/bin/env python3
"""Tests for the SQLite runtime-state backend (store.py).

Covers fresh-init schema version, migration from real JSON fixtures,
backup-before-migrate, fail-closed corruption recovery, concurrent
writers, busy-timeout behavior, and the rollback story (delete the db
-> re-import from the JSON mirror).

Stdlib only. All databases live under temp dirs; the real store and
the tester/bluefox/stan accounts are never touched.
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
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


class StoreCase(unittest.TestCase):
    """Fresh temp state home per test (db travels with the JSON names)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.db = os.path.join(self.home, 'state.db')
        self.bedtime_json = os.path.join(self.home, 'bedtime.json')
        self.budgets_json = os.path.join(self.home, 'budgets.json')
        self.prefs_json = os.path.join(self.home, 'process_prefs.json')

    def tearDown(self):
        self.tmp.cleanup()

    def write_json(self, path, data):
        with open(path, 'w') as f:
            json.dump(data, f)


class TestFreshInit(StoreCase):
    def test_default_path_lives_next_to_store(self):
        # No checkout/home hardcoding: the production path derives from
        # the module location, and all three legacy names map to it.
        self.assertEqual(store.default_db_path(),
                         os.path.join(store.BASE_DIR, 'state.db'))
        for name in ('bedtime.json', 'budgets.json', 'process_prefs.json'):
            self.assertEqual(
                store.db_path_for(os.path.join(store.BASE_DIR, name)),
                store.default_db_path())

    def test_schema_version_on_fresh_init(self):
        conn = store.open_db(self.db)
        conn.close()
        self.assertEqual(store.load_table(self.db, 'bedtime'), {})
        self.assertEqual(version_of(self.db), store.SCHEMA_VERSION)

    def test_pure_read_creates_nothing(self):
        self.assertEqual(store.load_table(self.db, 'bedtime'), {})
        self.assertFalse(os.path.exists(self.db))

    def test_tables_exist(self):
        store.save_table(self.db, 'budgets', {}, self.budgets_json)
        conn = sqlite3.connect(self.db)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertTrue({'schema_version', 'bedtime', 'budgets',
                         'process_prefs'} <= tables)

    def test_wal_mode(self):
        store.save_table(self.db, 'bedtime', {}, self.bedtime_json)
        conn = sqlite3.connect(self.db)
        try:
            mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), 'wal')

    def test_busy_timeout_set_on_open(self):
        conn = store.open_db(self.db)
        try:
            timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(timeout, store.BUSY_TIMEOUT_MS)

    def test_missing_everything_returns_empty(self):
        self.assertEqual(store.load_table(self.db, 'bedtime'), {})
        self.assertEqual(store.load_table(self.db, 'budgets'), {})
        self.assertEqual(store.load_table(self.db, 'process_prefs'), {})


class TestLegacyImport(StoreCase):
    BEDTIME = {'kid': {'start': '21:00', 'end': '07:00',
                       'days': [0, 1, 2, 3, 4, 5, 6],
                       'locked_by_bedtime': False,
                       'bonus_until': '2026-10-10T03:45:00',
                       'bonus_history': [{'kind': 'grant', 'minutes': 30}]}}
    BUDGETS = {'kid': {'limits': {'games': 60}, 'day': '2026-10-07',
                       'used_seconds': {'games': 150.0}, 'last_cpu': {},
                       'paused': {}, 'resumed': {}, 'warned_low': {},
                       'warned_paused': {}, 'was_in_bedtime': False,
                       'last_tick': None}}
    PREFS = {'kid': {'hidden_comm': ['steam'], 'shown_comm': []}}

    def _seed_legacy(self):
        self.write_json(self.bedtime_json, self.BEDTIME)
        self.write_json(self.budgets_json, self.BUDGETS)
        self.write_json(self.prefs_json, self.PREFS)

    def test_migrate_from_real_json_fixtures(self):
        self._seed_legacy()
        self.assertEqual(store.load_table(self.db, 'bedtime'), self.BEDTIME)
        self.assertEqual(store.load_table(self.db, 'budgets'), self.BUDGETS)
        self.assertEqual(store.load_table(self.db, 'process_prefs'),
                         self.PREFS)

    def test_import_runs_exactly_once(self):
        self._seed_legacy()
        self.assertEqual(store.load_table(self.db, 'bedtime'), self.BEDTIME)
        # Later JSON edits are ignored: the db is the truth from then on.
        self.write_json(self.bedtime_json, {'intruder': {}})
        self.assertEqual(store.load_table(self.db, 'bedtime'), self.BEDTIME)

    def test_non_dict_json_imports_as_empty(self):
        self.write_json(self.bedtime_json, ['not', 'a', 'dict'])
        self.assertEqual(store.load_table(self.db, 'bedtime'), {})

    def test_corrupt_json_imports_as_empty(self):
        with open(self.budgets_json, 'w') as f:
            f.write('{not valid json')
        self.assertEqual(store.load_table(self.db, 'budgets'), {})


class TestMigrationBackup(StoreCase):
    def _make_v0_db(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute('CREATE TABLE schema_version '
                         '(version INTEGER NOT NULL)')
            conn.execute('INSERT INTO schema_version (version) VALUES (0)')
            conn.execute('CREATE TABLE legacy (id INTEGER)')
            conn.execute('INSERT INTO legacy VALUES (42)')
            conn.commit()
        finally:
            conn.close()

    def _backup_contents(self, backup):
        conn = sqlite3.connect(backup)
        try:
            self.assertEqual(
                conn.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            return (conn.execute(
                        'SELECT version FROM schema_version').fetchone()[0],
                    conn.execute('SELECT id FROM legacy').fetchall())
        finally:
            conn.close()

    def test_backup_before_migrate(self):
        self._make_v0_db()
        store.save_table(self.db, 'bedtime',
                         {'kid': {'start': '21:00'}}, self.bedtime_json)
        backup = self.db + '.bak.0'
        self.assertTrue(os.path.isfile(backup))
        # The backup is a restorable pre-migration snapshot: old version,
        # old data, structurally sound — not a raw file copy (which can
        # miss the WAL sidecar or catch a live file mid-write).
        version, rows = self._backup_contents(backup)
        self.assertEqual(version, 0)
        self.assertEqual(rows, [(42,)])
        self.assertEqual(version_of(self.db), store.SCHEMA_VERSION)
        # Pre-existing user tables survive the migration untouched.
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute('SELECT id FROM legacy').fetchall(),
                             [(42,)])
        finally:
            conn.close()

    def test_no_rebackup_on_clean_open(self):
        self._make_v0_db()
        store.load_table(self.db, 'bedtime')
        self.assertTrue(os.path.isfile(self.db + '.bak.0'))
        os.unlink(self.db + '.bak.0')
        store.load_table(self.db, 'bedtime')
        store.save_table(self.db, 'bedtime', {}, self.bedtime_json)
        self.assertFalse(os.path.isfile(self.db + '.bak.0'))
        self.assertFalse(os.path.isfile(self.db + '.bak.1'))


class TestFailClosed(StoreCase):
    def test_corrupt_db_loads_empty_never_crashes(self):
        with open(self.db, 'wb') as f:
            f.write(os.urandom(256))
        self.assertEqual(store.load_table(self.db, 'bedtime'), {})
        self.assertEqual(store.load_table(self.db, 'budgets'), {})
        self.assertEqual(store.load_table(self.db, 'process_prefs'), {})
        # Still corrupt on the next read: empty, never fabricated.
        self.assertEqual(store.load_table(self.db, 'bedtime'), {})

    def test_corrupt_db_save_raises_oserror(self):
        with open(self.db, 'wb') as f:
            f.write(os.urandom(256))
        with self.assertRaises(OSError):
            store.save_table(self.db, 'bedtime', {'kid': {}},
                             self.bedtime_json)

    def test_unparseable_row_is_skipped(self):
        store.save_table(self.db, 'bedtime', {'kid': {'start': '21:00'}},
                         self.bedtime_json)
        conn = sqlite3.connect(self.db)
        try:
            # v2 rows are (username, field, value); an unparseable field
            # value must be skipped without sinking the store.
            conn.execute("INSERT INTO bedtime (username, field, value) "
                         "VALUES ('garbage', 'start', '{nope')")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(store.load_table(self.db, 'bedtime'),
                         {'kid': {'start': '21:00'}})


class TestConcurrency(StoreCase):
    def test_concurrent_same_table_writes_stay_atomic(self):
        errors = []
        payloads = [{'user%d' % i: {'n': i}} for i in range(8)]

        def hammer(payload):
            try:
                for _ in range(25):
                    store.save_table(self.db, 'bedtime', payload,
                                     self.bedtime_json)
            except Exception as e:  # noqa: BLE001 — collected, asserted below
                errors.append(e)

        threads = [threading.Thread(target=hammer, args=(p,))
                   for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # Every write replaced the whole table atomically: the final
        # state is exactly one writer's payload, never a mixture.
        self.assertIn(store.load_table(self.db, 'bedtime'), payloads)

    def test_concurrent_cross_table_writes_all_persist(self):
        bedtime = {'kid': {'start': '21:00'}}
        budget_data = {'kid': {'limits': {'games': 30}}}
        prefs = {'kid': {'hidden_comm': [], 'shown_comm': ['x']}}
        errors = []

        def run(table, data, mirror):
            try:
                for _ in range(20):
                    store.save_table(self.db, table, data, mirror)
            except Exception as e:  # noqa: BLE001 — collected, asserted below
                errors.append(e)

        threads = [
            threading.Thread(target=run,
                             args=('bedtime', bedtime, self.bedtime_json)),
            threading.Thread(target=run,
                             args=('budgets', budget_data, self.budgets_json)),
            threading.Thread(target=run,
                             args=('process_prefs', prefs, self.prefs_json)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(store.load_table(self.db, 'bedtime'), bedtime)
        self.assertEqual(store.load_table(self.db, 'budgets'), budget_data)
        self.assertEqual(store.load_table(self.db, 'process_prefs'), prefs)

    def test_concurrent_reads_during_writes_always_valid(self):
        store.save_table(self.db, 'budgets', {'kid': {'limits': {}}},
                         self.budgets_json)
        errors = []
        stop = threading.Event()

        def read_loop():
            try:
                while not stop.is_set():
                    data = store.load_table(self.db, 'budgets')
                    assert isinstance(data, dict)
                    for doc in data.values():
                        assert isinstance(doc, dict)
            except Exception as e:  # noqa: BLE001 — collected, asserted below
                errors.append(e)

        readers = [threading.Thread(target=read_loop) for _ in range(4)]
        for t in readers:
            t.start()
        try:
            for i in range(30):
                store.save_table(self.db, 'budgets',
                                 {'kid': {'limits': {'games': i}}},
                                 self.budgets_json)
        finally:
            stop.set()
        for t in readers:
            t.join()
        self.assertEqual(errors, [])

    def test_connect_survives_contented_wal_change(self):
        # The journal-mode pragma ignores the busy timeout and fails
        # immediately under a rival write lock when the mode still needs
        # changing; the open must survive that (steady state retries the
        # upgrade on the next open).
        raw = sqlite3.connect(self.db)
        try:
            raw.execute('CREATE TABLE bedtime '
                        '(username TEXT PRIMARY KEY, doc TEXT NOT NULL)')
            raw.execute("INSERT INTO bedtime VALUES ('kid', '{\"a\": 1}')")
            raw.commit()
        finally:
            raw.close()
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone(),
                             ('delete',))
        finally:
            conn.close()
        holder = sqlite3.connect(self.db, isolation_level=None)
        try:
            holder.execute('BEGIN IMMEDIATE')
            holder.execute("UPDATE bedtime SET doc = '{\"a\": 2}' "
                           "WHERE username = 'kid'")
            conn = store._connect(self.db)  # must not raise
            try:
                # Mode change lost the race: still delete, open survived.
                self.assertEqual(
                    conn.execute('PRAGMA journal_mode').fetchone(),
                    ('delete',))
            finally:
                conn.close()
        finally:
            holder.execute('ROLLBACK')
            holder.close()

    def test_busy_timeout_waits_instead_of_failing(self):
        store.save_table(self.db, 'bedtime', {'kid': {'start': '21:00'}},
                         self.bedtime_json)
        holder = sqlite3.connect(self.db, isolation_level=None)
        try:
            holder.execute('BEGIN IMMEDIATE')
            # v2 layout: the field name is split out of the doc column.
            holder.execute("UPDATE bedtime SET value = '{}' "
                           "WHERE username = 'kid' AND field = 'start'")
            started = time.monotonic()

            def locked_write():
                store.save_table(self.db, 'bedtime',
                                 {'kid': {'start': '22:00'}},
                                 self.bedtime_json)

            worker = threading.Thread(target=locked_write)
            worker.start()
            time.sleep(0.5)
            holder.execute('COMMIT')
            worker.join(timeout=10)
            elapsed = time.monotonic() - started
        finally:
            holder.close()
        self.assertFalse(worker.is_alive())
        # The writer waited out the lock (did not fail fast) and won.
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertEqual(store.load_table(self.db, 'bedtime'),
                         {'kid': {'start': '22:00'}})


class TestRollback(StoreCase):
    def test_delete_db_reimports_from_mirror(self):
        data = {'kid': {'start': '21:00', 'end': '07:00', 'days': [1]}}
        store.save_table(self.db, 'bedtime', data, self.bedtime_json)
        with open(self.bedtime_json) as f:
            self.assertEqual(json.load(f), data)
        os.unlink(self.db)
        self.assertEqual(store.load_table(self.db, 'bedtime'), data)

    def test_mirror_matches_every_save(self):
        for data in ({}, {'a': {'x': 1}}, {'a': {'x': 2}, 'b': {}},
                     {'b': {}}):
            store.save_table(self.db, 'budgets', data, self.budgets_json)
            with open(self.budgets_json) as f:
                self.assertEqual(json.load(f), data)
            self.assertEqual(store.load_table(self.db, 'budgets'), data)


class TestPublicApi(unittest.TestCase):
    """Same signatures, same shapes: the app/budgets loaders backed by
    SQLite (isolated via the *_FILE constants, as the existing suite
    already does)."""

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

    def test_bedtime_api_roundtrip(self):
        self.assertEqual(app.load_bedtime(), {})
        data = {'kid': {'start': '21:00', 'locked_by_bedtime': True,
                        'bonus_history': [{'kind': 'grant'}]}}
        app.save_bedtime(data)
        self.assertEqual(app.load_bedtime(), data)

    def test_budgets_api_roundtrip(self):
        self.assertEqual(budgets.load_budgets(), {})
        entry = budgets.blank_entry()
        entry['limits'] = {'games': 60}
        budgets.save_budgets({'kid': entry})
        self.assertEqual(budgets.load_budgets(), {'kid': entry})

    def test_prefs_api_roundtrip(self):
        self.assertEqual(budgets.load_prefs(), {})
        prefs = {'kid': {'hidden_comm': ['steam'], 'shown_comm': []}}
        budgets.save_prefs(prefs)
        self.assertEqual(budgets.load_prefs(), prefs)

    def test_save_failure_surfaces_as_oserror(self):
        # A corrupt store must surface exactly like the old unwritable
        # JSON file did, so every existing `except OSError` still holds.
        db = store.db_path_for(app.BEDTIME_FILE)
        store.save_table(db, 'bedtime', {'kid': {}}, None)
        with open(db, 'wb') as f:
            f.write(os.urandom(128))
        with self.assertRaises(OSError):
            app.save_bedtime({'kid': {}})
        self.assertEqual(app.load_bedtime(), {})


if __name__ == '__main__':
    unittest.main()
