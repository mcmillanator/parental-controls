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
# Admin self-protection (lockout safeguard, MVP)
# ---------------------------------------------------------------------------
# Rule-based, no hardcoded names: members of the admin (`wheel`) group are
# never auto-locked by bedtime enforcement nor auto-frozen by budget
# enforcement. Manual lock via the UI still works (behind its confirm
# dialog). Every skip logs a note so it is visible in the service journal.

ADMIN_GROUP = 'wheel'


def is_admin_protected(username):
    """True if `username` is an admin (member of the `wheel` group).

    Covers both supplementary membership (gr_mem) and a wheel primary GID.
    Returns False when the group or user is unknown.
    """
    if not username:
        return False
    try:
        wheel = grp.getgrnam(ADMIN_GROUP)
    except Exception:
        return False
    if username in wheel.gr_mem:
        return True
    try:
        return pwd.getpwnam(username).pw_gid == wheel.gr_gid
    except Exception:
        return False


def valid_username(username):
    """Guard against path traversal before a username touches cgroup paths.

    Beyond the charset check this rejects a leading '-': usernames reach
    privileged commands as bare argv elements, and a name like '-x'
    would be parsed as an option rather than a login. No real account
    can begin with '-' (mirrors app.valid_username), so this costs
    nothing and closes an argument-injection path.
    """
    return (bool(username) and not username.startswith('-')
            and bool(_USERNAME_RE.match(username)))


def valid_category(category):
    return category in CATEGORIES


def valid_comm(comm):
    """Guard for per-app override keys and ``parental-<user>-app-<comm>``.

    comm originates from ``ps -o comm`` (already truncated to 15 chars)
    and reaches privileged cgroup paths and argv as a bare element, so it
    must stay inside the same charset the sudoers grant is anchored on
    (``[A-Za-z0-9_.-]``, no leading ``-``), be non-empty and not made
    only of dots (``.``/``..`` would be path segments), and fit a real
    comm (1-15 chars). Checked BEFORE any cgroup path/argv construction.
    """
    if not comm or len(comm) > 15:
        return False
    if comm.startswith('-') or not _COMM_RE.match(comm):
        return False
    if not comm.strip('.'):
        return False
    return True


# ---------------------------------------------------------------------------
# Process filtering
# ---------------------------------------------------------------------------
# Curated default denylist. `ps -o comm` truncates to 15 chars, so the set
# carries both truncated and full spellings; we also match the argv[0]
# basename and full-cmdline path prefixes.

DEFAULT_HIDDEN_COMMS = frozenset({
    # session / dbus plumbing
    'systemd', '(sd-pam)', 'sd-pam', 'dbus-broker', 'dbus-broker-lau',
    'dbus-daemon', 'dconf-service', 'at-spi-bus-laun', 'at-spi2-registr',
    # portals / file services
    'xdg-desktop-por', 'xdg-desktop-po', 'xdg-permission-', 'xdg-document-po',
    'xdg-desktop-i', 'gvfsd', 'gvfs-udisks2-vo', 'gvfs-mtp-volume',
    'gvfs-gphoto2-vo', 'gvfs-afc-volume', 'gvfsd-trash', 'gvfsd-metadata',
    'gvfsd-fuse',
    # audio
    'pipewire', 'pipewire-pulse', 'wireplumber', 'pipewire-media-',
    # misc daemons / helpers
    'mpris-proxy', 'dmemcg-booster', 'uresourced', 'baloo_file',
    'obexd', 'kunifiedpush-di', 'kunifiedpush-distributor', 'agent',
    'geoclue', 'findmnt', 'catatonit',
    # shells / wrappers (plumbing, not apps a parent manages)
    'bash', 'sh', 'dash',
    # desktop shell components
    'kwin_wayland', 'kwin_x11', 'Xwayland', 'plasmashell', 'kded6',
    'ksmserver', 'klauncher', 'kactivitymanage', 'kglobalaccel6',
    'kaccess', 'kwalletd6', 'gnome-shell', 'mutter', 'gdm-wayland-ses',
})

DEFAULT_HIDDEN_PATH_PREFIXES = (
    '/usr/libexec/',
    '/usr/lib/systemd/',
    '/usr/lib64/libexec/',
)

# Binaries hidden by exact path even though they live in /usr/bin.
DEFAULT_HIDDEN_PATHS = frozenset({
    '/usr/bin/dbus-broker-launch',
    '/usr/bin/pipewire',
    '/usr/bin/pipewire-pulse',
    '/usr/bin/wireplumber',
    '/usr/bin/mpris-proxy',
    '/usr/bin/dmemcg-booster',
    '/usr/bin/kunifiedpush-distributor',
    '/usr/bin/findmnt',
})


def _argv0_basename(cmdline):
    if not cmdline:
        return ''
    first = cmdline.split(None, 1)[0] if isinstance(cmdline, str) else cmdline[0]
    return os.path.basename(first).lower()


def is_default_hidden(proc):
    """True if a proc dict should be hidden by the curated default denylist.

    proc needs: comm (str), cmdline (str, full command line or '').
    """
    comm = (proc.get('comm') or '').lower()
    if comm in DEFAULT_HIDDEN_COMMS:
        return True
    cmdline = proc.get('cmdline') or proc.get('args') or ''
    base = _argv0_basename(cmdline)
    if base in DEFAULT_HIDDEN_COMMS:
        return True
    low = cmdline.lower()
    for prefix in DEFAULT_HIDDEN_PATH_PREFIXES:
        if low.startswith(prefix):
            return True
    argv0 = cmdline.split(None, 1)[0] if cmdline else ''
    if argv0 in DEFAULT_HIDDEN_PATHS:
        return True
    return False


# ---------------------------------------------------------------------------
# NEVER-pause safety list (checked at match time AND at freeze time)
# ---------------------------------------------------------------------------

NEVER_PAUSE_COMMS = frozenset({
    'kwin_wayland', 'kwin_x11', 'Xwayland',
    'pipewire', 'pipewire-pulse', 'wireplumber', 'pipewire-media-',
    'dbus-broker', 'dbus-broker-lau', 'dbus-daemon',
    'systemd', '(sd-pam)', 'sd-pam',
    'xdg-desktop-por', 'xdg-desktop-po', 'xdg-permission-',
    'xdg-document-po', 'xdg-desktop-i',
    'sshd', 'sudo', 'su',
    'bash', 'sh', 'dash', 'zsh', 'fish', 'tmux', 'screen',
    'python', 'python3', 'flask', 'gunicorn',
    'plasmashell', 'kded6', 'ksmserver', 'klauncher',
    'kactivitymanage', 'kglobalaccel6', 'kaccess', 'kwalletd6',
    'gnome-shell', 'mutter',
    'pipewire-pulse', 'pulseaudio',
})


def is_never_pause(proc):
    """True if this process must never be frozen, regardless of budgets."""
    comm = (proc.get('comm') or '').lower()
    if comm in NEVER_PAUSE_COMMS:
        return True
    cmdline = (proc.get('cmdline') or proc.get('args') or '').lower()
    # The parental-controls app itself (runs as another user, belt and braces).
    if 'app.py' in cmdline and ('parental' in cmdline or 'flask' in cmdline):
        return True
    if _argv0_basename(cmdline) in NEVER_PAUSE_COMMS:
        return True
    return False


# ---------------------------------------------------------------------------
# process_prefs — per-user Hide / Show-hidden state (SQLite table holding
# the same JSON document per user; PROCESS_PREFS_FILE is the legacy
# import source and rollback mirror)
# Shape: {username: {"hidden_comm": [...], "shown_comm": [...]}}
# Effective hidden = (default_hidden | hidden_comm) - shown_comm
# ---------------------------------------------------------------------------

def load_prefs():
    return store.load_table(store.db_path_for(PROCESS_PREFS_FILE),
                            'process_prefs')


def save_prefs(data):
    store.save_table(store.db_path_for(PROCESS_PREFS_FILE),
                     'process_prefs', data, PROCESS_PREFS_FILE)


def set_pref(username, comm, action):
    """action 'hide' -> hide this comm; 'show' -> re-show it.

    Returns (ok, message).
    """
    if not valid_username(username):
        return False, 'Invalid username'
    comm = (comm or '').strip()
    if not comm:
        return False, 'No command provided'
    if action not in ('hide', 'show'):
        return False, f'Unknown action: {action}'
    data = load_prefs()
    entry = data.setdefault(username, {'hidden_comm': [], 'shown_comm': []})
    hidden = set(entry.get('hidden_comm', []))
    shown = set(entry.get('shown_comm', []))
    if action == 'hide':
        hidden.add(comm)
        shown.discard(comm)
    else:
        shown.add(comm)
        hidden.discard(comm)
    entry['hidden_comm'] = sorted(hidden)
    entry['shown_comm'] = sorted(shown)
    try:
        save_prefs(data)
    except OSError as e:
        return False, str(e)
    return True, 'OK'


def apply_filter(procs, username, prefs=None):
    """Split procs into (visible, hidden) honoring defaults + user prefs.

    Effective hidden = (default_hidden | auto_hidden | hidden_comm) -
    shown_comm, where auto_hidden covers anything not explicitly
    user-started (proc['user_app'] False, set by mark_user_apps; an
    absent flag defaults to True so unmarked snapshots behave exactly
    as before). Hidden never means unmanaged: budgets/freezer matching
    is untouched.
    """
    if prefs is None:
        prefs = load_prefs()
    entry = prefs.get(username, {}) if isinstance(prefs, dict) else {}
    user_hidden = set(entry.get('hidden_comm', []))
    user_shown = set(entry.get('shown_comm', []))
    visible, hidden = [], []
    for proc in procs:
        comm = proc.get('comm', '')
        default = is_default_hidden(proc) or not proc.get('user_app', True)
        if (default or comm in user_hidden) and comm not in user_shown:
            hidden.append(proc)
        else:
            visible.append(proc)
    return visible, hidden


# ---------------------------------------------------------------------------
# Categorization: .desktop Categories + curated overrides
# ---------------------------------------------------------------------------

# keyword -> category. Matched as whole words (\b) against "comm + cmdline"
# (lowercased) so e.g. 'edge' does not match 'knowledge'. Steam chain first.
CATEGORY_OVERRIDES = {
    'games': (
        'steam', 'gameoverlayui', 'gamescope', 'pressure-vessel',
        'steamwebhelper', 'reaper', 'proton', 'wineserver', 'wine-preloader',
        'lutris', 'heroic', 'bottles',
    ),
    'browsers': (
        'firefox', 'chrome', 'chromium', 'brave', 'msedge', 'opera',
        'vivaldi', 'librewolf', 'waterfox', 'zen-bin', 'falkon', 'konqueror',
    ),
    'media': (
        'vlc', 'mpv', 'celluloid', 'haruna', 'clapper', 'totem', 'elisa',
        'lollypop', 'rhythmbox', 'spotify', 'strawberry', 'parole', 'kodi',
        'audacious', 'clementine', 'amberol',
    ),
    'chat': (
        'discord', 'vesktop', 'telegram', 'signal', 'element', 'slack',
        'teams', 'fractal', 'neochat', 'nheko', 'whatsapp', 'caprine',
        'gajim', 'dino', 'cinny',
    ),
}

_OVERRIDE_RES = {
    cat: [re.compile(r'\b' + re.escape(kw) + r'\b') for kw in kws]
    for cat, kws in CATEGORY_OVERRIDES.items()
}

# .desktop Categories tokens -> our categories (priority: games first).
_DESKTOP_TOKEN_MAP = (
    ('games', {'Game'}),
    ('browsers', {'WebBrowser'}),
    ('media', {'Audio', 'Video', 'AudioVideo', 'Player', 'Music', 'Midi',
               'Mixer', 'Sequencer', 'Tuner', 'TV'}),
    ('chat', {'Chat', 'InstantMessaging', 'IRCClient', 'Telephony',
              'VideoConference'}),
)


def _exec_candidates(exec_value):
    """Yield lookup keys for a .desktop Exec= value (executable + flatpak id)."""
    try:
        tokens = shlex.split(exec_value)
    except ValueError:
        tokens = exec_value.split()
    # Drop env assignments (FOO=bar) and field codes (%U); skip wrappers.
    words = [t for t in tokens
             if '=' not in t and not t.startswith('%') and not t.startswith('-')]
    if not words:
        return
    if os.path.basename(words[0]) == 'flatpak' and len(words) >= 3 \
            and words[1] == 'run':
        appid = words[2]
        for part in appid.split('.'):
            part = part.lower()
            if part and part not in ('org', 'com', 'io', 'net', 'desktop', 'app'):
                yield part
        return
    yield os.path.basename(words[0]).lower()


def _desktop_category(categories_value):
    tokens = {t.strip() for t in categories_value.split(';') if t.strip()}
    for cat, wanted in _DESKTOP_TOKEN_MAP:
        if tokens & wanted:
            return cat
    return None


