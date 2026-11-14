#!/usr/bin/env python3
"""Live payload parity: panel /api/user/<u>/live vs agent read_user_live.

agent.py's read_user_live mirrors the panel's live endpoint payload — the
DIVERGENT pair AGENT_CONTEXT.md documents as left alone. The user page's
10s poll now drives the manual-override card's DOM add/remove straight
from payload fields (in_bedtime, manual_override, bedtime_end), so a key
added on ONE side silently breaks the remote-device page (the hub proxy
forwards the agent payload verbatim). This pins the shared key surface
exactly: adding a field to either payload without the other fails here —
update BOTH sides, then (deliberately) this list.

Sanctioned divergences:
* panel-only `countdown` — the same accepted degradation as the Overview
  (tests/test_ui_overview_parity.py AGENT_ACCEPTED_DEGRADATIONS); remote
  countdown re-seeding degrades to the server-painted value.
* agent-only `uid`/`home`/`shell`/`bedtime`/`processes`/`visible`/
  `app_names` — context extras the hub re-sanitizes through
  app._remote_live_context for the first paint.

Everything is mocked (readers + one forced process-listing error); no
live account, service, /etc, /opt or checkout is touched.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import agent
import app
from test_lock_regression import _AdminSessionMixin, KID
from test_ui_user import live_dict

PANEL_ONLY_KEYS = frozenset({'countdown'})
AGENT_ONLY_KEYS = frozenset({'uid', 'home', 'shell', 'bedtime',
                             'processes', 'visible', 'app_names'})

# Fields the override-card sync (user.html syncOverrideCard) reads from
# the payload — they MUST ship on both sides, local and remote.
OVERRIDE_CARD_FIELDS = ('in_bedtime', 'manual_override', 'bedtime_end')


def agent_live_payload():
    """Agent-side read_user_live payload, readers mocked, ps forced to fail."""
    entry = {'start': '21:00', 'end': '07:00', 'days': [0, 1, 2, 3, 4],
             'manual_override': True}
    users = [{'username': KID, 'uid': '1001', 'home': '/home/kid',
              'shell': '/bin/bash'}]
    with mock.patch.object(agent, 'get_human_users', return_value=users), \
         mock.patch.object(agent, 'status_row',
                           return_value=(False, 'Unlocked', True)), \
         mock.patch.object(agent, 'load_bedtime', return_value={KID: entry}), \
         mock.patch.object(agent, '_schedule_map', return_value={}), \
         mock.patch.object(agent.budgets, 'list_user_processes',
                           side_effect=RuntimeError('no ps in tests')), \
         mock.patch.object(agent.budgets, 'load_overrides', return_value={}), \
         mock.patch.object(agent.budgets, 'budget_status', return_value={}), \
         mock.patch.object(agent.budgets, 'overrides_view',
                           return_value={'app_overrides': {},
                                         'windows': []}):
        payload = agent.read_user_live(KID)
    assert payload is not None
    return payload


class LivePayloadParityTest(_AdminSessionMixin, unittest.TestCase):
    """Key-surface parity between the two live payloads."""

    def panel_live_payload(self):
        live = live_dict()
        with mock.patch.object(app, 'get_user_live', return_value=live):
            resp = self.client.get('/api/user/' + KID + '/live')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        return payload

    def test_panel_only_keys_are_exactly_the_sanctioned_divergence(self):
        panel = set(self.panel_live_payload())
        agent_keys = set(agent_live_payload())
        self.assertEqual(panel - agent_keys, PANEL_ONLY_KEYS,
                         'panel-only live keys drifted — update '
                         'PANEL_ONLY_KEYS only with a mirroring decision')

    def test_agent_only_keys_are_exactly_the_context_extras(self):
        panel = set(self.panel_live_payload())
        agent_keys = set(agent_live_payload())
        self.assertEqual(agent_keys - panel, AGENT_ONLY_KEYS,
                         'agent-only live keys drifted — update '
                         'AGENT_ONLY_KEYS only with a mirroring decision')

    def test_override_card_fields_ship_on_both_sides(self):
        panel = self.panel_live_payload()
        agent_payload = agent_live_payload()
        for field in OVERRIDE_CARD_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, panel,
                              'user.html syncOverrideCard reads this key '
                              'on the local page')
                self.assertIn(field, agent_payload,
                              'the same page polls a REMOTE box through '
                              'the hub proxy with this payload')

    def test_bedtime_end_is_the_window_end_on_both_sides(self):
        # Display-only 'HH:MM' (24h storage; fmt12/twelve mirror client-
        # and server-side). The fixture and the agent entry both end 07:00.
        self.assertEqual(self.panel_live_payload()['bedtime_end'], '07:00')
        self.assertEqual(agent_live_payload()['bedtime_end'], '07:00')


if __name__ == '__main__':
    unittest.main()
