"""Game-asset helpers: antialiased layer outlines and per-layer PNG export."""
import json
import math
from pathlib import Path
import re
import shutil
import tempfile

from .. import ops as b
from ..compat import QColor, QImage, QPainter, QPointF, QRect
from .animation import integer
from .common import check_pixels, number

MAX_OUTLINE = 32
MAX_EXPORT_LAYERS = 256
_THRESHOLD = bytes(0 if a < 128 else 255 for a in range(256))
_INVERT = bytes(255 - a for a in range(256))


def _rendered(node, rect):
    """The layer's own rendered pixels (masks applied), straight ARGB32 bytes."""
    raw = bytes(node.projectionPixelData(rect.x(), rect.y(), rect.width(), rect.height()))
    if len(raw) != rect.width() * rect.height() * 4:
        raise b.OpError("Krita returned an incomplete layer buffer")
    return raw


def _alpha_image(alpha, width, height):
    return QImage(alpha, width, height, width, QImage.Format_Alpha8).copy()


def _dilate(mask, radius, smooth):
    """Union of the mask shifted to every offset within ``radius``."""
    out = QImage(mask.size(), QImage.Format_ARGB32_Premultiplied)
    out.fill(QColor(0, 0, 0, 0))
    painter = QPainter(out)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, smooth)
    reach = int(math.floor(radius))
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            if dx * dx + dy * dy <= radius * radius:
                painter.drawImage(dx, dy, mask)
    if smooth:
        # Sub-pixel shifts along the rim round the outline and antialias it.
        # A near-identity scale keeps Qt from snapping them to whole pixels.
        steps = max(8, int(math.ceil(4 * math.pi * radius)))
        for k in range(steps):
            turn = 2 * math.pi * k / steps
            painter.save()
            painter.translate(QPointF(radius * math.cos(turn), radius * math.sin(turn)))
            painter.scale(1.000001, 1.000001)
            painter.drawImage(0, 0, mask)
            painter.restore()
    painter.end()
    return out


def _keep_inside(target, mask):
    painter = QPainter(target)
    painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, mask)
    painter.end()


def outline_layer(params):
    from ..native_commands import group
    doc = b.resolve_document(params.get("document"))
    node = b.resolve_node(doc, params.get("layer"))
    if node.type().endswith("mask"):
        raise b.OpError("Choose a layer, not a mask, to outline")
    b.imaging.require_pixel_ops(node)
    width = number(b._arg(params, "width", 2), "width", 0.5, MAX_OUTLINE)
    position = str(b._arg(params, "position", "outside"))
    if position not in ("outside", "inside", "center"):
        raise b.OpError("position must be outside, inside or center")
    smooth = b._as_bool(b._arg(params, "antialias", True), "antialias")
    color = b.parse_color(b._arg(params, "color", "#000000"))
    name = str(b._arg(params, "name", node.name() + " outline")).strip()
    if not name:
        raise b.OpError("name cannot be empty")
    doc.waitForDone()
    pad = int(math.ceil(width)) + 2
    region = node.bounds().adjusted(-pad, -pad, pad, pad).intersected(
        QRect(0, 0, doc.width(), doc.height()))
    if region.isEmpty():
        raise b.OpError("Layer has no pixels on the canvas to outline")
    check_pixels(region)
    alpha = _rendered(node, region)[3::4]
    if not alpha.strip(b"\0"):
        raise b.OpError("Layer has no pixels to outline")
    if not smooth:
        alpha = alpha.translate(_THRESHOLD)
    w, h = region.width(), region.height()
    shape = _alpha_image(alpha, w, h)
    if position == "outside":
        band = _dilate(shape, width, smooth)
    else:
        # Inside: shape pixels within reach of the outside. Center: that
        # band at half width, joined to the shape grown by half width.
        reach = width if position == "inside" else width / 2.0
        band = _dilate(_alpha_image(alpha.translate(_INVERT), w, h), reach, smooth)
        _keep_inside(band, shape if position == "inside" else _dilate(shape, reach, smooth))
    painter = QPainter(band)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(band.rect(), color)
    painter.end()
    image = band.convertToFormat(QImage.Format_ARGB32)
    parent = node.parentNode()
    siblings = list(parent.childNodes())  # bottom first
    below = siblings.index(node) - 1
    with group(doc, "MCP outline"):
        layer = doc.createNode(name, "paintlayer")
        b.imaging.write_node_image(layer, region, image)
        # An outside outline sits under its source and fills beneath it, so
        # antialiased source edges cover it without a seam.
        anchor = node if position != "outside" else (siblings[below] if below >= 0 else None)
        if not parent.addChildNode(layer, anchor):
            raise b.OpError("Could not attach the outline layer")
    b._refresh(doc)
    return {"layer": b._find_in_tree(b.layer_tree(doc), layer), "source": node.name(),
            "position": position, "width": width, "antialias": smooth,
            "region": {"x": region.x(), "y": region.y(), "width": w, "height": h},
            "undoable": True, "undo_steps": 1}


