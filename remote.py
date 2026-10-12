#!/usr/bin/env python3
"""Hub-side remote control client + ``devices.json`` store (Phase 1).

MULTIDEVICE_PLAN.md §4.1/§4.2: the panel acts as a client of the managed
boxes' agents. This module is stdlib-only (``urllib.request`` + ``ssl``) so
the project keeps its lean runtime dependency set; the transport is isolated
in :class:`RemoteClient` so a different HTTP library could be dropped in
without touching callers.

Security properties (pinned by tests/):

* **Per-box token.** Each device carries its own bearer token; a leaked
  token controls exactly one box. Tokens live in ``devices.json`` (0600)
  and never appear in logs, errors, or audit detail.
* **Fail visible.** An unreachable/timeout/malformed agent raises
  :class:`RemoteError` with a clear kind; the hub never fabricates a
  success or serves stale data.
* **Loopback-first.** A plain-HTTP URL is refused unless the host is
  loopback (or the operator explicitly allows insecure LAN), mirroring the
  panel's localhost-first stance (§1.3, §4.3).

Root-only CLI (this module's ``__main__``):

    python3 remote.py device add <name> --url http://localhost:5001 \
        --token <printed-once> [--insecure-lan]
    python3 remote.py device list
    python3 remote.py device remove <name>
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

DEVICES_FILE_DEFAULT = '/etc/parental-controls/devices.json'
DEVICES_ENV = 'PARENTAL_CONTROLS_DEVICES'
AGENT_API_PREFIX = '/api/v1'
DEFAULT_TIMEOUT = 8.0
TOKEN_MIN_BYTES = 16  # a real token is >= 256 bits (token_urlsafe(32))

_NAME_RX = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_LOOPBACK_HOSTS = ('127.0.0.1', '::1', 'localhost', '')


class DevicesError(Exception):
    """devices.json missing-when-required, corrupt, or rejected config."""


class RemoteError(Exception):
    """A remote call that did not produce a valid agent response.

    ``kind`` is one of: unreachable, timeout, http, unauthorized,
    malformed, refused, config. The message never contains the token.
    """

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# devices.json store (0600, absent == single-box mode)
# ---------------------------------------------------------------------------

def devices_path():
    return os.environ.get(DEVICES_ENV) or DEVICES_FILE_DEFAULT


def blank_devices():
    return {'version': 1, 'devices': {}}


def load_devices(path=None):
    """Load devices.json.

    A missing file is *not* an error: it means single-box mode and returns
    a blank store. A corrupt/unreadable file raises DevicesError (fail
    closed — never silently drop a registered device).
    """
    path = path or devices_path()
    if not os.path.exists(path):
        return blank_devices()
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise DevicesError(f'devices store unreadable ({path}): {e}')
    if not isinstance(data, dict) or not isinstance(data.get('devices'), dict):
        raise DevicesError(f'devices store corrupt ({path}): bad shape')
    return data


def save_devices(data, path=None):
    """Atomic write (temp + rename), mode 0600. Raises DevicesError."""
    path = path or devices_path()
    parent = os.path.dirname(path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent or '.', prefix='.devices-')
        try:
            with os.fdopen(fd, 'w') as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError) as e:
        raise DevicesError(f'devices store write failed ({path}): {e}')


def devices_enabled(path=None):
    """Feature gate: multi-device mode is on only when devices.json exists."""
    return os.path.exists(path or devices_path())


def token_fingerprint(token):
    """Stable, non-reversible identifier for display/audit (never the token)."""
    return hashlib.sha256(str(token).encode('utf-8')).hexdigest()[:12]


def validate_name(name):
    if not _NAME_RX.match(str(name or '')):
        raise DevicesError(
            f'invalid device name {name!r}: use letters, digits, . _ - '
            '(1-64 chars, must not start with a separator)')
    return str(name)


def validate_url(url, allow_insecure=False):
    """Refuse plain HTTP to a non-loopback host unless explicitly allowed."""
    if not url:
        raise DevicesError('device URL is required')
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme not in ('http', 'https'):
        raise DevicesError(f'device URL must be http:// or https:// ({url})')
    if not parsed.netloc:
        raise DevicesError(f'device URL has no host ({url})')
    if parsed.scheme == 'http' and not allow_insecure:
        host = (parsed.hostname or '').lower()
        if host not in _LOOPBACK_HOSTS:
            raise DevicesError(
                f'refusing plain-HTTP device URL {url!r}: the agent token '
                'would travel in the clear. Use an SSH tunnel to a loopback '
                'URL, https://, or pass --insecure-lan to override.')
    return str(url).rstrip('/')


def add_device(name, url, token, hostname=None, allow_insecure=False,
               path=None):
    """Add or update a device. Returns the stored entry (no token echoed)."""
    name = validate_name(name)
    url = validate_url(url, allow_insecure=allow_insecure)
    if not token or len(str(token)) < TOKEN_MIN_BYTES:
        raise DevicesError('agent token is missing or too short')
    data = load_devices(path)
    entry = {
        'name': name,
        'url': url,
        'token': str(token),
        'token_fingerprint': token_fingerprint(token),
        'hostname': hostname or '',
        'registered_at': _now_iso(),
    }
    data['devices'][name] = entry
    save_devices(data, path)
    return entry


def remove_device(name, path=None):
    """Remove a device. Returns True when one was removed."""
    data = load_devices(path)
    if name not in data.get('devices', {}):
        return False
    del data['devices'][name]
    save_devices(data, path)
    return True


def get_device(name, path=None):
    data = load_devices(path)
    entry = data.get('devices', {}).get(name)
    if not isinstance(entry, dict):
        return None
    return entry


def list_devices(path=None):
    data = load_devices(path)
    return [data['devices'][k] for k in sorted(data['devices'])]


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def describe_device(entry):
    """Token-free summary dict for logs/UI/CLI output."""
    return {
        'name': entry.get('name'),
        'url': entry.get('url'),
        'hostname': entry.get('hostname') or '',
        'registered_at': entry.get('registered_at') or '',
        'token_fingerprint': entry.get('token_fingerprint') or '',
        'token_set': bool(entry.get('token')),
    }


# ---------------------------------------------------------------------------
# HTTP transport (stdlib). Isolated so it can be swapped without callers.
# ---------------------------------------------------------------------------

def _default_opener(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout)


class RemoteClient:
    """Client for one managed box's agent.

    ``opener`` is injectable for tests (signature ``opener(request,
    timeout)`` -> file-like). Errors are normalized to :class:`RemoteError`
    and never include the token.
    """

    def __init__(self, name, url, token, timeout=DEFAULT_TIMEOUT,
                 opener=None, panel_actor=None, panel_id=None):
        self.name = name
        self.url = str(url).rstrip('/')
        self.token = token
        self.timeout = timeout
        self._opener = opener or _default_opener
        self.panel_actor = panel_actor
        self.panel_id = panel_id

    def _request(self, method, path, params=None):
        url = f'{self.url}{AGENT_API_PREFIX}{path}'
        data = None
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        if self.panel_actor:
            headers['X-Panel-Actor'] = str(self.panel_actor)
        if self.panel_id:
            headers['X-Panel-Id'] = str(self.panel_id)
        if method == 'GET':
            if params:
                url += '?' + urllib.parse.urlencode(params)
        else:
            data = urllib.parse.urlencode(params or {}).encode('utf-8')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            resp = self._opener(req, self.timeout)
        except urllib.error.HTTPError as e:
            body = self._read(e)
            status = e.code
            payload = self._decode(body)
            if status == 401:
                return status, payload
            return status, payload
        except urllib.error.URLError as e:
            reason = getattr(e, 'reason', e)
            if isinstance(reason, TimeoutError) or 'timed out' in str(reason):
                raise RemoteError('timeout',
                                  f'device {self.name!r} timed out')
            raise RemoteError('unreachable',
                              f'device {self.name!r} unreachable: {reason}')
        except TimeoutError:
            raise RemoteError('timeout', f'device {self.name!r} timed out')
        except OSError as e:
            raise RemoteError('unreachable',
                              f'device {self.name!r} unreachable: {e}')
        body = self._read(resp)
        return getattr(resp, 'status', 200), self._decode(body)

    @staticmethod
    def _read(resp):
        try:
            return resp.read()
        except Exception:
            return b''

    @staticmethod
    def _decode(body):
        if not body:
            return {}
        try:
            obj = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError, AttributeError):
            raise RemoteError('malformed',
                              'device returned a non-JSON response')
        if not isinstance(obj, (dict, list)):
            raise RemoteError('malformed', 'device returned unexpected JSON')
        return obj

    def get(self, path, params=None):
        return self._request('GET', path, params)

    def post(self, path, params=None):
        return self._request('POST', path, params)


def client_for(entry, timeout=DEFAULT_TIMEOUT, opener=None, panel_actor=None,
               panel_id=None):
    return RemoteClient(entry.get('name'), entry.get('url'),
                        entry.get('token'), timeout=timeout, opener=opener,
                        panel_actor=panel_actor, panel_id=panel_id)


def proxy(entry, method, subpath, params=None, timeout=DEFAULT_TIMEOUT,
          opener=None, panel_actor=None, panel_id=None):
    """Forward one hub request to a device agent.

    Returns ``(status_code, payload_dict, token_rejected_bool)``. Raises
    :class:`RemoteError` on transport failure so the caller can answer with
    a clear, fail-visible error (never a silent success).
    """
    client = client_for(entry, timeout=timeout, opener=opener,
                        panel_actor=panel_actor, panel_id=panel_id)
    path = '/' + str(subpath or '').lstrip('/')
    status, payload = client._request(method.upper(), path, params)
    return status, payload, status == 401


# ---------------------------------------------------------------------------
# CLI (root-only device management)
# ---------------------------------------------------------------------------

def _cmd_device_add(args):
    hostname = args.hostname or ''
    entry = add_device(args.name, args.url, args.token, hostname=hostname,
                       allow_insecure=args.insecure_lan,
                       path=args.devices)
    print(f"Registered device {entry['name']} -> {entry['url']} "
          f"(token fingerprint {entry['token_fingerprint']})")
    return 0


def _cmd_device_list(args):
    try:
        entries = list_devices(path=args.devices)
    except DevicesError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1
    if not entries:
        print('No devices registered (single-box mode).')
        return 0
    for entry in entries:
        info = describe_device(entry)
        print(f"{info['name']}\t{info['url']}\thost={info['hostname'] or '-'}"
              f"\ttoken={info['token_fingerprint'] or '-'}"
              f"\tregistered={info['registered_at'] or '-'}")
    return 0


def _cmd_device_remove(args):
    try:
        removed = remove_device(args.name, path=args.devices)
    except DevicesError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1
    if not removed:
        print(f'No such device: {args.name}', file=sys.stderr)
        return 1
    print(f'Removed device {args.name}.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='parental-controls devices',
        description='Manage hub -> managed-box registrations '
                    '(root-only; writes devices.json 0600).')
    parser.add_argument('--devices', default=None,
                        help='override devices.json path (tests/ops)')
    sub = parser.add_subparsers(dest='cmd', required=True)
    add = sub.add_parser('add', help='register a device')
    add.add_argument('name')
    add.add_argument('--url', required=True)
    add.add_argument('--token', required=True)
    add.add_argument('--hostname', default='')
    add.add_argument('--insecure-lan', action='store_true')
    add.set_defaults(func=_cmd_device_add)
    lst = sub.add_parser('list', help='list registered devices')
    lst.set_defaults(func=_cmd_device_list)
    rm = sub.add_parser('remove', help='remove a device')
    rm.add_argument('name')
    rm.set_defaults(func=_cmd_device_remove)
    args = parser.parse_args(argv)
    if os.geteuid() != 0 and not args.devices:
        print('WARNING: not running as root; writing /etc/parental-controls '
              'may fail. Use sudo.', file=sys.stderr)
    try:
        return args.func(args)
    except DevicesError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
