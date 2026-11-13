#!/usr/bin/env python3
"""Calendar schedule evaluator — tables, equivalence, migration, expand.

schedule.py is the pure core of the calendar refactor (CALENDAR_DESIGN.md):
one recurring "block" model replaces the bedtime table and the weekly
windows sub-doc. This module proves the core before any shim is wired in:

* normalize_block / preset grammar: fail-closed matrix;
* activity predicates across midnight, UNTIL edges, week/month boundaries;
* overlap validation (same-target only, adjacency allowed);
* migration converters: deterministic, exact legacy-shape mapping,
  disabled/dead windows NOT migrated;
* the EQUIVALENCE SAFETY NET: for EVERY old-shape bedtime/window config in
  the matrix, the frozen old predicates (copied verbatim below +
  budgets._in_window) agree with the migrated schedule across a sampled
  timeline.

Stdlib only, no I/O, no Flask, no VM capabilities needed.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import budgets          # facade is stdlib-only (no Flask)
import schedule

# ---------------------------------------------------------------------------
# Frozen reference implementations (the OLD predicates, verbatim from the
# pre-calendar master: app.is_in_bedtime / budgets_actions._in_window share
# this body). The equivalence net compares the NEW evaluator against these.
# ---------------------------------------------------------------------------

def oracle_in_window(entry, now):
    """Verbatim pre-calendar predicate (app.is_in_bedtime / _in_window)."""
    if not entry:
        return False
    start = schedule.parse_hhmm(entry.get('start', ''))
    end = schedule.parse_hhmm(entry.get('end', ''))
    try:
        days = {int(d) for d in entry.get('days', [])}
    except (ValueError, TypeError):
        return False
    if start is None or end is None or not days or start == end:
        return False
    cur = now.hour * 60 + now.minute
    today = now.weekday()
    yesterday = (today - 1) % 7
    if start < end:
        return today in days and start <= cur < end
    if today in days and cur >= start:
        return True
    if yesterday in days and cur < end:
        return True
    return False


def timeline(anchor, days=16, step_minutes=30):
    """Sampled wall-clock timeline from anchor for `days` days.

    16 days x 48 samples/day covers every weekday, both biweekly parities,
    midnight-crossing, month boundaries (choose the anchor) and start==end
    transitions.
    """
    out = []
    t = anchor
    end = anchor + timedelta(days=days)
    while t < end:
        out.append(t)
        t += timedelta(minutes=step_minutes)
    return out


ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
SCHOOL_DAYS = [0, 1, 2, 3, 4]

# Old-shape bedtime config matrix: valid, malformed and fail-closed rows.
BEDTIME_CONFIGS = [
    {'start': '21:00', 'end': '08:00', 'days': ALL_DAYS},       # overnight
    {'start': '20:00', 'end': '06:00', 'days': SCHOOL_DAYS},    # school nights
    {'start': '09:00', 'end': '17:00', 'days': [5, 6]},         # weekend day
    {'start': '12:00', 'end': '14:00', 'days': ALL_DAYS},       # same-day
    {'start': '23:30', 'end': '00:15', 'days': [0, 3]},         # crosses midnight
    {'start': '08:00', 'end': '08:00', 'days': ALL_DAYS},       # start==end
    {'start': '25:00', 'end': '08:00', 'days': ALL_DAYS},       # bad start
    {'start': '21:00', 'end': '8:00pm', 'days': ALL_DAYS},      # bad end
    {'start': '21:00', 'end': '08:00', 'days': []},             # no days
    {'start': '21:00', 'end': '08:00', 'days': ['x']},          # bad days
    {'start': '', 'end': '', 'days': ALL_DAYS},                 # empty times
    {},                                                         # empty entry
    {'start': '21:00', 'end': '08:00', 'days': [7, -1]},        # out of range
]

# Old-shape window configs (post-normalize_window shape): the ones with
# valid=False exercise the fail-closed path at the NORMALIZER level.
def _win(target_type='category', target='games', days=None, start='16:00',
         end='18:00', allowance=60, enabled=True, wid=None):
    return {
        'id': wid or 'w-%s-%s' % (target_type, target),
        'target_type': target_type, 'target': target,
        'days': days if days is not None else list(ALL_DAYS),
        'start': start, 'end': end,
        'allowance_minutes': allowance, 'enabled': enabled,
        'set_by': 'stan', 'set_at': '2026-11-01T10:00:00',
    }


WINDOW_CONFIGS = [
    _win(),
    _win(days=SCHOOL_DAYS),
    _win(start='22:00', end='02:00'),                 # overnight window
    _win(target_type='app', target='steam'),
    _win(start='23:30', end='00:30'),                 # crosses midnight
    _win(days=[5, 6], start='10:00', end='20:00'),    # weekend block
    _win(enabled=False),                              # NOT migrated
    _win(allowance=4),                                # sub-minimum
    _win(start='16:00', end='16:00'),                 # start == end
    _win(days=[]),                                    # no days
]


def valid_windows():
    """Old-shape windows that budgets.normalize_window accepts."""
    return [w for w in WINDOW_CONFIGS if budgets.normalize_window(w) is not None]


# ---------------------------------------------------------------------------
# Preset grammar
# ---------------------------------------------------------------------------

class PresetGrammarTest(unittest.TestCase):
    def test_simple_presets_round_trip(self):
        for text in ('ONE', 'DAILY', 'SCHOOL'):
            with self.subTest(preset=text):
                parsed = schedule.parse_preset(text)
                self.assertIsNotNone(parsed)
                self.assertEqual(schedule.format_preset(*parsed), text)

    def test_weekly_byday_canonicalizes_order(self):
        parsed = schedule.parse_preset('WEEKLY:BYDAY=FR,MO,WE')
        self.assertEqual(parsed, ('WEEKLY', (0, 2, 4)))
        self.assertEqual(schedule.format_preset(*parsed),
                         'WEEKLY:BYDAY=MO,WE,FR')

    def test_biweekly_byday(self):
        parsed = schedule.parse_preset('BIWEEKLY:BYDAY=SU')
        self.assertEqual(parsed, ('BIWEEKLY', (6,)))
        self.assertEqual(schedule.format_preset(*parsed), 'BIWEEKLY:BYDAY=SU')

    def test_lowercase_input_is_normalized(self):
        self.assertEqual(schedule.parse_preset('weekly:byday=mo,tu'),
                         ('WEEKLY', (0, 1)))

    def test_bad_presets_rejected(self):
        for text in ('', None, 'WEEKLY', 'WEEKLY:BYDAY=', 'WEEKLY:BYDAY=XX',
                     'WEEKLY:BYDAY=MO,MO', 'WEEKLY:BYDAY=MO,,TU', 'MONTHLY',
                     'DAILY:BYDAY=MO', 'weekly:byda=mo', 'ONE:BYDAY=MO'):
            with self.subTest(preset=text):
                self.assertIsNone(schedule.parse_preset(text))

    def test_preset_for_days_mapping(self):
        self.assertEqual(schedule.preset_for_days(ALL_DAYS), 'DAILY')
        self.assertEqual(schedule.preset_for_days(SCHOOL_DAYS), 'SCHOOL')
        self.assertEqual(schedule.preset_for_days([5, 6]),
                         'WEEKLY:BYDAY=SA,SU')
        self.assertEqual(schedule.preset_for_days([2]),
                         'WEEKLY:BYDAY=WE')
        for bad in ([], None, [7], ['x'], [1, 'a']):
            with self.subTest(days=bad):
                self.assertIsNone(schedule.preset_for_days(bad))


# ---------------------------------------------------------------------------
# normalize_block — fail-closed matrix
# ---------------------------------------------------------------------------

def _raw(**over):
    base = {'id': 'b-1', 'target': 'games', 'mode': 'permitted',
            'preset': 'DAILY', 'dtstart': '2026-01-01',
            'start': '16:00', 'end': '18:00', 'enabled': True}
    base.update(over)
    return base


class NormalizeBlockTest(unittest.TestCase):
    def test_permitted_block_normalizes(self):
        block = schedule.normalize_block(_raw())
        self.assertIsNotNone(block)
        self.assertEqual(block['allowance_minutes'], None)
        self.assertEqual(block['mode'], 'permitted')
        self.assertEqual(block['until'], None)

    def test_capped_block_needs_allowance(self):
        block = schedule.normalize_block(_raw(mode='capped',
                                              allowance_minutes=90))
        self.assertEqual(block['allowance_minutes'], 90)
        self.assertIsNone(schedule.normalize_block(_raw(mode='capped')))
        self.assertIsNone(schedule.normalize_block(_raw(mode='capped',
                                                        allowance_minutes=4)))
        self.assertIsNone(schedule.normalize_block(_raw(mode='capped',
                                                        allowance_minutes='x')))
        self.assertEqual(schedule.normalize_block(
            _raw(mode='capped', allowance_minutes=5))['allowance_minutes'], 5)

    def test_bedtime_target_mode_rules(self):
        self.assertIsNotNone(schedule.normalize_block(
            _raw(target='bedtime', mode=None)))
        self.assertIsNone(schedule.normalize_block(
            _raw(target='bedtime', mode='permitted')))
        self.assertIsNone(schedule.normalize_block(
            _raw(target='bedtime', mode='capped', allowance_minutes=30)))

    def test_targets(self):
        for good in ('bedtime', 'games', 'browsers', 'media', 'chat',
                     'other', 'app:firefox', 'app:steam_1'):
            with self.subTest(target=good):
                self.assertIsNotNone(schedule.normalize_block(
                    _raw(target=good, mode=None if good == 'bedtime'
                         else 'permitted')))
        for bad in ('', 'games2', 'bedtime:', 'app:', 'app:bad comm!',
                    'APP:firefox', 'custom', None, 5):
            with self.subTest(target=bad):
                self.assertIsNone(schedule.normalize_block(
                    _raw(target=bad, mode=None)))

    def test_time_validation(self):
        self.assertIsNone(schedule.normalize_block(_raw(start='24:00')))
        self.assertIsNone(schedule.normalize_block(_raw(end='noon')))
        self.assertIsNone(schedule.normalize_block(_raw(start='16:00',
                                                        end='16:00')))
        self.assertIsNotNone(schedule.normalize_block(_raw(start='22:00',
                                                           end='02:00')))

    def test_times_canonicalized(self):
        block = schedule.normalize_block(_raw(start='9:5', end='05:30'))
        self.assertEqual((block['start'], block['end']), ('09:05', '05:30'))

    def test_dtstart_required_until_valid(self):
        self.assertIsNone(schedule.normalize_block(_raw(dtstart=None)))
        self.assertIsNone(schedule.normalize_block(_raw(dtstart='13-11-2026')))
        self.assertIsNone(schedule.normalize_block(_raw(until='garbage')))
        self.assertIsNone(schedule.normalize_block(_raw(dtstart='2026-11-13',
                                                        until='2026-11-12')))
        self.assertIsNotNone(schedule.normalize_block(_raw(until='2026-12-24')))
        self.assertIsNotNone(schedule.normalize_block(
            _raw(dtstart='2026-11-13', until='2026-11-13')))

    def test_one_requires_dtstart_as_occurrence(self):
        block = schedule.normalize_block(_raw(preset='ONE',
                                              dtstart='2026-12-24'))
        self.assertEqual(block['dtstart'], '2026-12-24')

    def test_id_rules(self):
        self.assertIsNone(schedule.normalize_block(_raw(id='')))
        self.assertIsNone(schedule.normalize_block(_raw(id='has space')))
        self.assertIsNone(schedule.normalize_block(_raw(id='x' * 65)))
        self.assertIsNotNone(schedule.normalize_block(_raw(id='w-category-'
                                                           'games')))
        # block_id override wins (iter_blocks passes the dict key)
        block = schedule.normalize_block(_raw(id='ignored'),
                                         block_id='real-id')
        self.assertEqual(block['id'], 'real-id')

    def test_garbage_inputs(self):
        for bad in (None, [], 'x', 7, {}):
            with self.subTest(raw=bad):
                self.assertIsNone(schedule.normalize_block(bad))

    def test_set_by_set_at_pass_through(self):
        block = schedule.normalize_block(_raw(set_by='stan',
                                              set_at='2026-11-13T10:00:00'))
        self.assertEqual(block['set_by'], 'stan')
        self.assertEqual(block['set_at'], '2026-11-13T10:00:00')


# ---------------------------------------------------------------------------
# Activity predicates
# ---------------------------------------------------------------------------

MON_2026_11_09 = datetime(2026, 11, 9)     # a Monday


def _blk(**over):
    block = schedule.normalize_block(_raw(**over))
    assert block is not None, over
    return block


class ActivityTest(unittest.TestCase):
    def test_daily_same_day_window(self):
        block = _blk(start='16:00', end='18:00')
        self.assertFalse(schedule.is_block_active(
            block, MON_2026_11_09.replace(hour=15, minute=59)))
        self.assertTrue(schedule.is_block_active(
            block, MON_2026_11_09.replace(hour=16)))
        self.assertTrue(schedule.is_block_active(
            block, MON_2026_11_09.replace(hour=17, minute=59)))
        self.assertFalse(schedule.is_block_active(
            block, MON_2026_11_09.replace(hour=18)))

    def test_midnight_crossing_tail_belongs_to_anchor_day(self):
        block = _blk(start='22:00', end='02:00')
        tue = MON_2026_11_09 + timedelta(days=1)
        self.assertTrue(schedule.is_block_active(
            block, MON_2026_11_09.replace(hour=23)))
        self.assertTrue(schedule.is_block_active(
            block, tue.replace(hour=1, minute=59)))
        self.assertFalse(schedule.is_block_active(
            block, tue.replace(hour=2)))

    def test_disabled_block_never_active(self):
        block = _blk(start='00:00', end='23:59', enabled=False)
        self.assertFalse(schedule.is_block_active(
            block, MON_2026_11_09.replace(hour=12)))

    def test_school_skips_weekend(self):
        block = _blk(preset='SCHOOL', start='20:00', end='06:00')
        self.assertTrue(schedule.is_block_active(
            block, datetime(2026, 11, 13, 21, 0)))    # Fri night
        self.assertTrue(schedule.is_block_active(
            block, datetime(2026, 11, 14, 5, 0)))     # Sat tail of Fri night
        self.assertFalse(schedule.is_block_active(
            block, datetime(2026, 11, 14, 21, 0)))    # Sat night -> no Sun block
        self.assertFalse(schedule.is_block_active(
            block, datetime(2026, 11, 15, 21, 0)))    # Sun night
        # Mon 05:00 is NOT a school-night tail (Sunday did not anchor).
        self.assertFalse(schedule.is_block_active(
            block, datetime(2026, 11, 16, 5, 0)))

    def test_weekly_byday_only_selected_weekdays(self):
        block = _blk(preset='WEEKLY:BYDAY=TU,TH', start='17:00', end='19:00')
        days = {9: False, 10: True, 11: False, 12: True, 13: False,
                14: False, 15: False}   # Mon..Sun of the anchor week
        for day, want in days.items():
            with self.subTest(day=day):
                self.assertEqual(
                    schedule.is_block_active(
                        block, datetime(2026, 11, day, 18)),
                    want)

    def test_biweekly_anchored_on_dtstart_week(self):
        block = _blk(preset='BIWEEKLY:BYDAY=WE', dtstart='2026-11-11',
                     start='17:00', end='19:00')  # Wed before anchor? anchor Wed 11
        for day, want in ((11, True), (18, False), (25, True),
                          (2, False), (30, False)):   # Nov 2 is before dtstart
            with self.subTest(day=day):
                self.assertEqual(
                    schedule.is_block_active(block,
                                             datetime(2026, 11, day, 18)),
                    want, 'Nov %d expected %s' % (day, want))

    def test_biweekly_weekly_even_at_month_boundary(self):
        block = _blk(preset='BIWEEKLY:BYDAY=MO', dtstart='2026-10-26',
                     start='17:00', end='19:00')
        for stamp, want in ((datetime(2026, 10, 26, 18), True),
                            (datetime(2026, 11, 2, 18), False),
                            (datetime(2026, 11, 9, 18), True),
                            (datetime(2026, 12, 7, 18), True)):
            with self.subTest(stamp=stamp):
                self.assertEqual(schedule.is_block_active(block, stamp), want)

    def test_one_single_occurrence(self):
        block = _blk(preset='ONE', dtstart='2026-12-24', start='10:00',
                     end='14:00')
        self.assertFalse(schedule.is_block_active(
            block, datetime(2026, 12, 24, 9)))
        self.assertTrue(schedule.is_block_active(
            block, datetime(2026, 12, 24, 12)))
        self.assertFalse(schedule.is_block_active(
            block, datetime(2026, 12, 25, 12)))
        self.assertFalse(schedule.is_block_active(
            block, datetime(2026, 12, 23, 12)))

    def test_one_crossing_midnight(self):
        block = _blk(preset='ONE', dtstart='2026-12-24', start='22:00',
                     end='02:00')
        self.assertTrue(schedule.is_block_active(
            block, datetime(2026, 12, 25, 1, 59)))
        self.assertFalse(schedule.is_block_active(
            block, datetime(2026, 12, 25, 2, 0)))

    def test_until_is_inclusive_on_the_anchor_day(self):
        block = _blk(start='20:00', end='06:00', until='2026-11-13')
        self.assertTrue(schedule.is_block_active(
            block, datetime(2026, 11, 13, 21)))     # anchor day itself
        self.assertTrue(schedule.is_block_active(
            block, datetime(2026, 11, 14, 5)))      # tail of the last anchor
        self.assertFalse(schedule.is_block_active(
            block, datetime(2026, 11, 14, 21)))     # next anchor past until
        self.assertFalse(schedule.is_block_active(
            block, datetime(2026, 11, 15, 5)))

    def test_dtstart_is_inclusive(self):
        block = _blk(dtstart='2026-11-20', start='20:00', end='22:00')
        self.assertFalse(schedule.is_block_active(
            block, datetime(2026, 11, 19, 21)))
        self.assertTrue(schedule.is_block_active(
            block, datetime(2026, 11, 20, 21)))

    def test_malformed_block_never_active(self):
        for bad in ({}, None, {'enabled': True, 'start': 'x', 'end': 'y',
                               'preset': 'DAILY', 'dtstart': '2026-01-01'},
                    'string', []):
            with self.subTest(block=bad):
                self.assertFalse(schedule.is_block_active(
                    bad, MON_2026_11_09.replace(hour=17)))


# ---------------------------------------------------------------------------
# Doc readers — fail-closed on garbage
# ---------------------------------------------------------------------------

class DocReaderTest(unittest.TestCase):
    def _doc(self, *blocks):
        return {'events': {b['id']: b for b in blocks},
                'migrated': '2026-11-13T08:00:00'}

    def test_iter_blocks_skips_malformed(self):
        good = _blk(id='g', start='10:00', end='11:00')
        doc = self._doc(good)
        doc['events']['bad'] = {'target': 'nope'}
        doc['events']['also_bad'] = 7
        ids = [bid for bid, _b in schedule.iter_blocks(doc)]
        self.assertEqual(ids, ['g'])

    def test_garbage_docs_yield_nothing(self):
        for doc in (None, {}, [], {'events': 'x'}, {'events': []}, 7, ''):
            with self.subTest(doc=doc):
                self.assertEqual(list(schedule.iter_blocks(doc)), [])

    def test_doc_is_migrated(self):
        self.assertFalse(schedule.doc_is_migrated({}))
        self.assertFalse(schedule.doc_is_migrated(None))
        self.assertFalse(schedule.doc_is_migrated({'events': {}}))
        self.assertTrue(schedule.doc_is_migrated(
            {'events': {}, 'migrated': '2026-11-13T08:00:00'}))

    def test_active_blocks_and_in_bedtime(self):
        bt = _blk(id='bt', target='bedtime', mode=None, start='21:00',
                  end='07:00')
        games = _blk(id='g', target='games', mode='capped',
                     allowance_minutes=30, start='16:00', end='18:00')
        doc = self._doc(bt, games)
        self.assertTrue(schedule.in_bedtime(doc, datetime(2026, 11, 9, 22)))
        self.assertFalse(schedule.in_bedtime(doc, datetime(2026, 11, 9, 17)))
        active = schedule.active_blocks(doc, 'category', 'games',
                                        datetime(2026, 11, 9, 17))
        self.assertEqual([b['id'] for b in active], ['g'])
        capped = schedule.active_capped_block(doc, 'category', 'games',
                                              datetime(2026, 11, 9, 17))
        self.assertEqual(capped['allowance_minutes'], 30)
        self.assertIsNone(schedule.active_capped_block(
            doc, 'category', 'games', datetime(2026, 11, 9, 19)))
        self.assertIsNone(schedule.active_capped_block(
            doc, 'category', 'media', datetime(2026, 11, 9, 17)))
        self.assertEqual(
            schedule.active_blocks(doc, 'bogus', 'x',
                                   datetime(2026, 11, 9, 17)), [])
        self.assertEqual(
            schedule.active_blocks(doc, 'app', 'games',
                                   datetime(2026, 11, 9, 17)), [])

    def test_target_pair_and_legacy_app_target(self):
        app_blk = _blk(id='a', target='app:steam', mode='permitted',
                       start='16:00', end='18:00')
        doc = self._doc(app_blk)
        self.assertTrue(schedule.permitted_active(doc, 'app', 'steam',
                                                  datetime(2026, 11, 9, 17)))
        self.assertFalse(schedule.permitted_active(doc, 'category', 'steam',
                                                   datetime(2026, 11, 9, 17)))

    def test_has_enabled_blocks_gating_probe(self):
        off = _blk(id='off', target='games', mode='permitted',
                   start='16:00', end='18:00', enabled=False)
        doc = self._doc(off)
        self.assertFalse(schedule.has_enabled_blocks(doc, 'category',
                                                     'games'))
        on = _blk(id='on', target='games', mode='permitted',
                  start='16:00', end='18:00')
        doc['events']['on'] = on
        self.assertTrue(schedule.has_enabled_blocks(doc, 'category',
                                                    'games'))

    def test_in_bedtime_malformed_block_is_absent(self):
        doc = {'events': {'bad': {'target': 'bedtime', 'enabled': True,
                                  'preset': 'DAILY'}},
               'migrated': 'x'}
        self.assertFalse(schedule.in_bedtime(doc, datetime(2026, 11, 9, 22)))


# ---------------------------------------------------------------------------
# Migration converters
# ---------------------------------------------------------------------------

T_MIGRATE = datetime(2026, 11, 13, 8, 30)   # a Friday


class MigrationTest(unittest.TestCase):
    def test_bedtime_full_week_becomes_daily(self):
        blocks = schedule.blocks_from_bedtime_entry(
            {'start': '21:00', 'end': '08:00', 'days': ALL_DAYS}, T_MIGRATE)
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block['target'], 'bedtime')
        self.assertIsNone(block['mode'])
        self.assertEqual(block['preset'], 'DAILY')
        # Anchored the day before so an in-progress overnight tail keeps
        # locking at cutover:
        self.assertEqual(block['dtstart'], '2026-11-12')
        self.assertEqual((block['start'], block['end']), ('21:00', '08:00'))
        self.assertTrue(block['enabled'])

    def test_bedtime_school_days_become_school(self):
        blocks = schedule.blocks_from_bedtime_entry(
            {'start': '20:00', 'end': '06:00', 'days': SCHOOL_DAYS},
            T_MIGRATE)
        self.assertEqual(blocks[0]['preset'], 'SCHOOL')

    def test_bedtime_subset_becomes_weekly_byday(self):
        blocks = schedule.blocks_from_bedtime_entry(
            {'start': '09:00', 'end': '17:00', 'days': [5, 6]}, T_MIGRATE)
        self.assertEqual(blocks[0]['preset'], 'WEEKLY:BYDAY=SA,SU')

    def test_bedtime_malformed_or_absent_migrates_to_nothing(self):
        for entry in (None, {}, {'start': '21:00'}, {'start': '21:00',
                                                     'end': '21:00',
                                                     'days': ALL_DAYS},
                      {'start': 'x', 'end': 'y', 'days': ALL_DAYS},
                      {'start': '21:00', 'end': '08:00', 'days': []}):
            with self.subTest(entry=entry):
                self.assertEqual(
                    schedule.blocks_from_bedtime_entry(entry, T_MIGRATE), [])

    def test_window_keeps_id_and_allowance(self):
        win = budgets.normalize_window(_win(days=SCHOOL_DAYS,
                                            start='22:00', end='02:00',
                                            allowance=45))
        blocks = schedule.blocks_from_windows([win], T_MIGRATE)
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block['id'], 'w-category-games')
        self.assertEqual(block['target'], 'games')
        self.assertEqual(block['mode'], 'capped')
        self.assertEqual(block['allowance_minutes'], 45)
        self.assertEqual(block['preset'], 'SCHOOL')
        self.assertEqual((block['start'], block['end']), ('22:00', '02:00'))

    def test_app_window_maps_to_app_target(self):
        win = budgets.normalize_window(_win(target_type='app',
                                            target='steam'))
        blocks = schedule.blocks_from_windows([win], T_MIGRATE)
        self.assertEqual(blocks[0]['target'], 'app:steam')

    def test_disabled_windows_not_migrated(self):
        win = budgets.normalize_window(_win(enabled=False))
        self.assertEqual(schedule.blocks_from_windows([win], T_MIGRATE), [])

    def test_dead_windows_not_migrated(self):
        # normalize_window returns None for these: they never reach the
        # converter as windows; garbage dicts are rejected too.
        self.assertEqual(schedule.blocks_from_windows(
            [{'target_type': 'category', 'target': 'nope', 'id': 'x',
              'days': ALL_DAYS, 'start': '16:00', 'end': '18:00',
              'allowance_minutes': 60, 'enabled': True}], T_MIGRATE), [])
        self.assertEqual(schedule.blocks_from_windows(None, T_MIGRATE), [])

    def test_migrate_doc_shape_and_determinism(self):
        entry = {'start': '21:00', 'end': '08:00', 'days': SCHOOL_DAYS}
        wins = [budgets.normalize_window(_win()),
                budgets.normalize_window(_win(enabled=False))]
        doc = schedule.migrate(entry, wins, T_MIGRATE)
        self.assertEqual(doc['migrated'], '2026-11-13T08:30:00')
        self.assertEqual(sorted(doc['events']),
                         ['b-bedtime-1', 'w-category-games'])
        again = schedule.migrate(entry, wins, T_MIGRATE)
        self.assertEqual(doc, again)   # re-runnable, deterministic
        self.assertEqual(sorted(doc['events']) ,
                         sorted(again['events']))

    def test_migrate_empty_config_still_marks_migrated(self):
        doc = schedule.migrate(None, [], T_MIGRATE)
        self.assertEqual(doc['events'], {})
        self.assertTrue(schedule.doc_is_migrated(doc))

    def test_migration_conflict_bedtime_id_reserved(self):
        # A legacy window can never own 'b-bedtime-1' (ids are
        # w-category-*/w-app-*), and migrate() puts bedtime first.
        doc = schedule.migrate({'start': '21:00', 'end': '08:00',
                                'days': ALL_DAYS},
                               [budgets.normalize_window(_win())], T_MIGRATE)
        self.assertEqual(doc['events']['b-bedtime-1']['target'], 'bedtime')
        self.assertEqual(len(doc['events']), 2)


# ---------------------------------------------------------------------------
# Equivalence safety net — OLD predicate vs NEW evaluator on migrated docs
# ---------------------------------------------------------------------------

class EquivalenceTest(unittest.TestCase):
    """For EVERY old-shape config the OLD predicate and the migrated
    schedule agree across a sampled 16-day timeline (month boundary
    anchor, midnight-crossing and day transitions inside)."""

    ANCHOR = datetime(2026, 10, 26, 0, 0)   # mid-month -> crosses Nov 1

    def _samples(self):
        return timeline(self.ANCHOR, days=16, step_minutes=30)

    def test_bedtime_configs_equivalent(self):
        samples = self._samples()
        self.assertEqual(len(samples), 16 * 48)
        checked = 0
        for entry in BEDTIME_CONFIGS:
            with self.subTest(entry=entry):
                doc = schedule.migrate(entry, [], self.ANCHOR)
                for now in samples:
                    checked += 1
                    self.assertEqual(
                        oracle_in_window(entry, now),
                        schedule.in_bedtime(doc, now),
                        'bedtime divergence at %s for %r' % (now, entry))
        self.assertGreater(checked, 12 * 16 * 48)

    def test_window_configs_equivalent(self):
        samples = self._samples()
        pairs = valid_windows()
        self.assertGreaterEqual(len(pairs), 6)
        checked = 0
        for win in pairs:
            with self.subTest(window=win['id'], start=win['start'],
                              end=win['end'], days=win['days']):
                doc = schedule.migrate(None, [win], self.ANCHOR)
                for now in samples:
                    checked += 1
                    want = bool(win['enabled']) and oracle_in_window(win, now)
                    got = schedule.active_capped_block(
                        doc, win['target_type'], win['target'], now)
                    self.assertEqual(
                        want, got is not None,
                        'window divergence at %s for %r' % (now, win))
        self.assertGreater(checked, 6 * 16 * 48)

    def test_window_predicate_via_budgets_in_window(self):
        # budgets._in_window IS the enforcement-side old predicate; pin the
        # migrated evaluator against it directly for the same windows.
        samples = timeline(self.ANCHOR, days=10, step_minutes=15)
        for win in valid_windows():
            if not win.get('enabled', True):
                continue   # _in_window ignores 'enabled'; blocks do not
            block = schedule.window_to_block(
                win, self.ANCHOR.date())
            self.assertIsNotNone(block)
            for now in samples:
                self.assertEqual(
                    budgets._in_window(win, now),
                    schedule.is_block_active(block, now),
                    'divergence at %s for %r' % (now, win))

    def test_in_bedtime_entry_matches_legacy_body(self):
        # The relocated legacy fallback predicate must equal the frozen
        # oracle for every old-shape bedtime entry (non-migrated users).
        samples = timeline(self.ANCHOR, days=8, step_minutes=30)
        for entry in BEDTIME_CONFIGS:
            for now in samples:
                self.assertEqual(
                    oracle_in_window(entry, now),
                    schedule.in_bedtime_entry(entry, now),
                    'fallback divergence at %s for %r' % (now, entry))

    def test_migrated_doc_matches_fallback_after_migration(self):
        # Once migrated, in_bedtime(doc) and in_bedtime_entry(entry) agree
        # for every config the migration source accepts.
        samples = timeline(self.ANCHOR, days=8, step_minutes=30)
        for entry in BEDTIME_CONFIGS:
            doc = schedule.migrate(entry, [], self.ANCHOR)
            for now in samples:
                self.assertEqual(
                    schedule.in_bedtime_entry(entry, now),
                    schedule.in_bedtime(doc, now),
                    'cutover divergence at %s for %r' % (now, entry))


# ---------------------------------------------------------------------------
# Overlap validation
# ---------------------------------------------------------------------------

class OverlapTest(unittest.TestCase):
    def _blocks(self, *specs):
        out = []
        for i, spec in enumerate(specs):
            block = _blk(id='b-%d' % i, **spec)
            out.append(block)
        return out

    def test_same_target_daily_overlap_rejected(self):
        existing = self._blocks({'target': 'games',
                                 'start': '16:00', 'end': '18:00'})
        cand = _blk(id='new', target='games', start='17:00', end='19:00')
        self.assertEqual(schedule.find_overlap(existing, cand), 'b-0')

    def test_adjacent_blocks_allowed(self):
        existing = self._blocks({'target': 'games',
                                 'start': '16:00', 'end': '18:00'})
        for spec, label in (({'start': '18:00', 'end': '20:00'}, 'after'),
                            ({'start': '14:00', 'end': '16:00'}, 'before')):
            cand = _blk(id='new', target='games', **spec)
            with self.subTest(edge=label):
                self.assertIsNone(schedule.find_overlap(existing, cand))

    def test_different_targets_never_conflict(self):
        existing = self._blocks({'target': 'games',
                                 'start': '16:00', 'end': '18:00'})
        cand = _blk(id='new', target='media', start='16:00', end='18:00')
        self.assertIsNone(schedule.find_overlap(existing, cand))

    def test_disjoint_weekdays_allowed(self):
        existing = self._blocks({'target': 'games', 'preset':
                                 'WEEKLY:BYDAY=MO',
                                 'start': '16:00', 'end': '18:00'})
        cand = _blk(id='new', target='games',
                    preset='WEEKLY:BYDAY=TU', start='16:00', end='18:00')
        self.assertIsNone(schedule.find_overlap(existing, cand))

    def test_weekend_only_candidate_vs_school_series_allowed(self):
        existing = self._blocks({'target': 'games', 'preset': 'SCHOOL',
                                 'start': '16:00', 'end': '18:00'})
        cand = _blk(id='new', target='games', preset='WEEKLY:BYDAY=SU',
                    start='16:00', end='18:00')
        self.assertIsNone(schedule.find_overlap(existing, cand))
        cand2 = _blk(id='new2', target='games', preset='WEEKLY:BYDAY=SA,SU',
                     start='17:00', end='19:00')
        self.assertIsNone(schedule.find_overlap(existing, cand2))

    def test_disabled_existing_blocks_do_not_conflict(self):
        existing = self._blocks({'target': 'games', 'start': '16:00',
                                 'end': '18:00', 'enabled': False})
        cand = _blk(id='new', target='games', start='17:00', end='19:00')
        self.assertIsNone(schedule.find_overlap(existing, cand))

    def test_disabled_candidate_never_conflicts(self):
        existing = self._blocks({'target': 'games',
                                 'start': '16:00', 'end': '18:00'})
        cand = _blk(id='new', target='games', start='17:00', end='19:00',
                    enabled=False)
        self.assertIsNone(schedule.find_overlap(existing, cand))

    def test_until_separates_series(self):
        existing = self._blocks({'target': 'games', 'start': '16:00',
                                 'end': '18:00', 'until': '2026-11-20'})
        cand = _blk(id='new', target='games', start='17:00', end='19:00',
                    dtstart='2026-11-25')
        self.assertIsNone(schedule.find_overlap(existing, cand))
        cand2 = _blk(id='new2', target='games', start='17:00', end='19:00',
                     dtstart='2026-11-18')
        self.assertEqual(schedule.find_overlap(existing, cand2), 'b-0')

    def test_midnight_crossing_overlap_detected(self):
        existing = self._blocks({'target': 'games', 'start': '22:00',
                                 'end': '02:00'})
        cand = _blk(id='new', target='games', start='01:00', end='03:00')
        self.assertEqual(schedule.find_overlap(existing, cand), 'b-0')

    def test_cross_anchor_tail_overlap_detected(self):
        # A: 22:00-02:00 daily; B: 23:30-01:30 on Sundays only. On the
        # Sunday->Monday boundary both the same-day and cross-day
        # intersections hold; conflict must be found.
        existing = self._blocks({'target': 'games', 'start': '22:00',
                                 'end': '02:00'})
        cand = _blk(id='new', target='games', preset='WEEKLY:BYDAY=SU',
                    start='23:30', end='01:30')
        self.assertEqual(schedule.find_overlap(existing, cand), 'b-0')

    def test_biweekly_parity_mismatch_allowed(self):
        a = _blk(id='a', target='games', preset='BIWEEKLY:BYDAY=MO',
                 dtstart='2026-11-02', start='16:00', end='18:00')
        b = _blk(id='b', target='games', preset='BIWEEKLY:BYDAY=MO',
                 dtstart='2026-11-09', start='16:00', end='18:00')
        self.assertIsNone(schedule.find_overlap([a], b))
        c = _blk(id='c', target='games', preset='BIWEEKLY:BYDAY=MO',
                 dtstart='2026-11-16', start='17:00', end='19:00')
        self.assertEqual(schedule.find_overlap([a], c), 'a')

    def test_one_vs_one_same_day(self):
        a = _blk(id='a', target='games', preset='ONE',
                 dtstart='2026-12-24', start='10:00', end='12:00')
        b = _blk(id='b', target='games', preset='ONE',
                 dtstart='2026-12-24', start='11:00', end='13:00')
        c = _blk(id='c', target='games', preset='ONE',
                 dtstart='2026-12-25', start='11:00', end='13:00')
        self.assertEqual(schedule.find_overlap([a], b), 'a')
        self.assertIsNone(schedule.find_overlap([a], c))

    def test_bedtime_vs_bedtime_conflicts(self):
        a = _blk(id='a', target='bedtime', mode=None, start='21:00',
                 end='07:00')
        b = _blk(id='b', target='bedtime', mode=None, start='22:00',
                 end='06:00')
        self.assertEqual(schedule.find_overlap([a], b), 'a')


# ---------------------------------------------------------------------------
# expand() — concrete intervals for a date range
# ---------------------------------------------------------------------------

class ExpandTest(unittest.TestCase):
    def test_daily_window_across_week(self):
        block = _blk(start='16:00', end='18:00', dtstart='2026-11-09')
        rows = schedule.expand(datetime(2026, 11, 9), datetime(2026, 11, 16),
                               [block])
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0]['start'], '2026-11-09T16:00:00')
        self.assertEqual(rows[0]['end'], '2026-11-09T18:00:00')
        self.assertEqual(rows[-1]['start'], '2026-11-15T16:00:00')

    def test_midnight_crossing_emits_full_interval(self):
        block = _blk(start='22:00', end='06:00', dtstart='2026-11-09')
        rows = schedule.expand(datetime(2026, 11, 9), datetime(2026, 11, 12),
                               [block])
        self.assertEqual([(r['start'], r['end']) for r in rows],
                         [('2026-11-09T22:00:00', '2026-11-10T06:00:00'),
                          ('2026-11-10T22:00:00', '2026-11-11T06:00:00'),
                          ('2026-11-11T22:00:00', '2026-11-12T06:00:00')])

    def test_yesterday_tail_included_when_range_starts_midnight(self):
        block = _blk(start='22:00', end='06:00', dtstart='2026-11-09')
        rows = schedule.expand(datetime(2026, 11, 11), datetime(2026, 11, 12),
                               [block])
        # Nov-10 anchor bleeds into Nov-11 00:00-06:00 AND Nov-11 anchor.
        self.assertEqual([r['start'] for r in rows],
                         ['2026-11-10T22:00:00', '2026-11-11T22:00:00'])

    def test_month_boundary(self):
        block = _blk(preset='DAILY', start='16:00', end='18:00',
                     dtstart='2026-10-30')
        rows = schedule.expand(datetime(2026, 10, 30),
                               datetime(2026, 11, 3), [block])
        self.assertEqual(len(rows), 4)   # Oct 30, 31, Nov 1, 2
        self.assertEqual(rows[2]['start'], '2026-11-01T16:00:00')

    def test_until_edge_last_occurrence(self):
        block = _blk(start='16:00', end='18:00', dtstart='2026-11-09',
                     until='2026-11-11')
        rows = schedule.expand(datetime(2026, 11, 9),
                               datetime(2026, 11, 15), [block])
        self.assertEqual(len(rows), 3)   # 9,10,11 — until inclusive
        self.assertEqual(rows[-1]['start'], '2026-11-11T16:00:00')

    def test_biweekly_occurs_every_other_week(self):
        block = _blk(preset='BIWEEKLY:BYDAY=WE', dtstart='2026-11-04',
                     start='17:00', end='19:00')
        rows = schedule.expand(datetime(2026, 11, 1), datetime(2026, 12, 1),
                               [block])
        # Wed Nov 4 anchors (week 0); Nov 11 is the odd week; Nov 18 is
        # week 2; Nov 25 odd again.
        self.assertEqual([r['start'] for r in rows],
                         ['2026-11-04T17:00:00', '2026-11-18T17:00:00'],
                         rows)

    def test_one_single_row(self):
        block = _blk(preset='ONE', dtstart='2026-11-11', start='10:00',
                     end='14:00')
        rows = schedule.expand(datetime(2026, 11, 9), datetime(2026, 11, 13),
                               [block])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['block_id'], 'b-1')
        self.assertEqual(rows[0]['target'], 'games')
        self.assertEqual(rows[0]['mode'], 'permitted')
        self.assertIsNone(rows[0]['allowance_minutes'])

    def test_sorted_across_blocks(self):
        a = _blk(id='a', start='20:00', end='21:00', dtstart='2026-11-09')
        b = _blk(id='b', start='08:00', end='09:00', dtstart='2026-11-09')
        rows = schedule.expand(datetime(2026, 11, 9), datetime(2026, 11, 10),
                               [a, b])
        self.assertEqual([r['block_id'] for r in rows], ['b', 'a'])

    def test_disabled_blocks_not_expanded(self):
        block = _blk(start='16:00', end='18:00', dtstart='2026-11-09',
                     enabled=False)
        self.assertEqual(schedule.expand(datetime(2026, 11, 9),
                                         datetime(2026, 11, 10), [block]),
                         [])

    def test_garbage_inputs_return_empty(self):
        block = _blk(start='16:00', end='18:00', dtstart='2026-11-09')
        for args in (('x', 'y', [block]),
                     (datetime(2026, 11, 9), datetime(2026, 11, 8), [block]),
                     (datetime(2026, 11, 9), datetime(2026, 11, 10), None),
                     (datetime(2026, 11, 9), datetime(2026, 11, 10),
                      ['garbage']),
                     (datetime(2026, 11, 9),
                      datetime(2026, 11, 9) + timedelta(days=900), [block])):
            with self.subTest(args=args):
                self.assertEqual(schedule.expand(*args), [])

    def test_expand_accepts_doc(self):
        block = _blk(start='16:00', end='18:00', dtstart='2026-11-09')
        doc = {'events': {'b-1': block}, 'migrated': 'x'}
        rows = schedule.expand(datetime(2026, 11, 9), datetime(2026, 11, 11),
                               doc)
        self.assertEqual(len(rows), 2)

    def test_malformed_block_never_expands(self):
        rows = schedule.expand(datetime(2026, 11, 9), datetime(2026, 11, 10),
                               [{'id': 'x', 'target': 'games',
                                 'enabled': True, 'preset': 'DAILY',
                                 'dtstart': '2026-11-01',
                                 'start': '16:00', 'end': '16:00'}])
        self.assertEqual(rows, [])


class ConstantMirrorTest(unittest.TestCase):
    """The pure core mirrors literals defined in budgets; pin them."""

    def test_categories_match_budgets(self):
        self.assertEqual(schedule.CATEGORIES, budgets.CATEGORIES)

    def test_min_minutes_match_budgets(self):
        self.assertEqual(schedule.WINDOW_MIN_MINUTES,
                         budgets.WINDOW_MIN_MINUTES)


if __name__ == '__main__':
    unittest.main()
