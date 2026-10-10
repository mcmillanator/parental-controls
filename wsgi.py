#!/usr/bin/env python3
"""Production WSGI entrypoint (waitress). Fail-closed auth init on import."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
from app import app  # noqa: E402

try:
    auth.ensure_hash_backend()
    auth.init_store()
except auth.AuthStoreError as e:
    print(f'FATAL: {e}', flush=True)
    raise SystemExit(1)

application = app

if __name__ == '__main__':
    from waitress import serve
    serve(application, host='127.0.0.1', port=5000)
