#!/usr/bin/env python3
"""Child-side per-user focus session agent (stdlib only).

Part of the per-user session agent (``SESSION_AGENT_PLAN.md`` §3, §7–§8).
Runs as the **child**, inside the child's own graphical session (systemd user
unit ``parental-session-agent.service`` with linger). It:

* polls the desktop for the focused window (KWin ``activeWindow()`` /
  ``getWindowInfo()`` via the ``qdbus`` CLI — the plan's v1 default backend),
* appends **focus** rows (one per focus change, plus one at start), periodic
  **state** heartbeats, and **error** rows to a small JSONL feed under the
  child's own ``XDG_RUNTIME_DIR``,
* records **no** window captions, keystrokes, URLs or any PII, and
* **authenticates nowhere and holds no secrets**: it never imports the panel's
  app/auth modules, never reads ``/etc/parental-controls``, and never invokes
  sudo. The panel pulls the feed read-only; the child drives nothing.

Verify-on-a-real-box note: the KWin ``qdbus`` invocation, the exact
``getWindowInfo`` field names, and whether a lingering logged-out user manager
is reachable by the panel's ``systemd-run --machine=<u>@.host --user cat``
drain (SESSION_AGENT_PLAN.md open question 1) can only be confirmed inside a
live child desktop session. This module is exercised headlessly in tests with
injected clock/source hooks; nothing here was validated against a real KWin.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

# --- schema / limits (mirrored in focus_feed.py; tests pin equality) --------
AGENT_VERSION = '0.1.0'
SCHEMA_VERSION = 1
AGENT_TYPES = ('focus', 'state', 'error')
KINDS = ('app', 'shell', 'lockscreen')
STATES = ('active', 'no_session', 'suspend', 'no_engine', 'locked')

MAX_LINE_BYTES = 4096
MAX_FEED_BYTES = 128 * 1024
MAX_ROWS_PER_DRAIN = 200
FEED_STALE_SECONDS = 180.0
MAX_FOCUS_SESSION_SECONDS = 4 * 3600.0
MAX_TICK_WALL_SECONDS = 300.0

WINDOW_TOKEN_RE = re.compile(r'^[A-Za-z0-9_.\-+]+$')

# Window classes that are never "the child engaging with an app".
LOCK_SCREEN_CLASSES = frozenset({
    'kscreenlocker_greet', 'kscreenlocker', 'ksplashqml',
    'plasma-fallback-session', 'gdm', 'gdm-wayland-session',
    'lightdm-gtk-greeter', 'sddm',
})
SHELL_CLASSES = frozenset({
    'plasmashell', 'kwin_wayland', 'kwin_x11', 'ksmserver', 'klauncher',
    'kded6', 'ksplashqml', 'gnome-shell', 'mutter',
})

# Source sentinel: the desktop/engine is not reachable at all.
NO_ENGINE = object()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def boottime():
    """CLOCK_BOOTTIME seconds (advances across suspend, unlike MONOTONIC)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def read_boot_id(path='/proc/sys/kernel/random/boot_id'):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            value = fh.read().strip()
        if value:
            return value
    except OSError:
        pass
    # Deterministic-ish fallback so a feed row always carries a boot_id.
    return 'unknown-boot'


def default_feed_path(uid=None):
    if uid is None:
        uid = os.getuid()
    runtime = os.environ.get('XDG_RUNTIME_DIR') or '/run/user/%d' % int(uid)
    return os.path.join(runtime, 'parental-controls', 'focus.jsonl')


def classify_kind(wm_class):
    wc = (wm_class or '').lower()
    if wc in LOCK_SCREEN_CLASSES:
        return 'lockscreen'
    if wc in SHELL_CLASSES:
        return 'shell'
    return 'app'


def _valid_window_token(value):
    return isinstance(value, str) and bool(WINDOW_TOKEN_RE.match(value))


