#!/usr/bin/env python3
"""MCP stdio server that drives Krita through the in-app HTTP bridge.

The server uses the Python standard library only. `krita-mcp install` copies
the Krita plugin into the local resource folder.

Transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, so stdout is
reserved exclusively for protocol messages -- all logging goes to stderr.

Run `krita-mcp --selftest` or `python mcp_server.py --selftest` to exercise
the bridge from a shell.
"""

import argparse
import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from painter_schema import STROKE,TARGET,FRAMES

SERVER_NAME = "krita"
SERVER_VERSION = "1.4.1"

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL = "2025-06-18"

HEALTH_TIMEOUT = 3.0
CALL_TIMEOUT = 315.0  # backstop only; the bridge enforces its own per-op limit


def log(message):
    sys.stderr.write("[krita-mcp] {0}\n".format(message))
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# bridge client
# ---------------------------------------------------------------------------

class BridgeUnavailable(Exception):
    pass


class _RequestNotSent(OSError):
    """TCP connection failed before any HTTP request bytes were sent."""

    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


class BridgeError(Exception):
    """An operation/response error; the execution outcome may be unknown."""

    def __init__(self, kind, message, detail=None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def state_dir():
    """Krita's per-user data directory -- must match the plugin's copy."""
    override = os.environ.get("KRITA_MCP_STATE_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "krita")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/krita")
    base = (os.environ.get("XDG_DATA_HOME")
            or os.path.expanduser("~/.local/share"))
    return os.path.join(base, "krita")


def info_file_path():
    override = os.environ.get("KRITA_MCP_INFO_FILE")
    if override:
        return override
    return os.path.join(state_dir(), "krita_mcp_bridge.json")


NOT_RUNNING_HELP = (
    "Could not reach the Krita MCP bridge.\n"
    "  1. Is Krita running?\n"
    "  2. Is the bridge plugin enabled? Settings > Configure Krita > Python "
    "Plugin Manager > 'MCP Bridge', then restart Krita.\n"
    "  3. Check Tools > Scripts > 'MCP Bridge Status...' inside Krita.\n"
    "Connection details are read from: {path}"
)


class BridgeClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._info = None

    # -- discovery --------------------------------------------------------
    def _load_info(self, force=False):
        if self._info is not None and not force:
            return self._info

        host = os.environ.get("KRITA_MCP_HOST", "127.0.0.1")
        port = os.environ.get("KRITA_MCP_PORT")
        token = os.environ.get("KRITA_MCP_TOKEN")
        if port and token:
            self._info = {"host": host, "port": int(port), "token": token}
            return self._info

        path = info_file_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise BridgeUnavailable(NOT_RUNNING_HELP.format(path=path))
        except (OSError, ValueError) as exc:
            raise BridgeUnavailable(
                "The bridge file at {0} could not be read ({1}). Restart Krita "
                "to rewrite it.".format(path, exc))

        if not data.get("port") or not data.get("token"):
            raise BridgeUnavailable(
                "The bridge file at {0} is missing the port or token. Restart "
                "Krita.".format(path))
        self._info = {"host": host, "port": int(data["port"]),
                      "token": data["token"]}
        return self._info

    # -- requests ---------------------------------------------------------
    def _request(self, method, path, body, timeout, info):
        conn = http.client.HTTPConnection(info["host"], info["port"],
                                          timeout=timeout)
        try:
            headers = {"Connection": "close"}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(payload))
                headers["X-Krita-MCP-Token"] = info["token"]
            # Separate connection establishment from sending. Only this
            # failure boundary proves that rediscovery/retry cannot replay
            # an edit. A send/read failure has an unknown execution outcome.
            try:
                conn.connect()
            except OSError as exc:
                raise _RequestNotSent(exc) from exc
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        finally:
            try:
                conn.close()
            except Exception:
                pass

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            raise BridgeError(
                "bad_response",
                "The bridge returned HTTP {0} with a body that is not JSON."
                .format(status),
                raw[:500].decode("utf-8", "replace"))
        return status, parsed

    def call(self, op, params=None, timeout=CALL_TIMEOUT):
        body = {"op": op, "params": params or {}}
        last_exc = None

        # Rediscover once only when no request was sent, or the authenticated
        # bridge rejects it with 401 before dispatch. Never replay an edit
        # after a send/read failure, including timeout/reset OSError subclasses.
        for attempt in (0, 1):
            with self._lock:
                info = self._load_info(force=(attempt == 1))
            try:
                status, parsed = self._request("POST", "/rpc", body, timeout, info)
            except _RequestNotSent as exc:
                last_exc = exc.cause
                continue
            except (OSError, http.client.HTTPException) as exc:
                raise BridgeError("incomplete_response", "The bridge response failed during {0}. "
                                  "The operation may have completed; its execution outcome is unknown. "
                                  "Check status and inspect the document before retrying.".format(op),
                                  {"operation": op, "exception": type(exc).__name__,
                                   "execution_outcome": "unknown"}) from exc

            if status == 401 and attempt == 0:
                with self._lock:
                    self._info = None
                continue

            if parsed.get("ok"):
                return parsed.get("result")
            error = parsed.get("error") or {}
            raise BridgeError(error.get("type", "error"),
                              error.get("message", "The operation failed."),
                              error.get("detail"))

        raise BridgeUnavailable(
            "{0}\n\nUnderlying error: {1}: {2}".format(
                NOT_RUNNING_HELP.format(path=info_file_path()),
                type(last_exc).__name__, last_exc))

    def health(self):
        with self._lock:
            info = self._load_info(force=True)
        try:
            status, parsed = self._request("GET", "/health", None,
                                           HEALTH_TIMEOUT, info)
        except OSError as exc:
            raise BridgeUnavailable(
                "{0}\n\nUnderlying error: {1}: {2}".format(
                    NOT_RUNNING_HELP.format(path=info_file_path()),
                    type(exc).__name__, exc))
        if status != 200 or not parsed.get("ok"):
            raise BridgeUnavailable(
                "The bridge answered HTTP {0}: {1}".format(status, parsed))
        return parsed


BRIDGE = BridgeClient()


# ---------------------------------------------------------------------------
# tool definitions
# ---------------------------------------------------------------------------

DOCUMENT_PROP = {
    "type": ["string", "integer"],
    "description": ("Which open document: its index from `status`, part of its "
                    "name or file name, or omit for the active one."),
}

LAYER_PROP = {
    "type": "string",
    "description": ("Which layer: a name (\"Sky\"), a path through groups "
                    "(\"Background/Sky\"), a top-first index path (\"#0/#1\"), "
                    "\"uuid:<id>\", or omit for the active layer."),
}

REGION_PROP = {
    "type": "object",
    "description": "Pixel rectangle on the canvas.",
    "properties": {
        "x": {"type": "integer", "default": 0},
        "y": {"type": "integer", "default": 0},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
    },
    "required": ["width", "height"],
}

