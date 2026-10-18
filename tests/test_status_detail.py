#!/usr/bin/env python3
"""Parent-safe status details: the raw-probe-text leak, pinned shut.

Owner bug report (2026-10-18, live on aiden-pc and the VM): every child
card on the Overview showed a warning box full of raw system text —

    sudo: pam_open_session: Permission denied
    sudo: policy plugin failed session initialization

— because ``get_lock_status`` captures ``sudo passwd -S``'s stderr verbatim
whenever the probe exits non-zero, and that string was copied straight into
every payload's ``status_detail``. "A parent will have no idea what that
means or what to do about it. If system administration is required then
that level of details belongs in the logs for an admin to access."

Root cause (manager, from the VM journal): the enforce timer's 60s
``sudo passwd -S <user>`` hit pam_systemd's login policy
(``io.systemd.Login.CreateSession failed: ... TooManySessions``) on a box
with many active sessions. The status read itself is fine; the *session*
PAM layer is what failed, and its complaints were being rendered as if a
parent could act on them.

Three properties are pinned here:

1. **CLASSIFICATION.** One pure table in app.py (mirrored in agent.py)
   splits a probe detail into OK / UNAVAILABLE / VERBATIM. Known-benign
   session noise is dropped so the parent keeps the normal pill; genuine
   failures become friendly copy; already-human product copy passes
   through untouched.
2. **NO LEAK, ANYWHERE.** The raw text is cut at the payload boundary —
   every producer writes ``status_display()``'s return value, never the raw
   detail — so ``/``, ``/api/users``, ``/user/<name>`` and the agent's
   ``/users`` payload are all parent-safe by construction, including when a
   REMOTE box runs an older agent.
3. **ADMIN LOG.** The raw detail is not simply discarded: UNAVAILABLE and
   OK-with-noise outcomes go to the admin-only audit chain as
   ``account.status_probe``, deduped to one line per (username, class,
   raw) CHANGE — the Overview polls every 10s, so an identical persistent
   failure must not append a line forever. Same documented pattern as
   budgets._LAST_AUTO_PAUSE_SIGS / budgets._LAST_SUMMARY_SIGS.

Every system read is mocked (subprocess.run included); nothing here touches
a real account, the service, ``/etc``, ``/opt``, or the checkout. Flask test
client + raw WSGI calls only, stdlib unittest.
"""

import ast
import inspect
import json
import os
import re
import sys
import textwrap
import unittest
from contextlib import ExitStack
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import agent
import app
import auth
from test_lock_regression import _AdminSessionMixin
from test_multidevice_agent import BaseAgentTest, auth_header, call

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KID = 'kidstatus'

# The EXACT two lines from the owner's bug report, verbatim.
OWNER_REPORT = (
    'sudo: pam_open_session: Permission denied\n'
    'sudo: policy plugin failed session initialization'
)

# The full stderr the VM actually produced: the manager's root-cause line
# (pam_systemd -> systemd-logind over Varlink) plus the owner's two lines.
VM_STDERR = (
    'sudo: pam_systemd(sudo:session): Failed to create session: '
    'io.systemd.Login.CreateSession failed: io.systemd.Login.TooManySessions\n'
    + OWNER_REPORT
)

# Every nasty detail seeded into the render tests below.
NASTY_DETAILS = (
    OWNER_REPORT,
    VM_STDERR,
    'passwd command not found',
    "[Errno 13] Permission denied: '/usr/bin/passwd'",
    'sudo: a password is required',
    'sudo: tester is not in the sudoers file',
    'Command timed out',
    'Unexpected passwd output',
    'Unknown status: LK',
)

# Nothing a parent may ever be shown. Matched case-insensitively against
# every rendered page and every JSON payload.
FORBIDDEN_IN_PARENT_VIEW = ('sudo', 'pam_', 'policy plugin', 'cgroup',
                            '/sys/', 'varlink', 'passwd', 'usermod',
                            'errno', 'traceback')

FRIENDLY = app.STATUS_UNAVAILABLE_COPY


def _one_user():
    return [{'username': KID, 'uid': 1001, 'home': '/home/kidstatus',
             'shell': '/bin/bash'}]


