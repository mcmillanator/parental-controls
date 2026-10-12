#!/usr/bin/env python3
"""Phase-1 multi-device: live two-box tests.

HUMAN RUNS ON REAL BOXES. These are skipped by default and only run when
both ``MULTIDEVICE_LIVE_URL`` and ``MULTIDEVICE_LIVE_TOKEN`` are set to a
real managed box's agent (typically over an SSH tunnel). They are strictly
read-only: no lock/unlock/freeze/terminate/notify is ever issued, so they
cannot disturb a live session.

Example (human, on the hub box, with the tunnel up):

    MULTIDEVICE_LIVE_URL=http://localhost:5001 \
    MULTIDEVICE_LIVE_TOKEN="$(sudo cat /path/to/agent_token)" \
    .venv/bin/python -m unittest tests.test_multidevice_live -v
"""

import os
import unittest

import remote

LIVE_URL = os.environ.get('MULTIDEVICE_LIVE_URL', '')
LIVE_TOKEN = os.environ.get('MULTIDEVICE_LIVE_TOKEN', '')


@unittest.skipUnless(LIVE_URL and LIVE_TOKEN,
                     'human runs on real boxes: set MULTIDEVICE_LIVE_URL and '
                     'MULTIDEVICE_LIVE_TOKEN (read-only probe)')
class TestLiveTwoBox(unittest.TestCase):
    """Read-only reachability probe against a real agent (skipped by default)."""

    def test_health_reachable(self):
        client = remote.RemoteClient('live', LIVE_URL, LIVE_TOKEN)
        status, payload = client.get('/health')
        self.assertEqual(status, 200)
        self.assertTrue(payload['success'])
        self.assertTrue(payload.get('box_id'))

    def test_users_readable(self):
        client = remote.RemoteClient('live', LIVE_URL, LIVE_TOKEN)
        status, payload = client.get('/users')
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, list)

    def test_wrong_token_is_401(self):
        client = remote.RemoteClient('live', LIVE_URL, 'definitely-wrong')
        status, payload = client.get('/users')
        self.assertEqual(status, 401)

    def test_audit_chain_reported(self):
        client = remote.RemoteClient('live', LIVE_URL, LIVE_TOKEN)
        status, payload = client.get('/audit', {'limit': '1'})
        self.assertEqual(status, 200)
        self.assertIn('chain_ok', payload)


if __name__ == '__main__':
    unittest.main()
