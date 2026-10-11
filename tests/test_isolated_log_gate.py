"""The isolated gate must reject successful tests with native assertions."""
from pathlib import Path
import tempfile
import unittest
from scripts.run_isolated_tests import native_diagnostics


class LogGateTests(unittest.TestCase):
    def test_native_assertions_and_crashes_are_reported_with_location(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)
            (out/'krita.log').write_text('ordinary startup\nASSERT: "row >= 0"\nSegmentation fault\n')
            (out/'native-krita.log').write_text('SAFE ASSERT (krita): "!newNode->parent()"\n')
            results=native_diagnostics(out)
            self.assertEqual(len(results),3)
            self.assertEqual([(r['file'],r['line']) for r in results],
                             [('krita.log',2),('krita.log',3),('native-krita.log',1)])

    def test_unrelated_dbus_startup_warnings_are_not_native_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)
            (out/'krita.log').write_text('dbus: service unavailable\nPipeWire: could not connect\n')
            self.assertEqual(native_diagnostics(out),[])
