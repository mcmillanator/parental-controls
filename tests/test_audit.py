#!/usr/bin/env python3
"""Tests for the tamper-evident audit log (audit.py + hooks + read API).

Conventions mirror tests/test_lock_regression.py: stdlib only, all
subprocess work mocked -- these tests never lock, unlock, terminate,
freeze, or notify any real account.

The audit log is a SEPARATE append-only file (audit.jsonl); these tests
never touch budgets.json/bedtime.json/process_prefs.json/auth.json
schemas beyond the normal helper side effects on their temp files.
"""

import json
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
import audit
import auth
import budgets
import cli
from test_lock_regression import (_AdminSessionMixin, _BedtimeFileMixin,
                                  KID, WINDOW)

Result = namedtuple('Result', ['returncode', 'stdout', 'stderr'])


def _result(rc=0, stdout='', stderr=''):
    return Result(rc, stdout, stderr)


BONUS = {'start': '03:00', 'end': '04:00', 'days': [0, 1, 2, 3, 4, 5, 6]}


class _AuditFileMixin:
    """Point the audit log + budgets store at temp files for each test."""

    def setUp(self):
        super().setUp()
        self._audit_tmp = tempfile.TemporaryDirectory()
        self._audit_path = os.path.join(self._audit_tmp.name, 'audit.jsonl')
        os.environ['PARENTAL_CONTROLS_AUDIT'] = self._audit_path
        self.addCleanup(self._drop_audit_env)
        # Budget hooks write budgets.json: keep that in temp space too.
        self._budgets_path = os.path.join(self._audit_tmp.name,
                                          'budgets.json')
        patcher = mock.patch.object(budgets, 'BUDGETS_FILE',
                                    self._budgets_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _drop_audit_env(self):
        os.environ.pop('PARENTAL_CONTROLS_AUDIT', None)
        self._audit_tmp.cleanup()

    def audit_entries(self):
        """All audit entries, newest-first."""
        return audit.read_entries(limit=1000)

    def audit_triples(self):
        return [(e['actor'], e['action'], e['target'])
                for e in self.audit_entries()]

    def assert_logged(self, actor, action, target):
        self.assertIn((actor, action, target), self.audit_triples(),
                      f'missing audit entry {(actor, action, target)}; '
                      f'got {self.audit_triples()}')


# ---------------------------------------------------------------------------
# Chain: genesis / append / verify
# ---------------------------------------------------------------------------

class TestAuditChain(_AuditFileMixin, unittest.TestCase):
    def test_genesis_entry(self):
        entry = audit.log('qaadmin', 'account.lock', KID, {'success': True})
        self.assertEqual(entry['seq'], 1)
        self.assertEqual(entry['prev_hash'], '0' * 64)
        self.assertEqual(len(entry['hash']), 64)
        self.assertTrue(entry['at'])
        ok, bad = audit.verify_chain()
        self.assertTrue(ok)
        self.assertIsNone(bad)

    def test_append_chains_seq_and_hash(self):
        first = audit.log('a', 'account.lock', KID)
        second = audit.log('a', 'account.unlock', KID)
        third = audit.log('b', 'bedtime.set', KID, {'start': '21:00'})
        self.assertEqual([e['seq'] for e in (first, second, third)],
                         [1, 2, 3])
        self.assertEqual(second['prev_hash'], first['hash'])
        self.assertEqual(third['prev_hash'], second['hash'])
        ok, bad = audit.verify_chain()
        self.assertTrue(ok)
        self.assertIsNone(bad)

    def test_hash_is_stable_recomputation(self):
        entry = audit.log('a', 'bonus.grant', KID, {'minutes': 30})
        payload = {k: entry[k] for k in ('seq', 'at', 'actor', 'action',
                                        'target', 'detail', 'prev_hash')}
        self.assertEqual(entry['hash'], audit._entry_hash(payload))

    def test_empty_log_verifies(self):
        ok, bad = audit.verify_chain()
        self.assertTrue(ok)
        self.assertIsNone(bad)
        self.assertEqual(audit.read_entries(), [])

    def test_env_override_selects_path(self):
        other = os.path.join(self._audit_tmp.name, 'other.jsonl')
        os.environ['PARENTAL_CONTROLS_AUDIT'] = other
        audit.log('a', 'account.lock', KID)
        self.assertTrue(os.path.exists(other))
        self.assertFalse(os.path.exists(self._audit_path))


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------

class TestAuditTamper(_AuditFileMixin, unittest.TestCase):
    def _seed(self, n=3):
        for i in range(n):
            audit.log('qaadmin', f'action.{i}', KID, {'i': i})

    def _rewrite(self, lines):
        with open(self._audit_path, 'w') as f:
            f.write(''.join(lines))

    def _lines(self):
        with open(self._audit_path) as f:
            return f.readlines()

    def test_mutated_detail_detected_at_that_seq(self):
        self._seed()
        lines = self._lines()
        obj = json.loads(lines[1])
        obj['detail'] = {'i': 999}
        lines[1] = json.dumps(obj) + '\n'
        self._rewrite(lines)
        ok, bad = audit.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(bad, 2)

    def test_mutated_actor_detected(self):
        self._seed()
        lines = self._lines()
        obj = json.loads(lines[0])
        obj['actor'] = 'mallory'
        lines[0] = json.dumps(obj) + '\n'
        self._rewrite(lines)
        ok, bad = audit.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(bad, 1)

    def test_deleted_line_breaks_seq_chain(self):
        self._seed()
        lines = self._lines()
        del lines[1]
        self._rewrite(lines)
        ok, bad = audit.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(bad, 3)

    def test_reordered_lines_detected(self):
        self._seed()
        lines = self._lines()
        self._rewrite([lines[1], lines[0], lines[2]])
        ok, bad = audit.verify_chain()
        self.assertFalse(ok)

    def test_garbage_line_detected(self):
        self._seed()
        lines = self._lines()
        lines[1] = 'not json at all\n'
        self._rewrite(lines)
        ok, bad = audit.verify_chain()
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# logging-never-breaks
# ---------------------------------------------------------------------------

class TestAuditNeverBreaks(_AuditFileMixin, unittest.TestCase):
    def test_safe_log_never_raises(self):
        # Unserializable detail would raise from log(); safe_log swallows.
        self.assertIsNone(audit.safe_log('a', 'x', KID, detail=object()))
        # Missing directory likewise.
        self.assertIsNone(audit.safe_log(
            'a', 'x', KID,
            path=os.path.join(self._audit_tmp.name, 'nope', 'a.jsonl')))

    def test_broken_audit_does_not_break_unlock(self):
        with mock.patch.object(audit, 'log',
                               side_effect=OSError('disk full')), \
             mock.patch.object(app.subprocess, 'run',
                               return_value=_result(0, stdout='')):
            success, message = app.run_lock_command(KID, 'unlock')
        self.assertTrue(success)
        self.assertEqual(message, 'OK')


# ---------------------------------------------------------------------------
# Hook coverage per endpoint (Flask client + mocked subprocess)
# ---------------------------------------------------------------------------

class TestAuditHooks(_AdminSessionMixin, _AuditFileMixin, _BedtimeFileMixin,
                     unittest.TestCase):
    def _ok_run(self):
        """subprocess.run fake: every privileged call succeeds, no session."""
        return mock.patch.object(app.subprocess, 'run',
                                 return_value=_result(0, stdout=''))

    def test_run_lock_command_logs_system_actor_outside_request(self):
        with self._ok_run():
            success, _msg = app.run_lock_command(KID, 'lock')
        self.assertTrue(success)
        self.assert_logged('system', 'account.lock', KID)

    def test_api_lock_logs_lock_and_manual_lock(self):
        self.write_bedtime({})
        with self._ok_run():
            resp = self.authed_post('/api/lock', data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'account.lock', KID)
        self.assert_logged(self.ADMIN_USER, 'bedtime.manual_lock', KID)

    def test_api_lock_screen_logs(self):
        self.write_bedtime({})
        with self._ok_run():
            resp = self.authed_post('/api/lock-screen',
                                    data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'account.lock', KID)

    def test_api_unlock_logs_unlock_and_override(self):
        self.write_bedtime({KID: dict(WINDOW)})
        with self._ok_run():
            resp = self.authed_post('/api/unlock', data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'account.unlock', KID)
        self.assert_logged(self.ADMIN_USER, 'bedtime.manual_unlock', KID)

    def test_api_logout_logs(self):
        self.write_bedtime({})
        with self._ok_run():
            resp = self.authed_post('/api/logout', data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'account.logout', KID)

    def test_bedtime_set_and_clear_logged(self):
        self.write_bedtime({})
        with mock.patch.object(app, 'is_in_bedtime', return_value=False), \
             mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')):
            resp = self.authed_post('/api/bedtime',
                                    data={'username': KID, 'start': '21:00',
                                          'end': '07:00', 'all_days': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'bedtime.set', KID)
        entry = [e for e in self.audit_entries()
                 if e['action'] == 'bedtime.set'][0]
        self.assertEqual(entry['detail']['start'], '21:00')

        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')):
            resp = self.authed_post('/api/bedtime',
                                    data={'username': KID, 'clear': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'bedtime.clear', KID)

    def test_bonus_grant_and_revoke_logged(self):
        self.write_bedtime({KID: dict(BONUS)})
        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')), \
             mock.patch.object(app, '_safe_notify'):
            resp = self.authed_post('/api/bonus',
                                    data={'username': KID, 'minutes': '30'})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'bonus.grant', KID)

        with mock.patch.object(app, 'run_lock_command',
                               return_value=(True, 'OK')), \
             mock.patch.object(app, 'get_lock_status',
                               return_value=(True, 'Locked')), \
             mock.patch.object(app, '_safe_notify'):
            resp = self.authed_post('/api/bonus/revoke',
                                    data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'bonus.revoke', KID)

    def test_budget_set_limit_and_resume_logged(self):
        resp = self.authed_post('/api/budgets',
                                data={'username': KID, 'category': 'games',
                                      'minutes': '30'})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'budget.set_limit',
                           f'{KID}:games')

        resp = self.authed_post('/api/budgets/resume',
                                data={'username': KID, 'category': 'games'})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'budget.resume',
                           f'{KID}:games')

    def test_admin_add_and_remove_logged(self):
        resp = self.authed_post('/api/admins',
                                data={'username': 'newadmin'})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'admin.add', 'newadmin')

        resp = self.authed_post('/api/admins/remove',
                                data={'username': 'newadmin'})
        self.assertEqual(resp.status_code, 200)
        self.assert_logged(self.ADMIN_USER, 'admin.remove', 'newadmin')

    def test_broken_audit_does_not_break_endpoint(self):
        self.write_bedtime({KID: dict(WINDOW)})
        with mock.patch.object(audit, 'log',
                               side_effect=OSError('disk full')), \
             self._ok_run():
            resp = self.authed_post('/api/unlock', data={'username': KID})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])


