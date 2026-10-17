#!/usr/bin/env python3
"""QA reproduction tests for the live-box time-tracking complaints.

Owner report (2026-10-17):
  1. Minecraft (and similar games) never show up / never accrue per-category
     usage.
  2. Category budgets never pause/freeze once exhausted.

Originally written as CHARACTERIZATION tests (documenting the wrong
behavior as passing) plus one ``expectedFailure`` DESIRED test per confirmed
bug, with the desired behavior for the implementer (TIME_TRACKING_QA.md §7).

Status (2026-10-17): **FIX-1, FIX-2 and FIX-3 are implemented.** The DESIRED
tests were un-marked into regular asserts, and the characterization tests
that documented the bugs now document the fixed behavior. Two tests stay
characterization-only by design:

* ``test_parse_desktop_file_keys_on_launcher_binary_not_the_game`` — still
  true; the .desktop path cannot attribute the JVM, which is exactly why
  the cmdline keyword/token scan (FIX-1) exists.
* ``test_stale_cgroup_member_permanently_aborts_freeze`` — FIX-4 is
  DEFERRED by the owner (2026-10-17, TIME_TRACKING_QA.md §7), so the
  stale-member deadlock is still the live behavior; this test guards the
  safety property that FIX-4 would later have to preserve.

Stdlib only, no live system access (no PAM/sudo/cgroup writes; scratch
under .scratch/ via TMPDIR; audit writes redirected to a temp file).
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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

MULTIMC_JAVA = {
    # MultiMC spelling of the same shape: instance under a multimc dir.
    'pid': 90006, 'comm': 'java',
    'cmdline': ('/usr/bin/java -Xmx1G -cp /home/kid/.local/share/multimc/'
                'instances/1.20/libraries/foo.jar '
                'org.multimc.impl.Entry --workDir /home/kid/'
                '.local/share/multimc/instances/1.20'),
    'args': '', 'etime': '00:10:00', 'cpu_seconds': 50.0,
}

JETBRAINS_JAVA = {
    # The category-neutral control: an IDE on the same JVM must NOT be
    # attributed to games (FIX-1 keeps bare `java` neutral; attribution
    # comes from app identity in the args, and JetBrains' .desktop is
    # Categories=Development -> never in the categorized index).
    'pid': 90007, 'comm': 'java',
    'cmdline': ('/usr/bin/java -Deditable.java.test.console=true '
                '-jar /opt/JetBrains/idea-2026.2/lib/idea.jar'),
    'args': '', 'etime': '01:00:00', 'cpu_seconds': 120.0,
}

PRISM_LAUNCHER = {
    # The launcher GUI itself — has a real .desktop entry (Exec=prismlauncher).
    'pid': 90003, 'comm': 'prismlaunch',
    'cmdline': '/usr/bin/prismlauncher',
    'args': '', 'etime': '00:43:00', 'cpu_seconds': 65.0,
}

# A .desktop-categorized app with NO curated keyword: the ONLY signal is
# the index. Used to test FIX-2 (tick parity) and the generic word-token
# fallback without leaning on the Minecraft keywords.
DESKTOP_ONLY_GAME = {
    'pid': 90008, 'comm': 'sol',
    'cmdline': '/usr/bin/sol --theme cards',
    'args': '', 'etime': '00:30:00', 'cpu_seconds': 40.0,
}
SOL_INDEX = {'sol': 'games'}

# A JVM spawning THAT app: argv0/comm miss the index, but the args carry
# the app identity (path segments + class names) -> FIX-1.2 token scan.
DESKTOP_ONLY_JVM = {
    'pid': 90009, 'comm': 'java',
    'cmdline': ('/usr/bin/java -cp /home/kid/Games/Solitaire/lib/solitaire-'
                '1.0.jar com.solitaire.Main --workDir /home/kid/Games/'
                'Solitaire'),
    'args': '', 'etime': '00:30:00', 'cpu_seconds': 40.0,
}
SOLITAIRE_INDEX = {'solitaire': 'games'}

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
# live box (Categories=Game -> games; keys are Exec= basenames). There is
# still NO 'java' key: the game itself is not what the .desktop starts —
# that is why FIX-1 attributes via cmdline keywords/tokens instead.
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
    """H1 + FIX-1: launcher-spawned JVMs attribute by app identity."""

    def test_steam_control_categorizes_games(self):
        self.assertEqual(categorize(dict(STEAM), {}), 'games')
        self.assertEqual(categorize(dict(FIREFOX), {}), 'browsers')

    def test_minecraft_jvm_categorizes_as_games(self):
        """FIX-1 (was test_DESIRE_...): both JVM shapes -> 'games'.

        The keyword scan ('minecraft', 'prismlauncher', ...) matches on
        word boundaries inside `net.minecraft.client.main.Main`, the
        `--gameDir .../.minecraft` paths and the PrismLauncher instance
        dirs — no .desktop index needed for the JVM itself.
        """
        for var in (VANILLA_JAVA, SYSTEM_JAVA):
            with self.subTest(var=var['pid']):
                self.assertEqual(categorize(dict(var), index={}), 'games')
                self.assertEqual(categorize(dict(var),
                                             index=LIVE_LIKE_INDEX), 'games')

    def test_multimc_and_other_launcher_spellings(self):
        """FIX-1 policy spellings: multimc + adventure-launcher are games."""
        self.assertEqual(categorize(dict(MULTIMC_JAVA), index={}), 'games')
        self.assertEqual(
            categorize({'comm': 'adventure-la',
                        'cmdline': '/usr/bin/adventure-launcher'}, index={}),
            'games')
        self.assertEqual(categorize({'comm': 'multimc',
                                     'cmdline': '/usr/bin/multimc'},
                                    index={}), 'games')

    def test_jvm_attributed_by_cmdline_token_scan(self):
        """FIX-1.2 generic fallback: index-keyed app identity in the args.

        Not a curated keyword: `solitaire` only appears inside -cp paths,
        the class name com.solitaire.Main and --workDir, so only the
        word-token scan against the desktop index can attribute it.
        """
        self.assertEqual(categorize(dict(DESKTOP_ONLY_JVM),
                                    index=SOLITAIRE_INDEX), 'games')

    def test_bare_java_is_category_neutral(self):
        """FIX-1 guard: `java` itself must not pick up a category.

        JetBrains IDEs run on the same JVM and must stay out of 'games'
        (their .desktop is Development -> never enters the categorized
        index, and 'java' is in _GENERIC_INTERPRETERS so token lookups
        skip it too).
        """
        self.assertEqual(categorize(dict(JETBRAINS_JAVA), index={}), 'other')
        self.assertEqual(categorize(dict(JETBRAINS_JAVA),
                                     index=LIVE_LIKE_INDEX), 'other')
        self.assertEqual(categorize(dict(JETBRAINS_JAVA),
                                     index=SOLITAIRE_INDEX), 'other')
        self.assertFalse(is_user_app(dict(JETBRAINS_JAVA),
                                     app_index=DESKTOP_NAMES, home=None))

    def test_jvm_no_longer_needs_the_launcher_desktop_entry(self):
        """FIX-1: the JVM attributes on cmdline identity alone.

        (Was the characterization test_launcher_desktop_entry_does_not_
        help_the_jvm: the .desktop still keys on the launcher basename —
        see test_parse_desktop_file_... below — but the JVM no longer
        depends on it.)
        """
        self.assertEqual(categorize(dict(VANILLA_JAVA),
                                    index=LIVE_LIKE_INDEX), 'games')
        self.assertEqual(categorize(dict(SYSTEM_JAVA),
                                    index=LIVE_LIKE_INDEX), 'games')

    def test_system_jre_java_is_a_user_app_and_visible(self):
        """FIX-1/F2: the Prism system-JRE row is no longer auto-hidden."""
        marked = dict(SYSTEM_JAVA)
        self.assertTrue(is_user_app(marked, app_index=DESKTOP_NAMES,
                                    home='/home/kid'))
        marked['user_app'] = True
        visible, hidden = apply_filter([marked], 'kid', prefs={})
        self.assertEqual([p['pid'] for p in visible], [SYSTEM_JAVA['pid']])
        self.assertEqual(hidden, [])

    def test_vanilla_launcher_jvm_is_games_with_friendly_name(self):
        """FIX-1/F3+F4: vanilla JVM groups under Games, shown 'Minecraft'."""
        self.assertTrue(is_user_app(dict(VANILLA_JAVA),
                                    app_index=DESKTOP_NAMES,
                                    home='/home/kid'))
        procs = [dict(VANILLA_JAVA)]
        annotate_display_names(procs, DESKTOP_NAMES)
        groups = budgets.group_by_category(procs, index=LIVE_LIKE_INDEX)
        self.assertEqual([p['pid'] for p in groups['games']],
                         [VANILLA_JAVA['pid']])
        self.assertEqual(groups['other'], [])
        self.assertEqual(procs[0]['display_name'], 'Minecraft')

    def test_display_name_falls_back_per_launcher_flavour(self):
        """MultiMC JVMs read 'MultiMC', not 'java' (FIX-1.4); the launcher
        GUI itself still prefers its .desktop Name=."""
        procs = [dict(MULTIMC_JAVA), dict(PRISM_LAUNCHER),
                 dict(JETBRAINS_JAVA)]
        annotate_display_names(procs, DESKTOP_NAMES)
        self.assertEqual(procs[0]['display_name'], 'MultiMC')
        self.assertEqual(procs[1]['display_name'], 'Prism Launcher')
        # Neutral JVM with no known app identity: plain comm fallback.
        self.assertEqual(procs[2]['display_name'], 'java')

    def test_window_class_minecraft_maps_to_games(self):
        """FIX-1/F7: focus_feed's pseudo-proc picks up the keyword."""
        self.assertEqual(
            focus_feed.map_window_to_category('Minecraft', None,
                                              index=LIVE_LIKE_INDEX),
            'games')
        self.assertEqual(
            focus_feed.map_window_to_category('prismlauncher',
                                              'PrismLauncher', index={}),
            'games')

    def test_parse_desktop_file_keys_on_launcher_binary_not_the_game(self):
        # STILL TRUE (intentional characterization): a faithful Minecraft
        # .desktop file yields the LAUNCHER basename, never 'java'. The
        # .desktop path cannot attribute the JVM — which is exactly why
        # FIX-1 attributes via cmdline keywords/tokens instead.
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


