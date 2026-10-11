#!/usr/bin/env python3
"""Phase 1: process filtering, app categorization, per-category time budgets.

Prototype mindset: stdlib only (no Flask import here so unit tests run with
system python3), JSON files like bedtime.json, no DB.

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
import json
import os
import pwd
import re
import shlex
import subprocess
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESS_PREFS_FILE = os.path.join(BASE_DIR, 'process_prefs.json')
BUDGETS_FILE = os.path.join(BASE_DIR, 'budgets.json')
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

_USERNAME_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')


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
# process_prefs.json — per-user Hide / Show-hidden state
# Shape: {username: {"hidden_comm": [...], "shown_comm": [...]}}
# Effective hidden = (default_hidden | hidden_comm) - shown_comm
# ---------------------------------------------------------------------------

def load_prefs():
    try:
        with open(PROCESS_PREFS_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_prefs(data):
    with open(PROCESS_PREFS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


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
# budgets.json
# Shape: {username: {"limits": {cat: minutes}, "day": "YYYY-MM-DD",
#   "used_seconds": {cat: float}, "last_cpu": {pid: {comm, cpu}},
#   "paused": {cat: bool}, "resumed": {cat: bool},
#   "manual_frozen": {cat: bool}, "manual_frozen_at": {cat: iso},
#   "warned_low": {cat: "YYYY-MM-DD"}, "warned_paused": {cat: "YYYY-MM-DD"},
#   "was_in_bedtime": bool}}
# ---------------------------------------------------------------------------

def load_budgets():
    try:
        with open(BUDGETS_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_budgets(data):
    with open(BUDGETS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def blank_entry():
    return {'limits': {}, 'day': None, 'used_seconds': {}, 'last_cpu': {},
            'paused': {}, 'resumed': {}, 'manual_frozen': {},
            'manual_frozen_at': {}, 'warned_low': {},
            'warned_paused': {}, 'was_in_bedtime': False,
            'last_tick': None}


def set_limit(username, category, minutes):
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
        ok, msg = set_frozen(username, category, False)
        return True, 'Limit cleared' + ('' if ok else f' (unfreeze: {msg})')
    entry['limits'][category] = minutes
    try:
        save_budgets(data)
    except OSError as e:
        return False, str(e)
    return True, 'OK'


def budget_status(username, data=None):
    """UI-friendly status: {cat: {minutes, used_minutes, remaining_minutes,
    paused}} for every category. All displayed times are whole minutes —
    seconds are never shown."""
    if data is None:
        data = load_budgets()
    entry = data.get(username, {})
    limits = entry.get('limits', {})
    used = entry.get('used_seconds', {})
    paused = entry.get('paused', {})
    manual_frozen = entry.get('manual_frozen', {})
    status = {}
    for cat in CATEGORIES:
        limit_min = float(limits.get(cat, 0) or 0)
        used_sec = float(used.get(cat, 0) or 0)
        is_paused = bool(paused.get(cat, False))
        is_manual = bool(manual_frozen.get(cat, False))
        status[cat] = {
            'minutes': limit_min,
            'used_minutes': int(round(used_sec / 60.0)),
            'remaining_minutes': int(round(max(0.0, limit_min * 60 - used_sec) / 60.0))
            if limit_min > 0 else None,
            'paused': is_paused,
            'exhausted': bool(limit_min > 0 and used_sec >= limit_min * 60),
            'manual_frozen': is_manual,
            'frozen': bool(is_paused or is_manual),
        }
    return status


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
# Cgroup freezer ops (privileged via sudo tee; validated username/category)
# ---------------------------------------------------------------------------

def cgroup_path(username, category):
    if not valid_username(username) or not valid_category(category):
        raise ValueError('Invalid username or category for cgroup path')
    return os.path.join(CGROUP_ROOT, f'parental-{username}-{category}')


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


def ensure_cgroup(username, category):
    """Create parental-<user>-<cat> if missing. Never touches other groups."""
    path = cgroup_path(username, category)
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


def read_freeze(username, category):
    content = _sudo_read_file(os.path.join(cgroup_path(username, category),
                                           'cgroup.freeze'))
    if content is None:
        return None
    return content.strip() == '1'


def set_frozen(username, category, frozen):
    """Freeze (True) or thaw (False) a category cgroup; verify by readback."""
    try:
        path = os.path.join(cgroup_path(username, category), 'cgroup.freeze')
    except ValueError as e:
        return False, str(e)
    if not os.path.isdir(os.path.dirname(path)):
        if frozen:
            ok, msg = ensure_cgroup(username, category)
            if not ok:
                return False, msg
        else:
            return True, 'no cgroup (already unfrozen)'
    ok, msg = _sudo_write_file(path, '1' if frozen else '0')
    if not ok:
        return False, msg
    state = read_freeze(username, category)
    if state is None:
        return False, 'could not verify freeze state'
    if state != bool(frozen):
        return False, 'freeze state mismatch after write'
    return True, ''


def cgroup_members(username, category):
    """PIDs currently in the category cgroup ([] if missing/unreadable)."""
    try:
        path = os.path.join(cgroup_path(username, category), 'cgroup.procs')
    except ValueError:
        return []
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


def migrate_pid(username, category, pid):
    """Move one PID into the category cgroup (no-op behaviorally)."""
    try:
        path = os.path.join(cgroup_path(username, category), 'cgroup.procs')
    except ValueError as e:
        return False, str(e)
    return _sudo_write_file(path, str(int(pid)))


def unfreeze_all_user(username):
    """Thaw every parental-<username>-* cgroup. Only our own name pattern."""
    if not valid_username(username):
        return False, 'Invalid username'
    results = []
    try:
        names = os.listdir(CGROUP_ROOT)
    except OSError as e:
        return False, str(e)
    prefix = f'parental-{username}-'
    for name in names:
        if not name.startswith(prefix):
            continue
        category = name[len(prefix):]
        if not valid_category(category):
            continue
        ok, msg = set_frozen(username, category, False)
        results.append((category, ok, msg))
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
# `parental-<user>-<category>` groups. No signals, no kills, no membership
# changes anywhere.

def parse_managed_cgroup(name):
    """Split `parental-<user>-<category>` -> (username, category).

    Returns None for anything not matching our naming scheme (foreign
    groups such as a future `parental-*-quarantine` reappearing). The split
    is on the LAST hyphen so usernames containing hyphens still parse.
    """
    if not name.startswith('parental-'):
        return None
    rest = name[len('parental-'):]
    if '-' not in rest:
        return None
    username, category = rest.rsplit('-', 1)
    if not valid_username(username) or not valid_category(category):
        return None
    return username, category


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

    budgets_data: budgets.json-shaped dict (default: loaded from disk).
    cgroup_root: directory to scan (default: real /sys/fs/cgroup).
    thaw_fn: callable(username, category) -> (ok, msg) used for the
      unfreeze write (default: set_frozen(..., False) on the real tree,
      direct '0' write on any alternate root so tests never touch sudo
      or the real hierarchy).

    Returns a summary dict {thawed, legit, clean, unknown, skipped, note}.
    Only OUR managed groups are ever thawed, and only when truly frozen
    (desired AND effective) with NO paused/manual-frozen flag for that
    user/category.
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
            def thaw_fn(username, category, _root=root):
                try:
                    cdir = os.path.join(
                        _root, f'parental-{username}-{category}')
                    with open(os.path.join(cdir, 'cgroup.freeze'), 'w') as f:
                        f.write('0')
                except (OSError, ValueError) as e:
                    return False, str(e)
                return True, ''
        else:
            thaw_fn = lambda u, c: set_frozen(u, c, False)  # noqa: E731
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
        username, category = parsed
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
        # budget state marks this user/category paused (auto-exhaustion)
        # OR manually frozen by the parent.
        try:
            user_entry = budgets_data.get(username) or {}
            paused = bool(user_entry.get('paused', {}).get(category, False))
            manual = bool(user_entry.get('manual_frozen', {})
                           .get(category, False))
        except (AttributeError, TypeError):
            paused, manual = False, False
        if paused or manual:
            summary['legit'].append(name)
            continue
        print(f'WATCHDOG: STUCK freeze detected: {name} is frozen '
              f'(freeze=1, frozen 1) with NO paused budget state; '
              f'thawing to self-heal', flush=True)
        try:
            ok, msg = thaw_fn(username, category)
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


def freezable_members(procs, username, category, limits, prefs=None,
                      index=None):
    """Match-time gate: visible after filtering AND in the budgeted category
    AND not NEVER-pause AND category actually has a limit configured."""
    if float(limits.get(category, 0) or 0) <= 0:
        return []
    visible, _hidden = apply_filter(procs, username, prefs)
    out = []
    for proc in visible:
        if categorize(proc, index) != category:
            continue
        if is_never_pause(proc):
            continue
        out.append(proc)
    return out


# ---------------------------------------------------------------------------
# Tick: accounting + reset + enforcement (called every 60s)
# ---------------------------------------------------------------------------

def account_and_enforce(username, entry, procs, bedtime_entry, now,
                        prefs=None, index=None, act=True, notify=None):
    """One budget tick for a single user. Mutates `entry` in place.

    Returns a summary dict {reset, froze: [...], notified: [...], note}.
    With act=False no privileged cgroup operations are performed (used by
    unit tests). notify, when given, is a callable
    ``notify(username, title, body)`` used for best-effort "minutes left"
    and "paused" warnings — every call is guarded so a notification
    failure never blocks enforcement.
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
            return
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
        entry['day'] = today_str
        summary['reset'] = True
        if act:
            ok, _res = unfreeze_all_user(username)
            summary['unfroze'].append('all' if ok else 'failed')
    entry['was_in_bedtime'] = now_in

    limits = entry.get('limits', {})
    if not limits:
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

    def _note_paused(category):
        """'Out of time' notification, once per day per category."""
        if warned_paused.get(category) == today_str:
            return
        label = CATEGORY_LABELS.get(category, category)
        if _warn('%s paused \u2014 out of time' % label,
                 'Your %s time is up for today.' % label,
                 category + ' (paused)'):
            warned_paused[category] = today_str

    for category, limit_min in list(limits.items()):
        try:
            limit_sec = float(limit_min) * 60.0
        except (TypeError, ValueError):
            continue
        if limit_sec <= 0:
            continue
        members = freezable_members(procs, username, category, limits,
                                    prefs, index)
        # An active category burns the whole tick's wall-clock duration;
        # an idle one burns nothing — so usage tracks the Elapsed time
        # parents see for apps actually in use.
        used[category] = float(used.get(category, 0.0) or 0.0)
        category_active = any(
            deltas.get(p['pid'], 0.0) >= ACTIVE_THRESHOLD_SECONDS
            for p in members)
        if category_active:
            used[category] += wall
        exhausted = used[category] >= limit_sec
        remaining = limit_sec - used[category]
        if (not exhausted and 0 < remaining <= LOW_WARN_SECONDS
                and warned_low.get(category) != today_str):
            label = CATEGORY_LABELS.get(category, category)
            if _warn('%s: 5 minutes left' % label,
                     'Less than 5 minutes of %s time left today.' % label,
                     category + ' (low)'):
                warned_low[category] = today_str
        if exhausted and not paused.get(category) and not resumed.get(category) and not manual_frozen.get(category):
            if not act:
                paused[category] = True
                summary['froze'].append(category + ' (dry-run)')
                _note_paused(category)
                continue
            ok, msg = ensure_cgroup(username, category)
            if not ok:
                summary['note'] = f'ensure cgroup: {msg}'
                continue
            for proc in members:
                migrate_pid(username, category, proc['pid'])
            expected = {p['pid']: p for p in members}
            current = cgroup_members(username, category)
            safe, reason = verify_freeze_safety(username, current, expected)
            if not safe:
                summary['note'] = f'freeze aborted for {category}: {reason}'
                continue
            if not members and not current:
                paused[category] = True  # exhausted, nothing running yet
                _note_paused(category)
                continue
            if not members:
                summary['note'] = (f'freeze aborted for {category}: '
                                    'cgroup has members but nothing is freezable')
                continue
            ok, msg = set_frozen(username, category, True)
            if ok:
                paused[category] = True
                summary['froze'].append(category)
                _note_paused(category)
            else:
                summary['note'] = f'freeze failed for {category}: {msg}'
        elif exhausted and paused.get(category) and members and act:
            # Exhausted earlier while nothing was running: a matching app
            # launched since. Migrate it in and (re-)freeze.
            ok, _msg = ensure_cgroup(username, category)
            if ok:
                current = set(cgroup_members(username, category))
                for proc in members:
                    if proc['pid'] not in current:
                        migrate_pid(username, category, proc['pid'])
                expected = {p['pid']: p for p in members}
                safe, reason = verify_freeze_safety(
                    username, cgroup_members(username, category), expected)
                if safe:
                    ok, msg = set_frozen(username, category, True)
                    if ok:
                        summary['froze'].append(category + ' (late start)')
                    else:
                        summary['note'] = (f're-freeze failed for {category}: '
                                           f'{msg}')
                else:
                    summary['note'] = (f're-freeze aborted for {category}: '
                                       f'{reason}')
        elif manual_frozen.get(category) and act:
            # Manually-frozen steady state: the parent deliberately froze
            # this category, so the tick must neither thaw it nor record
            # an auto-pause — just re-assert the freeze for late starters
            # (apps launched after the click), under the same safety
            # gates. Fail-closed: a safety failure leaves the freeze in
            # place and records a note (no notify — the parent sees the
            # state in the UI).
            ok, _msg = ensure_cgroup(username, category)
            if ok:
                current = set(cgroup_members(username, category))
                for proc in members:
                    if proc['pid'] not in current:
                        migrate_pid(username, category, proc['pid'])
                expected = {p['pid']: p for p in members}
                safe, reason = verify_freeze_safety(
                    username, cgroup_members(username, category), expected)
                if safe:
                    ok, msg = set_frozen(username, category, True)
                    if ok:
                        summary['froze'].append(category + ' (manual)')
                    else:
                        summary['note'] = (f'manual re-freeze failed for '
                                           f'{category}: {msg}')
                else:
                    summary['note'] = (f'manual re-freeze aborted for '
                                       f'{category}: {reason}')
        elif members and act and not paused.get(category):
            # Pre-place matching processes so a later freeze is instant.
            # No budget configured -> code never reaches here (limits gate
            # in freezable_members), so uninvolved users are untouched.
            ok, _msg = ensure_cgroup(username, category)
            if ok:
                current = set(cgroup_members(username, category))
                for proc in members:
                    if proc['pid'] not in current:
                        migrate_pid(username, category, proc['pid'])
    return summary


