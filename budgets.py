#!/usr/bin/env python3
"""Phase 1: process filtering, app categorization, per-category time budgets.

Prototype mindset: stdlib only (no Flask import here so unit tests run with
system python3), SQLite runtime state via store.py, no other DB.

Budget model
------------
Usage is measured in **active wall-clock minutes**, not raw CPU seconds and
not raw wall-clock: on every 60s tick we read each process's cumulative CPU
time (``ps -o time``) and compute the delta since the previous tick. If any
process in a budgeted category was active this tick (delta at or above
``ACTIVE_THRESHOLD_SECONDS``), the whole tick's wall-clock duration is
charged to that category; a tick with no active process charges nothing.
An idle app therefore contributes ~0, so background-idle MUST NOT burn
budget — while an app in actual use accrues time matching the Elapsed
column parents see.

A **locked** account (usermod -L: manual Lock, Lock Screen, bedtime
auto-lock — all reflected by get_lock_status) likewise MUST NOT burn
budget: if the child cannot reach the app, they are not spending time on
it. While locked, each tick advances the accounting baselines
(last_cpu / last_tick / was_in_bedtime) without billing a second, so the
first tick after unlock bills only NEW activity, never the locked window.

**Focus engine (opt-in, per user).** When an entry's ``focus_engine`` is
``'focus'`` (the session agent; see ``SESSION_AGENT_PLAN.md``), the tick
bills per-category *focused* seconds drained from the child's validated
feed (``focus_feed``) instead of the CPU-delta gate, behind the same
freezable-member gate. ``'cpu'`` (the default) is byte-identical to the
description above. A missing/stale/malformed feed flips the engine to
``'no_data'`` and HALTS billing for that tick (``used_seconds`` is left
untouched — never silently zeroed) until a fresh feed recovers it. The
``last_cpu`` baseline keeps advancing under both engines so switching back
to ``'cpu'`` is lossless.

**Active threshold:** per-process deltas smaller than
``ACTIVE_THRESHOLD_SECONDS`` (1.0 CPU-second per 60s tick, i.e. processes
averaging under ~1.7% CPU) are treated as background noise and ignored.

**Wall-clock cap:** the charged duration is capped at
``MAX_TICK_WALL_SECONDS`` (5 minutes) so sleep/downtime gaps never bill a
burst of phantom hours on the first tick back.

**Display:** all user-facing times are whole minutes only — no seconds are
ever shown. Internal accounting keeps float seconds to avoid rounding drift.

Budgets reset at **bedtime end** (the True->False transition of the bedtime
window, detected in the tick loop). Users with no bedtime reset at
**midnight** (first tick whose calendar date differs from the stored day).

Enforcement uses the cgroup freezer (``parental-<username>-<category>``),
never signals. On exhaust the category cgroup is frozen; the parent can
resume manually, and everything auto-unfreezes at reset.
"""

import glob
import grp
import os
import pwd
import re
import shlex
import subprocess
import time
from datetime import datetime

import audit
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESS_PREFS_FILE = os.path.join(BASE_DIR, 'process_prefs.json')
BUDGETS_FILE = os.path.join(BASE_DIR, 'budgets.json')
OVERRIDES_FILE = os.path.join(BASE_DIR, 'overrides.json')
CGROUP_ROOT = '/sys/fs/cgroup'

CATEGORIES = ('games', 'browsers', 'media', 'chat', 'other')
CATEGORY_LABELS = {
    'games': 'Games',
    'browsers': 'Browsers',
    'media': 'Media',
    'chat': 'Chat',
    'other': 'Other',
}

# CPU seconds per 60s tick below which a process counts as background-idle
# noise and does not burn budget. See module docstring.
ACTIVE_THRESHOLD_SECONDS = 1.0
# Upper bound on wall-clock seconds billed for a single tick. Ticks normally
# land ~60s apart; the cap only bites after sleep/service downtime so the
# first tick back cannot bill phantom hours.
MAX_TICK_WALL_SECONDS = 300.0
TICK_SECONDS = 60
# Remaining budget seconds at or below which a "minutes left" warning is
# sent (once per day per category).
LOW_WARN_SECONDS = 300.0
# Minimum allowance for a weekly window (mirrors the bonus custom-entry
# floor: BONUS_CUSTOM_MIN = 5). A window below this is rejected/malformed.
WINDOW_MIN_MINUTES = 5

