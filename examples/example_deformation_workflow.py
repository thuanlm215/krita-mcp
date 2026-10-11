#!/usr/bin/env python3
"""Create grid/curve and organic-silhouette warp demos through MCP tools only.

Run safely with run_isolated_tests.py, leaving the desktop document untouched:
python scripts/run_isolated_tests.py --output artifacts/implementation/demo -- \
    python examples/example_deformation_workflow.py --output artifacts/implementation/demo
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_mcp import Client, PROTOCOL, _image_of


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/local-warp-demo"))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for filename in ("grid.kra", "silhouette.kra"):
        if (output / filename).exists():
            raise SystemExit("Choose a new output directory; demo KRA already exists")
    client = Client()
    client.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "local-warp-demo", "version": "1"}})
    client.notify("notifications/initialized")
    receipts = []
    try:
        def call(tool, **arguments):
            payload, result = client.ok(tool, arguments)
            receipts.append({"tool": tool, "arguments": arguments, "result": payload})
            return payload, result

        def image(doc, filename, layer="merged"):
            _, result = call("get_image", document=doc, layer=layer, max_size=512)
            raw = base64.b64decode(_image_of(result)["data"])
            (output / filename).write_bytes(raw)
            return raw

        for kind in ("grid", "silhouette"):
            doc = "MCP local warp " + kind + " " + uuid.uuid4().hex[:8]
            call("create_document", width=256, height=192, name=doc)
            call("set_layer", document=doc, layer="Background", visible=False)
            call("create_layer", document=doc, name="Source")
            if kind == "grid":
                commands = [{"type": "fill_rect", "x": 16, "y": 16, "w": 224, "h": 160, "color": "#203844"}]
                commands += [{"type": "line", "x1": x, "y1": 16, "x2": x, "y2": 176,
                              "color": "#6b9baa", "stroke_width": 1} for x in range(16, 241, 16)]
                commands += [{"type": "line", "x1": 16, "y1": y, "x2": 240, "y2": y,
                              "color": "#6b9baa", "stroke_width": 1} for y in range(16, 177, 16)]
                commands += [{"type": "polyline", "points": [[24, 145], [48, 125], [72, 80], [100, 58],
                              [128, 62], [156, 92], [188, 116], [232, 120]], "color": "#ffbf55", "stroke_width": 4}]
                warp = {"center": {"x": 128, "y": 96}, "offset": {"x": 14, "y": -8}, "radius": 68}
            else:
                # A small authored organic profile, unrelated to the user's
                # cat or the earlier shoulder/arm source asset.
                commands = [{"type": "polygon", "points": [[35, 120], [38, 85], [51, 60], [72, 44],
                             [99, 40], [126, 48], [143, 64], [157, 77], [188, 87], [196, 103],
                             [180, 113], [162, 114], [156, 133], [141, 153], [116, 163],
                             [84, 164], [59, 151], [43, 137]], "fill": "#dd9a57", "stroke_width": 0},
                            {"type": "polyline", "points": [[64, 131], [68, 97], [89, 70], [117, 68], [136, 83]],
                             "color": "#845637", "stroke_width": 3},
                            {"type": "circle", "cx": 145, "cy": 91, "radius": 4,
                             "fill": "#3d3028", "stroke_width": 0}]
                warp = {"center": {"x": 190, "y": 101}, "offset": {"x": -14, "y": -5}, "radius": 64}
            call("draw", document=doc, layer="Source", commands=commands)
            source_before = image(doc, kind + "-source-before.png", "Source")
            before = image(doc, kind + "-before.png")
            checkpoint, _ = call("preview_changes", document=doc)
            params = dict(warp, document=doc, layer="Source", name="Edited", max_size=512)
            preview, response = call("warp_layer", **params, mode="preview")
            preview_bytes = base64.b64decode(_image_of(response)["data"])
            (output / (kind + "-preview.png")).write_bytes(preview_bytes)
            applied, _ = call("warp_layer", **params, mode="apply")
            after = image(doc, kind + "-after.png")
            assert applied["patch_sha256"] == preview["patch_sha256"]
            assert after == preview_bytes, "Preview must match applied Krita composite"
            assert after != before, "Local correction must be visible"
            assert source_before == image(doc, kind + "-source-after.png", "Source")
            _, comparison = call("preview_changes", document=doc, mode="compare",
                                 checkpoint=checkpoint["checkpoint"], max_size=1536)
            (output / (kind + "-comparison.png")).write_bytes(base64.b64decode(_image_of(comparison)["data"]))
            call("preview_changes", document=doc, mode="discard", checkpoint=checkpoint["checkpoint"])
            call("undo", document=doc)
            assert image(doc, kind + "-undo.png") == before
            call("redo", document=doc)
            assert image(doc, kind + "-redo.png") == after
            call("save_document", document=doc, path=str(output / (kind + ".kra")))
            call("export_document", document=doc, path=str(output / (kind + ".png")))
            print(kind, "changed pixels:", applied["changed_pixels"], "source PNG sha256:",
                  hashlib.sha256(source_before).hexdigest(), flush=True)
    finally:
        output.joinpath("receipt.json").write_text(json.dumps(receipts, indent=2) + "\n")
        client.close()


if __name__ == "__main__":
    main()
