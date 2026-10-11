#!/usr/bin/env python3
"""TLS WSGI entrypoint for LAN binds (cheroot + stdlib ssl).

Production use only, via the systemd unit rendered by `install.sh --tls`.
Loopback serving stays on waitress (wsgi.py); this module holds no app
logic — it wraps the fail-closed `wsgi:application` in a TLS-capable
server. The bind happens inside main() so importing this module never
listens on any port.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cheroot.wsgi import Server as WSGIServer  # noqa: E402
from cheroot.ssl.builtin import BuiltinSSLAdapter  # noqa: E402

from wsgi import application  # noqa: E402  (fail-closed auth init on import)


def build_server(host, port, cert, key):
    """A cheroot WSGI server terminating TLS with `cert`/`key`."""
    server = WSGIServer((host, port), application)
    server.ssl_adapter = BuiltinSSLAdapter(cert, key)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='TLS WSGI server for LAN binds')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--cert', required=True)
    parser.add_argument('--key', required=True)
    args = parser.parse_args(argv)
    server = build_server(args.host, args.port, args.cert, args.key)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()


if __name__ == '__main__':
    main()