class TestBillingNeverReachesTheBudget(unittest.TestCase):
    """FIX-1: the Minecraft JVM is a games member, so it bills Games."""

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

    def test_minecraft_jvm_bills_games_minutes(self):
        """(Was: burns_no_games_minutes) Both JVM shapes bill the tick."""
        for var in (VANILLA_JAVA, SYSTEM_JAVA):
            with self.subTest(var=var['pid']):
                entry = self._entry()
                self._two_active_ticks(entry, [dict(var)],
                                       index=LIVE_LIKE_INDEX)
                self.assertEqual(entry['used_seconds']['games'], 60.0)

    def test_steam_control_burns_games_minutes(self):
        entry = self._entry()
        self._two_active_ticks(entry, [dict(STEAM)], index={})
        self.assertAlmostEqual(entry['used_seconds']['games'], 60.0)

    def test_hot_jvm_bills_while_minimized_launcher_idles(self):
        """(Was: burns_nothing) FIX-1/F4: the GAME is the active member.

        With the JVM attributed, an idle launcher in the tray no longer
        keeps the whole Games category at zero.
        """
        entry = self._entry()
        now = datetime(2026, 10, 17, 15, 0)
        base = _pro(*[dict(VANILLA_JAVA), dict(PRISM_LAUNCHER)], cpu=100.0)
        account_and_enforce('kid', entry, base, None, now,
                            prefs={}, index=LIVE_LIKE_INDEX, act=False)
        hot_java = [dict(VANILLA_JAVA), dict(PRISM_LAUNCHER)]
        hot_java[0]['cpu_seconds'] = 130.0   # the GAME is hot
        hot_java[1]['cpu_seconds'] = 100.0   # the launcher is idle
        account_and_enforce('kid', entry, hot_java, None,
                            now + timedelta(seconds=60),
                            prefs={}, index=LIVE_LIKE_INDEX, act=False)
        self.assertEqual(entry['used_seconds']['games'], 60.0)
        status = budget_status('kid', {'kid': entry})
        # 60 seconds against a 60-minute limit: playing, not exhausted.
        self.assertFalse(status['games']['exhausted'])
        self.assertFalse(status['games']['paused'])

    def test_minecraft_burns_games_minutes(self):
        """FIX-1 (was test_DESIRE_...): un-marked desired-behavior test."""
        entry = self._entry()
        self._two_active_ticks(entry, [dict(VANILLA_JAVA)],
                               index=LIVE_LIKE_INDEX)
        self.assertAlmostEqual(entry['used_seconds']['games'], 60.0)