def _alpha_bounds(alpha, width, height):
    """Exact box of nonzero alpha; layer bounds() can round to tiles."""
    top, bottom, left, right = None, -1, width, -1
    empty = bytes(width)
    for y in range(height):
        row = alpha[y * width:(y + 1) * width]
        if row == empty:
            continue
        if top is None:
            top = y
        bottom = y
        left = min(left, width - len(row.lstrip(b"\0")))
        right = max(right, len(row.rstrip(b"\0")) - 1)
    if top is None:
        return None
    return QRect(left, top, right - left + 1, bottom - top + 1)


def _file_name(name, used):
    stem = re.sub(r"[^\w.-]+", "_", name).strip("._") or "layer"
    candidate, count = stem, 2
    while candidate.lower() in used:
        candidate = "{0}_{1}".format(stem, count)
        count += 1
    used.add(candidate.lower())
    return candidate + ".png"


def export_layers(params):
    doc = b.resolve_document(params.get("document"))
    output = Path(str(b._req(params, "directory"))).expanduser()
    if not output.is_absolute():
        raise b.OpError("directory must be an absolute path")
    if output.exists():
        raise b.OpError("Export directory must be new; existing files are preserved")
    refs = params.get("layers")
    if refs is None:
        nodes = [n for n in b._children(doc.rootNode())
                 if n.visible() and not n.type().endswith("mask")]
    else:
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_EXPORT_LAYERS:
            raise b.OpError("layers must list 1 to {0} layers".format(MAX_EXPORT_LAYERS))
        nodes = [b.resolve_node(doc, ref) for ref in refs]
    if not nodes:
        raise b.OpError("No visible top-level layers to export")
    for node in nodes:
        if node.type().endswith("mask"):
            raise b.OpError("{0!r} is a mask; export its layer instead".format(node.name()))
        b.imaging.require_pixel_ops(node)
    trim = b._as_bool(b._arg(params, "trim", True), "trim")
    padding = integer(b._arg(params, "padding", 0), "padding", 0, 512)
    canvas = QRect(0, 0, doc.width(), doc.height())
    check_pixels(canvas)
    doc.waitForDone()
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".krita-layers-", dir=str(output.parent)))
    entries, skipped, used = [], [], set()
    try:
        for node in nodes:
            area = node.bounds().intersected(canvas) if trim else canvas
            raw = _rendered(node, area) if not area.isEmpty() else b""
            box = _alpha_bounds(raw[3::4], area.width(), area.height()) if raw else None
            if trim and box is None:
                skipped.append(node.name())
                continue
            image = QImage(raw, area.width(), area.height(), area.width() * 4,
                           QImage.Format_ARGB32).copy()
            x, y = area.x(), area.y()
            if trim:
                image = image.copy(box)
                x, y = x + box.x(), y + box.y()
            if padding:
                padded = QImage(image.width() + 2 * padding, image.height() + 2 * padding,
                                QImage.Format_ARGB32)
                padded.fill(QColor(0, 0, 0, 0))
                painter = QPainter(padded)
                painter.setCompositionMode(QPainter.CompositionMode_Source)
                painter.drawImage(padding, padding, image)
                painter.end()
                image, x, y = padded, x - padding, y - padding
            name = _file_name(node.name(), used)
            if not image.save(str(stage / name), "PNG"):
                raise b.OpError("Could not save " + name)
            entries.append({"layer": node.name(), "file": name, "x": x, "y": y,
                            "width": image.width(), "height": image.height()})
        if not entries:
            raise b.OpError("Every chosen layer is empty")
        manifest = {"document": doc.name(), "canvas": {"width": doc.width(), "height": doc.height()},
                    "trim": trim, "padding": padding, "layers": entries,
                    "note": "x, y place each PNG's top-left corner on the canvas."}
        (stage / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"directory": str(output), "manifest": str(output / "manifest.json"),
            "layers": entries, "skipped_empty": skipped, "mutated": False}


OPERATIONS = [("outline_layer", outline_layer, 120, True),
              ("export_layers", export_layers, 120, False)]
