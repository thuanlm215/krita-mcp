#!/usr/bin/env python3
"""Stress one close strategy until Krita dies (or the run completes).

Each iteration builds a document with a history like the integration suite's
-- draw, filter, geometry, save, export -- then closes it with the chosen
strategy and immediately asks for status, which is what exposed the crash.

    python scripts/run_isolated_tests.py -- python scripts/stress_close.py 8

Exit code 0 means it survived every iteration.
"""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mcp_server import BRIDGE, BridgeError, BridgeUnavailable

NAME = "stress doc"

GROUPS = os.environ.get("STRESS_GROUPS", "layers,restack,errors,reads").split(",")


def build(tmpdir, index):
    if "restack" in GROUPS or "layers" in GROUPS:
        pass
    return _build(tmpdir, index)


def _restack(name):
    """The layer surgery the integration suite performs."""
    BRIDGE.call("duplicate_layer", {"document": name, "layer": "Art",
                                    "name": "Art copy"}, timeout=60)
    BRIDGE.call("move_layer", {"document": name, "layer": "Art copy",
                               "parent": "G", "index": 0}, timeout=60)
    BRIDGE.call("delete_layer", {"document": name, "layer": "G/Art copy"},
                timeout=60)


def _errors(name):
    """Operations that fail, to leave failed-call debris behind."""
    for params, op in (
        ({"document": name, "commands": [{"type": "banana"}]}, "draw"),
        ({"document": name, "commands": [{"type": "rect", "x": 0, "y": 0}]},
         "draw"),
        ({"document": name, "x": 99999, "y": 99999}, "get_pixel"),
        ({"document": "does-not-exist"}, "document_info"),
        ({"document": name, "layer": "no such layer", "opacity": 100},
         "set_layer"),
        ({"document": name, "filter": "not-a-filter"}, "apply_filter"),
    ):
        try:
            BRIDGE.call(op, params, timeout=30)
        except BridgeError:
            pass


def _reads(name):
    BRIDGE.call("get_image", {"document": name, "max_size": 200})
    BRIDGE.call("get_image", {"document": name, "layer": "Art",
                              "region": {"x": 0, "y": 0, "width": 60,
                                         "height": 60}})
    BRIDGE.call("get_pixel", {"document": name, "x": 5, "y": 5})
    BRIDGE.call("status", {})


def _build(tmpdir, index):
    BRIDGE.call("create_document", {"width": 300, "height": 220,
                                    "name": NAME, "background": "#203040"})
    BRIDGE.call("create_layer", {"document": NAME, "name": "Art"})
    BRIDGE.call("create_layer", {"document": NAME, "name": "G",
                                 "type": "grouplayer"})
    BRIDGE.call("create_layer", {"document": NAME, "name": "Nested",
                                 "parent": "G"})
    BRIDGE.call("draw", {"document": NAME, "layer": "Art", "commands": [
        {"type": "fill_rect", "x": 0, "y": 0, "w": 300, "h": 220,
         "color": "#ffffff"},
        {"type": "rect", "x": 10, "y": 10, "w": 90, "h": 60, "fill": "#e94f37",
         "color": "#111111", "stroke_width": 3},
        {"type": "ellipse", "x": 120, "y": 10, "w": 80, "h": 60,
         "fill": "#3f88c5"},
        {"type": "text", "x": 12, "y": 150, "text": "stress", "size": 22,
         "color": "#111111"},
    ]})
    if "restack" in GROUPS:
        _restack(NAME)
    if "reads" in GROUPS:
        _reads(NAME)
    BRIDGE.call("apply_filter", {"document": NAME, "layer": "Art",
                                 "filter": "invert"}, timeout=60)
    BRIDGE.call("apply_filter", {"document": NAME, "layer": "Art",
                                 "filter": "blur",
                                 "region": {"x": 0, "y": 0, "width": 150,
                                            "height": 120}}, timeout=60)
    BRIDGE.call("set_selection", {"document": NAME, "mode": "rect", "x": 5,
                                  "y": 5, "width": 40, "height": 40})
    BRIDGE.call("set_selection", {"document": NAME, "mode": "none"})
    BRIDGE.call("crop_image", {"document": NAME, "x": 0, "y": 0,
                               "width": 220, "height": 160}, timeout=60)
    BRIDGE.call("scale_image", {"document": NAME, "width": 150,
                                "height": 110}, timeout=90)
    BRIDGE.call("rotate_image", {"document": NAME, "degrees": 90},
                timeout=60)
    BRIDGE.call("flatten_image", {"document": NAME}, timeout=60)

    png = os.path.join(tmpdir, "s{0}.png".format(index))
    kra = os.path.join(tmpdir, "s{0}.kra".format(index))
    BRIDGE.call("export_document", {"document": NAME, "path": png},
                timeout=90)
    BRIDGE.call("save_document", {"document": NAME, "path": kra}, timeout=90)
    opened = BRIDGE.call("open_document", {"path": png}, timeout=90)
    BRIDGE.call("close_document", {"document": opened["name"],
                                   "discard_changes": True}, timeout=60)
    if "errors" in GROUPS:
        _errors(NAME)
    # dirty it again, exactly as the suite does before its final close
    BRIDGE.call("set_layer", {"document": NAME, "blending_mode": "multiply"})


def main():
    strategy = 'close_document'
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    tmpdir = tempfile.mkdtemp(prefix="krita-stress-")
    print("strategy: {0}, {1} iterations, groups: {2}".format(
        strategy, iterations, ",".join(GROUPS)))

    for i in range(1, iterations + 1):
        try:
            build(tmpdir, i)
            BRIDGE.call("close_document", {"document": NAME,
                                           "discard_changes": True}, timeout=90)
            # the call that used to read freed memory
            status = BRIDGE.call("status", {}, timeout=15)
            still = [d for d in status["documents"] if d.get("name") == NAME]
            if still:
                # the deferred strategy needs a beat before it takes effect
                time.sleep(0.5)
                status = BRIDGE.call("status", {}, timeout=15)
                still = [d for d in status["documents"]
                         if d.get("name") == NAME]
            print("  iteration {0}: ok ({1} docs open, target closed={2})"
                  .format(i, status["open_document_count"], not still))
        except BridgeUnavailable:
            print("  iteration {0}: KRITA DIED".format(i))
            return 2
        except BridgeError as exc:
            print("  iteration {0}: error {1}: {2}".format(i, exc.kind,
                                                           str(exc)[:200]))
            return 3

    print("survived all {0} iterations".format(iterations))
    return 0


if __name__ == "__main__":
    sys.exit(main())
