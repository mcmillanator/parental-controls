#!/usr/bin/env python3
"""Normalize a unittest failure list for the slice gate.

The container's baseline failures are all environment-bound, and two of the
suites print the ORIGIN COORDINATES of what they scanned in the subtest id:
`test_deployment` reports `(module='app.py', line=1221)` and
`(origin='app.py:lock_user_sessions')`. Those coordinates legitimately move
when a slice relocates code — which is the entire point of the series — so
comparing them byte-for-byte would report drift on every slice that moves a
sudo call.

This normalizer replaces exactly those two coordinate fields with a marker and
leaves everything else (the test id, the failure kind, the argv, the count)
untouched. A moved origin is then invisible; a NEW failure, a REMOVED
failure, a changed argv or a changed subtest still shows up as drift.

    .venv/bin/python .scratch/normalize_failures.py < list > normalized
"""

import re
import sys

MODULE = re.compile(r"module='[^']*'(, line=\d+)?")
ORIGIN = re.compile(r"origin='[^']*'")


def normalize(line: str) -> str:
    line = MODULE.sub("module='<moved>'(, line=<n>)?", line)
    line = ORIGIN.sub("origin='<moved>'", line)
    return line


def main(argv):
    for raw in sys.stdin:
        sys.stdout.write(normalize(raw))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))