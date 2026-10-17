#!/usr/bin/env python3
"""Run one bedtime-enforcement pass (used by parental-controls-enforce.timer).

Imports the enforcement logic from the app so the timer and the app's
background thread behave identically.
"""

import os
import sys

# Locate the app relative to this script — the systemd unit runs the copy
# that lives alongside app.py in the install dir, so no checkout path is
# ever baked in. Fail loudly rather than silently importing the wrong code.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(os.path.join(APP_DIR, 'app.py')):
    sys.stderr.write(
        f'FATAL: app.py not found next to {__file__} (looked in {APP_DIR}); '
        f'this script must run from the installed app directory.\n')
    raise SystemExit(1)

sys.path.insert(0, APP_DIR)

from app import enforce_bedtime, get_lock_status, load_bedtime  # noqa: E402
try:
    from app import notify_user  # noqa: E402
except ImportError:
    notify_user = None  # older checkout without warnings; tick silently
from datetime import datetime  # noqa: E402
import budgets  # noqa: E402

if __name__ == '__main__':
    enforce_bedtime()
    try:
        # tick_budgets() runs budget enforcement first, then the freezer
        # watchdog (same pass), so this timer path self-heals stuck cgroup
        # freezes exactly like the app's background thread does. Locked
        # accounts bill nothing: get_lock_status (sudo passwd -S, already
        # covered by the sudoers grants) feeds account_locked the same way
        # the app thread does.
        # FIX-2: tick_budgets(index=None) now builds the per-user
        # .desktop index (cached), so the tick categorizes like the UI.
        summaries = budgets.tick_budgets(
            load_bedtime(), datetime.now(),
            notify=notify_user, run_watchdog=True,
            account_locked=lambda u: get_lock_status(u)[0])
        # FIX-3 (TIME_TRACKING_QA.md F8): surface what the tick decided —
        # a silently aborted freeze repeating every minute used to mean
        # zero journal output. stdout here goes to journald.
        budgets.emit_tick_summaries(summaries)
    except Exception as e:
        print(f'budget tick failed: {e}')