def validate_row(row):
    """Return '' when a feed row is schema-valid, else an error string.

    Mirrors ``focus_feed._validate_row`` (defence in depth: the panel
    re-validates everything it reads; neither side trusts the other).
    """
    if row.get('v') != SCHEMA_VERSION:
        return 'bad schema version'
    if row.get('type') not in AGENT_TYPES:
        return 'unknown row type'
    seq = row.get('seq')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return 'bad seq'
    if not isinstance(row.get('agent'), str) or not row['agent']:
        return 'bad agent version'
    if not isinstance(row.get('boot_id'), str) or not row['boot_id']:
        return 'bad boot_id'
    rtype = row['type']
    if rtype == 'focus':
        if not isinstance(row.get('session'), int) or row['session'] < 0:
            return 'bad session'
        if row.get('kind') not in KINDS:
            return 'bad kind'
        wm = row.get('wm_class')
        app = row.get('app_id')
        if wm is None and app is None:
            return 'focus row missing window identity'
        if wm is not None and not _valid_window_token(wm):
            return 'bad wm_class'
        if app is not None and not _valid_window_token(app):
            return 'bad app_id'
        since = row.get('since')
        if not isinstance(since, (int, float)) or isinstance(since, bool) \
                or since < 0 or since > MAX_FOCUS_SESSION_SECONDS:
            return 'bad since'
    elif rtype == 'state':
        if row.get('state') not in STATES:
            return 'bad state'
    else:
        if not isinstance(row.get('code'), str) or not row['code']:
            return 'bad error code'
    return ''


# ---------------------------------------------------------------------------
# Feed writer — appends validated rows, assigns monotonic seq, rotates
# ---------------------------------------------------------------------------

class FeedWriter:
    """Append-only JSONL writer with seq, boot_id and size rotation.

    ``seq`` is strictly monotonic for the life of the feed. On restart within
    the same boot it resumes from the file's high-water mark; on a different
    boot id it starts a fresh epoch (the panel resets on boot_id change too).
    Rotation keeps the file under ``max_bytes`` while never resetting ``seq``
    (the panel's cursor is a drain position, not a budget quantity).
    """

    def __init__(self, path, boot_id=None, agent_version=AGENT_VERSION,
                 max_bytes=MAX_FEED_BYTES):
        self.path = path
        self.stdout = path in (None, '-')
        self.boot_id = boot_id or read_boot_id()
        self.agent_version = agent_version
        self.max_bytes = max_bytes
        if not self.stdout:
            self.dir = os.path.dirname(path) or '.'
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            self._reject_symlink()
        self.seq = self._resume_seq()

    def _reject_symlink(self):
        if os.path.islink(self.path):
            # Never follow a planted symlink out of the runtime dir.
            raise ValueError('feed path is a symlink: %s' % self.path)

    def _resume_seq(self):
        if self.stdout:
            return 0
        try:
            with open(self.path, 'r', encoding='utf-8', errors='replace') as fh:
                lines = fh.read().splitlines()
        except OSError:
            return 0
        seq = 0
        boot = None
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if boot is None:
                boot = row.get('boot_id')
            try:
                seq = max(seq, int(row.get('seq', 0)))
            except (TypeError, ValueError):
                continue
        if boot is not None and boot != self.boot_id:
            # New boot: previous feed cannot be trusted for cursor/session.
            self._truncate()
            return 0
        return seq

    def _truncate(self):
        try:
            with open(self.path, 'w', encoding='utf-8'):
                pass
        except OSError:
            pass

    def _rotate_if_needed(self, incoming_bytes):
        if self.stdout:
            return
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size and size + incoming_bytes > self.max_bytes:
            backup = self.path + '.1'
            try:
                os.replace(self.path, backup)
            except OSError:
                pass

    def base_row(self, rtype):
        return {'v': SCHEMA_VERSION, 'agent': self.agent_version,
                'boot_id': self.boot_id, 'ts': now_iso(), 'type': rtype}

    def write(self, row, now_bt=None):
        """Assign seq/ids, validate, append. Returns the written row."""
        row = dict(row)
        row['v'] = SCHEMA_VERSION
        row.setdefault('agent', self.agent_version)
        row.setdefault('boot_id', self.boot_id)
        row.setdefault('ts', now_iso())
        if now_bt is not None:
            row['bt'] = now_bt
        self.seq += 1
        row['seq'] = self.seq
        error = validate_row(row)
        if error:
            self.seq -= 1
            raise ValueError('refusing to write invalid row: %s' % error)
        line = json.dumps(row, separators=(',', ':'), sort_keys=True)
        encoded = (line + '\n').encode('utf-8')
        if len(encoded) > MAX_LINE_BYTES:
            self.seq -= 1
            raise ValueError('row exceeds line cap')
        if self.stdout:
            sys.stdout.write(line + '\n')
            sys.stdout.flush()
            return row
        self._rotate_if_needed(len(encoded))
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        return row

    def write_focus(self, wm_class, app_id, kind, session, since, now_bt):
        row = self.base_row('focus')
        row.update({'wm_class': wm_class, 'kind': kind, 'session': session,
                    'since': float(since), 'bt': now_bt})
        if app_id:
            row['app_id'] = app_id
        return self.write(row, now_bt)

    def write_state(self, state, now_bt):
        row = self.base_row('state')
        row['state'] = state
        return self.write(row, now_bt)

    def write_error(self, code, msg, now_bt):
        row = self.base_row('error')
        row.update({'code': code, 'msg': msg})
        return self.write(row, now_bt)


