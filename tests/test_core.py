"""Offline codec, PNG response and transport regressions; PyQt6/Pillow, no Krita."""
import base64
import http.client
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import mcp_server
from tests.preview_payload import preview_content
from tests.test_advanced import png

# Import actual plugin modules without executing its Krita registration hook.
package = ModuleType("krita_test_plugin")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "pykrita/krita_mcp")]
sys.modules[package.__name__] = package
codec = importlib.import_module(package.__name__ + ".filter_parameters")
bridge_http = importlib.import_module(package.__name__ + ".httpserver")


class CodecTests(unittest.TestCase):
    def test_qt_defaults_roundtrip_with_precision_and_binary_nulls(self):
        values = [codec.QColor.fromRgbF(.1234, .4567, .7891, .3333),
                  codec.QByteArray(b"\0\xffnon-text\0"), True, 7, .123, "text", None,
                  [codec.QColor("red"), 1, False]]
        for default in values:
            encoded, info = codec.describe(default)
            self.assertTrue(info["writable"])
            json.dumps(encoded, allow_nan=False)
            decoded = codec.decode(encoded, default)
            self.assertEqual(decoded, default)
            self.assertEqual(type(decoded), type(default))

    def test_malformed_typed_data_and_unsupported_values(self):
        for value in ({"type": "QColor", "rgba": [True, 0, 0, 1]},
                      {"type": "QColor", "rgba": [float("nan"), 0, 0, 1]},
                      {"type": "QColor", "rgba": [0, 0, 0]},
                      {"type": "QColor", "rgba": [0, 0, 0, 1], "extra": "ignored?"}):
            with self.assertRaises(ValueError):
                codec.decode(value, codec.QColor("red"))
        for value in ({"type": "QByteArray", "base64": "AA==", "byte_length": 2},
                      {"type": "QByteArray", "base64": "not base64"},
                      {"type": "QByteArray", "base64": base64.b64encode(b"a"*(codec.MAX_BINARY_BYTES+1)).decode()}):
            with self.assertRaises(ValueError):
                codec.decode(value, codec.QByteArray())
        for default in (object(), float("nan"), codec.QColor()):
            encoded, info = codec.describe(default)
            self.assertFalse(info["writable"])
            json.dumps(encoded, allow_nan=False)
            with self.assertRaises(ValueError):
                codec.decode(encoded, default)