def _assert_parent_safe(self, haystack, label):
    """No raw system text anywhere in a parent-facing artifact."""
    low = haystack.lower()
    for needle in FORBIDDEN_IN_PARENT_VIEW:
        with self.subTest(where=label, needle=needle):
            self.assertNotIn(needle.lower(), low,
                             '%s leaked %r to the parent view' % (label,
                                                                  needle))


# ---------------------------------------------------------------------------
# 1. The classification table
# ---------------------------------------------------------------------------

class StatusClassificationTest(unittest.TestCase):
    """The pure table: pattern -> class -> parent-facing copy."""

    def test_benign_session_noise_alone_is_unavailable_not_leaked(self):
        # Nothing usable behind the noise: the probe told the parent
        # nothing, so say so kindly rather than rendering the chatter.
        self.assertEqual(app.classify_status_detail(OWNER_REPORT),
                         app.STATUS_UNAVAILABLE)
        text, klass = app.status_display(OWNER_REPORT)
        self.assertEqual(klass, app.STATUS_UNAVAILABLE)
        self.assertEqual(text, FRIENDLY)
        _assert_parent_safe(self, text, 'owner-report')

    def test_status_word_with_noise_stays_ok_and_shows_no_warning(self):
        """The manager's root cause: the status read SUCCEEDS, the session
        PAM layer fails. The parent must keep the normal pill."""
        detail = 'Unlocked\n' + OWNER_REPORT
        text, klass = app.status_display(detail, locked=False)
        self.assertEqual(klass, app.STATUS_OK)
        self.assertEqual(text, 'Unlocked')
        _assert_parent_safe(self, text, 'ok-with-noise')

    def test_every_documented_noise_pattern_is_dropped(self):
        """The ONE noise list: each marker, alone and beside a status."""
        for pattern in app.STATUS_NOISE_PATTERNS:
            with self.subTest(pattern=pattern):
                text, klass = app.status_display(
                    'Locked\nsudo: %s: whatever happened' % pattern, True)
                self.assertEqual(klass, app.STATUS_OK)
                self.assertEqual(text, 'Locked',
                                 'noise %r must not reach the pill'
                                 % pattern)

    def test_every_failure_class_yields_friendly_copy(self):
        for detail in NASTY_DETAILS:
            with self.subTest(detail=detail[:40]):
                text, klass = app.status_display(detail, None)
                self.assertEqual(klass, app.STATUS_UNAVAILABLE)
                self.assertEqual(text, FRIENDLY)
                _assert_parent_safe(self, text, 'failure')

    def test_friendly_copy_is_non_technical(self):
        """The owner copy must never name a tool, a daemon or a path."""
        low = FRIENDLY.lower()
        for needle in ('sudo', 'pam', 'cgroup', 'passwd', 'usermod',
                       'loginctl', 'varlink', 'systemd', '/', 'root',
                       'stderr', 'exit code', 'errno'):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, low,
                                 'parent-facing copy names %r' % needle)
        # ...and it must read like a sentence a parent can act on.
        self.assertIn('admin', low)
        self.assertIn('help', low)

    def test_human_product_copy_passes_through_unchanged(self):
        for detail in ('Locked', 'Unlocked', 'No password', 'unreachable'):
            with self.subTest(detail=detail):
                text, klass = app.status_display(detail, None)
                self.assertEqual(text, detail)
                self.assertEqual(klass, app.STATUS_OK)

    def test_verbatim_unknown_human_detail_is_unchanged(self):
        # Not a status word, but genuinely human product copy: it must
        # survive the mapping byte-for-byte, not be rewritten.
        detail = 'Account has no bedtime schedule yet'
        text, klass = app.status_display(detail, None)
        self.assertEqual(text, detail)
        self.assertEqual(klass, app.STATUS_VERBATIM)

    def test_classification_is_case_insensitive(self):
        for detail in ('SUDO: PAM_OPEN_SESSION: PERMISSION DENIED',
                       'PAM_UNIX(SUDO:SESSION): SESSION OPENED',
                       'PASSWD COMMAND NOT FOUND'):
            with self.subTest(detail=detail):
                text, klass = app.status_display(detail, None)
                self.assertEqual(klass, app.STATUS_UNAVAILABLE)
                self.assertEqual(text, FRIENDLY)

    def test_empty_detail_next_to_a_known_status_renders_the_status(self):
        # Degenerate but real: the probe succeeded and said nothing. The
        # status IS the answer; a warning box would be wrong.
        self.assertEqual(app.status_display('', locked=False),
                         ('Unlocked', app.STATUS_OK))
        self.assertEqual(app.status_display('', locked=True),
                         ('Locked', app.STATUS_OK))
        # With no status at all, there is nothing to be reassuring about.
        self.assertEqual(app.status_display('', locked=None),
                         (FRIENDLY, app.STATUS_UNAVAILABLE))

    def test_classifier_is_total_and_never_raises(self):
        """Pure + total: no input may make the probe path blow up."""
        for detail in (None, '', 0, 1, [], {}, object(), b'bytes',
                       'x' * 20000, '\n\n\n', '\x00\x01'):
            with self.subTest(detail=repr(detail)[:30]):
                text, klass = app.status_display(detail, None)
                self.assertIsInstance(text, str)
                self.assertIn(klass, (app.STATUS_OK, app.STATUS_UNAVAILABLE,
                                     app.STATUS_VERBATIM))


