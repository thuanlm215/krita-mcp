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


class ToolSchemaTests(unittest.TestCase):
    # Empty schemas plus annotations are about 45 KB. The sentences on top of
    # that are capped here so the list cannot grow back toward the 1.7 size.
    SCHEMA_BUDGET = 71000

    def test_shared_stroke_schema_and_size_budget(self):
        plan = mcp_server.TOOLS_BY_NAME["plan_stroke"]["inputSchema"]["properties"]["spec"]
        paint = mcp_server.TOOLS_BY_NAME["paint_stroke"]["inputSchema"]["properties"]["spec"]
        self.assertIs(plan, paint)
        size = len(json.dumps(mcp_server.public_tools()))
        self.assertLess(size, self.SCHEMA_BUDGET)

    def test_every_tool_publishes_annotations(self):
        listed = {tool["name"]: tool for tool in mcp_server.public_tools()}
        self.assertEqual(set(listed), {spec["name"] for spec in mcp_server.TOOLS})
        read_only = set()
        destructive = set()
        for spec in mcp_server.TOOLS:
            ann = spec["annotations"]
            self.assertIsInstance(ann["readOnlyHint"], bool)
            self.assertIsInstance(ann["destructiveHint"], bool)
            self.assertIs(ann["openWorldHint"], False)
            self.assertTrue(ann["title"])
            self.assertEqual(listed[spec["name"]]["annotations"], ann)
            if ann["readOnlyHint"]:
                read_only.add(spec["name"])
                self.assertIs(ann["destructiveHint"], False)
            elif ann["destructiveHint"]:
                destructive.add(spec["name"])
        self.assertEqual(read_only, set(mcp_server.READ_ONLY_TOOLS))
        self.assertEqual(destructive, set(mcp_server.DESTRUCTIVE_TOOLS))


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
        self.assertTrue({"outline_layer", "export_layers", "slice_sheet",
                         "remove_background", "quantize_layer"} <= set(tools))
        for name in ("slice_sheet", "remove_background", "quantize_layer"):
            self.assertEqual(tools[name]["_required"], [], name)
        self.assertEqual(tools["draw_brush"]["_required"], [])
        self.assertIn("strokes", tools["draw_brush"]["inputSchema"]["properties"])
        self.assertEqual(tools["export_layers"]["_required"], ["directory"])
        self.assertTrue({"path", "scaling"} <= set(tools["create_layer"]["inputSchema"]["properties"]))
        command = tools["draw"]["inputSchema"]["properties"]["commands"]["items"]["properties"]
        self.assertTrue({"svg", "smooth", "rx", "ry", "angle", "path"} <= set(command))

    def test_draw_schema_lists_every_documented_command(self):
        command = mcp_server.DRAW_COMMAND
        documented = set()
        for line in command["description"].splitlines():
            if line.startswith("- ") and ":" in line:
                documented.update(name.strip() for name in line[2:].split(":")[0].split("/"))
        self.assertTrue({"ink", "svg"} <= documented)
        self.assertEqual(documented - set(command["properties"]["type"]["enum"]), set())

    def test_layer_tools_echo_the_tree_only_on_request(self):
        tools = mcp_server.TOOLS_BY_NAME
        for name in ("create_layer", "delete_layer", "duplicate_layer", "move_layer", "merge_layer_down"):
            self.assertIn("include_tree", tools[name]["inputSchema"]["properties"], name)
        self.assertIn("row", tools["slice_sheet"]["inputSchema"]["properties"]["align"]["enum"])
        self.assertTrue({"accents", "despeckle"} <= set(tools["quantize_layer"]["inputSchema"]["properties"]))

    def test_batch_summary_drops_receipts_and_keeps_facts(self):
        editing = importlib.import_module(package.__name__ + ".operations.editing")
        schema = mcp_server.TOOLS_BY_NAME["edit_batch"]["inputSchema"]["properties"]["results"]
        self.assertEqual((schema["enum"], schema["default"]), (["summary", "full", "none"], "summary"))
        layer = {"name": "Boots", "type": "paintlayer", "uuid": "u", "bounds": {"x": 0, "y": 0, "width": 0, "height": 0}}
        self.assertEqual(editing.brief({"created": True, "layer": layer}), {"layer": "Boots"})
        self.assertEqual(editing.brief({"drawn": 3, "selection_respected": True, "undoable": True, "undo_steps": 1,
                                        "layer": "Boots", "region": {"x": 1, "y": 2, "width": 3, "height": 4},
                                        "note": "Recorded ..."}),
                         {"drawn": 3, "layer": "Boots", "region": [1, 2, 3, 4]})
        self.assertEqual(editing.brief({"selection_respected": False, "palette": list(range(20))}),
                         {"selection_respected": False, "palette": "20 items"})

    def test_results_keep_short_containers_on_one_line(self):
        result = {"results": [{"tool": "draw", "region": [304, 1172, 143, 139]}],
                  "layer": {"name": "x" * 60, "bounds": {"x": 0, "y": 0, "width": 10, "height": 12}, "z": "y" * 40}}
        text = mcp_server._summarise(result)
        self.assertEqual(json.loads(text), result)
        self.assertIn('"region": [304, 1172, 143, 139]', text)
        self.assertIn('"bounds": {"x": 0, "y": 0, "width": 10, "height": 12}', text)
        self.assertTrue(all(len(line) <= 100 for line in text.splitlines()), text)
        for value in ([], {}, {"k": {}}, [[1, 2]] * 40, {"s": "x" * 300}):
            self.assertEqual(json.loads(mcp_server._summarise(value)), value)

    def test_insert_node_reaches_every_docker_position(self):
        class Node:
            def __init__(self, name):
                self.name, self.kids = name, []

            def childNodes(self):
                return list(self.kids)  # bottom first, as libkis

            def addChildNode(self, child, above):
                # libkis puts a child with no anchor on top.
                self.kids.insert(len(self.kids) if above is None else self.kids.index(above) + 1, child)
                return True

            def removeChildNode(self, child):
                self.kids.remove(child)
                return True

        for count in range(4):
            for ui_index in range(count + 1):
                parent = Node("parent")
                parent.kids = [Node(str(i)) for i in range(count)]
                docker = [n.name for n in reversed(parent.kids)]
                self.assertTrue(self.ops._insert_node(parent, Node("new"), ui_index))
                docker.insert(ui_index, "new")
                self.assertEqual([n.name for n in reversed(parent.kids)], docker, (count, ui_index))

    def test_row_alignment_floors_follow_each_sprite_row(self):
        cells = [{"index": 0, "box": (0, 10, 20, 50)}, {"index": 1, "box": (30, 2, 50, 42)},
                 {"index": 2, "box": (0, 70, 20, 90)}]
        self.assertEqual(self.assets._row_floors(cells), {0: 50, 1: 50, 2: 90})
        grid = [{"index": 0, "box": (0, 0, 10, 5), "row": 0}, {"index": 1, "box": (20, 8, 30, 12), "row": 0}]
        self.assertEqual(self.assets._row_floors(grid), {0: 12, 1: 12})

    def test_export_staging_directories_follow_the_umask(self):
        common = importlib.import_module(package.__name__ + ".operations.common")
        previous = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                stage = common.stage_directory(tmp, ".stage-")
                self.assertEqual(stage.stat().st_mode & 0o777, 0o755)
                self.assertTrue(stage.name.startswith(".stage-"))
        finally:
            os.umask(previous)

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


