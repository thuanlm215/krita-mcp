"""Native recovery dialog regressions; run with the isolated recovery fixture.

python scripts/run_isolated_tests.py --output artifacts/recovery \
  --recovery-fixture artifacts/implementation/completion/desktop-warp.kra -- \
  python -m unittest -v tests.test_recovery
"""
import hashlib
import json
import os
from pathlib import Path
import time
import unittest

from tests.test_mcp import Client, PROTOCOL
from tests.fixture_client import fixture


class RecoveryDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_home = Path.home()
        if not (os.environ.get("KRITA_MCP_TEST_ARTIFACTS") and test_home.name == "home"
                and test_home.parent.name.startswith("krita-mcp-test-")):
            raise RuntimeError("Use run_isolated_tests.py --recovery-fixture; desktop calls are forbidden")
        cls.client = Client()
        cls.client.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                         "clientInfo": {"name": "native-recovery-tests", "version": "1"}})
        cls.client.notify("notifications/initialized")
        cls.receipt = {}

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        if os.environ.get("KRITA_MCP_TEST_ARTIFACTS"):
            (Path(os.environ["KRITA_MCP_TEST_ARTIFACTS"]) / "recovery.json").write_text(
                json.dumps(cls.receipt, indent=2) + "\n")

    def modal(self):
        return fixture('modal')

    def test_00_cancel_real_native_recovery_keeps_every_autosave(self):
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in Path.home().glob("*mcp-recovery-*-autosave.kra")}
        self.assertEqual(len(files), 2, "Use --recovery-fixture; never run this on desktop")
        for _ in range(100):
            if self.modal() == "KisAutoSaveRecoveryDialog":
                break
            time.sleep(.1)
        self.assertEqual(self.modal(), "KisAutoSaveRecoveryDialog")
        before, _ = self.client.ok("status")
        self.assertEqual(before["open_document_count"], 0)
        schemas = self.client.request("tools/list")["result"]["tools"]
        self.assertEqual(len(schemas), len(__import__('mcp_server').TOOLS))
        self.assertIn("dismiss_recovery_dialog", {t["name"] for t in schemas})
        result, _ = self.client.ok("dismiss_recovery_dialog")
        self.assertTrue(result["dismissed"])
        self.assertEqual(result["action"], "cancel")
        self.assertEqual(result["dialog_class"], "KisAutoSaveRecoveryDialog")
        for _ in range(100):
            if self.modal() is None:
                break
            time.sleep(.1)
        self.assertIsNone(self.modal())
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in Path.home().glob("*mcp-recovery-*-autosave.kra")}
        self.assertEqual(files, after)
        self.assertEqual(self.client.ok("status")[0]["open_document_count"], 0)
        self.receipt["native_cancel"] = {"result": result, "before": files, "after": after,
                                          "documents_opened": 0, "modal_after": None}

    def test_01_unrelated_modal_even_with_same_title_is_left_open(self):
        fixture('modal_create')
        try:
            response = self.client.call("dismiss_recovery_dialog")
            self.assertTrue(response.get("isError"), response)
            self.assertIn("unsupported_dialog", str(response))
            self.assertTrue(fixture('modal_visible'))
            self.receipt["other_modal"] = {"response": response, "kept_open": True}
        finally:
            fixture('modal_close')

    def test_02_no_dialog_is_idempotent(self):
        self.assertIsNone(self.modal())
        result, _ = self.client.ok("dismiss_recovery_dialog")
        self.assertFalse(result["dismissed"])
        self.assertEqual(result["reason"], "no_modal_dialog")
        self.receipt["no_dialog"] = result


if __name__ == "__main__":
    unittest.main(verbosity=2)