class TestTickUiParityGap(unittest.TestCase):
    """FIX-2: the tick classifies like the UI (per-user desktop index)."""

    def _blank(self):
        e = blank_entry()
        e['limits'] = {'games': 60}
        e['day'] = '2026-10-17'
        return e

    def _tick(self, entry, procs, cpu, now):
        return account_and_enforce('kid', entry, _pro(*procs, cpu=cpu),
                                   None, now, prefs={}, act=False)

    def test_direct_call_without_index_still_bills_nothing(self):
        """account_and_enforce does not glob by itself (intentional): the
        index is the CALLER's job — tick_budgets now supplies one per user.
        A desktop-only app (no curated keyword) proves the plumbing:
        index=None bills 'other' (nowhere), an index bills games.
        """
        e = self._blank()
        now = datetime(2026, 10, 17, 15, 0)
        self._tick(e, [DESKTOP_ONLY_GAME], 100.0, now)
        self._tick(e, [DESKTOP_ONLY_GAME], 130.0, now + timedelta(seconds=60))
        self.assertEqual(e['used_seconds'].get('games', 0.0), 0.0)

        # Same procs through the UI-side index: 'sol' is a games member.
        self.assertEqual(categorize(dict(DESKTOP_ONLY_GAME),
                                    index=SOL_INDEX), 'games')

    def test_tick_uses_the_desktop_index_like_the_ui(self):
        """FIX-2 (was test_DESIRE_...): re-scoped to the tick_budgets level.

        tick_budgets(index=None) must build build_desktop_index(username)
        and pass it into the classification path: a Prism launcher tick
        bills Games the same minute the UI already groups it there.
        """
        now = datetime(2026, 10, 17, 15, 0)
        entry = self._blank()
        entry['last_cpu'] = {'90003': {'comm': 'prismlaunch', 'cpu': 100.0}}
        entry['last_tick'] = (now - timedelta(seconds=60)).isoformat()
        procs = [dict(PRISM_LAUNCHER)]
        procs[0]['cpu_seconds'] = 130.0
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(budgets, 'BUDGETS_FILE',
                                  os.path.join(d, 'budgets.json')), \
                mock.patch.object(budgets, 'OVERRIDES_FILE',
                                  os.path.join(d, 'overrides.json')), \
                mock.patch.object(budgets, 'is_admin_protected',
                                  return_value=False), \
                mock.patch.object(budgets, 'build_desktop_index',
                                  return_value=dict(LIVE_LIKE_INDEX)) as builder:
            budgets._DESKTOP_INDEX_CACHE.clear()
            budgets.save_budgets({'kid': entry})
            summaries = budgets.tick_budgets(
                {}, now, procs_provider=lambda u: [dict(p) for p in procs],
                prefs={}, act=False, run_watchdog=False)
            saved = budgets.load_budgets()['kid']
        self.assertAlmostEqual(saved['used_seconds']['games'], 60.0)
        builder.assert_called_once_with('kid')
        kid_summaries = [s for s in summaries if s.get('user') == 'kid']
        self.assertEqual(len(kid_summaries), 1)
        self.assertFalse(kid_summaries[0]['note'])
        budgets._DESKTOP_INDEX_CACHE.clear()

    def test_tick_desktop_index_is_cached_per_user(self):
        """FIX-2 cost guard: the glob runs once per user per TTL, not per tick."""
        now = datetime(2026, 10, 17, 15, 0)
        e1 = self._blank()
        e1['last_cpu'] = {'90008': {'comm': 'sol', 'cpu': 100.0}}
        e1['last_tick'] = (now - timedelta(seconds=60)).isoformat()
        sol = dict(DESKTOP_ONLY_GAME)
        sol['cpu_seconds'] = 130.0
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(budgets, 'BUDGETS_FILE',
                                  os.path.join(d, 'budgets.json')), \
                mock.patch.object(budgets, 'OVERRIDES_FILE',
                                  os.path.join(d, 'overrides.json')), \
                mock.patch.object(budgets, 'is_admin_protected',
                                  return_value=False), \
                mock.patch.object(budgets, 'build_desktop_index',
                                  return_value=dict(SOL_INDEX)) as builder:
            budgets._DESKTOP_INDEX_CACHE.clear()
            budgets.save_budgets({'kid': e1})
            budgets.tick_budgets(
                {}, now, procs_provider=lambda u: [dict(sol)],
                prefs={}, act=False, run_watchdog=False)
            # Second tick, 60s later: same store/user -> cache hit,
            # build_desktop_index is NOT globsed again.
            budgets.tick_budgets(
                {}, now + timedelta(seconds=60),
                procs_provider=lambda u: [dict(sol)],
                prefs={}, act=False, run_watchdog=False)
            saved = budgets.load_budgets()['kid']
        builder.assert_called_once_with('kid')
        self.assertAlmostEqual(saved['used_seconds']['games'], 60.0)
        budgets._DESKTOP_INDEX_CACHE.clear()


