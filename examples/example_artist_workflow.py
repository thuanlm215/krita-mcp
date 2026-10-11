#!/usr/bin/env python3
"""Exercise the real arm-edit workflow through MCP, without pixel-edit scripts.

This fixture expects the 512x512 source_context.png from the shoulder/arm
study. Geometry is specific to that reference; it is a tool demonstration,
not an automatic character segmentation algorithm or an approved final pose.
"""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil

from example_poster import Client

OFFSET = (390, 350)
PARTS = {
    "shoulder": [(488,394),(524,436),(579,448),(595,466),(603,505),(625,523),
                 (583,521),(551,531),(520,553),(466,588),(462,536),(435,530),
                 (458,473),(468,462),(501,444)],
    "upper": [(552,534),(581,525),(609,530),(638,545),(620,558),(645,581),
              (652,600),(625,632),(609,650),(583,626),(564,629),(532,609),
              (510,584),(513,557)],
    "forearm": [(603,595),(623,588),(647,588),(672,592),(684,612),(698,636),
                (714,663),(738,666),(742,697),(722,722),(686,743),(662,724),
                (646,709),(632,692),(612,688),(607,665),(590,641),(587,624)],
    "hand": [(727,695),(741,698),(751,708),(766,723),(777,741),(776,757),
             (765,763),(757,776),(743,779),(726,773),(716,762),(703,755),
             (696,736),(689,723),(701,708)],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/workflow/demo"))
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    input_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    # Krita works on an output-local copy, and save-as creates its own KRA.
    copied = output / "reference.png"
    if source != copied:
        shutil.copy2(source, copied)
    client = Client()
    calls = []
    document = None

    def call(tool_name, **params):
        if tool_name not in ("status", "open_document"):
            params.setdefault("document", document)
        result, image = client.call(tool_name, params)
        calls.append({"tool": tool_name, "arguments": params, "result": result})
        return result, image

    def snapshot(name, layer=None):
        params = {"max_size": 512}
        if layer:
            params["layer"] = layer
        _, png = call("get_image", **params)
        raw = base64.b64decode(png)
        (output / name).write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def select(names):
        for i, name in enumerate(names):
            call("select_polygon", points=[[x-OFFSET[0], y-OFFSET[1]] for x,y in PARTS[name]],
                 combine="replace" if i == 0 else "add", antialias=False)

    try:
        client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "artist-workflow", "version": "1"}})
        client.notify("notifications/initialized")
        status, _ = call("status")
        opened, _ = call("open_document", path=str(copied))
        document = opened["index"]
        if (opened["width"], opened["height"]) != (512, 512):
            raise RuntimeError("This fixture requires the study's 512x512 source_context.png")
        info, _ = call("inspect_document")
        original = info["active_layer"]
        call("set_layer", layer=original, new_name="Original - untouched", visible=False, locked=True)
        source_pixels = snapshot("reference-layer.png", "Original - untouched")
        call("duplicate_layer", layer="Original - untouched", name="Working source")
        call("set_layer", layer="Working source", visible=True, locked=False)
        select(("shoulder", "upper", "forearm", "hand"))
        call("selection_to_layer", layer="Working source", name="Arm assembly", mode="copy")
        call("set_layer", layer="Working source", visible=False)
        before = snapshot("before.png")
        select(("forearm", "hand"))
        extraction, _ = call("selection_to_layer", layer="Arm assembly", name="Forearm", mode="cut")
        before_transform = snapshot("after-extraction.png")
        transform = {"layer": "Forearm", "pivot": {"x": 223, "y": 263},
                     "degrees": -30, "strategy": "Bilinear"}
        _, preview = call("transform_layer", **transform)
        (output / "transform-preview.png").write_bytes(base64.b64decode(preview))
        applied, _ = call("transform_layer", mode="apply", **transform)
        after_transform = snapshot("after-transform.png")
        call("undo", count=applied["undo_steps"])
        undo_pose = snapshot("undo-pose.png")
        if undo_pose != before_transform:
            raise RuntimeError("Native transform Undo did not restore the pre-transform image")
        call("redo", count=applied["undo_steps"])
        if snapshot("redo-pose.png") != after_transform:
            raise RuntimeError("Native transform Redo differed from the applied image")
        call("create_layer", name="Cleanup")
        cleanup_before = snapshot("before-cleanup.png")
        stroke, _ = call("draw_brush", layer="Cleanup", preset="g) Dry Bristles",
                        color="#39312b", size=18, opacity=0.75, points=[
                            {"x": 222, "y": 258, "pressure": 0.3},
                            {"x": 244, "y": 252, "pressure": 0.8}])
        after_cleanup = snapshot("after.png")
        if after_cleanup == cleanup_before:
            raise RuntimeError("Cleanup stroke caused no visible pixel change")
        call("undo", count=stroke["undo_steps"])
        if snapshot("undo-cleanup.png") != cleanup_before:
            raise RuntimeError("Native brush Undo did not restore the image")
        call("redo", count=stroke["undo_steps"])
        if snapshot("redo-cleanup.png") != after_cleanup:
            raise RuntimeError("Native brush Redo differed from the painted image")
        if snapshot("reference-layer-after.png", "Original - untouched") != source_pixels:
            raise RuntimeError("The preserved original layer changed")
        if hashlib.sha256(source.read_bytes()).hexdigest() != input_sha:
            raise RuntimeError("Source file changed")
        call("save_document", path=str(output / "knight-workflow.kra"))
        call("export_document", path=str(output / "knight-workflow.png"))
        receipt = {"krita_version": status["krita_version"],
                   "plugin_version": status["plugin_version"],
                   "source": str(source), "source_sha256": input_sha,
                   "source_file_preserved": True, "original_layer_preserved": True,
                   "transform_visible": before_transform != after_transform,
                   "transform_undo_redo_exact": True, "brush_undo_redo_exact": True,
                   "extraction_undo_steps": extraction["undo_steps"],
                   "acceptance": "TECHNICAL_PASS_NEEDS_HUMAN_ART_REVIEW", "calls": calls}
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print("Saved " + str(output / "knight-workflow.kra"))
    finally:
        client.close()


if __name__ == "__main__":
    main()