class StatusRealProbeOutputTest(unittest.TestCase):
    """End-to-end: a real `sudo passwd -S` failure, raw, all the way out.

    Everything above feeds a hand-written detail string. This drives the
    ACTUAL `get_lock_status` with a mocked subprocess that fails the way
    the VM did, then runs the real `status_row`, so the seam between the
    probe and the mapping is pinned too.
    """

    def _probe(self, rc, stdout='', stderr=''):
        result = mock.Mock(returncode=rc, stdout=stdout, stderr=stderr)
        with mock.patch.object(app.subprocess, 'run', return_value=result):
            return app.get_lock_status(KID)

    def test_the_owners_stderr_stays_in_get_lock_status_unchanged(self):
        """The probe still reports what it saw — that is the admin log's
        job, and it must not be 'cleaned up' where the read happens."""
        locked, detail = self._probe(1, stderr=OWNER_REPORT)
        self.assertIsNone(locked)
        self.assertEqual(detail, OWNER_REPORT)

    def test_status_row_sanitizes_the_real_probe_failure(self):
        with mock.patch.object(app.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=1, stdout='',
                                         stderr=OWNER_REPORT)
            with mock.patch.object(app, '_LAST_STATUS_PROBE_SIGS', {}), \
                 mock.patch.object(app, '_safe_audit') as logged:
                locked, safe, ok = app.status_row(KID)
        self.assertIsNone(locked)
        self.assertEqual(safe, FRIENDLY)
        self.assertIs(ok, False)
        # ...and the RAW text went to the admin log instead.
        self.assertEqual(logged.call_count, 1)
        self.assertEqual(logged.call_args[0][3]['raw'], OWNER_REPORT)

    def test_a_healthy_probe_is_untouched_end_to_end(self):
        with mock.patch.object(app.subprocess, 'run') as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout=f'{KID} P 2026-10-06 0 99999 7 -1\n', stderr='')
            with mock.patch.object(app, '_LAST_STATUS_PROBE_SIGS', {}), \
                 mock.patch.object(app, '_safe_audit') as logged:
                locked, safe, ok = app.status_row(KID)
        self.assertIs(locked, False)
        self.assertEqual(safe, 'Unlocked')
        self.assertIs(ok, True)
        logged.assert_not_called()

    def test_passwd_not_found_is_friendly_end_to_end(self):
        """The container-preview leak the manager flagged."""
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=FileNotFoundError(2, 'passwd')):
            with mock.patch.object(app, '_LAST_STATUS_PROBE_SIGS', {}), \
                 mock.patch.object(app, '_safe_audit'):
                locked, safe, ok = app.status_row(KID)
        self.assertIsNone(locked)
        self.assertEqual(safe, FRIENDLY)
        _assert_parent_safe(self, safe, 'passwd-missing')


# ---------------------------------------------------------------------------
# 2. No leak, anywhere: local pages, JSON, per-user page, agent payload
# ---------------------------------------------------------------------------

