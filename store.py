#!/usr/bin/env python3
"""SQLite-backed runtime state for parental-controls.

Replaces the per-file JSON stores (``bedtime.json``, ``budgets.json``,
``process_prefs.json``) with a single SQLite database while keeping the
exact same ``load_*``/``save_*`` behaviour for callers:

* one table per store, keyed by username, holding the same JSON document
  per user (TEXT column) — the granularity migrated, not the shape;
* ``schema_version`` table + ordered ``MIGRATIONS`` applied on open;
* WAL journal mode + busy timeout for the three writers (web app,
  60s enforcement thread, systemd timer);
* backup-before-migrate (``state.db.bak.<version>``);
* fail-closed corruption recovery: an unreadable/corrupt store yields a
  safe empty state, never a crash, never fabricated entries;
* one-time legacy import: a freshly created database absorbs the sibling
  JSON files when they exist; afterwards the database is the single
  source of truth and the JSON files are only a best-effort rollback
  mirror refreshed on every save (never read back).

``auth.json``/``auth.py`` is deliberately OUT of scope: it is a
security-critical fail-closed credential store with its own threat
model, and mixing credentials into the runtime database would widen
the blast radius of any state-store bug.

stdlib only (``sqlite3``) so ``budgets.py`` stays importable with
system python3. Every operation opens its own short-lived connection
(never shared across threads); writers take ``BEGIN IMMEDIATE`` so
contention waits on the busy timeout instead of failing fast.
"""

import json
import os
import sqlite3
import tempfile

SCHEMA_VERSION = 1
DB_FILENAME = 'state.db'
BUSY_TIMEOUT_MS = 5000

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TABLES = ('bedtime', 'budgets', 'process_prefs')

LEGACY_FILES = {
    'bedtime': 'bedtime.json',
    'budgets': 'budgets.json',
    'process_prefs': 'process_prefs.json',
}


def db_path_for(json_path):
    """Database path for the state home containing ``json_path``.

    The database lives next to the legacy JSON file: in production all
    three ``*_FILE`` constants sit in ``BASE_DIR``, so they share one
    ``state.db``. Tests that redirect a ``*_FILE`` constant into a temp
    dir transparently get an isolated database there.
    """
    home = os.path.dirname(os.path.abspath(json_path))
    return os.path.join(home, DB_FILENAME)


def default_db_path():
    """Production database path (no checkout/home hardcoding: derived
    from this file's location). In production this equals
    ``db_path_for()`` of any ``*_FILE`` constant; it exists so ops and
    tests have one canonical accessor instead of inferring it."""
    return os.path.join(BASE_DIR, DB_FILENAME)


def _legacy_map_for(db_path):
    home = os.path.dirname(os.path.abspath(db_path))
    return {table: os.path.join(home, name)
            for table, name in LEGACY_FILES.items()}


def _connect(db_path):
    """Open a connection with a busy timeout (autocommit; callers manage
    transactions explicitly). WAL mode is requested best-effort: unlike
    normal statements the journal-mode pragma ignores the busy timeout
    and fails immediately under a rival write lock (verified), so a
    failure here must never fail the open — steady state retries on the
    next open, and the database works fine in rollback mode meanwhile.
    Raises sqlite3.DatabaseError on a corrupt/unreadable file, OSError
    on filesystem failures."""
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000.0,
                           isolation_level=None)
    try:
        conn.execute('PRAGMA busy_timeout=%d' % BUSY_TIMEOUT_MS)
    except sqlite3.DatabaseError:
        conn.close()
        raise
    try:
        conn.execute('PRAGMA journal_mode=WAL')
    except sqlite3.DatabaseError:
        pass
    return conn


def _get_version(conn):
    """Current schema version, or 0 when unknown (fresh/foreign/empty)."""
    try:
        row = conn.execute(
            'SELECT version FROM schema_version LIMIT 1').fetchone()
    except sqlite3.DatabaseError:
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _set_version(conn, version):
    conn.execute('CREATE TABLE IF NOT EXISTS schema_version '
                 '(version INTEGER NOT NULL)')
    conn.execute('DELETE FROM schema_version')
    conn.execute('INSERT INTO schema_version (version) VALUES (?)',
                 (version,))


def _migrate_1(conn):
    """v1: initial schema — one JSON-document table per store."""
    for table in TABLES:
        conn.execute('CREATE TABLE IF NOT EXISTS %s '
                     '(username TEXT PRIMARY KEY, doc TEXT NOT NULL)' % table)


MIGRATIONS = [
    (1, 'initial schema: schema_version + one JSON-document '
        'table per store (bedtime, budgets, process_prefs)',
     _migrate_1),
]


