"""Bounded raster local pull; source preservation and actual composite preview.

Inverse map at destination pixel center p:
    sample = p - offset * (1 - |p - (center + offset)|^2/radius^2)^2
inside the destination disk; identity outside it. The displacement bound
keeps this inverse map smooth and avoids folds. This is not a native mesh.
"""
import hashlib
import math

from .. import ops as b
from ..compat import QImage, QRect
from .common import check_pixels, number, paint_layer, rect_info

MAX_RADIUS = 256
MAX_WORK_PIXELS = 1024 * 1024


def vector(value, name):
    if not isinstance(value, dict) or set(value) != {"x", "y"}:
        raise b.OpError(name + " must be an {x,y} object")
    return tuple(number(value[key], name + "." + key, -100000, 100000) for key in ("x", "y"))


def render_pull(doc, node, center, offset, radius):
    end = (center[0] + offset[0], center[1] + offset[1])
    left, top = math.floor(end[0] - radius), math.floor(end[1] - radius)
    right, bottom = math.ceil(end[0] + radius), math.ceil(end[1] + radius)
    rect = QRect(left, top, right - left, bottom - top).intersected(QRect(0, 0, doc.width(), doc.height()))
    if rect.isEmpty():
        raise b.OpError("The influence disk does not intersect the canvas")
    pad = math.ceil(math.hypot(*offset)) + 1
    sample_rect = rect.adjusted(-pad, -pad, pad, pad)
    if sample_rect.width() * sample_rect.height() > MAX_WORK_PIXELS:
        raise b.OpError("Warp exceeds the 1 megapixel sampling budget; reduce radius")
    # Read outside the canvas too: Krita may store real source pixels there.
    source = b.imaging.image_bytes(b.imaging.read_node_image(node, sample_rect))
    before = b.imaging.image_bytes(b.imaging.read_node_image(node, rect))
    out = bytearray(before)
    selection = doc.selection()
    mask = bytes(selection.pixelData(rect.x(), rect.y(), rect.width(), rect.height())) if selection else None
    if mask is not None and len(mask) != rect.width() * rect.height():
        raise b.OpError("Invalid selection mask buffer")
    sw = sample_rect.width()
    changed = 0
    for y in range(rect.height()):
        py = rect.y() + y + .5
        for x in range(rect.width()):
            pixel = y * rect.width() + x
            strength = mask[pixel] / 255 if mask is not None else 1
            if not strength:
                continue
            px = rect.x() + x + .5
            distance = ((px - end[0]) ** 2 + (py - end[1]) ** 2) / radius ** 2
            if distance >= 1:
                continue
            weight = (1 - distance) ** 2
            if offset == (0, 0):
                continue
            # Coordinates of sample pixel centers, expressed as array indices.
            sx = px - offset[0] * weight - sample_rect.x() - .5
            sy = py - offset[1] * weight - sample_rect.y() - .5
            ix, iy = math.floor(sx), math.floor(sy)
            fx, fy = sx - ix, sy - iy
            values = [0., 0., 0., 0.]
            for ox, oy, factor in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                                   (0, 1, (1-fx)*fy), (1, 1, fx*fy)):
                index = ((iy + oy) * sw + ix + ox) * 4
                alpha = source[index + 3]
                for c in range(3):
                    values[c] += source[index+c] * alpha / 255 * factor
                values[3] += alpha * factor
            i = pixel * 4
            old_alpha = before[i+3]
            alpha = old_alpha * (1-strength) + values[3] * strength
            a = min(255, max(0, round(alpha)))
            # Interpolate premultiplied color and alpha, then unpremultiply;
            # transparent edges retain their color instead of a black fringe.
            color = [min(255, max(0, round((before[i+c] * old_alpha / 255 * (1-strength) +
                                           values[c] * strength) * 255 / alpha))) if a else 0
                     for c in range(3)]
            color.append(a)
            if bytes(color) != before[i:i+4]:
                changed += 1
                out[i:i+4] = bytes(color)
    image = QImage(bytes(out), rect.width(), rect.height(), rect.width()*4, QImage.Format_ARGB32).copy()
    return rect, image, changed, hashlib.sha256(source).hexdigest(), hashlib.sha256(out).hexdigest()


def staged_copy(doc, node, name, rect, image):
    copy = node.duplicate()
    if copy is None:
        raise b.OpError("Krita could not duplicate the warp source", kind="native_failure")
    copy.setName(name)
    b.imaging.write_node_image(copy, rect, image)
    return copy


