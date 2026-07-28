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


if __name__ == "__main__":
    unittest.main()
