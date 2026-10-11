#!/usr/bin/env python3
"""Exercise the plugin lifecycle with real Qt actions and a stub bridge."""
import ast
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6 import QtWidgets, sip

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'pykrita/krita_mcp/extension.py'


class ExtensionStub:
    def __init__(self, parent):
        pass


# Load the actual extension class without importing Krita's embedded-only API.
tree = ast.parse(SOURCE.read_text())
definition = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                  and n.name == 'KritaMcpExtension')
namespace = {'Extension': ExtensionStub}
exec(compile(ast.Module(body=[definition], type_ignores=[]), str(SOURCE), 'exec'), namespace)
Extension = namespace['KritaMcpExtension']
APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.extension = Extension(None)
        self.stops = 0
        self.action = QtWidgets.QWidgetAction(None)
        self.extension._toggle_action = self.action
        self.extension._bridge = SimpleNamespace(running=True, port=9797, stop=self.stop)

    def stop(self):
        self.stops += 1

    def tearDown(self):
        if not sip.isdeleted(self.action):
            sip.delete(self.action)

    def test_normal_stop_updates_menu(self):
        self.extension._stop()
        self.assertEqual(self.action.text(), 'Start MCP Bridge')
        self.assertEqual(self.stops, 1)

    def test_shutdown_after_qt_deletes_action(self):
        sip.delete(self.action)
        self.extension._on_closing()
        self.extension._on_closing()
        self.assertIsNone(self.extension._bridge)
        self.assertEqual(self.stops, 1)

    def test_shutdown_does_not_touch_a_live_menu(self):
        self.action.setText('Closing')
        self.extension._on_closing()
        self.assertEqual(self.action.text(), 'Closing')
        self.assertEqual(self.stops, 1)

    def test_refresh_after_window_deletes_action(self):
        sip.delete(self.action)
        self.extension._refresh_action_text()
        self.assertIsNone(self.extension._toggle_action)

    def test_unrelated_runtime_error_is_not_hidden(self):
        def fail(text):
            raise RuntimeError('unexpected failure')
        self.extension._toggle_action = SimpleNamespace(setText=fail)
        with self.assertRaisesRegex(RuntimeError, 'unexpected failure'):
            self.extension._refresh_action_text()


if __name__ == '__main__':
    unittest.main()