# ---------------------------------------------------------------------------
# Read API: auth / pagination / cap / chain status
# ---------------------------------------------------------------------------

class TestAuditReadAPI(_AdminSessionMixin, _AuditFileMixin,
                       _BedtimeFileMixin, unittest.TestCase):
    def test_requires_auth(self):
        anon = app.app.test_client()
        resp = anon.get('/api/audit')
        self.assertEqual(resp.status_code, 401)

    def test_entries_newest_first_with_chain_status(self):
        audit.log('qaadmin', 'account.lock', KID)
        audit.log('qaadmin', 'account.unlock', KID)
        audit.log('qaadmin', 'bedtime.set', KID)
        resp = self.client.get('/api/audit')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['chain_ok'])
        self.assertIsNone(payload['bad_seq'])
        seqs = [e['seq'] for e in payload['entries']]
        self.assertEqual(seqs, [3, 2, 1])

    def test_limit_param(self):
        for _ in range(5):
            audit.log('qaadmin', 'account.lock', KID)
        resp = self.client.get('/api/audit?limit=2')
        payload = resp.get_json()
        self.assertEqual(len(payload['entries']), 2)
        self.assertEqual(payload['limit'], 2)
        self.assertEqual([e['seq'] for e in payload['entries']], [5, 4])

    def test_limit_capped_at_1000(self):
        audit.log('qaadmin', 'account.lock', KID)
        resp = self.client.get('/api/audit?limit=5000')
        payload = resp.get_json()
        self.assertEqual(payload['limit'], 1000)
        self.assertEqual(len(payload['entries']), 1)

    def test_bad_limit_falls_back_to_default(self):
        audit.log('qaadmin', 'account.lock', KID)
        resp = self.client.get('/api/audit?limit=bogus')
        self.assertEqual(resp.get_json()['limit'], 100)

    def test_tamper_surfaced_via_api(self):
        audit.log('qaadmin', 'account.lock', KID)
        audit.log('qaadmin', 'account.unlock', KID)
        with open(self._audit_path) as f:
            lines = f.readlines()
        obj = json.loads(lines[0])
        obj['target'] = 'mallory'
        lines[0] = json.dumps(obj) + '\n'
        with open(self._audit_path, 'w') as f:
            f.write(''.join(lines))
        resp = self.client.get('/api/audit')
        payload = resp.get_json()
        self.assertFalse(payload['chain_ok'])
        self.assertEqual(payload['bad_seq'], 1)


