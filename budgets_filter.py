"""Process filtering: the curated hidden denylist, NEVER-pause, user prefs.

`is_default_hidden` applies the shipped denylist (comm, argv[0] basename and
path prefixes), `is_never_pause` is the safety list of processes that must
never be frozen regardless of budgets, and `load_prefs`/`save_prefs`/
`set_pref`/`apply_filter` layer each user's Hide / Show-hidden state on top.
The freezer gates all read `is_never_pause` again at freeze time.

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 3; the facade
re-exports every name by name. `PROCESS_PREFS_FILE` and `valid_username`
resolve through `budgets` at call time so the sqlite/safeguard patches stay
effective — see the cross-seam convention in budgets_guards.py.
"""

import os

import store

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
    import budgets
    return store.load_table(store.db_path_for(budgets.PROCESS_PREFS_FILE),
                            'process_prefs')


def save_prefs(data):
    import budgets
    store.save_table(store.db_path_for(budgets.PROCESS_PREFS_FILE),
                     'process_prefs', data, budgets.PROCESS_PREFS_FILE)


def set_pref(username, comm, action):
    """action 'hide' -> hide this comm; 'show' -> re-show it.

    Returns (ok, message).
    """
    import budgets
    if not budgets.valid_username(username):
        return False, 'Invalid username'
    comm = (comm or '').strip()
    if not comm:
        return False, 'No command provided'
    if action not in ('hide', 'show'):
        return False, f'Unknown action: {action}'
    data = budgets.load_prefs()
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
    import budgets
    if prefs is None:
        prefs = budgets.load_prefs()
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
