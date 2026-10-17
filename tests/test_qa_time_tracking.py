#!/usr/bin/env python3
"""QA reproduction tests for the live-box time-tracking complaints.

Owner report (2026-10-17):
  1. Minecraft (and similar games) never show up / never accrue per-category
     usage.
  2. Category budgets never pause/freeze once exhausted.

These are CHARACTERIZATION tests: they document the CURRENT (wrong) behavior
as passing assertions, plus one ``expectedFailure`` per confirmed bug showing
the DESIRED behavior for the follow-up implementer. Stdlib only, no live
system access (no PAM/sudo/cgroup writes; scratch under .scratch/ via
TMPDIR). See TIME_TRACKING_QA.md for the diagnosis.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import budgets
import focus_feed
from budgets import (
    account_and_enforce,
    annotate_display_names,
    apply_filter,
    blank_entry,
    budget_status,
    categorize,
    is_user_app,
)

# ---------------------------------------------------------------------------
# Synthetic Minecraft process shapes (three real-world install variants).
# comm is what `ps -o comm` shows for the JVM; args/cmdline is the full
# command line `list_user_processes` captures.
# ---------------------------------------------------------------------------

VANILLA_JAVA = {
    # Official launcher: uses its own runtime under ~/.minecraft/runtime —
    # argv0 IS under the user's home.
    'pid': 90001, 'comm': 'java',
    'cmdline': ('/home/kid/.minecraft/runtime/java-runtime-glibc2.17-x64/'
                'bin/java -Xmx2G -Djava.library.path=/home/kid/.minecraft/'
                'versions/1.21.4-natives -cp /home/kid/.minecraft/assets/'
                'virtual/legacy:/home/kid/.minecraft/versions/1.21.4/'
                '1.21.4.jar net.minecraft.client.main.Main --username kid '
                '--gameDir /home/kid/.minecraft --assetsIndex 12'),
    'args': '', 'etime': '00:42:00', 'cpu_seconds': 420.0,
}

SYSTEM_JAVA = {
    # PrismLauncher / MultiMC with the system JRE: argv0 is NOT under home.
    'pid': 90002, 'comm': 'java',
    'cmdline': ('/usr/bin/java -Xmx1G -cp /home/kid/.local/share/'
                'PrismLauncher/instances/1.21/libraries/.../client.jar '
                'net.minecraft.client.main.Main --gameDir /home/kid/'
                '.local/share/PrismLauncher/instances/1.21/minecraft'),
    'args': '', 'etime': '00:42:00', 'cpu_seconds': 420.0,
}

PRISM_LAUNCHER = {
    # The launcher GUI itself — has a real .desktop entry (Exec=prismlauncher).
    'pid': 90003, 'comm': 'prismlaunch',
    'cmdline': '/usr/bin/prismlauncher',
    'args': '', 'etime': '00:43:00', 'cpu_seconds': 65.0,
}

STEAM = {
    'pid': 90004, 'comm': 'steam',
    'cmdline': '/home/kid/.local/share/Steam/ubuntu12_32/steam -silent',
    'args': '', 'etime': '01:00:00', 'cpu_seconds': 100.0,
}

FIREFOX = {
    'pid': 90005, 'comm': 'firefox',
    'cmdline': '/usr/lib64/firefox/firefox --new-window',
    'args': '', 'etime': '01:00:00', 'cpu_seconds': 90.0,
}

# What build_desktop_index() yields for the launcher .desktop files on the
# live box (Categories=Game -> games; keys are Exec= basenames). Crucially
# there is NO 'java' key: the game itself is not what the .desktop starts.
LIVE_LIKE_INDEX = {
    'minecraft-launcher': 'games',
    'prismlauncher': 'games',
    'firefox': 'browsers',
}
# Names index for display annotation: every .desktop entry with a Name=.
DESKTOP_NAMES = {
    'minecraft-launcher': 'Minecraft Launcher',
    'prismlauncher': 'Prism Launcher',
    'firefox': 'Firefox',
}


def _pro(*procs, cpu=0.0):
    """Fresh copies with an absolute cpu_seconds reading."""
    out = []
    for p in procs:
        q = dict(p)
        q['cpu_seconds'] = cpu
        out.append(q)
    return out


class TestMinecraftAttribution(unittest.TestCase):
    """H1: a Minecraft JVM matches no rule -> miscategorized or hidden."""

    def test_steam_control_categorizes_games(self):
        self.assertEqual(categorize(dict(STEAM), {}), 'games')
        self.assertEqual(categorize(dict(FIREFOX), {}), 'browsers')

    def test_java_matches_no_games_keyword(self):
        # CATEGORY_OVERRIDES['games'] has no 'minecraft'/'java' entry, so the
        # literal 'net.minecraft.client.main.Main' in the cmdline matches
        # nothing: whole-word regexes only know steam/proton/lutris/heroic/...
        for var in (VANILLA_JAVA, SYSTEM_JAVA):
            with self.subTest(var=var['pid']):
                self.assertEqual(categorize(dict(var), index={}), 'other')

    def test_launcher_desktop_entry_does_not_help_the_jvm(self):
        # Even with the real .desktop index on the box, lookup is keyed by
        # argv0-basename ('java'), which no launcher .desktop produces.
        self.assertEqual(categorize(dict(VANILLA_JAVA),
                                    index=LIVE_LIKE_INDEX), 'other')
        self.assertEqual(categorize(dict(SYSTEM_JAVA),
                                    index=LIVE_LIKE_INDEX), 'other')

    def test_system_jre_java_is_not_a_user_app_and_is_hidden(self):
        # Prism/MultiMC with the system JRE: argv0 is not under home, 'java'
        # is in no app index, and no keyword matches -> auto-hidden in the
        # /user page. This is the "Minecraft never shows up at all" symptom.
        marked = dict(SYSTEM_JAVA)
        self.assertFalse(is_user_app(marked, app_index=DESKTOP_NAMES,
                                     home='/home/kid'))
        marked['user_app'] = False
        visible, hidden = apply_filter([marked], 'kid', prefs={})
        self.assertEqual(visible, [])
        self.assertEqual([p['pid'] for p in hidden], [SYSTEM_JAVA['pid']])

    def test_vanilla_launcher_jvm_is_visible_but_unnamed_other(self):
        # Vanilla launcher java runs from ~/.minecraft/runtime -> the home
        # rule makes it a "user app" (visible), but it lands in category
        # 'other' and is displayed as bare 'java' — no parent would call
        # that "tracked".
        self.assertTrue(is_user_app(dict(VANILLA_JAVA),
                                    app_index=DESKTOP_NAMES,
                                    home='/home/kid'))
        procs = [dict(VANILLA_JAVA)]
        annotate_display_names(procs, DESKTOP_NAMES)
        groups = budgets.group_by_category(procs, index=LIVE_LIKE_INDEX)
        self.assertEqual(groups['games'], [])
        self.assertEqual([p['pid'] for p in groups['other']],
                         [VANILLA_JAVA['pid']])
        self.assertEqual(procs[0]['display_name'], 'java')

    def test_window_class_minecraft_maps_to_other_too(self):
        # focus_feed mirrors categorize for the focused window: the
        # 'Minecraft' WM_CLASS also maps to 'other', not 'games'.
        self.assertEqual(
            focus_feed.map_window_to_category('Minecraft', None,
                                              index=LIVE_LIKE_INDEX),
            'other')

    def test_parse_desktop_file_keys_on_launcher_binary_not_the_game(self):
        # A faithful Minecraft .desktop file yields the LAUNCHER basename,
        # never 'java' — proving the .desktop path cannot attribute the JVM.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'com.mojang.MinecraftLauncher.desktop')
            with open(path, 'w') as f:
                f.write('[Desktop Entry]\nName=Minecraft Launcher\n'
                        'Exec=/usr/bin/minecraft-launcher %U\n'
                        'Type=Application\nCategories=Game;Joystick;\n')
            parsed = budgets.parse_desktop_file(path)
            self.assertEqual(parsed[0], ['minecraft-launcher'])
            self.assertEqual(parsed[1], 'games')
            self.assertNotIn('java', parsed[0])

    @unittest.expectedFailure
    def test_DESIRE_minecraft_jvm_categorizes_as_games(self):
        """Desired behavior (fix target): both JVM shapes -> 'games'."""
        self.assertEqual(categorize(dict(VANILLA_JAVA),
                                    index=LIVE_LIKE_INDEX), 'games')
        self.assertEqual(categorize(dict(SYSTEM_JAVA),
                                    index=LIVE_LIKE_INDEX), 'games')


class TestBillingNeverReachesTheBudget(unittest.TestCase):
    """Symptom 1 + start of symptom 2: a games limit + Minecraft = 0 usage."""

    def _entry(self, limits=None):
        e = blank_entry()
        e['limits'] = limits or {'games': 60}
        e['day'] = '2026-10-17'
        return e

    def _two_active_ticks(self, entry, procs, index):
        now = datetime(2026, 10, 17, 15, 0)
        base = _pro(*procs, cpu=100.0)
        account_and_enforce('kid', entry, base, None, now,
                            prefs={}, index=index, act=False)
        hot = _pro(*procs, cpu=130.0)  # 30 CPU-s/tick: clearly "playing"
        return account_and_enforce('kid', entry, hot, None,
                                   now + timedelta(seconds=60),
                                   prefs={}, index=index, act=False)

    def test_minecraft_jvm_burns_no_games_minutes(self):
        entry = self._entry()
        self._two_active_ticks(entry, [dict(VANILLA_JAVA)],
                               index=LIVE_LIKE_INDEX)
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)
        # ...and no other category either: 'other' has no limit configured,
        # so the wall-clock the JVM clearly deserves is billed nowhere.
        # (_enforce_target touches used['games']=0.0 when evaluating it —
        # every value stays 0.)
        self.assertTrue(all(v == 0.0 for v in entry['used_seconds'].values()))

    def test_steam_control_burns_games_minutes(self):
        entry = self._entry()
        self._two_active_ticks(entry, [dict(STEAM)], index={})
        self.assertAlmostEqual(entry['used_seconds']['games'], 60.0)

    def test_minimized_launcher_while_game_running_burns_nothing(self):
        # Only the launcher GUI counts as 'games' (via the .desktop index),
        # but it sits idle in the tray: CPU delta < ACTIVE_THRESHOLD_SECONDS,
        # so the activity gate closes and the category never burns.
        entry = self._entry()
        now = datetime(2026, 10, 17, 15, 0)
        procs = [dict(VANILLA_JAVA), dict(PRISM_LAUNCHER)]
        base = _pro(*[dict(VANILLA_JAVA), dict(PRISM_LAUNCHER)], cpu=100.0)
        account_and_enforce('kid', entry, base, None, now,
                            prefs={}, index=LIVE_LIKE_INDEX, act=False)
        hot_java = [dict(VANILLA_JAVA), dict(PRISM_LAUNCHER)]
        hot_java[0]['cpu_seconds'] = 130.0   # the GAME is hot
        hot_java[1]['cpu_seconds'] = 100.0   # the launcher is idle
        account_and_enforce('kid', entry, hot_java, None,
                            now + timedelta(seconds=60),
                            prefs={}, index=LIVE_LIKE_INDEX, act=False)
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)
        status = budget_status('kid', {'kid': entry})
        self.assertFalse(status['games']['exhausted'])
        self.assertFalse(status['games']['paused'])

    @unittest.expectedFailure
    def test_DESIRE_minecraft_burns_games_minutes(self):
        entry = self._entry()
        self._two_active_ticks(entry, [dict(VANILLA_JAVA)],
                               index=LIVE_LIKE_INDEX)
        self.assertAlmostEqual(entry['used_seconds']['games'], 60.0)


class TestTickUiParityGap(unittest.TestCase):
    """The tick never receives a .desktop index; the UI does."""

    def test_tick_with_no_index_miscategorizes_desktop_named_apps(self):
        # The timer (`enforce_bedtime_timer.py`) and the app thread both call
        # tick_budgets/account_and_enforce WITHOUT index= -> index=None.
        # A prisma launcher that the UI groups under Games (index present)
        # is billed to 'other' by the tick.
        entry = self._blank()
        now = datetime(2026, 10, 17, 15, 0)
        base = _pro(PRISM_LAUNCHER, cpu=100.0)
        account_and_enforce('kid', entry, base, None, now,
                            prefs={}, index=None, act=False)
        hot = _pro(PRISM_LAUNCHER, cpu=130.0)
        account_and_enforce('kid', entry, hot, None,
                            now + timedelta(seconds=60),
                            prefs={}, index=None, act=False)
        self.assertEqual(entry['used_seconds'].get('games', 0.0), 0.0)

        # Same procs the UI sees: with an index, the launcher is 'games'.
        self.assertEqual(categorize(dict(PRISM_LAUNCHER),
                                    index=LIVE_LIKE_INDEX), 'games')

    def _blank(self):
        e = blank_entry()
        e['limits'] = {'games': 60}
        e['day'] = '2026-10-17'
        return e

    @unittest.expectedFailure
    def test_DESIRE_tick_uses_the_desktop_index_like_the_ui(self):
        e = self._blank()
        now = datetime(2026, 10, 17, 15, 0)
        account_and_enforce('kid', e, _pro(PRISM_LAUNCHER, cpu=100.0),
                            None, now, prefs={}, act=False)  # default index
        account_and_enforce('kid', e, _pro(PRISM_LAUNCHER, cpu=130.0),
                            None, now + timedelta(seconds=60),
                            prefs={}, act=False)
        self.assertAlmostEqual(e['used_seconds']['games'], 60.0)


class TestEnforcementGateFailures(unittest.TestCase):
    """H3: enumerate what must hold for exhaustion -> freeze on the live box.

    Uses the real current user + real own PID so the genuine
    ``verify_freeze_safety`` os.stat/pwd checks run; only the privileged
    cgroup primitives are faked.
    """

    USER = None  # resolved in setUp to the container's real user

    def setUp(self):
        import pwd
        self.USER = pwd.getpwuid(os.getuid()).pw_name
        self.migrated = []

    def _entry(self):
        e = blank_entry()
        e['limits'] = {'games': 0.05}  # 3 seconds: exhausts on first bill
        e['day'] = '2026-10-17'
        e['last_tick'] = None
        return e

    def _steam(self):
        q = dict(STEAM)
        q['pid'] = os.getpid()   # real pid, owned by the test user
        return q

    def _java(self):
        q = dict(VANILLA_JAVA)
        q['pid'] = 90090001      # fake: never enters verify (it's 'other')
        return q

    def _ticks_to_exhaust(self, entry, procs, cgroup_state,
                          ensure_ok=(True, '')):
        """Run a baseline tick then an exhausting tick, both act=True.

        Only the privileged cgroup primitives are faked; the real
        verify_freeze_safety runs against live pids we own.
        """
        now = datetime(2026, 10, 17, 15, 0)
        base = [dict(p) for p in procs]
        for p in base:
            p['cpu_seconds'] = 100.0
        hot = [dict(p) for p in procs]
        for p in hot:
            p['cpu_seconds'] = 130.0  # 30 CPU-s/tick: clearly active
        with mock.patch.object(budgets, 'is_admin_protected',
                               return_value=False), \
             mock.patch.object(budgets, 'ensure_cgroup',
                               return_value=ensure_ok), \
             mock.patch.object(budgets, 'migrate_pid',
                               side_effect=self._record_migrate), \
             mock.patch.object(budgets, 'cgroup_members',
                               side_effect=lambda u, c:
                                   list(cgroup_state)), \
             mock.patch.object(budgets, 'set_frozen',
                               return_value=(True, '')):
            account_and_enforce(self.USER, entry, base, None, now,
                                prefs={}, index={}, act=True)
            self.migrated = []  # keep only the exhausting tick's moves
            return account_and_enforce(self.USER, entry, hot, None,
                                       now + timedelta(seconds=60),
                                       prefs={}, index={}, act=True)

    def _record_migrate(self, username, category, pid):
        self.migrated.append((category, pid))
        return True, ''

    def test_sudoers_or_perms_failure_freezes_silently(self):
        # ensure_cgroup fails (missing/stale sudoers, EPERM...): the tick
        # records a note and marks NOTHING. paused stays False.
        entry = self._entry()
        steam = self._steam()
        summary = self._ticks_to_exhaust(
            entry, [steam], [], ensure_ok=(False, "sudo: a password is "
                                                   "required"))
        self.assertFalse(entry['paused'].get('games'))
        self.assertIn('ensure cgroup', summary['note'])
        self.assertEqual(summary['froze'], [])

    def test_tick_summaries_are_dropped_by_the_timer_path(self):
        # Characterization of the diagnosis gap: neither the timer script nor
        # the app thread prints the per-user summaries, so 'ensure cgroup' /
        # 'freeze aborted' / 're-freeze failed' notes never reach the journal.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, 'enforce_bedtime_timer.py')) as f:
            timer_src = f.read()
        self.assertIn('tick_budgets', timer_src)
        self.assertNotIn('summary', timer_src)
        # app.py: bedtime_loop() body only — from `def bedtime_loop` to the
        # next top-level def (no assignment/print of tick_budgets' return).
        with open(os.path.join(here, 'app.py')) as f:
            lines = f.read().splitlines()
        start = next(i for i, ln in enumerate(lines)
                     if ln.startswith('def bedtime_loop'))
        body = []
        for ln in lines[start + 1:]:
            if ln.startswith('def ') or ln.startswith('# ---'):
                break
            body.append(ln)
        loop_src = '\n'.join(body)
        self.assertIn('tick_budgets', loop_src)
        self.assertNotIn('summary', loop_src)

    def test_stale_cgroup_member_permanently_aborts_freeze(self):
        # Foreign/stale PID still inside parental-<user>-games (e.g. the
        # owner's pre-existing experiment groups): verify_freeze_safety
        # aborts EVERY tick. paused never gets set -> exhausted category
        # accrues, UI shows no pause, forever.
        entry = self._entry()
        steam = self._steam()
        stale = os.getppid()  # a live pid that is NOT a steam member
        self.assertNotEqual(stale, steam['pid'])
        summary = self._ticks_to_exhaust(entry, [steam], [stale])
        self.assertFalse(entry['paused'].get('games'))
        self.assertIn('freeze aborted', summary['note'])

    def test_exhausted_games_pauses_but_minecraft_keeps_playing(self):
        # THE unified symptom-2 story: games DOES exhaust (via tracked
        # processes), the freeze fires — but only over the games-classified
        # members. The running Minecraft JVM is 'other', never migrated, so
        # the parent sees "Games paused" while the child keeps playing.
        entry = self._entry()
        steam, java = self._steam(), self._java()
        cgroup_state = [steam['pid']]
        summary = self._ticks_to_exhaust(entry, [steam, java], cgroup_state)
        self.assertTrue(entry['paused'].get('games'))
        self.assertEqual(self.migrated, [('games', steam['pid'])])
        java_pids = [pid for _c, pid in self.migrated]
        self.assertNotIn(java['pid'], java_pids)
        self.assertIn('games', [tag for tag in summary['froze']])

    @unittest.expectedFailure
    def test_DESIRE_minecraft_frozen_with_the_games_budget(self):
        entry = self._entry()
        steam, java = self._steam(), self._java()
        cgroup_state = [steam['pid']]
        self._ticks_to_exhaust(entry, [steam, java], cgroup_state)
        migrated_pids = {pid for _c, pid in self.migrated}
        self.assertIn(java['pid'], migrated_pids)


class TestFocusEngineGate(unittest.TestCase):
    """Even with the focus engine opted in, Minecraft windows bill nothing."""

    def _rows(self):
        return [
            {'type': 'focus', 'seq': 1, 'kind': 'app', 'bt': 100.0,
             'wm_class': 'Minecraft', 'app_id': 'minecraft', 'since': 0.0},
            {'type': 'focus', 'seq': 2, 'kind': 'app', 'bt': 160.0,
             'wm_class': 'Minecraft', 'app_id': 'minecraft', 'since': 60.0},
        ]

    def test_minecraft_window_not_billable_games_empty(self):
        agg = focus_feed.aggregate_focus(
            self._rows(), (100.0, 160.0),
            procs=[dict(VANILLA_JAVA)], limits={'games': 60},
            username='kid', prefs={}, index=LIVE_LIKE_INDEX,
            now_bt=160.0)
        self.assertTrue(agg['billable'])
        self.assertEqual(agg['seconds']['games'], 0.0)
        self.assertGreaterEqual(agg['stats']['not_billable'], 1)

    def test_firefox_window_control_is_billable(self):
        rows = self._rows()
        for r in rows:
            r['wm_class'], r['app_id'] = 'firefox', 'firefox'
        agg = focus_feed.aggregate_focus(
            rows, (100.0, 160.0),
            procs=[dict(FIREFOX)], limits={'browsers': 60},
            username='kid', prefs={}, index={}, now_bt=160.0)
        self.assertEqual(agg['seconds']['browsers'], 60.0)

    @unittest.expectedFailure
    def test_DESIRE_minecraft_window_bills_games(self):
        agg = focus_feed.aggregate_focus(
            self._rows(), (100.0, 160.0),
            procs=[dict(VANILLA_JAVA)], limits={'games': 60},
            username='kid', prefs={}, index=LIVE_LIKE_INDEX,
            now_bt=160.0)
        self.assertEqual(agg['seconds']['games'], 60.0)


if __name__ == '__main__':
    unittest.main()