DRAW_COMMAND = {
    "type": "object",
    "description": (
        "One drawing primitive. `type` decides which other fields apply:\n"
        "- fill_rect: x, y, w, h, color\n"
        "- rect: x, y, w, h, plus optional fill, color (outline), "
        "stroke_width, radius (rounded corners)\n"
        "- ellipse: x, y, w, h (bounding box), fill, color, stroke_width\n"
        "- circle: cx, cy, radius, fill, color, stroke_width\n"
        "- line: x1, y1, x2, y2, color, stroke_width, cap (flat|square|round)\n"
        "- polyline / polygon: points [[x,y],...], color, fill, stroke_width, "
        "close\n"
        "- ink: points [[x,y,width],...] (width optional, else stroke_width), "
        "color, smooth (default true): a filled line whose width varies per "
        "point, for tapered inking\n"
        "- text: x, y, text, size (pixels), color, font, bold, italic, "
        "anchor (top-left|baseline|center); \\n starts a new line\n"
        "- linear_gradient: x, y, w, h (fill box), stops [[0,\"#000\"],[1,\"#fff\"]], "
        "direction (horizontal|vertical|diagonal), or x1, y1, x2, y2 for any axis\n"
        "- radial_gradient: stops plus either x, y, w, h (ellipse inscribed in "
        "the box) or cx, cy, radius (or rx, ry, angle) to fill only that ellipse\n"
        "- image: x, y, data (base64 PNG/JPEG) or path (absolute file path); "
        "w, h optional, natural size if both omitted\n"
        "- svg: x, y, svg (markup) or path (absolute .svg file); w, h as for image\n"
        "- clear: x, y, w, h to erase back to transparency\n"
        "Colours accept #rrggbb, #rrggbbaa, SVG names, or [r,g,b,a]."
    ),
    "properties": {
        "type": {
            "type": "string",
            "enum": ["fill_rect", "rect", "ellipse", "circle", "line",
                     "polyline", "polygon", "text", "linear_gradient",
                     "radial_gradient", "image", "clear"],
        },
        "x": {"type": "number"}, "y": {"type": "number"},
        "w": {"type": "number"}, "h": {"type": "number"},
        "x1": {"type": "number"}, "y1": {"type": "number"},
        "x2": {"type": "number"}, "y2": {"type": "number"},
        "cx": {"type": "number"}, "cy": {"type": "number"},
        "radius": {"type": "number"},
        "rx": {"type": "number"}, "ry": {"type": "number"},
        "angle": {"type": "number", "description": "Degrees, clockwise."},
        "color": {"type": ["string", "array"]},
        "fill": {"type": ["string", "array"]},
        "stroke_width": {"type": "number", "default": 1},
        "cap": {"type": "string", "enum": ["flat", "square", "round"]},
        "join": {"type": "string", "enum": ["miter", "bevel", "round"]},
        "close": {"type": "boolean"},
        "points": {"type": "array", "items": {
            "type": "array", "items": {"type": "number"},
            "minItems": 2, "maxItems": 3},
            "description": "[x, y] pairs; ink also takes [x, y, width]."},
        "smooth": {"type": "boolean", "default": True,
                   "description": "ink: pass a Catmull-Rom curve through the points."},
        "svg": {"type": "string", "description": "SVG markup for an svg command."},
        "text": {"type": "string"},
        "size": {"type": "integer"},
        "font": {"type": "string"},
        "bold": {"type": "boolean"},
        "italic": {"type": "boolean"},
        "anchor": {"type": "string",
                   "enum": ["top-left", "baseline", "center"]},
        "direction": {"type": "string",
                      "enum": ["horizontal", "vertical", "diagonal"]},
        "stops": {"type": "array", "items": {"type": "array"}},
        "data": {"type": "string", "description": "base64 image bytes"},
        "path": {"type": "string",
                 "description": "Absolute PNG/JPEG/WebP/BMP path for image, or .svg "
                                "path for svg; instead of data or svg markup"},
    },
    "required": ["type"],
}


BRUSH_POINTS = {
    "type": "array", "minItems": 2, "maxItems": 128,
    "items": {"type": "object", "properties": {
        "x": {"type": "number"}, "y": {"type": "number"},
        "pressure": {"type": "number", "minimum": 0, "maximum": 1, "default": 1},
        "tilt_x": {"type": "number", "minimum": -60, "maximum": 60},
        "tilt_y": {"type": "number", "minimum": -60, "maximum": 60},
        "timestamp_ms": {"type": "number", "minimum": 0,
                         "description": "Increasing sample time; default 16 ms spacing."}},
        "required": ["x", "y"], "additionalProperties": False},
}

ALPHA_LOCKED_PROP = {
    "type": "boolean",
    "description": "Paint changes colour only; transparency stays as it is.",
}

INHERIT_ALPHA_PROP = {
    "type": "boolean",
    "description": "Clip this layer to the combined alpha of the layers below it.",
}

PARAMS_FILE_PROP = {
    "type": "string",
    "description": ("Absolute path to a .json file holding an object of this "
                    "tool's arguments; inline arguments override it. Keeps "
                    "large commands, points or edits out of the conversation."),
}

SAVE_PATH_PROP = {
    "type": "string",
    "description": ("Absolute .png path. The image is written there and left "
                    "out of the reply; the text result names the file."),
}


def tool(name, description, properties, required=None, op=None, image=False,
         transform=None):
    properties = dict(properties, params_file=PARAMS_FILE_PROP)
    if image:
        properties["save_path"] = SAVE_PATH_PROP
    required = list(required or [])
    # Required arguments may arrive in params_file, so the server checks them
    # after merging rather than letting a client reject the call.
    if required:
        description = "{0} Required, inline or in params_file: {1}.".format(
            description, ", ".join(required))
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": [],
            "additionalProperties": False,
        },
        "_op": op or name,
        "_image": image,
        "_transform": transform,
        "_required": required,
    }


def _transform_image(args):
    action = args.pop("action")
    mapping = {
        "resize_canvas": "resize_canvas",
        "scale": "scale_image",
        "rotate": "rotate_image",
        "crop": "crop_image",
        "flatten": "flatten_image",
    }
    if action not in mapping:
        raise BridgeError("invalid_request",
                          "action must be one of: " + ", ".join(mapping))
    return mapping[action], args


def _list_capabilities(args):
    kind = args.pop("kind", "filters")
    if kind == "filters":
        return "list_filters", args
    if kind == "blending_modes":
        return "list_blending_modes", args
    if kind == "workflow":
        return "workflow_capabilities", args
    raise BridgeError("invalid_request",
                      "kind must be 'filters', 'blending_modes' or 'workflow'")


