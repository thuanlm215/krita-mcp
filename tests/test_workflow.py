#!/usr/bin/env python3
"""Live artist-workflow tests over MCP stdio -> HTTP -> actual Krita.

Run with Krita open: python -m unittest tests.test_workflow
Uses new fixture documents; never closes or saves a user's document.
"""

from pathlib import Path
import tempfile
import unittest
import uuid
from tests.fixture_client import fixture

from tests.test_mcp import Client, PROTOCOL, _image_of


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = Client()
        cls.client.request("initialize", {"protocolVersion": PROTOCOL,
                           "capabilities": {}, "clientInfo": {
                               "name": "workflow-tests", "version": "1"}})
        cls.client.notify("notifications/initialized")
        cls.temp = tempfile.TemporaryDirectory(prefix="krita-workflow-")

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.temp.cleanup()

    def setUp(self):
        self.doc = "workflow-test-" + uuid.uuid4().hex[:8]
        self.call("create_document", width=128, height=128, name=self.doc)
        self.call("set_layer", layer="Background", visible=False)
        self.call("create_layer", name="Paint")

    def call(self, tool, **args):
        if tool not in ("create_document", "open_document"):
            args.setdefault("document", self.doc)
        payload, _ = self.client.ok(tool, args)
        return payload

    def fixture(self, scenario, **args):
        args.setdefault('document', self.doc)
        return fixture(scenario, **args)

    def digest(self, layer="Paint", projection=False):
        return self.fixture('digest', layer=layer, projection=projection)

    def pixel(self, x, y, layer="Paint"):
        return self.call("get_pixel", layer=layer, x=x, y=y)["rgba"]

    def fill(self, color, layer="Paint"):
        return self.call("draw", layer=layer, commands=[{
            "type": "fill_rect", "x": 0, "y": 0, "w": 128, "h": 128,
            "color": color}])

    def brush_state(self):
        return self.fixture('brush_state')

    def test_tool_schemas(self):
        specs = self.client.request("tools/list")["result"]["tools"]
        names = {s["name"] for s in specs}
        self.assertTrue({"draw_brush", "list_brushes", "select_polygon",
                         "selection_to_layer", "transform_layer", "undo", "redo"} <= names)
        self.assertEqual(len(names), len(__import__('mcp_server').TOOLS))
        self.assertNotIn('run_python', names)
        self.assertNotIn('_test_fixture', names)
        self.assertIn("b) Basic-5 Size", self.call("list_brushes", query="Basic-5")["presets"])

    def test_draw_rect_selection_and_soft_mask(self):
        self.fill("blue")
        self.call("set_selection", x=10, y=10, width=20, height=20)
        result = self.fill("red")
        self.assertTrue(result["selection_respected"])
        self.assertEqual(self.pixel(15, 15), [255, 0, 0, 255])
        self.assertEqual(self.pixel(50, 50), [0, 0, 255, 255])
        self.call("set_selection", x=10, y=10, width=20, height=20, value=128)
        self.fill("blue")
        self.assertEqual(self.pixel(15, 15), [127, 0, 128, 255])
        self.call("draw", layer="Paint", commands=[{
            "type": "clear", "x": 0, "y": 0, "w": 128, "h": 128}])
        self.assertEqual(self.pixel(15, 15)[3], 127)
        self.assertEqual(self.pixel(50, 50), [0, 0, 255, 255])

    def test_polygon_mask_and_combine(self):
        self.call("select_polygon", points=[[10, 10], [60, 10], [10, 60]], antialias=False)
        self.fill("red")
        self.assertEqual(self.pixel(15, 15), [255, 0, 0, 255])
        self.assertEqual(self.pixel(50, 50)[3], 0)
        self.call("select_polygon", points=[[80, 80], [110, 80], [110, 110], [80, 110]],
                  combine="add", antialias=False)
        self.fill("blue")
        self.assertEqual(self.pixel(90, 90), [0, 0, 255, 255])
        self.call("select_polygon", points=[[80, 80], [110, 80], [110, 110], [80, 110]],
                  combine="subtract", antialias=False)
        self.fill("green")
        self.assertEqual(self.pixel(90, 90), [0, 0, 255, 255])
        self.call("select_polygon", points=[[10, 10], [30, 10], [30, 30], [10, 30]],
                  combine="intersect", antialias=False)
        self.fill("white")
        self.assertEqual(self.pixel(15, 15), [255, 255, 255, 255])
        self.assertNotEqual(self.pixel(35, 15), [255, 255, 255, 255])

    def test_polygon_feather(self):
        self.call("select_polygon", points=[[30, 30], [90, 30], [90, 90], [30, 90]],
                  feather=8)
        self.fill("red")
        self.assertEqual(self.pixel(60, 60)[3], 255)
        self.assertTrue(0 < self.pixel(30, 60)[3] < 255)
        self.assertEqual(self.pixel(10, 60)[3], 0)

    def test_selection_undo_keeps_previous_mask(self):
        self.call("set_selection", x=20, y=20, width=30, height=30)
        before = self.call("inspect_document")["selection"]
        self.call("set_selection", mode="grow", amount=6)
        self.assertNotEqual(self.call("inspect_document")["selection"], before)
        self.call("undo")
        self.assertEqual(self.call("inspect_document")["selection"], before)
        self.call("select_polygon", points=[[70, 70], [110, 70], [110, 110], [70, 110]])
        self.call("undo")
        self.assertEqual(self.call("inspect_document")["selection"], before)

    def test_native_pressure_undo_redo_and_ui_restore(self):
        before = self.digest()
        state = self.brush_state()
        result = self.call("draw_brush", layer="Paint", preset="b) Basic-5 Size",
                           size=30, color="#a53218", points=[
                               {"x": 10, "y": 60, "pressure": 0.15},
                               {"x": 115, "y": 60, "pressure": 1}])
        self.assertEqual(result["undo_steps"], 1)
        self.assertEqual(self.brush_state(), state)
        raw = bytes.fromhex(self.fixture('pixels'))
        widths = [sum(raw[(y*128+x)*4+3] > 0 for y in range(128)) for x in (25,60,100)]
        self.assertTrue(0 < widths[0] < widths[1] < widths[2], widths)
        painted = self.digest()
        self.assertNotEqual(before, painted)
        self.assertEqual(self.call("undo")["actions_triggered"], 1)
        self.assertEqual(self.digest(), before)
        self.call("redo")
        self.assertEqual(self.digest(), painted)

    def test_pressure_segments_and_native_path(self):
        before = self.digest()
        points = [{"x": 10, "y": 20, "pressure": 0.2},
                  {"x": 60, "y": 40, "pressure": 1},
                  {"x": 100, "y": 70, "pressure": 0.4}]
        result = self.call("draw_brush", layer="Paint", preset="b) Basic-5 Size", points=points)
        self.assertEqual(result["undo_steps"], 1)
        self.call("undo", count=1)
        self.assertEqual(self.digest(), before)
        for point in points:
            point["pressure"] = 1
        result = self.call("draw_brush", layer="Paint", mode="path",
                           preset="b) Basic-5 Size", points=points)
        self.assertEqual(result["undo_steps"], 1)
        self.assertNotEqual(self.digest(), before)
        self.call("undo")
        self.assertEqual(self.digest(), before)

    def test_brush_selection_opacity_and_eraser(self):
        self.call("set_selection", x=40, y=20, width=20, height=80)
        args = {"layer": "Paint", "preset": "b) Basic-5 Size", "size": 20,
                "points": [{"x": 10, "y": 60}, {"x": 100, "y": 60}]}
        self.call("draw_brush", opacity=0.5, **args)
        self.assertEqual(self.pixel(25, 60)[3], 0)
        self.assertTrue(100 <= self.pixel(50, 60)[3] <= 140)
        painted = self.digest()
        self.call("draw_brush", erase=True, **args)
        self.assertLess(self.pixel(50, 60)[3], 30)
        self.call("undo")
        self.assertEqual(self.digest(), painted)

    def test_extract_copy_cut_and_native_undo(self):
        self.fill("#a53218")
        original = self.digest()
        for mode, steps in (("copy", 2), ("cut", 3)):
            self.call("select_polygon", points=[[20, 20], [60, 20], [20, 60]], antialias=False)
            result = self.call("selection_to_layer", layer="Paint", name="Part", mode=mode)
            self.assertEqual(result["undo_steps"], steps)
            self.assertEqual(self.pixel(25, 25, layer="Part"), [165, 50, 24, 255])
            self.assertEqual(self.pixel(55, 55, layer="Part")[3], 0)
            self.assertIsNone(self.call("inspect_document")["selection"])
            if mode == "copy":
                self.assertEqual(self.digest(), original)
            else:
                self.assertEqual(self.pixel(25, 25)[3], 0)
                self.assertEqual(self.pixel(55, 55), [165, 50, 24, 255])
            self.call("undo", count=steps)
            self.assertEqual(self.digest(), original)
            self.assertNotIn("Part", [n["name"] for n in self.call("inspect_document")["layers"]])
            self.call("redo", count=steps)
            self.assertEqual(self.pixel(25, 25, layer="Part"), [165, 50, 24, 255])
            self.call("undo", count=steps)

    def test_transform_preview_pivot_native_undo_and_save(self):
        self.call("draw", layer="Paint", commands=[{
            "type": "fill_rect", "x": 40, "y": 20, "w": 10, "h": 20, "color": "red"}])
        raw = self.digest()
        before_projection = self.digest(projection=True)
        before_tree = self.call("inspect_document")["layers"]
        params = {"document": self.doc, "layer": "Paint", "pivot": {"x": 20, "y": 20},
                  "degrees": 90, "strategy": "NearestNeighbor"}
        preview, blocks = self.client.ok("transform_layer", params)
        self.assertIsNotNone(_image_of(blocks))
        self.assertFalse(preview["mutated"])
        self.assertEqual(self.call("inspect_document")["layers"], before_tree)
        applied = self.call("transform_layer", **dict(params, mode="apply"))
        self.assertEqual(applied["native_matrix"], applied["matrix"])
        self.assertEqual(self.digest(), raw)
        transformed = self.digest(projection=True)
        self.assertNotEqual(transformed, before_projection)
        self.call("undo")
        self.assertEqual(self.digest(projection=True), before_projection)
        self.call("redo")
        self.assertEqual(self.digest(projection=True), transformed)
        # Updating the same mask stays absolute and adds no new mask.
        applied = self.call("transform_layer", **dict(params, mode="apply", dx=8,
                            scale_x=1.5, scale_y=0.75))
        for native, expected in zip(applied["native_matrix"], applied["matrix"]):
            self.assertAlmostEqual(native, expected, places=6)
        self.assertFalse(applied["mask_created"])
        updated = self.digest(projection=True)
        self.call("undo")
        self.assertEqual(self.digest(projection=True), transformed)
        self.call("redo")
        self.assertEqual(self.digest(projection=True), updated)
        path = str(Path(self.temp.name) / (self.doc + ".kra"))
        self.call("save_document", path=path)
        saved_index = self.call("inspect_document")["index"]
        reopened = self.call("open_document", path=path)
        self.assertNotEqual(reopened["index"], saved_index)
        self.doc = reopened["index"]
        # Opening the same KRA twice must select the reopened document rather
        # than the first copy (the persisted layer IDs match both copies).
        self.call("undo")
        self.assertEqual(self.fixture('digest', document=saved_index, projection=True), updated)
        self.assertEqual(self.digest(), raw)
        self.assertEqual(self.digest(projection=True), updated)

    def test_invalid_inputs_leave_pixels_and_brush_state_unchanged(self):
        self.fill("blue")
        before, state = self.digest(), self.brush_state()
        bad_calls = [
            ("draw_brush", {"points": [{"x": 10, "y": 20}, {"x": 60, "y": 20, "pressure": 2}]}),
            ("draw_brush", {"points": [{"x": 10, "y": 20}, {"x": 60, "y": 20}], "preset": "missing"}),
            ("draw_brush", {"points": [{"x": 10, "y": 20}, {"x": 60, "y": 20}], "size": float('nan')}),
            ("select_polygon", {"points": [[10, 10], [20, 20]]}),
            ("transform_layer", {"scale_x": 0}),
            ("transform_layer", {"mode": "apply", "scale_x": 100, "scale_y": 100}),
            ("selection_to_layer", {"mode": "cut"}),
            ("undo", {"count": 0}),
        ]
        for tool, args in bad_calls:
            with self.subTest(tool=tool, args=args):
                self.assertTrue(self.client.call(tool, dict(args, document=self.doc, layer="Paint"))
                                .get("isError"))
                self.assertEqual(self.digest(), before)
                self.assertEqual(self.brush_state(), state)

    def test_locked_and_masked_layers_fail_before_paint(self):
        self.fill("blue")
        before = self.digest()
        args = {"document": self.doc, "layer": "Paint", "points": [
            {"x": 20, "y": 20}, {"x": 80, "y": 80}]}
        self.call("set_layer", layer="Paint", locked=True)
        self.assertTrue(self.client.call("draw_brush", args).get("isError"))
        self.assertEqual(self.digest(), before)
        self.call("set_layer", layer="Paint", locked=False)
        self.call("transform_layer", layer="Paint", mode="apply", degrees=15)
        self.assertTrue(self.client.call("draw_brush", args).get("isError"))
        self.assertEqual(self.digest(), before)

    def test_requested_document_undo_and_selection(self):
        first = self.doc
        before = self.digest()
        self.call("draw_brush", layer="Paint", points=[{"x": 10, "y": 20}, {"x": 60, "y": 20}])
        self.setUp()
        second_before = self.digest()
        self.call("undo", document=first)
        self.assertEqual(self.digest(), second_before)
        self.doc = first
        self.assertEqual(self.digest(), before)
        self.call("set_selection", x=10, y=10, width=20, height=20)
        self.assertIsNotNone(self.call("inspect_document")["selection"])

    def test_deselect_clears_only_the_requested_document(self):
        first = self.doc
        self.call("set_selection", x=10, y=10, width=20, height=20)
        self.setUp()
        self.call("set_selection", x=30, y=30, width=10, height=10)
        for _ in range(3):
            self.call("set_selection", document=first, mode="none")
            self.assertIsNone(self.call("inspect_document", document=first)["selection"])
            self.assertIsNotNone(self.call("inspect_document")["selection"])
            self.call("undo", document=first)
            self.assertIsNotNone(self.call("inspect_document", document=first)["selection"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
