#!/usr/bin/env python3
"""Plugin scanner — discovers plugins from manifest.json files and derives
their status from PID files + config existence. Source-pinned against the
kernel (importing it runs boot reconcile — tests/README)."""
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)


class ScannerSourcePin(unittest.TestCase):
    """The kernel must contain the scanner + status functions."""
    def setUp(self):
        self.src = open(os.path.join(ROOT, "kernel", "kernel.py")).read()

    def test_scan_plugins_present(self):
        self.assertIn("def _scan_plugins(", self.src)

    def test_plugin_status_present(self):
        self.assertIn("def _plugin_status(", self.src)

    def test_plugin_post_route(self):
        self.assertIn('u.path == "/plugin"', self.src)

    def test_plugins_page_route(self):
        self.assertIn('p == "/plugins"', self.src)
        self.assertIn("_plugins_page()", self.src)


class ManifestShape(unittest.TestCase):
    """The moonlight manifest exists and has the required fields."""
    def test_manifest_valid(self):
        with open(os.path.join(ROOT, "plugins", "moonlight", "manifest.json")) as f:
            m = json.load(f)
        for key in ("name", "description", "entry", "pidFile"):
            self.assertIn(key, m, "manifest missing %s" % key)
        self.assertEqual(m["name"], "moonlight")


class PluginRouteShape(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(ROOT, "kernel", "kernel.py")).read()

    def test_start_action(self):
        i = self.src.find('u.path == "/plugin"')
        self.assertGreater(i, -1)
        block = self.src[i:i + 3000]
        self.assertIn('"start"', block)
        self.assertIn('"stop"', block)
        self.assertIn('SIGTERM', block)
        self.assertIn('"check-deps"', block)
        self.assertIn('"light-led"', block)


class PaneIntegration(unittest.TestCase):
    """The plugins pane must be wired into the landing shell (rail button, pane
    div, collapse-state machine) and its own static-file route, the same way the
    other panes (chat/fleet/feed/timeline) are."""
    def setUp(self):
        self.src = open(os.path.join(ROOT, "kernel", "kernel.py")).read()

    def test_landing_has_plugins_pane(self):
        self.assertIn("plugins-pane", self.src)
        self.assertIn("data-pane=plugins", self.src)

    def test_collapse_js_knows_plugins(self):
        i = self.src.find("var PK='romp-panes'")
        self.assertGreater(i, -1)
        block = self.src[i:i + 500]
        self.assertIn("plugins", block)

    def test_plugins_static_route(self):
        self.assertIn('p.startswith("/plugins/")', self.src)


class ShimMessageHook(unittest.TestCase):
    """The shim must expose window.__rompOnMessage so plain inline-JS pages (no
    bundle, like the plugins pane) can register a callback for every parsed WS
    message — not just the federation/postMessage path chat/fleet/feed/timeline
    ride."""
    def setUp(self):
        self.src = open(os.path.join(ROOT, "kernel", "kernel.py")).read()

    def test_shim_exposes_rompOnMessage(self):
        self.assertIn("__rompOnMessage", self.src)

    def test_shim_dispatches_to_registered_callbacks(self):
        i = self.src.find("def _shim(")
        self.assertGreater(i, -1)
        block = self.src[i:i + 6000]
        self.assertIn("_msgCbs", block)


if __name__ == "__main__":
    unittest.main()
