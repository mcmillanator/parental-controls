#!/usr/bin/env python3
"""Tests for the per-user session agent (Phase 1).

Covers the child-side agent (``session_agent.py``), the panel-side feed
module (``focus_feed.py``), the systemd USER unit template, the sudoers
transport grant and the installer wiring.

Everything here is static or mocked: no live desktop, no live accounts, no
privileged commands, no service restart. The sudoers grant is checked with the
faithful in-tree matcher in ``tests/test_deployment.py`` (NOT live ``visudo``,
which reads ``/etc/sudoers`` outside the worktree). Where a property can only
be confirmed on a real machine it is left as a labelled "verify on the live
box" note rather than executed.
"""

import ast
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import focus_feed
import session_agent
import test_deployment as dep  # faithful in-tree sudoers matcher

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BOOT = 'boot-0123456789abcdef'


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def iso(bt):
    # A fixed, monotonic wall-clock image for a boottime offset.
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=float(bt))).isoformat()


def make_row(seq, bt, rtype='focus', boot=BOOT, v=1, **fields):
    row = {'v': v, 'seq': seq, 'agent': '0.1.0', 'boot_id': boot,
           'ts': iso(bt), 'bt': float(bt), 'type': rtype}
    if rtype == 'focus':
        row.setdefault('session', seq)
        row.setdefault('wm_class', 'firefox')
        row.setdefault('kind', 'app')
        row.setdefault('since', 0.0)
    elif rtype == 'state':
        row.setdefault('state', 'active')
    else:
        row.setdefault('code', 'x')
    row.update(fields)
    return row


def feed(*rows):
    return ''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in rows)


def firefox_proc(pid=100, comm='firefox',
                 cmdline='/usr/lib64/firefox/firefox'):
    return {'pid': pid, 'comm': comm, 'cmdline': cmdline, 'user_app': True}


def vlc_proc(pid=200, comm='vlc', cmdline='/usr/bin/vlc'):
    return {'pid': pid, 'comm': comm, 'cmdline': cmdline, 'user_app': True}


class SeqSource:
    """Deterministic focus source for the agent loop."""

    def __init__(self, windows, available=True):
        self.windows = list(windows)
        self._available = available

    def available(self):
        return self._available

    def current_window(self):
        if not self.windows:
            return None
        nxt = self.windows.pop(0)
        return nxt


def window(wm='firefox', app='firefox.desktop', kind='app'):
    return {'wm_class': wm, 'app_id': app, 'kind': kind}


class FakeProc:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ===========================================================================
# Schema constants mirror (the agent and the panel must agree)
# ===========================================================================

