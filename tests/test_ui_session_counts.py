#!/usr/bin/env python3
"""Active-session counts on the Overview's System summary card.

Owner request (2026-10-18): the card printed a hardcoded
`<p id="summary-sessions">1 active session</p>` next to a placeholder
line admitting the real number was not exposed. It is measured now, with
the SAME two loginctl calls the screen-lock path already makes:

    sudo loginctl show-user <u> -p Sessions --value
    sudo loginctl show-session <id> -p Class -p Type   (once per session)

Both are already on the scoped allowlist, so this adds NO sudoers rule and
NO new privileged argv — pinned at the bottom of this module.

Only `Class=user` sessions count. Every user running a systemd user
manager also has a `Class=manager` session (verified live: bluefox ->
"21 20", 21 = unspecified/manager, 20 = wayland/user) and a manager
session is NOT a login.

What these tests pin:
  * a manager session is excluded and a user-class session is counted;
  * 0 / 1 / N render "No active sessions" / "1 active session" /
    "N active sessions", and the placeholder line is gone;
  * the count is cached 30s per user (same pattern as the lock-status
    probe) and invalidated by the emergency logout;
  * the first paint's total and updateSummary()'s rebuild total agree;
  * remote rows that omit `sessions` do not break the sum;
  * the rendered sudoers templates are byte-for-byte the pinned grants.

Nothing here touches a real account: `subprocess.run` is mocked and the
agent's own helpers are exercised through its Flask test app. No /etc,
/run, /opt or checkout path is read outside the repo.
"""

import os
import sys
import unittest
from collections import namedtuple
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import agent
import app
import auth
import test_deployment as dep
from test_lock_regression import _AdminSessionMixin
from test_multidevice_agent import BaseAgentTest, auth_header, call

Result = namedtuple('Result', ['returncode', 'stdout', 'stderr'])

KID = 'sessionkid'
OTHER = 'sessionkid2'

# Live logind output for a child sitting at a graphical login: the manager
# session first, then the real one.
LIVE_SESSIONS = '21 20'
MANAGER_SESSION = '21'
USER_SESSION = '20'

USERS = [
    {'username': KID, 'uid': 1001, 'home': '/home/' + KID, 'shell': '/bin/bash'},
    {'username': OTHER, 'uid': 1002, 'home': '/home/' + OTHER,
     'shell': '/bin/bash'},
]


def _session_props(klass, type_='wayland'):
    """stdout of `loginctl show-session <id> -p Class -p Type`."""
    return f'Class={klass}\nType={type_}\n'


