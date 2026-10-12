#!/usr/bin/env python3
"""Production WSGI entrypoint for the parental-controls agent (managed box).

Fail-closed on import: with no agent token provisioned there is nothing to
authenticate, so the service refuses to start rather than exposing an
unauthenticated privileged surface. The agent never imports the panel app
or the session machinery.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402

if agent.load_token() is None:
    print(f'FATAL: agent token missing ({agent.token_path()}); provision it '
          'with `install.sh --enable-agent`. Refusing to start.',
          flush=True)
    raise SystemExit(1)

application = agent.application

if __name__ == '__main__':
    from waitress import serve
    serve(application, host='127.0.0.1', port=5001)