# ---------------------------------------------------------------------------
# Focus source — KWin activeWindow() poll backend (v1 default)
# ---------------------------------------------------------------------------

def parse_kwin_info(text):
    """Best-effort parse of KWin ``getWindowInfo`` output.

    Only ``resourceClass`` (wm_class), ``desktopFileName`` (app_id) and a
    derived ``kind`` are extracted — captions are deliberately ignored so no
    PII reaches the feed. Returns None when no window is identifiable.
    """
    if not text:
        return None

    def field(name):
        m = re.search(r'(?m)^\s*%s:\s*"?([^"\n]*)"?\s*$'
                      % re.escape(name), text)
        return m.group(1).strip() if m else None

    wm_class = field('resourceClass')
    if not wm_class:
        return None
    app_id = field('desktopFileName')
    return {'wm_class': wm_class, 'app_id': app_id or None,
            'kind': classify_kind(wm_class)}


class KWinPollSource:
    """Polls ``org.kde.KWin.activeWindow``/``getWindowInfo`` via qdbus.

    ``runner``/``which`` are injectable so tests never touch a real desktop.
    ``available()`` is False when no qdbus CLI or no display is present
    (headless / non-KDE), which makes the agent report ``no_engine``.
    """

    def __init__(self, runner=None, which=None):
        self.runner = runner or subprocess.run
        self.which = which or shutil.which

    def _qdbus(self):
        for name in ('qdbus6', 'qdbus-qt6', 'qdbus'):
            found = self.which(name)
            if found:
                return found
        return None

    def available(self):
        if not self._qdbus():
            return False
        return bool(os.environ.get('WAYLAND_DISPLAY')
                    or os.environ.get('DISPLAY'))

    def current_window(self):
        qdbus = self._qdbus()
        if not qdbus:
            return NO_ENGINE
        try:
            active = self.runner(
                [qdbus, 'org.kde.KWin', '/KWin',
                 'org.kde.KWin.activeWindow'],
                capture_output=True, text=True, timeout=5)
        except Exception:
            return NO_ENGINE
        if getattr(active, 'returncode', 1) != 0:
            return NO_ENGINE
        wid = (active.stdout or '').strip().splitlines()
        if not wid:
            return None
        try:
            info = self.runner(
                [qdbus, 'org.kde.KWin', '/KWin',
                 'org.kde.KWin.getWindowInfo', wid[-1].strip()],
                capture_output=True, text=True, timeout=5)
        except Exception:
            return NO_ENGINE
        if getattr(info, 'returncode', 1) != 0:
            return NO_ENGINE
        return parse_kwin_info(info.stdout)


# ---------------------------------------------------------------------------
# Session state machine
# ---------------------------------------------------------------------------

