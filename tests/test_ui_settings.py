#!/usr/bin/env python3
"""Settings screen tests (design-exports/SPEC.md Screen 6).

Server-render checks for ``/settings``: the status stack, the notifications /
local-access fieldsets, the device cards and the annotation sections. Anything
the backend does not own yet is asserted to be a disabled/placeholder state.

Mocks only: temp auth store, mocked device list.
"""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import app
from test_lock_regression import _AdminSessionMixin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, 'templates', 'settings.html')
EXTERNAL_ASSET = re.compile(r'''(?:href|src)\s*=\s*["']https?://''', re.I)


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class SettingsRenderTest(_AdminSessionMixin, unittest.TestCase):
    def render(self, devices=None, query=''):
        with mock.patch.object(app, '_device_entries',
                               return_value=devices if devices is not None else []):
            resp = self.client.get('/settings' + query)
        self.assertEqual(200, resp.status_code)
        return resp.get_data(as_text=True)

    def test_header_and_save(self):
        html = self.render()
        self.assertIn('>Settings</h1>', html)
        self.assertIn('Manage notifications, local access, and connected devices.', html)
        self.assertIn('Save settings', html)

    def test_active_section_is_settings(self):
        html = self.render()
        sidebar = html[html.index('class="pc-sidebar"'):]
        sidebar = sidebar[:sidebar.index('</aside>')]
        self.assertIn('Settings', sidebar)
        self.assertIn('aria-current="page"', sidebar)

    def test_status_stack(self):
        html = self.render()
        self.assertIn('Last sync 4 min ago', html)
        self.assertIn('Offline — lock controls require a fresh connection', html)
        self.assertIn('Unavailable', html)
        self.assertIn('Could not reach local service', html)
        self.assertIn('role="alert"', html)
        self.assertIn('aria-live="assertive"', html)
        self.assertIn('#pc-i-lock-keyhole', html)
        self.assertIn('#pc-i-triangle-alert', html)

    def test_notifications_fieldset(self):
        html = self.render()
        self.assertIn('id="settings-notifications"', html)
        self.assertIn("Send pre-lock warnings to child's desktop", html)
        self.assertIn('id="prelock-warning" disabled', html)
        self.assertIn('id="warning-leadtime"', html)
        self.assertIn('You have 15 minutes before bedtime', html)
        self.assertIn('Test notification', html)
        self.assertIn('testNotification', read(TEMPLATE))

    def test_local_access_fieldset_is_scheme_less(self):
        html = self.render()
        self.assertIn('id="settings-local-access"', html)
        self.assertIn('id="local-url"', html)
        self.assertIn('Optional HTTPS enabled', html)
        self.assertIn('id="cert-status"', html)
        # offline promise: no scheme literal anywhere in the page
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)

    def test_devices_cards(self):
        html = self.render()
        self.assertIn('id="current-device"', html)
        self.assertIn('Fedora Atomic · Bazzite', html)
        self.assertIn('Last heartbeat just now', html)
        self.assertIn('Other devices', html)
        self.assertIn('Multi-device management is planned.', html)
        self.assertIn('Add device', html)

    def test_devices_empty_state(self):
        html = self.render()
        self.assertIn('No devices connected', html)
        self.assertIn('Check parental.service.', html)
        self.assertIn('#pc-i-monitor-off', html)

    def test_registered_devices_render(self):
        html = self.render(devices=[{'name': 'livingroom', 'hostname': 'lr.local'}])
        self.assertIn('data-device="livingroom"', html)
        self.assertIn('lr.local', html)
        self.assertNotIn('No devices connected', html)

    def test_annotation_sections(self):
        # Developer guidance only: dev-only preview, never the product page.
        self.assertNotIn('id="settings-annotations"', self.render())
        html = self.render(query='?preview=1')
        self.assertIn('id="settings-annotations"', html)
        self.assertIn('Flask + Jinja + vanilla JavaScript + Bootstrap 5', html)
        self.assertIn('Responsive Bootstrap pattern', html)
        self.assertIn('Accessibility implementation', html)
        self.assertIn('col-12 col-xl-6', html)

    def test_no_external_assets(self):
        html = self.render()
        self.assertIsNone(EXTERNAL_ASSET.search(html))


class SettingsSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read(TEMPLATE)

    def test_extends_the_shell(self):
        self.assertTrue(self.src.lstrip().startswith('{% extends "base.html" %}'))

    def test_semantic_fieldsets_and_legends(self):
        self.assertIn('<fieldset', self.src)
        self.assertIn('<legend', self.src)

    def test_placeholders_are_disabled(self):
        # The not-yet-backed features render disabled, not invented. The
        # toggles now go through the shared pc_toggle macro
        # (templates/_partials/macros.html), so the source asserts the macro
        # call + the rendered page asserts the disabled attribute.
        self.assertIn("pc_toggle('prelock-warning'", self.src)
        self.assertIn("pc_toggle('https-enabled'", self.src)
        self.assertIn('disabled=true', self.src)


if __name__ == '__main__':
    unittest.main()