TOOLS = [
    tool('import_animation','Import validated ordered PNGs into an owned native animation, preserving padding, FPS and anchor annotation.',
         {'name':{'type':'string','minLength':1,'maxLength':128},'frames':FRAMES,'fps':{'type':'integer','minimum':1,'maximum':120},
          'anchor':{'type':'object','properties':{'x':{'type':'number'},'y':{'type':'number'}},'required':['x','y'],'additionalProperties':False},
          'end':{'type':'integer','minimum':0,'maximum':99999}},
         required=['name','frames','fps','anchor']),
    tool('animation_review','Aligned previous/current/next clean frames or ROI; preserves source timeline/history and returns registration.',
         {'document':DOCUMENT_PROP,'layer':LAYER_PROP,'time':{'type':'integer'},'region':REGION_PROP,'max_size':{'type':'integer'}},
         required=['document','time'],image=True),
    tool('prepare_repair','Freeze an explicit baseline/allowed mask for at most three bounded repair alternatives; source unchanged.',
         {'target':TARGET,'region':REGION_PROP,'allowed_mask':{'type':'string'},'target_mask':{'type':'string'},
          'region_mode':{'type':'string','enum':['roi','whole_frame'],'default':'roi'},
          'defect':{'type':'string'},'intended_change':{'type':'string'}},
         required=['target','region','allowed_mask','defect','intended_change']),
    tool('render_repair','Render a frozen replacement or native stroke/clone/patch/warp on an owned static copy; source unchanged.',
         {'transaction':{'type':'string'},'edit':{'type':'object'}},required=['transaction','edit']),
    tool('inspect_repair','Inspect one frozen preview image with small metadata for both views. Panel is scaled; before/after are exact-resolution transparent PNGs.',
         {'transaction':{'type':'string'},'candidate':{'type':'string'},'max_size':{'type':'integer'},
          'view':{'type':'string','enum':['composite','raw_layer'],'default':'composite'},
          'image':{'type':'string','enum':['panel','before','after'],'default':'panel',
                   'description':'Select one image content block. before/after retain full resolution and alpha; max_size applies only to panel.'}},required=['transaction','candidate'],image=True),
    tool('commit_repair','Atomically apply one frozen candidate with dependency checks and idempotent outcome; one native Undo.',
         {'transaction':{'type':'string'},'candidate':{'type':'string'}},required=['transaction','candidate']),
    tool('repair_status','Read session-local transaction outcome after an ambiguous response; no replay.',
         {'transaction':{'type':'string'}},required=['transaction']),
    tool('discard_repair','Release a prepared repair baseline and its candidates without source changes.',
         {'transaction':{'type':'string'}},required=['transaction']),
    tool('sample_colors','Bounded sRGB region statistics: linear-light alpha-weighted mean and deterministic 4-bit palette.',
         {'document':DOCUMENT_PROP,'layer':LAYER_PROP,'region':REGION_PROP,'time':{'type':'integer','minimum':0},
          'palette_size':{'type':'integer','minimum':1,'maximum':16}},required=['document']),
    tool('compare_images','Aligned before/after/difference/protected-violation panels, mask IoU/coverage and Manhattan boundary distance.',
         {'before':{'type':'string'},'after':{'type':'string'},'allowed_mask':{'type':'string'},'target_mask':{'type':'string'},
          'offset':{'type':'object'},'landmarks':{'type':'array'},'alpha_threshold':{'type':'integer','minimum':1,'maximum':255},
          'max_size':{'type':'integer','minimum':64,'maximum':2048}},required=['before','after'],image=True),
    tool('plan_stroke','Plan version 1 polyline/cubic geometry and arc-length pressure/time samples; no painting.',
         {'spec':STROKE},required=['spec']),
    tool('paint_stroke','Execute a validated stroke plan as one native Krita stroke, restoring brush UI state.',
         {'spec':STROKE},required=['spec']),
    tool("status",
         "Krita's version plus every open document (index, size, colour "
         "space, unsaved state). Start here to see what you are working with.",
         {}),

    tool("dismiss_recovery_dialog",
         "Cancel Krita's active autosave Recover Files dialog, keeping recovery files. "
         "Uses native dialog identity; refuses every other modal, including save-changes prompts. "
         "No document edits. Safe no-op when no modal is open.",
         {}),

    tool("inspect_document",
         "Full detail for one document: canvas size, resolution, colour "
         "space, the whole layer tree in docker order (top first), and the "
         "current selection.",
         {"document": DOCUMENT_PROP,
          "max_depth": {"type": "integer", "default": -1,
                        "description": "Group nesting to descend; -1 for all."}},
         op="document_info"),

    tool("create_document",
         "Create a new document and open it in a Krita view.",
         {"width": {"type": "integer"},
          "height": {"type": "integer"},
          "name": {"type": "string", "default": "Untitled"},
          "resolution_dpi": {"type": "number", "default": 300},
          "color_model": {"type": "string", "default": "RGBA",
                          "description": "RGBA, GRAYA, CMYKA, LABA, XYZA, YCbCrA"},
          "color_depth": {"type": "string", "default": "U8",
                          "description": "U8, U16, F16 or F32. Drawing needs U8."},
          "color_profile": {"type": "string", "default": "",
                            "description": "Empty for Krita's default."},
          "background": {"type": ["string", "array"],
                         "description": "Optional fill colour, e.g. #ffffff."}},
         required=["width", "height"]),

    tool("open_document",
         "Open an image file from disk in Krita.",
         {"path": {"type": "string", "description": "Absolute path."}},
         required=["path"]),

    tool("save_document",
         "Save a document. Give `path` to save-as (the format follows the "
         "extension); omit it to save over the existing file.",
         {"document": DOCUMENT_PROP,
          "path": {"type": "string"}}),

    tool("export_document",
         "Export a flattened copy to any format Krita can write (.png, .jpg, "
         ".webp, .tif, ...) without changing what the document is bound to.",
         {"document": DOCUMENT_PROP,
          "path": {"type": "string", "description": "Absolute output path."},
          "options": {"type": "object",
                      "description": "Exporter settings, e.g. {\"quality\": 90}."}},
         required=["path"]),

    tool("close_document",
         "Close a document. Refuses to discard unsaved work unless you say "
         "so. CAUTION: Krita 5.3.3 has a bug where tearing down a "
         "heavily-edited document sometimes crashes Krita itself (roughly one "
         "close in five after a long editing session; it happens through "
         "Krita's own File > Close too, and saving first does not help). "
         "Prefer leaving documents open and letting the user close them.",
         {"document": DOCUMENT_PROP,
          "save": {"type": "boolean", "default": False},
          "discard_changes": {"type": "boolean", "default": False}}),

    tool("transform_image",
         "Whole-image geometry. `action` picks the operation: resize_canvas "
         "(change the canvas, keeping layer pixels where they are), scale "
         "(resample everything), rotate (by degrees, clockwise), crop, or "
         "flatten (merge all layers into one).",
         {"document": DOCUMENT_PROP,
          "action": {"type": "string",
                     "enum": ["resize_canvas", "scale", "rotate", "crop",
                              "flatten"]},
          "x": {"type": "integer"}, "y": {"type": "integer"},
          "width": {"type": "integer"}, "height": {"type": "integer"},
          "degrees": {"type": "number"},
          "strategy": {"type": "string", "default": "Bicubic",
                       "description": "Scaling filter: Bicubic, Bilinear, "
                                      "NearestNeighbor, Lanczos3, Box."}},
         required=["action"], transform=_transform_image),

    tool("create_layer",
         "Add a layer. New layers go to the top of their parent unless you "
         "pass `index` (0 is topmost, negative counts from the bottom). A "
         "filelayer with `path` links an image file and follows its changes.",
         {"document": DOCUMENT_PROP,
          "name": {"type": "string", "default": "Layer"},
          "type": {"type": "string", "default": "paintlayer",
                   "enum": ["paintlayer", "grouplayer", "filterlayer",
                            "filllayer", "filelayer", "clonelayer",
                            "vectorlayer", "transparencymask", "filtermask",
                            "transformmask", "selectionmask", "colorizemask"]},
          "parent": {"type": "string",
                     "description": "A group layer to nest inside; omit for "
                                    "the top level."},
          "index": {"type": "integer"},
          "path": {"type": "string",
                   "description": "filelayer: absolute path of the image file to link."},
          "scaling": {"type": "string", "enum": ["none", "to_image_size", "to_ppi"],
                      "default": "none", "description": "filelayer: how to fit the file."},
          "select": {"type": "boolean", "default": True},
          "opacity": {"type": "number"},
          "blending_mode": {"type": "string"},
          "visible": {"type": "boolean"},
          "locked": {"type": "boolean"},
          "alpha_locked": ALPHA_LOCKED_PROP,
          "inherit_alpha": INHERIT_ALPHA_PROP}),

    tool("set_layer",
         "Change a layer's properties: rename it, set opacity (0-255, or 0-1), "
         "blending mode, visibility, lock, alpha lock, inherit alpha (clip to "
         "the layers below), and optionally make it active.",
         {"document": DOCUMENT_PROP,
          "layer": LAYER_PROP,
          "new_name": {"type": "string"},
          "opacity": {"type": "number"},
          "blending_mode": {"type": "string",
                            "description": "See list_capabilities."},
          "visible": {"type": "boolean"},
          "locked": {"type": "boolean"},
          "alpha_locked": ALPHA_LOCKED_PROP,
          "inherit_alpha": INHERIT_ALPHA_PROP,
          "select": {"type": "boolean", "default": False,
                     "description": "Also make this the active layer."}}),

    tool("delete_layer", "Delete a layer and everything inside it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP},
         required=["layer"]),

    tool("duplicate_layer", "Copy a layer, placing the copy just above it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "name": {"type": "string"},
          "select": {"type": "boolean", "default": True}}),

    tool("move_layer",
         "Restack a layer, optionally into a different group. `index` is "
         "top-first within the new parent.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "parent": {"type": "string",
                     "description": "Target group; omit to stay put."},
          "index": {"type": "integer", "default": 0}},
         required=["layer"]),

    tool("merge_layer_down", "Merge a layer into the one directly beneath it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP}),

    tool("get_image",
         "Render the canvas and return it as a PNG you can actually look at. "
         "Use this to check your work. Defaults to the merged image; pass "
         "`layer` for one layer in isolation.",
         {"document": DOCUMENT_PROP,
          "layer": {"type": "string",
                    "description": "Omit or \"merged\" for the composite."},
          "region": REGION_PROP,
          "time": {'type':'integer','minimum':0,'description':'Explicit raster playback time; restored without history. Onion skins excluded.'},
          "max_size": {"type": "integer", "default": 1024,
                       "description": "Longest edge of the returned image; "
                                      "the canvas is never upscaled."}},
         image=True),

    tool("get_pixel", "Read the exact colour at one pixel.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "x": {"type": "integer"}, "y": {"type": "integer"}},
         required=["x", "y"]),

    tool("draw",
         "Paint shapes, text, gradients and bitmaps onto an RGBA/8-bit paint "
         "layer. Commands are drawn in order, so later ones sit on top. "
         "Records one native Undo command using the Linux/Krita 6.0.4 helper. "
         "Supports static layers. Respects the active "
         "selection including feathered alpha. Use draw_brush for native strokes.",
         {"document": DOCUMENT_PROP,
          "layer": LAYER_PROP,
          "antialias": {"type": "boolean", "default": True},
          "commands": {"type": "array", "items": DRAW_COMMAND,
                       "minItems": 1}},
         required=["commands"]),

    tool("list_brushes", "List native brush preset names, optionally filtered by query.",
         {"document": DOCUMENT_PROP, "query": {"type": "string"}}),

    tool("draw_brush",
         "Draw using Krita's actual brush engine, preset, size, opacity, eraser "
         "and selection. Pressure/continuous modes keep one native stroke across "
         "pressure, tilt and timestamp samples with one Undo. Requires the native helper. "
         "Segments mode retains independent lines. Path mode "
         "paints a connected path at pressure 1 with one Undo entry. Pass `strokes` "
         "instead of `points` to paint up to 64 strokes in one call and one Undo; "
         "each may set its own color, size, opacity and erase. Brush UI "
         "settings are restored. Use an unmasked Cleanup layer above transformed parts.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "preset": {"type": "string", "description": "Exact name from list_brushes."},
          "size": {"type": "number", "minimum": 0.1, "maximum": 2048, "default": 20},
          "opacity": {"type": "number", "minimum": 0, "maximum": 1, "default": 1},
          "color": {"type": ["string", "array"], "default": "#000000"},
          "erase": {"type": "boolean", "default": False},
          "mode": {"type": "string", "enum": ["pressure", "continuous", "segments", "path"], "default": "pressure"},
          "points": BRUSH_POINTS,
          "strokes": {"type": "array", "minItems": 1, "maxItems": 64,
                      "description": "Several strokes in one Undo; unset settings "
                                     "inherit the top-level ones.",
                      "items": {"type": "object", "properties": {
                          "points": BRUSH_POINTS,
                          "color": {"type": ["string", "array"]},
                          "size": {"type": "number", "minimum": 0.1, "maximum": 2048},
                          "opacity": {"type": "number", "minimum": 0, "maximum": 1},
                          "erase": {"type": "boolean"}},
                          "required": ["points"], "additionalProperties": False}},
          "clamp": {"type": "boolean", "default": False,
                    "description": "Pull off-canvas points onto the nearest edge "
                                   "instead of rejecting the call."}}),

    tool("select_polygon",
         "Set a closed polygon/lasso selection in document pixels; supports "
         "antialias, feather and replace/add/subtract/intersect. Creates one "
         "native Undo entry.",
         {"document": DOCUMENT_PROP,
          "points": {"type": "array", "minItems": 3, "maxItems": 2048,
                     "items": {"type": "array", "items": {"type": "number"},
                               "minItems": 2, "maxItems": 2}},
          "combine": {"type": "string", "enum": ["replace", "add", "subtract", "intersect"],
                      "default": "replace"},
          "antialias": {"type": "boolean", "default": True},
          "value": {"type": "integer", "minimum": 0, "maximum": 255, "default": 255},
          "feather": {"type": "integer", "minimum": 0, "maximum": 256, "default": 0}},
         required=["points"]),

    tool("selection_to_layer",
         "Extract the active selection from a static, unmasked paint layer "
         "using native copy/cut-to-new-layer. Copy uses one Undo; cut uses two "
         "(cut and new layer). Clearing selection adds one more Undo entry. "
         "Preserves document coordinates. Clears selection "
         "by default so the next brush edit is not unexpectedly clipped.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "name": {"type": "string"},
          "mode": {"type": "string", "enum": ["copy", "cut"], "default": "copy"},
          "clear_selection": {"type": "boolean", "default": True}}),

    tool("transform_layer",
         "Preview or apply an absolute paint/group-layer move/rotate/scale/shear around "
         "a pivot in document pixels. Apply uses a native MCP Transform mask, "
         "preserving original pixels and recording the pose change in Undo. "
         "Repeated calls replace the MCP transform; they do not accumulate. "
         "Preview renders the actual Krita composite on a detached clone.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "mode": {"type": "string", "enum": ["preview", "apply"], "default": "preview"},
          "pivot": {"type": "object", "properties": {
              "x": {"type": "number"}, "y": {"type": "number"}},
                    "required": ["x", "y"], "additionalProperties": False},
          "dx": {"type": "number", "default": 0}, "dy": {"type": "number", "default": 0},
          "degrees": {"type": "number", "default": 0,
                      "description": "Clockwise degrees in document coordinates."},
          "scale_x": {"type": "number", "minimum": -100, "maximum": 100, "default": 1,
                      "description": "Negative values flip; absolute value must be >= 0.01."},
          "scale_y": {"type": "number", "minimum": -100, "maximum": 100, "default": 1},
          "shear_x": {"type": "number", "minimum": -10, "maximum": 10, "default": 0},
          "shear_y": {"type": "number", "minimum": -10, "maximum": 10, "default": 0},
          "strategy": {"type": "string", "enum": ["Bilinear", "Bicubic", "NearestNeighbor", "Lanczos3", "Box"],
                       "default": "Bilinear"},
          "max_size": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 1024}},
         image=True),

    tool("warp_layer",
         "Preview/apply a small local pull on a static unmasked RGBA/U8 paint layer. "
         "Bilinear raster deformation on a new duplicate, preserving and hiding the source. "
         "Actual Krita composite preview changes no source/history; apply is one Undo. "
         "center maps to center+offset with a smooth radius falloff; offset length <= radius/4. "
         "Soft selection weights destination changes. Cannot be used in edit_batch.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "mode": {"type": "string", "enum": ["preview", "apply"], "default": "preview"},
          "center": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                     "required": ["x", "y"], "additionalProperties": False},
          "offset": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                     "required": ["x", "y"], "additionalProperties": False},
          "radius": {"type": "number", "minimum": 2, "maximum": 256},
          "name": {"type": "string", "description": "New paint-layer name on apply."},
          "max_size": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 1024}},
         required=["center", "offset", "radius"], image=True),

    tool("select_content", "Select alpha, matching RGBA colors, a contiguous 4-connected "
         "color region or an imported raster mask. One native Undo. Omit layer for composite sampling.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "mode": {"type": "string", "enum": ["alpha", "color", "contiguous", "mask"], "default": "alpha"},
          "color": {"type": ["string", "array"]}, "data": {"type": "string", "description": "Base64 PNG mask."},
          "data_path": {"type": "string", "description": "Absolute PNG/JPEG/WebP/BMP path; instead of data."},
          "channel": {"type": "string", "enum": ["alpha", "luminance"], "default": "alpha"},
          "x": {"type": "integer"}, "y": {"type": "integer"},
          "threshold": {"type": "integer", "minimum": 0, "maximum": 255, "default": 1},
          "tolerance": {"type": "integer", "minimum": 0, "maximum": 255, "default": 0},
          "feather": {"type": "integer", "minimum": 0, "maximum": 256, "default": 0},
          "combine": {"type": "string", "enum": ["replace", "add", "subtract", "intersect"], "default": "replace"}}),

    tool("texture_patch", "Place texture from a PNG or source layer into a target rectangle, "
         "with opacity and optional PNG alpha/luminance mask. Respects selection, one native Undo.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP, "source_layer": LAYER_PROP,
          "data": {"type": "string", "description": "Base64 image; mutually exclusive with source_layer."},
          "data_path": {"type": "string", "description": "Absolute PNG/JPEG/WebP/BMP path; instead of data."},
          "source_region": REGION_PROP, "region": REGION_PROP,
          "opacity": {"type": "number", "minimum": 0, "maximum": 1, "default": 1},
          "mask_data": {"type": "string", "description": "Base64 PNG matching destination dimensions."},
          "mask_path": {"type": "string", "description": "Absolute PNG/JPEG/WebP/BMP path; instead of mask_data."},
          "mask_channel": {"type": "string", "enum": ["alpha", "luminance"], "default": "alpha"}},
         required=["region"]),

    tool("clone_stamp", "Clone sampled RGBA pixels along a soft pressure-opacity stroke. "
         "Freezes the source at stroke start, respects selection and records one Undo. "
         "Uses a raster compositor; brush presets apply to draw_brush.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP, "source_layer": LAYER_PROP,
          "source": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                     "required": ["x", "y"], "additionalProperties": False},
          "points": {"type": "array", "minItems": 1, "maxItems": 512, "items": {
              "type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"},
              "pressure": {"type": "number", "minimum": 0, "maximum": 1, "default": 1}}, "required": ["x", "y"],
              "additionalProperties": False}},
          "size": {"type": "integer", "minimum": 1, "maximum": 512, "default": 32},
          "softness": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
          "opacity": {"type": "number", "minimum": 0, "maximum": 1, "default": 1}}, required=["source", "points"]),

    tool("edit_batch", "Apply up to 64 edits to one document as one native Undo group. "
         "On error, undo the group and discard its redo. Supports draw/brush, selection, extraction, "
         "transform apply, patch/clone, mask, bake, outline, layer creation, filters and blank/duplicate frames. "
         "A new edit discards an existing redo branch according to Krita's normal history behavior.",
         {"document": DOCUMENT_PROP, "label": {"type": "string"},
          "edits": {"type": "array", "minItems": 1, "maxItems": 64, "items": {
              "type": "object", "properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}},
              "required": ["tool"], "additionalProperties": False}}}, required=["edits"]),

    tool("mask_layer", "Create or replace a static paint/group layer's transparency mask "
         "from selection, a constant fill or a canvas-sized PNG. One native Undo.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP, "mask": LAYER_PROP, "name": {"type": "string"},
          "mode": {"type": "string", "enum": ["selection", "fill", "image"], "default": "selection"},
          "value": {"type": "integer", "minimum": 0, "maximum": 255, "default": 255},
          "data": {"type": "string"},
          "data_path": {"type": "string", "description": "Absolute PNG/JPEG/WebP/BMP path; instead of data."},
          "channel": {"type": "string", "enum": ["alpha", "luminance"], "default": "alpha"}}),

    tool("bake_layer", "Bake the static layer/group projection and masks into a new paint "
         "layer and hide the preserved source. One grouped native Undo.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP, "name": {"type": "string"}}),

    tool("outline_layer", "Trace an antialiased outline around a layer's rendered alpha "
         "onto a new paint layer, as for a sprite or sticker edge. outside goes "
         "under the source and fills beneath it; inside and center go above it. "
         "The source is untouched. One grouped native Undo.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "width": {"type": "number", "minimum": 0.5, "maximum": 32, "default": 2,
                    "description": "Outline thickness in pixels."},
          "color": {"type": ["string", "array"], "default": "#000000"},
          "position": {"type": "string", "enum": ["outside", "inside", "center"],
                       "default": "outside"},
          "antialias": {"type": "boolean", "default": True,
                        "description": "false gives a hard pixel-art outline from alpha >= 128."},
          "name": {"type": "string", "description": "New layer name; default '<layer> outline'."}}),

    tool("export_layers", "Write each layer as its own PNG plus manifest.json with canvas "
         "offsets into a new directory, for sprite parts and game assets. Defaults "
         "to the visible top-level layers. trim crops to exact alpha bounds; padding "
         "adds a transparent margin. Read-only for the document.",
         {"document": DOCUMENT_PROP, "directory": {"type": "string",
                                                   "description": "Absolute path of a new directory."},
          "layers": {"type": "array", "items": LAYER_PROP, "minItems": 1, "maxItems": 256},
          "trim": {"type": "boolean", "default": True,
                   "description": "false exports full-canvas images."},
          "padding": {"type": "integer", "minimum": 0, "maximum": 512, "default": 0}},
         required=["directory"]),

    tool("preview_changes", "Capture a composite checkpoint, compare before/after with a "
         "difference map, or discard it. Panels: before, after, magenta changed pixels. "
         "Read-only for the document; up to 8 checkpoints / 128 MiB.",
         {"document": DOCUMENT_PROP,
          "mode": {"type": "string", "enum": ["checkpoint", "compare", "discard"], "default": "checkpoint"},
          "checkpoint": {"type": "string"},
          "max_size": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 1536}}, image=True),

    tool("animation_frame", "Inspect raster keyframes; create blank/duplicate frames, delete "
         "an exact frame after frame 0, scrub, set FPS/clip range or toggle onion skin. Edits "
         "use native Undo. Initial animation preserves static content at frame 0. Scrub changes viewing time.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "mode": {"type": "string", "enum": ["info", "blank", "duplicate", "delete", "settings", "scrub", "onion"], "default": "info"},
          "time": {"type": "integer", "minimum": 0, "maximum": 100000},
          "source_time": {"type": "integer", "minimum": 0, "maximum": 100000},
          "replace": {"type": "boolean", "default": False}, "enabled": {"type": "boolean"},
          "fps": {"type": "integer", "minimum": 1, "maximum": 120},
          "start": {"type": "integer", "minimum": 0}, "end": {"type": "integer", "minimum": 0}}),

    tool("export_animation", "Export composite frames, transparent atlas and JSON manifest "
         "with FPS, durations, rectangles and anchors to a new directory. Restores viewing time. "
         "Up to 128 frames / 16 megapixel atlas. Existing directories are preserved.",
         {"document": DOCUMENT_PROP, "directory": {"type": "string"},
          "start": {"type": "integer", "minimum": 0}, "end": {"type": "integer", "minimum": 0},
          "step": {"type": "integer", "minimum": 1, "default": 1},
          "columns": {"type": "integer", "minimum": 1, "maximum": 128},
          "anchor": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                     "required": ["x", "y"], "additionalProperties": False}}, required=["directory"]),

    tool("undo", "Undo native Krita history entries on the requested document. "
         "Use the undo_steps returned by a workflow tool; also affects manual history.",
         {"document": DOCUMENT_PROP,
          "count": {"type": "integer", "minimum": 1, "maximum": 128, "default": 1}}),
    tool("redo", "Redo native Krita history entries on the requested document.",
         {"document": DOCUMENT_PROP,
          "count": {"type": "integer", "minimum": 1, "maximum": 128, "default": 1}}),

    tool("apply_filter",
         "Run one of Krita's filters over a layer, optionally limited to a "
         "region. Call list_capabilities first to see names and parameters.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "filter": {"type": "string", "description": "e.g. blur, gaussianblur, "
                                                      "invert, desaturate."},
          "settings": {"type": "object",
                       "description": "Filter parameters; must match the "
                                      "filter's own names."},
          "region": REGION_PROP},
         required=["filter"]),

    tool("set_selection",
         "Change the active selection, which constrains filters and painting.",
         {"document": DOCUMENT_PROP,
          "mode": {"type": "string", "default": "rect",
                   "enum": ["rect", "all", "none", "invert", "grow", "shrink",
                            "feather"]},
          "x": {"type": "integer"}, "y": {"type": "integer"},
          "width": {"type": "integer"}, "height": {"type": "integer"},
          "value": {"type": "integer", "default": 255,
                    "description": "Selection strength 0-255."},
          "add": {"type": "boolean", "default": False,
                  "description": "Union with the existing selection."},
          "amount": {"type": "integer", "default": 1,
                     "description": "Pixels, for grow/shrink/feather."}}),

    tool("list_capabilities",
         "Discover filters with typed editable parameters, curated blending modes, "
         "or workflow support. kind workflow reports Python APIs separately from "
         "the verified native helper/ABI, target-layer restrictions and resource limits "
         "without editing or switching the document.",
         {"kind": {"type": "string", "default": "filters",
                   "enum": ["filters", "blending_modes", "workflow"]},
          "document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "include_parameters": {"type": "boolean", "default": False}},
         transform=_list_capabilities),

    tool("trigger_action",
         "Fire an allowlisted Krita history or selection action. Useful ids: edit_undo, edit_redo, "
         "deselect, select_all, invert_selection.",
         {"name": {"type": "string"}},
         required=["name"]),

    tool("self_test",
         "Verify the bridge end to end: document creation, pixel round-trip, "
         "channel order, drawing, PNG encoding and layer handling. Run this "
         "first if something looks wrong.",
         {}),
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def public_tools():
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in TOOLS]


