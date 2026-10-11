"""Absolute affine transforms via an undoable, non-destructive native mask."""

import math
import xml.etree.ElementTree as ET

from .. import ops as b
from ..compat import QColor, QImage, QPainter, QPointF, QRect, QTransform
from .common import activate, check_pixels, number, paint_layer, rect_info

MASK_NAME = "MCP Transform"
STRATEGIES = ("Bilinear", "Bicubic", "NearestNeighbor", "Lanczos3", "Box")


def transform_xml(pivot, dx, dy, degrees, sx, sy, strategy, shear_x=0, shear_y=0):
    root = ET.Element("transform_params")
    ET.SubElement(root, "main", id="tooltransformparams")
    free = ET.SubElement(ET.SubElement(root, "data", mode="0"), "free_transform")
    for name, x, y in (("originalCenter", pivot.x(), pivot.y()),
                       ("transformedCenter", pivot.x()+dx, pivot.y()+dy),
                       ("rotationCenterOffset", 0, 0)):
        ET.SubElement(free, name, type="pointf", x=str(x), y=str(y))
    for name, value in (("transformAroundRotationCenter", 0), ("aX", 0),
                        ("aY", 0), ("aZ", math.radians(degrees)),
                        ("scaleX", sx), ("scaleY", sy), ("shearX", shear_x),
                        ("shearY", shear_y), ("boundsRotation", 0),
                        ("keepAspectRatio", 0), ("filterId", strategy)):
        ET.SubElement(free, name, type="value", value=str(value))
    ET.SubElement(free, "cameraPos", type="vector3d", x="0", y="0", z="1024")
    ET.SubElement(free, "flattenedPerspectiveTransform", type="transform",
                  **{"m%d%d" % (r, c): "1" if r == c else "0"
                     for r in range(1, 4) for c in range(1, 4)})
    return ET.tostring(root, encoding="unicode")