# ---------------------------------------------------------------------------
# Focus engine (per-user session agent) — SESSION_AGENT_PLAN.md §8/§9.
#
# The measured axis is per-category *focused* seconds from the child's
# session feed (``focus_feed``), replacing the CPU-delta activity gate. The
# CPU engine stays the default, so the whole focus path is inert until a
# parent opts a child in via ``focus_engine = 'focus'``.
# ---------------------------------------------------------------------------
FOCUS_ENGINE_CPU = 'cpu'
FOCUS_ENGINE_FOCUS = 'focus'
# Auto-degraded marker: the parent configured the focus engine but the feed
# is missing/stale/malformed. Billing is HALTED for that tick (used_seconds
# is left untouched — never silently billed as zero) and the state is
# surfaced; a fresh feed recovers automatically to FOCUS_ENGINE_FOCUS.
FOCUS_ENGINE_NO_DATA = 'no_data'
FOCUS_ENGINES = (FOCUS_ENGINE_CPU, FOCUS_ENGINE_FOCUS, FOCUS_ENGINE_NO_DATA)
# Engines that engage the feed-drain + focus-billing path.
FOCUS_FAMILY = (FOCUS_ENGINE_FOCUS, FOCUS_ENGINE_NO_DATA)
# Plan §8.4 spelling; accepted as an alias for the focus engine.
_FOCUS_ENGINE_ALIASES = {FOCUS_ENGINE_FOCUS: FOCUS_ENGINE_FOCUS,
                         'agent': FOCUS_ENGINE_FOCUS}

_USERNAME_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')
# Same charset the sudoers grant is anchored on
# (parental-[A-Za-z0-9_.-]+): reusing it means per-app cgroup names
# (parental-<user>-app-<comm>) need NO new privilege grant.
_COMM_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')


# ---------------------------------------------------------------------------
# Named re-exports — the budgets facade surface (RESTRUCTURE_PLAN.md §3.2).
#
# budgets.py stays the ONE module object the runtime and the 40 test modules
# bind to, and the ONLY module `mock.patch` targets (490 sites). Every name
# moved out is therefore re-exported here BY NAME — never a wildcard — with
# the consumer that needs it named in the comment. Concern modules never
# import from here at module level; they resolve `budgets.<name>` at CALL
# time so a patch on this module still reaches them (plan §1.1).
#
# Code still DEFINED in this file keeps calling the bare name: this module's
# own global binding IS the attribute the suite patches, so it needs no
# rewrite.
# ---------------------------------------------------------------------------

# budgets_format.py — canonical duration display. Needs: agent.py, app.py,
# the session/focus paths, 2 test modules (`test_process_grouping`,
# `test_budgets`).
from budgets_format import (  # noqa: E402
    etime_to_minutes, format_hm, format_minutes, group_by_comm)

# budgets_guards.py — validation + admin self-protection. Needs:
# device_policy.py (is_admin_protected), app.py (all four + ADMIN_GROUP),
# cli.py/agent.py through `budgets.<name>`, 5 test modules.
from budgets_guards import (  # noqa: E402
    ADMIN_GROUP, is_admin_protected, valid_category, valid_comm,
    valid_username)

# budgets_proc.py — reading `ps` and CPU deltas. Needs: agent.py and app.py
# (list_user_processes), `test_budgets` (parse_cpu_seconds/compute_deltas).
from budgets_proc import (  # noqa: E402
    compute_deltas, list_user_processes, parse_cpu_seconds, read_cmdline)

# budgets_filter.py — process filtering + the NEVER-pause safety list. Needs:
# app.py and agent.py (apply_filter/load_prefs/set_pref/is_never_pause),
# the freezer gates, 8 test modules.
from budgets_filter import (  # noqa: E402
    DEFAULT_HIDDEN_COMMS, DEFAULT_HIDDEN_PATHS, DEFAULT_HIDDEN_PATH_PREFIXES,
    NEVER_PAUSE_COMMS, _argv0_basename, apply_filter, is_default_hidden,
    is_never_pause, load_prefs, save_prefs, set_pref)

# budgets_attribution.py — categorization + display names. Needs: agent.py
# and app.py (12 names), the tick, 10 test modules. The two cache dicts are
# re-exported BY IDENTITY (tests .clear() them in place — R6).
from budgets_attribution import (  # noqa: E402
    CATEGORY_OVERRIDES, _DESKTOP_INDEX_CACHE, _DESKTOP_INDEX_TTL_SECONDS,
    _DESKTOP_TOKEN_MAP, _GENERIC_DOT_PARTS, _GENERIC_INTERPRETERS,
    _JVM_APP_NAMES, _JVM_APP_RES, _OVERRIDE_RES, _cmdline_app_tokens,
    _desktop_category, _exec_candidates, _flatpak_appid_parts,
    _scan_desktop_files, annotate_display_names, build_desktop_index,
    build_desktop_names, categorize, desktop_dirs_for, group_by_category,
    is_user_app, mark_user_apps, parse_desktop_file)

