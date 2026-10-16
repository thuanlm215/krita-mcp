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
SERVER_VERSION = "1.8.0"

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
    "description": "Index from status, a name fragment, or omit for the active document.",
}

LAYER_PROP = {
    "type": "string",
    "description": "Name, group path, #index path, uuid:<id>, or omit for the active layer.",
}

REGION_PROP = {
    "type": "object",
    "description": "Canvas rectangle in pixels.",
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
        "One primitive. Colours are #rrggbb, #rrggbbaa, an SVG name, or [r,g,b,a]. "
        "Field detail is in docs/usage.md.\n"
        "- fill_rect: x, y, w, h, color\n"
        "- rect: x, y, w, h, fill, color, stroke_width, radius\n"
        "- ellipse: x, y, w, h, fill, color, stroke_width\n"
        "- circle: cx, cy, radius, fill, color, stroke_width\n"
        "- line: x1, y1, x2, y2, color, stroke_width, cap\n"
        "- polyline / polygon: points, color, fill, stroke_width, close\n"
        "- ink: points as [x, y] or [x, y, width], color, smooth\n"
        "- text: x, y, text, size, color, font, bold, italic, anchor\n"
        "- linear_gradient: box or x1, y1, x2, y2, stops, direction\n"
        "- radial_gradient: stops plus a box or cx, cy, radius (rx, ry, angle)\n"
        "- image: x, y, data or path; w, h optional\n"
        "- svg: x, y, svg or path; w, h optional\n"
        "- clear: x, y, w, h"
    ),
    "properties": {
        "type": {
            "type": "string",
            "enum": ["fill_rect", "rect", "ellipse", "circle", "line",
                     "polyline", "polygon", "ink", "text", "linear_gradient",
                     "radial_gradient", "image", "svg", "clear"],
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
            "minItems": 2, "maxItems": 3}},
        "smooth": {"type": "boolean", "default": True},
        "svg": {"type": "string"},
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
        "data": {"type": "string"},
        "path": {"type": "string"},
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
        "timestamp_ms": {"type": "number", "minimum": 0}},
        "required": ["x", "y"], "additionalProperties": False},
}

ALPHA_LOCKED_PROP = {
    "type": "boolean",
    "description": "Paint changes colour only; existing transparency stays.",
}

INHERIT_ALPHA_PROP = {
    "type": "boolean",
    "description": "Clip this layer to the combined alpha of the layers below it.",
}

INCLUDE_TREE_PROP = {
    "type": "boolean", "default": False,
    "description": "Also return the layer tree as `layers`.",
}

PARAMS_FILE_PROP = {
    "type": "string",
    "description": "Absolute .json of arguments; inline values override the file.",
}

SAVE_PATH_PROP = {
    "type": "string",
    "description": "Absolute .png path. The image is written there and omitted from the reply.",
}


# Verified against the plugin ops: each of these returns data and leaves the
# document, its history, and its viewing time unchanged, or restores them.
READ_ONLY_TOOLS = frozenset({
    "status", "inspect_document", "get_image", "get_pixel", "list_brushes",
    "list_capabilities", "sample_colors", "compare_images", "plan_stroke",
    "inspect_repair", "repair_status", "animation_review", "preview_changes",
})
# self_test is not read-only: it creates and closes a scratch document, and
# closing is the teardown path that can crash Krita.

# Deletes a layer, a document, history, a keyframe, or a prepared repair.
DESTRUCTIVE_TOOLS = frozenset({
    "delete_layer", "close_document", "merge_layer_down", "discard_repair",
    "trigger_action", "undo", "animation_frame",
})