class _Clock:
    def __init__(self, start=500.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SessionProbeBase(unittest.TestCase):
    """A fake logind driven by a session-id -> properties table."""

    def setUp(self):
        for mod in (app, agent):
            mod.invalidate_session_count()
            self.addCleanup(mod.invalidate_session_count)
        self.clock = _Clock()
        for mod in (app, agent):
            patcher = mock.patch.object(mod.time, 'monotonic', self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sessions = {}      # username -> raw `show-user` stdout
        self.classes = {}       # session id -> (Class, Type)
        self.calls = []         # every argv, in order
        for mod in (app, agent):
            patcher = mock.patch.object(mod.subprocess, 'run',
                                        side_effect=self._run)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[:3] == ['sudo', 'loginctl', 'show-user']:
            return Result(0, self.sessions.get(cmd[3], ''), '')
        if cmd[:3] == ['sudo', 'loginctl', 'show-session']:
            return Result(0, self.classes.get(cmd[3], ''), '')
        if cmd[:2] == ['sudo', 'usermod'] or cmd[:3] == [
                'sudo', 'loginctl', 'terminate-user']:
            return Result(0, '', '')
        raise AssertionError('unexpected command %r' % (cmd,))

    def _logged_in(self, username=KID, klass='user', type_='wayland'):
        """One account with a manager session and a user-class session."""
        self.sessions[username] = LIVE_SESSIONS
        self.classes[MANAGER_SESSION] = _session_props('manager', 'unspecified')
        self.classes[USER_SESSION] = _session_props(klass, type_)


# ---------------------------------------------------------------------------
# 1. The counting rule
# ---------------------------------------------------------------------------

class ActiveSessionCountTest(SessionProbeBase):
    def test_the_manager_session_is_excluded_and_the_user_session_counted(self):
        # The exact live case: two sessions reported, ONE login.
        self._logged_in()
        self.assertEqual(1, app.active_session_count(KID))

    def test_a_non_user_class_session_is_excluded(self):
        # greeter / tty / background sessions are not logins either.
        self.sessions[KID] = 'a b c'
        self.classes['a'] = _session_props('manager', 'unspecified')
        self.classes['b'] = _session_props('greeter', 'x11')
        self.classes['c'] = _session_props('tty', 'tty')
        self.assertEqual(0, app.active_session_count(KID))

    def test_a_tty_login_counts_because_it_is_a_user_session(self):
        # Class=user is the rule, not "graphical": a console login is a
        # real session the parent cares about seeing.
        self.sessions[KID] = 'a'
        self.classes['a'] = _session_props('user', 'tty')
        self.assertEqual(1, app.active_session_count(KID))

    def test_an_offline_account_counts_zero(self):
        self.assertEqual(0, app.active_session_count(KID))

    def test_a_vanished_session_does_not_break_the_count(self):
        # logind listed it, then it died between the two calls.
        self.sessions[KID] = '21 20'
        self.classes[MANAGER_SESSION] = _session_props('manager', 'unspecified')
        self.classes[USER_SESSION] = ''      # show-session failed / empty
        self.assertEqual(0, app.active_session_count(KID))

    def test_the_loginctl_call_sequence(self):
        self._logged_in()
        app.active_session_count(KID)
        self.assertEqual([
            ['sudo', 'loginctl', 'show-user', KID, '-p', 'Sessions', '--value'],
            ['sudo', 'loginctl', 'show-session', MANAGER_SESSION,
             '-p', 'Class', '-p', 'Type'],
            ['sudo', 'loginctl', 'show-session', USER_SESSION,
             '-p', 'Class', '-p', 'Type'],
        ], self.calls, 'both argv are pre-existing allowlist entries')

    def test_agent_counts_its_own_sessions_the_same_way(self):
        # The agent runs the identical two commands, so a remote box can
        # report the same number with no extra grant.
        self._logged_in()
        self.assertEqual(app.active_session_count(KID),
                         agent.active_session_count(KID))

    def test_agent_and_hub_agree_on_a_mixed_case(self):
        self.sessions[KID] = '1 2 3'
        self.classes['1'] = _session_props('manager', 'unspecified')
        self.classes['2'] = _session_props('user', 'wayland')
        self.classes['3'] = _session_props('user', 'x11')
        self.assertEqual(2, app.active_session_count(KID))
        self.assertEqual(2, agent.active_session_count(KID))


# ---------------------------------------------------------------------------
# 2. The 30s cache
# ---------------------------------------------------------------------------

class SessionCountCacheTest(SessionProbeBase):
    def test_the_probe_runs_once_inside_the_ttl_and_again_after_it(self):
        self._logged_in()
        self.assertEqual(1, app.active_session_count(KID))
        self.assertEqual(1, app.active_session_count(KID))
        self.assertEqual(1, app.active_session_count(KID))
        self.assertEqual(3, len(self.calls), 'the 10s poll must not re-probe')
        self.clock.advance(app.SESSION_COUNT_TTL_SECONDS + 1)
        self.assertEqual(1, app.active_session_count(KID))
        self.assertEqual(6, len(self.calls))

    def test_the_cache_is_per_username(self):
        # KID has manager+user (3 loginctl calls), OTHER has two user
        # sessions (3 more) — and KID's repeat stays cached (0 more).
        self._logged_in(KID)
        self.sessions[OTHER] = '30 31'
        self.classes['30'] = _session_props('user', 'x11')
        self.classes['31'] = _session_props('user', 'wayland')
        self.assertEqual(1, app.active_session_count(KID))
        self.assertEqual(2, app.active_session_count(OTHER))
        self.assertEqual(1, app.active_session_count(KID))
        self.assertEqual(6, len(self.calls))
        # The agent keeps its OWN cache (separate process, separate map).
        self.assertEqual({}, agent._SESSION_COUNT_CACHE)

    def test_a_login_inside_the_ttl_appears_after_it_expires(self):
        self.sessions[KID] = ''
        self.assertEqual(0, app.active_session_count(KID))
        self._logged_in()
        self.clock.advance(app.SESSION_COUNT_TTL_SECONDS + 1)
        self.assertEqual(1, app.active_session_count(KID))

    def test_a_logout_inside_the_ttl_is_reported_after_it_expires(self):
        self._logged_in()
        self.assertEqual(1, app.active_session_count(KID))
        self.sessions[KID] = ''
        self.clock.advance(app.SESSION_COUNT_TTL_SECONDS + 1)
        self.assertEqual(0, app.active_session_count(KID))

    def test_both_caches_share_the_thirty_second_ttl(self):
        self.assertEqual(30.0, app.SESSION_COUNT_TTL_SECONDS)
        self.assertEqual(30.0, agent.SESSION_COUNT_TTL_SECONDS)

    def test_the_emergency_logout_invalidates_the_count(self):
        self._logged_in()
        self.assertEqual(1, app.active_session_count(KID))
        with mock.patch.object(app, 'note_manual_lock'), \
             mock.patch.object(app, '_safe_audit'):
            ok, _msg = app._emergency_logout(KID)
        self.assertTrue(ok)
        # The terminate-user in that call ended the sessions for real, so
        # the cached "1 active session" must be gone, not served for a
        # whole TTL.
        self.sessions[KID] = ''
        self.assertEqual(0, app.active_session_count(KID))

    def test_invalidate_session_count_clears_one_user_or_all(self):
        self._logged_in(KID)
        self.sessions[OTHER] = '40'
        self.classes['40'] = _session_props('user', 'wayland')
        app.active_session_count(KID)
        app.active_session_count(OTHER)
        app.invalidate_session_count(KID)
        self.assertNotIn(KID, app._SESSION_COUNT_CACHE)
        self.assertIn(OTHER, app._SESSION_COUNT_CACHE)
        app.invalidate_session_count()
        self.assertEqual({}, app._SESSION_COUNT_CACHE)


# ---------------------------------------------------------------------------
# 3. The card: payload, first paint and rebuild
# ---------------------------------------------------------------------------

def _sessions_text(html):
    """The rendered copy of the #summary-sessions line."""
    start = html.index('id="summary-sessions">') + len('id="summary-sessions">')
    return html[start:html.index('<', start)].strip()


class SessionCountCardTest(_AdminSessionMixin, unittest.TestCase):
    """The System summary card shows the real number, both paints."""

    def _fakes(self, counts):
        store = {'users': {self.ADMIN_USER: {'hash': 'x', 'must_change':
                                             False, 'locked': False}}}
        return [
            mock.patch.object(app, 'get_human_users', return_value=USERS),
            mock.patch.object(app, 'load_bedtime', return_value={}),
            mock.patch.object(app, 'get_lock_status',
                              return_value=(False, 'Unlocked')),
            mock.patch.object(app, 'is_in_bedtime', return_value=False),
            mock.patch.object(app, 'bonus_state',
                              return_value={'active': False}),
            mock.patch.object(app, 'active_session_count',
                              side_effect=lambda u: counts[u]),
            mock.patch.object(auth, 'is_wheel_member',
                              side_effect=lambda u: u == self.ADMIN_USER),
            mock.patch.object(auth, 'load_store', return_value=store),
        ]

    def _render_both(self, counts):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for patcher in self._fakes(counts):
                stack.enter_context(patcher)
            html = self.client.get('/').get_data(as_text=True)
            rows = self.client.get('/api/users').get_json()
        return html, {r['username']: r for r in rows}

    def _rebuild_total(self, rows):
        """The total updateSummary() computes from the payload rows.

        Mirrors the page script's reduce() so the two paints are compared
        by value, not by substring.
        """
        return sum(r['sessions'] for r in rows.values()
                   if isinstance(r.get('sessions'), int))

    def test_card_shows_no_active_sessions_when_nobody_is_logged_in(self):
        html, rows = self._render_both({KID: 0, OTHER: 0})
        self.assertEqual('No active sessions', _sessions_text(html))
        self.assertEqual(0, self._rebuild_total(rows))

    def test_card_says_one_active_session_in_the_singular(self):
        html, rows = self._render_both({KID: 1, OTHER: 0})
        self.assertEqual('1 active session', _sessions_text(html))
        self.assertEqual(1, self._rebuild_total(rows))

    def test_card_pluralizes_for_more_than_one(self):
        html, rows = self._render_both({KID: 2, OTHER: 3})
        self.assertEqual('5 active sessions', _sessions_text(html))
        self.assertEqual(5, self._rebuild_total(rows))

    def test_first_paint_and_rebuild_totals_agree(self):
        # The 10s rebuild recomputes the total from the payload; if the two
        # ever disagree the card flips numbers under the parent.
        for counts in ({KID: 0, OTHER: 0}, {KID: 1, OTHER: 0},
                       {KID: 2, OTHER: 3}, {KID: 4, OTHER: 0}):
            with self.subTest(counts=counts):
                html, rows = self._render_both(counts)
                total = self._rebuild_total(rows)
                self.assertEqual(
                    'No active sessions' if total == 0 else
                    '1 active session' if total == 1 else
                    f'{total} active sessions',
                    _sessions_text(html))

    def test_the_placeholder_hint_is_gone(self):
        html, _rows = self._render_both({KID: 1, OTHER: 0})
        self.assertNotIn('Active-session counts are not exposed',
                         html)
        self.assertNotIn('not exposed by the local service', html)

    def test_api_users_rows_carry_an_integer_sessions_key(self):
        _html, rows = self._render_both({KID: 2, OTHER: 0})
        for username, row in sorted(rows.items()):
            with self.subTest(row=username):
                self.assertIsInstance(row['sessions'], int)
                self.assertNotIsInstance(row['sessions'], bool)

    def test_the_page_script_totals_the_same_key(self):
        # updateSummary() must read `sessions` — that is what makes the
        # parity contract in tests/test_ui_overview_parity.py require the
        # key on every /api/users row.
        import test_ui_overview_parity as parity
        self.assertIn('sessions', parity.js_fields_read_by_update_summary())

    def test_remote_rows_without_the_key_do_not_break_the_sum(self):
        # An agent that does not report `sessions` yet: its rows simply
        # contribute nothing (the documented degradation, like `countdown`).
        entry = {'name': 'boxb', 'url': 'http://localhost:5001'}
        remote_rows = [
            {'username': 'remotekid', 'uid': 1005, 'locked': False,
             'status_detail': 'Unlocked', 'bedtime': {}, 'in_bedtime': False,
             'bonus': {'active': False}, 'sessions': 2},
            {'username': 'legacykid', 'uid': 1006, 'locked': False,
             'status_detail': 'Unlocked', 'bedtime': {}, 'in_bedtime': False,
             'bonus': {'active': False}},
        ]
        store = {'users': {self.ADMIN_USER: {'hash': 'x', 'must_change':
                                             False, 'locked': False}}}
        with mock.patch.object(app.remote, 'get_device',
                               return_value=entry), \
             mock.patch.object(app, '_remote_users',
                               return_value=(remote_rows, None)), \
             mock.patch.object(app, 'get_human_users', return_value=[]), \
             mock.patch.object(app, 'load_bedtime', return_value={}), \
             mock.patch.object(auth, 'load_store', return_value=store):
            html = self.client.get('/?device=boxb').get_data(as_text=True)
        self.assertEqual('2 active sessions', _sessions_text(html))


# ---------------------------------------------------------------------------
# 4. The agent reports the same key
# ---------------------------------------------------------------------------

class AgentSessionKeyTest(BaseAgentTest):
    AGENT_USERS = [
        {'username': 'kidremote', 'uid': 1004, 'home': '/home/kidremote',
         'shell': '/bin/bash'},
    ]

    def _read_users(self, count):
        store = {'users': {'boxadmin': {'hash': 'x', 'must_change': False,
                                       'locked': False}}}
        with mock.patch.object(agent, 'get_human_users',
                               return_value=self.AGENT_USERS), \
             mock.patch.object(agent, 'get_lock_status',
                               return_value=(False, 'Unlocked')), \
             mock.patch.object(agent, 'active_session_count',
                               return_value=count), \
             mock.patch.object(agent, 'load_bedtime', return_value={}), \
             mock.patch.object(agent, 'is_in_bedtime', return_value=False), \
             mock.patch.object(auth, 'is_wheel_member', return_value=False), \
             mock.patch.object(auth, 'load_store', return_value=store):
            return call(self.app, '/api/v1/users', headers=auth_header(self.TOKEN))

    def test_agent_users_rows_carry_the_sessions_key(self):
        res = self._read_users(2)
        self.assertEqual(200, res['code'])
        row = res['payload'][0]
        self.assertEqual(2, row['sessions'])
        # ...so the hub's parity contract needs no new degradation entry.
        import test_ui_overview_parity as parity
        self.assertNotIn('sessions', parity.AGENT_ACCEPTED_DEGRADATIONS)


# ---------------------------------------------------------------------------
# 5. No new privileged surface (the whole point of reusing the argv)
# ---------------------------------------------------------------------------

class NoNewSudoersLineTest(unittest.TestCase):
    """The count reuses two existing grants; no template gained a line.

    Statically pinned on purpose: the live `authorized_by_template()`
    helper resolves command paths through `secure_path`, so it can only run
    on a box that has loginctl installed. These assertions are about the
    TEMPLATE TEXT, which is the thing that must not change.
    """

    PINNED_LOGINCTL_GRANTS = (
        '^terminate-user [A-Za-z][A-Za-z0-9_.-]*$',
        '^terminate-user [A-Za-z][A-Za-z0-9_.-]*$',
        '^show-user [A-Za-z0-9_.-]+ -p Sessions --value$',
        '^show-user [A-Za-z0-9_.-]+ -p Sessions --value$',
        '^lock-session [A-Za-z0-9_.-]+( [A-Za-z0-9_.-]+)*$',
        '^lock-session [A-Za-z0-9_.-]+( [A-Za-z0-9_.-]+)*$',
        '^show-session [A-Za-z0-9_.-]+ -p Class -p Type$',
        '^show-session [A-Za-z0-9_.-]+ -p Class -p Type$',
    )

    def _argv_literals(self, module, function):
        """The `['sudo', ...]` argv literals inside one function's source.

        Mirrors test_deployment._sudo_list_argvs (including the
        `['sudo', ...] + runtime_list` shape) but scoped to one function,
        so it can assert WHERE a privileged call lives.
        """
        import ast
        source = dep.read(module)
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == function)

        def render(node):
            parts = []
            for element in node.elts:
                if isinstance(element, ast.Constant):
                    parts.append(element.value)
                elif isinstance(element, ast.Name):
                    parts.append('<%s>' % element.id)
                elif isinstance(element, ast.Starred):
                    parts.append('<...>')
                else:
                    parts.append('<dyn>')
            return parts

        def headed(node):
            return (isinstance(node, ast.List) and node.elts
                    and isinstance(node.elts[0], ast.Constant)
                    and node.elts[0].value == 'sudo')

        concatenated = {id(n.left) for n in ast.walk(fn)
                        if isinstance(n, ast.BinOp) and headed(n.left)}
        found = []
        for node in ast.walk(fn):
            if headed(node) and id(node) not in concatenated:
                found.append(' '.join(render(node)))
            elif (isinstance(node, ast.BinOp) and headed(node.left)
                  and isinstance(node.right, ast.Name)):
                found.append(' '.join(render(node.left)
                                      + ['<%s...>' % node.right.id]))
        return sorted(found)

    def test_the_panel_loginctl_grants_are_exactly_the_pinned_set(self):
        specs = dep.parse_command_specs(
            dep.rendered_template('sudoers-parental-controls'))
        grants = tuple(args for binary, args in specs
                       if binary.endswith('/loginctl'))
        self.assertEqual(self.PINNED_LOGINCTL_GRANTS, grants,
                         'session counting must not have added, removed or '
                         'reordered a loginctl grant')

    def test_the_budgets_template_is_untouched_by_this_feature(self):
        # It is the other template install.sh renders; nothing here reads a
        # cgroup, so it must stay exactly as it was.
        specs = dep.parse_command_specs(
            dep.rendered_template('sudoers-parental-controls-budgets'))
        self.assertEqual(10, len(specs))
        self.assertEqual({('/usr/sbin/tee', '^/sys/fs/cgroup/parental-'
                           '[A-Za-z0-9_.-]+/cgroup[.]freeze$'),
                          ('/usr/bin/tee', '^/sys/fs/cgroup/parental-'
                           '[A-Za-z0-9_.-]+/cgroup[.]freeze$'),
                          ('/usr/sbin/tee', '^/sys/fs/cgroup/parental-'
                           '[A-Za-z0-9_.-]+/cgroup[.]procs$'),
                          ('/usr/bin/tee', '^/sys/fs/cgroup/parental-'
                           '[A-Za-z0-9_.-]+/cgroup[.]procs$'),
                          ('/usr/sbin/cat', '^/sys/fs/cgroup/parental-'
                           '[A-Za-z0-9_.-]+/cgroup[.]freeze$'),
                          ('/usr/bin/cat', '^/sys/fs/cgroup/parental-'
                           '[A-Za-z0-9_.-]+/cgroup[.]freeze$'),
                          ('/usr/sbin/cat', '^/sys/fs/cgroup/parental-'
                           '[A-Za-z0-9_.-]+/cgroup[.]procs$'),
                          ('/usr/bin/cat', '^/sys/fs/cgroup/parental-'
                           '[A-Za-z0-9_.-]+/cgroup[.]procs$'),
                          ('/usr/sbin/mkdir', '^-p /sys/fs/cgroup/parental-'
                           '[A-Za-z0-9_.-]+$'),
                          ('/usr/bin/mkdir', '^-p /sys/fs/cgroup/parental-'
                           '[A-Za-z0-9_.-]+$')},
                         set(specs))

    def test_the_count_helpers_run_only_the_two_allowlisted_argv(self):
        # Both modules resolve sessions with the SAME two pre-existing
        # grants — a new one would be a new root rule, which this feature
        # explicitly must not add.
        expected = (
            "sudo loginctl show-user <username> -p Sessions --value",
            "sudo loginctl show-session <session_id> -p Class -p Type",
        )
        for module in ('app.py', 'agent.py'):
            for function in ('_user_session_ids', '_session_properties'):
                with self.subTest(module=module, function=function):
                    self.assertEqual(
                        [expected[0] if function == '_user_session_ids'
                         else expected[1]],
                        self._argv_literals(module, function),
                        f'{module}:{function} must run exactly one '
                        'already-allowlisted argv')

    def test_the_shared_helpers_are_the_only_session_probes(self):
        # `lock_user_sessions` used to inline both calls; it must now go
        # through the shared helpers, so the surface really is unchanged
        # and not two call sites that drifted apart. The panel's core lives
        # in locks.py since RESTRUCTURE_PLAN.md slice 10; the helpers it
        # resolves stay facade-side (app.py, read as `app._user_session_ids`
        # / `app._session_properties` at call time per §1.1).
        for module in ('locks.py', 'agent.py'):
            with self.subTest(module=module):
                fn = self._argv_literals(module, 'lock_user_sessions')
                self.assertEqual(['sudo loginctl lock-session '
                                      '<lockable...>'], fn,
                                 'only the lock-session call may remain '
                                 'inline; the probes are shared now')

    def test_emergency_logout_still_terminates_sessions(self):
        # The one invalidating call site: if terminate-user were dropped the
        # cache would never be refreshed early. `_emergency_logout` lives
        # in locks.py since slice 10; the agent's twin is `do_logout`.
        for module in ('locks.py', 'agent.py'):
            with self.subTest(module=module):
                self.assertIn('sudo loginctl terminate-user <username>',
                              ' '.join(self._argv_literals(module,
                                                            'do_logout')
                                       if module == 'agent.py'
                                       else self._argv_literals(
                                           module, '_emergency_logout')))


if __name__ == '__main__':
    unittest.main()