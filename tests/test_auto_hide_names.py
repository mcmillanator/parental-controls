#!/usr/bin/env python3
"""Tests for auto-hide + human-friendly process names.

Everything here is mocked/synthetic: .desktop files live in tmp dirs,
process snapshots are hand-built dicts, and Flask paths use a mocked
get_user_live / admin session. No live accounts, no real ps reads.

Covers budgets.parse_desktop_file Name= capture, build_desktop_names,
is_user_app (each rule), mark_user_apps, annotate_display_names, the
mark -> filter -> annotate pipeline (pipewire hidden, firefox visible
with a friendly name, steam visible via override, home-launched script
visible via the home rule, shown_comm re-show), and the live payload +
template display_name rendering with comm fallback.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import budgets
from budgets import (
    annotate_display_names,
    apply_filter,
    build_desktop_index,
    build_desktop_names,
    group_by_comm,
    is_user_app,
    mark_user_apps,
    parse_desktop_file,
)

try:
    import flask  # noqa: F401
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

FIREFOX_DESKTOP = (
    '[Desktop Entry]\n'
    'Name=Firefox Web Browser\n'
    'Exec=/usr/lib64/firefox/firefox %u\n'
    'Categories=Network;WebBrowser;\n'
)

HELPER_DESKTOP = (
    '[Desktop Entry]\n'
    'Name=My Helper\n'
    'Exec=/opt/helper/helper --serve\n'
)


def write_desktop_files(tmpdir):
    with open(os.path.join(tmpdir, 'firefox.desktop'), 'w') as f:
        f.write(FIREFOX_DESKTOP)
    with open(os.path.join(tmpdir, 'helper.desktop'), 'w') as f:
        f.write(HELPER_DESKTOP)


def witness_procs():
    """Synthetic snapshot: plumbing, a real app, a daemon, the steam
    chain, a home-launched script, and a flatpak launch."""
    return [
        {'pid': 100, 'comm': 'pipewire',
         'cmdline': '/usr/bin/pipewire', 'args': '',
         'etime': '01:00:00', 'cpu_seconds': 5.0},
        {'pid': 101, 'comm': 'firefox',
         'cmdline': '/usr/lib64/firefox/firefox --new-window', 'args': '',
         'etime': '01:00:00', 'cpu_seconds': 60.0},
        {'pid': 102, 'comm': 'mydaemon',
         'cmdline': '/usr/bin/mydaemon --fork', 'args': '',
         'etime': '01:00:00', 'cpu_seconds': 1.0},
        {'pid': 103, 'comm': 'reaper',
         'cmdline': '/opt/steam/reaper SteamLaunch AppId=123', 'args': '',
         'etime': '01:00:00', 'cpu_seconds': 200.0},
        {'pid': 104, 'comm': 'python3',
         'cmdline': '/home/kid/scripts/run.py', 'args': '',
         'etime': '01:00:00', 'cpu_seconds': 10.0},
        {'pid': 105, 'comm': 'flatpak',
         'cmdline': '/usr/bin/flatpak run org.mozilla.firefox',
         'args': '', 'etime': '01:00:00', 'cpu_seconds': 3.0},
    ]


class TestParseDesktopName(unittest.TestCase):
    def test_name_captured(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'app.desktop')
            with open(p, 'w') as f:
                f.write(FIREFOX_DESKTOP)
            keys, cat, name = parse_desktop_file(p)
            self.assertEqual(keys, ['firefox'])
            self.assertEqual(cat, 'browsers')
            self.assertEqual(name, 'Firefox Web Browser')

    def test_uncategorized_entry_still_yields_name(self):
        """Entries with Exec but no mapped Categories= keep keys + name
        (display names are not limited to budgeted categories)."""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'helper.desktop')
            with open(p, 'w') as f:
                f.write(HELPER_DESKTOP)
            keys, cat, name = parse_desktop_file(p)
            self.assertEqual(keys, ['helper'])
            self.assertIsNone(cat)
            self.assertEqual(name, 'My Helper')

    def test_no_exec_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'noexec.desktop')
            with open(p, 'w') as f:
                f.write('[Desktop Entry]\nName=NoExec\n'
                        'Categories=Game;\n')
            self.assertIsNone(parse_desktop_file(p))


class TestDesktopNames(unittest.TestCase):
    def test_build_names_covers_uncategorized(self):
        with tempfile.TemporaryDirectory() as d:
            write_desktop_files(d)
            with mock.patch.object(budgets, 'desktop_dirs_for',
                                    return_value=[d]):
                names = build_desktop_names()
                index = build_desktop_index()
        self.assertEqual(names.get('firefox'), 'Firefox Web Browser')
        # Uncategorized helper: named but not categorized.
        self.assertEqual(names.get('helper'), 'My Helper')
        self.assertEqual(index.get('firefox'), 'browsers')
        self.assertNotIn('helper', index)


class TestIsUserApp(unittest.TestCase):
    def test_argv0_basename_in_index(self):
        proc = {'comm': 'firefox',
                'cmdline': '/usr/lib64/firefox/firefox --new-window'}
        self.assertTrue(is_user_app(proc, {'firefox': 'browsers'}))

    def test_comm_in_index(self):
        proc = {'comm': 'firefox', 'cmdline': '/usr/bin/wrapper --x'}
        self.assertTrue(is_user_app(proc, {'firefox': 'browsers'}))

    def test_override_whole_word_steam_chain(self):
        proc = {'comm': 'reaper',
                'cmdline': '/opt/steam/reaper SteamLaunch AppId=123'}
        self.assertTrue(is_user_app(proc, {}, home='/home/kid'))

    def test_override_word_boundary(self):
        """'edge' must not match 'knowledge' (same guard as categorize)."""
        proc = {'comm': 'python3',
                'cmdline': '/usr/bin/python3 /opt/knowledge/app.py'}
        self.assertFalse(is_user_app(proc, {}, home='/home/kid'))

    def test_home_rule(self):
        """A ~/ script launched via python is user-started even with an
        empty app index and no override match."""
        proc = {'comm': 'python3',
                'cmdline': '/home/kid/scripts/run.py'}
        self.assertTrue(is_user_app(proc, {}, home='/home/kid'))
        # Outside home: not a user app.
        proc2 = {'comm': 'python3', 'cmdline': '/usr/bin/tool --x'}
        self.assertFalse(is_user_app(proc2, {}, home='/home/kid'))
        # Prefix trap: /home/kid2 is not /home/kid/.
        proc3 = {'comm': 'mytool', 'cmdline': '/home/kid2/mytool'}
        self.assertFalse(is_user_app(proc3, {}, home='/home/kid'))

    def test_flatpak_appid_rule(self):
        proc = {'comm': 'bwrap',
                'cmdline': '/usr/bin/flatpak run org.gimp.GIMP'}
        self.assertTrue(is_user_app(proc, {'gimp': 'other'}))
        self.assertFalse(is_user_app(proc, {}))

    def test_plumbing_is_not_user_app(self):
        proc = {'comm': 'pipewire', 'cmdline': '/usr/bin/pipewire'}
        self.assertFalse(is_user_app(proc, {'firefox': 'browsers'},
                                     home='/home/kid'))

    def test_uncategorized_installed_app_is_user_app(self):
        """Installed but uncategorized apps (terminals, editors) are
        user-started: the mark index must cover every .desktop Exec
        entry, so get_user_live passes build_desktop_names (not the
        categorized index) to mark_user_apps."""
        proc = {'comm': 'helper', 'cmdline': '/opt/helper/helper --serve'}
        self.assertTrue(is_user_app(proc, {'helper': 'My Helper'},
                                    home='/home/kid'))
        # ...but the categorized index alone would wrongly hide it.
        self.assertFalse(is_user_app(proc, {'firefox': 'browsers'},
                                     home='/home/kid'))


class TestMarkAndAnnotate(unittest.TestCase):
    def test_mark_sets_flags(self):
        procs = witness_procs()
        mark_user_apps(procs, {'firefox': 'browsers'}, home='/home/kid')
        by_comm = {p['comm']: p for p in procs}
        self.assertFalse(by_comm['pipewire']['user_app'])
        self.assertTrue(by_comm['firefox']['user_app'])
        self.assertFalse(by_comm['mydaemon']['user_app'])
        self.assertTrue(by_comm['reaper']['user_app'])
        self.assertTrue(by_comm['python3']['user_app'])
        self.assertTrue(by_comm['flatpak']['user_app'])

    def test_annotate_friendly_and_fallback(self):
        procs = witness_procs()
        annotate_display_names(procs, {'firefox': 'Firefox Web Browser'})
        by_comm = {p['comm']: p for p in procs}
        self.assertEqual(by_comm['firefox']['display_name'],
                         'Firefox Web Browser')
        # No known name -> comm fallback.
        self.assertEqual(by_comm['mydaemon']['display_name'], 'mydaemon')


class TestAutoHidePipeline(unittest.TestCase):
    def _marked(self):
        procs = witness_procs()
        mark_user_apps(procs, {'firefox': 'browsers'}, home='/home/kid')
        annotate_display_names(procs, {'firefox': 'Firefox Web Browser'})
        return procs

    def test_pipewire_hidden_firefox_visible(self):
        visible, hidden = apply_filter(self._marked(), 'kid', prefs={})
        vcomms = {p['comm'] for p in visible}
        hcomms = {p['comm'] for p in hidden}
        self.assertIn('pipewire', hcomms)
        self.assertNotIn('pipewire', vcomms)
        self.assertIn('firefox', vcomms)
        ff = next(p for p in visible if p['comm'] == 'firefox')
        self.assertEqual(ff['display_name'], 'Firefox Web Browser')

    def test_unlisted_daemon_auto_hidden(self):
        """mydaemon is in no denylist — it hides purely via user_app."""
        self.assertFalse(budgets.is_default_hidden(
            {'comm': 'mydaemon', 'cmdline': '/usr/bin/mydaemon --fork'}))
        visible, hidden = apply_filter(self._marked(), 'kid', prefs={})
        self.assertIn('mydaemon', {p['comm'] for p in hidden})

    def test_steam_visible_via_override(self):
        visible, _ = apply_filter(self._marked(), 'kid', prefs={})
        self.assertIn('reaper', {p['comm'] for p in visible})

    def test_home_script_visible_via_home_rule(self):
        visible, _ = apply_filter(self._marked(), 'kid', prefs={})
        self.assertIn('python3', {p['comm'] for p in visible})

    def test_shown_comm_reshows_auto_hidden(self):
        prefs = {'kid': {'hidden_comm': [], 'shown_comm': ['mydaemon']}}
        visible, _ = apply_filter(self._marked(), 'kid', prefs=prefs)
        self.assertIn('mydaemon', {p['comm'] for p in visible})

    def test_hidden_comm_still_hides(self):
        prefs = {'kid': {'hidden_comm': ['firefox'], 'shown_comm': []}}
        visible, hidden = apply_filter(self._marked(), 'kid', prefs=prefs)
        self.assertIn('firefox', {p['comm'] for p in hidden})
        self.assertNotIn('firefox', {p['comm'] for p in visible})

    def test_unmarked_snapshot_behaves_as_before(self):
        """Absent user_app flags default to visible (existing callers
        such as the freezer match path pass unmarked procs)."""
        procs = [{'pid': 1, 'comm': 'steam', 'cmdline': '/x/steam'}]
        visible, hidden = apply_filter(procs, 'kid', prefs={})
        self.assertEqual([p['comm'] for p in visible], ['steam'])
        self.assertEqual(hidden, [])


class TestGroupDisplayName(unittest.TestCase):
    def test_group_takes_first_member_name(self):
        procs = [
            {'pid': 2, 'comm': 'firefox', 'etime': '01:00:00',
             'display_name': 'Firefox Web Browser'},
            {'pid': 1, 'comm': 'firefox', 'etime': '59:00',
             'display_name': 'Firefox Web Browser'},
        ]
        groups = group_by_comm(procs)
        self.assertEqual(groups[0]['display_name'], 'Firefox Web Browser')

    def test_group_falls_back_to_comm(self):
        procs = [{'pid': 1, 'comm': 'mydaemon', 'etime': '01:00:00'}]
        groups = group_by_comm(procs)
        self.assertEqual(groups[0]['display_name'], 'mydaemon')


@unittest.skipUnless(HAS_FLASK, 'Flask not installed')
class TestAutoHideLive(unittest.TestCase):
    def _admin_patches(self):
        import app
        store = {'users': {'boss': {'hash': 'x', 'must_change': False,
                                    'locked': False}}}
        sess = {'username': 'boss', 'csrf': 'test-csrf', 'created': 0,
                'last_seen': 0}
        return (
            mock.patch.object(app.auth, 'get_session',
                              return_value=dict(sess)),
            mock.patch.object(app.auth, 'load_store', return_value=store),
            mock.patch.object(app.auth, 'is_wheel_member',
                              return_value=True),
            mock.patch.object(app.auth, 'is_system_user',
                              return_value=True),
        )

    def _live_patches(self):
        import app
        procs = witness_procs()
        pw = mock.Mock()
        pw.pw_dir = '/home/kid'
        return (
            mock.patch.object(app, 'get_human_users',
                              return_value=[{'username': 'kid', 'uid': 1001,
                                             'home': '/home/kid',
                                             'shell': '/bin/bash'}]),
            mock.patch.object(app, 'get_lock_status',
                              return_value=(False, 'Unlocked')),
            mock.patch.object(app, 'load_bedtime', return_value={}),
            mock.patch.object(app.budgets, 'list_user_processes',
                              return_value=procs),
            mock.patch.object(app.budgets, 'load_prefs', return_value={}),
            mock.patch.object(app.budgets, 'build_desktop_index',
                              return_value={'firefox': 'browsers'}),
            mock.patch.object(app.budgets, 'build_desktop_names',
                              return_value={'firefox':
                                            'Firefox Web Browser'}),
            mock.patch.object(app.budgets, 'budget_status', return_value={}),
            mock.patch('pwd.getpwnam', return_value=pw),
        )

    def test_get_user_live_auto_hides_and_names(self):
        import app
        patches = self._live_patches()
        for p in patches:
            p.start()
        try:
            data = app.get_user_live('kid')
        finally:
            for p in patches:
                p.stop()
        visible_comms = {p['comm'] for p in data['visible']}
        hidden_comms = {g['comm'] for g in data['hidden']}
        self.assertIn('pipewire', hidden_comms)
        self.assertIn('mydaemon', hidden_comms)
        self.assertIn('firefox', visible_comms)
        ff = next(p for p in data['visible'] if p['comm'] == 'firefox')
        self.assertEqual(ff['display_name'], 'Firefox Web Browser')
        self.assertIn('reaper', visible_comms)    # override rule
        self.assertIn('python3', visible_comms)   # home rule

    def test_api_live_payload_display_names(self):
        import app
        live = {
            'user': {'username': 'kid', 'uid': 1001, 'home': '/home/kid',
                     'shell': '/bin/bash', 'locked': False,
                     'status_detail': 'Unlocked', 'bedtime': {},
                     'in_bedtime': False,
                     'bonus': {'active': False, 'until': None,
                               'remaining_minutes': None,
                               'granted_by': None, 'last_minutes': None}},
            'processes': [],
            'visible': [],
            'hidden': [],
            'groups': {c: [] for c in budgets.CATEGORIES},
            'budget_state': {},
        }
        live['groups']['browsers'] = budgets.group_by_comm([
            {'pid': 101, 'comm': 'firefox', 'etime': '01:00:00',
             'elapsed': '1h', 'category': 'browsers',
             'display_name': 'Firefox Web Browser'},
            {'pid': 102, 'comm': 'mydaemon', 'etime': '59:00',
             'elapsed': '59m', 'category': 'browsers'},
        ])
        with mock.patch.object(app, 'get_user_live',
                               return_value=live), \
                self._admin_patches()[0], self._admin_patches()[1], \
                self._admin_patches()[2], self._admin_patches()[3]:
            client = app.app.test_client()
            client.set_cookie('pc_sid', 'fake-test-session')
            resp = client.get('/api/user/kid/live')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        by_comm = {g['comm']: g for g in payload['groups']['browsers']}
        # Friendly name on the group (first member's) and the member.
        self.assertEqual(by_comm['firefox']['display_name'],
                         'Firefox Web Browser')
        self.assertEqual(by_comm['firefox']['members'][0]['display_name'],
                         'Firefox Web Browser')
        # Comm fallback where no friendly name is known.
        self.assertEqual(by_comm['mydaemon']['display_name'], 'mydaemon')
        self.assertEqual(by_comm['mydaemon']['members'][0]['display_name'],
                         'mydaemon')

    def test_user_page_renders_display_names(self):
        import app
        live = {
            'user': {'username': 'kid', 'uid': 1001, 'home': '/home/kid',
                     'shell': '/bin/bash', 'locked': False,
                     'status_detail': 'Unlocked', 'bedtime': {},
                     'in_bedtime': False,
                     'bonus': {'active': False, 'until': None,
                               'remaining_minutes': None,
                               'granted_by': None, 'last_minutes': None}},
            'processes': [],
            'visible': [{'comm': 'firefox'}],
            'hidden': [],
            'groups': {c: [] for c in budgets.CATEGORIES},
            'budget_state': {c: {'minutes': 0, 'used_minutes': 0,
                                 'remaining_minutes': None,
                                 'paused': False, 'exhausted': False}
                             for c in budgets.CATEGORIES},
        }
        live['groups']['browsers'] = budgets.group_by_comm([
            {'pid': 101, 'comm': 'firefox', 'etime': '01:00:00',
             'elapsed': '1h', 'category': 'browsers',
             'display_name': 'Firefox Web Browser'},
            {'pid': 102, 'comm': 'mydaemon', 'etime': '59:00',
             'elapsed': '59m', 'category': 'browsers'},
        ])
        with mock.patch.object(app, 'get_user_live',
                               return_value=live), \
                self._admin_patches()[0], self._admin_patches()[1], \
                self._admin_patches()[2], self._admin_patches()[3]:
            client = app.app.test_client()
            client.set_cookie('pc_sid', 'fake-test-session')
            resp = client.get('/user/kid')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # Friendly name shown, comm fallback where unknown.
        self.assertIn('Firefox Web Browser', html)
        self.assertIn('<span class="process-name">mydaemon</span>', html)
        # Identity plumbing stays on comm: expand keys, aria targets,
        # Hide buttons.
        self.assertIn('data-group-comm="firefox"', html)
        self.assertIn('detail-browsers-firefox', html)
        self.assertNotIn('detail-browsers-Firefox-Web-Browser', html)
        self.assertIn('data-comm="firefox" onclick="hideGroup(this)">'
                      'Hide</button>', html)


if __name__ == '__main__':
    unittest.main()
