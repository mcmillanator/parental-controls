"""Categorization: .desktop Categories + curated overrides.

`parse_desktop_file`/`build_desktop_index`/`build_desktop_names` read the
installed .desktop entries, `categorize` maps a visible app process to
games/browsers/media/chat/other (curated overrides > .desktop Categories >
launcher-spawned app tokens), and `is_user_app`/`mark_user_apps`/
`annotate_display_names` label what the UI shows. JVM/flatpak app tokens are
recognised here too (TIME_TRACKING_QA.md F1/F3, §7 FIX-1.x).

Moved verbatim out of budgets.py by RESTRUCTURE_PLAN.md slice 4; the facade
re-exports every name by name, INCLUDING the two cache dicts by identity —
`_DESKTOP_INDEX_CACHE` and `_DESKTOP_INDEX_TTL_SECONDS` stay the same
objects, so a test that `.clear()`s them through the facade still resets the
real cache. `CATEGORIES`/`_argv0_basename` resolve through `budgets` at call
time — see the cross-seam convention in budgets_guards.py.
"""

import glob
import os
import re
import shlex

# ---------------------------------------------------------------------------
# Categorization: .desktop Categories + curated overrides
# ---------------------------------------------------------------------------

# keyword -> category. Matched as whole words (\b) against "comm + cmdline"
# (lowercased) so e.g. 'edge' does not match 'knowledge'. Steam chain first.
# Minecraft is a JVM (`comm=java`): the game itself is attributed through
# these launcher/game keywords on the cmdline (`net.minecraft...` class
# names, `--gameDir .../.minecraft` paths), never through `java` itself —
# see _GENERIC_INTERPRETERS / _cmdline_app_tokens (TIME_TRACKING_QA.md F1,
# §7 FIX-1).
CATEGORY_OVERRIDES = {
    'games': (
        'steam', 'gameoverlayui', 'gamescope', 'pressure-vessel',
        'steamwebhelper', 'reaper', 'proton', 'wineserver', 'wine-preloader',
        'lutris', 'heroic', 'bottles',
        'minecraft', 'prismlauncher', 'multimc', 'adventure-launcher',
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
        import budgets
        try:
            home = budgets.pwd.getpwnam(username).pw_dir
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
    import budgets
    for d in budgets.desktop_dirs_for(username):
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


# Tick-side desktop index cache (FIX-2, TIME_TRACKING_QA.md §7): the
# enforcement tick must categorize with the same .desktop index the UI
# builds per render, but a 60s timer should not glob the desktop dirs
# once per user per tick — cache per user with a short TTL.
_DESKTOP_INDEX_TTL_SECONDS = 300.0
_DESKTOP_INDEX_CACHE = {}  # username -> (monotonic time, index dict)


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


# Interpreter/runtime basenames: an argv0 or path segment like `java`
# says nothing about WHICH app runs on it (a Minecraft JVM and a
# JetBrains IDE both run `java`), so these never attribute a process by
# themselves — attribution comes from the app identity carried in the
# arguments instead (TIME_TRACKING_QA.md §7 FIX-1: "keep bare java
# category-neutral").
_GENERIC_INTERPRETERS = frozenset({
    'java', 'python', 'python2', 'python3', 'node', 'nodejs', 'bun',
    'deno', 'electron', 'mono', 'dotnet', 'ruby', 'perl', 'php', 'sh',
    'bash', 'dash', 'zsh', 'fish', 'wine', 'wine64',
})

# Registry/TLD-ish and generic dot-name parts dropped from class-name
# style tokens (mirrors _flatpak_appid_parts / _exec_candidates).
_GENERIC_DOT_PARTS = frozenset({'org', 'com', 'io', 'net', 'edu', 'gov',
                                'desktop', 'app', 'main', 'client'})

# Curated friendly names for launcher-spawned games whose visible process
# is the JVM (comm 'java'): shown on /user rows instead of bare 'java'
# so parents can find the app and set per-app overrides for it.
_JVM_APP_NAMES = (
    ('minecraft', 'Minecraft'),
    ('prismlauncher', 'Prism Launcher'),
    ('multimc', 'MultiMC'),
    ('adventure-launcher', 'Adventure Launcher'),
)
_JVM_APP_RES = [(re.compile(r'\b' + re.escape(kw) + r'\b'), friendly)
                for kw, friendly in _JVM_APP_NAMES]


def _cmdline_app_tokens(cmdline):
    """Yield candidate app-identity tokens from a command line (lowercase).

    Launcher-spawned processes (JVMs, wrappers) carry the real app in
    their args rather than in argv0: path segments (`~/.minecraft`,
    `.../PrismLauncher/instances/...`), JVM class names
    (`net.minecraft.client.main.Main`) and flag values (`--gameDir
    <path>`, `-cp <paths>`, `-Djava.library.path=<path>`). Used by
    categorize() as a fallback index lookup and for the friendly
    game-name annotation (TIME_TRACKING_QA.md §7 FIX-1.2). Bare
    interpreters, registry-ish dot-parts and <4-char noise are dropped
    so a JetBrains `java -jar /opt/idea/idea.jar` stays neutral.
    """
    seen = set()

    def emit(word):
        w = word.lower()
        if len(w) < 4 or w in _GENERIC_INTERPRETERS or w in _GENERIC_DOT_PARTS:
            return None
        if w in seen:
            return None
        seen.add(w)
        return w

    for raw in (cmdline or '').split():
        tok = raw.strip('"\'')
        if tok.startswith('-'):
            tok = tok.lstrip('-')
            _flag, sep, rhs = tok.partition('=')
            if not sep:
                continue  # bare flag: the identity is in the NEXT token
            tok = rhs
        if not tok:
            continue
        parts = tok.split('/') if '/' in tok else [tok]
        for part in parts:
            if not part:
                continue
            w = emit(part)
            if w:
                yield w
            if '.' in part:  # class names, ids and dotfiles: `minecraft`
                for sub in part.split('.'):
                    w = emit(sub)
                    if w:
                        yield w


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
    import budgets
    comm = (proc.get('comm') or '').lower()
    cmdline = proc.get('cmdline') or proc.get('args') or ''
    base = budgets._argv0_basename(cmdline)
    if base and base in index:
        return True
    if comm and comm in index:
        return True
    haystack = (comm + ' ' + cmdline).lower()
    for cat in budgets.CATEGORIES:
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

    Lookup is argv[0]-basename first, then comm; a process with neither
    still gets a friendly name when its cmdline carries a known
    launcher-spawned app identity (a Minecraft JVM shows as "Minecraft",
    not bare "java" — TIME_TRACKING_QA.md F3, §7 FIX-1.4). Anything
    without a known friendly name falls back to its comm so callers can
    render `display_name` unconditionally.
    """
    names = names or {}
    import budgets
    for proc in procs:
        cmdline = proc.get('cmdline') or proc.get('args') or ''
        base = budgets._argv0_basename(cmdline)
        comm = proc.get('comm') or ''
        name = names.get(base) or names.get(comm.lower())
        if not name:
            haystack = (comm + ' ' + cmdline).lower()
            for rx, friendly in _JVM_APP_RES:
                if rx.search(haystack):
                    name = friendly
                    break
        proc['display_name'] = name or comm
    return procs


def categorize(proc, index=None):
    """Map a visible app proc to games/browsers/media/chat/other.

    Precedence: curated overrides (full cmdline match) > .desktop
    Categories (argv0/comm key, then launcher-spawned app-identity
    tokens from the cmdline) > fallback 'other'.
    """
    comm = (proc.get('comm') or '').lower()
    cmdline = proc.get('cmdline') or proc.get('args') or ''
    haystack = (comm + ' ' + cmdline).lower()
    import budgets
    for cat in budgets.CATEGORIES:
        if cat == 'other':
            continue
        for rx in _OVERRIDE_RES[cat]:
            if rx.search(haystack):
                return cat
    if index:
        base = budgets._argv0_basename(cmdline) or comm
        if base in index:
            return index[base]
        if comm in index:
            return index[comm]
        for token in _cmdline_app_tokens(cmdline):
            if token in index:
                return index[token]
    return 'other'


def group_by_category(procs, index=None):
    """Group procs into an ordered {category: [procs]} dict."""
    import budgets
    groups = {cat: [] for cat in budgets.CATEGORIES}
    for proc in procs:
        cat = budgets.categorize(proc, index)
        proc['category'] = cat
        groups[cat].append(proc)
    return groups