def tick_budgets(bedtime_map, now, procs_provider=None, prefs=None,
                 index=None, act=True, notify=None, run_watchdog=True,
                 cgroup_root=None):
    """Run one budget tick for every user in budgets.json.

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
    Returns a list of per-user summary dicts (+ watchdog entry).
    """
    data = load_budgets()
    if prefs is None:
        prefs = load_prefs()
    changed = True  # usage baselines always advance; persist every tick
    summaries = []
    for username, entry in list(data.items()):
        if not valid_username(username):
            continue
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
                    'warned_low', 'warned_paused'):
            entry.setdefault(key, {})
        if procs_provider is not None:
            procs = procs_provider(username)
        else:
            procs = list_user_processes(username)
        summaries.append(account_and_enforce(
            username, entry, procs, (bedtime_map or {}).get(username),
            now, prefs, index, act, notify))
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


def freeze_category(username, category):
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
        ok, msg = set_frozen(username, category, True)
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


def unfreeze_category(username, category):
    """Manual Unfreeze: thaw and inhibit re-freeze until the next reset.

    Subsumes the old Resume: clears BOTH ``paused`` and ``manual_frozen``
    (plus ``manual_frozen_at``) and sets ``resumed`` so the tick does not
    instantly re-freeze an exhausted category.
    """
    if not valid_username(username):
        return False, 'Invalid username'
    if not valid_category(category):
        return False, f'Unknown category: {category}'
    ok, msg = set_frozen(username, category, False)
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
    return True, 'Resumed; will not re-pause until the next reset'


def resume_category(username, category):
    """Manual Resume: unfreeze and inhibit re-freeze until the next reset.

    Thin alias over :func:`unfreeze_category` (kept so the existing
    ``/api/budgets/resume`` endpoint and CLI-adjacent callers keep
    working with the exact same return contract).
    """
    return unfreeze_category(username, category)


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


def _in_bedtime(entry, now):
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