class TestSchemaConstants(unittest.TestCase):
    NAMES = ('SCHEMA_VERSION', 'AGENT_TYPES', 'KINDS', 'STATES',
             'MAX_LINE_BYTES', 'MAX_FEED_BYTES', 'MAX_ROWS_PER_DRAIN',
             'FEED_STALE_SECONDS', 'MAX_FOCUS_SESSION_SECONDS',
             'MAX_TICK_WALL_SECONDS')

    def test_agent_and_panel_constants_match(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                self.assertEqual(getattr(session_agent, name),
                                 getattr(focus_feed, name),
                                 f'{name} drifted between agent and panel')

    def test_schema_version_is_one(self):
        self.assertEqual(focus_feed.SCHEMA_VERSION, 1)


# ===========================================================================
# Feed parser / validation
# ===========================================================================

class TestFeedParser(unittest.TestCase):
    def test_valid_focus_row(self):
        body = feed(make_row(1, 100))
        result = focus_feed.parse_feed(body, feed_seq=0)
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['last_seq'], 1)

    def test_wrong_schema_version_rejected(self):
        result = focus_feed.parse_feed(feed(make_row(1, 100, v=2)),
                                       feed_seq=0)
        self.assertEqual(result['rows'], [])
        self.assertIn('bad schema version', result['errors'])

    def test_malformed_json_counted(self):
        result = focus_feed.parse_feed('this is not json\n', feed_seq=0)
        self.assertIn('malformed JSON', result['errors'])

    def test_unknown_type_rejected(self):
        result = focus_feed.parse_feed(feed(make_row(1, 100, rtype='bogus')),
                                       feed_seq=0)
        self.assertEqual(result['rows'], [])
        self.assertIn('unknown row type', result['errors'])

    def test_non_monotonic_seq_is_error(self):
        body = feed(make_row(3, 100), make_row(2, 110))
        result = focus_feed.parse_feed(body, feed_seq=0)
        self.assertIn('non-monotonic seq 2', result['errors'])

    def test_line_over_cap_rejected(self):
        self.assertGreater(focus_feed.MAX_LINE_BYTES, 100)
        long_wm = 'a' * (focus_feed.MAX_LINE_BYTES + 50)
        result = focus_feed.parse_feed(feed(make_row(1, 100, wm_class=long_wm)),
                                       feed_seq=0)
        self.assertIn('line too long', result['errors'])

    def test_body_over_size_cap_rejected(self):
        # Build a body just over the cap using many valid-ish lines.
        line = json.dumps(make_row(1, 100), separators=(',', ':'))
        body = (line + '\n') * (focus_feed.MAX_FEED_BYTES // len(line) + 5)
        result = focus_feed.parse_feed(body, feed_seq=0)
        self.assertTrue(result['too_large'])
        self.assertTrue(result['stale'])
        self.assertEqual(result['rows'], [])

    def test_truncated_tail_tolerated(self):
        good = json.dumps(make_row(1, 100), separators=(',', ':'))
        body = good + '\n' + '{"v":1,"seq":2,"agent":"0.1.0"'
        result = focus_feed.parse_feed(body, feed_seq=0)
        self.assertTrue(result['truncated_tail'])
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['errors'], [])

    def test_boot_id_change_signalled(self):
        body = feed(make_row(2, 100, boot='other-boot'))
        result = focus_feed.parse_feed(body, feed_seq=0,
                                       expected_boot_id=BOOT)
        self.assertTrue(result['boot_changed'])

    def test_seq_at_or_below_cursor_ignored(self):
        body = feed(make_row(1, 100), make_row(2, 110), make_row(3, 120))
        result = focus_feed.parse_feed(body, feed_seq=2)
        self.assertEqual(result['ignored_below_cursor'], 2)
        self.assertEqual([r['seq'] for r in result['rows']], [3])

    def test_rate_limit_truncates(self):
        rows = [make_row(i + 1, 100 + i) for i in
                range(focus_feed.MAX_ROWS_PER_DRAIN + 5)]
        result = focus_feed.parse_feed(feed(*rows), feed_seq=0)
        self.assertTrue(result['rate_limited'])
        self.assertEqual(len(result['rows']), focus_feed.MAX_ROWS_PER_DRAIN)

    def test_bad_wm_class_charset(self):
        result = focus_feed.parse_feed(
            feed(make_row(1, 100, wm_class='../evil')), feed_seq=0)
        self.assertIn('bad wm_class', result['errors'])

    def test_bad_kind(self):
        result = focus_feed.parse_feed(
            feed(make_row(1, 100, kind='wat')), feed_seq=0)
        self.assertIn('bad kind', result['errors'])

    def test_bad_since_negative_and_capped(self):
        r1 = focus_feed.parse_feed(feed(make_row(1, 100, since=-1)),
                                   feed_seq=0)
        self.assertIn('bad since', r1['errors'])
        r2 = focus_feed.parse_feed(
            feed(make_row(1, 100,
                          since=focus_feed.MAX_FOCUS_SESSION_SECONDS + 1)),
            feed_seq=0)
        self.assertIn('bad since', r2['errors'])

    def test_bad_bt_and_ts(self):
        r1 = focus_feed.parse_feed(feed(make_row(1, -5)), feed_seq=0)
        self.assertIn('bad bt', r1['errors'])
        r2 = focus_feed.parse_feed(
            feed(make_row(1, 100, ts='not-a-date')), feed_seq=0)
        self.assertIn('bad ts', r2['errors'])

    def test_bad_session_and_state_and_error(self):
        r1 = focus_feed.parse_feed(feed(make_row(1, 100, session='x')),
                                   feed_seq=0)
        self.assertIn('bad session', r1['errors'])
        r2 = focus_feed.parse_feed(
            feed(make_row(1, 100, rtype='state', state='bogus')), feed_seq=0)
        self.assertIn('bad state', r2['errors'])
        r3 = focus_feed.parse_feed(
            feed(make_row(1, 100, rtype='error', code='')), feed_seq=0)
        self.assertIn('bad error code', r3['errors'])

    def test_focus_row_without_identity_rejected(self):
        result = focus_feed.parse_feed(
            feed(make_row(1, 100, wm_class=None, app_id=None)), feed_seq=0)
        self.assertIn('focus row missing window identity', result['errors'])

    def test_missing_feed_is_stale_not_zero(self):
        result = focus_feed.parse_feed(None, feed_seq=7)
        self.assertTrue(result['missing'])
        self.assertTrue(result['stale'])
        self.assertEqual(result['last_seq'], 7)


# ===========================================================================
# Heartbeat / staleness
# ===========================================================================

