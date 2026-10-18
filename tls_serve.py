#!/usr/bin/env python3
"""TLS WSGI entrypoint for LAN binds (cheroot + stdlib ssl).

Production use only, via the systemd unit rendered by `install.sh --tls`.
Loopback serving stays on waitress (wsgi.py); this module holds no app
logic — it wraps the fail-closed `wsgi:application` in a TLS-capable
server. With `--http-port` (rendered only by `install.sh
--deploy-webserver`) it also runs a plain-HTTP listener on that port that
answers EVERY request with a 301 to the same URL over https. The binds
happen inside main()/start_redirect_server(), so importing this module
never listens on any port.
"""

import argparse
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cheroot.wsgi import Server as WSGIServer  # noqa: E402
from cheroot.ssl.builtin import BuiltinSSLAdapter  # noqa: E402

from wsgi import application  # noqa: E402  (fail-closed auth init on import)


def build_server(host, port, cert, key):
    """A cheroot WSGI server terminating TLS with `cert`/`key`."""
    server = WSGIServer((host, port), application)
    server.ssl_adapter = BuiltinSSLAdapter(cert, key)
    return server


# ---------------------------------------------------------------------------
# Plain-HTTP -> HTTPS redirector (--http-port; install.sh --deploy-webserver)
# ---------------------------------------------------------------------------
# RFC-3986-shaped authority: a dot-separated hostname/IPv4, or a bracketed
# IPv6 literal, with an optional port. Anything else (schemes, credentials,
# path characters, control-character leftovers of malformed requests) is
# rejected, so a hostile Host header can never smuggle text into the
# Location authority.
_AUTHORITY_RE = re.compile(
    r'^(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252}'
    r'[A-Za-z0-9])?)(?::(?P<port>[0-9]{1,5}))?$')


def _sanitize_authority(value):
    """Validate a Host-header/tls-name value; '' when it is unusable.

    Default ports (80/443) are stripped so the https Location never
    carries them; an explicit non-default port is preserved.
    """
    if not value:
        return ''
    value = value.strip()
    if len(value) > 253:
        return ''
    match = _AUTHORITY_RE.match(value)
    if not match:
        return ''
    host = match.group('host')
    port = match.group('port')
    if port is None or port in ('80', '443'):
        return host
    if not 1 <= int(port) <= 65535:
        return ''
    return host + ':' + port


def _sanitize_target(target):
    """The request target as a safe absolute path(+query).

    Backslashes become slashes and repeated leading slashes collapse, so
    `//evil.com` can never be read as an authority by the client; a
    scheme-prefixed (abs-URI/proxied) target degrades to a plain path.
    Control characters are dropped; OPTIONS `*` maps to `/`.
    """
    path = target or '/'
    path = ''.join(ch for ch in path if ch >= ' ' and ch != '\x7f')
    if path == '*':
        path = '/'
    path = path.replace('\\', '/')
    if not path.startswith('/'):
        path = '/' + path
    return re.sub(r'^/+', '/', path)


def redirect_location(target, host_header=None, tls_name=''):
    """The https Location for one redirector request.

    Authority = the sanitized request Host header, falling back to the
    server's --tls-name; when neither is usable the Location is the bare
    (relative) path — the simplest safe option, because fabricating an
    authority for a malformed request is what header-smuggling attacks
    probe for. Path and query string are preserved.
    """
    path = _sanitize_target(target)
    authority = (_sanitize_authority(host_header)
                 or _sanitize_authority(tls_name))
    if not authority:
        return path
    return 'https://' + authority + path


class RedirectRequestHandler(BaseHTTPRequestHandler):
    """Answers EVERY request with a 301 to the same URL over https.

    Deliberately tiny: it never proxies, never touches the WSGI app, and
    never logs (a port-80 listener on an internet-facing box is scanner
    bait; the cheroot TLS listener keeps the real access logging).
    """

    protocol_version = 'HTTP/1.1'
    server_version = 'ParentalControlsRedirect'
    sys_version = ''

    def _redirect(self):
        location = redirect_location(
            self.path, self.headers.get('Host'),
            getattr(self.server, 'tls_name', ''))
        self.close_connection = True
        self.send_response(301)
        self.send_header('Location', location)
        self.send_header('Connection', 'close')
        self.send_header('Content-Length', '0')
        self.end_headers()

    do_GET = _redirect
    do_HEAD = _redirect
    do_POST = _redirect
    do_PUT = _redirect
    do_DELETE = _redirect
    do_OPTIONS = _redirect
    do_PATCH = _redirect
    do_TRACE = _redirect

    def log_message(self, fmt, *args):
        pass  # silent by design; see the class docstring


def start_redirect_server(host, port, tls_name=''):
    """The plain-HTTP 301 redirector on (host, port), served in a daemon
    thread. Returns the ThreadingHTTPServer (its server_address carries the
    bound port when 0 was requested) for shutdown()/server_close().

    `port <= 0` is OFF — install.sh renders --http-port only in webserver
    mode, so default installs keep the exact pre-`--deploy-webserver`
    behavior. main() guards the flag; passing 0 here directly binds an
    ephemeral port (how the tests exercise it without touching 80/443).
    """
    httpd = ThreadingHTTPServer((host, port), RedirectRequestHandler)
    httpd.tls_name = tls_name
    threading.Thread(target=httpd.serve_forever,
                     name='parental-http-redirect', daemon=True).start()
    return httpd


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='TLS WSGI server for LAN binds')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--cert', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--http-port', type=int, default=0,
                        help='also serve plain HTTP on this port, answering '
                             'every request with a 301 to the https URL '
                             '(0 = off; rendered by install.sh '
                             '--deploy-webserver)')
    parser.add_argument('--tls-name', default='',
                        help='fallback redirect authority when a request '
                             'carries no usable Host header')
    args = parser.parse_args(argv)
    server = build_server(args.host, args.port, args.cert, args.key)
    redirector = None
    if args.http_port > 0:
        redirector = start_redirect_server(args.host, args.http_port,
                                           args.tls_name)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
    finally:
        if redirector is not None:
            redirector.shutdown()
            redirector.server_close()


if __name__ == '__main__':
    main()
