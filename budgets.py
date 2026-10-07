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

_USERNAME_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')


def valid_username(username):
    """Guard against path traversal before a username touches cgroup paths."""
    return bool(username) and bool(_USERNAME_RE.match(username))


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
    """Split procs into (visible, hidden) honoring defaults + user prefs."""
    if prefs is None:
        prefs = load_prefs()
    entry = prefs.get(username, {}) if isinstance(prefs, dict) else {}
    user_hidden = set(entry.get('hidden_comm', []))
    user_shown = set(entry.get('shown_comm', []))
    visible, hidden = [], []
    for proc in procs:
        comm = proc.get('comm', '')
        default = is_default_hidden(proc)
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
    """Parse one .desktop file -> (keys, category) or None."""
    category = None
    exec_value = None
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
    except OSError:
        return None
    if not exec_value or not category:
        return None
    return list(_exec_candidates(exec_value)), category


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


def build_desktop_index(username=None):
    """Map executable-basename -> category from installed .desktop files."""
    index = {}
    for d in desktop_dirs_for(username):
        try:
            files = glob.glob(os.path.join(d, '*.desktop'))
        except OSError:
            continue
        for path in files:
            parsed = parse_desktop_file(path)
            if not parsed:
                continue
            keys, category = parsed
            for key in keys:
                # First file wins; games-relevant entries rarely collide.
                index.setdefault(key, category)
    return index


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
            'paused': {}, 'resumed': {}, 'was_in_bedtime': False,
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
        # Dropping the limit releases any freeze for that category.
        entry.get('paused', {}).pop(category, None)
        entry.get('resumed', {}).pop(category, None)
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
    status = {}
    for cat in CATEGORIES:
        limit_min = float(limits.get(cat, 0) or 0)
        used_sec = float(used.get(cat, 0) or 0)
        status[cat] = {
            'minutes': limit_min,
            'used_minutes': int(round(used_sec / 60.0)),
            'remaining_minutes': int(round(max(0.0, limit_min * 60 - used_sec) / 60.0))
            if limit_min > 0 else None,
            'paused': bool(paused.get(cat, False)),
            'exhausted': bool(limit_min > 0 and used_sec >= limit_min * 60),
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
                        prefs=None, index=None, act=True):
    """One budget tick for a single user. Mutates `entry` in place.

    Returns a summary dict {reset, froze: [...], note}. With act=False no
    privileged cgroup operations are performed (used by unit tests).
    """
    today_str = now.date().isoformat()
    has_bedtime = bool(bedtime_entry and bedtime_entry.get('start'))
    now_in = bool(bedtime_entry and _in_bedtime(bedtime_entry, now))
    was_in = bool(entry.get('was_in_bedtime', False))
    summary = {'user': username, 'reset': False, 'froze': [],
               'unfroze': [], 'note': ''}

    if entry.get('day') is None:
        entry['day'] = today_str
    if budget_reset_needed(entry.get('day'), has_bedtime, was_in, now_in,
                           today_str):
        entry['used_seconds'] = {}
        entry['paused'] = {}
        entry['resumed'] = {}
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
        if exhausted and not paused.get(category) and not resumed.get(category):
            if not act:
                paused[category] = True
                summary['froze'].append(category + ' (dry-run)')
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
                continue
            if not members:
                summary['note'] = (f'freeze aborted for {category}: '
                                    'cgroup has members but nothing is freezable')
                continue
            ok, msg = set_frozen(username, category, True)
            if ok:
                paused[category] = True
                summary['froze'].append(category)
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
                 index=None, act=True):
    """Run one budget tick for every user in budgets.json.

    bedtime_map: {username: bedtime_entry} (from the app's bedtime loader).
    procs_provider: callable(username) -> procs (default: live ps snapshot).
    Returns a list of per-user summary dicts.
    """
    data = load_budgets()
    if prefs is None:
        prefs = load_prefs()
    changed = True  # usage baselines always advance; persist every tick
    summaries = []
    for username, entry in list(data.items()):
        if not valid_username(username):
            continue
        for key in ('limits', 'used_seconds', 'last_cpu', 'paused', 'resumed'):
            entry.setdefault(key, {})
        if procs_provider is not None:
            procs = procs_provider(username)
        else:
            procs = list_user_processes(username)
        summaries.append(account_and_enforce(
            username, entry, procs, (bedtime_map or {}).get(username),
            now, prefs, index, act))
    if changed:
        try:
            save_budgets(data)
        except OSError:
            pass
    return summaries


def resume_category(username, category):
    """Manual Resume: unfreeze and inhibit re-freeze until the next reset."""
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
        entry.setdefault('resumed', {})[category] = True
        save_budgets(data)
    except OSError as e:
        return False, str(e)
    return True, 'Resumed; will not re-pause until the next reset'


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
