"""Owned libkis/SIP wrapper lifetime at the Python-to-native boundary."""
import importlib
from pathlib import Path
import sys
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from PyQt6.QtCore import QObject
from PyQt6 import sip

package=ModuleType('krita_native_lifetime')
package.__path__=[str(Path(__file__).resolve().parents[1]/'pykrita/krita_mcp')]
sys.modules[package.__name__]=package
importlib.import_module(package.__name__+'.ops')
native=importlib.import_module(package.__name__+'.native_commands')


class Document:
    def rootNode(self):
        node=QObject()  # Like libkis, return a fresh, caller-owned wrapper.
        self.last=weakref.ref(node)
        return node

    def waitForDone(self):pass


class NativeLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.doc=Document();self.window=QObject()
        self.app=SimpleNamespace(activeWindow=lambda:SimpleNamespace(qwindow=lambda:self.window))

    def alive(self,*args):
        node=self.doc.last()
        self.assertIsNotNone(node,'Caller-owned wrapper was destroyed before C call')
        self.assertFalse(sip.isdeleted(node))
        return 4

    def test_logical_time_retains_wrapper_and_reproduces_old_pattern(self):
        lib=SimpleNamespace(mcp_ui_time=self.alive)
        with patch.object(native,'library',return_value=lib):
            # Deterministic ownership reproducer; never dereference a freed
            # pointer in C or intentionally crash the running application.
            with self.assertRaisesRegex(AssertionError,'destroyed before C call'):
                lib.mcp_ui_time(native.pointer(self.doc.rootNode()))
            self.assertEqual(native.get_time(self.doc),4)

    def test_navigation_retains_wrapper(self):
        lib=SimpleNamespace(mcp_time=self.alive,mcp_ui_time=self.alive,mcp_flush_events=lambda:None)
        with patch.object(native,'library',return_value=lib),patch.object(native.b,'_krita',return_value=self.app),\
             patch.object(native.b,'_refresh',return_value=None):
            native.set_time(self.doc,4)

    def test_metadata_push_retains_wrapper(self):
        lib=SimpleNamespace(mcp_push_state=self.alive)
        keys=set(native.STATE['callbacks'])
        try:
            with patch.object(native,'stack',return_value=(lib,1)),patch.object(native.b,'_krita',return_value=self.app),\
                 patch.object(native.b,'_refresh',return_value=None):
                native.push(self.doc,lambda doc,state:None,0,1,'Ownership regression')
        finally:
            for key in set(native.STATE['callbacks'])-keys:native.STATE['callbacks'][key].dispose()


if __name__=='__main__':unittest.main(verbosity=2)
