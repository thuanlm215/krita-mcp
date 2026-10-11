"""Live regressions for typed filters, runtime capabilities and local warp."""
import json
import os
from pathlib import Path
import unittest
import base64

from tests.test_mcp import _text_of, _image_of
import tests.test_advanced as advanced


class RevisionTests(unittest.TestCase):
    setUpClass = advanced.AdvancedTests.__dict__["setUpClass"]
    tearDownClass = advanced.AdvancedTests.__dict__["tearDownClass"]
    setUp = advanced.AdvancedTests.setUp
    call = advanced.AdvancedTests.call
    fixture = advanced.AdvancedTests.fixture
    digest = advanced.AdvancedTests.digest
    pixel = advanced.AdvancedTests.pixel
    fill = advanced.AdvancedTests.fill
    brush_state = advanced.AdvancedTests.brush_state
    composite = advanced.AdvancedTests.composite
    authored_tree = advanced.AdvancedTests.authored_tree
    draw_rect = advanced.AdvancedTests.draw_rect

    def test_filter_metadata_and_qcolor_roundtrip(self):
        data = self.call("list_capabilities", kind="filters", include_parameters=True)
        entries = {f["name"]: f for f in data["filters"]}
        self.assertEqual(data["count"], len(entries))
        self.assertGreater(len(entries), 40)
        for name, key, kind in (("colortoalpha", "targetcolor", "QColor"),
                                ("fastcoloroverlay", "color", "QColor"),
                                ("indexcolors", "paletteGen", "QByteArray"),
                                ("phongbumpmap", "illuminantColor0", "QColor")):
            self.assertEqual(entries[name]["parameters"][key]["type"], kind)
            self.assertTrue(entries[name]["parameter_info"][key]["writable"])
        self.fill("blue")
        self.draw_rect(0, 0, 64, 128, "red")
        before = self.digest()
        r = self.call("apply_filter", layer="Paint", filter="colortoalpha",
                      settings={"targetcolor": {"type": "QColor", "rgba": [1, 0, 0, 1]}})
        self.assertEqual(r["settings"]["targetcolor"]["rgba"], [1, 0, 0, 1])
        self.assertEqual(self.pixel(20, 50)[3], 0)
        self.assertEqual(self.pixel(90, 50), [0, 0, 255, 255])
        after = self.digest()
        self.call("undo")
        self.assertEqual(self.digest(), before)
        self.call("redo")
        self.assertEqual(self.digest(), after)
        # Real Properties conversion, including binary NULs and published
        # defaults from every Qt-valued filter; no pixel edits in this probe.
        self.assertTrue(self.fixture('filter_roundtrip'))
        if os.environ.get("KRITA_MCP_TEST_ARTIFACTS"):
            Path(os.environ["KRITA_MCP_TEST_ARTIFACTS"]).joinpath("filter-metadata.json").write_text(
                json.dumps(data, indent=2) + "\n")

    def test_invalid_filter_settings_do_not_mutate(self):
        self.fill("red")
        before = self.digest()
        for name, settings in (("colortoalpha", {"targetcolor": "red"}),
                               ("colortoalpha", {"targetcolor": {"type": "QColor", "rgba": [2, 0, 0, 1]}}),
                               ("indexcolors", {"paletteGen": {"type": "QByteArray", "base64": "!bad!"}}),
                               ("gaussian blur", {"horizRadius": "wrong"})):
            result = self.client.call("apply_filter", {"document": self.doc, "layer": "Paint",
                                                       "filter": name, "settings": settings})
            self.assertTrue(result.get("isError"), result)
            self.assertEqual(self.digest(), before)
        self.call("status")

    def test_serialization_fault_returns_error_and_bridge_survives(self):
        # Fixed private fault still traverses the production MCP serializer.
        self.fixture('fault_status')
        try:
            result = self.client.call("status")
            self.assertTrue(result.get("isError"), result)
            self.assertIn("response_serialization_error", _text_of(result))
            self.assertIn("status", _text_of(result))
            self.assertNotIn("Could not reach", _text_of(result))
        finally:
            self.fixture('fault_restore')
        self.call("status")

    def native_state(self):
        return self.fixture('history')

    def save_image(self, name, layer=None):
        _, result = self.client.ok("get_image", {"document": self.doc, "layer": layer or "merged", "max_size": 128})
        raw = base64.b64decode(_image_of(result)["data"])
        if os.environ.get("KRITA_MCP_TEST_ARTIFACTS"):
            Path(os.environ["KRITA_MCP_TEST_ARTIFACTS"]).joinpath(name).write_bytes(raw)
        return raw

    def test_capabilities_inspection_and_missing_helper(self):
        state = self.native_state()
        data = self.call("list_capabilities", kind="workflow", layer="Paint")
        self.assertTrue(data["native_helper"]["available"], data)
        self.assertEqual(data["native_helper"]["abi"], "krita-6.0.4-qt6")
        self.assertTrue(data["python_api"]["paintLine"])
        self.assertTrue(data["features"]["pixel_edits"]["available"])
        self.assertEqual(self.native_state(), state)
        self.fixture('fault_helper')
        try:
            unavailable = self.call("list_capabilities", kind="workflow", layer="Paint")
            self.assertFalse(unavailable["native_helper"]["available"])
            self.assertTrue(unavailable["python_api"]["paintLine"])
            self.assertFalse(unavailable["features"]["draw_brush_pressure"]["available"])
            self.assertTrue(unavailable["features"]["draw_brush_segments"]["available"])
            self.assertIn("test missing helper", unavailable["native_helper"]["reason"])
            readback=self.call('get_image',layer='Paint',max_size=0)
            self.assertEqual(readback['region']['width'],128)
        finally:
            self.fixture('fault_restore')
        self.assertEqual(self.native_state(), state)
        if os.environ.get("KRITA_MCP_TEST_ARTIFACTS"):
            Path(os.environ["KRITA_MCP_TEST_ARTIFACTS"]).joinpath("capabilities.json").write_text(json.dumps(data, indent=2) + "\n")

    def test_native_smudge_soft_selection_and_ui_restoration(self):
        presets = self.call("list_brushes", query="blender")["presets"]
        self.assertTrue(presets, "Need an installed native Blender preset")
        preset = next((p for p in presets if "smear" in p.lower()), presets[0])
        self.fill("blue")
        self.draw_rect(0, 0, 64, 128, "red")
        self.call("set_selection", x=10, y=32, width=108, height=64)
        self.call("set_selection", mode="feather", amount=10)
        mask = bytes.fromhex(self.fixture('selection'))
        self.assertTrue(any(0 < value < 255 for value in mask))
        before = self.digest()
        old_pixels = bytes.fromhex(self.fixture('pixels',layer='Paint'))
        state = self.brush_state()
        self.save_image("smudge-before.png")
        result = self.call("draw_brush", layer="Paint", preset=preset, size=48,
                           color="#00ff00", points=[{"x": x, "y": 64, "pressure": 1} for x in (35, 50, 65, 80, 95)])
        self.assertEqual(result["undo_steps"], 1)
        self.assertTrue(result["native"])
        self.assertEqual(self.brush_state(), state)
        after = self.digest()
        self.assertNotEqual(before, after)
        new_pixels = bytes.fromhex(self.fixture('pixels',layer='Paint'))
        for i, strength in enumerate(mask):
            if strength == 0:
                self.assertEqual(old_pixels[i*4:i*4+4], new_pixels[i*4:i*4+4])
        raw = bytes.fromhex(self.fixture('pixels'))
        values = [list(raw[(64*128+x)*4:(64*128+x)*4+4]) for x in range(55,96)]
        # Pure smearing/blending must transport red into blue, not paint the
        # supplied green foreground. At least one pixel has both R and B.
        self.assertTrue(any(p[0] > 0 and p[2] > 0 and p[1] == 0 for p in values), (preset, values))
        self.assertEqual(self.pixel(90, 5), [0, 0, 255, 255])
        self.save_image("smudge-after.png")
        self.call("undo")
        self.assertEqual(self.digest(), before)
        self.call("redo")
        self.assertEqual(self.digest(), after)
        if os.environ.get("KRITA_MCP_TEST_ARTIFACTS"):
            Path(os.environ["KRITA_MCP_TEST_ARTIFACTS"]).joinpath("smudge.json").write_text(
                json.dumps({"preset": preset, "result": result, "before": before, "after": after,
                            "brush_state_restored": state == self.brush_state()}, indent=2) + "\n")

    def test_masked_transform_cleanup_and_bake(self):
        self.draw_rect(30, 30, 40, 40, "red")
        raw = self.digest()
        self.call("mask_layer", layer="Paint", mode="fill", value=192)
        self.call("transform_layer", layer="Paint", mode="apply", dx=20, dy=5)
        projection = self.composite()
        history = self.native_state()
        cap = self.call("list_capabilities", kind="workflow", layer="Paint")
        self.assertFalse(cap["features"]["draw_brush_pressure"]["available"])
        for tool, args in (("draw_brush", {"points": [{"x": 1, "y": 1}, {"x": 20, "y": 20}]}),
                           ("draw", {"commands": [{"type": "circle", "x": 50, "y": 50, "radius": 5, "color": "blue"}]}),
                           ("apply_filter", {"filter": "invert", "region": {"x": 0, "y": 0, "width": 64, "height": 64}})):
            response = self.client.call(tool, dict(args, document=self.doc, layer="Paint"))
            self.assertTrue(response.get("isError"), response)
            self.assertIn("Cleanup", _text_of(response))
        self.assertEqual(self.digest(), raw)
        self.assertEqual(self.composite(), projection)
        self.assertEqual(self.native_state(), history)
        self.call("create_layer", name="Cleanup")
        self.call("draw_brush", layer="Cleanup", preset="b) Basic-5 Size", color="blue", size=10,
                  points=[{"x": 50, "y": 50}, {"x": 65, "y": 50}])
        self.assertGreater(self.pixel(55, 50, layer="Cleanup")[3], 0)
        self.call("undo")
        self.assertEqual(self.composite(), projection)
        self.save_image("masked-before-bake.png")
        baked = self.call("bake_layer", layer="Paint", name="Baked")
        self.assertTrue(baked["source_preserved"])
        self.save_image("masked-after-bake.png")
        # A native transformed projection may contain RGB under zero alpha
        # just outside exactBounds. Bake clips those invisible bytes. Compare
        # premultiplied RGBA here; the preserved source stays byte-exact below.
        self.assertTrue(self.fixture('premul_bake_equal'))
        self.call("draw_brush", layer="Baked", preset="b) Basic-5 Size", color="blue", size=10,
                  points=[{"x": 50, "y": 50}, {"x": 65, "y": 50}])
        edited = self.digest("Baked")
        self.call("undo")
        self.assertNotEqual(self.digest("Baked"), edited)
        self.call("redo")
        self.assertEqual(self.digest("Baked"), edited)
        self.assertEqual(self.digest(), raw)

    def warp_fixture(self):
        self.call("draw", layer="Paint", commands=[
            {"type": "fill_rect", "x": 8, "y": 8, "w": 112, "h": 112, "color": "#ffc02080"},
            *[{"type": "line", "x1": x, "y1": 8, "x2": x, "y2": 120, "color": "white", "stroke_width": 2}
              for x in range(16, 121, 16)],
            *[{"type": "line", "x1": 8, "y1": y, "x2": 120, "y2": y, "color": "#5c203a", "stroke_width": 2}
              for y in range(16, 121, 16)]])

    def test_warp_preview_apply_preserved_source_history_and_kra(self):
        self.warp_fixture()
        before = self.digest()
        tree = self.authored_tree()
        self.draw_rect(0, 0, 2, 2, "blue")
        self.call("undo")
        state = self.native_state()
        self.assertLess(state[0], state[1], "Preview must also preserve an existing redo branch")
        image_before = self.save_image("warp-grid-before.png")
        args = {"layer": "Paint", "center": {"x": 64, "y": 64}, "offset": {"x": 8, "y": -4},
                "radius": 40, "name": "Warped", "max_size": 128}
        preview, response = self.client.ok("warp_layer", dict(args, document=self.doc, mode="preview"))
        preview_png = base64.b64decode(_image_of(response)["data"])
        self.assertFalse(preview["mutated"])
        self.assertEqual(self.native_state(), state)
        self.assertEqual(self.authored_tree(), tree)
        self.assertEqual(self.digest(), before)
        result = self.call("warp_layer", **args, mode="apply")
        self.assertTrue(result["source_preserved"])
        self.assertEqual(result["undo_steps"], 1)
        self.assertEqual(result["patch_sha256"], preview["patch_sha256"])
        self.assertEqual(result["changed_pixels"], preview["changed_pixels"])
        after = self.digest("Warped")
        self.assertNotEqual(before, after)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.pixel(16, 16, layer="Warped"), self.pixel(16, 16))
        self.assertEqual(self.save_image("warp-grid-after.png"), preview_png)
        self.call("undo")
        self.assertEqual(self.authored_tree(), tree)
        self.assertEqual(self.save_image("warp-grid-undo.png"), image_before)
        self.call("redo")
        self.assertEqual(self.digest("Warped"), after)
        self.assertEqual(self.save_image("warp-grid-redo.png"), preview_png)
        directory = Path(os.environ.get("KRITA_MCP_TEST_ARTIFACTS", self.temp.name))
        path = directory / "warp-grid.kra"
        self.call("save_document", path=str(path))
        opened = self.call("open_document", path=str(path))
        self.doc = opened["index"]
        self.assertEqual(self.digest("Warped"), after)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.save_image("warp-grid-reopened.png"), preview_png)

    def test_warp_alpha_and_soft_selection_exact_preservation(self):
        self.call("draw", layer="Paint", antialias=False, commands=[{"type": "circle", "cx": 55, "cy": 64,
                   "radius": 25, "color": "#ff8000", "fill": "#ff8000", "stroke_width": 0}])
        raw = self.fixture('pixels',layer='Paint')
        args = {"layer": "Paint", "center": {"x": 74, "y": 64}, "offset": {"x": 8, "y": 0}, "radius": 40}
        self.call("warp_layer", **args, mode="apply", name="Full Warp")
        full = bytes.fromhex(self.fixture('pixels',layer='Full Warp'))
        self.call("undo")
        self.call("set_selection", x=50, y=20, width=60, height=90, value=128)
        self.call("set_selection", mode="feather", amount=4)
        mask = bytes.fromhex(self.fixture('selection'))
        self.assertTrue(any(0 < v < 128 for v in mask))
        _, response = self.client.ok("warp_layer", dict(args, document=self.doc))
        preview_png = base64.b64decode(_image_of(response)["data"])
        r = self.call("warp_layer", **args, mode="apply", name="Soft Warp")
        new = bytes.fromhex(self.fixture('pixels',layer='Soft Warp'))
        old = bytes.fromhex(raw)
        self.assertGreater(r["changed_pixels"], 0)
        for i in range(128*128):
            x, y = i % 128 + .5, i // 128 + .5
            if not mask[i] or (x-82)**2+(y-64)**2 >= 40**2:
                self.assertEqual(old[i*4:i*4+4], new[i*4:i*4+4])
            expected_alpha = round((old[i*4+3]*(255-mask[i])+full[i*4+3]*mask[i])/255)
            self.assertLessEqual(abs(expected_alpha-new[i*4+3]), 1)
            if new[i*4+3]:
                # The fixture has a single orange color; resampled/soft edges
                # must not darken toward black.
                self.assertEqual(new[i*4+2], 255)
                self.assertLessEqual(abs(new[i*4+1]-128), 1)
                self.assertEqual(new[i*4], 0)
        self.assertEqual(self.save_image("warp-soft-after.png"), preview_png)

    def test_warp_rejects_invalid_before_mutation_and_batch(self):
        self.warp_fixture()
        state = self.native_state()
        tree = self.authored_tree()
        before = self.digest()
        args = {"document": self.doc, "layer": "Paint", "mode": "apply", "center": {"x": 64, "y": 64},
                "offset": {"x": 8, "y": 0}, "radius": 40}
        for changes in ({"radius": 0}, {"radius": 1000}, {"radius": float("nan")},
                        {"offset": {"x": 30, "y": 0}}, {"center": {"x": True, "y": 64}},
                        {"center": {"x": -1, "y": 64}}, {"mode": "unknown"}, {"name": ""}):
            response = self.client.call("warp_layer", dict(args, **changes))
            self.assertTrue(response.get("isError"), response)
            self.assertEqual(self.native_state(), state)
            self.assertEqual(self.authored_tree(), tree)
            self.assertEqual(self.digest(), before)
        response = self.client.call("edit_batch", {"document": self.doc, "edits": [
            {"tool": "draw", "arguments": {"layer": "Paint", "commands": [{"type": "fill_rect", "x": 0,
             "y": 0, "w": 20, "h": 20, "color": "red"}]}},
            {"tool": "warp_layer", "arguments": args}]})
        self.assertTrue(response.get("isError"))
        self.assertIn("no edits were started", _text_of(response))
        self.assertEqual(self.native_state(), state)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.authored_tree(), tree)
        unchanged = self.call("warp_layer", layer="Paint", mode="apply", center={"x": 64, "y": 64},
                              offset={"x": 0, "y": 0}, radius=40)
        self.assertEqual(unchanged["undo_steps"], 0)
        self.assertFalse(unchanged["mutated"])
        self.assertEqual(self.native_state(), state)

    def test_warp_failure_rolls_back_and_preserves_off_canvas(self):
        self.warp_fixture()
        self.fixture('seed_offcanvas')
        before = self.digest()
        tree = self.authored_tree()
        state = self.native_state()
        self.fixture('fault_visibility')
        args = {"document": self.doc, "layer": "Paint", "mode": "apply", "center": {"x": 64, "y": 64},
                "offset": {"x": 8, "y": 0}, "radius": 40, "name": "Warped"}
        try:
            response = self.client.call("warp_layer", args)
            self.assertTrue(response.get("isError"), response)
            self.assertIn("injected visibility failure", _text_of(response))
        finally:
            self.fixture('fault_restore')
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.authored_tree(), tree)
        self.assertEqual(self.native_state(), state)
        self.call("warp_layer", **{key: value for key, value in args.items() if key != "document"})
        self.assertEqual(self.fixture('pixels',layer='Warped',rect=[-4,-4,2,2]),
                         bytes([11,22,33,44]*4).hex())
        self.assertEqual(self.digest(), before)

    def test_transformed_ancestor_guard(self):
        self.call("create_layer", name="Group", type="grouplayer")
        self.call("create_layer", name="Part", parent="Group")
        self.draw_rect(30, 30, 40, 40, "red", layer="Group/Part")
        self.call("transform_layer", layer="Group", mode="apply", dx=10)
        state = self.native_state()
        raw = self.digest("Group/Part")
        response = self.client.call("draw_brush", {"document": self.doc, "layer": "Group/Part",
                  "points": [{"x": 40, "y": 40}, {"x": 70, "y": 40}]})
        self.assertTrue(response.get("isError"))
        self.assertIn("parent group", _text_of(response))
        self.assertEqual(self.native_state(), state)
        self.assertEqual(self.digest("Group/Part"), raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