class TestHeartbeatStaleness(unittest.TestCase):
    def test_fresh_heartbeat(self):
        body = feed(make_row(1, 100, rtype='state'))
        result = focus_feed.parse_feed(body, feed_seq=0, now_bt=250)
        self.assertFalse(result['stale'])
        self.assertEqual(result['last_heartbeat_bt'], 100)

    def test_stale_heartbeat(self):
        body = feed(make_row(1, 100, rtype='state'))
        result = focus_feed.parse_feed(body, feed_seq=0,
                                       now_bt=100 + int(
                                           focus_feed.FEED_STALE_SECONDS) + 1)
        self.assertTrue(result['stale'])
        self.assertTrue(result['degraded'])

    def test_is_stale_none(self):
        self.assertTrue(focus_feed.is_stale(None, 1000))

    def test_focus_rows_count_as_liveness_but_not_heartbeat(self):
        body = feed(make_row(1, 100), make_row(2, 130))
        result = focus_feed.parse_feed(body, feed_seq=0, now_bt=140)
        self.assertFalse(result['stale'])
        self.assertEqual(result['last_bt'], 130)
        self.assertIsNone(result['last_heartbeat_bt'])

    def test_load_feed_missing_file(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            result = focus_feed.load_feed(os.path.join(tmp, 'nope.jsonl'))
        self.assertTrue(result['missing'])
        self.assertTrue(result['stale'])

    def test_states_seen(self):
        body = feed(make_row(1, 100, rtype='state', state='no_session'),
                    make_row(2, 130, rtype='state', state='active'))
        result = focus_feed.parse_feed(body, feed_seq=0)
        self.assertEqual(result['states'], ['no_session', 'active'])


# ===========================================================================
# aggregate_focus
# ===========================================================================

class TestAggregateFocus(unittest.TestCase):
    def agg(self, rows, window, procs=None, limits=None, index=None,
            heartbeat_bt=None, now_bt=None,
            cap_seconds=focus_feed.MAX_TICK_WALL_SECONDS):
        return focus_feed.aggregate_focus(
            rows, window, procs=procs or [firefox_proc()],
            limits=limits if limits is not None else {'browsers': 60},
            username='child', prefs={}, index=index, heartbeat_bt=heartbeat_bt,
            now_bt=now_bt, cap_seconds=cap_seconds)

    def test_single_window_full_tick(self):
        rows = [make_row(1, 1000),
                make_row(2, 1060, rtype='state')]
        result = self.agg(rows, (1000, 1060))
        self.assertAlmostEqual(result['seconds']['browsers'], 60.0, places=3)
        self.assertTrue(result['billable'])

    def test_partial_tick_overlap_clipped(self):
        rows = [make_row(1, 1000),
                make_row(2, 1060, rtype='state')]
        result = self.agg(rows, (1030, 1060))
        self.assertAlmostEqual(result['seconds']['browsers'], 30.0, places=3)

    def test_consecutive_same_window_merge(self):
        rows = [make_row(1, 1000),
                make_row(2, 1030, since=30.0),
                make_row(3, 1060, rtype='state')]
        result = self.agg(rows, (1000, 1060))
        self.assertAlmostEqual(result['seconds']['browsers'], 60.0, places=3)

    def test_since_bounds_the_interval(self):
        # The second row claims the first only held focus for 100s.
        rows = [make_row(1, 1000),
                make_row(2, 2000, since=100.0),
                make_row(3, 2000, rtype='state')]
        result = self.agg(rows, (1000, 2000))
        self.assertAlmostEqual(result['seconds']['browsers'], 100.0, places=3)

    def test_per_session_cap(self):
        rows = [make_row(1, 0),
                make_row(2, focus_feed.MAX_FOCUS_SESSION_SECONDS + 500,
                         rtype='state')]
        result = self.agg(rows, (0, focus_feed.MAX_FOCUS_SESSION_SECONDS + 500),
                          cap_seconds=None)
        self.assertAlmostEqual(result['seconds']['browsers'],
                               focus_feed.MAX_FOCUS_SESSION_SECONDS, places=3)

    def test_per_tick_cap(self):
        rows = [make_row(1, 0),
                make_row(2, 1000, rtype='state')]
        result = self.agg(rows, (0, 1000))
        self.assertAlmostEqual(result['total'],
                               focus_feed.MAX_TICK_WALL_SECONDS, places=3)
        self.assertTrue(result['stats'].get('tick_capped'))

    def test_unmappable_window_falls_to_other(self):
        rows = [make_row(1, 1000, wm_class='weirdapp', app_id=None),
                make_row(2, 1060, rtype='state')]
        procs = [{'pid': 9, 'comm': 'weirdapp', 'cmdline': 'weirdapp',
                  'user_app': True}]
        result = self.agg(rows, (1000, 1060), procs=procs,
                          limits={'other': 60})
        self.assertAlmostEqual(result['seconds']['other'], 60.0, places=3)

    def test_non_app_kind_excluded(self):
        rows = [make_row(1, 1000, kind='lockscreen'),
                make_row(2, 1060, rtype='state')]
        result = self.agg(rows, (1000, 1060))
        self.assertEqual(result['total'], 0.0)
        self.assertEqual(result['stats']['skipped_kind'], 1)

    def test_bedtime_boundary_split(self):
        rows = [make_row(1, 1000),
                make_row(2, 1060, rtype='state')]
        result = self.agg(rows, (1040, 1100))
        self.assertAlmostEqual(result['seconds']['browsers'], 20.0, places=3)

    def test_wide_window_with_two_sessions(self):
        rows = [make_row(1, 1000),
                make_row(2, 3000, since=2000.0),
                make_row(3, 3500, rtype='state')]
        result = self.agg(rows, (0, 100000), cap_seconds=None)
        self.assertAlmostEqual(result['seconds']['browsers'], 2500.0, places=3)

    def test_non_monotonic_focus_raises(self):
        rows = [make_row(2, 1000), make_row(1, 1060)]
        with self.assertRaises(focus_feed.FeedError):
            self.agg(rows, (1000, 1060))

    def test_no_liveness_is_degraded_and_not_billable(self):
        result = self.agg([], (1000, 1060))
        self.assertTrue(result['degraded'])
        self.assertFalse(result['billable'])

    def test_stale_now_bt_marks_degraded(self):
        rows = [make_row(1, 1000),
                make_row(2, 1060, rtype='state')]
        result = self.agg(rows, (1000, 1060),
                          now_bt=1060 + int(focus_feed.FEED_STALE_SECONDS)
                          + 1)
        self.assertTrue(result['degraded'])
        self.assertFalse(result['billable'])

    def test_accepts_parse_result_dict(self):
        body = feed(make_row(1, 1000), make_row(2, 1060, rtype='state'))
        parsed = focus_feed.parse_feed(body, feed_seq=0, now_bt=1060)
        result = self.agg(parsed, (1000, 1060))
        self.assertTrue(result['billable'])
        self.assertAlmostEqual(result['seconds']['browsers'], 60.0, places=3)


# ===========================================================================
# Category mapping + freezable gate ("only bill what you can freeze")
# ===========================================================================

class TestFreezableGate(unittest.TestCase):
    def agg(self, rows, procs, limits, index=None):
        return focus_feed.aggregate_focus(
            rows, (0, 100000), procs=procs, limits=limits,
            username='child', prefs={}, index=index,
            cap_seconds=None)

    def rows_one(self, wm='firefox', app='firefox.desktop', kind='app'):
        return [make_row(1, 1000, wm_class=wm, app_id=app, kind=kind),
                make_row(2, 2000, rtype='state')]

    def test_override_mapping_firefox_to_browsers(self):
        result = self.agg(self.rows_one(), [firefox_proc()],
                          {'browsers': 60})
        self.assertGreater(result['seconds']['browsers'], 0)

    def test_desktop_index_mapping(self):
        result = self.agg(self.rows_one(wm='mygame', app=None),
                          [{'pid': 5, 'comm': 'mygame',
                            'cmdline': '/usr/bin/mygame', 'user_app': True}],
                          {'games': 60}, index={'mygame': 'games'})
        self.assertGreater(result['seconds']['games'], 0)

    def test_freezable_member_present_bills(self):
        result = self.agg(self.rows_one(), [firefox_proc()],
                          {'browsers': 60})
        self.assertEqual(result['stats']['intervals'], 1)

    def test_all_never_pause_not_billable(self):
        # bash is NEVER-pause (and default-hidden): nothing to freeze.
        result = self.agg(self.rows_one(wm='bash', app=None),
                          [{'pid': 7, 'comm': 'bash', 'cmdline': 'bash',
                            'user_app': True}],
                          {'other': 60})
        self.assertEqual(result['total'], 0.0)
        self.assertGreaterEqual(result['stats']['not_billable'], 1)

    def test_no_matching_procs_not_billable(self):
        result = self.agg(self.rows_one(), [vlc_proc()], {'browsers': 60})
        self.assertEqual(result['total'], 0.0)
        self.assertGreaterEqual(result['stats']['not_billable'], 1)

    def test_no_limit_configured_not_billable(self):
        result = self.agg(self.rows_one(), [firefox_proc()], {})
        self.assertEqual(result['total'], 0.0)

    def test_multi_pid_match_bills_once(self):
        procs = [firefox_proc(pid=1), firefox_proc(pid=2)]
        result = self.agg(self.rows_one(), procs, {'browsers': 60})
        self.assertEqual(result['stats']['intervals'], 1)

    def test_window_matches_proc_variants(self):
        self.assertTrue(focus_feed.window_matches_proc(
            'firefox', None, firefox_proc()))
        self.assertTrue(focus_feed.window_matches_proc(
            'firefox', 'firefox.desktop', firefox_proc()))
        self.assertFalse(focus_feed.window_matches_proc(
            'firefox', None, vlc_proc()))


# ===========================================================================
# Child-side agent: feed writer
# ===========================================================================

class TestFeedWriter(unittest.TestCase):
    def make_writer(self, tmp, name='focus.jsonl', **kw):
        return session_agent.FeedWriter(os.path.join(tmp, name),
                                        boot_id=BOOT, **kw)

    def test_write_focus_assigns_seq_and_fields(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            writer = self.make_writer(tmp)
            row = writer.write_focus('firefox', 'firefox.desktop', 'app',
                                     session=1, since=0.0, now_bt=1000.0)
            self.assertEqual(row['v'], 1)
            self.assertEqual(row['seq'], 1)
            self.assertEqual(row['boot_id'], BOOT)
            self.assertEqual(row['type'], 'focus')
            self.assertEqual(row['kind'], 'app')

    def test_seq_is_monotonic_and_resumes_from_file(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            path = os.path.join(tmp, 'focus.jsonl')
            w1 = session_agent.FeedWriter(path, boot_id=BOOT)
            w1.write_state('active', 1000.0)
            w1.write_state('active', 1030.0)
            w2 = session_agent.FeedWriter(path, boot_id=BOOT)
            self.assertEqual(w2.seq, 2)
            row = w2.write_state('active', 1060.0)
            self.assertEqual(row['seq'], 3)

    def test_boot_id_change_resets_epoch(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            path = os.path.join(tmp, 'focus.jsonl')
            w1 = session_agent.FeedWriter(path, boot_id='old-boot')
            w1.write_state('active', 1000.0)
            w2 = session_agent.FeedWriter(path, boot_id='new-boot')
            self.assertEqual(w2.seq, 0)
            self.assertEqual(w2.write_state('active', 1.0)['seq'], 1)

    def test_rotation_keeps_seq_monotonic(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            path = os.path.join(tmp, 'focus.jsonl')
            writer = session_agent.FeedWriter(path, boot_id=BOOT,
                                             max_bytes=400)
            last = 0
            for i in range(20):
                last = writer.write_state('active', float(i))['seq']
            self.assertEqual(last, 20)
            self.assertTrue(os.path.exists(path + '.1'))

    def test_invalid_row_refused(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            writer = self.make_writer(tmp)
            with self.assertRaises(ValueError):
                writer.write({'type': 'focus', 'wm_class': '../evil',
                              'kind': 'app', 'since': 0.0, 'session': 1,
                              'bt': 1.0})

    def test_feed_dir_mode_and_symlink_guard(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            nested = os.path.join(tmp, 'sub', 'focus.jsonl')
            session_agent.FeedWriter(nested, boot_id=BOOT)
            mode = stat.S_IMODE(os.stat(os.path.dirname(nested)).st_mode)
            self.assertEqual(mode, 0o700)
            link = os.path.join(tmp, 'link.jsonl')
            os.symlink(nested, link)
            with self.assertRaises(ValueError):
                session_agent.FeedWriter(link, boot_id=BOOT)

    def test_stdout_writer_for_dry_run(self):
        import contextlib
        import io
        writer = session_agent.FeedWriter('-', boot_id=BOOT)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            row = writer.write_state('active', 1.0)
        self.assertEqual(row['seq'], 1)
        self.assertIn('"type":"state"', buf.getvalue())


# ===========================================================================
# Child-side agent: source parsing + state machine
# ===========================================================================

class TestKWinPollSource(unittest.TestCase):
    def test_parse_kwin_info(self):
        text = ('WId: 0x1\nresourceClass: "firefox"\n'
                'desktopFileName: "firefox.desktop"\ncaption: "secret"')
        info = session_agent.parse_kwin_info(text)
        self.assertEqual(info['wm_class'], 'firefox')
        self.assertEqual(info['app_id'], 'firefox.desktop')
        self.assertEqual(info['kind'], 'app')
        self.assertNotIn('caption', info)

    def test_parse_kwin_info_no_window(self):
        self.assertIsNone(session_agent.parse_kwin_info(''))
        self.assertIsNone(session_agent.parse_kwin_info('resourceName: "x"'))

    def test_classify_kind(self):
        self.assertEqual(session_agent.classify_kind('kscreenlocker_greet'),
                         'lockscreen')
        self.assertEqual(session_agent.classify_kind('plasmashell'), 'shell')
        self.assertEqual(session_agent.classify_kind('firefox'), 'app')

    def test_source_available_requires_qdbus_and_display(self):
        src = session_agent.KWinPollSource(which=lambda n: None)
        self.assertFalse(src.available())
        src2 = session_agent.KWinPollSource(which=lambda n: '/usr/bin/' + n)
        saved = os.environ.pop('WAYLAND_DISPLAY', None)
        saved_d = os.environ.pop('DISPLAY', None)
        try:
            self.assertFalse(src2.available())
            os.environ['DISPLAY'] = ':0'
            self.assertTrue(src2.available())
        finally:
            os.environ.pop('DISPLAY', None)
            if saved is not None:
                os.environ['DISPLAY'] = saved
            if saved_d is not None:
                os.environ['WAYLAND_DISPLAY'] = saved_d

    def test_current_window_via_injected_runner(self):
        def runner(argv, capture_output=True, text=True, timeout=None):
            if any('activeWindow' in a for a in argv):
                return FakeProc(0, '0x1\n')
            if any('getWindowInfo' in a for a in argv):
                return FakeProc(0, 'resourceClass: "firefox"\n'
                                   'desktopFileName: "firefox.desktop"\n')
            return FakeProc(1)

        src = session_agent.KWinPollSource(runner=runner,
                                           which=lambda n: '/usr/bin/' + n)
        info = src.current_window()
        self.assertEqual(info['wm_class'], 'firefox')

    def test_current_window_no_active_window(self):
        def runner(argv, capture_output=True, text=True, timeout=None):
            return FakeProc(0, '\n')

        src = session_agent.KWinPollSource(runner=runner,
                                           which=lambda n: '/usr/bin/' + n)
        self.assertIsNone(src.current_window())

    def test_current_window_engine_unavailable(self):
        src = session_agent.KWinPollSource(which=lambda n: None)
        self.assertIs(src.current_window(), session_agent.NO_ENGINE)


class TestSessionAgentMachine(unittest.TestCase):
    def agent(self, tmp, windows, **kw):
        writer = session_agent.FeedWriter(os.path.join(tmp, 'focus.jsonl'),
                                          boot_id=BOOT)
        src = SeqSource(windows, available=kw.pop('available', True))
        return session_agent.SessionAgent(writer, src, **kw), writer

    def test_first_cycle_seeds_focus_and_heartbeat(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            agent, _ = self.agent(tmp, [window()])
            rows = agent.run_once(now_bt=1000.0)
            types = [r['type'] for r in rows]
            self.assertEqual(types[0], 'focus')
            self.assertEqual(rows[0]['since'], 0.0)
            self.assertIn('state', types)

    def test_focus_change_closes_previous(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            agent, _ = self.agent(tmp, [window('firefox'),
                                        window('vlc', 'vlc.desktop')])
            agent.run_once(now_bt=1000.0)
            rows = agent.run_once(now_bt=1035.0)
            focus = [r for r in rows if r['type'] == 'focus']
            # One closing row for the outgoing window, one opening row for the
            # new one (the opening row's bt starts the next interval).
            self.assertEqual(len(focus), 2)
            self.assertEqual(focus[0]['wm_class'], 'firefox')
            self.assertAlmostEqual(focus[0]['since'], 35.0, places=3)
            self.assertEqual(focus[1]['wm_class'], 'vlc')
            self.assertEqual(focus[1]['since'], 0.0)

    def test_same_window_no_focus_row(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            agent, _ = self.agent(tmp, [window(), window()])
            agent.run_once(now_bt=1000.0)
            rows = agent.run_once(now_bt=1005.0)
            self.assertEqual([r for r in rows if r['type'] == 'focus'], [])

    def test_no_session_heartbeat(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            agent, _ = self.agent(tmp, [None])
            rows = agent.run_once(now_bt=1000.0)
            states = [r['state'] for r in rows if r['type'] == 'state']
            self.assertEqual(states, ['no_session'])

    def test_no_engine_reports_error_once_then_heartbeat(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            agent, _ = self.agent(tmp, [], available=False)
            rows = agent.run_once(now_bt=1000.0)
            self.assertIn('error', [r['type'] for r in rows])
            rows2 = agent.run_once(now_bt=1001.0)
            self.assertNotIn('error', [r['type'] for r in rows2])
            self.assertEqual([r['state'] for r in rows2
                              if r['type'] == 'state'], [])

    def test_suspend_gap_emits_suspend_and_closes(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            agent, _ = self.agent(tmp, [window()])
            agent.run_once(now_bt=1000.0)
            rows = agent.run_once(now_bt=1000.0
                                  + agent.suspend_gap_seconds + 1)
            states = [r['state'] for r in rows if r['type'] == 'state']
            self.assertIn('suspend', states)

    def test_heartbeat_cadence(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            agent, _ = self.agent(tmp, [window(), window(), window()])
            agent.run_once(now_bt=1000.0)          # seed + heartbeat
            rows = agent.run_once(now_bt=1005.0)   # too soon
            self.assertEqual([r for r in rows if r['type'] == 'state'], [])


# ===========================================================================
# Headless end-to-end (synthetic JSONL through the module)
# ===========================================================================

class TestHeadlessE2E(unittest.TestCase):
    def test_writer_to_aggregate(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            path = os.path.join(tmp, 'focus.jsonl')
            writer = session_agent.FeedWriter(path, boot_id=BOOT)
            agent = session_agent.SessionAgent(
                writer, SeqSource([window('firefox', 'firefox.desktop'),
                                   window('firefox', 'firefox.desktop'),
                                   window('vlc', 'vlc.desktop'),
                                   window('vlc', 'vlc.desktop')]))
            agent.run_once(now_bt=1000.0)
            agent.run_once(now_bt=1030.0)
            agent.run_once(now_bt=1035.0)
            agent.run_once(now_bt=1060.0)
            parsed = focus_feed.load_feed(path, feed_seq=0, now_bt=1060.0)
            self.assertFalse(parsed['stale'])
            result = focus_feed.aggregate_focus(
                parsed, (1000.0, 1060.0),
                procs=[firefox_proc(), vlc_proc()],
                limits={'browsers': 60, 'media': 60},
                username='child', prefs={})
            self.assertTrue(result['billable'])
            self.assertAlmostEqual(result['seconds']['browsers'], 35.0,
                                   places=3)
            self.assertAlmostEqual(result['seconds']['media'], 25.0, places=3)

    def test_gap_maps_to_degraded_not_zero(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            path = os.path.join(tmp, 'focus.jsonl')
            writer = session_agent.FeedWriter(path, boot_id=BOOT)
            writer.write_state('active', 1000.0)
            parsed = focus_feed.load_feed(path, feed_seq=0, now_bt=1000.0
                                          + focus_feed.FEED_STALE_SECONDS + 10)
            self.assertTrue(parsed['stale'])
            result = focus_feed.aggregate_focus(
                parsed, (1000.0, 1400.0), procs=[firefox_proc()],
                limits={'browsers': 60}, username='child', prefs={},
                now_bt=1000.0 + focus_feed.FEED_STALE_SECONDS + 10)
            self.assertFalse(result['billable'])
            self.assertEqual(result['total'], 0.0)


# ===========================================================================
# Transport grant (sudoers) — in-tree matcher, never live visudo
# ===========================================================================

class TestTransportGrant(unittest.TestCase):
    TEMPLATE = 'sudoers-parental-controls'

    def body(self):
        return dep.rendered_template(self.TEMPLATE)

    def drain(self, user='testchild', uid=1001):
        return focus_feed.drain_argv(user, uid)

    def test_grant_authorizes_the_exact_drain_argv(self):
        argv = ['sudo', 'systemd-run', '--machine=testchild@.host', '--user',
                '--wait', '--collect', 'cat',
                '/run/user/1001/parental-controls/focus.jsonl']
        self.assertTrue(dep.sudoers_authorizes(self.body(), argv))

    def test_code_uses_the_granted_argv(self):
        argv = self.drain()
        self.assertEqual(argv[0], 'sudo')
        self.assertEqual(argv[1], 'systemd-run')
        self.assertTrue(dep.sudoers_authorizes(self.body(), argv))

    def test_grant_present_for_sbin_and_bin(self):
        text = read(self.TEMPLATE)
        self.assertIn('/usr/sbin/systemd-run ^--machine=', text)
        self.assertIn('/usr/bin/systemd-run ^--machine=', text)
        self.assertIn('focus[.]jsonl$', text)

    def test_template_keeps_placeholder(self):
        self.assertIn('@SERVICE_USER@', read(self.TEMPLATE))

    def test_denies_cat_of_other_paths(self):
        argv = ['sudo', 'systemd-run', '--machine=testchild@.host', '--user',
                '--wait', '--collect', 'cat', '/etc/shadow']
        self.assertFalse(dep.sudoers_authorizes(self.body(), argv))

    def test_denies_other_payloads(self):
        argv = ['sudo', 'systemd-run', '--machine=testchild@.host', '--user',
                '--wait', '--collect', 'sh', '-c', 'id']
        self.assertFalse(dep.sudoers_authorizes(self.body(), argv))

    def test_denies_missing_machine_flag(self):
        argv = ['sudo', 'systemd-run', '--user', '--wait', '--collect', 'cat',
                '/run/user/1001/parental-controls/focus.jsonl']
        self.assertFalse(dep.sudoers_authorizes(self.body(), argv))

    def test_denies_feed_path_traversal(self):
        argv = ['sudo', 'systemd-run', '--machine=testchild@.host', '--user',
                '--wait', '--collect', 'cat',
                '/run/user/1001/parental-controls/../focus.jsonl']
        self.assertFalse(dep.sudoers_authorizes(self.body(), argv))

    def test_denies_machine_name_with_slash(self):
        argv = ['sudo', 'systemd-run', '--machine=test/child@.host', '--user',
                '--wait', '--collect', 'cat',
                '/run/user/1001/parental-controls/focus.jsonl']
        self.assertFalse(dep.sudoers_authorizes(self.body(), argv))

    def test_denies_other_feed_names(self):
        argv = ['sudo', 'systemd-run', '--machine=testchild@.host', '--user',
                '--wait', '--collect', 'cat',
                '/run/user/1001/parental-controls/secret.jsonl']
        self.assertFalse(dep.sudoers_authorizes(self.body(), argv))

    def test_drain_failure_is_not_an_empty_feed(self):
        def runner(argv, capture_output=True, text=True, timeout=None):
            return FakeProc(1, '', 'machine transport failed')

        text, error = focus_feed.drain_feed('testchild', 1001, runner=runner)
        self.assertIsNone(text)
        self.assertIn('machine transport', error)

    def test_drain_success_returns_text(self):
        def runner(argv, capture_output=True, text=True, timeout=None):
            return FakeProc(0, '{"v":1}\n', '')

        text, error = focus_feed.drain_feed('testchild', 1001, runner=runner)
        self.assertEqual(error, '')
        self.assertEqual(text, '{"v":1}\n')


# ===========================================================================
# Installer staging + user unit
# ===========================================================================

class TestInstallerAndUnit(unittest.TestCase):
    def test_installer_parses_session_user(self):
        script = read('install.sh')
        self.assertIn('--session-user) SESSION_USERS+=("$2")', script)
        self.assertIn('SESSION_USERS=()', script)

    def test_installer_has_disable_flag(self):
        self.assertIn('--no-session-agent', read('install.sh'))

    def test_installer_renders_app_dir_placeholder(self):
        script = read('install.sh')
        self.assertIn('systemd/parental-session-agent.service', script)
        self.assertIn('s|@APP_DIR@|$INSTALL_DIR|g', script)

    def test_installer_enables_linger(self):
        script = read('install.sh')
        self.assertIn('loginctl enable-linger "$session_user"', script)

    def test_installer_checks_agent_sources_present(self):
        script = read('install.sh')
        self.assertIn('session_agent.py focus_feed.py', script)

    def test_installer_no_checkout_paths(self):
        self.assertNotRegex(read('install.sh'),
                            r'(/home/|Documents/Projects)')

    def test_unit_uses_app_dir_placeholder(self):
        unit = read(os.path.join('systemd', 'parental-session-agent.service'))
        self.assertIn('@APP_DIR@/session_agent.py', unit)
        self.assertNotRegex(unit, r'/home/')

    def test_unit_is_a_user_unit_with_runtime_dir_and_hardening(self):
        unit = read(os.path.join('systemd', 'parental-session-agent.service'))
        self.assertIn('WantedBy=default.target', unit)
        self.assertIn('RuntimeDirectory=parental-controls', unit)
        self.assertIn('NoNewPrivileges=yes', unit)
        self.assertIn('ProtectSystem=strict', unit)
        self.assertIn('RestrictSUIDSGID=yes', unit)
        self.assertNotIn('@SERVICE_USER@', unit)

    def test_unit_runs_system_python_not_the_venv(self):
        # The agent is stdlib-only; the user unit must not depend on the
        # panel's venv (owned by the service user).
        unit = read(os.path.join('systemd', 'parental-session-agent.service'))
        self.assertIn('ExecStart=/usr/bin/python3', unit)
        self.assertNotIn('.venv', unit)


# ===========================================================================
# Static safety: the agent holds no secrets, calls no privileged tools
# ===========================================================================

class TestAgentStaticSafety(unittest.TestCase):
    ALLOWED_IMPORTS = {
        'argparse', 'json', 'os', 're', 'shutil', 'signal', 'subprocess',
        'sys', 'time', 'datetime',
    }
    FORBIDDEN_IMPORTS = {'auth', 'app', 'budgets', 'store', 'flask',
                         'requests', 'wtforms', 'audit'}

    def _tree(self, rel):
        return ast.parse(read(rel))

    def test_agent_imports_are_stdlib_only(self):
        for node in ast.walk(self._tree('session_agent.py')):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split('.')[0]
                    self.assertNotIn(root, self.FORBIDDEN_IMPORTS,
                                     f'agent must not import {root}')
                    self.assertIn(root, self.ALLOWED_IMPORTS,
                                  f'unexpected agent import {root}')
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or '').split('.')[0]
                self.assertNotIn(root, self.FORBIDDEN_IMPORTS)
                self.assertIn(root, self.ALLOWED_IMPORTS)

    def test_agent_never_reads_the_auth_store(self):
        # Scan string literals, skipping the module docstring (which names the
        # path only to say the agent never reads it).
        tree = self._tree('session_agent.py')
        docstring_nodes = set()
        if tree.body and isinstance(tree.body[0], ast.Expr) \
                and isinstance(tree.body[0].value, ast.Constant) \
                and isinstance(tree.body[0].value.value, str):
            docstring_nodes.add(id(tree.body[0].value))
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and id(n) not in docstring_nodes]
        for literal in literals:
            for forbidden in ('/etc/parental-controls', 'auth.json',
                              'state.db'):
                self.assertNotIn(forbidden, literal,
                                 f'agent literal references {forbidden}')

    def test_agent_never_invokes_sudo(self):
        for node in ast.walk(self._tree('session_agent.py')):
            if isinstance(node, ast.List) and node.elts:
                head = node.elts[0]
                if isinstance(head, ast.Constant) and head.value == 'sudo':
                    self.fail('session_agent.py must not build a sudo argv')
        self.assertNotIn("'sudo'", read('session_agent.py'))

    def test_panel_module_does_not_import_budgets_at_top_level(self):
        tree = self._tree('focus_feed.py')
        top_imports = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_imports.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                top_imports.add((node.module or '').split('.')[0])
        self.assertNotIn('budgets', top_imports,
                         'budgets must be imported lazily inside functions')

    def test_writer_emits_no_pii_keys(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, '.scratch')) \
                as tmp:
            writer = session_agent.FeedWriter(os.path.join(tmp, 'f.jsonl'),
                                              boot_id=BOOT)
            row = writer.write_focus('firefox', 'firefox.desktop', 'app',
                                     1, 0.0, 1.0)
        allowed = {'v', 'seq', 'agent', 'boot_id', 'ts', 'bt', 'type',
                   'session', 'wm_class', 'app_id', 'kind', 'since'}
        self.assertTrue(set(row).issubset(allowed),
                        f'unexpected feed keys: {set(row) - allowed}')
        for banned in ('caption', 'title', 'url', 'cmdline', 'args'):
            self.assertNotIn(banned, row)


if __name__ == '__main__':
    unittest.main()