def warp_layer(params):
    doc = b.resolve_document(params.get("document"))
    node = paint_layer(doc, params.get("layer"))
    b.imaging.require_pixel_ops(node)
    mode = str(b._arg(params, "mode", "preview"))
    if mode not in ("preview", "apply"):
        raise b.OpError("mode must be preview or apply")
    center = vector(b._req(params, "center"), "center")
    offset = vector(b._req(params, "offset"), "offset")
    radius = number(b._req(params, "radius"), "radius", 2, MAX_RADIUS)
    if math.hypot(*offset) > radius / 4:
        raise b.OpError("offset length must be <= radius/4 to avoid folds; use several small edits")
    if not (0 <= center[0] < doc.width() and 0 <= center[1] < doc.height()):
        raise b.OpError("center must be inside the canvas")
    end = (center[0] + offset[0], center[1] + offset[1])
    if not (0 <= end[0] < doc.width() and 0 <= end[1] < doc.height()):
        raise b.OpError("center + offset must be inside the canvas")
    name = b._arg(params, "name", node.name() + " warped")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise b.OpError("name must contain 1 to 200 characters")
    if not node.visible():
        raise b.OpError("Choose a visible source layer so preview/apply can replace its visible result")
    max_size = number(b._arg(params, "max_size", 1024), "max_size", 64, 2048)
    if not max_size.is_integer():
        raise b.OpError("max_size must be an integer")
    # Validate the preview allocation and native ABI before any document edits.
    if mode == "preview":
        check_pixels(QRect(0, 0, doc.width(), doc.height()))
        if not hasattr(doc, "clone"):
            raise b.OpError("This Krita build lacks document clone previews", kind="unsupported")
    from ..native_commands import library, group, visibility
    library()
    doc.waitForDone()
    rect, image, changed, source_hash, patch_hash = render_pull(doc, node, center, offset, radius)
    result = {"mode": mode, "kind": "raster_local_pull", "source": {"name": node.name(), "uuid": b._node_uuid(node)},
              "center": {"x": center[0], "y": center[1]}, "offset": {"x": offset[0], "y": offset[1]},
              "radius": radius, "influence_center": {"x": end[0], "y": end[1]}, "region": rect_info(rect),
              "changed_pixels": changed, "source_sample_sha256": source_hash, "patch_sha256": patch_hash,
              "source_preserved": True, "selection_respected": True,
              "selection_semantics": "Soft destination mask weights the before/warped difference in premultiplied RGBA. "
                                     "Source sampling is not clipped by selection; zero-weight pixels remain byte-exact.",
              "note": "Bilinear raster resampling on a new copy; source is hidden on apply. "
                      "Preview and apply recompute from current source/selection. This is not a native mesh mask."}
    if mode == "preview":
        clone = doc.clone()
        if clone is None:
            raise b.OpError("Could not clone document for warp preview")
        try:
            if changed:
                clone_node = b.resolve_node(clone, "uuid:" + b._node_uuid(node))
                copy = staged_copy(clone, clone_node, name, rect, image)
                if not clone_node.parentNode().addChildNode(copy, clone_node):
                    raise b.OpError("Could not attach preview warp copy")
                clone_node.setVisible(False)
            b._refresh(clone)
            preview = clone.projection(0, 0, doc.width(), doc.height()).copy()
        finally:
            clone.setBatchmode(True)
            clone.setModified(False)
            clone.close()
        preview, factor = b.imaging.scale_to_fit(preview, int(max_size))
        result.update(png_base64=b.imaging.qimage_to_png_b64(preview), width=preview.width(),
                      height=preview.height(), scale=factor, mutated=False, undo_steps=0,
                      preview_kind="krita_composite")
        return result
    if not changed:
        result.update(mutated=False, undoable=True, undo_steps=0, layer=None)
        return result
    copy = staged_copy(doc, node, name, rect, image)
    previous_active = doc.activeNode()
    try:
        with group(doc, "MCP local raster warp"):
            if not node.parentNode().addChildNode(copy, node):
                raise b.OpError("Could not attach warped copy", kind="native_failure")
            visibility(doc, node, False)
    except Exception:
        doc.setActiveNode(previous_active)
        raise
    doc.setActiveNode(copy)
    b._refresh(doc)
    result.update(layer=b._find_in_tree(b.layer_tree(doc), copy), mutated=True, undoable=True, undo_steps=1)
    return result


OPERATIONS = [("warp_layer", warp_layer, 180, True)]