class TestEnforcementGateFailures(unittest.TestCase):
    """H3: what must hold for exhaustion -> freeze (and FIX-3 surfacing).

    Uses the real current user + real own PID so the genuine
    ``verify_freeze_safety`` os.stat/pwd checks run; only the privileged
    cgroup primitives are faked.
    """

    USER = None  # resolved in setUp to the container's real user

    def setUp(self):
        import pwd
        self.USER = pwd.getpwuid(os.getuid()).pw_name
        self.migrated = []
        # FIX-3 dedup state is process-global; keep tests independent...
        budgets._LAST_SUMMARY_SIGS.clear()
        budgets._LAST_AUTO_PAUSE_SIGS.clear()
        # Redirect the real audit log into scratch (house env pattern).
        self._audit_env = os.environ.get('PARENTAL_CONTROLS_AUDIT')
        self._audit_dir = tempfile.TemporaryDirectory()
        os.environ['PARENTAL_CONTROLS_AUDIT'] = os.path.join(
            self._audit_dir.name, 'audit.jsonl')

    def tearDown(self):
        if self._audit_env is None:
            os.environ.pop('PARENTAL_CONTROLS_AUDIT', None)
        else:
            os.environ['PARENTAL_CONTROLS_AUDIT'] = self._audit_env
        self._audit_dir.cleanup()

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
        q['pid'] = 90090001      # fake: never enters verify (not in cgroup)
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

    def test_sudoers_or_perms_failure_is_surfaced(self):
        """FIX-3 (was: freezes_silently): ensure-cgroup failure now audits.

        The tick still records the note and marks NOTHING (paused stays
        False — fail-safe unchanged); what changed is that the failure is
        no longer invisible: audit.safe_log sees budget.auto_pause_failed.
        """
        entry = self._entry()
        steam = self._steam()
        with mock.patch.object(budgets.audit, 'safe_log') as logged:
            summary = self._ticks_to_exhaust(
                entry, [steam], [], ensure_ok=(False, "sudo: a password is "
                                                       "required"))
        self.assertFalse(entry['paused'].get('games'))
        self.assertIn('ensure cgroup', summary['note'])
        self.assertEqual(summary['froze'], [])
        events = [c for c in logged.call_args_list
                  if c.args[1] == 'budget.auto_pause_failed']
        self.assertEqual(len(events), 1)
        detail = events[0].args[3]
        self.assertFalse(detail['ok'])
        self.assertEqual(detail['stage'], 'ensure-cgroup')
        self.assertIn('password', detail['reason'])

    def test_auto_pause_failure_is_audited_once_per_change(self):
        """FIX-3 dedupe: an identical persistent failure is logged once,
        not every 60s tick; a changed reason is logged again."""
        entry = self._entry()
        steam = self._steam()
        with mock.patch.object(budgets.audit, 'safe_log') as logged:
            self._ticks_to_exhaust(entry, [steam], [],
                                   ensure_ok=(False, 'EPERM'))
            # Third and fourth identical ticks: still failing, still quiet.
            now = datetime(2026, 10, 17, 15, 2)
            hot = _pro(steam, cpu=130.0)
            for i in range(2):
                with mock.patch.object(budgets, 'is_admin_protected',
                                       return_value=False), \
                     mock.patch.object(budgets, 'ensure_cgroup',
                                       return_value=(False, 'EPERM')), \
                     mock.patch.object(budgets, 'migrate_pid',
                                       side_effect=self._record_migrate), \
                     mock.patch.object(budgets, 'cgroup_members',
                                       side_effect=lambda u, c: []), \
                     mock.patch.object(budgets, 'set_frozen',
                                       return_value=(True, '')):
                    account_and_enforce(self.USER, entry,
                                        _pro(steam, cpu=160.0 + i), None,
                                        now + timedelta(seconds=60 * i),
                                        prefs={}, index={}, act=True)
        events = [c for c in logged.call_args_list
                  if c.args[1] == 'budget.auto_pause_failed']
        self.assertEqual(len(events), 1)

    def test_tick_summaries_are_surfaced_by_both_production_paths(self):
        """FIX-3 (was: dropped_by_the_timer_path): both callers print.

        Static-source guard (subprocess-free): the timer and the app
        thread must feed the summaries they used to discard into
        budgets.emit_tick_summaries (stdout -> journald).
        """
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, 'enforce_bedtime_timer.py')) as f:
            timer_src = f.read()
        self.assertIn('tick_budgets', timer_src)
        self.assertRegex(timer_src,
                         r'summaries\s*=\s*budgets\.tick_budgets\(')
        self.assertIn('budgets.emit_tick_summaries(summaries)', timer_src)
        # app.py: bedtime_loop() body only — from `def bedtime_loop` to the
        # next top-level def (tick return value assigned AND emitted).
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
        self.assertRegex(loop_src,
                         r'summaries\s*=\s*budgets\.tick_budgets\(')
        self.assertIn('budgets.emit_tick_summaries(summaries)', loop_src)

    def test_stale_cgroup_member_permanently_aborts_freeze(self):
        """STILL THE LIVE BEHAVIOR — FIX-4 deferred by owner 2026-10-17
        (see TIME_TRACKING_QA.md §7): a foreign/stale PID still inside
        parental-<user>-games (e.g. the owner's pre-existing experiment
        groups) aborts verify_freeze_safety EVERY tick; paused never gets
        set. This characterization guards the safety property FIX-4 must
        preserve (never freeze what this tick did not classify); FIX-4's
        reconciliation/rebuild of stale members is deliberately NOT
        implemented. What FIX-3 DID change: the abort is no longer
        silent — the note surfaces via emit + audit."""
        entry = self._entry()
        steam = self._steam()
        stale = os.getppid()  # a live pid that is NOT a steam member
        self.assertNotEqual(stale, steam['pid'])
        summary = self._ticks_to_exhaust(entry, [steam], [stale])
        self.assertFalse(entry['paused'].get('games'))
        self.assertIn('freeze aborted', summary['note'])

    def test_exhausted_games_pauses_and_captures_minecraft_too(self):
        """(Was: pauses_but_minecraft_keeps_playing) FIX-1 unified symptom 2.

        The JVM is a games member, so the exhaustion freeze migrates it
        alongside correctly-tracked processes: paused games now actually
        pauses Minecraft.
        """
        entry = self._entry()
        steam, java = self._steam(), self._java()
        cgroup_state = [steam['pid']]
        summary = self._ticks_to_exhaust(entry, [steam, java], cgroup_state)
        self.assertTrue(entry['paused'].get('games'))
        self.assertEqual(self.migrated, [('games', steam['pid']),
                                         ('games', java['pid'])])
        self.assertIn('games', [tag for tag in summary['froze']])

    def test_minecraft_frozen_with_the_games_budget(self):
        """FIX-1 (was test_DESIRE_...): the JVM migrates into the group."""
        entry = self._entry()
        steam, java = self._steam(), self._java()
        cgroup_state = [steam['pid']]
        self._ticks_to_exhaust(entry, [steam, java], cgroup_state)
        migrated_pids = {pid for _c, pid in self.migrated}
        self.assertIn(java['pid'], migrated_pids)