# ---------------------------------------------------------------------------
# tool execution
# ---------------------------------------------------------------------------

def _image_metadata(value):
    """Keep PNG transport fields out of text, including nested diagnostics.

    QByteArray's typed ``base64`` field is binary parameter data, not an image
    transport field, and must remain available for filter round trips.
    """
    if isinstance(value, dict):
        return {k: _image_metadata(v) for k, v in value.items()
                if not k.endswith('png_base64')}
    if isinstance(value, (list, tuple)):
        return [_image_metadata(v) for v in value]
    return value


def _summarise(result):
    return json.dumps(_image_metadata(result), indent=2, ensure_ascii=False, default=str)


MAX_PARAMS_FILE_BYTES = 32 * 1024 * 1024


def _absolute_file(value, key, suffixes):
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("invalid_request", "{0} must be a non-empty path".format(key))
    path = os.path.expanduser(value.strip())
    if not os.path.isabs(path):
        raise BridgeError("invalid_request", "{0} must be an absolute path".format(key))
    if not path.lower().endswith(suffixes):
        raise BridgeError("invalid_request", "{0} must end with {1}".format(
            key, " or ".join(suffixes)))
    return os.path.normpath(path)


def _merge_params_file(spec, args):
    """Arguments read from params_file, overridden by the inline ones."""
    if "params_file" not in args:
        return args
    path = _absolute_file(args.pop("params_file"), "params_file", (".json",))
    try:
        if os.path.getsize(path) > MAX_PARAMS_FILE_BYTES:
            raise BridgeError("resource_limit", "params_file is larger than {0} MiB"
                              .format(MAX_PARAMS_FILE_BYTES // (1024 * 1024)))
        # utf-8-sig: PowerShell's Set-Content writes a BOM.
        with open(path, "r", encoding="utf-8-sig") as handle:
            loaded = json.load(handle)
    except OSError as exc:
        raise BridgeError("invalid_request", "could not read params_file {0}: {1}"
                          .format(path, exc.strerror or exc))
    except ValueError as exc:
        raise BridgeError("invalid_request", "params_file {0} is not valid JSON: {1}"
                          .format(path, exc))
    if not isinstance(loaded, dict):
        raise BridgeError("invalid_request",
                          "params_file must hold one JSON object of arguments")
    known = spec["inputSchema"]["properties"]
    unknown = sorted(k for k in loaded if k not in known or k == "params_file")
    if unknown:
        raise BridgeError("invalid_request", "{0} does not take {1} (from params_file)"
                          .format(spec["name"], ", ".join(unknown)))
    merged = dict(loaded)
    merged.update(args)
    return merged


def _write_png(path, encoded):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(encoded))
    except OSError as exc:
        raise BridgeError("invalid_request", "could not write save_path {0}: {1}"
                          .format(path, exc.strerror or exc))


def call_tool(name, arguments):
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        raise BridgeError("unknown_tool",
                          "No tool named {0!r}. Available: {1}".format(
                              name, ", ".join(sorted(TOOLS_BY_NAME))))

    args = _merge_params_file(spec, dict(arguments or {}))
    missing = [key for key in spec["_required"] if args.get(key) is None]
    if missing:
        raise BridgeError("invalid_request", "{0} needs {1}, inline or in params_file"
                          .format(name, ", ".join(missing)))
    save_path = None
    if spec["_image"] and args.get("save_path") is not None:
        save_path = _absolute_file(args.pop("save_path"), "save_path", (".png",))
    args.pop("save_path", None)
    op = spec["_op"]
    if spec["_transform"] is not None:
        op, args = spec["_transform"](args)

    result = BRIDGE.call(op, args)

    if spec["_image"] and isinstance(result, dict) and result.get("png_base64"):
        png = result["png_base64"]
        if save_path is not None:
            _write_png(save_path, png)
            return [{"type": "text", "text": _summarise(dict(result, saved_to=save_path))}]
        return [
            {"type": "text", "text": _summarise(result)},
            {"type": "image", "data": png, "mimeType": "image/png"},
        ]
    return [{"type": "text", "text": _summarise(result)}]


# ---------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# ---------------------------------------------------------------------------

def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class Server:
    def __init__(self):
        self.initialized = False
        self.protocol = PREFERRED_PROTOCOL

    def handle(self, message):
        """Return a response dict, or None for notifications."""
        if not isinstance(message, dict):
            return _error(None, -32600, "Request must be a JSON object.")

        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message

        if method is None:
            # A response to something we never sent; ignore it.
            return None

        try:
            if method == "initialize":
                payload = self._initialize(message.get("params") or {})
            elif method == "notifications/initialized":
                self.initialized = True
                return None
            elif method in ("notifications/cancelled", "notifications/progress",
                            "notifications/roots/list_changed"):
                return None
            elif method == "ping":
                payload = {}
            elif method == "tools/list":
                payload = {"tools": public_tools()}
            elif method == "tools/call":
                payload = self._call(message.get("params") or {})
            elif method in ("resources/list", "resources/templates/list"):
                payload = {"resources": [], "resourceTemplates": []}
            elif method == "prompts/list":
                payload = {"prompts": []}
            elif method == "logging/setLevel":
                payload = {}
            else:
                if is_notification:
                    return None
                return _error(request_id, -32601,
                              "Method not found: {0}".format(method))
        except BridgeError as exc:
            if is_notification:
                return None
            return _error(request_id, -32603, str(exc))
        except Exception as exc:
            log("internal error handling {0}: {1!r}".format(method, exc))
            if is_notification:
                return None
            return _error(request_id, -32603,
                          "{0}: {1}".format(type(exc).__name__, exc))

        if is_notification:
            return None
        return _result(request_id, payload)

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        self.protocol = (requested if requested in SUPPORTED_PROTOCOLS
                         else PREFERRED_PROTOCOL)
        return {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Drives a running Krita instance. Call `status` to see open "
                "documents, `inspect_document` for the layer tree, `draw` to "
                "paint onto a paint layer, and `get_image` to look at the "
                "result. If nothing responds, run `self_test`."
            ),
        }

    def _call(self, params):
        name = params.get("name")
        if not isinstance(name, str):
            raise BridgeError("invalid_request", "tools/call needs a `name`.")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise BridgeError("invalid_request", "`arguments` must be an object.")

        started = time.time()
        try:
            content = call_tool(name, arguments)
        except BridgeUnavailable as exc:
            return {"content": [{"type": "text", "text": str(exc)}],
                    "isError": True}
        except BridgeError as exc:
            text = "{0}: {1}".format(exc.kind, exc)
            if exc.detail:
                text += "\n\n" + (exc.detail if isinstance(exc.detail, str) else
                                   json.dumps(exc.detail, ensure_ascii=False))
            return {"content": [{"type": "text", "text": text}],
                    "isError": True}
        except Exception as exc:
            log("tool {0} blew up: {1!r}".format(name, exc))
            return {"content": [{"type": "text", "text": "{0}: {1}".format(
                type(exc).__name__, exc)}], "isError": True}

        log("{0} ok in {1:.0f}ms".format(name, (time.time() - started) * 1000))
        return {"content": content, "isError": False}