# budgets_state.py — budgets document IO + accounting. Needs: app.py
# (budget_status/set_limit), agent.py (budget_status/group_usage_minutes),
# the tick, 10 test modules (`budgets.BUDGETS_FILE` is patched in 10).
from budgets_state import (  # noqa: E402
    app_budget_status, blank_entry, budget_reset_needed, budget_status,
    group_usage_minutes, load_budgets, save_budgets, set_limit)

# budgets_overrides.py — app overrides + weekly windows. Needs: app.py (7
# names), agent.py (overrides_view/load_overrides), `test_overrides` and
# `test_session_agent_phase2` (`budgets.OVERRIDES_FILE` is patched in 3).
from budgets_overrides import (  # noqa: E402
    _clear_app_runtime, _config_entry, _release_window_freeze,
    _reset_window_runtime, active_window, clear_window, iter_overrides,
    iter_windows, load_overrides, normalize_override, normalize_window,
    override_map, overrides_view, parse_days, save_overrides,
    set_app_override, set_window, update_window, window_id_for)

# budgets_cgroup.py — the privileged freezer. Needs: cli.py (CGROUP_ROOT,
# thaw_bucket), the tick, the watchdog, `test_freeze_buttons` /
# `test_overrides` / `test_qa_time_tracking` (CGROUP_ROOT is patched in 1),
# and tests/test_deployment.py's sudo-argv scan (MODULES widened in slice 6).
from budgets_cgroup import (  # noqa: E402
    APP_BUCKET_PREFIX, _app_bucket, _cgroup_members_path, _ensure_cgroup_dir,
    _read_freeze_path, _set_frozen_path, _sudo_read_file, _sudo_write_file,
    cgroup_members, cgroup_members_app, cgroup_path, cgroup_path_app,
    ensure_cgroup, ensure_cgroup_app, migrate_pid, migrate_pid_app,
    read_freeze, read_freeze_app, set_frozen, set_frozen_app, split_bucket,
    thaw_bucket, unfreeze_all_user)

# budgets_watchdog.py — stuck/orphan freeze self-heal + the freezable gates.
# Needs: cli.py (parse_managed_cgroup), the tick, `test_watchdog` (which
# imports parse_managed_cgroup/watchdog_tick by name) and `test_freeze_buttons`
# (`verify_freeze_safety` is a pinned patch target).
from budgets_watchdog import (  # noqa: E402
    _read_desired_frozen, _read_effective_frozen, _visible_category_members,
    freezable_app_members, freezable_members, parse_managed_cgroup,
    verify_freeze_safety, watchdog_tick)

# budgets_actions.py — manual freeze/unfreeze/resume + the window and bedtime
# predicates. Needs: app.py (freeze/unfreeze/resume), agent.py
# (unfreeze_category), enforce_bedtime_timer.py, 4 test modules.
from budgets_actions import (  # noqa: E402
    _in_bedtime, _in_window, _parse_hhmm, freeze_category, resume_category,
    unfreeze_category)

# budgets_focus.py — the focus engine (per-user session agent). Needs: the
# tick/enforcement path, `test_session_agent_phase2` (focus_engine_of /
# focus_status) and `test_qa_time_tracking`. The mutual `import focus_feed`
# stays INSIDE the two provider functions — see the module docstring.
from budgets_focus import (  # noqa: E402
    _default_focus_provider, _feed_cursor_update, _focus_billing,
    _locked_feed_bookkeeping, boottime_now, focus_engine_of, focus_status)

# budgets_enforce.py — the per-target freeze decision + the per-user tick.
# Needs: the tick, `test_budgets` / `test_safeguards` / `test_notifications`
# (account_and_enforce is a pinned patch target).
from budgets_enforce import account_and_enforce, _enforce_target  # noqa: E402

# budgets_tick.py — tick orchestration and summary emission. Needs:
# enforce_bedtime_timer.py (tick_budgets + emit_tick_summaries), app.py's
# bedtime_loop, `test_safeguards` / `test_qa_time_tracking` (both pinned patch
# targets). This completes the split: budgets.py is now a pure facade.
from budgets_tick import (  # noqa: E402
    _LAST_AUTO_PAUSE_SIGS, _LAST_SUMMARY_SIGS, _audit_auto_pause_failed,
    _clear_window_target_freeze, _locked_tick_bookkeeping,
    _summary_signature, emit_tick_summaries, summary_line, tick_budgets,
    tick_desktop_index)