class TestEmitHelper(unittest.TestCase):
    """FIX-3 unit: the emit helper prints one line per changed user."""

    def setUp(self):
        self.state = {}
        self.buf = io.StringIO()

    def emit(self, summaries):
        with redirect_stdout(self.buf):
            lines = budgets.emit_tick_summaries(summaries, state=self.state)
        return lines

    def test_note_change_prints_repeat_does_not(self):
        lines = self.emit([{'user': 'kid', 'note': 'ensure cgroup: EPERM',
                            'froze': [], 'unfroze': []}])
        self.assertEqual(len(lines), 1)
        self.assertIn('budget-tick kid', lines[0])
        self.assertIn('ensure cgroup: EPERM', lines[0])
        self.assertIn('budget-tick kid', self.buf.getvalue())
        # Identical note next tick: silent (the timer PROCESS gets a fresh
        # state per run, so repeating failures still resurface there).
        self.assertEqual(self.emit([{'user': 'kid',
                                     'note': 'ensure cgroup: EPERM'}]), [])
        # A CHANGED note prints again.
        lines = self.emit([{'user': 'kid', 'note': 'freeze aborted: stale'}])
        self.assertEqual(len(lines), 1)
        self.assertIn('freeze aborted', lines[0])

    def test_degraded_and_froze_fields_print(self):
        lines = self.emit([{'user': 'kid', 'degraded': True,
                            'froze': ['games'], 'unfroze': ['media'],
                            'skipped': ['watchdog-skip']}])
        self.assertEqual(len(lines), 1)
        for token in ('degraded', 'froze=games', 'unfroze=media',
                      'skipped=watchdog-skip'):
            self.assertIn(token, lines[0])

    def test_boring_summary_is_silent(self):
        self.assertEqual(self.emit([{'user': 'kid', 'note': '',
                                     'froze': [], 'unfroze': [],
                                     'notified': ['games (paused)'],
                                     'reset': True}]), [])
        self.assertEqual(self.buf.getvalue(), '')

    def test_summary_line_renders_note(self):
        line = budgets.summary_line({'user': 'kid', 'note': 'boom'})
        self.assertEqual(line, 'budget-tick kid note=boom')


