#!/usr/bin/env python3
"""Tamper-evident audit log for Parental Controls (stdlib only).

Append-only JSONL file (one JSON object per line) at ``audit.jsonl`` next
to this module. Each entry chains to the previous one:

    {seq, at, actor, action, target, detail, prev_hash, hash}

``hash`` is sha256 over the canonical JSON serialization of the entry
*including* ``prev_hash`` (genesis ``prev_hash`` is ``"0" * 64``).
``verify_chain()`` recomputes every hash and linkage, so mutating,
deleting, or reordering any line is detected.

Tamper-*evidence*, not encryption: the file is plaintext by design.

This module is deliberately stdlib-only and framework-free (no Flask
import): callers pass ``actor`` explicitly. The logging path must never
break a privileged operation -- callers use :func:`safe_log`, which
swallows all errors, mirroring ``app._safe_notify``.

A SEPARATE append-only file: this log never touches budgets.json,
bedtime.json, process_prefs.json, or auth.json schemas.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_FILE = os.path.join(BASE_DIR, 'audit.jsonl')

GENESIS_PREV = '0' * 64

# Env override (tests point this at a temp file).
AUDIT_ENV_VAR = 'PARENTAL_CONTROLS_AUDIT'


def audit_file_path():
    """Effective audit log path (env override wins, for tests)."""
    return os.environ.get(AUDIT_ENV_VAR, AUDIT_FILE)


def set_audit_file(path):
    """Override the module-default audit path (tests)."""
    global AUDIT_FILE
    AUDIT_FILE = path


def _canonical(payload):
    """Canonical bytes hashed for an entry (sorted keys, compact)."""
    return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')


def _entry_hash(payload):
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _iter_lines(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    yield line
    except FileNotFoundError:
        return
    except OSError:
        return


def log(actor, action, target='', detail=None, path=None):
    """Append one audit entry. Returns the entry dict.

    May raise (OSError, TypeError for unserializable detail); callers
    that must not fail use :func:`safe_log` instead.
    """
    path = path or audit_file_path()
    actor = str(actor or 'system')
    action = str(action or '')
    target = str(target or '')
    # Fail fast on unserializable detail before touching the chain.
    json.dumps(detail)

    seq = 0
    prev_hash = GENESIS_PREV
    for line in _iter_lines(path):
        try:
            prior = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(prior, dict) and isinstance(prior.get('seq'), int):
            seq = prior['seq']
            if isinstance(prior.get('hash'), str):
                prev_hash = prior['hash']
    entry = {
        'seq': seq + 1,
        'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'actor': actor,
        'action': action,
        'target': target,
        'detail': detail,
        'prev_hash': prev_hash,
    }
    entry['hash'] = _entry_hash(entry)
    existed = os.path.exists(path)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    if not existed:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return entry


def safe_log(actor, action, target='', detail=None, path=None):
    """log() that can never raise (returns None on failure)."""
    try:
        return log(actor, action, target, detail, path)
    except Exception as e:
        print(f'audit (suppressed): {e}')
        return None


def read_entries(limit=100, path=None):
    """Read audit entries, newest-first, capped at `limit`.

    Unparseable lines are skipped (verify_chain reports the break).
    """
    path = path or audit_file_path()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))
    entries = []
    for line in _iter_lines(path):
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get('seq'), int):
            entries.append(obj)
    entries.sort(key=lambda e: e['seq'], reverse=True)
    return entries[:limit]


def verify_chain(path=None):
    """Verify the hash chain. Returns (ok, bad_seq).

    ok=True, bad_seq=None when the chain is intact (including an empty
    log). Otherwise bad_seq is the seq of the first broken entry, or
    None when the break cannot be attributed to a seq (e.g. an
    unparseable line).
    """
    path = path or audit_file_path()
    expected_seq = 1
    prev_hash = GENESIS_PREV
    for line in _iter_lines(path):
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            return False, None
        if not isinstance(entry, dict):
            return False, None
        seq = entry.get('seq')
        if seq != expected_seq:
            return False, seq if isinstance(seq, int) else expected_seq
        if entry.get('prev_hash') != prev_hash:
            return False, seq
        payload = {k: entry.get(k) for k in
                   ('seq', 'at', 'actor', 'action', 'target', 'detail',
                    'prev_hash')}
        if entry.get('hash') != _entry_hash(payload):
            return False, seq
        prev_hash = entry.get('hash')
        expected_seq += 1
    return True, None