def parse_desktop_file(path):
    """Parse one .desktop file -> (keys, category, name) or None.

    keys: executable-basename lookup keys from Exec= (including
    flatpak app-id parts). category: our games/browsers/media/chat
    bucket, or None when the file carries no mapped Categories=.
    name: the human-friendly Name= value, or None when absent.
    Returns None only when the file has no usable Exec= line
    (nothing to key on) or is unreadable — entries without a mapped
    category still yield keys + name so display-name lookup covers
    every installed app, not just categorized ones.
    """
    category = None
    exec_value = None
    name = None
    try:
        with open(path, 'r', errors='ignore') as f:
            in_entry = False
            for line in f:
                line = line.strip()
                if line == '[Desktop Entry]':
                    in_entry = True
                elif line.startswith('['):
                    if in_entry:
                        break
                elif in_entry:
                    if line.startswith('Categories='):
                        category = _desktop_category(line[len('Categories='):])
                    elif line.startswith('Exec='):
                        exec_value = line[len('Exec='):]
                    elif line.startswith('Name=') and name is None:
                        name = line[len('Name='):].strip() or None
    except OSError:
        return None
    if not exec_value:
        return None
    return list(_exec_candidates(exec_value)), category, name


def desktop_dirs_for(username=None):
    dirs = [
        '/usr/share/applications',
        '/usr/local/share/applications',
        '/var/lib/flatpak/exports/share/applications',
    ]
    if username:
        try:
            home = pwd.getpwnam(username).pw_dir
        except KeyError:
            home = None
        if home:
            dirs.append(os.path.join(home, '.local/share/applications'))
            dirs.append(os.path.join(
                home, '.local/share/flatpak/exports/share/applications'))
    # The user running the app may have per-user flatpaks too.
    dirs.append(os.path.expanduser('~/.local/share/applications'))
    return dirs


def _scan_desktop_files(username=None):
    """Yield (keys, category, name) per .desktop file with an Exec= line.

    Single shared scan pass behind build_desktop_index (categorized
    entries only) and build_desktop_names (every entry with a Name=).
    """
    for d in desktop_dirs_for(username):
        try:
            files = glob.glob(os.path.join(d, '*.desktop'))
        except OSError:
            continue
        for path in files:
            parsed = parse_desktop_file(path)
            if parsed:
                yield parsed


def build_desktop_index(username=None):
    """Map executable-basename -> category from installed .desktop files."""
    index = {}
    for keys, category, _name in _scan_desktop_files(username):
        if not category:
            continue
        for key in keys:
            # First file wins; games-relevant entries rarely collide.
            index.setdefault(key, category)
    return index


def build_desktop_names(username=None):
    """Map executable-basename -> human-friendly app Name=.

    Covers ALL entries with an Exec= line, even those without a mapped
    category (display names are not limited to budgeted categories).
    First file wins, mirroring build_desktop_index.
    """
    names = {}
    for keys, _category, name in _scan_desktop_files(username):
        if not name:
            continue
        for key in keys:
            names.setdefault(key, name)
    return names


def _flatpak_appid_parts(cmdline):
    """Dot-parts of a `flatpak run <appid>` cmdline (lowercased).

    Bare registry/TLD-ish parts (org/com/io/net) and generic suffixes
    (desktop/app) are dropped, mirroring _exec_candidates. Returns []
    when the cmdline is not a flatpak invocation.
    """
    if not cmdline or 'flatpak' not in cmdline.lower():
        return []
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        tokens = cmdline.split()
    low = [t.lower() for t in tokens]
    for i, tok in enumerate(low):
        if os.path.basename(tok) == 'flatpak' \
                and i + 2 < len(tokens) and low[i + 1] == 'run':
            appid = tokens[i + 2]
            if appid.startswith('-'):
                continue
            return [p.lower() for p in appid.split('.')
                    if p and p.lower() not in
                    ('org', 'com', 'io', 'net', 'desktop', 'app')]
    return []


def is_user_app(proc, app_index=None, home=None):
    """True if a proc looks explicitly user-started (not background plumbing).

    Any rule wins: argv[0]-basename or comm is a known installed app
    (app_index covers every .desktop Exec entry, e.g. build_desktop_names —
    not just categorized ones, so terminals/editors stay visible); a CATEGORY_OVERRIDES
    whole-word match on comm+cmdline (covers the Steam chain,
    browsers, media, chat — the same match categorize() uses);
    argv[0] is an absolute path under the user's home dir (their own
    scripts/binaries); or flatpak app-id dot-parts from the cmdline
    match the app index.
    """
    index = app_index or {}
    comm = (proc.get('comm') or '').lower()
    cmdline = proc.get('cmdline') or proc.get('args') or ''
    base = _argv0_basename(cmdline)
    if base and base in index:
        return True
    if comm and comm in index:
        return True
    haystack = (comm + ' ' + cmdline).lower()
    for cat in CATEGORIES:
        if cat == 'other':
            continue
        for rx in _OVERRIDE_RES[cat]:
            if rx.search(haystack):
                return True
    if home:
        argv0 = cmdline.split(None, 1)[0] if cmdline else ''
        h = home.rstrip('/')
        if argv0 and h and (argv0 == h or argv0.startswith(h + '/')):
            return True
    for part in _flatpak_appid_parts(cmdline):
        if part in index:
            return True
    return False


def mark_user_apps(procs, app_index=None, home=None):
    """Set proc['user_app'] on every proc via is_user_app (in place)."""
    for proc in procs:
        proc['user_app'] = is_user_app(proc, app_index, home)
    return procs


def annotate_display_names(procs, names=None):
    """Set proc['display_name'] to the friendly .desktop Name= (in place).

    Lookup is argv[0]-basename first, then comm; anything without a
    known friendly name falls back to its comm so callers can render
    `display_name` unconditionally.
    """
    names = names or {}
    for proc in procs:
        cmdline = proc.get('cmdline') or proc.get('args') or ''
        base = _argv0_basename(cmdline)
        comm = proc.get('comm') or ''
        proc['display_name'] = (
            names.get(base) or names.get(comm.lower()) or comm)
    return procs


def categorize(proc, index=None):
    """Map a visible app proc to games/browsers/media/chat/other.

    Precedence: curated overrides (full cmdline match) > .desktop
    Categories > fallback 'other'.
    """
    comm = (proc.get('comm') or '').lower()
    cmdline = proc.get('cmdline') or proc.get('args') or ''
    haystack = (comm + ' ' + cmdline).lower()
    for cat in CATEGORIES:
        if cat == 'other':
            continue
        for rx in _OVERRIDE_RES[cat]:
            if rx.search(haystack):
                return cat
    if index:
        base = _argv0_basename(cmdline) or comm
        if base in index:
            return index[base]
        if comm in index:
            return index[comm]
    return 'other'


def group_by_category(procs, index=None):
    """Group procs into an ordered {category: [procs]} dict."""
    groups = {cat: [] for cat in CATEGORIES}
    for proc in procs:
        cat = categorize(proc, index)
        proc['category'] = cat
        groups[cat].append(proc)
    return groups


def etime_to_minutes(etime):
    """Parse a raw ps ETIME value into whole minutes (floored).

    Handles 'HH:MM:SS', 'MM:SS' (sub-hour ps output) and 'D-HH:MM:SS'
    forms. Returns None when the value cannot be parsed. Whole minutes
    only (seconds never surface) — matches the display convention.
    """
    try:
        days = 0
        rest = (etime or '').strip()
        if not rest:
            return None
        if '-' in rest:
            days_part, rest = rest.split('-', 1)
            days = int(days_part)
        fields = [int(f) for f in rest.split(':')]
        if len(fields) == 3:
            hours, minutes, seconds = fields
        elif len(fields) == 2:
            hours, minutes, seconds = 0, fields[0], fields[1]
        else:
            return None
        return max(0, days * 1440 + hours * 60 + minutes + seconds // 60)
    except (ValueError, AttributeError):
        return None


def format_minutes(total_minutes):
    """Format whole minutes as '3d 2h 5m' / '2h 5m' / '5m' / '0m'.

    Mirrors the shape of app.format_etime but takes minutes, so group
    aggregates never string-compare formatted times.
    """
    total = int(total_minutes)
    days, rem = divmod(total, 1440)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f'{days}d')
    if hours:
        parts.append(f'{hours}h')
    if minutes or not parts:
        parts.append(f'{minutes}m')
    return ' '.join(parts)


def format_hm(minutes):
    """Format a whole-minute duration as '1h 30m' / '45m' / '2h' / '0m'.

    The canonical user-facing duration formatter (budgets, allowances,
    bonus time): parents set and read these in hours+minutes, never
    converting units themselves. Unlike ``format_minutes`` above — which
    renders elapsed process age and adds a ``d`` component for runs past
    a day — budget-scale durations carry no days part.

    Pure and stdlib-only. ``None``, a non-number, or a negative value
    returns ``''`` so each call site decides its own empty placeholder
    (``'—'`` where a missing limit means "none set").
    """
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return ''
    if total < 0:
        return ''
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f'{hours}h {mins}m'
    if hours:
        return f'{hours}h'
    return f'{mins}m'


def group_by_comm(procs):
    """Group proc dicts by comm into group dicts.

    Input: [proc dict] with fields per list_user_processes (pid, comm,
    etime, ...) plus a 'category' when known (group_by_category sets it
    for visible procs; app.py annotates hidden procs before grouping).

    Output (non-empty groups only, sorted by comm for determinism):
      {comm, display_name, count, members (sorted by pid asc),
       max_minutes, max_elapsed, categories, uniform_category}

    display_name is the first (lowest-PID) member's annotated
    display_name, falling back to the comm when members carry none —
    so group rows render a friendly app name where one is known.

    max_minutes is the max over members of etime_to_minutes(etime) —
    computed from raw minutes, never by string-comparing formatted time.
    max_elapsed is that max formatted once (whole minutes only); it is
    '' when no member's etime parses.
    """
    buckets = {}
    for proc in procs:
        comm = proc.get('comm') or ''
        buckets.setdefault(comm, []).append(proc)
    out = []
    for comm in sorted(buckets):
        members = sorted(buckets[comm], key=lambda m: m.get('pid', 0))
        minutes = [m for m in
                   (etime_to_minutes(mem.get('etime')) for mem in members)
                   if m is not None]
        max_minutes = max(minutes) if minutes else None
        cats = sorted({m.get('category') for m in members
                       if m.get('category')})
        out.append({
            'comm': comm,
            'display_name': members[0].get('display_name') or comm,
            'count': len(members),
            'members': members,
            'max_minutes': max_minutes,
            'max_elapsed': (format_minutes(max_minutes)
                            if max_minutes is not None else ''),
            'categories': cats,
            'uniform_category': len(cats) <= 1,
        })
    return out


# ---------------------------------------------------------------------------
# Process listing (read-only ps + /proc cmdline enrichment)
# ---------------------------------------------------------------------------

def parse_cpu_seconds(value):
    """Parse ps TIME ([[dd-]hh:]mm:ss) into float seconds. None if invalid."""
    try:
        rest = value.strip()
        days = 0
        if '-' in rest:
            days_part, rest = rest.split('-', 1)
            days = int(days_part)
        fields = [int(f) for f in rest.split(':')]
        if len(fields) == 3:
            h, m, s = fields
        elif len(fields) == 2:
            h, m, s = 0, fields[0], fields[1]
        else:
            return None
        return float(days * 86400 + h * 3600 + m * 60 + s)
    except (ValueError, AttributeError):
        return None


def read_cmdline(pid):
    """Full command line from /proc (readable cross-user); '' on failure."""
    try:
        with open(f'/proc/{int(pid)}/cmdline', 'rb') as f:
            raw = f.read()
        parts = [p.decode('utf-8', 'replace') for p in raw.split(b'\0') if p]
        return ' '.join(parts)
    except (OSError, ValueError):
        return ''