class TestSaveBudgetsFailure(unittest.TestCase):
    """FIX-3 tail: a swallowed save_budgets OSError now prints + audits."""

    def test_save_oserror_prints_and_audits(self):
        now = datetime(2026, 10, 17, 15, 0)
        entry = blank_entry()
        entry['limits'] = {'games': 60}
        entry['day'] = '2026-10-17'
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(budgets, 'BUDGETS_FILE',
                                  os.path.join(d, 'budgets.json')), \
                mock.patch.object(budgets, 'OVERRIDES_FILE',
                                  os.path.join(d, 'overrides.json')), \
                mock.patch.object(budgets, 'is_admin_protected',
                                  return_value=False):
            budgets.save_budgets({'kid': entry})
            with mock.patch.object(budgets, 'save_budgets',
                                   side_effect=OSError('disk gone')), \
                    mock.patch.object(budgets.audit, 'safe_log') as logged:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    budgets.tick_budgets(
                        {}, now, procs_provider=lambda u: [], prefs={},
                        index={}, act=False, run_watchdog=False)
        out = buf.getvalue()
        self.assertIn('BUDGETS SAVE FAILED', out)
        self.assertIn('disk gone', out)
        events = [c for c in logged.call_args_list
                  if c.args[1] == 'budget.save_failed']
        self.assertEqual(len(events), 1)
        self.assertIn('disk gone', events[0].args[3]['error'])