class StatusNoLeakRenderTest(_AdminSessionMixin, unittest.TestCase):
    """Seed nasty details, assert every parent-facing surface is clean."""

    def _fakes(self, detail, locked=None):
        # NOTE: is_wheel_member must keep qaadmin a wheel member or
        # require_admin() refuses every request (the mixin logged it in as
        # one). Only the CHILD must read as a non-admin.
        return [
            mock.patch.object(app, 'get_human_users', return_value=_one_user()),
            mock.patch.object(app, 'load_bedtime', return_value={}),
            mock.patch.object(app, 'get_lock_status',
                              return_value=(locked, detail)),
            mock.patch.object(app, 'is_in_bedtime', return_value=False),
            mock.patch.object(app, 'bonus_state',
                              return_value={'active': False}),
            mock.patch.object(auth, 'is_wheel_member', side_effect=(
                lambda u: u == self.ADMIN_USER)),
            mock.patch.object(auth, 'load_store', return_value={
                'users': {self.ADMIN_USER: {'hash': 'x', 'must_change':
                                            False, 'locked': False}}}),
            # Keep the audit out of the repo's real chain for these tests.
            mock.patch.object(app, 'audit_status_probe', return_value=True),
        ]

    def _render_all(self, detail, locked=None):
        with ExitStack() as stack:
            for patcher in self._fakes(detail, locked):
                stack.enter_context(patcher)
            html = self.client.get('/').get_data(as_text=True)
            rows = self.client.get('/api/users').get_json()
            # ensure_ascii=False so the em dash survives and the blob is
            # compared as the browser would actually see it (Flask's own
            # JSON provider escapes it as   by default).
            api_json = json.dumps(rows, ensure_ascii=False)
            user_html = self.client.get(f'/user/{KID}').get_data(as_text=True)
            live_json = self.client.get(
                f'/api/user/{KID}/live').get_data(as_text=True)
            rules_html = self.client.get('/time-rules').get_data(as_text=True)
        return html, rows, api_json, user_html, live_json, rules_html

    def test_owner_report_never_renders_anywhere(self):
        """The exact bug: raw sudo/PAM text on the child cards."""
        for detail in NASTY_DETAILS:
            with self.subTest(detail=detail[:44]):
                html, rows, api_json, user_html, live_json, rules = \
                    self._render_all(detail)
                _assert_parent_safe(self, html, 'GET /')
                _assert_parent_safe(self, api_json, 'GET /api/users')
                _assert_parent_safe(self, user_html, 'GET /user/<name>')
                _assert_parent_safe(self, live_json, 'GET /api/user/live')
                _assert_parent_safe(self, rules, 'GET /time-rules')
                # ...and the parent is TOLD something, not just spared.
                self.assertIn(FRIENDLY, html)
                self.assertIn(FRIENDLY, api_json)
                self.assertIn(FRIENDLY, user_html)

    def test_ok_with_noise_keeps_the_normal_pill_and_no_warning_box(self):
        """The manager's root cause: status read fine, session PAM noisy."""
        html, rows, api_json, user_html, live_json, rules = \
            self._render_all(VM_STDERR, locked=False)
        for label, blob in (('/', html), ('api', api_json),
                            ('user', user_html), ('live', live_json)):
            _assert_parent_safe(self, blob, label)
            with self.subTest(where=label):
                self.assertIn('Unlocked', blob)
                self.assertNotIn(FRIENDLY, blob,
                                 'a healthy status must not raise a warning '
                                 'box just because the probe logged noise')
        # The pill keeps today's OK class semantics (status-unlocked).
        self.assertIn('status-unlocked', html)

    def test_unavailable_row_carries_the_machine_readable_flag(self):
        _, rows, _, _, _, _ = self._render_all(OWNER_REPORT)
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]['status_ok'], False)

    def test_healthy_row_carries_the_machine_readable_flag(self):
        _, rows, _, _, _, _ = self._render_all('Unlocked', locked=False)
        self.assertIs(rows[0]['status_ok'], True)

    def test_pill_css_classes_are_unchanged_for_every_outcome(self):
        """UI_UX_PLAN §7.4: the pinned status-* classes must not move."""
        for detail, locked, expected in (
                ('Unlocked', False, 'status-unlocked'),
                ('Locked', True, 'status-locked'),
                (OWNER_REPORT, None, 'status-unknown')):
            with self.subTest(detail=detail[:30]):
                html, _, _, _, _, _ = self._render_all(detail, locked)
                self.assertIn(expected, html)

    def test_remote_row_from_an_older_agent_is_resanitized(self):
        """Defence in depth: the hub must not TRUST the agent's copy.

        An agent predating this fix ships raw stderr in `/users`. The
        Overview must still render friendly copy.
        """
        raw_row = {'username': 'remotekid', 'uid': 1002, 'locked': None,
                   'status_detail': OWNER_REPORT, 'bedtime': {},
                   'in_bedtime': False, 'bonus': {'active': False},
                   'wheel': False, 'admin': False}
        with mock.patch.object(app, 'get_human_users', return_value=[]), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             mock.patch.object(app.remote, 'get_device', return_value={
                 'name': 'boxb', 'url': 'http://localhost:5001'}), \
             mock.patch.object(app, '_remote_proxy',
                               return_value=(200, [dict(raw_row)], False)), \
             mock.patch.object(auth, 'load_store', return_value={
                 'users': {self.ADMIN_USER: {'hash': 'x', 'must_change':
                                             False, 'locked': False}}}):
            html = self.client.get('/?device=boxb').get_data(as_text=True)
        _assert_parent_safe(self, html, 'remote /')
        self.assertIn(FRIENDLY, html)

    def test_remote_healthy_row_is_untouched(self):
        """Sanitizing must not rewrite a normal remote status."""
        row = {'username': 'remotekid', 'uid': 1002, 'locked': False,
               'status_detail': 'Unlocked', 'bedtime': {},
               'in_bedtime': False, 'bonus': {'active': False},
               'wheel': False, 'admin': False}
        clean = app._sanitize_remote_status(dict(row))
        self.assertEqual(clean['status_detail'], 'Unlocked')
        self.assertIs(clean['status_ok'], True)