def serve():
    stdin = sys.stdin
    stdout = sys.stdout
    try:  # never let the console codepage mangle protocol bytes
        stdin.reconfigure(encoding="utf-8", errors="replace")
        stdout.reconfigure(encoding="utf-8", newline="\n")
    except AttributeError:
        pass

    server = Server()
    log("ready (pid {0})".format(os.getpid()))

    while True:
        try:
            line = stdin.readline()
        except (KeyboardInterrupt, ValueError):
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except ValueError as exc:
            response = _error(None, -32700, "Parse error: {0}".format(exc))
        else:
            if isinstance(message, list):  # JSON-RPC batch
                responses = [r for r in (server.handle(m) for m in message)
                             if r is not None]
                response = responses or None
            else:
                response = server.handle(message)

        if response is None:
            continue
        try:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
        except (BrokenPipeError, OSError):
            break

    log("stdin closed, exiting")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli_selftest():
    print("bridge file: {0}".format(info_file_path()))
    try:
        health = BRIDGE.health()
    except BridgeUnavailable as exc:
        print("UNAVAILABLE\n{0}".format(exc))
        return 1
    print("health: Krita {0}, plugin {1}, {2} operations".format(
        health.get("krita_version"), health.get("plugin_version"),
        len(health.get("operations", []))))

    result = BRIDGE.call("self_test", {})
    for check in result.get("checks", []):
        print("  [{0}] {1:<22} {2}".format(
            "ok" if check["ok"] else "FAIL", check["check"], check["detail"]))
    print("{0}/{1} checks passed".format(result.get("passed"),
                                         result.get("total")))
    return 0 if result.get("all_ok") else 2


