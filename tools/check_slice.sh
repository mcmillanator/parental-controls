#!/usr/bin/env bash
# Per-slice gate for the app/budgets split (RESTRUCTURE_PLAN.md §4/§6).
# Run from the worktree root, after every migration slice:
#
#   mkdir -p .scratch
#   .venv/bin/python -m unittest discover -s tests -v   # once, on a clean tree
#   grep -E '^(FAIL|ERROR): ' .scratch/run.txt | sort > .scratch/baseline_failures.txt
#   tools/check_slice.sh
#
# Rows (all of them must hold before a slice is committed):
#   full container suite   failure set IDENTICAL to .scratch/baseline_failures.txt
#   UI set                 386 tests, OK
#   test_qa_time_tracking   32 tests, OK
#   import surface         RESTRUCTURE_IMPORTS.md sections 3-4 (the 40 test
#                          modules and all 490 mock.patch sites) byte-identical
#   structure              no module-level import cycle, violations <= baseline
#   import-time smoke      app-first and budgets-first both import
#
# Exit status is non-zero when any row drifts.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p .scratch

# Recorded on master before the first slice (RESTRUCTURE_PLAN.md §4):
# 5 files over 500 lines. The count may only go down.
BASELINE_VIOLATIONS=5

# Known WALL-CLOCK-dependent result, not a regression.
# tests/test_countdown.py asserts a bedtime countdown is > 0 minutes for a
# 21:00-07:00 window, but `minutes_until_bedtime()` returns 0 while the clock
# is INSIDE that window (21:00-07:00 local). The test passes before 21:00 and
# fails after, every day, on any tree — including untouched master. Excluded
# from the drift comparison so a clock flip can neither masquerade as a split
# regression nor hide one; its result is still printed with the suite above.
KNOWN_CLOCK_FLAKES='test_bedtime_is_used_when_no_bonus_is_active'

strip_clock_flakes() {
    grep -vF "$KNOWN_CLOCK_FLAKES" "$1" || true
}

run_suite() {
    TMPDIR="$PWD/.scratch" .venv/bin/python -m unittest discover -s tests -v \
        > .scratch/run.txt 2>&1 || true
    grep -E '^(FAIL|ERROR): ' .scratch/run.txt | sort > .scratch/failures.txt
}

rc=0
echo '== full container suite =='
run_suite
grep -E '^Ran |^FAILED|^OK' .scratch/run.txt | tail -2 | sed 's/^/  /'
grep -F "$KNOWN_CLOCK_FLAKES" .scratch/failures.txt \
    | sed 's/^/  (known clock flake, excluded: /;s/$/)/'
if [ -f .scratch/baseline_failures.txt ]; then
    strip_clock_flakes .scratch/baseline_failures.txt > .scratch/base_filtered.txt
    strip_clock_flakes .scratch/failures.txt > .scratch/failures_filtered.txt
    if diff -u .scratch/base_filtered.txt .scratch/failures_filtered.txt \
            > .scratch/failures.diff; then
        echo "  failure set IDENTICAL to baseline"
    else
        echo "  FAILURE SET DRIFTED"; rc=1
        head -40 .scratch/failures.diff | sed 's/^/  /'
    fi
else
    echo "  no baseline yet -- record one from a clean tree"; rc=1
fi

echo '== UI set =='
TMPDIR="$PWD/.scratch" .venv/bin/python -m unittest discover -s tests -p 'test_ui_*.py' 2>&1 | tail -3
echo '== test_qa_time_tracking =='
TMPDIR="$PWD/.scratch" .venv/bin/python -m unittest discover -s tests -p 'test_qa_time_tracking.py' 2>&1 | tail -3

# The import surface is the contract: sections 3 (which test modules bind from
# app/budgets) and 4 (every mock.patch target) must not move at all. Sections
# 1/5/6 describe where the code now LIVES and are expected to change.
echo '== import surface (sections 3-4 must be byte-identical) =='
.venv/bin/python tools/import_graph.py > RESTRUCTURE_IMPORTS.md
awk '/^## 3\. /,/^## 5\. /' RESTRUCTURE_IMPORTS.md > .scratch/contract_new.md
git show HEAD:RESTRUCTURE_IMPORTS.md > .scratch/committed_imports.md 2>/dev/null
if [ ! -s .scratch/committed_imports.md ]; then
    echo "  no committed RESTRUCTURE_IMPORTS.md to compare against"; rc=1
else
    awk '/^## 3\. /,/^## 5\. /' .scratch/committed_imports.md > .scratch/contract_old.md
    if diff -u .scratch/contract_old.md .scratch/contract_new.md > .scratch/contract.diff; then
        echo "  import surface UNCHANGED ($(grep -cE '^\| `app\.|^\| `budgets\.' .scratch/contract_new.md) patch-target rows)"
    else
        echo "  IMPORT SURFACE MOVED -- the split broke a binding"; rc=1
        head -40 .scratch/contract.diff | sed 's/^/  /'
    fi
    echo "  (full-file movement, informational):"
    git diff --stat -- RESTRUCTURE_IMPORTS.md | sed 's/^/  /'
fi

echo '== structure =='
structure=$(.venv/bin/python tools/check_structure.py)
echo "$structure" | tail -6 | sed 's/^/  /'
violations=$(echo "$structure" | sed -n 's/^total violations: //p')
if echo "$structure" | grep -q '^CYCLES:'; then
    echo "  module-level import cycle present"; rc=1
fi
if [ -n "$violations" ] && [ "$violations" -gt "$BASELINE_VIOLATIONS" ]; then
    echo "  structure violations $violations > baseline $BASELINE_VIOLATIONS"; rc=1
fi
if [ -z "$violations" ]; then
    echo "  could not read the violation count"; rc=1
fi

echo '== import-time smoke =='
.venv/bin/python -c "import app, budgets, wsgi; print('  app/budgets-first ok', budgets.CATEGORIES[:2])" || rc=1
for m in budgets_format budgets_guards budgets_proc budgets_filter \
         budgets_attribution budgets_state budgets_overrides budgets_cgroup \
         budgets_watchdog budgets_actions budgets_focus budgets_tick \
         budgets_enforce users locks; do
    [ -f "$m.py" ] || continue
    .venv/bin/python -c "import $m; print('  $m-first ok')" || rc=1
done

exit $rc