#!/usr/bin/env python3
"""Budget accounting must NOT elapse while an account is LOCKED.

usermod -L (manual Lock, Lock Screen, bedtime auto-lock) all surface via
get_lock_status as locked==True. While locked the child cannot reach the
app, so that user's tick bills nothing and makes no pause/notify/minutes-
left decisions; already-paused/frozen state simply persists. The
accounting baselines (last_cpu, last_tick, was_in_bedtime) still advance
exactly as if billed, so the first tick after unlock bills only NEW CPU,
never the locked window.

Mocked only: fake users, fake procs, mocked store — no real accounts, no
lock/unlock/freeze/notify, no sudo anywhere.
"""

import ast
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import budgets
from budgets import blank_entry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def steam_proc(pid=9001, cpu=100.0):
    return {'pid': pid, 'comm': 'steam',
            'cmdline': '/home/kid/.local/share/Steam/steam',
            'args': '', 'etime': '01:00:00', 'cpu_seconds': cpu}


class Recorder:
    """Callable notify mock recording (username, title, body) tuples."""

    def __init__(self):
        self.calls = []

    def __call__(self, username, title, body):
        self.calls.append((username, title, body))
        return True


def budget_entry(limit_min=60.0, used_sec=0.0, last_cpu=None, last_tick=None,
                 day='2026-10-12'):
    entry = blank_entry()
    entry['limits'] = {'games': limit_min}
    entry['day'] = day
    entry['used_seconds'] = {'games': used_sec}
    if last_cpu is not None:
        entry['last_cpu'] = last_cpu
    if last_tick is not None:
        entry['last_tick'] = last_tick
    return entry


def run_tick(data, now, account_locked, procs_by_user=None, notify=None):
    """Run tick_budgets against a temp store; return (saved_data, summaries)."""
    procs_by_user = procs_by_user or {}
    with tempfile.TemporaryDirectory() as d:
        budgets_file = os.path.join(d, 'budgets.json')
        with mock.patch.object(budgets, 'BUDGETS_FILE', budgets_file):
            budgets.save_budgets(data)
            summaries = budgets.tick_budgets(
                {}, now,
                procs_provider=lambda u: procs_by_user.get(u, []),
                prefs={}, index={}, act=False, notify=notify,
                account_locked=account_locked)
            saved = budgets.load_budgets()
    return saved, summaries


def kid_summary(summaries):
    return next(s for s in summaries if s.get('user') == 'kid')