# ---------------------------------------------------------------------------
# 3. The admin audit hook + its dedup
# ---------------------------------------------------------------------------

class StatusAuditDedupTest(unittest.TestCase):
    """Raw text goes to the admin log — once per CHANGE, not every 10s."""

    def test_unavailable_outcome_is_audited_with_the_raw_detail(self):
        state = {}
        with mock.patch.object(app, 'get_lock_status',
                               return_value=(None, OWNER_REPORT)), \
             mock.patch.object(app, '_safe_audit') as logged:
            app.audit_status_probe(KID, app.STATUS_UNAVAILABLE,
                                   OWNER_REPORT, state)
        logged.assert_called_once()
        args = logged.call_args[0]
        self.assertEqual(args[0], 'system')
        self.assertEqual(args[1], 'account.status_probe')
        self.assertEqual(args[2], KID)
        self.assertEqual(args[3], {'ok': False,
                                   'class': app.STATUS_UNAVAILABLE,
                                   'raw': OWNER_REPORT})

    def test_identical_detail_audits_once_then_a_change_audits_again(self):
        state = {}
        with mock.patch.object(app, '_safe_audit') as logged:
            for _ in range(5):
                app.audit_status_probe(KID, app.STATUS_UNAVAILABLE,
                                       OWNER_REPORT, state)
            self.assertEqual(logged.call_count, 1,
                             'the 10s poll must not append the same line '
                             'forever')
            # A CHANGED detail is a new fact and must be recorded.
            app.audit_status_probe(KID, app.STATUS_UNAVAILABLE,
                                   'passwd command not found', state)
            self.assertEqual(logged.call_count, 2)
            # And back to the original: still a change, still logged.
            app.audit_status_probe(KID, app.STATUS_UNAVAILABLE,
                                   OWNER_REPORT, state)
            self.assertEqual(logged.call_count, 3)

    def test_ok_with_noise_is_audited(self):
        state = {}
        with mock.patch.object(app, '_safe_audit') as logged:
            app.audit_status_probe(KID, app.STATUS_OK, VM_STDERR, state)
        self.assertEqual(logged.call_count, 1)
        detail = logged.call_args[0][3]
        self.assertIs(detail['ok'], True)
        self.assertEqual(detail['class'], app.STATUS_OK)
        self.assertEqual(detail['raw'], VM_STDERR)

    def test_plain_healthy_outcome_is_not_audited(self):
        state = {}
        with mock.patch.object(app, '_safe_audit') as logged:
            self.assertFalse(app.audit_status_probe(
                KID, app.STATUS_OK, 'Unlocked', state))
            self.assertFalse(app.audit_status_probe(
                KID, app.STATUS_VERBATIM, 'Account has no schedule', state))
        logged.assert_not_called()

    def test_status_row_wires_probe_sanitize_and_audit_together(self):
        """One boundary helper: raw in, parent-safe out, admin log written."""
        with mock.patch.object(app, 'get_lock_status',
                               return_value=(None, OWNER_REPORT)), \
             mock.patch.object(app, '_LAST_STATUS_PROBE_SIGS', {}) as sigs, \
             mock.patch.object(app, '_safe_audit') as logged:
            locked, safe, ok = app.status_row(KID)
        self.assertIsNone(locked)
        self.assertEqual(safe, FRIENDLY)
        self.assertIs(ok, False)
        self.assertEqual(logged.call_count, 1)
        self.assertIn(KID, sigs, 'the dedup signature must be remembered')