def _annotations(name):
    read_only = name in READ_ONLY_TOOLS
    return {
        "title": name.replace("_", " ").capitalize(),
        "readOnlyHint": read_only,
        "destructiveHint": False if read_only else name in DESTRUCTIVE_TOOLS,
        "openWorldHint": False,
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
        "annotations": _annotations(name),
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


# plan_stroke and paint_stroke publish this same object, so their schemas cannot drift.
STROKE_ARGUMENTS = {"spec": STROKE}

TOOLS = [
    tool('import_animation','Import ordered PNGs as one native animation. Padding, FPS, and the anchor annotation are kept.',
         {'name':{'type':'string','minLength':1,'maxLength':128},'frames':FRAMES,'fps':{'type':'integer','minimum':1,'maximum':120},
          'anchor':{'type':'object','properties':{'x':{'type':'number'},'y':{'type':'number'}},'required':['x','y'],'additionalProperties':False},
          'end':{'type':'integer','minimum':0,'maximum':99999}},
         required=['name','frames','fps','anchor']),
    tool('animation_review','Previous, current, and next frames, aligned. Does not change the timeline or history.',
         {'document':DOCUMENT_PROP,'layer':LAYER_PROP,'time':{'type':'integer'},'region':REGION_PROP,'max_size':{'type':'integer'}},
         required=['document','time'],image=True),
    tool('prepare_repair','Freeze a baseline and an allowed mask for at most three repair candidates. The source stays unchanged.',
         {'target':TARGET,'region':REGION_PROP,'allowed_mask':{'type':'string'},'target_mask':{'type':'string'},
          'region_mode':{'type':'string','enum':['roi','whole_frame'],'default':'roi'},
          'defect':{'type':'string'},'intended_change':{'type':'string'}},
         required=['target','region','allowed_mask','defect','intended_change']),
    tool('render_repair','Render one candidate on an owned static copy. The source document stays unchanged.',
         {'transaction':{'type':'string'},'edit':{'type':'object'}},required=['transaction','edit']),
    tool('inspect_repair','Return one frozen preview. before and after stay full resolution; max_size applies only to panel.',
         {'transaction':{'type':'string'},'candidate':{'type':'string'},'max_size':{'type':'integer'},
          'view':{'type':'string','enum':['composite','raw_layer'],'default':'composite'},
          'image':{'type':'string','enum':['panel','before','after'],'default':'panel'}},required=['transaction','candidate'],image=True),
    tool('commit_repair','Apply one frozen candidate once. A repeat of the same commit returns the same outcome. One native Undo.',
         {'transaction':{'type':'string'},'candidate':{'type':'string'}},required=['transaction','candidate']),
    tool('repair_status','Read a repair transaction outcome. Does not replay the commit.',
         {'transaction':{'type':'string'}},required=['transaction']),
    tool('discard_repair','Drop a prepared repair and its candidates. The source document is not edited.',
         {'transaction':{'type':'string'}},required=['transaction']),
    tool('sample_colors','sRGB region mean in linear light, plus a deterministic 4-bit palette.',
         {'document':DOCUMENT_PROP,'layer':LAYER_PROP,'region':REGION_PROP,'time':{'type':'integer','minimum':0},
          'palette_size':{'type':'integer','minimum':1,'maximum':16}},required=['document']),
    tool('compare_images','Aligned before/after/difference panels, changed-pixel counts, and mask overlap.',
         {'before':{'type':'string'},'after':{'type':'string'},'allowed_mask':{'type':'string'},'target_mask':{'type':'string'},
          'offset':{'type':'object'},'landmarks':{'type':'array'},'alpha_threshold':{'type':'integer','minimum':1,'maximum':255},
          'max_size':{'type':'integer','minimum':64,'maximum':2048}},required=['before','after'],image=True),
    tool('plan_stroke','Plan version 1 polyline or cubic geometry with arc-length pressure and timing. Does not paint.',
         STROKE_ARGUMENTS,required=['spec']),
    tool('paint_stroke','Paint one validated plan as a single native stroke, then restore the brush UI.',
         STROKE_ARGUMENTS,required=['spec']),
    tool("status",
         "Krita's version and every open document: index, size, colour space, unsaved state.",
         {}),

    tool("dismiss_recovery_dialog",
         "Cancel Krita's Recover Files dialog and keep the autosaves. Other modals are refused. No document edits.",
         {}),

    tool("inspect_document",
         "Canvas, resolution, colour space, the layer tree (top first), and the current selection.",
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
                          "description": "RGBA, GRAYA, CMYKA, LABA, XYZA, or YCbCrA."},
          "color_depth": {"type": "string", "default": "U8",
                          "description": "U8, U16, F16, or F32. Drawing needs U8."},
          "color_profile": {"type": "string", "default": ""},
          "background": {"type": ["string", "array"]}},
         required=["width", "height"]),

    tool("open_document",
         "Open an image file from disk in Krita.",
         {"path": {"type": "string", "description": "Absolute path of the file to open."}},
         required=["path"]),

    tool("save_document",
         "Save the document. `path` saves a copy under a new name; omit it to overwrite the current file.",
         {"document": DOCUMENT_PROP,
          "path": {"type": "string"}}),

    tool("export_document",
         "Write a flattened copy. The document's own file binding is unchanged.",
         {"document": DOCUMENT_PROP,
          "path": {"type": "string", "description": "Absolute output path. The extension picks the format."},
          "options": {"type": "object"}},
         required=["path"]),

    tool("close_document",
         "Close a document. Unsaved work is kept unless discard_changes is true. "
         "Closing a heavily edited document can crash Krita; see docs/usage.md.",
         {"document": DOCUMENT_PROP,
          "save": {"type": "boolean", "default": False},
          "discard_changes": {"type": "boolean", "default": False}}),

    tool("transform_image",
         "Whole-image geometry. resize_canvas keeps layer pixels in place; scale resamples; "
         "rotate is clockwise degrees; crop; flatten merges every layer.",
         {"document": DOCUMENT_PROP,
          "action": {"type": "string",
                     "enum": ["resize_canvas", "scale", "rotate", "crop",
                              "flatten"]},
          "x": {"type": "integer"}, "y": {"type": "integer"},
          "width": {"type": "integer"}, "height": {"type": "integer"},
          "degrees": {"type": "number"},
          "strategy": {"type": "string", "default": "Bicubic",
                       "description": "Scale filter: Bicubic, Bilinear, NearestNeighbor, Lanczos3, Box."}},
         required=["action"], transform=_transform_image),

    tool("create_layer",
         "Add a layer. Index 0 is the top; a negative index counts from the bottom. "
         "A filelayer links `path` and follows that file.",
         {"document": DOCUMENT_PROP,
          "name": {"type": "string", "default": "Layer"},
          "type": {"type": "string", "default": "paintlayer",
                   "enum": ["paintlayer", "grouplayer", "filterlayer",
                            "filllayer", "filelayer", "clonelayer",
                            "vectorlayer", "transparencymask", "filtermask",
                            "transformmask", "selectionmask", "colorizemask"]},
          "parent": {"type": "string",
                     "description": "Group to nest inside. Omit for the top level."},
          "index": {"type": "integer"},
          "path": {"type": "string",
                   "description": "filelayer: absolute path of the linked image."},
          "scaling": {"type": "string", "enum": ["none", "to_image_size", "to_ppi"],
                      "default": "none"},
          "select": {"type": "boolean", "default": True},
          "opacity": {"type": "number"},
          "blending_mode": {"type": "string"},
          "visible": {"type": "boolean"},
          "locked": {"type": "boolean"},
          "alpha_locked": ALPHA_LOCKED_PROP,
          "inherit_alpha": INHERIT_ALPHA_PROP,
          "include_tree": INCLUDE_TREE_PROP}),

    tool("set_layer",
         "Rename a layer or set opacity (0-255 or 0-1), blend mode, visibility, locks, and inherit alpha.",
         {"document": DOCUMENT_PROP,
          "layer": LAYER_PROP,
          "new_name": {"type": "string"},
          "opacity": {"type": "number"},
          "blending_mode": {"type": "string"},
          "visible": {"type": "boolean"},
          "locked": {"type": "boolean"},
          "alpha_locked": ALPHA_LOCKED_PROP,
          "inherit_alpha": INHERIT_ALPHA_PROP,
          "select": {"type": "boolean", "default": False}}),

    tool("delete_layer", "Delete a layer and everything inside it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "include_tree": INCLUDE_TREE_PROP},
         required=["layer"]),

    tool("duplicate_layer", "Copy a layer, placing the copy just above it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "name": {"type": "string"},
          "select": {"type": "boolean", "default": True},
          "include_tree": INCLUDE_TREE_PROP}),

    tool("move_layer",
         "Restack a layer, optionally into another group. Index is top-first; -1 is the bottom.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "parent": {"type": "string",
                     "description": "Target group. Omit to stay in the current parent."},
          "index": {"type": "integer", "default": 0},
          "include_tree": INCLUDE_TREE_PROP},
         required=["layer"]),

    tool("merge_layer_down", "Merge a layer into the one directly beneath it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "include_tree": INCLUDE_TREE_PROP}),

    tool("get_image",
         "Render a PNG of the merged canvas, or of one layer when `layer` is set.",
         {"document": DOCUMENT_PROP,
          "layer": {"type": "string",
                    "description": "Omit or \"merged\" for the composite."},
          "region": REGION_PROP,
          "time": {'type':'integer','minimum':0,'description':'Raster playback time. Restored afterwards. Onion skins are excluded.'},
          "max_size": {"type": "integer", "default": 1024,
                       "description": "Longest edge. The canvas is never upscaled."}},
         image=True),

    tool("get_pixel", "Read the exact colour at one pixel.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "x": {"type": "integer"}, "y": {"type": "integer"}},
         required=["x", "y"]),

    tool("draw",
         "Paint commands in order on an RGBA/8-bit layer, as one Undo. "
         "An animated layer needs `time` of an existing key. The selection, including feather, applies.",
         {"document": DOCUMENT_PROP,
          "layer": LAYER_PROP,
          "time": {"type": "integer", "minimum": 0, "maximum": 99999,
                   "description": "Animated layers: an existing keyframe. A held time is rejected."},
          "antialias": {"type": "boolean", "default": True},
          "commands": {"type": "array", "items": DRAW_COMMAND,
                       "minItems": 1}},
         required=["commands"]),

    tool("list_brushes", "List native brush preset names, optionally filtered by query.",
         {"document": DOCUMENT_PROP, "query": {"type": "string"}}),

    tool("draw_brush",
         "One native brush stroke, or up to 64 via `strokes`, then the brush UI is restored. "
         "Needs the native helper. Off-canvas points are rejected unless clamp is true.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "preset": {"type": "string", "description": "Exact name from list_brushes."},
          "size": {"type": "number", "minimum": 0.1, "maximum": 2048, "default": 20},
          "opacity": {"type": "number", "minimum": 0, "maximum": 1, "default": 1},
          "color": {"type": ["string", "array"], "default": "#000000"},
          "erase": {"type": "boolean", "default": False},
          "mode": {"type": "string", "enum": ["pressure", "continuous", "segments", "path"], "default": "pressure"},
          "points": BRUSH_POINTS,
          "strokes": {"type": "array", "minItems": 1, "maxItems": 64,
                      "description": "Up to 64 strokes in one Undo. Unset fields inherit the top level.",
                      "items": {"type": "object", "properties": {
                          "points": BRUSH_POINTS,
                          "color": {"type": ["string", "array"]},
                          "size": {"type": "number", "minimum": 0.1, "maximum": 2048},
                          "opacity": {"type": "number", "minimum": 0, "maximum": 1},
                          "erase": {"type": "boolean"}},
                          "required": ["points"], "additionalProperties": False}},
          "clamp": {"type": "boolean", "default": False,
                    "description": "Pull off-canvas points onto the nearest edge."}}),

    tool("select_polygon",
         "Set a polygon selection in document pixels. One native Undo.",
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
         "Copy or cut the selection from a static unmasked paint layer into a new layer. "
         "The selection clears unless clear_selection is false.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "name": {"type": "string"},
          "mode": {"type": "string", "enum": ["copy", "cut"], "default": "copy"},
          "clear_selection": {"type": "boolean", "default": True}}),

    tool("transform_layer",
         "Move, rotate, scale, or shear around a pivot. Apply replaces the MCP transform mask; it does not stack. "
         "Preview does not change the document.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "mode": {"type": "string", "enum": ["preview", "apply"], "default": "preview"},
          "pivot": {"type": "object", "properties": {
              "x": {"type": "number"}, "y": {"type": "number"}},
                    "required": ["x", "y"], "additionalProperties": False},
          "dx": {"type": "number", "default": 0}, "dy": {"type": "number", "default": 0},
          "degrees": {"type": "number", "default": 0,
                      "description": "Clockwise degrees."},
          "scale_x": {"type": "number", "minimum": -100, "maximum": 100, "default": 1,
                      "description": "Negative flips. Absolute value must be at least 0.01."},
          "scale_y": {"type": "number", "minimum": -100, "maximum": 100, "default": 1},
          "shear_x": {"type": "number", "minimum": -10, "maximum": 10, "default": 0},
          "shear_y": {"type": "number", "minimum": -10, "maximum": 10, "default": 0},
          "strategy": {"type": "string", "enum": ["Bilinear", "Bicubic", "NearestNeighbor", "Lanczos3", "Box"],
                       "default": "Bilinear"},
          "max_size": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 1024}},
         image=True),

    tool("warp_layer",
         "Pull pixels inside a radius on a static unmasked RGBA/U8 layer. Offset length is at most radius/4. "
         "Apply writes a new layer and hides the source. Not valid inside edit_batch.",
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

    tool("select_content", "Select by alpha, colour, a 4-connected region, or an imported mask. One native Undo.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "mode": {"type": "string", "enum": ["alpha", "color", "contiguous", "mask"], "default": "alpha"},
          "color": {"type": ["string", "array"]}, "data": {"type": "string", "description": "Base64 PNG mask."},
          "data_path": {"type": "string", "description": "Absolute image path instead of data."},
          "channel": {"type": "string", "enum": ["alpha", "luminance"], "default": "alpha"},
          "x": {"type": "integer"}, "y": {"type": "integer"},
          "threshold": {"type": "integer", "minimum": 0, "maximum": 255, "default": 1},
          "tolerance": {"type": "integer", "minimum": 0, "maximum": 255, "default": 0},
          "feather": {"type": "integer", "minimum": 0, "maximum": 256, "default": 0},
          "combine": {"type": "string", "enum": ["replace", "add", "subtract", "intersect"], "default": "replace"}}),

    tool("texture_patch", "Place a PNG or source-layer texture into a rectangle. Respects the selection. One Undo.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP, "source_layer": LAYER_PROP,
          "data": {"type": "string", "description": "Base64 image. Not together with source_layer."},
          "data_path": {"type": "string", "description": "Absolute image path instead of data."},
          "source_region": REGION_PROP, "region": REGION_PROP,
          "opacity": {"type": "number", "minimum": 0, "maximum": 1, "default": 1},
          "mask_data": {"type": "string", "description": "Base64 mask the size of the destination."},
          "mask_path": {"type": "string", "description": "Absolute image path instead of mask_data."},
          "mask_channel": {"type": "string", "enum": ["alpha", "luminance"], "default": "alpha"}},
         required=["region"]),

    tool("clone_stamp", "Clone pixels along a stroke. The source is frozen at the start. One Undo. Brush presets use draw_brush.",
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

    tool("edit_batch", "Apply up to 64 edits as one Undo group. On error the group is undone. "
         "warp_layer is not allowed. A later edit drops the redo branch.",
         {"document": DOCUMENT_PROP, "label": {"type": "string"},
          "results": {"type": "string", "enum": ["summary", "full", "none"], "default": "summary",
                      "description": "summary omits per-edit Undo receipts; full returns each tool; none returns the batch receipt."},
          "edits": {"type": "array", "minItems": 1, "maxItems": 64, "items": {
              "type": "object", "properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}},
              "required": ["tool"], "additionalProperties": False}}}, required=["edits"]),

    tool("mask_layer", "Set a transparency mask from the selection, a fill, or a canvas-sized image. One Undo.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP, "mask": LAYER_PROP, "name": {"type": "string"},
          "mode": {"type": "string", "enum": ["selection", "fill", "image"], "default": "selection"},
          "value": {"type": "integer", "minimum": 0, "maximum": 255, "default": 255},
          "data": {"type": "string"},
          "data_path": {"type": "string", "description": "Absolute image path instead of data."},
          "channel": {"type": "string", "enum": ["alpha", "luminance"], "default": "alpha"}}),

    tool("bake_layer", "Bake a static layer or group, including masks, into a new paint layer and hide the source. One Undo.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP, "name": {"type": "string"}}),

    tool("outline_layer", "Trace the rendered alpha onto a new paint layer. outside sits under the source. The source is not edited.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "width": {"type": "number", "minimum": 0.5, "maximum": 32, "default": 2},
          "color": {"type": ["string", "array"], "default": "#000000"},
          "position": {"type": "string", "enum": ["outside", "inside", "center"],
                       "default": "outside"},
          "antialias": {"type": "boolean", "default": True,
                        "description": "false uses a hard edge at alpha 128."},
          "name": {"type": "string"}}),

    tool("export_layers", "Write each layer as a PNG plus manifest.json. The document is not changed. trim crops to alpha bounds.",
         {"document": DOCUMENT_PROP, "directory": {"type": "string",
                                                   "description": "Absolute path of a new directory."},
          "layers": {"type": "array", "items": LAYER_PROP, "minItems": 1, "maxItems": 256},
          "trim": {"type": "boolean", "default": True},
          "padding": {"type": "integer", "minimum": 0, "maximum": 512, "default": 0}},
         required=["directory"]),

    tool("slice_sheet", "Cut a sprite sheet into cells, as layers or as PNG files. "
         "The sheet is a layer, or `path` or `data` that is not imported. "
         "auto splits on empty rows; columns forces that many frames per row.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "path": {"type": "string", "description": "Absolute image sheet instead of layer."},
          "data": {"type": "string", "description": "Base64 PNG sheet instead of layer."},
          "mode": {"type": "string", "enum": ["auto", "grid"], "default": "auto"},
          "background": {"type": ["string", "array"], "default": "auto",
                         "description": "auto, transparent, or a colour. auto uses alpha, else the corner colour."},
          "tolerance": {"type": "integer", "minimum": 0, "maximum": 255, "default": 24,
                        "description": "Per-channel distance still treated as background."},
          "threshold": {"type": "integer", "minimum": 1, "maximum": 255, "default": 16,
                        "description": "Lowest alpha counted as content."},
          "min_gap": {"type": "integer", "minimum": 1, "default": 2,
                      "description": "auto: empty pixels required between cells."},
          "min_pixels": {"type": "integer", "minimum": 1, "default": 16,
                         "description": "Smaller cells are dropped."},
          "columns": {"type": "integer", "minimum": 1, "maximum": 256,
                      "description": "grid: cells per row. auto: frames per sprite row."},
          "rows": {"type": "integer", "minimum": 1, "maximum": 256},
          "margin": {"type": "integer", "minimum": 0, "default": 0},
          "spacing": {"type": "integer", "minimum": 0, "default": 0},
          "trim": {"type": "boolean", "default": True},
          "output": {"type": "string", "enum": ["layers", "files"], "default": "layers"},
          "name": {"type": "string"},
          "directory": {"type": "string", "description": "files: absolute path of a new directory."},
          "prefix": {"type": "string", "default": "cell"},
          "uniform": {"type": "boolean", "default": False,
                      "description": "files: pad every PNG to the largest cell."},
          "align": {"type": "string", "enum": ["center", "bottom", "row"], "default": "center",
                    "description": "With uniform: center, bottom baseline, or row so a hop stays up."},
          "padding": {"type": "integer", "minimum": 0, "maximum": 512, "default": 0}}),

    tool("remove_background", "Make a solid background transparent. contiguous keeps enclosed areas of the same colour. "
         "defringe only cleans edges and needs color. One Undo.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "mode": {"type": "string", "enum": ["contiguous", "global", "defringe"],
                   "default": "contiguous"},
          "color": {"type": ["string", "array"],
                    "description": "Background colour. Default is the corners. Required for defringe."},
          "tolerance": {"type": "integer", "minimum": 0, "maximum": 255, "default": 12,
                        "description": "Per-channel distance cleared completely."},
          "softness": {"type": "integer", "minimum": 0, "maximum": 255, "default": 12,
                       "description": "Band above tolerance that fades to opaque."},
          "holes": {"type": "integer", "minimum": 0, "default": 0,
                    "description": "contiguous: also clear enclosed areas of at least this many pixels."},
          "defringe": {"type": "boolean", "default": True},
          "shrink": {"type": "integer", "minimum": 0, "maximum": 8, "default": 0,
                     "description": "Erode remaining alpha by this many pixels."}}),

    tool("quantize_layer", "Map pixels to the nearest palette colour, or shift matches in swap mode so shading remains. One Undo.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "mode": {"type": "string", "enum": ["quantize", "swap"], "default": "quantize"},
          "palette": {"type": "array", "items": {"type": ["string", "array"]},
                      "minItems": 1, "maxItems": 256},
          "colors": {"type": "integer", "minimum": 2, "maximum": 256,
                     "description": "quantize: build this many colours instead of palette."},
          "replace": {"type": "array", "items": {"type": ["string", "array"]},
                      "minItems": 1, "maxItems": 256,
                      "description": "Same length as palette."},
          "dither": {"type": "string", "enum": ["none", "ordered"], "default": "none"},
          "accents": {"type": "array", "items": {"type": ["string", "array"]},
                      "minItems": 1, "maxItems": 256,
                      "description": "quantize: these colours match only within tolerance."},
          "despeckle": {"type": "boolean", "default": False,
                        "description": "quantize: lone pixels take the nearest neighbour colour. Not with dither."},
          "tolerance": {"type": "integer", "minimum": 0, "maximum": 255, "default": 32,
                        "description": "swap and accents: per-channel distance from a palette colour."},
          "alpha": {"type": "string", "enum": ["keep", "threshold"], "default": "keep",
                    "description": "threshold cuts alpha at 128."}}),

    tool("preview_changes", "Store a composite checkpoint, compare it, or discard it. Does not edit the document. At most 8 checkpoints.",
         {"document": DOCUMENT_PROP,
          "mode": {"type": "string", "enum": ["checkpoint", "compare", "discard"], "default": "checkpoint"},
          "checkpoint": {"type": "string"},
          "max_size": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 1536}}, image=True),

    tool("animation_frame", "Inspect or edit raster keys: blank, duplicate, delete after frame 0, scrub, FPS, or onion skin. "
         "Scrub changes the viewing time. Delete removes that key.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "mode": {"type": "string", "enum": ["info", "blank", "duplicate", "delete", "settings", "scrub", "onion"], "default": "info"},
          "time": {"type": "integer", "minimum": 0, "maximum": 100000},
          "source_time": {"type": "integer", "minimum": 0, "maximum": 100000},
          "replace": {"type": "boolean", "default": False}, "enabled": {"type": "boolean"},
          "fps": {"type": "integer", "minimum": 1, "maximum": 120},
          "start": {"type": "integer", "minimum": 0}, "end": {"type": "integer", "minimum": 0}}),

    tool("export_animation", "Write composite frames, an atlas, and a JSON manifest into a new directory. Viewing time is restored.",
         {"document": DOCUMENT_PROP, "directory": {"type": "string"},
          "start": {"type": "integer", "minimum": 0}, "end": {"type": "integer", "minimum": 0},
          "step": {"type": "integer", "minimum": 1, "default": 1},
          "columns": {"type": "integer", "minimum": 1, "maximum": 128},
          "anchor": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                     "required": ["x", "y"], "additionalProperties": False}}, required=["directory"]),

    tool("undo", "Undo native history on this document, including edits made by hand.",
         {"document": DOCUMENT_PROP,
          "count": {"type": "integer", "minimum": 1, "maximum": 128, "default": 1}}),
    tool("redo", "Redo native Krita history entries on the requested document.",
         {"document": DOCUMENT_PROP,
          "count": {"type": "integer", "minimum": 1, "maximum": 128, "default": 1}}),

    tool("apply_filter",
         "Run one Krita filter on a layer, optionally inside a region. Names come from list_capabilities.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "filter": {"type": "string"},
          "settings": {"type": "object",
                       "description": "Parameter names must match the filter."},
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
          "value": {"type": "integer", "default": 255},
          "add": {"type": "boolean", "default": False},
          "amount": {"type": "integer", "default": 1}}),

    tool("list_capabilities",
         "List filters, blend modes, or workflow limits. Does not edit or switch the document.",
         {"kind": {"type": "string", "default": "filters",
                   "enum": ["filters", "blending_modes", "workflow"]},
          "document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "include_parameters": {"type": "boolean", "default": False}},
         transform=_list_capabilities),

    tool("trigger_action",
         "Run an allowlisted action: edit_undo, edit_redo, deselect, select_all, invert_selection.",
         {"name": {"type": "string"}},
         required=["name"]),

    tool("self_test",
         "Check the bridge: a document, a pixel round-trip, drawing, and PNG encoding.",
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


def _dump(value, indent=0, lead=0, width=100):
    """JSON text that keeps a container on one line while it fits in `width`.

    Plain indent=2 puts every number of a rectangle on its own line, which
    multiplies the lines a caller has to read; the data stays the same.
    """
    flat = json.dumps(value, ensure_ascii=False, default=str)
    if not isinstance(value, (dict, list)) or not value or indent + lead + len(flat) <= width:
        return flat
    pad = " " * (indent + 2)
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            name = json.dumps(str(key), ensure_ascii=False) + ": "
            lines.append(pad + name + _dump(item, indent + 2, len(name), width))
    else:
        lines = [pad + _dump(item, indent + 2, 0, width) for item in value]
    opener, closer = ("{", "}") if isinstance(value, dict) else ("[", "]")
    return opener + "\n" + ",\n".join(lines) + "\n" + " " * indent + closer


def _summarise(result):
    return _dump(_image_metadata(result))


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