def transform_layer(params):
    doc = b.resolve_document(params.get("document"))
    node = b.resolve_node(doc, params.get("layer"))
    if node.type() not in ("paintlayer", "grouplayer") or node.locked() or node.animated():
        raise b.OpError("Choose an unlocked static paint or group layer")
    b.imaging.require_pixel_ops(node)
    mode = str(b._arg(params, "mode", "preview"))
    if mode not in ("preview", "apply"):
        raise b.OpError("mode must be preview or apply")
    children = list(node.childNodes())
    masks = [c for c in children if c.type() == "transformmask" and c.name() == MASK_NAME]
    if len(masks) > 1 or any(c.type()=="transformmask" and c not in masks for c in children):
        raise b.OpError("Layer already has another transform mask")
    if any(n.animated() for _,_,n in b._flatten_tree(node,"","",[])):
        raise b.OpError("Animated children cannot use this static transform")
    mask = masks[0] if masks else None
    if not hasattr(doc, "createTransformMask") or (mask and not hasattr(mask, "fromXML")):
        raise b.OpError("This Krita build lacks the transform-mask API.", kind="unsupported")
    bounds = node.bounds()
    if bounds.isEmpty():
        raise b.OpError("Cannot transform an empty layer")
    check_pixels(bounds)
    raw_pivot = params.get("pivot")
    if raw_pivot is None:
        pivot = QPointF(bounds.x()+bounds.width()/2, bounds.y()+bounds.height()/2)
    elif isinstance(raw_pivot, dict):
        pivot = QPointF(number(b._req(raw_pivot, "x"), "pivot.x", -100000, 100000),
                        number(b._req(raw_pivot, "y"), "pivot.y", -100000, 100000))
    else:
        raise b.OpError("pivot must be an {x,y} object in document pixels")
    dx = number(b._arg(params, "dx", 0), "dx", -100000, 100000)
    dy = number(b._arg(params, "dy", 0), "dy", -100000, 100000)
    degrees = number(b._arg(params, "degrees", 0), "degrees", -3600, 3600)
    sx = number(b._arg(params, "scale_x", 1), "scale_x", -100, 100)
    sy = number(b._arg(params, "scale_y", 1), "scale_y", -100, 100)
    if abs(sx)<0.01 or abs(sy)<0.01:
        raise b.OpError("Absolute scale must be at least 0.01; negative scales flip")
    shear_x = number(b._arg(params,"shear_x",0),"shear_x",-10,10)
    shear_y = number(b._arg(params,"shear_y",0),"shear_y",-10,10)
    strategy = str(b._arg(params, "strategy", "Bilinear"))
    if strategy not in STRATEGIES:
        raise b.OpError("Unknown strategy; choose " + ", ".join(STRATEGIES))
    max_size = int(number(b._arg(params, "max_size", 1024), "max_size", 64, 2048))
    matrix = QTransform()
    matrix.translate(pivot.x()+dx, pivot.y()+dy)
    matrix.rotate(degrees)
    # Krita applies Y and X shear sequentially (not Qt's combined shear).
    matrix.shear(0,shear_y)
    matrix.shear(shear_x,0)
    matrix.scale(sx, sy)
    matrix.translate(-pivot.x(), -pivot.y())
    mapped = matrix.mapRect(b.QRectF(bounds)).toAlignedRect()
    check_pixels(mapped)
    result = {"layer": node.name(), "mode": mode,
              "pivot": {"x": pivot.x(), "y": pivot.y()},
              "source_bounds": rect_info(bounds), "transformed_bounds": rect_info(mapped),
              "matrix": [matrix.m11(), matrix.m12(), matrix.m21(), matrix.m22(),
                         matrix.dx(), matrix.dy()],
              "absolute": True,
              "note": "Parameters replace this layer's MCP transform relative to "
                      "the original pixels. Selection does not limit the whole-layer transform."}
    if mode == "preview":
        canvas = QRect(0, 0, doc.width(), doc.height())
        check_pixels(canvas)
        clone=doc.clone()
        if clone is None: raise b.OpError("Could not clone document for preview")
        try:
            clone_node=b.resolve_node(clone,"uuid:"+b._node_uuid(node))
            clone_mask=next((c for c in clone_node.childNodes()
                             if c.type()=="transformmask" and c.name()==MASK_NAME),None)
            if clone_mask is None:
                clone_mask=clone.createTransformMask(MASK_NAME)
                if not clone_node.addChildNode(clone_mask,None):
                    raise b.OpError("Could not attach preview transform mask")
                clone.waitForDone()
            if not clone_mask.fromXML(transform_xml(pivot,dx,dy,degrees,sx,sy,strategy,shear_x,shear_y)):
                raise b.OpError("Krita rejected preview transform")
            b._refresh(clone)
            preview=clone.projection(0,0,doc.width(),doc.height()).copy()
            t=clone_mask.finalAffineTransform()
            result["native_matrix"]=[t.m11(),t.m12(),t.m21(),t.m22(),t.dx(),t.dy()]
        finally:
            clone.setBatchmode(True);clone.setModified(False);clone.close()
        preview, factor = b.imaging.scale_to_fit(preview, max_size)
        result.update({"png_base64": b.imaging.qimage_to_png_b64(preview),
                       "width": preview.width(), "height": preview.height(),
                       "scale": factor, "mutated": False,
                       "preview_kind": "krita_composite",
                       "preview_note": "Rendered by Krita on a detached document clone."})
        return result
    activate(doc, node)
    created = mask is None
    if created:
        mask = doc.createTransformMask(MASK_NAME)
        if mask is None:
            raise b.OpError("Krita could not create a transform mask", kind="native_failure")
        if not node.addChildNode(mask, None):
            raise b.OpError("Krita could not attach the transform mask", kind="native_failure")
        doc.waitForDone()
    if not mask.fromXML(transform_xml(pivot, dx, dy, degrees, sx, sy, strategy,shear_x,shear_y)):
        raise b.OpError("Krita rejected the transform parameters", kind="native_failure")
    b._refresh(doc)
    doc.setActiveNode(node)
    t = mask.finalAffineTransform()
    result.update({"mask": {"name": mask.name(), "uuid": b._node_uuid(mask)},
                   "native_matrix": [t.m11(), t.m12(), t.m21(), t.m22(), t.dx(), t.dy()],
                   "undoable": True, "undo_steps": 1,
                   "mask_created": created,
                   "history_note": "One Undo restores the previous pose. First apply "
                                   "also creates an identity mask in a separate history entry."})
    return result


OPERATIONS = [("transform_layer", transform_layer, 90, True)]