class TransportTests(unittest.TestCase):
    def test_preview_recursively_keeps_png_bytes_out_of_text(self):
        encoded=png(2,2,[120,60,30,255])
        result={'png_base64':encoded,'hash':'frozen-hash','mapping':{'scale':1},
                'raw_layer':{'png_base64':encoded,'metrics':{'changed_pixels':4}},
                'composite':{'before_png_base64':encoded,'after_png_base64':encoded,
                             'nested':[{'png_base64':encoded,'name':'before'}]}}
        for tool in ('inspect_repair','compare_images','get_image'):
            needed={key:'x' for key in mcp_server.TOOLS_BY_NAME[tool]['_required']}
            with patch.object(mcp_server.BRIDGE,'call',return_value=result):
                blocks=mcp_server.call_tool(tool,needed)
            metadata,image,_=preview_content(blocks)
            self.assertEqual(image.size,(2,2))
            self.assertEqual(blocks[1]['data'],encoded)
            self.assertEqual(metadata['composite']['nested'],[{'name':'before'}])
            self.assertEqual(metadata['raw_layer']['metrics'],{'changed_pixels':4})
            self.assertEqual(result['png_base64'],encoded)

    def test_image_text_guard_preserves_typed_binary_filter_parameters(self):
        binary={'type':'QByteArray','base64':'AA==','byte_length':1}
        with patch.object(mcp_server.BRIDGE,'call',return_value={'parameter':binary,
            'diagnostics':[{'before_png_base64':png(1,1,[0,0,0,0]),'hash':'kept'}]}):
            blocks=mcp_server.call_tool('list_capabilities',{})
        self.assertEqual(json.loads(blocks[0]['text']),{'parameter':binary,'diagnostics':[{'hash':'kept'}]})

    def test_http_fallback_does_not_leak_result_and_keeps_serving(self):
        token = "secret-session-token-never-in-errors"
        bridge = SimpleNamespace(token=token, trace=SimpleNamespace(write=lambda text: None),
                                 timeout_for=lambda name: 1, log=lambda text: None)
        def run(name, params, timeout):
            return 200, {"ok": True, "result": {"secret": token, "bad": object()}} if name == "fault" else {
                "ok": True, "result": {"healthy": True}}
        bridge.run = run
        server = bridge_http._Server(("127.0.0.1", 0), bridge_http._Handler, bridge)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        client = mcp_server.BridgeClient()
        client._info = {"host": "127.0.0.1", "port": server.server_port, "token": token}
        try:
            with self.assertRaises(mcp_server.BridgeError) as caught:
                client.call("fault")
            error = caught.exception
            self.assertEqual(error.kind, "response_serialization_error")
            self.assertEqual(error.detail, {"operation": "fault", "exception": "TypeError"})
            self.assertNotIn(token, str(error) + json.dumps(error.detail))
            self.assertEqual(client.call("status"), {"healthy": True})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_incomplete_response_is_never_retried_as_an_edit(self):
        for fault in (http.client.RemoteDisconnected("closed"), http.client.IncompleteRead(b"partial", 100)):
            client = mcp_server.BridgeClient()
            client._info = {"host": "127.0.0.1", "port": 1, "token": "unused"}
            client._request = Mock(side_effect=fault)
            with self.assertRaises(mcp_server.BridgeError) as caught:
                client.call("draw_brush")
            self.assertEqual(caught.exception.kind, "incomplete_response")
            self.assertNotIn("Is Krita running", str(caught.exception))
            self.assertEqual(client._request.call_count, 1)


class ArgumentFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, payload, encoding="utf-8"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding=encoding) as handle:
            handle.write(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def rejected(self, tool, arguments):
        with patch.object(mcp_server.BRIDGE, "call") as call:
            with self.assertRaises(mcp_server.BridgeError) as caught:
                mcp_server.call_tool(tool, arguments)
        call.assert_not_called()
        return caught.exception

    def test_every_tool_takes_params_file_and_image_tools_take_save_path(self):
        for spec in mcp_server.TOOLS:
            schema = spec["inputSchema"]
            self.assertIn("params_file", schema["properties"], spec["name"])
            self.assertEqual(schema["required"], [], spec["name"])
            self.assertEqual("save_path" in schema["properties"], spec["_image"], spec["name"])
            for key in spec["_required"]:
                self.assertIn(key, schema["properties"], spec["name"])
                self.assertIn(key, spec["description"].rsplit("params_file:", 1)[-1])

    def test_params_file_merges_under_inline_arguments(self):
        commands = [{"type": "fill_rect", "x": 0, "y": 0, "w": 4, "h": 4}]
        path = self.write("draw.json", {"commands": commands, "layer": "File"},
                          encoding="utf-8-sig")
        with patch.object(mcp_server.BRIDGE, "call", return_value={"drawn": 1}) as call:
            mcp_server.call_tool("draw", {"params_file": path, "layer": "Inline"})
        call.assert_called_once_with("draw", {"commands": commands, "layer": "Inline"})

    def test_params_file_supplies_required_arguments_and_reaches_transforms(self):
        path = self.write("crop.json", {"action": "crop", "x": 1, "y": 2,
                                        "width": 3, "height": 4})
        with patch.object(mcp_server.BRIDGE, "call", return_value={}) as call:
            mcp_server.call_tool("transform_image", {"params_file": path})
        self.assertEqual(call.call_args[0][0], "crop_image")
        self.assertNotIn("action", call.call_args[0][1])

    def test_bad_params_files_are_rejected_before_krita(self):
        good = self.write("ok.json", {"commands": []})
        cases = {
            "absolute path": "ok.json",
            "must end with .json": self.write("args.txt", "{}"),
            "not valid JSON": self.write("broken.json", "{\"commands\": ["),
            "one JSON object": self.write("list.json", [1, 2]),
            "does not take bogus": self.write("bogus.json", {"commands": [], "bogus": 1}),
            "does not take params_file": self.write("nested.json", {"params_file": good}),
            "could not read": os.path.join(self.tmp.name, "missing.json"),
        }
        for fragment, path in cases.items():
            error = self.rejected("draw", {"params_file": path})
            self.assertEqual(error.kind, "invalid_request", fragment)
            self.assertIn(fragment, str(error))

    def test_missing_required_arguments_are_named(self):
        error = self.rejected("compare_images", {"after": "x"})
        self.assertEqual(error.kind, "invalid_request")
        self.assertIn("compare_images needs before", str(error))

    def test_save_path_writes_png_and_keeps_it_out_of_the_reply(self):
        encoded = png(3, 2, [10, 20, 30, 255])
        target = os.path.join(self.tmp.name, "nested", "shot.png")
        with patch.object(mcp_server.BRIDGE, "call",
                          return_value={"png_base64": encoded, "width": 3}) as call:
            blocks = mcp_server.call_tool("get_image", {"save_path": target})
        self.assertNotIn("save_path", call.call_args[0][1])
        self.assertEqual([block["type"] for block in blocks], ["text"])
        metadata = json.loads(blocks[0]["text"])
        self.assertEqual(metadata, {"width": 3, "saved_to": target})
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), base64.b64decode(encoded))

    def test_save_path_must_be_an_absolute_png(self):
        for path in ("shot.png", os.path.join(self.tmp.name, "shot.jpg")):
            error = self.rejected("get_image", {"save_path": path})
            self.assertEqual(error.kind, "invalid_request")


class AssetToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.ops = importlib.import_module(package.__name__ + ".ops")
        cls.painting = importlib.import_module(package.__name__ + ".operations.painting")
        cls.assets = importlib.import_module(package.__name__ + ".operations.assets")

    def test_new_tools_and_arguments_are_published(self):
        tools = mcp_server.TOOLS_BY_NAME
        self.assertTrue({"outline_layer", "export_layers"} <= set(tools))
        self.assertEqual(tools["draw_brush"]["_required"], [])
        self.assertIn("strokes", tools["draw_brush"]["inputSchema"]["properties"])
        self.assertEqual(tools["export_layers"]["_required"], ["directory"])
        self.assertTrue({"path", "scaling"} <= set(tools["create_layer"]["inputSchema"]["properties"]))
        command = tools["draw"]["inputSchema"]["properties"]["commands"]["items"]["properties"]
        self.assertTrue({"svg", "smooth", "rx", "ry", "angle", "path"} <= set(command))

    def test_changed_spans_match_a_per_pixel_scan(self):
        import random
        rng = random.Random(7)
        for _ in range(200):
            width, height = rng.randint(1, 24), rng.randint(1, 24)
            before = bytes(rng.randrange(256) for _ in range(width * height * 4))
            after = bytearray(before)
            for _ in range(rng.randint(0, 20)):
                i = rng.randrange(len(after))
                after[i] = (after[i] + rng.randint(1, 255)) % 256
            after = bytes(after)
            expected = [i // 4 for i in range(0, len(before), 4) if before[i:i + 4] != after[i:i + 4]]
            found = [i for start, end in self.painting._changed_spans(before, after, width)
                     for i in range(start, end) if before[i * 4:i * 4 + 4] != after[i * 4:i * 4 + 4]]
            self.assertEqual(found, expected)

    def render(self, commands, width=160, height=120):
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QImage, QPainter
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for index, command in enumerate(commands):
            self.ops._run_command(painter, self.ops._prepare(command, index), QRect(0, 0, width, height))
        painter.end()
        return image.convertToFormat(QImage.Format.Format_ARGB32)

    def test_translucent_ink_fills_its_overlaps_once(self):
        image = self.render([{"type": "ink", "color": [0, 0, 0, 128],
                              "points": [[10, 60, 2], [50, 20, 16], [100, 90, 16], [150, 60, 1]]}])
        alphas = [image.pixelColor(x, y).alpha() for x in range(160) for y in range(120)]
        self.assertEqual(max(alphas), 128)
        self.assertGreater(alphas.count(128), 800)
        for bad in ({"type": "ink", "points": [[0, 0]]}, {"type": "ink", "points": [[0, 0, -1], [5, 5]]},
                    {"type": "svg", "svg": "<nope"}, {"type": "svg"}):
            with self.assertRaises(self.ops.OpError):
                self.ops._prepare(bad, 0)

    def test_svg_keeps_its_aspect_when_one_side_is_given(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
               '<rect width="20" height="10" fill="#f00"/></svg>')
        image = self.render([{"type": "svg", "x": 10, "y": 10, "w": 40, "svg": svg}])
        self.assertEqual(image.pixelColor(49, 29).getRgb(), (255, 0, 0, 255))
        self.assertEqual(image.pixelColor(49, 30).alpha(), 0)
        self.assertEqual(image.pixelColor(50, 29).alpha(), 0)

    def test_outline_dilation_is_round(self):
        from PyQt6.QtGui import QImage
        size = 64
        alpha = bytearray(size * size)
        alpha[32 * size + 32] = 255
        dot = self.assets._alpha_image(bytes(alpha), size, size)
        grown = self.assets._dilate(dot, 5, True).convertToFormat(QImage.Format.Format_ARGB32)
        self.assertEqual(grown.pixelColor(37, 32).alpha(), 255)
        self.assertEqual(grown.pixelColor(38, 32).alpha(), 0)
        self.assertEqual(grown.pixelColor(35, 35).alpha(), 255)
        self.assertTrue(0 < grown.pixelColor(36, 36).alpha() < 255)
        self.assertEqual(grown.pixelColor(37, 37).alpha(), 0)


class InstallConfigTests(unittest.TestCase):
    def test_plugin_files_ship_next_to_the_server(self):
        source = mcp_server.plugin_source_dir()
        self.assertTrue(os.path.isfile(os.path.join(source, "krita_mcp.desktop")))
        self.assertTrue(os.path.isdir(os.path.join(source, "krita_mcp")))

    def test_enable_flag_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "kritarc")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write("[general]\r\nfoo=1\r\n\r\n[python]\r\n"
                             "enable_krita_mcp=false\r\nother=1\r\n[ui]\r\nx=1\r\n")
            mcp_server.enable_plugin_flag(path)
            with open(path, "r", encoding="utf-8", newline="") as handle:
                text = handle.read()
            self.assertIn("[general]\r\nfoo=1\r\n", text)
            self.assertIn("enable_krita_mcp=true\r\n", text)
            self.assertIn("other=1\r\n[ui]\r\nx=1\r\n", text)
            self.assertNotIn("enable_krita_mcp=false", text)
            mcp_server.enable_plugin_flag(path)
            with open(path, "r", encoding="utf-8") as handle:
                again = handle.read()
            self.assertEqual(again.count("enable_krita_mcp=true"), 1)

    def test_enable_flag_creates_a_missing_python_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "kritarc")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("[general]\nfoo=1\n")
            mcp_server.enable_plugin_flag(path)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertEqual(text, "[general]\nfoo=1\n\n[python]\nenable_krita_mcp=true\n")


if __name__ == "__main__":
    unittest.main()
