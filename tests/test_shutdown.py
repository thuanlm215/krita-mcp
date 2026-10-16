#!/usr/bin/env python3
"""Exercise the plugin lifecycle with real Qt actions and a stub bridge."""
import ast
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6 import QtCore, QtWidgets, sip

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


class KisNodeModel(QtCore.QObject):
    """A real Qt slot and queued signal stand in for Krita's layer model."""
    changed = QtCore.pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.pending = False
        self.changed.connect(self.enqueue, QtCore.Qt.ConnectionType.QueuedConnection)

    @QtCore.pyqtSlot()
    def enqueue(self):
        self.pending = True

    @QtCore.pyqtSlot()
    def processUpdateQueue(self):
        self.pending = False
        self.flushed = True


class CloseFlushTests(unittest.TestCase):
    def test_release_suffix_still_drains_posted_model_updates_without_helper(self):
        # Execute the production function with actual Qt event delivery, without
        # importing Krita or loading a native helper into the offline process.
        source = ROOT / 'pykrita/krita_mcp/ops.py'
        tree = ast.parse(source.read_text())
        definition = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                          and n.name == '_flush_before_close')
        compat = ModuleType('close_test.compat')
        compat.QtCore = QtCore
        compat.QObject = QtCore.QObject
        compat.QtCompat = SimpleNamespace(
            DirectConnection=QtCore.Qt.ConnectionType.DirectConnection)
        for version, should_flush in (('6.0.4', True), ('6.0.4 (git 57db7cd)', True),
                                     ('6.0.40', False), ('6.1.0', False),
                                     ('6.0.4-beta1', False)):
            with self.subTest(version=version):
                window = QtCore.QObject()
                model = KisNodeModel(window)
                model.flushed = False
                model.changed.emit()
                calls = []
                krita = SimpleNamespace(version=lambda: version, windows=lambda: [
                    SimpleNamespace(views=lambda: [object()], qwindow=lambda: window)])
                namespace = {'__package__': 'close_test', '_krita': lambda: krita,
                             '_trace': lambda text: None}
                with patch.dict(sys.modules, {'close_test.compat': compat}):
                    exec(compile(ast.Module(body=[definition], type_ignores=[]),
                                 str(source), 'exec'), namespace)
                    doc = SimpleNamespace(waitForDone=lambda: calls.append('idle'))
                    namespace['_flush_before_close'](doc)
                self.assertEqual(model.flushed, should_flush)
                if should_flush:
                    self.assertFalse(model.pending)
                    self.assertEqual(calls, ['idle'])
                sip.delete(window)


if __name__ == '__main__':
    unittest.main()