def list_user_processes(username):
    """Read-only snapshot: [{pid, comm, etime, cpu_seconds, args, cmdline}]."""
    procs = []
    try:
        result = subprocess.run(
            ['ps', '-u', username, '-o', 'pid,comm,etime,time,args',
             '--cols', '1000'],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        return procs
    lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
    for line in lines[1:]:  # skip header
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cpu = parse_cpu_seconds(parts[3])
        args = parts[4] if len(parts) > 4 else ''
        cmdline = read_cmdline(pid) or args
        procs.append({
            'pid': pid,
            'comm': parts[1],
            'etime': parts[2],
            'cpu_seconds': cpu if cpu is not None else 0.0,
            'args': args,
            'cmdline': cmdline,
        })
    return procs


def compute_deltas(last_cpu, procs):
    """CPU-time deltas since the previous tick.

    last_cpu: {pid_str: {'comm': str, 'cpu': float}} from the prior tick.
    Returns (deltas, new_last_cpu) where deltas maps pid -> delta seconds
    (>= 0). First sightings and PID-reuse (comm changed) yield delta 0 —
    we only bill CPU we actually observed accumulate.
    """
    deltas = {}
    new_last = {}
    for proc in procs:
        pid = str(proc['pid'])
        cur = float(proc.get('cpu_seconds') or 0.0)
        prev = last_cpu.get(pid)
        if prev is not None and prev.get('comm') == proc.get('comm'):
            delta = max(0.0, cur - float(prev.get('cpu', 0.0)))
        else:
            delta = 0.0
        deltas[proc['pid']] = delta
        new_last[pid] = {'comm': proc.get('comm'), 'cpu': cur}
    return deltas, new_last


# ---------------------------------------------------------------------------
# budgets store (SQLite table holding the same JSON document per user;
# BUDGETS_FILE is the legacy import source and rollback mirror)
# Shape: {username: {"limits": {cat: minutes}, "day": "YYYY-MM-DD",
#   "used_seconds": {cat: float}, "last_cpu": {pid: {comm, cpu}},
#   "paused": {cat: bool}, "resumed": {cat: bool},
#   "manual_frozen": {cat: bool}, "manual_frozen_at": {cat: iso},
#   "warned_low": {cat: "YYYY-MM-DD"}, "warned_paused": {cat: "YYYY-MM-DD"},
#   "was_in_bedtime": bool}}
# ---------------------------------------------------------------------------

def load_budgets():
    return store.load_table(store.db_path_for(BUDGETS_FILE), 'budgets')


def save_budgets(data):
    store.save_table(store.db_path_for(BUDGETS_FILE), 'budgets',
                     data, BUDGETS_FILE)


def blank_entry():
    return {'limits': {}, 'day': None, 'used_seconds': {}, 'last_cpu': {},
            'paused': {}, 'resumed': {}, 'manual_frozen': {},
            'manual_frozen_at': {}, 'warned_low': {},
            'warned_paused': {}, 'was_in_bedtime': False,
            'last_tick': None,
            # Per-app runtime (parallel to the per-category keys above):
            # keyed by comm. Populated by the tick, reset in lockstep
            # with the daily reset.
            'app_used': {}, 'app_paused': {}, 'app_resumed': {},
            'app_manual_frozen': {}, 'app_manual_frozen_at': {},
            'app_warned_low': {}, 'app_warned_paused': {},
            # Weekly-window runtime: keyed by window id. ``window_used``
            # is deliberately NOT cleared at the daily reset while its
            # window is active (spans-midnight accumulation).
            'window_used': {}, 'window_active': {},
            # Focus engine (session agent) runtime. Defaults preserve the
            # CPU engine exactly; ``feed_seq`` is a drain cursor (never
            # reset by the daily reset) and ``feed_boot_id`` invalidates
            # carried-over sessions across a reboot.
            # feed_seq is the last-consumed feed seq: parse_feed ignores
            # rows with seq <= cursor, and the agent's first row is seq=1,
            # so the cursor starts at 0 (nothing consumed yet).
            'focus_engine': FOCUS_ENGINE_CPU, 'feed_seq': 0,
            'feed_last_seen': None, 'feed_agent': None,
            'feed_boot_id': None, 'feed_status': None}


def set_limit(username, category, minutes, actor='system'):
    """Set/clear a per-category daily budget. minutes<=0 clears the limit."""
    if not valid_username(username):
        return False, 'Invalid username'
    if not valid_category(category):
        return False, f'Unknown category: {category}'
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        return False, 'Minutes must be a number'
    data = load_budgets()
    entry = data.setdefault(username, blank_entry())
    limits = entry.setdefault('limits', {})
    if minutes <= 0:
        limits.pop(category, None)
        # Dropping the limit releases any freeze for that category
        # (auto-pause AND manual freeze alike).
        entry.get('paused', {}).pop(category, None)
        entry.get('resumed', {}).pop(category, None)
        entry.get('manual_frozen', {}).pop(category, None)
        entry.get('manual_frozen_at', {}).pop(category, None)
        try:
            save_budgets(data)
        except OSError as e:
            return False, str(e)
        ok, msg = set_frozen(username, category, False, actor=actor)
        audit.safe_log(actor, 'budget.limit_cleared',
                       f'{username}:{category}',
                       {'ok': True, 'unfreeze': msg})
        return True, 'Limit cleared' + ('' if ok else f' (unfreeze: {msg})')
    entry['limits'][category] = minutes
    try:
        save_budgets(data)
    except OSError as e:
        return False, str(e)
    audit.safe_log(actor, 'budget.set_limit', f'{username}:{category}',
                   {'minutes': minutes})
    return True, 'OK'


def budget_status(username, data=None, overrides=None, now=None):
    """UI-friendly status: {cat: {minutes, used_minutes, remaining_minutes,
    paused}} for every category, plus an ``app_budgets`` map.

    Legacy per-category keys stay byte-identical (regression guard); the
    new keys are additive: ``window_active`` and ``effective_source``
    (``window|daily|none``). All displayed times are whole minutes —
    seconds are never shown.
    """
    if data is None:
        data = load_budgets()
    if overrides is None:
        try:
            overrides = load_overrides().get(username)
        except Exception:
            overrides = None
    now = now or datetime.now()
    entry = data.get(username, {})
    limits = entry.get('limits', {})
    used = entry.get('used_seconds', {})
    paused = entry.get('paused', {})
    manual_frozen = entry.get('manual_frozen', {})
    active_by_cat = {}
    for _wid, win in iter_windows(overrides):
        if (win['target_type'] == 'category' and win['enabled']
                and _in_window(win, now)):
            active_by_cat[win['target']] = win
    status = {}
    for cat in CATEGORIES:
        limit_min = float(limits.get(cat, 0) or 0)
        used_sec = float(used.get(cat, 0) or 0)
        is_paused = bool(paused.get(cat, False))
        is_manual = bool(manual_frozen.get(cat, False))
        win = active_by_cat.get(cat)
        if win is not None:
            effective_source = 'window'
        elif limit_min > 0:
            effective_source = 'daily'
        else:
            effective_source = 'none'
        status[cat] = {
            'minutes': limit_min,
            'used_minutes': int(round(used_sec / 60.0)),
            'remaining_minutes': int(round(max(0.0, limit_min * 60 - used_sec) / 60.0))
            if limit_min > 0 else None,
            'paused': is_paused,
            'exhausted': bool(limit_min > 0 and used_sec >= limit_min * 60),
            'manual_frozen': is_manual,
            'frozen': bool(is_paused or is_manual),
            'window_active': bool(win),
            'effective_source': effective_source,
            'window_allowance_minutes': (win['allowance_minutes']
                                         if win is not None else None),
        }
    status['app_budgets'] = app_budget_status(
        username, data=data, overrides=overrides, now=now)
    return status


def app_budget_status(username, data=None, overrides=None, now=None):
    """Per-app view for the UI/live payload, keyed by comm.

    {'mode', 'exempt', 'minutes', 'used_minutes', 'remaining_minutes',
    'paused', 'manual_frozen', 'frozen', 'window_active',
    'effective_source'} — only for comms with a valid override.
    """
    if data is None:
        data = load_budgets()
    if overrides is None:
        try:
            overrides = load_overrides().get(username)
        except Exception:
            overrides = None
    now = now or datetime.now()
    entry = data.get(username) if isinstance(data, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    app_used = entry.get('app_used', {}) or {}
    app_paused = entry.get('app_paused', {}) or {}
    app_manual = entry.get('app_manual_frozen', {}) or {}
    window_used = entry.get('window_used', {}) or {}
    out = {}
    for comm, ov in iter_overrides(overrides):
        is_paused = bool(app_paused.get(comm, False))
        is_manual = bool(app_manual.get(comm, False))
        if ov['mode'] == 'exempt':
            out[comm] = {
                'mode': 'exempt', 'exempt': True, 'minutes': None,
                'used_minutes': 0, 'remaining_minutes': None,
                'paused': is_paused, 'manual_frozen': is_manual,
                'frozen': bool(is_paused or is_manual),
                'window_active': False, 'effective_source': 'exempt',
            }
            continue
        win = active_window(overrides, 'app', comm, now)
        if win is not None:
            limit_min = float(win['allowance_minutes'])
            used_sec = float(window_used.get(win['id'], 0.0) or 0.0)
            source = 'window'
        else:
            limit_min = float(ov['minutes'])
            used_sec = float(app_used.get(comm, 0.0) or 0.0)
            source = 'daily'
        out[comm] = {
            'mode': 'limit', 'exempt': False, 'minutes': limit_min,
            'used_minutes': int(round(used_sec / 60.0)),
            'remaining_minutes': int(round(
                max(0.0, limit_min * 60 - used_sec) / 60.0)),
            'paused': is_paused, 'manual_frozen': is_manual,
            'frozen': bool(is_paused or is_manual),
            'window_active': bool(win), 'effective_source': source,
        }
    return out


def group_usage_minutes(category, comm, budget_state):
    """Billable minutes a process group has accrued since the last reset.

    The Processes table shows budget USAGE, not the process's ps ETIME
    wall-clock age, so it resets with the budget day reset (and a
    matching window reset) exactly like the budget rows. An app override
    governs its comm: use the override's per-app ``used_minutes`` (``0``
    for an ``exempt`` override, which is never billed at all). Otherwise
    the owning CATEGORY's ``used_minutes`` applies.

    ``budget_state`` is a ``budget_status()`` payload. Unknown comm and
    category both return ``None`` so the caller can render its own empty
    placeholder (``'—'``).
    """
    if not isinstance(budget_state, dict):
        return None
    app_budgets = budget_state.get('app_budgets') or {}
    app = app_budgets.get(comm) if isinstance(app_budgets, dict) else None
    if isinstance(app, dict) and 'used_minutes' in app:
        return app.get('used_minutes')
    cat = budget_state.get(category)
    if isinstance(cat, dict) and 'used_minutes' in cat:
        return cat.get('used_minutes')
    return None


def budget_reset_needed(entry_day, has_bedtime, was_in_bedtime, now_in_bedtime,
                        today_str):
    """Pure reset rule (unit-tested):

    - Bedtime users: reset on the bedtime-end transition (was in bedtime,
      now out). Overnight windows keep accruing past midnight until the
      morning transition.
    - No-bedtime users: reset on the first tick of a new calendar date.
    - Staleness guard: any stored day older than today while currently out
      of bedtime resets (covers downtime spanning the transition).
    """
    if was_in_bedtime and not now_in_bedtime:
        return True
    if entry_day != today_str and not now_in_bedtime:
        return True
    return False


# ---------------------------------------------------------------------------
# overrides config store — per-app allowances/exemptions + weekly windows
# (whole-document SQLite table; OVERRIDES_FILE is the legacy import source
# and rollback mirror)
#
# Shape: {username: {"app_overrides": {comm: {...}}, "windows": {wid: {...}}}}
# Fail closed everywhere: a malformed override/window is treated as absent
# (category rules govern) and must never silently exempt an app.
# ---------------------------------------------------------------------------

def load_overrides():
    return store.load_table(store.db_path_for(OVERRIDES_FILE), 'overrides')


def save_overrides(data):
    store.save_table(store.db_path_for(OVERRIDES_FILE), 'overrides',
                     data, OVERRIDES_FILE)


def window_id_for(target_type, target):
    """Stable per-target window id (V1: one window per target)."""
    return 'w-%s-%s' % (target_type, target)


def _config_entry(data, username):
    """Per-user config doc with both top-level keys present."""
    entry = data.get(username)
    if not isinstance(entry, dict):
        entry = {}
        data[username] = entry
    if not isinstance(entry.get('app_overrides'), dict):
        entry['app_overrides'] = {}
    if not isinstance(entry.get('windows'), dict):
        entry['windows'] = {}
    return entry


def normalize_override(raw):
    """A validated app override, or None (fail closed).

    {'mode': 'limit'|'exempt', 'minutes': float|None, 'set_by', 'set_at'}
    Unknown mode / missing / non-positive minutes -> None: the override is
    ignored and the category rules govern (never silently exempt).
    """
    if not isinstance(raw, dict):
        return None
    mode = raw.get('mode')
    if mode == 'exempt':
        return {'mode': 'exempt', 'minutes': None,
                'set_by': raw.get('set_by'), 'set_at': raw.get('set_at')}
    if mode != 'limit':
        return None
    try:
        minutes = float(raw.get('minutes'))
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    return {'mode': 'limit', 'minutes': minutes,
            'set_by': raw.get('set_by'), 'set_at': raw.get('set_at')}


def normalize_window(raw, window_id=None):
    """A validated window dict, or None (fail closed).

    Unknown target/target_type, empty/bad ``days``, unparseable times,
    ``start == end``, or a sub-``WINDOW_MIN_MINUTES`` allowance -> None
    (the window is ignored; the daily rules govern).
    """
    if not isinstance(raw, dict):
        return None
    target_type = raw.get('target_type')
    target = raw.get('target')
    if target_type == 'category':
        if not valid_category(target):
            return None
    elif target_type == 'app':
        if not valid_comm(target):
            return None
    else:
        return None
    start = _parse_hhmm(raw.get('start', ''))
    end = _parse_hhmm(raw.get('end', ''))
    if start is None or end is None or start == end:
        return None
    try:
        days = sorted({int(d) for d in raw.get('days', [])})
    except (TypeError, ValueError):
        return None
    if not days or any(d < 0 or d > 6 for d in days):
        return None
    try:
        allowance = int(raw.get('allowance_minutes'))
    except (TypeError, ValueError):
        return None
    if allowance < WINDOW_MIN_MINUTES:
        return None
    return {
        'id': window_id or window_id_for(target_type, target),
        'target_type': target_type,
        'target': target,
        'days': days,
        'start': raw.get('start'),
        'end': raw.get('end'),
        'allowance_minutes': allowance,
        'enabled': bool(raw.get('enabled', True)),
        'set_by': raw.get('set_by'),
        'set_at': raw.get('set_at'),
    }


def parse_days(value):
    """CSV/list of day numbers -> sorted list, or None on any bad token."""
    if isinstance(value, (list, tuple, set, frozenset)):
        tokens = list(value)
    else:
        tokens = str(value or '').replace(' ', '').split(',')
    out = set()
    for token in tokens:
        if token in ('', None):
            continue
        try:
            day = int(token)
        except (TypeError, ValueError):
            return None
        if day < 0 or day > 6:
            return None
        out.add(day)
    return sorted(out) or None


def iter_overrides(overrides):
    """(comm, normalized_override) for every valid app override."""
    if not isinstance(overrides, dict):
        return
    raw = overrides.get('app_overrides')
    if not isinstance(raw, dict):
        return
    for comm, value in raw.items():
        if not valid_comm(comm):
            continue
        norm = normalize_override(value)
        if norm is not None:
            yield comm, norm


def iter_windows(overrides):
    """(window_id, normalized_window) for every valid configured window."""
    if not isinstance(overrides, dict):
        return
    raw = overrides.get('windows')
    if not isinstance(raw, dict):
        return
    for wid, value in raw.items():
        norm = normalize_window(value, window_id=wid)
        if norm is not None:
            yield wid, norm


def override_map(overrides):
    return {comm: ov for comm, ov in iter_overrides(overrides)}


def active_window(overrides, target_type, target, now):
    """The enabled, currently-active window governing ``target`` (or None)."""
    for _wid, win in iter_windows(overrides):
        if win['target_type'] != target_type or win['target'] != target:
            continue
        if win['enabled'] and _in_window(win, now):
            return win
    return None


def overrides_view(username, data=None):
    """UI-facing config view: {'app_overrides': {...}, 'windows': [...]}."""
    if data is None:
        data = load_overrides()
    entry = data.get(username) if isinstance(data, dict) else None
    raw = entry if isinstance(entry, dict) else {}
    view = {
        'app_overrides': {comm: ov for comm, ov in iter_overrides(raw)},
        'windows': [win for _wid, win in iter_windows(raw)],
    }
    return view


def _reset_window_runtime(username, window_id):
    """Drop one window's runtime counters from the budgets doc."""
    try:
        data = load_budgets()
    except Exception:
        return
    entry = data.get(username)
    if not isinstance(entry, dict):
        return
    for key in ('window_used', 'window_active'):
        flags = entry.get(key)
        if isinstance(flags, dict):
            flags.pop(window_id, None)
    try:
        save_budgets(data)
    except OSError:
        pass


def _clear_app_runtime(username, comm):
    """Drop one app's runtime flags from the budgets doc (best effort)."""
    try:
        data = load_budgets()
    except Exception:
        return
    entry = data.get(username)
    if not isinstance(entry, dict):
        return
    for key in ('app_paused', 'app_resumed', 'app_manual_frozen',
                'app_manual_frozen_at'):
        flags = entry.get(key)
        if isinstance(flags, dict):
            flags.pop(comm, None)
    try:
        save_budgets(data)
    except OSError:
        pass


def set_app_override(username, comm, mode, minutes=None, actor='system'):
    """Set/clear a per-app override.

    mode 'exempt' means unlimited (never billed, never placed in a
    cgroup); 'limit' needs a positive integer ``minutes``. Any other
    mode, or a non-positive ``minutes`` with mode=limit, CLEARS the
    override. Clearing also releases the app's freeze and thaws its
    group (mirrors ``set_limit``).
    """
    if not valid_username(username):
        return False, 'Invalid username'
    if not valid_comm(comm):
        return False, f'Invalid command: {comm}'
    data = load_overrides()
    entry = _config_entry(data, username)
    overrides = entry['app_overrides']

    mode = (mode or '').strip().lower()
    to_set = None
    clear = False
    if mode in ('', 'none', 'absent', 'clear'):
        clear = True
    elif mode == 'exempt':
        to_set = {'mode': 'exempt'}
    elif mode == 'limit':
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            return False, 'Minutes must be a number'
        if minutes < 0:
            return False, 'Minutes must not be negative'
        if minutes == 0:
            clear = True
        else:
            to_set = {'mode': 'limit', 'minutes': int(minutes)}
    else:
        return False, f'Unknown mode: {mode}'

    if clear:
        overrides.pop(comm, None)
        try:
            save_overrides(data)
        except OSError as e:
            return False, str(e)
        _clear_app_runtime(username, comm)
        ok, msg = set_frozen_app(username, comm, False, actor=actor)
        audit.safe_log(actor, 'budget.override_cleared',
                       f'{username}:{comm}',
                       {'ok': True, 'unfreeze': msg})
        return True, ('Override cleared'
                      + ('' if ok else f' (unfreeze: {msg})'))

    to_set['set_by'] = actor
    to_set['set_at'] = datetime.now().isoformat()
    overrides[comm] = to_set
    try:
        save_overrides(data)
    except OSError as e:
        return False, str(e)
    audit.safe_log(actor, 'budget.override_set', f'{username}:{comm}',
                   {'mode': to_set['mode'],
                    'minutes': to_set.get('minutes')})
    return True, 'OK'


def set_window(username, target_type, target, days, start, end,
               allowance_minutes, enabled=True, actor='system'):
    """Create a weekly window for a target.

    V1: one window per target. A second create for the same target is a
    collision -> (False, reason); use ``update_window`` to edit it.
    """
    if not valid_username(username):
        return False, 'Invalid username'
    if target_type == 'category':
        if not valid_category(target):
            return False, f'Unknown category: {target}'
    elif target_type == 'app':
        if not valid_comm(target):
            return False, f'Invalid command: {target}'
    else:
        return False, f'Unknown target type: {target_type}'
    days = parse_days(days)
    if days is None:
        return False, 'Invalid days'
    window_id = window_id_for(target_type, target)
    data = load_overrides()
    entry = _config_entry(data, username)
    if window_id in entry['windows']:
        return False, 'A window already exists for this target'
    norm = normalize_window({
        'target_type': target_type, 'target': target, 'days': days,
        'start': start, 'end': end, 'allowance_minutes': allowance_minutes,
        'enabled': enabled, 'set_by': actor,
        'set_at': datetime.now().isoformat(),
    }, window_id=window_id)
    if norm is None:
        return False, 'Invalid window fields'
    entry['windows'][window_id] = norm
    try:
        save_overrides(data)
    except OSError as e:
        return False, str(e)
    audit.safe_log(actor, 'budget.window_set', f'{username}:{window_id}',
                   {'target_type': target_type, 'target': target,
                    'days': norm['days'], 'start': norm['start'],
                    'end': norm['end'],
                    'allowance_minutes': norm['allowance_minutes'],
                    'enabled': norm['enabled']})
    return True, 'OK'


def update_window(username, window_id, days=None, start=None, end=None,
                  allowance_minutes=None, enabled=None, actor='system'):
    """Edit/disable an existing window (keyed by its stable id)."""
    if not valid_username(username):
        return False, 'Invalid username'
    data = load_overrides()
    entry = _config_entry(data, username)
    windows = entry['windows']
    stored = windows.get(window_id)
    if not isinstance(stored, dict):
        return False, 'No such window'
    merged = dict(stored)
    if days is not None:
        merged['days'] = parse_days(days)
        if merged['days'] is None:
            return False, 'Invalid days'
    if start is not None:
        merged['start'] = start
    if end is not None:
        merged['end'] = end
    if allowance_minutes is not None:
        merged['allowance_minutes'] = allowance_minutes
    if enabled is not None:
        merged['enabled'] = bool(enabled)
    norm = normalize_window(merged, window_id=window_id)
    if norm is None:
        return False, 'Invalid window fields'
    norm['set_by'] = actor
    norm['set_at'] = datetime.now().isoformat()
    windows[window_id] = norm
    try:
        save_overrides(data)
    except OSError as e:
        return False, str(e)
    if enabled is not None and not bool(enabled):
        # Disabling resets the window's runtime and releases its freeze.
        _reset_window_runtime(username, window_id)
        _release_window_freeze(username, norm, actor=actor)
    audit.safe_log(actor, 'budget.window_set', f'{username}:{window_id}',
                   {'target_type': norm['target_type'],
                    'target': norm['target'],
                    'enabled': norm['enabled']})
    return True, 'OK'


def clear_window(username, window_id=None, target_type=None, target=None,
                 actor='system'):
    """Remove a window, reset its runtime, and thaw its target."""
    if not valid_username(username):
        return False, 'Invalid username'
    if window_id is None:
        if not target_type or not target:
            return False, 'window id required'
        window_id = window_id_for(target_type, target)
    data = load_overrides()
    entry = _config_entry(data, username)
    removed = entry['windows'].pop(window_id, None)
    if not isinstance(removed, dict):
        return False, 'No such window'
    try:
        save_overrides(data)
    except OSError as e:
        return False, str(e)
    _reset_window_runtime(username, window_id)
    norm = normalize_window(removed, window_id=window_id)
    if norm is not None:
        _release_window_freeze(username, norm, actor=actor)
    audit.safe_log(actor, 'budget.window_cleared', f'{username}:{window_id}',
                   {'ok': True})
    return True, 'Window cleared'


def _release_window_freeze(username, window, actor='system'):
    """Thaw a window's target group (open/close/disable/clear)."""
    if window['target_type'] == 'category':
        return set_frozen(username, window['target'], False, actor=actor)
    return set_frozen_app(username, window['target'], False, actor=actor)


# ---------------------------------------------------------------------------
# Cgroup freezer ops (privileged via sudo tee; validated username/category)
# ---------------------------------------------------------------------------

def cgroup_path(username, category):
    if not valid_username(username) or not valid_category(category):
        raise ValueError('Invalid username or category for cgroup path')
    return os.path.join(CGROUP_ROOT, f'parental-{username}-{category}')


def cgroup_path_app(username, comm):
    """Path of the per-app group ``parental-<user>-app-<comm>``.

    ``valid_comm`` pins the same charset the sudoers grant is already
    anchored on (``parental-[A-Za-z0-9_.-]+``), so no privilege grant is
    needed for these groups.
    """
    if not valid_username(username) or not valid_comm(comm):
        raise ValueError('Invalid username or comm for app cgroup path')
    return os.path.join(CGROUP_ROOT, f'parental-{username}-app-{comm}')


def _sudo_read_file(path):
    try:
        result = subprocess.run(
            ['sudo', '-n', 'cat', path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception:
        return None


def _sudo_write_file(path, content):
    """Write via `sudo tee` (the only privileged write pattern used)."""
    try:
        result = subprocess.run(
            ['sudo', '-n', 'tee', path],
            input=content, capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0, (
            '' if result.returncode == 0
            else (result.stderr.strip() or 'tee failed'))
    except Exception as e:
        return False, str(e)


def _ensure_cgroup_dir(path):
    """Create a cgroup dir (sudo mkdir -p); never touches other groups."""
    if os.path.isdir(path):
        return True, ''
    try:
        result = subprocess.run(
            ['sudo', '-n', 'mkdir', '-p', path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or 'mkdir failed'
        return True, ''
    except Exception as e:
        return False, str(e)


def ensure_cgroup(username, category):
    """Create parental-<user>-<cat> if missing. Never touches other groups."""
    return _ensure_cgroup_dir(cgroup_path(username, category))


def ensure_cgroup_app(username, comm):
    """Create parental-<user>-app-<comm> if missing."""
    return _ensure_cgroup_dir(cgroup_path_app(username, comm))


def _read_freeze_path(path):
    content = _sudo_read_file(path)
    if content is None:
        return None
    return content.strip() == '1'


def read_freeze(username, category):
    return _read_freeze_path(os.path.join(cgroup_path(username, category),
                                          'cgroup.freeze'))


def read_freeze_app(username, comm):
    return _read_freeze_path(os.path.join(cgroup_path_app(username, comm),
                                          'cgroup.freeze'))


def _set_frozen_path(path, frozen, actor, target, ensure):
    """Freeze/thaw one cgroup.freeze file; verify by readback.

    Shared by the category and per-app helpers. The audit hook is
    best-effort and can never fail the freeze itself.
    """
    if not os.path.isdir(os.path.dirname(path)):
        if frozen:
            ok, msg = ensure()
            if not ok:
                return False, msg
        else:
            audit.safe_log(actor, 'budget.thaw', target,
                           {'ok': True, 'note': 'no cgroup (already unfrozen)',
                            'frozen': False})
            return True, 'no cgroup (already unfrozen)'
    ok, msg = _sudo_write_file(path, '1' if frozen else '0')
    if not ok:
        audit.safe_log(actor, 'budget.freeze' if frozen else 'budget.thaw',
                       target, {'ok': False, 'error': msg,
                                'frozen': bool(frozen)})
        return False, msg
    content = _sudo_read_file(path)
    if content is None:
        audit.safe_log(actor, 'budget.freeze' if frozen else 'budget.thaw',
                       target,
                       {'ok': False, 'error': 'could not verify freeze state',
                        'frozen': bool(frozen)})
        return False, 'could not verify freeze state'
    state = content.strip() == '1'
    if state != bool(frozen):
        audit.safe_log(actor, 'budget.freeze' if frozen else 'budget.thaw',
                       target,
                       {'ok': False,
                        'error': 'freeze state mismatch after write',
                        'frozen': bool(frozen)})
        return False, 'freeze state mismatch after write'
    audit.safe_log(actor, 'budget.freeze' if frozen else 'budget.thaw',
                   target, {'ok': True, 'frozen': bool(frozen)})
    return True, ''


def set_frozen(username, category, frozen, actor='system'):
    """Freeze (True) or thaw (False) a category cgroup; verify by readback."""
    try:
        path = os.path.join(cgroup_path(username, category), 'cgroup.freeze')
    except ValueError as e:
        return False, str(e)
    return _set_frozen_path(path, frozen, actor, f'{username}:{category}',
                            lambda: ensure_cgroup(username, category))


def set_frozen_app(username, comm, frozen, actor='system'):
    """Freeze (True) or thaw (False) a per-app cgroup; verify by readback."""
    try:
        path = os.path.join(cgroup_path_app(username, comm), 'cgroup.freeze')
    except ValueError as e:
        return False, str(e)
    return _set_frozen_path(path, frozen, actor, f'{username}:app:{comm}',
                            lambda: ensure_cgroup_app(username, comm))


def _cgroup_members_path(path):
    content = _sudo_read_file(path)
    if content is None:
        return []
    pids = []
    for token in content.split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def cgroup_members(username, category):
    """PIDs currently in the category cgroup ([] if missing/unreadable)."""
    try:
        path = os.path.join(cgroup_path(username, category), 'cgroup.procs')
    except ValueError:
        return []
    return _cgroup_members_path(path)


def cgroup_members_app(username, comm):
    """PIDs currently in the per-app cgroup ([] if missing/unreadable)."""
    try:
        path = os.path.join(cgroup_path_app(username, comm), 'cgroup.procs')
    except ValueError:
        return []
    return _cgroup_members_path(path)


def migrate_pid(username, category, pid):
    """Move one PID into the category cgroup (no-op behaviorally)."""
    try:
        path = os.path.join(cgroup_path(username, category), 'cgroup.procs')
    except ValueError as e:
        return False, str(e)
    return _sudo_write_file(path, str(int(pid)))


def migrate_pid_app(username, comm, pid):
    """Move one PID into the per-app cgroup (no-op behaviorally)."""
    try:
        path = os.path.join(cgroup_path_app(username, comm), 'cgroup.procs')
    except ValueError as e:
        return False, str(e)
    return _sudo_write_file(path, str(int(pid)))


APP_BUCKET_PREFIX = 'app-'


def _app_bucket(comm):
    """Cgroup bucket name for a per-app group (the suffix after <user>-)."""
    return APP_BUCKET_PREFIX + comm


def split_bucket(bucket):
    """('app', comm) | ('category', cat) | None for a parental suffix."""
    if bucket.startswith(APP_BUCKET_PREFIX):
        comm = bucket[len(APP_BUCKET_PREFIX):]
        if valid_comm(comm):
            return 'app', comm
        return None
    if valid_category(bucket):
        return 'category', bucket
    return None


def thaw_bucket(username, bucket, actor='system'):
    """Thaw one managed bucket (category or app), dispatching by kind."""
    parts = split_bucket(bucket)
    if parts is None:
        return False, 'unknown cgroup bucket'
    kind, target = parts
    if kind == 'app':
        return set_frozen_app(username, target, False, actor=actor)
    return set_frozen(username, target, False, actor=actor)


def unfreeze_all_user(username, actor='system', keep_categories=None,
                      keep_apps=None):
    """Thaw every parental-<username>-* cgroup (category + app groups).

    keep_categories/keep_apps list targets governed by an ACTIVE weekly
    window: their groups are skipped so a day reset does not undo a
    window-exhaustion freeze (the window close thaws them instead).
    """
    if not valid_username(username):
        return False, 'Invalid username'
    keep_categories = set(keep_categories or ())
    keep_apps = set(keep_apps or ())
    results = []
    try:
        names = os.listdir(CGROUP_ROOT)
    except OSError as e:
        return False, str(e)
    prefix = f'parental-{username}-'
    for name in names:
        if not name.startswith(prefix):
            continue
        bucket = name[len(prefix):]
        parts = split_bucket(bucket)
        if parts is None:
            continue
        kind, target = parts
        if kind == 'category':
            if target in keep_categories:
                continue
            ok, msg = set_frozen(username, target, False, actor=actor)
        else:
            if target in keep_apps:
                continue
            ok, msg = set_frozen_app(username, target, False, actor=actor)
        results.append((bucket, ok, msg))
    audit.safe_log(actor, 'budget.unfreeze_all', username,
                   {'thawed': [b for b, ok, _m in results if ok]})
    return True, results


# ---------------------------------------------------------------------------
# Freezer watchdog: detect + self-heal stuck/orphan freezes (prototype)
# ---------------------------------------------------------------------------
# Incident (2026-10-09): a stale `parental-tester-quarantine` cgroup held an
# entire desktop session frozen (cgroup.freeze=1, 17 procs incl.
# kwin/pipewire/dbus) with no corresponding budget state. The desktop stayed
# black until manually thawed. This watchdog makes that class of incident
# self-heal on the next 60s tick.
#
# Ordering contract (no flapping vs. legitimate freezes): enforcement runs
# FIRST, watchdog SECOND, against the SAME in-memory budgets dict — so a
# category the current tick legitimately froze already has
# paused[category]=True set and the watchdog sees it as legitimate.
# `tick_budgets()` implements exactly this order (see below).
#
# Frozen means BOTH files agree: desired state `cgroup.freeze == '1'` AND
# effective state `cgroup.events` reports `frozen 1`. A cgroup mid-transition
# (freeze=1 written, events still `frozen 0` while processes settle) is
# skipped, never thawed — this avoids flapping with a freeze that is still
# landing. Any unreadable/ambiguous state is logged + skipped, never thawed
# blindly. Only UNFREEZE writes are ever issued, only on OUR
# `parental-<user>-<category>` and `parental-<user>-app-<comm>` groups.
# No signals, no kills, no membership changes anywhere.

def parse_managed_cgroup(name):
    """Split a managed group name -> (username, bucket), or None.

    ``bucket`` is a category name (``parental-<user>-<category>``) or an
    ``app-<comm>`` bucket (``parental-<user>-app-<comm>``); use
    ``split_bucket`` to tell them apart. Returns None for anything not
    matching our naming scheme (foreign groups such as a reappearing
    ``parental-*-quarantine``). The category split is on the LAST hyphen
    so hyphenated usernames still parse; app groups are recognized by the
    ``-app-`` infix (a real category never contains a hyphen).
    """
    if not name.startswith('parental-'):
        return None
    rest = name[len('parental-'):]
    if '-' not in rest:
        return None
    username, last = rest.rsplit('-', 1)
    if valid_username(username) and valid_category(last):
        return username, last
    idx = rest.rfind('-' + APP_BUCKET_PREFIX)
    if idx > 0:
        username = rest[:idx]
        comm = rest[idx + len(APP_BUCKET_PREFIX) + 1:]
        if valid_username(username) and valid_comm(comm):
            return username, _app_bucket(comm)
    return None


def _read_desired_frozen(cgroup_dir):
    """Desired freezer state from `cgroup.freeze`: True/False/None.

    None = missing, unreadable, or unexpected content (ambiguous: the
    caller must skip, never thaw blindly). Plain open() on purpose: these
    files are world-readable, and this keeps the watchdog usable without
    sudo for reads.
    """
    try:
        with open(os.path.join(cgroup_dir, 'cgroup.freeze'), 'r') as f:
            content = f.read().strip()
    except OSError:
        return None
    if content == '1':
        return True
    if content == '0':
        return False
    return None


def _read_effective_frozen(cgroup_dir):
    """Effective freezer state from `cgroup.events` (`frozen 1/0`).

    None = missing, unreadable, or unparseable (ambiguous: skip).
    """
    try:
        with open(os.path.join(cgroup_dir, 'cgroup.events'), 'r') as f:
            text = f.read()
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == 'frozen':
            if parts[1] == '1':
                return True
            if parts[1] == '0':
                return False
            return None
    return None


def watchdog_tick(budgets_data=None, cgroup_root=None, thaw_fn=None):
    """One watchdog pass: thaw stuck/orphan freezes, report foreign groups.

    budgets_data: budget-store-shaped dict (default: loaded from the store).
    cgroup_root: directory to scan (default: real /sys/fs/cgroup).
    thaw_fn: callable(username, bucket) -> (ok, msg) used for the
      unfreeze write, where bucket is a category or ``app-<comm>``
      (default: thaw_bucket on the real tree, direct '0' write on any
      alternate root so tests never touch sudo or the real hierarchy).

    Returns a summary dict {thawed, legit, clean, unknown, skipped, note}.
    Only OUR managed groups are ever thawed, and only when truly frozen
    (desired AND effective) with NO paused/manual-frozen flag for that
    user/bucket (category flags for category groups, ``app_*`` flags for
    app groups).
    Foreign `parental-*` groups are logged loudly and reported in
    `unknown` but NEVER touched.
    """
    root = cgroup_root or CGROUP_ROOT
    if budgets_data is None:
        try:
            budgets_data = load_budgets()
        except Exception as e:
            note = f'WATCHDOG: could not load budget state, skipping pass: {e}'
            print(note, flush=True)
            return {'thawed': [], 'legit': [], 'clean': [], 'unknown': [],
                    'skipped': [], 'note': note}
    if not isinstance(budgets_data, dict):
        budgets_data = {}
    if thaw_fn is None:
        if root != CGROUP_ROOT:
            # Alternate/test root: direct write, no sudo, no real tree.
            def thaw_fn(username, bucket, _root=root):
                try:
                    cdir = os.path.join(
                        _root, f'parental-{username}-{bucket}')
                    with open(os.path.join(cdir, 'cgroup.freeze'), 'w') as f:
                        f.write('0')
                except (OSError, ValueError) as e:
                    return False, str(e)
                return True, ''
        else:
            thaw_fn = thaw_bucket
    summary = {'thawed': [], 'legit': [], 'clean': [], 'unknown': [],
               'skipped': [], 'note': ''}
    try:
        names = os.listdir(root)
    except OSError as e:
        note = f'WATCHDOG: cannot list {root}, skipping pass: {e}'
        print(note, flush=True)
        summary['note'] = note
        return summary
    for name in names:
        if not name.startswith('parental-'):
            continue
        parsed = parse_managed_cgroup(name)
        if parsed is None:
            print(f'WATCHDOG: unknown parental cgroup {name} present; '
                  f'leaving untouched (foreign group, report only)',
                  flush=True)
            summary['unknown'].append(name)
            continue
        username, bucket = parsed
        parts = split_bucket(bucket)
        if parts is None:
            summary['unknown'].append(name)
            continue
        kind, target = parts
        cdir = os.path.join(root, name)
        desired = _read_desired_frozen(cdir)
        if desired is None:
            print(f'WATCHDOG: {name}: cannot read cgroup.freeze, '
                  f'skipping (never thaw on ambiguous state)', flush=True)
            summary['skipped'].append(name + ' (unreadable freeze state)')
            continue
        if not desired:
            summary['clean'].append(name)
            continue
        effective = _read_effective_frozen(cdir)
        if effective is None:
            print(f'WATCHDOG: {name}: freeze=1 but cgroup.events unreadable, '
                  f'skipping (never thaw on ambiguous state)', flush=True)
            summary['skipped'].append(name + ' (unreadable events)')
            continue
        if not effective:
            # Freeze requested but not yet effective: still settling.
            # Skip quietly-ish so the next tick re-checks without flapping.
            summary['skipped'].append(
                name + ' (transition: freeze=1, frozen 0)')
            continue
        # Truly frozen (desired + effective). Legitimate ONLY when the
        # budget state marks this bucket paused (auto-exhaustion) OR
        # manually frozen by the parent. App buckets consult the parallel
        # app_* maps.
        if kind == 'app':
            paused_key, manual_key = 'app_paused', 'app_manual_frozen'
        else:
            paused_key, manual_key = 'paused', 'manual_frozen'
        try:
            user_entry = budgets_data.get(username) or {}
            paused = bool(user_entry.get(paused_key, {}).get(target, False))
            manual = bool(user_entry.get(manual_key, {}).get(target, False))
        except (AttributeError, TypeError):
            paused, manual = False, False
        if paused or manual:
            summary['legit'].append(name)
            continue
        print(f'WATCHDOG: STUCK freeze detected: {name} is frozen '
              f'(freeze=1, frozen 1) with NO paused budget state; '
              f'thawing to self-heal', flush=True)
        try:
            ok, msg = thaw_fn(username, bucket)
        except Exception as e:
            ok, msg = False, str(e)
        if ok:
            print(f'WATCHDOG: THAWED stuck cgroup {name} '
                  f'(orphan freeze self-healed)', flush=True)
            summary['thawed'].append(name)
        else:
            print(f'WATCHDOG: FAILED to thaw stuck cgroup {name}: {msg}',
                  flush=True)
            summary['skipped'].append(name + f' (thaw failed: {msg})')
    if summary['thawed']:
        summary['note'] = ('self-healed stuck freeze(s): '
                           + ', '.join(summary['thawed']))
    if summary['unknown']:
        note = ('foreign parental cgroup(s) present, left untouched: '
                + ', '.join(summary['unknown']))
        summary['note'] = (summary['note'] + '; ' + note if summary['note']
                           else note)
    return summary


def verify_freeze_safety(username, member_pids, expected_freezable):
    """Freeze-time gate: every cgroup member must be owned by the user and
    not on the NEVER-pause list. expected_freezable maps pid -> proc dict
    from a fresh snapshot. Returns (ok, reason)."""
    try:
        uid = pwd.getpwnam(username).pw_uid
    except KeyError:
        return False, f'unknown user {username}'
    expected = set(expected_freezable)
    for pid in member_pids:
        try:
            st = os.stat(f'/proc/{pid}')
        except OSError:
            continue  # exited since the read; harmless
        if st.st_uid != uid:
            return False, f'PID {pid} not owned by {username}; aborting freeze'
        proc = expected_freezable.get(pid)
        if proc is None:
            # Member we did not classify this tick (stale/unknown): read its
            # comm and apply the NEVER-pause gate before allowing a freeze.
            try:
                with open(f'/proc/{pid}/comm') as f:
                    comm = f.read().strip()
            except OSError:
                continue
            if is_never_pause({'comm': comm, 'cmdline': read_cmdline(pid)}):
                return False, (f'PID {pid} ({comm}) is NEVER-pause; '
                               'aborting freeze')
            return False, (f'PID {pid} not in the freezable set; '
                            'aborting freeze')
        if is_never_pause(proc):
            return False, f"PID {pid} ({proc.get('comm')}) is NEVER-pause"
    if not expected and member_pids:
        return False, 'cgroup has members but nothing is freezable'
    return True, ''


def _visible_category_members(procs, username, category, prefs, index,
                              exclude_comms=None):
    """Visible + categorized-into-category + not NEVER-pause members.

    ``exclude_comms`` drops comms that carry an app override (exempt or
    allowance) so a category freeze/window never bills or freezes them.
    """
    visible, _hidden = apply_filter(procs, username, prefs)
    exclude = exclude_comms or frozenset()
    out = []
    for proc in visible:
        if categorize(proc, index) != category:
            continue
        if is_never_pause(proc):
            continue
        if proc.get('comm') in exclude:
            continue
        out.append(proc)
    return out


def freezable_members(procs, username, category, limits, prefs=None,
                      index=None, exclude_comms=None):
    """Match-time gate: visible after filtering AND in the budgeted category
    AND not NEVER-pause AND category actually has a limit configured."""
    if float(limits.get(category, 0) or 0) <= 0:
        return []
    return _visible_category_members(procs, username, category, prefs, index,
                                     exclude_comms)


def freezable_app_members(procs, username, comm, prefs=None):
    """Match-time gate for a per-app allowance: visible, same comm, and
    not NEVER-pause. (Exempt apps call this? No — exempt comms never
    enter a cgroup, so callers skip them entirely.)"""
    visible, _hidden = apply_filter(procs, username, prefs)
    out = []
    for proc in visible:
        if (proc.get('comm') or '') != comm:
            continue
        if is_never_pause(proc):
            continue
        out.append(proc)
    return out


# ---------------------------------------------------------------------------
# Focus engine plumbing (feed cursor, provider, per-tick aggregation)
# ---------------------------------------------------------------------------

def focus_engine_of(entry):
    """Normalized engine for a budget entry, failing closed to ``'cpu'``.

    An absent/unknown value means today's CPU behavior (never a silent
    engine switch). ``'no_data'`` is a focus-family engine: it keeps
    probing the feed and recovers to ``'focus'`` once fresh. ``'agent'`` is
    accepted as an alias of ``'focus'`` (SESSION_AGENT_PLAN.md §8.4).
    """
    raw = (entry or {}).get('focus_engine', FOCUS_ENGINE_CPU)
    if raw in _FOCUS_ENGINE_ALIASES:
        return FOCUS_ENGINE_FOCUS
    if raw in FOCUS_ENGINES:
        return raw
    return FOCUS_ENGINE_CPU


def boottime_now():
    """``CLOCK_BOOTTIME`` seconds (advances across suspend), or monotonic.

    Focus-feed intervals are expressed in boottime seconds; this is the
    panel-side ``now`` on that axis. The monotonic fallback only matters on
    platforms without ``CLOCK_BOOTTIME`` and keeps the module importable.
    """
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError, ValueError):
        return time.monotonic()


def focus_status(entry):
    """UI-facing focus-engine health (pure read; never bills/mutates).

    Additive helper so callers can surface "usage not being measured"
    without changing any existing endpoint or budget_status shape.
    """
    entry = entry or {}
    engine = focus_engine_of(entry)
    configured = engine in FOCUS_FAMILY
    return {
        'engine': engine,
        'configured': configured,
        'degraded': configured and (
            engine == FOCUS_ENGINE_NO_DATA
            or entry.get('feed_status') == 'no_data'),
        'feed_seq': entry.get('feed_seq'),
        'feed_last_seen': entry.get('feed_last_seen'),
        'feed_agent': entry.get('feed_agent'),
    }


def _feed_cursor_update(entry, parsed):
    """Advance an entry's feed watermark from a parse result, billing nothing.

    The cursor is a *drain position*, not a budget quantity: it moves even
    on locked ticks and on ticks where billing is halted, so rows at or
    below it are never accounted twice (mirrors the ``last_cpu`` baseline
    advance). Accepts any dict; silently ignores a non-dict result.
    """
    if not isinstance(parsed, dict):
        return
    last_seq = parsed.get('last_seq')
    if isinstance(last_seq, int) and not isinstance(last_seq, bool):
        try:
            cur = int(entry.get('feed_seq', 0))
        except (TypeError, ValueError):
            cur = 0
        if last_seq > cur:
            entry['feed_seq'] = last_seq
    heartbeat_bt = parsed.get('last_heartbeat_bt')
    newest = None
    agent = None
    for row in parsed.get('rows') or []:
        if not isinstance(row, dict):
            continue
        if agent is None and row.get('agent'):
            agent = row['agent']
        bt = row.get('bt')
        if isinstance(bt, (int, float)) and not isinstance(bt, bool):
            if newest is None or bt > newest.get('bt', -1):
                newest = row
    if agent is not None:
        entry['feed_agent'] = agent
    if (newest is not None and newest.get('ts') is not None
            and heartbeat_bt is not None and newest.get('bt') == heartbeat_bt):
        entry['feed_last_seen'] = newest['ts']
    boot = parsed.get('boot_id')
    if boot:
        entry['feed_boot_id'] = boot


def _locked_feed_bookkeeping(entry, parsed):
    """Locked tick: consume the locked window's feed rows without billing.

    The focus-engine analogue of ``_locked_tick_bookkeeping``: the cursor
    advances so the first tick after unlock bills only NEW focus sessions,
    never the locked window.
    """
    _feed_cursor_update(entry, parsed)


def _default_focus_provider(username, entry, now_bt):
    """Production feed provider: pull one child's feed via ``focus_feed``.

    A drain/transport failure yields the feed module's ``missing`` result
    (stale/degraded), so the caller halts billing instead of guessing zero.
    """
    import focus_feed
    feed_seq = entry.get('feed_seq', 0)
    try:
        uid = pwd.getpwnam(username).pw_uid
    except (KeyError, TypeError):
        return focus_feed.parse_feed(None, feed_seq=feed_seq)
    try:
        text, _err = focus_feed.drain_feed(username, uid)
    except Exception:
        text = None
    return focus_feed.parse_feed(text, feed_seq=feed_seq,
                                 expected_boot_id=entry.get('feed_boot_id'),
                                 now_bt=now_bt)


def _focus_billing(username, entry, procs, limits, prefs, index, window_bt,
                   provider, now_bt):
    """Fetch + validate the feed and return per-category focused seconds.

    Returns ``(seconds_by_category, info)``. ``info`` carries the surfacing
    fields (engine, billable, degraded, feed_seq, states, errors, stats).
    Never raises: a transport/provider failure or malformed/oversized/
    non-monotonic feed all fail closed (bill nothing, ``degraded`` True).
    """
    import focus_feed
    info = {'engine': FOCUS_ENGINE_FOCUS, 'billable': False,
            'degraded': True, 'seconds': {}, 'stats': {}, 'states': [],
            'errors': []}
    try:
        parsed = provider(username, entry, now_bt)
    except Exception as exc:                       # transport/provider boom
        info['errors'] = [str(exc)]
        return {}, info
    if not isinstance(parsed, dict):
        info['errors'] = ['provider returned no parse result']
        return {}, info
    _feed_cursor_update(entry, parsed)
    info['feed_seq'] = entry.get('feed_seq')
    info['states'] = list(parsed.get('states') or [])
    info['errors'] = list(parsed.get('errors') or [])
    if (parsed.get('too_large') or parsed.get('missing')
            or parsed.get('stale') or parsed.get('degraded')):
        return {}, info
    try:
        agg = focus_feed.aggregate_focus(
            parsed, window_bt, procs=procs, limits=limits, username=username,
            prefs=prefs, index=index, now_bt=now_bt)
    except focus_feed.FeedError as exc:
        info['errors'] = info['errors'] + [str(exc)]
        return {}, info
    except Exception as exc:
        info['errors'] = info['errors'] + [str(exc)]
        return {}, info
    info['billable'] = bool(agg.get('billable'))
    info['degraded'] = bool(agg.get('degraded'))
    info['seconds'] = dict(agg.get('seconds') or {})
    info['stats'] = dict(agg.get('stats') or {})
    if not info['billable']:
        return {}, info
    return info['seconds'], info


# ---------------------------------------------------------------------------
# Tick: accounting + reset + enforcement (called every 60s)
# ---------------------------------------------------------------------------

def _clear_window_target_freeze(entry, window, username, act, summary):
    """Clear a window target's freeze flags on an open/close transition.

    On open: fresh allowance. On close: auto-resume. Pops the auto-pause
    and resume-inhibit flags and (when acting) thaws the target's cgroup
    so the daily governor or the next window re-evaluates cleanly.
    """
    target = window['target']
    if window['target_type'] == 'category':
        entry.setdefault('paused', {}).pop(target, None)
        entry.setdefault('resumed', {}).pop(target, None)
        if act:
            ok, msg = set_frozen(username, target, False)
            if not ok:
                summary['note'] = f'window thaw failed for {target}: {msg}'
    else:
        entry.setdefault('app_paused', {}).pop(target, None)
        entry.setdefault('app_resumed', {}).pop(target, None)
        if act:
            ok, msg = set_frozen_app(username, target, False)
            if not ok:
                summary['note'] = (f'window thaw failed for app:'
                                   f'{target}: {msg}')


def _enforce_target(username, t, wall, deltas, summary, act):
    """Shared billing + freeze state machine for one target.

    ``t`` bundles: limit_sec, used, used_key, paused, paused_key, resumed,
    resumed_key, manual, manual_key, members, ensure, migrate,
    get_members, set_frozen, label, tag, warned_low, low_key, today_str,
    notify_low() -> bool, notify_paused(). The branches mirror the
    original per-category tick exactly, so with no app override or window
    configured the behavior is unchanged.
    """
    used = t['used']
    key = t['used_key']
    limit_sec = t['limit_sec']
    used[key] = float(used.get(key, 0.0) or 0.0)
    members = t['members']
    # An active target burns the whole tick's wall-clock duration; an idle
    # one burns nothing — usage tracks the Elapsed time parents see.
    # Under the focus engine the caller supplies ``bill_seconds`` (focused
    # seconds aggregated from the session feed) and the CPU gate is skipped
    # entirely: 0.0 halts billing without the wall-clock charge.
    if 'bill_seconds' in t:
        used[key] += max(0.0, float(t['bill_seconds']))
    elif any(deltas.get(p['pid'], 0.0) >= ACTIVE_THRESHOLD_SECONDS
             for p in members):
        used[key] += wall
    exhausted = used[key] >= limit_sec
    remaining = limit_sec - used[key]
    if (not exhausted and 0 < remaining <= LOW_WARN_SECONDS
            and t['warned_low'].get(t['low_key']) != t['today_str']):
        if t['notify_low']():
            t['warned_low'][t['low_key']] = t['today_str']

    paused = t['paused']
    resumed = t['resumed']
    manual = t['manual']
    pkey = t['paused_key']
    if (exhausted and not paused.get(pkey)
            and not resumed.get(t['resumed_key'])
            and not manual.get(t['manual_key'])):
        if not act:
            paused[pkey] = True
            summary['froze'].append(t['tag'] + ' (dry-run)')
            t['notify_paused']()
            return
        ok, msg = t['ensure']()
        if not ok:
            summary['note'] = f"ensure cgroup: {msg}"
            return
        for proc in members:
            t['migrate'](proc['pid'])
        expected = {p['pid']: p for p in members}
        current = t['get_members']()
        safe, reason = verify_freeze_safety(username, current, expected)
        if not safe:
            summary['note'] = f"freeze aborted for {t['label']}: {reason}"
            return
        if not members and not current:
            paused[pkey] = True  # exhausted, nothing running yet
            t['notify_paused']()
            return
        if not members:
            summary['note'] = (f"freeze aborted for {t['label']}: "
                               'cgroup has members but nothing is freezable')
            return
        ok, msg = t['set_frozen'](True)
        if ok:
            paused[pkey] = True
            summary['froze'].append(t['tag'])
            t['notify_paused']()
        else:
            summary['note'] = f"freeze failed for {t['label']}: {msg}"
    elif exhausted and paused.get(pkey) and members and act:
        # Exhausted earlier while nothing was running: a matching app
        # launched since. Migrate it in and (re-)freeze.
        ok, _msg = t['ensure']()
        if ok:
            current = set(t['get_members']())
            for proc in members:
                if proc['pid'] not in current:
                    t['migrate'](proc['pid'])
            expected = {p['pid']: p for p in members}
            safe, reason = verify_freeze_safety(
                username, t['get_members'](), expected)
            if safe:
                ok, msg = t['set_frozen'](True)
                if ok:
                    summary['froze'].append(t['tag'] + ' (late start)')
                else:
                    summary['note'] = (
                        f"re-freeze failed for {t['label']}: {msg}")
            else:
                summary['note'] = (
                    f"re-freeze aborted for {t['label']}: {reason}")
    elif manual.get(t['manual_key']) and act:
        # Manually-frozen steady state: re-assert for late starters only.
        ok, _msg = t['ensure']()
        if ok:
            current = set(t['get_members']())
            for proc in members:
                if proc['pid'] not in current:
                    t['migrate'](proc['pid'])
            expected = {p['pid']: p for p in members}
            safe, reason = verify_freeze_safety(
                username, t['get_members'](), expected)
            if safe:
                ok, msg = t['set_frozen'](True)
                if ok:
                    summary['froze'].append(t['tag'] + ' (manual)')
                else:
                    summary['note'] = (
                        f"manual re-freeze failed for {t['label']}: {msg}")
            else:
                summary['note'] = (
                    f"manual re-freeze aborted for {t['label']}: {reason}")
    elif members and act and not paused.get(pkey):
        # Pre-place matching processes so a later freeze is instant.
        ok, _msg = t['ensure']()
        if ok:
            current = set(t['get_members']())
            for proc in members:
                if proc['pid'] not in current:
                    t['migrate'](proc['pid'])


def account_and_enforce(username, entry, procs, bedtime_entry, now,
                        prefs=None, index=None, act=True, notify=None,
                        overrides=None, focus_provider=None, now_bt=None):
    """One budget tick for a single user. Mutates `entry` in place.

    ``overrides`` is this user's config doc
    (``{"app_overrides": ..., "windows": ...}``) or None/empty; absent
    config reproduces the pre-feature behavior exactly.

    ``focus_provider`` (``focus_provider(username, entry, now_bt)`` ->
    focus_feed parse result) and ``now_bt`` (``CLOCK_BOOTTIME`` seconds)
    drive the focus engine (SESSION_AGENT_PLAN.md §9). Both are ignored
    unless the entry's ``focus_engine`` is a focus-family value, so the
    default CPU behavior is byte-identical.

    Precedence (most specific wins): app exemption > app allowance (its
    own active window, else its daily override) > category active window
    > category daily limit. An active target window replaces the daily
    allowance entirely (limit AND reset timing).

    Returns a summary dict {reset, froze: [...], notified: [...], note}.
    With act=False no privileged cgroup operations are performed (used by
    unit tests). notify, when given, is a callable
    ``notify(username, title, body)`` used for best-effort warnings.
    """
    today_str = now.date().isoformat()
    has_bedtime = bool(bedtime_entry and bedtime_entry.get('start'))
    now_in = bool(bedtime_entry and _in_bedtime(bedtime_entry, now))
    was_in = bool(entry.get('was_in_bedtime', False))
    summary = {'user': username, 'reset': False, 'froze': [],
               'unfroze': [], 'notified': [], 'note': ''}

    def _warn(title, body, tag):
        """Best-effort notification; records tag in summary, never raises."""
        if notify is None:
            return False
        try:
            notify(username, title, body)
            summary['notified'].append(tag)
        except Exception as e:
            summary['note'] = 'notify failed: %s' % e
            return False
        return True

    if is_admin_protected(username):
        # Admin self-protection: never auto-freeze a wheel member's apps.
        note = (f'skipped {username}: {ADMIN_GROUP} member '
                '(admin self-protection)')
        print(note, flush=True)
        summary['note'] = note
        return summary

    # ---- weekly-window lifecycle: open/close transitions ----
    window_used = entry.setdefault('window_used', {})
    window_active = entry.setdefault('window_active', {})
    configured_windows = [w for _wid, w in iter_windows(overrides)]
    active_windows = []
    for win in configured_windows:
        wid = win['id']
        was_active = bool(window_active.get(wid, False))
        is_active = bool(win['enabled'] and _in_window(win, now))
        if is_active:
            active_windows.append(win)
        if is_active and not was_active:
            window_used[wid] = 0.0            # open: fresh allowance
            _clear_window_target_freeze(entry, win, username, act, summary)
        elif was_active and not is_active:
            window_used[wid] = 0.0            # close: auto-resume
            _clear_window_target_freeze(entry, win, username, act, summary)
        window_active[wid] = is_active

    if entry.get('day') is None:
        entry['day'] = today_str
    if budget_reset_needed(entry.get('day'), has_bedtime, was_in, now_in,
                           today_str):
        entry['used_seconds'] = {}
        entry['paused'] = {}
        entry['resumed'] = {}
        entry['manual_frozen'] = {}
        entry['manual_frozen_at'] = {}
        entry['warned_low'] = {}
        entry['warned_paused'] = {}
        entry['app_used'] = {}
        entry['app_paused'] = {}
        entry['app_resumed'] = {}
        entry['app_manual_frozen'] = {}
        entry['app_manual_frozen_at'] = {}
        entry['app_warned_low'] = {}
        entry['app_warned_paused'] = {}
        entry['day'] = today_str
        # window_used is NOT cleared for active windows (spans midnight).
        for wid in list(window_used):
            if not window_active.get(wid, False):
                window_used.pop(wid, None)
        summary['reset'] = True
        if act:
            keep_cats = {w['target'] for w in active_windows
                         if w['target_type'] == 'category'}
            keep_apps = {w['target'] for w in active_windows
                         if w['target_type'] == 'app'}
            if keep_cats or keep_apps:
                ok, _res = unfreeze_all_user(
                    username, keep_categories=keep_cats,
                    keep_apps=keep_apps)
            else:
                ok, _res = unfreeze_all_user(username)
            summary['unfroze'].append('all' if ok else 'failed')
    entry['was_in_bedtime'] = now_in

    limits = entry.get('limits', {}) or {}
    app_overrides = override_map(overrides)
    if not limits and not configured_windows and not app_overrides:
        entry['last_cpu'] = {}
        return summary

    deltas, new_last = compute_deltas(entry.get('last_cpu', {}), procs)
    entry['last_cpu'] = new_last
    # Wall-clock seconds since the previous tick. First tick (or unreadable
    # timestamp) bills nothing — baselines only. Capped so sleep/downtime
    # gaps cannot bill phantom hours.
    prev_iso = entry.get('last_tick')
    if prev_iso:
        try:
            wall = (now - datetime.fromisoformat(prev_iso)).total_seconds()
        except (ValueError, TypeError):
            wall = 0.0
    else:
        wall = 0.0
    wall = max(0.0, min(wall, MAX_TICK_WALL_SECONDS))
    entry['last_tick'] = now.isoformat()
    used = entry.setdefault('used_seconds', {})
    paused = entry.setdefault('paused', {})
    resumed = entry.setdefault('resumed', {})
    manual_frozen = entry.setdefault('manual_frozen', {})
    warned_low = entry.setdefault('warned_low', {})
    warned_paused = entry.setdefault('warned_paused', {})
    app_used = entry.setdefault('app_used', {})
    app_paused = entry.setdefault('app_paused', {})
    app_resumed = entry.setdefault('app_resumed', {})
    app_manual_frozen = entry.setdefault('app_manual_frozen', {})
    app_warned_low = entry.setdefault('app_warned_low', {})
    app_warned_paused = entry.setdefault('app_warned_paused', {})

    def _low_note(warned_map, key, label):
        def fn():
            return _warn('%s: 5 minutes left' % label,
                         'Less than 5 minutes of %s time left today.' % label,
                         key + ' (low)')
        return fn

    def _paused_note(warned_map, key, label):
        def fn():
            if warned_map.get(key) == today_str:
                return
            if _warn('%s paused \u2014 out of time' % label,
                     'Your %s time is up for today.' % label,
                     key + ' (paused)'):
                warned_map[key] = today_str
        return fn

    governed_comms = set(app_overrides)
    win_by_category = {w['target']: w for w in active_windows
                       if w['target_type'] == 'category'}

    # ---- focus engine: bill focused seconds instead of CPU deltas ----
    # The CPU path is untouched when focus_seconds stays None (default
    # ``focus_engine='cpu'``), so a rollback is byte-identical.
    focus_seconds = None
    if focus_engine_of(entry) in FOCUS_FAMILY:
        if now_bt is None:
            now_bt = boottime_now()
        provider = focus_provider or _default_focus_provider
        # Same tick span the CPU engine would bill, on the boottime axis.
        window_bt = (now_bt - wall, now_bt)
        # A category window may carry an allowance with no daily limit;
        # fold it in so the freezable gate ("only bill what you can
        # freeze") still sees the category as billable.
        focus_limits = dict(limits)
        for cat, win in win_by_category.items():
            try:
                focus_limits[cat] = float(win['allowance_minutes'])
            except (TypeError, ValueError):
                pass
        focus_seconds, focus_info = _focus_billing(
            username, entry, procs, focus_limits, prefs, index, window_bt,
            provider, now_bt)
        if focus_info.get('billable'):
            entry['focus_engine'] = FOCUS_ENGINE_FOCUS
            entry['feed_status'] = 'ok'
        else:
            # Missing/stale/malformed feed: HALT billing (used_seconds is
            # left untouched — never silently billed as zero) and surface
            # ``no_data``. A fresh feed recovers on the next tick.
            entry['focus_engine'] = FOCUS_ENGINE_NO_DATA
            entry['feed_status'] = 'no_data'
            focus_seconds = {}
            summary['degraded'] = True
        summary['focus'] = focus_info

    # ---- category targets (window replaces the daily limit) ----
    ordered = list(limits.keys())
    for cat in win_by_category:
        if cat not in ordered:
            ordered.append(cat)
    for category in ordered:
        if category not in CATEGORIES:
            continue
        members = _visible_category_members(
            procs, username, category, prefs, index,
            exclude_comms=governed_comms)
        label = CATEGORY_LABELS.get(category, category)
        win = win_by_category.get(category)
        if win is not None:
            limit_sec = float(win['allowance_minutes']) * 60.0
            used_map, used_key = window_used, win['id']
        else:
            try:
                limit_sec = float(limits.get(category, 0) or 0) * 60.0
            except (TypeError, ValueError):
                continue
            if limit_sec <= 0:
                continue
            used_map, used_key = used, category
        target = {
            'limit_sec': limit_sec,
            'used': used_map, 'used_key': used_key,
            'paused': paused, 'paused_key': category,
            'resumed': resumed, 'resumed_key': category,
            'manual': manual_frozen, 'manual_key': category,
            'members': members,
            'ensure': lambda c=category: ensure_cgroup(username, c),
            'migrate': lambda pid, c=category: migrate_pid(username, c, pid),
            'get_members': lambda c=category: cgroup_members(username, c),
            'set_frozen': lambda fr, c=category: set_frozen(username, c, fr),
            'label': label, 'tag': category,
            'warned_low': warned_low, 'low_key': category,
            'today_str': today_str,
            'notify_low': _low_note(warned_low, category, label),
            'notify_paused': _paused_note(warned_paused, category, label),
        }
        if focus_seconds is not None:
            # Focus engine: bill this category's focused seconds (0.0 when
            # the feed is degraded/missing -> billing halted for this tick).
            target['bill_seconds'] = float(focus_seconds.get(category, 0.0)
                                           or 0.0)
        _enforce_target(username, target, wall, deltas, summary, act)

    # ---- app targets ----
    for comm, ov in sorted(app_overrides.items()):
        if ov['mode'] == 'exempt':
            continue  # unlimited: never billed, never placed in a cgroup
        win = active_window(overrides, 'app', comm, now)
        members = freezable_app_members(procs, username, comm, prefs)
        if win is not None:
            limit_sec = float(win['allowance_minutes']) * 60.0
            used_map, used_key = window_used, win['id']
        else:
            limit_sec = float(ov['minutes']) * 60.0
            used_map, used_key = app_used, comm
        _enforce_target(username, {
            'limit_sec': limit_sec,
            'used': used_map, 'used_key': used_key,
            'paused': app_paused, 'paused_key': comm,
            'resumed': app_resumed, 'resumed_key': comm,
            'manual': app_manual_frozen, 'manual_key': comm,
            'members': members,
            'ensure': lambda c=comm: ensure_cgroup_app(username, c),
            'migrate': lambda pid, c=comm: migrate_pid_app(username, c, pid),
            'get_members': lambda c=comm: cgroup_members_app(username, c),
            'set_frozen': lambda fr, c=comm: set_frozen_app(username, c, fr),
            'label': comm, 'tag': 'app:' + comm,
            'warned_low': app_warned_low, 'low_key': comm,
            'today_str': today_str,
            'notify_low': _low_note(app_warned_low, comm, comm),
            'notify_paused': _paused_note(app_warned_paused, comm, comm),
        }, wall, deltas, summary, act)

    return summary


def _locked_tick_bookkeeping(entry, procs, now, bedtime_entry):
    """Advance a locked account's accounting baselines, billing nothing.

    Mirrors the baseline half of account_and_enforce (was_in_bedtime
    tracking, the compute_deltas last_cpu advance, and the last_tick
    wall-clock base) so the first tick after unlock bills only NEW CPU
    activity, never the locked window. No billing, no pause/notify/freeze
    decisions — already-paused/frozen state is unchanged and simply
    persists.
    """
    entry['was_in_bedtime'] = bool(
        bedtime_entry and _in_bedtime(bedtime_entry, now))
    if not entry.get('limits'):
        # Mirrors account_and_enforce: no configured limits -> drop the
        # baseline (and leave last_tick alone, exactly like that path).
        entry['last_cpu'] = {}
        return
    entry['last_cpu'] = compute_deltas(
        entry.get('last_cpu', {}), procs)[1]
    entry['last_tick'] = now.isoformat()


def tick_budgets(bedtime_map, now, procs_provider=None, prefs=None,
                 index=None, act=True, notify=None, run_watchdog=True,
                 cgroup_root=None, account_locked=None, focus_provider=None,
                 now_bt=None):
    """Run one budget tick for every user in the budget store.

    bedtime_map: {username: bedtime_entry} (from the app's bedtime loader).
    procs_provider: callable(username) -> procs (default: live ps snapshot).
    notify: optional callable ``notify(username, title, body)`` for
    best-effort "minutes left" / "paused" warnings.
    run_watchdog: when True (default), run the freezer watchdog AFTER
      enforcement against the same in-memory state, so categories this
      tick legitimately froze already carry paused=True and are never
      mistaken for stuck. The watchdog summary is appended as a
      {'user': '_watchdog', ...} entry. Both the app thread and the
      systemd timer path go through here, so both self-heal.
    cgroup_root: alternate cgroup tree for the watchdog (tests only;
      default: real /sys/fs/cgroup).
    account_locked: optional callable ``account_locked(username) -> bool``
      reporting whether the account is locked (usermod -L: manual Lock,
      Lock Screen, bedtime auto-lock — everything get_lock_status sees).
      While locked the child cannot reach the app, so that user's tick
      bills nothing and no pause/notify/minutes-left decisions are made;
      the accounting baselines still advance exactly as if billed, so the
      first tick after unlock bills only NEW activity, never the locked
      window. Default None (or absent) = every user unlocked, preserving
      the pure-module behavior.
    focus_provider: optional callable
      ``focus_provider(username, entry, now_bt) -> focus_feed parse
      result``, consulted only for users whose ``focus_engine`` is a
      focus-family value. Default None -> the real drain-based provider
      (``_default_focus_provider``). Tests inject a fake.
    now_bt: ``CLOCK_BOOTTIME`` seconds sampled once for the whole tick
      (tests inject a fixed value; default sampled here). Only the focus
      engine uses it.
    Returns a list of per-user summary dicts (+ watchdog entry).
    """
    data = load_budgets()
    overrides_map = load_overrides()
    if prefs is None:
        prefs = load_prefs()
    if now_bt is None:
        now_bt = boottime_now()
    changed = True  # usage baselines always advance; persist every tick
    summaries = []
    usernames = [u for u in data if valid_username(u)]
    # A user may have only an overrides config (app override / window)
    # with no category limits yet: give them a blank budget entry so the
    # tick governs them too.
    for u in overrides_map:
        if u not in data and valid_username(u):
            data[u] = blank_entry()
            usernames.append(u)
    for username in usernames:
        entry = data[username]
        user_overrides = overrides_map.get(username)
        if is_admin_protected(username):
            # Admin self-protection: not even a live `ps` snapshot is taken
            # for wheel members — skip entirely with a logged note.
            note = (f'skipped budget tick for {username}: {ADMIN_GROUP} '
                    'member (admin self-protection)')
            print(note, flush=True)
            summaries.append({'user': username, 'reset': False, 'froze': [],
                              'unfroze': [], 'note': note})
            continue
        for key in ('limits', 'used_seconds', 'last_cpu', 'paused', 'resumed',
                    'manual_frozen', 'manual_frozen_at',
                    'warned_low', 'warned_paused',
                    'app_used', 'app_paused', 'app_resumed',
                    'app_manual_frozen', 'app_manual_frozen_at',
                    'app_warned_low', 'app_warned_paused',
                    'window_used', 'window_active'):
            entry.setdefault(key, {})
        if procs_provider is not None:
            procs = procs_provider(username)
        else:
            procs = list_user_processes(username)
        if account_locked is not None and account_locked(username):
            # Locked account: the child cannot reach the app, so this tick
            # bills nothing and makes no pause/notify decisions —
            # already-paused/frozen state simply persists. Baselines
            # advance exactly as if billed, so the first tick after unlock
            # bills only NEW CPU, never the locked window.
            _locked_tick_bookkeeping(
                entry, procs, now, (bedtime_map or {}).get(username))
            # Focus engine: consume the locked window's feed rows (cursor
            # advance) so the first tick after unlock bills only NEW focus.
            if focus_engine_of(entry) in FOCUS_FAMILY:
                provider = focus_provider or _default_focus_provider
                try:
                    parsed = provider(username, entry, now_bt)
                except Exception:
                    parsed = None
                _locked_feed_bookkeeping(entry, parsed)
            summaries.append({'user': username, 'reset': False, 'froze': [],
                              'unfroze': [], 'notified': [], 'note':
                              f'skipped {username}: account locked'})
            continue
        summaries.append(account_and_enforce(
            username, entry, procs, (bedtime_map or {}).get(username),
            now, prefs, index, act, notify, overrides=user_overrides,
            focus_provider=focus_provider, now_bt=now_bt))
    if changed:
        try:
            save_budgets(data)
        except OSError:
            pass
    if run_watchdog:
        # Enforcement first, watchdog second, same in-memory `data`: a
        # category frozen above already has paused=True, so it reads as
        # legitimate and is never thawed back (no flapping).
        try:
            w = watchdog_tick(data, cgroup_root=cgroup_root or CGROUP_ROOT)
        except Exception as e:
            print(f'WATCHDOG: pass failed: {e}', flush=True)
            w = {'thawed': [], 'legit': [], 'clean': [], 'unknown': [],
                 'skipped': [], 'note': f'pass failed: {e}'}
        summaries.append(dict({'user': '_watchdog'}, **w))
    return summaries


def freeze_category(username, category, actor='system'):
    """Manual Freeze: freeze a category cgroup on the parent's click.

    Records ``manual_frozen[category]`` (plus ``manual_frozen_at``) so the
    tick re-asserts the freeze for late starters and the watchdog treats
    it as legitimate. ``paused`` is left untouched — a manual freeze is
    not an exhaustion, and unfreezing a dually-marked category clears
    both. Fail-closed throughout: any cgroup/safety failure returns
    ``(False, reason)`` WITHOUT persisting ``manual_frozen``.

    Uses only the existing privileged helpers (ensure_cgroup,
    migrate_pid, set_frozen, cgroup_members) — no new privilege grants.
    """
    if not valid_username(username):
        return False, 'Invalid username'
    if not valid_category(category):
        return False, f'Unknown category: {category}'
    if is_admin_protected(username):
        return False, (f'Refused: {username} is an admin and cannot '
                       'be frozen')
    data = load_budgets()
    entry = data.get(username, {})
    if float((entry.get('limits', {}) or {}).get(category, 0) or 0) <= 0:
        return False, 'Set a time limit for this category first'
    limits = entry.get('limits', {})
    try:
        prefs = load_prefs()
    except Exception:
        prefs = None
    try:
        index = build_desktop_index(username)
    except Exception:
        index = None
    try:
        procs = list_user_processes(username)
    except Exception as e:
        return False, f'could not snapshot processes: {e}'
    members = freezable_members(procs, username, category, limits, prefs,
                                index)
    ok, msg = ensure_cgroup(username, category)
    if not ok:
        return False, msg
    for proc in members:
        # Individual migrate failures are ignored: the readback safety
        # gate below is authoritative (matches the tick's ignore-and-verify
        # order).
        migrate_pid(username, category, proc['pid'])
    current = cgroup_members(username, category)
    safe, reason = verify_freeze_safety(username, current,
                                        {p['pid']: p for p in members})
    if not safe:
        return False, reason
    if not members and not current:
        # Nothing running yet: record the freeze without writing
        # cgroup.freeze. The tick's manual-frozen branch freezes late
        # starters under the same gates.
        pass
    else:
        ok, msg = set_frozen(username, category, True, actor=actor)
        if not ok:
            return False, msg
    try:
        data = load_budgets()
        entry = data.setdefault(username, blank_entry())
        entry.setdefault('manual_frozen', {})[category] = True
        entry.setdefault('manual_frozen_at', {})[category] = \
            datetime.now().isoformat()
        save_budgets(data)
    except OSError as e:
        return False, str(e)
    label = CATEGORY_LABELS.get(category, category)
    return True, f'Frozen {label} (manual freeze)'


def unfreeze_category(username, category, actor='system'):
    """Manual Unfreeze: thaw and inhibit re-freeze until the next reset.

    Subsumes the old Resume: clears BOTH ``paused`` and ``manual_frozen``
    (plus ``manual_frozen_at``) and sets ``resumed`` so the tick does not
    instantly re-freeze an exhausted category.
    """
    if not valid_username(username):
        return False, 'Invalid username'
    if not valid_category(category):
        return False, f'Unknown category: {category}'
    ok, msg = set_frozen(username, category, False, actor=actor)
    if not ok:
        return False, msg
    try:
        data = load_budgets()
        entry = data.setdefault(username, blank_entry())
        entry.get('paused', {}).pop(category, None)
        entry.get('manual_frozen', {}).pop(category, None)
        entry.get('manual_frozen_at', {}).pop(category, None)
        entry.setdefault('resumed', {})[category] = True
        save_budgets(data)
    except OSError as e:
        return False, str(e)
    audit.safe_log(actor, 'budget.resume', f'{username}:{category}',
                   {'ok': True})
    return True, 'Resumed; will not re-pause until the next reset'


def resume_category(username, category, actor='system'):
    """Manual Resume: unfreeze and inhibit re-freeze until the next reset.

    Thin alias over :func:`unfreeze_category` (kept so the existing
    ``/api/budgets/resume`` endpoint and CLI-adjacent callers keep
    working with the exact same return contract).
    """
    return unfreeze_category(username, category, actor=actor)


# ---------------------------------------------------------------------------
# Bedtime-window check, local copy so this module stays stdlib-only and
# importable without the Flask app (app.py passes its own bedtime map in).
# ---------------------------------------------------------------------------

def _parse_hhmm(value):
    try:
        hours, minutes = value.strip().split(':')
        h, m = int(hours), int(minutes)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return None


def _in_window(entry, now):
    """Recurring day-of-week + HH:MM range check with overnight wrap.

    Used for both bedtime and weekly budget windows (identical temporal
    model). Returns False for a missing/malformed entry or ``start ==
    end``.
    """
    if not entry:
        return False
    start = _parse_hhmm(entry.get('start', ''))
    end = _parse_hhmm(entry.get('end', ''))
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


def _in_bedtime(entry, now):
    return _in_window(entry, now)
