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

from app import enforce_bedtime  # noqa: E402

if __name__ == '__main__':
    enforce_bedtime()
