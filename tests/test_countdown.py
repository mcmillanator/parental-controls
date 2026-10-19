#!/usr/bin/env python3
"""countdown_state — the single source of truth for both countdown screens.

`app.countdown_state()` feeds the Time rules "Live countdown" card
(`_countdown_text`) and the Overview account cards (`index()` / `api_users()`).
These pin its shape and precedence so the two screens can never disagree, and
that a payload lacking `bedtime`/`bonus` degrades to no countdown.

Stdlib only. The bonus branch is a pure function of the payload; the bedtime
branch reads the clock through `minutes_until_bedtime`, so bedtime fixtures
that must land OUTSIDE the window derive it from `datetime.now()` — a fixed
window would make the suite clock-dependent (it failed every run between
21:00 and 07:00 local).
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app


def _user(bonus=None, bedtime=None):
    return {'bonus': bonus or {}, 'bedtime': bedtime or {}}


BONUS_ACTIVE = {'active': True, 'remaining_minutes': 90}
BONUS_NONE = {'active': False, 'remaining_minutes': None}
# Every weekday, so `minutes_until_bedtime` always finds a next window.
ALL_DAYS = {'start': '21:00', 'end': '07:00',
            'days': [0, 1, 2, 3, 4, 5, 6]}


def _window_ahead(minutes_ahead=180):
    """A next-week schedule window starting `minutes_ahead` after NOW.

    Used wherever the test needs `minutes_until_bedtime` to report a real
    wait: deriving the window from the clock keeps the assertion true at
    every wall-clock value, instead of only when the suite happens to run
    outside a hardcoded bedtime.
    """
    start = datetime.now() + timedelta(minutes=minutes_ahead)
    end = start + timedelta(minutes=60)
    return {'start': start.strftime('%H:%M'), 'end': end.strftime('%H:%M'),
            'days': [0, 1, 2, 3, 4, 5, 6]}


class CountdownStateTest(unittest.TestCase):
    def test_bonus_wins_over_the_bedtime_schedule(self):
        state = app.countdown_state(_user(BONUS_ACTIVE, ALL_DAYS))
        self.assertEqual({'kind': 'bonus', 'seconds': 90 * 60}, state)

    def test_bedtime_is_used_when_no_bonus_is_active(self):
        state = app.countdown_state(_user(BONUS_NONE, _window_ahead()))
        self.assertIsNotNone(state)
        self.assertEqual('bedtime', state['kind'])
        self.assertGreater(state['seconds'], 0)

    def test_no_bonus_and_no_schedule_is_none(self):
        self.assertIsNone(app.countdown_state(_user()))
        self.assertIsNone(app.countdown_state({}))
        self.assertIsNone(app.countdown_state(None))

    def test_an_active_bonus_without_remaining_falls_through(self):
        state = app.countdown_state(
            _user({'active': True, 'remaining_minutes': None}))
        self.assertIsNone(state)

    def test_a_remote_row_without_bonus_or_bedtime_degrades(self):
        # A remote agent /users row may carry neither key: never an error.
        self.assertIsNone(app.countdown_state({'username': 'kid'}))

    def test_zero_remaining_is_zero_not_none(self):
        state = app.countdown_state(
            _user({'active': True, 'remaining_minutes': 0}))
        self.assertEqual({'kind': 'bonus', 'seconds': 0}, state)

    def test_a_schedule_without_days_is_none(self):
        self.assertIsNone(app.countdown_state(
            _user(BONUS_NONE, {'start': '21:00', 'end': '07:00',
                               'days': []})))


class CountdownTextTest(unittest.TestCase):
    """``_countdown_text`` is built on ``countdown_state`` (one source)."""

    def test_text_matches_the_state(self):
        self.assertEqual('01:30:00',
                         app._countdown_text(_user(BONUS_ACTIVE, ALL_DAYS)))
        self.assertEqual('--:--:--', app._countdown_text(_user()))

    def test_format_seconds_clamps_at_zero(self):
        self.assertEqual('01:30:00', app._format_seconds(5400))
        self.assertEqual('00:00:00', app._format_seconds(0))
        self.assertEqual('00:00:00', app._format_seconds(-5))

    def test_format_countdown_keeps_the_placeholder(self):
        self.assertEqual('--:--:--', app._format_countdown(None))
        self.assertEqual('00:15:00', app._format_countdown(15))


if __name__ == '__main__':
    unittest.main()