def cli_call(op, params_json, params_file=None):
    if params_file:
        try:
            # utf-8-sig: PowerShell's Set-Content writes a BOM.
            with open(params_file, "r", encoding="utf-8-sig") as handle:
                params_json = handle.read()
        except OSError as exc:
            print("could not read {0}: {1}".format(params_file, exc))
            return 1
    try:
        params = json.loads(params_json) if params_json.strip() else {}
    except ValueError as exc:
        print("params is not valid JSON: {0}".format(exc))
        return 1
    try:
        result = BRIDGE.call(op, params)
    except (BridgeError, BridgeUnavailable) as exc:
        print("ERROR: {0}".format(exc))
        return 1
    if isinstance(result, dict) and "png_base64" in result:
        data = result.pop("png_base64")
        out = os.path.abspath("krita_mcp_output.png")
        with open(out, "wb") as handle:
            handle.write(base64.b64decode(data))
        result["png_written_to"] = out
    print(_summarise(result))
    return 0


def plugin_source_dir():
    """Directory that contains krita_mcp/ and krita_mcp.desktop."""
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pykrita")
    plugin = os.path.join(source, "krita_mcp")
    desktop = os.path.join(source, "krita_mcp.desktop")
    if not os.path.isdir(plugin) or not os.path.isfile(desktop):
        raise SystemExit(
            "Plugin files were not found next to the server. "
            "Run this from a krita-mcp checkout or from the installed package.")
    return source