class AssetCleanupTests(unittest.TestCase):
    """Sheet slicing, background removal and palette cores, without Krita."""

    setUpClass = AssetToolTests.__dict__["setUpClass"]

    @classmethod
    def cleanup(cls):
        return importlib.import_module(package.__name__ + ".operations.cleanup")

    def sprite(self, size=100):
        from PyQt6.QtCore import QRectF, Qt
        from PyQt6.QtGui import QColor, QImage, QPainter
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(QColor(255, 255, 255))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(200, 30, 30))
        painter.drawEllipse(QRectF(20, 20, 60, 60))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(QRectF(40, 40, 20, 20))
        painter.end()
        return self.ops.imaging.image_bytes(image)

    @staticmethod
    def rgba(data, x, y, width=100):
        i = (y * width + x) * 4
        return (data[i + 2], data[i + 1], data[i], data[i + 3])

    def test_auto_slicing_splits_at_gaps_and_drops_specks(self):
        width, height = 140, 130
        raw = bytearray(bytes([250, 250, 250, 255]) * (width * height))
        boxes = [(10, 10, 40, 50), (60, 12, 90, 45), (10, 80, 50, 120), (70, 90, 72, 92)]
        for x0, y0, x1, y1 in boxes:
            for y in range(y0, y1):
                raw[(y * width + x0) * 4:(y * width + x1) * 4] = bytes([30, 160, 220, 255]) * (x1 - x0)
        raw = bytes(raw)
        background = self.assets._corner_color(raw, width, height)
        self.assertEqual(background, (250, 250, 250, 255))
        mask, _ = self.assets._content_mask(raw, width, height, background, 16, 24)
        cells = []
        self.assets._xy_cut(mask, width, (0, 0, width, height), 2, cells)
        kept = [c for c in cells if self.assets._count(mask, width, c) >= 16]
        self.assertEqual(kept, boxes[:3])
        merged = []
        self.assets._xy_cut(mask, width, (0, 0, width, height), 25, merged)
        self.assertIn((10, 10, 90, 50), merged)

    def test_frame_columns_keep_effects_with_their_figure(self):
        width, height = 300, 100
        def sheet(ground):
            mask = bytearray(width * height)
            def fill(x0, y0, x1, y1):
                for y in range(y0, y1):
                    mask[y * width + x0:y * width + x1] = b"\x01" * (x1 - x0)
            for x in (20, 120, 220):
                fill(x, 20, x + 40, 90)
            fill(55, 10, 60, 20)       # figure 1 raises a slash ...
            fill(55, 10, 140, 15)      # ... over figure 2's columns
            fill(130, 16, 132, 18)     # a chip off the slash
            if ground:
                fill(10, 92, 290, 95)  # one ground line joins every figure
            cells = []
            self.assets._frame_cut(bytes(mask), width, height, 2, 3, cells)
            return {cell["column"]: cell["box"] for cell in cells}
        boxes = sheet(False)
        self.assertEqual(boxes, {0: (20, 10, 140, 90), 1: (120, 20, 160, 90), 2: (220, 20, 260, 90)})
        boxes = sheet(True)
        self.assertEqual(sorted(boxes), [0, 1, 2])
        for column, x in enumerate((20, 120, 220)):
            left, _, right, bottom = boxes[column]
            self.assertTrue(left <= x and x + 40 <= right and bottom == 95, boxes)
        self.assertLessEqual(boxes[0][2], 120);self.assertGreaterEqual(boxes[1][0], 60)
        self.assertLessEqual(boxes[1][2], 220);self.assertGreaterEqual(boxes[2][0], 160)
        cells = []
        self.assets._frame_cut(bytes(width * height), width, height, 2, 3, cells)
        self.assertEqual(cells, [])

    def test_holes_clear_enclosed_background_above_a_size(self):
        raw = self.sprite()
        kept = self.cleanup()._cleared(raw, 100, 100, (255, 255, 255), "contiguous", 32, 24, True, 0, 500)
        cleared = self.cleanup()._cleared(raw, 100, 100, (255, 255, 255), "contiguous", 32, 24, True, 0, 200)
        self.assertEqual(self.rgba(kept, 50, 50)[3], 255)
        self.assertEqual(self.rgba(cleared, 50, 50)[3], 0)
        self.assertEqual(self.rgba(cleared, 30, 50), (200, 30, 30, 255))

    def test_contiguous_removal_keeps_enclosed_background_colour(self):
        raw = self.sprite()
        kept = self.cleanup()._cleared(raw, 100, 100, (255, 255, 255), "contiguous", 32, 24, True, 0)
        cleared = self.cleanup()._cleared(raw, 100, 100, (255, 255, 255), "global", 32, 24, True, 0)
        self.assertEqual(self.rgba(kept, 2, 2)[3], 0)
        self.assertEqual(self.rgba(kept, 50, 50), (255, 255, 255, 255))
        self.assertEqual(self.rgba(cleared, 50, 50)[3], 0)
        self.assertEqual(self.rgba(kept, 30, 50), (200, 30, 30, 255))

    def test_defringe_removes_the_background_tint_from_edges(self):
        raw = self.sprite()
        clean = self.cleanup()._cleared(raw, 100, 100, (255, 255, 255), "contiguous", 32, 24, True, 0)
        plain = self.cleanup()._cleared(raw, 100, 100, (255, 255, 255), "contiguous", 32, 24, False, 0)
        rim = [(x, y) for y in range(100) for x in range(100) if 0 < self.rgba(clean, x, y)[3] < 255]
        self.assertGreater(len(rim), 50)
        tint = lambda data: max(min(self.rgba(data, x, y)[1:3]) for x, y in rim)
        self.assertLess(tint(clean), 45)
        self.assertGreater(tint(plain), 150)

    def test_palette_quantize_dither_replace_and_swap(self):
        from PyQt6.QtGui import QColor, QImage
        image = QImage(64, 4, QImage.Format.Format_ARGB32)
        for x in range(64):
            for y in range(4):
                image.setPixelColor(x, y, QColor(x * 4, x * 4, x * 4, 255 if x % 2 else 128))
        raw = self.ops.imaging.image_bytes(image)
        grey = [QColor(v, v, v) for v in (0, 85, 170, 255)]
        cleanup = self.cleanup()
        plain = cleanup._quantized(raw, 64, 4, grey, None, "none")
        self.assertEqual({plain[i] for i in range(0, len(plain), 4)}, {0, 85, 170, 255})
        self.assertEqual(plain[3::4], raw[3::4])
        dithered = cleanup._quantized(raw, 64, 4, grey, None, "ordered")
        band = [dithered[(y * 64 + x) * 4] for y in range(4) for x in range(22, 28)]
        self.assertEqual(set(band), {85, 170})
        swapped = cleanup._quantized(raw, 64, 4, grey, [QColor("red"), QColor("lime"),
                                                       QColor("blue"), QColor("yellow")], "none")
        self.assertEqual(self.rgba(swapped, 0, 0, 64)[:3], (255, 0, 0))
        samples = [(v, v, v) for v in range(0, 256, 4)]
        self.assertEqual(len(cleanup._median_cut(samples, 4)), 4)
        sprite = self.sprite()
        shifted, counts = cleanup._swapped(sprite, [QColor(200, 30, 30)], [QColor(30, 30, 200)], 40)
        self.assertGreater(counts[0], 2000)
        self.assertEqual(self.rgba(shifted, 30, 50), (30, 30, 200, 255))
        self.assertEqual(self.rgba(shifted, 50, 50), (255, 255, 255, 255))


    def test_quantize_picks_the_euclidean_nearest_colour(self):
        import random
        from PyQt6.QtGui import QColor
        cleanup = self.cleanup()
        rng = random.Random(7)
        colors = [tuple(rng.randrange(256) for _ in range(3)) for _ in range(200)]
        nearest = cleanup._Nearest(colors)
        for _ in range(3000):
            rgb = tuple(rng.randrange(256) for _ in range(3))
            key = int.from_bytes(bytes((rgb[2], rgb[1], rgb[0], 255)), sys.byteorder)
            want = min(range(len(colors)),
                       key=lambda i: (sum((a - b) ** 2 for a, b in zip(rgb, colors[i])), i))
            self.assertEqual(nearest[key], want, rgb)
        # Qt's table conversion sums channel differences and turned this
        # dark outline pixel green.
        palette = [QColor(c) for c in ("#1a1230", "#17663a", "#ff3b5c")]
        self.assertEqual(cleanup._indices(bytes((67, 57, 66, 255)), 1, 1, palette, "none")[0], 0)

    def test_accents_stay_off_blended_pixels(self):
        from PyQt6.QtGui import QColor
        cleanup = self.cleanup()
        palette = [QColor(c) for c in ("#ffffff", "#1a1230", "#ff3b5c")]
        raw = bytes((130, 120, 200, 255, 90, 60, 250, 255))  # BGRA: a blend, then a gem pixel
        self.assertEqual(list(cleanup._indices(raw, 2, 1, palette, "none")), [2, 2])
        self.assertEqual(list(cleanup._indices(raw, 2, 1, palette, "none", {2}, 32)), [0, 2])

    def test_despeckle_replaces_only_lone_pixels(self):
        from PyQt6.QtGui import QColor
        cleanup = self.cleanup()
        palette = [QColor(c) for c in ("#2a9e52", "#ffffff", "#e8798a")]
        w, h = 7, 5
        raw = bytearray(bytes((82, 158, 42, 255)) * (w * h))
        index = bytearray(w * h)
        index[2 * w + 1] = 2                       # a lone blended pixel
        raw[(2 * w + 1) * 4:(2 * w + 2) * 4] = bytes((150, 130, 140, 255))
        index[2 * w + 4] = index[2 * w + 5] = 1    # a two-pixel glint stays
        index[4 * w - 1] = index[4 * w] = 2        # adjacent only if rows wrapped
        self.assertEqual(cleanup._despeckle(index, bytes(raw), w, h, palette), 3)
        self.assertEqual((index[2 * w + 1], index[4 * w - 1], index[4 * w]), (0, 0, 0))
        self.assertEqual((index[2 * w + 4], index[2 * w + 5]), (1, 1))
        # Transparent neighbours do not count: a dot alone in empty space stays.
        raw = bytearray(w * h * 4)
        raw[(2 * w + 3) * 4:(2 * w + 4) * 4] = bytes((140, 120, 230, 255))
        index = bytearray(w * h)
        index[2 * w + 3] = 2
        self.assertEqual(cleanup._despeckle(index, bytes(raw), w, h, palette), 0)


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