class TestFocusEngineGate(unittest.TestCase):
    """FIX-1/F7: a focused Minecraft window now bills Games (was 'other')."""

    def _rows(self, wm_class='Minecraft', app_id='minecraft'):
        return [
            {'type': 'focus', 'seq': 1, 'kind': 'app', 'bt': 100.0,
             'wm_class': wm_class, 'app_id': app_id, 'since': 0.0},
            {'type': 'focus', 'seq': 2, 'kind': 'app', 'bt': 160.0,
             'wm_class': wm_class, 'app_id': app_id, 'since': 60.0},
        ]

    def test_minecraft_window_bills_games(self):
        """FIX-1 (was test_DESIRE_...): window + JVM both attribute."""
        agg = focus_feed.aggregate_focus(
            self._rows(), (100.0, 160.0),
            procs=[dict(VANILLA_JAVA)], limits={'games': 60},
            username='kid', prefs={}, index=LIVE_LIKE_INDEX,
            now_bt=160.0)
        self.assertEqual(agg['seconds']['games'], 60.0)
        self.assertTrue(agg['billable'])

    def test_firefox_window_control_is_billable(self):
        agg = focus_feed.aggregate_focus(
            self._rows('firefox', 'firefox'), (100.0, 160.0),
            procs=[dict(FIREFOX)], limits={'browsers': 60},
            username='kid', prefs={}, index={}, now_bt=160.0)
        self.assertEqual(agg['seconds']['browsers'], 60.0)

    def test_unattributed_window_is_still_not_billable(self):
        """(Was: minecraft_window_not_billable...) The fail-closed gate
        survives the fix: a window nothing categorizes into a limited
        category bills zero seconds."""
        agg = focus_feed.aggregate_focus(
            self._rows('SolitaireApp', 'solitaireapp'), (100.0, 160.0),
            procs=[dict(VANILLA_JAVA)], limits={'games': 60},
            username='kid', prefs={}, index={}, now_bt=160.0)
        self.assertTrue(agg['billable'])
        self.assertEqual(agg['seconds']['games'], 0.0)
        self.assertGreaterEqual(agg['stats']['not_billable'], 1)


if __name__ == '__main__':
    unittest.main()