def _read_legacy_json(json_path):
    """Legacy JSON payload, or None when absent/corrupt/non-dict.

    Mirrors the old loaders' fail-closed rule: anything that is not a
    readable dict is treated as empty, never fabricated.
    """
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _backup_online(db_path, version):
    """Consistent pre-migration backup via SQLite's online backup API.

    A raw file copy is unsafe here: it misses the WAL sidecar (stale
    snapshot) and can catch a live file mid-write. The backup API reads
    a consistent committed snapshot through the WAL. Callers must hold
    a write transaction (BEGIN IMMEDIATE) so racing migrators serialize
    and only the winner backs up.
    """
    backup = '%s.bak.%s' % (db_path, version)
    src = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000.0)
    try:
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return backup


def open_db(db_path):
    """Connect, migrate to SCHEMA_VERSION, one-time-import legacy JSON.

    Backup-before-migrate: when the existing database predates the
    schema, it is copied to ``state.db.bak.<version>`` first. When the
    database file is brand new, any sibling legacy JSON files are
    imported once (later JSON edits are ignored — the database is the
    truth from then on).

    Returns an open connection owned by the caller (close it). Raises
    sqlite3.DatabaseError on corruption, OSError on filesystem failure.
    """
    existed = os.path.exists(db_path)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            version = _get_version(conn)
            if version < SCHEMA_VERSION and existed:
                print('store: backing up %s -> %s.bak.%s before migration'
                      % (db_path, db_path, version), flush=True)
                _backup_online(db_path, version)
            for target, _desc, fn in sorted(MIGRATIONS):
                if target > version:
                    fn(conn)
                    _set_version(conn, target)
                    version = target
            if not existed:
                for table, json_path in _legacy_map_for(db_path).items():
                    data = _read_legacy_json(json_path)
                    if data:
                        conn.executemany(
                            'INSERT OR IGNORE INTO %s (username, doc) '
                            'VALUES (?, ?)' % table,
                            [(u, json.dumps(doc))
                             for u, doc in data.items()])
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.DatabaseError:
                pass
            raise
    except Exception:
        conn.close()
        raise
    return conn


def load_table(db_path, table):
    """Load a whole store table; fail closed ({}) on any store problem.

    Same contract as the old JSON loaders: missing/corrupt/unreadable
    store -> empty dict, never a crash. Per-row garbage is skipped, so
    one bad document cannot sink the whole store.
    """
    if table not in TABLES:
        raise ValueError('Unknown state table: %r' % (table,))
    if (not os.path.exists(db_path)
            and _read_legacy_json(_legacy_map_for(db_path)[table]) is None):
        # Nothing to materialize: like the old readers, a pure read never
        # creates state (no empty database file, no import attempt).
        return {}
    try:
        conn = open_db(db_path)
    except (sqlite3.DatabaseError, OSError):
        return {}
    try:
        try:
            rows = conn.execute(
                'SELECT username, doc FROM %s' % table).fetchall()
        except sqlite3.DatabaseError:
            return {}
        data = {}
        for username, doc in rows:
            try:
                data[username] = json.loads(doc)
            except (ValueError, TypeError):
                continue
        return data
    finally:
        conn.close()


def _write_mirror(json_path, data):
    """Best-effort atomic rollback mirror (tmp file + rename).

    The mirror is what a rollback re-imports (delete ``state.db`` and
    the next open absorbs it) and what a downgraded install.sh keeps
    working, since the installer only preserves the JSON names. A
    failed mirror must never fail the save itself: the database commit
    above is the truth, so log and move on. The temp file is unique per
    call (mkstemp) so concurrent savers in one process cannot steal
    each other's mirror; os.replace keeps every published mirror whole.
    """
    try:
        home = os.path.dirname(os.path.abspath(json_path))
        fd, tmp = tempfile.mkstemp(
            dir=home, prefix=os.path.basename(json_path) + '.tmp.')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, json_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        print('store: mirror write failed for %s: %s'
              % (json_path, e), flush=True)


def save_table(db_path, table, data, mirror_json_path=None):
    """Atomically replace a whole store table (same granularity as the
    old whole-file JSON rewrite: deletions propagate, last writer wins
    across writers, but readers never see a torn store).

    Raises OSError when the store cannot be persisted (callers already
    handle that exactly like the old JSON write failures). A corrupt
    store is NOT silently replaced: fail closed and let the rollback
    story (delete db -> re-import from mirror) handle recovery.
    """
    if table not in TABLES:
        raise ValueError('Unknown state table: %r' % (table,))
    try:
        conn = open_db(db_path)
    except sqlite3.DatabaseError as e:
        raise OSError('state store unreadable: %s: %s'
                      % (db_path, e)) from e
    try:
        payload = [(username, json.dumps(doc))
                   for username, doc in data.items()]
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('DELETE FROM %s' % table)
            conn.executemany('INSERT INTO %s (username, doc) '
                             'VALUES (?, ?)' % table, payload)
            conn.execute('COMMIT')
        except sqlite3.DatabaseError as e:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.DatabaseError:
                pass
            raise OSError('state store write failed: %s: %s'
                          % (db_path, e)) from e
    finally:
        conn.close()
    if mirror_json_path is not None:
        _write_mirror(mirror_json_path, data)