class StatusAuditSurvivesFailuresTest(unittest.TestCase):
    """An audit write must never break a status read."""

    def test_probe_still_returns_when_the_audit_raises(self):
        with mock.patch.object(app, 'get_lock_status',
                               return_value=(None, OWNER_REPORT)), \
             mock.patch.object(app, '_LAST_STATUS_PROBE_SIGS', {}), \
             mock.patch.object(app, 'audit', ) as audit_mod:
            audit_mod.safe_log.side_effect = OSError('disk full')
            locked, safe, ok = app.status_row(KID)
        self.assertEqual(safe, FRIENDLY)
        self.assertIs(ok, False)


# ---------------------------------------------------------------------------
# 4. The agent side (the managed box that never imports app)
# ---------------------------------------------------------------------------

class AgentStatusNoLeakTest(BaseAgentTest):
    """The agent's /users payload is parent-facing too — it must be clean."""

    def _users_payload(self, detail, locked=None):
        with mock.patch.object(agent, 'get_human_users',
                               return_value=_one_user()), \
             mock.patch.object(agent, 'get_lock_status',
                               return_value=(locked, detail)), \
             mock.patch.object(agent, 'load_bedtime', return_value={}), \
             mock.patch.object(agent, 'is_in_bedtime', return_value=False), \
             mock.patch.object(agent, 'bonus_state',
                               return_value={'active': False}), \
             mock.patch.object(auth, 'is_wheel_member', return_value=False), \
             mock.patch.object(agent, '_LAST_STATUS_PROBE_SIGS', {}), \
             mock.patch.object(agent, '_safe_audit'):
            return call(self.app, '/api/v1/users',
                        headers=auth_header(self.TOKEN))

    def test_agent_never_ships_raw_stderr_to_the_hub(self):
        for detail in NASTY_DETAILS:
            with self.subTest(detail=detail[:44]):
                res = self._users_payload(detail)
                self.assertEqual(200, res['code'])
                blob = json.dumps(res['payload'], ensure_ascii=False)
                _assert_parent_safe(self, blob, 'agent /users')
                self.assertIn(FRIENDLY, blob)
                self.assertIs(res['payload'][0]['status_ok'], False)

    def test_agent_keeps_a_healthy_status_unchanged(self):
        res = self._users_payload('Unlocked', locked=False)
        self.assertEqual(res['payload'][0]['status_detail'], 'Unlocked')
        self.assertIs(res['payload'][0]['status_ok'], True)

    def test_agent_status_ok_flag_is_additive_only(self):
        """The pinned wire contract must not lose a key (the hub's
        test_agent_users_rows_cover_js_consumed_fields guards it)."""
        res = self._users_payload('Unlocked', locked=False)
        row = res['payload'][0]
        for key in ('username', 'uid', 'locked', 'status_detail', 'bedtime',
                    'in_bedtime', 'bonus', 'wheel', 'admin'):
            with self.subTest(key=key):
                self.assertIn(key, row)


# ---------------------------------------------------------------------------
# 5. app.py <-> agent.py mirror (the agent may not import app)
# ---------------------------------------------------------------------------

