#!/usr/bin/env python3
"""Run one bedtime-enforcement pass (used by parental-controls-enforce.timer).

Imports the enforcement logic from the app so the timer and the app's
background thread behave identically.
"""

import os
import sys

APP_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
)
# Fall back to the canonical master checkout if the script lives elsewhere.
if not os.path.exists(os.path.join(APP_DIR, 'app.py')):
    APP_DIR = '/var/home/stan/Documents/Projects/bazzite-parental-controls-2/master'

sys.path.insert(0, APP_DIR)

from app import enforce_bedtime, load_bedtime  # noqa: E402
try:
    from app import notify_user  # noqa: E402
except ImportError:
    notify_user = None  # older checkout without warnings; tick silently
from datetime import datetime  # noqa: E402
import budgets  # noqa: E402

if __name__ == '__main__':
    enforce_bedtime()
    try:
        budgets.tick_budgets(load_bedtime(), datetime.now(),
                             notify=notify_user)
    except Exception as e:
        print(f'budget tick failed: {e}')