class SessionAgent:
    """Drives one focus source into a feed. Fully injectable for tests."""

    POLL_SECONDS = 5.0
    HEARTBEAT_SECONDS = 30.0
    SUSPEND_GAP_SECONDS = 120.0

    def __init__(self, writer, source, clock=boottime,
                 poll_seconds=None, heartbeat_seconds=None,
                 suspend_gap_seconds=None):
        self.writer = writer
        self.source = source
        self.clock = clock
        self.poll_seconds = (self.POLL_SECONDS if poll_seconds is None
                             else poll_seconds)
        self.heartbeat_seconds = (self.HEARTBEAT_SECONDS
                                  if heartbeat_seconds is None
                                  else heartbeat_seconds)
        self.suspend_gap_seconds = (self.SUSPEND_GAP_SECONDS
                                    if suspend_gap_seconds is None
                                    else suspend_gap_seconds)
        self.current = None
        self.since_bt = None
        self.session = 0
        self.last_tick_bt = None
        self.last_heartbeat_bt = None
        self.started = False
        self._stop = False

    # -- helpers -----------------------------------------------------------
    def _close_current(self, end_bt, rows):
        """Emit a closing focus row for the window leaving focus."""
        if self.current is None:
            return
        duration = max(0.0, end_bt - (self.since_bt or end_bt))
        if duration > MAX_FOCUS_SESSION_SECONDS:
            duration = MAX_FOCUS_SESSION_SECONDS
        self.session += 1
        rows.append(self.writer.write_focus(
            self.current.get('wm_class'), self.current.get('app_id'),
            self.current.get('kind', 'app'), self.session, duration, end_bt))
        self.current = None

    def _seed(self, window, now_bt, rows):
        """Emit the opening focus row for a newly focused window."""
        self.current = window
        self.since_bt = now_bt
        self.session += 1
        rows.append(self.writer.write_focus(
            window.get('wm_class'), window.get('app_id'),
            window.get('kind', 'app'), self.session, 0.0, now_bt))

    def _same(self, window):
        if self.current is None:
            return False
        return (window.get('wm_class') == self.current.get('wm_class')
                and window.get('app_id') == self.current.get('app_id')
                and window.get('kind') == self.current.get('kind'))

    def _heartbeat_due(self, now_bt):
        return (self.last_heartbeat_bt is None
                or now_bt - self.last_heartbeat_bt >= self.heartbeat_seconds)

    # -- one cycle ---------------------------------------------------------
    def run_once(self, now_bt=None):
        """Run a single poll/heartbeat cycle; returns the rows written."""
        now_bt = self.clock() if now_bt is None else now_bt
        rows = []

        # Suspend / sleep: BOOTTIME jumps but no ticks elapsed. Close the
        # open session at the pre-sleep instant and flag the gap.
        if self.last_tick_bt is not None \
                and now_bt - self.last_tick_bt > self.suspend_gap_seconds:
            self._close_current(self.last_tick_bt, rows)
            rows.append(self.writer.write_state('suspend', self.last_tick_bt))
            self.last_heartbeat_bt = self.last_tick_bt
        self.last_tick_bt = now_bt

        if not self.source.available():
            if not self.started:
                rows.append(self.writer.write_error(
                    'no_engine', 'no focus engine available', now_bt))
            if self._heartbeat_due(now_bt) or not self.started:
                rows.append(self.writer.write_state('no_engine', now_bt))
                self.last_heartbeat_bt = now_bt
            self.started = True
            return rows

        window = self.source.current_window()
        if window is NO_ENGINE:
            if not self.started:
                rows.append(self.writer.write_error(
                    'kwin_unreachable', 'kwin bus unreachable', now_bt))
            if self._heartbeat_due(now_bt) or not self.started:
                rows.append(self.writer.write_state('no_engine', now_bt))
                self.last_heartbeat_bt = now_bt
            self.started = True
            return rows

        if window is None:
            # No graphical session (logged out / lingering headless).
            self._close_current(now_bt, rows)
            if self._heartbeat_due(now_bt) or not self.started:
                rows.append(self.writer.write_state('no_session', now_bt))
                self.last_heartbeat_bt = now_bt
            self.started = True
            return rows

        # A live window holds focus.
        if self.current is None:
            self._seed(window, now_bt, rows)
        elif not self._same(window):
            self._close_current(now_bt, rows)
            self._seed(window, now_bt, rows)
        # else: same window still focused; nothing to record.

        if self._heartbeat_due(now_bt) or not self.started:
            rows.append(self.writer.write_state('active', now_bt))
            self.last_heartbeat_bt = now_bt
        self.started = True
        return rows

    # -- loop --------------------------------------------------------------
    def stop(self, *_args):
        self._stop = True

    def run_forever(self, sleep=time.sleep):
        while not self._stop:
            self.run_once()
            sleep(self.poll_seconds)


# ---------------------------------------------------------------------------
# CLI (mostly for the unit + headless verification)
# ---------------------------------------------------------------------------

def _build_source(kind, runner=None, which=None):
    if kind == 'none':
        class _NoEngine:
            def available(self):
                return False

            def current_window(self):
                return NO_ENGINE
        return _NoEngine()
    return KWinPollSource(runner=runner, which=which)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--feed-path', default=None,
                        help='JSONL feed path (default $XDG_RUNTIME_DIR/...)')
    parser.add_argument('--source', choices=('poll', 'none'), default='poll')
    parser.add_argument('--once', action='store_true',
                        help='run a single cycle and exit')
    parser.add_argument('--dry-run', action='store_true',
                        help='write rows to stdout instead of the feed')
    parser.add_argument('--poll-seconds', type=float, default=None)
    args = parser.parse_args(argv)

    path = '-' if args.dry_run else (args.feed_path or default_feed_path())
    writer = FeedWriter(path)
    source = _build_source(args.source)
    agent = SessionAgent(writer, source, poll_seconds=args.poll_seconds)
    if args.once:
        agent.run_once()
        return 0
    signal.signal(signal.SIGTERM, agent.stop)
    signal.signal(signal.SIGINT, agent.stop)
    agent.run_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