class TestLockedNoBilling(unittest.TestCase):
    def test_locked_tick_bills_nothing_and_advances_baseline(self):
        """Locked + CPU delta across a tick: used_seconds unchanged, but the
        last_cpu baseline advances exactly as if billed."""
        entry = budget_entry(
            last_cpu={'9001': {'comm': 'steam', 'cpu': 100.0}},
            last_tick='2026-10-12T07:59:00')
        saved, summaries = run_tick(
            {'kid': entry}, datetime(2026, 10, 12, 8, 0),
            account_locked=lambda u: True,
            procs_by_user={'kid': [steam_proc(cpu=130.0)]})
        kid = saved['kid']
        # Nothing billed...
        self.assertEqual(kid['used_seconds']['games'], 0.0)
        # ...but the CPU baseline advanced as if the delta had been billed.
        self.assertEqual(kid['last_cpu']['9001'],
                         {'comm': 'steam', 'cpu': 130.0})
        # No pause/notify/enforcement decisions for the locked tick.
        s = kid_summary(summaries)
        self.assertEqual(s['froze'], [])
        self.assertEqual(s['notified'], [])
        self.assertFalse(kid['paused'].get('games'))
        self.assertIn('locked', s['note'])

    def test_unlock_then_tick_bills_only_new_delta(self):
        """Two-tick proof: the locked window's CPU accumulation is consumed
        by the locked tick (baseline advance), so the first unlocked tick
        bills only the NEW delta, never the locked period."""
        entry = budget_entry(
            last_cpu={'9001': {'comm': 'steam', 'cpu': 100.0}},
            last_tick='2026-10-12T07:59:00')
        # Tick 1: LOCKED while CPU climbs 100 -> 200 (the locked window).
        saved, _summaries = run_tick(
            {'kid': entry}, datetime(2026, 10, 12, 8, 0),
            account_locked=lambda u: True,
            procs_by_user={'kid': [steam_proc(cpu=200.0)]})
        self.assertEqual(saved['kid']['used_seconds']['games'], 0.0)
        self.assertEqual(saved['kid']['last_cpu']['9001']['cpu'], 200.0)
        # The wall-clock base advanced too, so the unlock tick sees a normal
        # ~60s wall instead of the whole locked span.
        self.assertEqual(saved['kid']['last_tick'], '2026-10-12T08:00:00')
        # Tick 2: UNLOCKED. Only the fresh 200 -> 210 delta may bill — one
        # 60s tick of wall, not the 100 CPU-seconds accumulated while locked.
        saved2, summaries = run_tick(
            {'kid': saved['kid']}, datetime(2026, 10, 12, 8, 1),
            account_locked=lambda u: False,
            procs_by_user={'kid': [steam_proc(cpu=210.0)]})
        kid2 = saved2['kid']
        self.assertEqual(kid2['used_seconds']['games'], 60.0)
        self.assertEqual(kid2['last_cpu']['9001']['cpu'], 210.0)
        self.assertTrue(kid_summary(summaries)['froze'] == [])
        self.assertTrue(kid_summary(summaries)['notified'] == [])

    def test_locked_tick_skips_pause_and_notify_decisions(self):
        """A state that would normally trip the low warning / pause fires
        nothing while locked — and the SAME state fires once unlocked
        (proving the lock guard, not a broken harness, suppressed it)."""
        entry = budget_entry(
            limit_min=1.0, used_sec=56.0,  # 4s of 60s left: low-warn range
            last_cpu={'9001': {'comm': 'steam', 'cpu': 100.0}},
            last_tick='2026-10-12T07:59:00')
        recorder = Recorder()
        saved, summaries = run_tick(
            {'kid': entry}, datetime(2026, 10, 12, 8, 0),
            account_locked=lambda u: True,
            procs_by_user={'kid': [steam_proc(cpu=160.0)]},
            notify=recorder)
        kid = saved['kid']
        self.assertEqual(recorder.calls, [])
        self.assertEqual(kid_summary(summaries)['notified'], [])
        self.assertEqual(kid['used_seconds']['games'], 56.0)
        self.assertFalse(kid['paused'].get('games'))
        self.assertEqual(kid['warned_low'], {})
        self.assertEqual(kid['warned_paused'], {})
        # Same state, unlocked -> a NEW delta trips the decisions.
        recorder2 = Recorder()
        saved2, summaries2 = run_tick(
            {'kid': kid}, datetime(2026, 10, 12, 8, 1),
            account_locked=lambda u: False,
            procs_by_user={'kid': [steam_proc(cpu=200.0)]},
            notify=recorder2)
        self.assertTrue(recorder2.calls)
        self.assertTrue(saved2['kid']['paused'].get('games'))

    def test_default_account_locked_none_preserves_existing_behavior(self):
        """account_locked=None (or absent) == today exactly: billing,
        enforcement, and notifications all run normally."""

        def fresh():
            return budget_entry(
                used_sec=56.0,
                last_cpu={'9001': {'comm': 'steam', 'cpu': 100.0}},
                last_tick='2026-10-12T07:59:00')

        now = datetime(2026, 10, 12, 8, 0)
        procs = [steam_proc(cpu=160.0)]
        with tempfile.TemporaryDirectory() as d:
            budgets_file = os.path.join(d, 'budgets.json')
            with mock.patch.object(budgets, 'BUDGETS_FILE', budgets_file):
                budgets.save_budgets({'kid': fresh()})
                s_default = budgets.tick_budgets(
                    {}, now, procs_provider=lambda u: procs,
                    prefs={}, index={}, act=False, notify=None)
                d_default = budgets.load_budgets()['kid']
                budgets.save_budgets({'kid': fresh()})
                s_explicit = budgets.tick_budgets(
                    {}, now, procs_provider=lambda u: procs,
                    prefs={}, index={}, act=False, notify=None,
                    account_locked=lambda u: False)
                d_explicit = budgets.load_budgets()['kid']
        # Billing/enforcement/notification outcomes byte-identical.
        self.assertEqual(d_default, d_explicit)
        self.assertEqual(
            [s for s in s_default if s.get('user') != '_watchdog'],
            [s for s in s_explicit if s.get('user') != '_watchdog'])
        # Sanity: billing really happened (the guard did not disable it).
        self.assertEqual(d_default['used_seconds']['games'], 116.0)

    def test_callable_consulted_per_user(self):
        """One tick, mixed locked/unlocked users: the callable is consulted
        for each user and only the locked user's usage is frozen."""
        entry_locked = budget_entry(
            last_cpu={'9001': {'comm': 'steam', 'cpu': 100.0}},
            last_tick='2026-10-12T07:59:00')
        entry_free = budget_entry(
            last_cpu={'9001': {'comm': 'steam', 'cpu': 100.0}},
            last_tick='2026-10-12T07:59:00')
        calls = []

        def locked(u):
            calls.append(u)
            return u == 'kid_locked'

        procs = {'kid_locked': [steam_proc(cpu=150.0)],
                 'kid_free': [steam_proc(cpu=150.0)]}
        saved, summaries = run_tick(
            {'kid_locked': entry_locked, 'kid_free': entry_free},
            datetime(2026, 10, 12, 8, 0),
            account_locked=locked, procs_by_user=procs)
        # Locked user: nothing billed, baseline advanced.
        self.assertEqual(saved['kid_locked']['used_seconds']['games'], 0.0)
        self.assertEqual(saved['kid_locked']['last_cpu']['9001']['cpu'], 150.0)
        # Unlocked user: billed a full normal tick.
        self.assertEqual(saved['kid_free']['used_seconds']['games'], 60.0)
        self.assertEqual(saved['kid_free']['last_cpu']['9001']['cpu'], 150.0)
        # The callable was consulted for BOTH users.
        self.assertEqual(sorted(calls), ['kid_free', 'kid_locked'])


class TestCallSiteWiring(unittest.TestCase):
    """Static-source guards: both tick_budgets call sites must feed the
    real get_lock_status wrapper (repo static-parse style)."""

    def test_app_thread_wires_real_lock_status_into_tick_budgets(self):
        src = read('app.py')
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == 'bedtime_loop')
        body = ast.get_source_segment(src, fn)
        self.assertIsNotNone(body, 'bedtime_loop not found in app.py')
        self.assertIn('tick_budgets', body)
        self.assertRegex(
            body,
            r"account_locked\s*=\s*lambda\s+u:\s*get_lock_status\(u\)\[0\]",
            'the 60s thread must feed the real lock status to tick_budgets')

    def test_timer_process_wires_real_lock_status_into_tick_budgets(self):
        src = read('enforce_bedtime_timer.py')
        self.assertRegex(
            src,
            r"from app import .*\bget_lock_status\b",
            'the timer process must import the same get_lock_status check')
        self.assertRegex(
            src,
            r"account_locked\s*=\s*lambda\s+u:\s*get_lock_status\(u\)\[0\]",
            'the timer process must feed the real lock status to tick_budgets')


if __name__ == '__main__':
    unittest.main()