def krita_locations():
    """Return (resource directory, kritarc path) for this platform."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        local = os.environ.get("LOCALAPPDATA") or appdata
        return (os.path.join(appdata, "krita"),
                os.path.join(local, "kritarc"))
    if sys.platform == "darwin":
        library = os.path.expanduser("~/Library")
        return (os.path.join(library, "Application Support", "krita"),
                os.path.join(library, "Preferences", "kritarc"))
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(data, "krita"), os.path.join(config, "kritarc")


def krita_is_running():
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq krita.exe", "/NH"],
                stderr=subprocess.DEVNULL, text=True, errors="replace")
        except (OSError, subprocess.CalledProcessError):
            return False
        return "krita.exe" in out.lower()
    try:
        result = subprocess.run(
            ["pgrep", "-x", "krita"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return result.returncode == 0


def enable_plugin_flag(config_file, key="enable_krita_mcp"):
    """Set enable_krita_mcp=true under [python], preserving other settings."""
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    newline = "\n"
    lines = []
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8", errors="replace",
                  newline="") as handle:
            text = handle.read()
        if "\r\n" in text:
            newline = "\r\n"
        lines = text.splitlines()
    section = None
    for index, line in enumerate(lines):
        if line.strip() == "[python]":
            section = index
            break
    assignment = "{0}=true".format(key)
    if section is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[python]", assignment])
    else:
        end = len(lines)
        for index in range(section + 1, len(lines)):
            if lines[index].lstrip().startswith("["):
                end = index
                break
        existing = None
        for index in range(section + 1, end):
            stripped = lines[index].strip()
            if stripped.startswith(key + "=") or stripped.startswith(key + " ="):
                existing = index
                break
        if existing is None:
            lines.insert(section + 1, assignment)
        else:
            lines[existing] = assignment
    payload = newline.join(lines) + newline
    with open(config_file, "w", encoding="utf-8", newline="") as handle:
        handle.write(payload)


def install_plugin(force=False):
    """Copy the plugin into Krita's resource folder and enable it."""
    source = plugin_source_dir()
    resource_dir, config_file = krita_locations()
    pykrita_dir = os.path.join(resource_dir, "pykrita")
    os.makedirs(pykrita_dir, exist_ok=True)
    target = os.path.join(pykrita_dir, "krita_mcp")
    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.copytree(
        os.path.join(source, "krita_mcp"), target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    shutil.copy2(os.path.join(source, "krita_mcp.desktop"),
                 os.path.join(pykrita_dir, "krita_mcp.desktop"))
    print("Installed plugin to {0}".format(target))
    if krita_is_running() and not force:
        print("")
        print("Krita is running, so kritarc was not modified. Krita rewrites")
        print("that file on exit. Close Krita and run the installer again, or")
        print("enable MCP Bridge in Settings > Configure Krita >")
        print("Python Plugin Manager.")
        return 0
    enable_plugin_flag(config_file)
    print("Enabled enable_krita_mcp in {0}".format(config_file))
    print("")
    print("Done. Start Krita, then verify with:")
    print("  krita-mcp --selftest")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["install"],
                        help="copy the Krita plugin into the local resource "
                             "folder and enable it")
    parser.add_argument("--force", action="store_true",
                        help="with install, write kritarc even if Krita is running")
    parser.add_argument("--selftest", action="store_true",
                        help="check the bridge and run its self test")
    parser.add_argument("--call", metavar="OP",
                        help="invoke one bridge operation and print the result")
    parser.add_argument("--params", metavar="JSON", default="",
                        help="JSON parameters for --call")
    parser.add_argument("--params-file", metavar="PATH",
                        help="read --call parameters from a JSON file "
                             "(avoids shell quoting)")
    parser.add_argument("--list-tools", action="store_true",
                        help="print the MCP tool names and exit")
    args = parser.parse_args()

    if args.command == "install":
        return install_plugin(force=args.force)
    if args.list_tools:
        for spec in TOOLS:
            print("{0:<20} -> op {1}".format(spec["name"], spec["_op"]))
        return 0
    if args.selftest:
        return cli_selftest()
    if args.call:
        return cli_call(args.call, args.params, args.params_file)
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