class StatusDetailMirrorTest(unittest.TestCase):
    """agent.py duplicates the table; the two must not drift."""

    MIRRORED = ('STATUS_OK', 'STATUS_UNAVAILABLE', 'STATUS_VERBATIM',
                'STATUS_UNAVAILABLE_COPY', 'STATUS_NOISE_PATTERNS',
                'STATUS_TECHNICAL_PATTERNS', 'STATUS_HUMAN_WORDS')

    def test_constants_are_identical(self):
        for name in self.MIRRORED:
            with self.subTest(name=name):
                self.assertEqual(getattr(app, name), getattr(agent, name),
                                 'app.%s and agent.%s have drifted — the '
                                 'agent must classify exactly like the hub'
                                 % (name, name))

    @staticmethod
    def _code_without_docstring(fn):
        """`fn`'s AST with the docstring removed, as a canonical string.

        app.py carries the full rationale in its docstrings and agent.py
        carries a pointer to it, so the PROSE is expected to differ; the
        EXECUTABLE body must not, or the two boxes would classify a probe
        detail differently.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)).strip())
        fn_def = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
        body = fn_def.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        return ast.dump(ast.Module(body=body, type_ignores=[]))

    def test_function_bodies_are_identical(self):
        for name in ('status_residual_lines', 'classify_status_detail',
                     'status_display', 'audit_status_probe', 'status_row'):
            with self.subTest(name=name):
                self.assertEqual(
                    self._code_without_docstring(getattr(app, name)),
                    self._code_without_docstring(getattr(agent, name)),
                    'app.%s and agent.%s have drifted — the agent must '
                    'classify exactly like the hub' % (name, name))

    def test_both_modules_agree_on_every_nasty_detail(self):
        for detail in NASTY_DETAILS:
            with self.subTest(detail=detail[:44]):
                self.assertEqual(app.status_display(detail, None),
                                 agent.status_display(detail, None))

    def test_agent_does_not_import_app(self):
        """The mirror is the price of this hard property — keep it true."""
        with open(os.path.join(ROOT, 'agent.py'), encoding='utf-8') as fh:
            src = fh.read()
        self.assertNotRegex(
            re.sub(r'#.*', '', src), r'^\s*import\s+app\b',
            'agent.py must not import app (MULTIDEVICE_PLAN §4.1)')


# ---------------------------------------------------------------------------
# 6. Regression guards on the pinned surface
# ---------------------------------------------------------------------------

class StatusPayloadContractTest(_AdminSessionMixin, unittest.TestCase):
    """The keys the pinned selector/contract tests rely on must survive."""

    def test_api_users_row_keeps_every_js_consumed_field(self):
        with ExitStack() as stack:
            for patcher in self._fakes('Unlocked', False):
                stack.enter_context(patcher)
            rows = self.client.get('/api/users').get_json()
        row = rows[0]
        for key in ('username', 'uid', 'locked', 'status_detail', 'bedtime',
                    'in_bedtime', 'countdown', 'wheel', 'admin',
                    'status_ok'):
            with self.subTest(key=key):
                self.assertIn(key, row)

    def test_status_detail_is_still_a_non_empty_string(self):
        """Templates render it unconditionally (the pill, the card meta,
        the per-user hero). An empty value would silently blank them."""
        for detail, locked in ((OWNER_REPORT, None), ('Unlocked', False),
                               ('Locked', True), (VM_STDERR, False)):
            with self.subTest(detail=detail[:30]):
                with ExitStack() as stack:
                    for patcher in self._fakes(detail, locked):
                        stack.enter_context(patcher)
                    rows = self.client.get('/api/users').get_json()
                self.assertIsInstance(rows[0]['status_detail'], str)
                self.assertTrue(rows[0]['status_detail'].strip())

    def _fakes(self, detail, locked):
        # Same note as the render harness: qaadmin must stay a wheel member.
        return [
            mock.patch.object(app, 'get_human_users', return_value=_one_user()),
            mock.patch.object(app, 'load_bedtime', return_value={}),
            mock.patch.object(app, 'get_lock_status',
                              return_value=(locked, detail)),
            mock.patch.object(app, 'is_in_bedtime', return_value=False),
            mock.patch.object(app, 'bonus_state',
                              return_value={'active': False}),
            mock.patch.object(auth, 'is_wheel_member', side_effect=(
                lambda u: u == self.ADMIN_USER)),
            mock.patch.object(auth, 'load_store', return_value={
                'users': {self.ADMIN_USER: {'hash': 'x', 'must_change':
                                            False, 'locked': False}}}),
            mock.patch.object(app, 'audit_status_probe', return_value=True),
        ]


if __name__ == '__main__':
    unittest.main()