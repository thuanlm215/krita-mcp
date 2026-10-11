"""Read-only runtime discovery, including helper availability and layer guards."""
import sys

from .. import ops as b
from ..compat import QtCore
from .common import MAX_PIXELS, paint_layer


def workflow_capabilities(params):
    from krita import Document, Node, Selection
    from ..native_commands import library
    from .warp import MAX_RADIUS, MAX_WORK_PIXELS
    api = {"paintLine": hasattr(Node, "paintLine"), "paintPath": hasattr(Node, "paintPath"),
           "transform_mask": hasattr(Document, "createTransformMask"),
           "transparency_mask": hasattr(Document, "createTransparencyMask"),
           "clone_document": hasattr(Document, "clone"),
           "selection_pixels": hasattr(Selection, "pixelData"),
           "extraction_actions": all(b._krita().action(name) is not None for name in
                                     ("copy_selection_to_new_layer", "cut_selection_to_new_layer"))}
    helper = {"available": False, "required_abi": "krita-6.0.4-qt6",
              "required_platform": "linux", "required_byteorder": "little",
              "probe": "checksum, dynamic load, exported symbols and ABI marker; no document activation"}
    try:
        lib = library()
        helper.update(available=True, abi=lib.mcp_native_abi().decode("ascii"), reason=None)
    except Exception as exc:
        helper["reason"] = str(exc)
    doc = b.resolve_document(params.get("document")) if b._documents() else None
    if doc is None and (params.get("document") is not None or params.get("layer") is not None):
        raise b.OpError("Open a document to inspect a specific target", kind="no_document")
    node = b.resolve_node(doc, params.get("layer")) if doc is not None else None
    target = None
    if node is not None:
        target = {"document": doc.name(), "layer": node.name(), "uuid": b._node_uuid(node),
                  "type": node.type(), "color_model": node.colorModel(), "color_depth": node.colorDepth(),
                  "animated": node.animated(), "locked": node.locked(),
                  "masks": [{"name": c.name(), "type": c.type()} for c in node.childNodes()]}

    def support(requirements, target_check=None):
        reasons = [reason for enabled, reason in requirements if not enabled]
        if node is not None and target_check is not None:
            try:
                target_check()
            except (b.OpError, b.ImagingError) as exc:
                reasons.append(str(exc))
        return {"available": not reasons, "target_checked": node is not None,
                "unsupported_reasons": reasons}

    native = (helper["available"], helper.get("reason") or "Native helper unavailable")

    def pilot():
        from ..targeting import resolve
        from ..native_commands import get_time
        scope={'kind':'frame','time':get_time(doc),
               'exposure_policy':'existing_keyframe'} if node.animated() else {'kind':'static'}
        resolve({'document':b._doc_summary(doc)['session_id'],'layer':'uuid:'+b._node_uuid(node),'scope':scope},allow_frame=True)

    def frame_pilot():
        pilot()
        if not node.animated():raise b.OpError('Requires raster animation')

    def pixels():
        paint_layer(doc, params.get("layer"))
        b.imaging.require_pixel_ops(node)

    def brush():
        paint_layer(doc, params.get("layer"), allow_animated=True)

    def transform():
        if node.type() not in ("paintlayer", "grouplayer") or node.locked() or node.animated():
            raise b.OpError("Requires an unlocked static paint/group layer")
        b.imaging.require_pixel_ops(node)
        if any(n.animated() for _, _, n in b._flatten_tree(node, "", "", [])):
            raise b.OpError("Animated descendants are unsupported")
        masks = [c for c in node.childNodes() if c.type() == "transformmask"]
        if len(masks) > 1 or any(c.name() != "MCP Transform" for c in masks):
            raise b.OpError("Another transform mask is present")

    def mask_target():
        if node.type() not in ("paintlayer", "grouplayer") or node.locked() or node.animated():
            raise b.OpError("Requires an unlocked static paint/group layer")

    def animation_target():
        paint_layer(doc, params.get("layer"), allow_masks=True, allow_animated=True)

    def warp_target():
        pixels()
        if not node.visible():
            raise b.OpError("Requires a visible source layer")

    features = {
        "draw_brush_pressure": support([native, (api["paintLine"], "Python painting API absent")], brush),
        "draw_brush_path": support([(api["paintPath"], "Python paintPath absent")], brush),
        "draw_brush_segments": support([(api["paintLine"], "Python paintLine absent")], brush),
        "pixel_edits": support([native], pixels),
        "selection_to_layer": support([(api["extraction_actions"], "Native extraction actions absent")], pixels),
        "transform_layer": support([(api["transform_mask"], "Transform-mask API absent"),
                                      (api["clone_document"], "Document clone API absent")], transform),
        "mask_layer": support([native, (api["transparency_mask"], "Transparency-mask API absent")], mask_target),
        "animation": support([native], animation_target),
        "warp_layer": support([native, (api["clone_document"], "Document clone previews absent")], warp_target),
        'structured_stroke':support([native],pilot),
        'bounded_repair':support([native],pilot),
        'exact_frame_repair':support([native],frame_pilot),
        'color_feedback':support([native],pilot),
    }
    return {"plugin_version": b.PLUGIN_VERSION,
            "runtime": {"krita": b._krita().version(), "python": sys.version.split()[0],
                        "qt": QtCore.qVersion(), "pyqt": QtCore.PYQT_VERSION_STR,
                        "platform": sys.platform, "byteorder": sys.byteorder},
            "python_api": api, "native_helper": helper, "target": target, "features": features,
            "contracts": {
                "pixel_edits": {"layer_types": ["paintlayer"], "color_model": "RGBA", "color_depth": "U8",
                                "masks": False, "animation": False, "transformed_ancestors": False},
                "draw_brush": {"layer_types": ["paintlayer"], "color_spaces": "Krita brush-engine supported; validated RGBA/U8",
                               "masks": False, "animation": "current raster keyframe", "transformed_ancestors": False,
                               "smudge": "Uses the selected native preset; validated presets are documented, not every engine option"},
                "transform_layer": {"layer_types": ["paintlayer", "grouplayer"], "color_model": "RGBA", "color_depth": "U8",
                                    "transparency_masks": True, "animation": False, "kind": "absolute affine native mask"},
                "warp_layer": {"kind": "raster local pull on a preserved-source duplicate", "batch": False,
                               "selection": "destination pixel difference weighted by soft alpha"},
                "mask_layer": {"kind": "transparency mask only; not arbitrary filter/selection/transform mask editing"},
                "animation": {"kind": "raster keyframes only; no skeletal rig or animated transform masks"},
                'structured_stroke':{'schema_version':1,'geometry':['polyline','cubic'],'profile':'sRGB-elle-V2-srgbtrc.icc',
                    'tilt':'zero only','pixel_exact_replay':False},
                'bounded_repair':{'candidates':3,'ttl_seconds':600,'active_tokens':8,'retained_tokens':32,
                    'bytes':64*1024*1024,'source_extent_pixels':1048576,'commit':'frozen bytes; idempotent session outcome'},
                'exact_frame_repair':{'policies':['existing_keyframe','one_sample'],'regions':['roi','whole_frame'],
                    'import':'owned document; PNG registration unchanged; FPS/anchor annotation',
                    'static_tools':'owned static candidates only; source animated guards retained'}},
            "limits": {"image_buffer_pixels": MAX_PIXELS, "stroke_points": [2, 128], "brush_size": [0.1, 2048],
                       "draw_commands": 2000, "polygon_points": [3, 2048], "batch_edits": 64,
                       "clone_dabs": 8192, "clone_dab_work_pixels": MAX_PIXELS, "native_pixel_history_bytes": 256 * 1024 * 1024,
                       "preview_checkpoints": 8, "preview_checkpoint_bytes": 128 * 1024 * 1024,
                       "animation_export_samples": 128, "filter_binary_parameter_bytes": 1024 * 1024,
                       "warp_radius": [2, MAX_RADIUS], "warp_offset_max_radius_fraction": .25,
                       "warp_sampling_pixels": MAX_WORK_PIXELS},
            "masked_layer_workflow": "Create Cleanup above the masked/transformed ancestor at the document root. "
                                     "For direct edits to the rendered shape, bake_layer on the ancestor preserves "
                                     "and hides the source; edit the unmasked baked copy."}


OPERATIONS = [("workflow_capabilities", workflow_capabilities, 20, False)]