# ---------------------------------------------------------------------------
# CLI hooks (mocked subprocess; temp auth store from the admin harness)
# ---------------------------------------------------------------------------

class TestAuditCLI(_AdminSessionMixin, _AuditFileMixin, unittest.TestCase):
    def test_cli_lock_admin_logged(self):
        auth.ensure_hash_backend()
        store = auth.load_store()
        store['users']['lockme'] = {'hash': 'x', 'locked': False,
                                   'must_change': False}
        auth.save_store(store)
        cli.cmd_lock_admin(type('A', (), {'admin': 'lockme'})())
        self.assert_logged('cli:lock-admin', 'admin.lock', 'lockme')

    def test_cli_add_and_remove_admin_logged(self):
        auth.ensure_hash_backend()
        cli.cmd_add_admin(type('A', (), {'username': 'clinew'})())
        self.assert_logged('cli:add-admin', 'admin.add', 'clinew')
        cli.cmd_remove_admin(type('A', (), {'username': 'clinew'})())
        self.assert_logged('cli:remove-admin', 'admin.remove', 'clinew')

    def test_cli_unlock_logged(self):
        with mock.patch.object(cli.subprocess, 'run',
                               return_value=_result(0)):
            cli.cmd_unlock(type('A', (), {'username': KID})())
        self.assert_logged('cli:unlock', 'account.unlock', KID)


if __name__ == '__main__':
    unittest.main()
