"""Background removal, edge defringing and palette recolouring for assets."""
import bisect
import math
import re

from .. import ops as b
from ..compat import QColor, QImage, QRect
from .animation import integer
from .assets import _INVERT, _THRESHOLD, _alpha_image, _bitwise, _corner_color, _dilate, _near_tables
from .common import check_pixels, paint_layer

_RUN = re.compile(rb"\x01+")
_PARTIAL = re.compile(rb"[\x01-\xfe]+")
_BAYER = (0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5)
MAX_SWAPS = 16
MAX_SAMPLES = 40000


def _target(params):
    doc = b.resolve_document(params.get("document"))
    node = paint_layer(doc, params.get("layer"))
    b.imaging.require_pixel_ops(node)
    doc.waitForDone()
    region = node.bounds().intersected(QRect(0, 0, doc.width(), doc.height()))
    if region.isEmpty():
        raise b.OpError("Layer has no pixels on the canvas")
    check_pixels(region)
    return doc, node, region, b.imaging.read_node_image(node, region)


def _commit(doc, node, region, before, data, label):
    from ..native_commands import write_pixels
    after = QImage(bytes(data), region.width(), region.height(), region.width() * 4,
                   QImage.Format_ARGB32).copy()
    after = b.imaging.constrain_to_selection(before, after, doc.selection(), region)
    return write_pixels(doc, node, region, after, label)


def _rgb(color):
    return color.red(), color.green(), color.blue()


def _distance(raw, color):
    """Per pixel, the largest channel distance from ``color``."""
    tables = [bytes(abs(v - c) for v in range(256)) for c in color]
    return bytes(map(max, raw[2::4].translate(tables[0]), raw[1::4].translate(tables[1]),
                     raw[0::4].translate(tables[2])))


def _background_runs(candidate, width, height, holes=0):
    """Candidate runs, as absolute (start, end), connected to the region edge.

    With ``holes``, an enclosed candidate area of at least that many pixels,
    such as backdrop seen between legs, counts as background too.
    """
    rows = [[m.span() for m in _RUN.finditer(candidate, y * width, (y + 1) * width)]
            for y in range(height)]
    starts = [[s - y * width for s, _ in row] for y, row in enumerate(rows)]
    seen = [bytearray(len(row)) for row in rows]
    kept = []
    for y0, row in enumerate(rows):
        for i0 in range(len(row)):
            if seen[y0][i0]:
                continue
            seen[y0][i0] = 1
            queue, members, area, edge = [(y0, i0)], [], 0, False
            while queue:
                y, i = queue.pop()
                s, e = rows[y][i]
                members.append((s, e))
                area += e - s
                x0, x1 = s - y * width, e - y * width
                edge = edge or y in (0, height - 1) or x0 == 0 or x1 == width
                for ny in (y - 1, y + 1):
                    if not 0 <= ny < height:
                        continue
                    # Runs in the next row that overlap [x0, x1): sorted, disjoint.
                    k = bisect.bisect_right(starts[ny], x1 - 1) - 1
                    while k >= 0 and rows[ny][k][1] - ny * width > x0:
                        if not seen[ny][k]:
                            seen[ny][k] = 1
                            queue.append((ny, k))
                        k -= 1
            if edge or (holes and area >= holes):
                kept.extend(members)
    return kept


def _unmix(data, index, alpha, background):
    """Remove the background share from a pixel seen at coverage ``alpha``."""
    a = alpha / 255.0
    offset = index * 4
    for channel, bg in zip((2, 1, 0), background):
        value = (data[offset + channel] - (1 - a) * bg) / a
        data[offset + channel] = 0 if value < 0 else 255 if value > 255 else int(value + 0.5)


EDGE_BAND = 2
_NEIGHBOURS = sorted(((dx, dy) for dy in range(-3, 4) for dx in range(-3, 4)
                      if 0 < dx * dx + dy * dy <= 9), key=lambda d: d[0] * d[0] + d[1] * d[1])


def _unmix_edges(data, alpha, old, near_removed, solid, width, height, background):
    """Re-estimate coverage and colour of pixels next to the removed background.

    Each edge pixel C is read as a mix of the nearest solid foreground
    colour F and the background B: coverage a = (C-B).(F-B) / |F-B|^2, and
    the colour becomes (C - (1-a)B) / a. A pixel without a solid neighbour
    keeps the matte it has.
    """
    bg = background
    for match in _RUN.finditer(near_removed):
        for i in range(*match.span()):
            if not alpha[i]:
                continue
            x, y = i % width, i // width
            for dx, dy in _NEIGHBOURS:
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height and solid[ny * width + nx]:
                    j = (ny * width + nx) * 4
                    fg = (data[j + 2], data[j + 1], data[j])
                    break
            else:
                continue
            offset = i * 4
            color = (data[offset + 2], data[offset + 1], data[offset])
            axis = [f - g for f, g in zip(fg, bg)]
            length = sum(v * v for v in axis)
            if length < 1:
                continue
            a = sum((c - g) * v for c, g, v in zip(color, bg, axis)) / length
            a = 0.0 if a < 0 else 1.0 if a > 1 else a
            alpha[i] = min(alpha[i], int(a * old[i] + 0.5))
            if alpha[i]:
                _unmix(data, i, int(a * 255 + 0.5) or 1, bg)


def _cleared(raw, width, height, background, mode, tolerance, softness, defringe, shrink,
             holes=0):
    """ARGB32 bytes with the background made transparent; pure, no Krita."""
    old = raw[3::4]
    alpha = bytearray(old)
    data = bytearray(raw)
    if mode == "defringe":
        if defringe:
            for match in _PARTIAL.finditer(old):
                for i in range(*match.span()):
                    _unmix(data, i, old[i], background)
    else:
        # Coverage of the foreground: 0 within tolerance, rising to 255
        # across the softness band.
        ramp = bytes(0 if d <= tolerance else 255 if d >= tolerance + softness
                     else int((d - tolerance) * 255 / softness + 0.5) for d in range(256))
        distance = _distance(raw, background)
        matte = distance.translate(ramp)
        candidate = _bitwise([matte.translate(bytes([1]) * 255 + bytes([0])),
                              old.translate(bytes([1]) + bytes(255))],
                             lambda x, y: x | y, len(old))
        runs = (_background_runs(candidate, width, height, holes) if mode == "contiguous"
                else [m.span() for m in _RUN.finditer(candidate)])
        removed = bytearray(len(old))
        for s, e in runs:
            alpha[s:e] = bytes(map(min, alpha[s:e], matte[s:e]))
            removed[s:e] = matte[s:e].translate(bytes([1]) * 255 + bytes([0]))
        if defringe and runs:
            spread = _dilate(_alpha_image(bytes(removed).translate(bytes([0]) + bytes([255]) * 255),
                                          width, height), EDGE_BAND, False)
            near_removed = b.imaging.image_bytes(spread.convertToFormat(QImage.Format_ARGB32))[3::4]
            near_removed = near_removed.translate(bytes([0]) + bytes([1]) * 255)
            # Solid: visible pixels clearly unlike the background, beyond the
            # edge band, so a blended rim pixel is never taken as foreground.
            far = distance.translate(bytes(0 if d < tolerance + softness else 1 for d in range(256)))
            solid = _bitwise([far, near_removed.translate(bytes([1, 0]) + bytes(254)),
                              old.translate(bytes([0]) + bytes([1]) * 255)],
                             lambda x, y: x & y, len(old))
            _unmix_edges(data, alpha, old, near_removed, solid, width, height, background)
    if shrink:
        inverse = _alpha_image(bytes(alpha).translate(_INVERT), width, height)
        grown = _dilate(inverse, shrink, False).convertToFormat(QImage.Format_ARGB32)
        alpha = bytearray(map(min, alpha, b.imaging.image_bytes(grown)[3::4].translate(_INVERT)))
    data[3::4] = alpha
    return data


def remove_background(params):
    doc, node, region, before = _target(params)
    if node.alphaLocked():
        raise b.OpError("The layer's alpha is locked; unlock it with set_layer first")
    mode = str(b._arg(params, "mode", "contiguous"))
    if mode not in ("contiguous", "global", "defringe"):
        raise b.OpError("mode must be contiguous, global or defringe")
    tolerance = integer(b._arg(params, "tolerance", 12), "tolerance", 0, 255)
    softness = integer(b._arg(params, "softness", 12), "softness", 0, 255)
    shrink = integer(b._arg(params, "shrink", 0), "shrink", 0, 8)
    holes = integer(b._arg(params, "holes", 0), "holes", 0, 1 << 24)
    defringe = b._as_bool(b._arg(params, "defringe", True), "defringe")
    width, height = region.width(), region.height()
    raw = b.imaging.image_bytes(before)
    if params.get("color") is not None:
        background = _rgb(b.parse_color(params["color"]))
    elif mode == "defringe":
        raise b.OpError("mode defringe needs the old background color")
    else:
        corner = _corner_color(raw, width, height)
        if corner[3] < 128:
            raise b.OpError("The layer's corners are already transparent; pass color, "
                            "or use mode defringe to clean the edges")
        background = corner[:3]
    data = _cleared(raw, width, height, background, mode, tolerance, softness, defringe, shrink,
                    holes)
    steps = _commit(doc, node, region, before, data, "MCP remove background")
    old, alpha = raw[3::4], data[3::4]
    return {"layer": node.name(), "mode": mode,
            "background": "#{0:02x}{1:02x}{2:02x}".format(*background),
            "cleared_pixels": sum(1 for o, n in zip(old, alpha) if o and not n),
            "softened_pixels": sum(1 for o, n in zip(old, alpha) if 0 < n < o),
            "region": {"x": region.x(), "y": region.y(), "width": width, "height": height},
            "selection_respected": True, "undoable": True, "undo_steps": steps}


def _median_cut(samples, count):
    """Average colours of ``count`` boxes split at the median of their widest channel."""
    def spans(box):
        return [max(p[k] for p in box) - min(p[k] for p in box) for k in range(3)]
    boxes = [(samples, spans(samples))]
    while len(boxes) < count:
        index, widest = max(enumerate(boxes), key=lambda item: max(item[1][1]))
        if max(widest[1]) == 0 or len(widest[0]) < 2:
            break
        channel = widest[1].index(max(widest[1]))
        ordered = sorted(widest[0], key=lambda p: p[channel])
        half = len(ordered) // 2
        boxes[index:index + 1] = [(ordered[:half], spans(ordered[:half])),
                                  (ordered[half:], spans(ordered[half:]))]
    return [QColor(*(sum(p[k] for p in box) // len(box) for k in range(3))) for box, _ in boxes]


def _palette(params, key):
    values = params.get(key)
    if not isinstance(values, list) or not 1 <= len(values) <= 256:
        raise b.OpError("{0} must list 1 to 256 colours".format(key))
    return [b.parse_color(value) for value in values]


def _dither(data, width, height, palette):
    """Ordered 4x4 Bayer offsets, scaled to the palette's colour spacing."""
    if len(palette) < 2:
        return
    steps = []
    for color in palette:
        steps.append(min(math.dist(_rgb(color), _rgb(other)) for other in palette
                         if other is not color))
    spread = sum(steps) / len(steps) / math.sqrt(3)
    tables = []
    for level in _BAYER:
        offset = int(round(((level + 0.5) / 16 - 0.5) * spread))
        tables.append(bytes(min(255, max(0, v + offset)) for v in range(256)))
    stride = width * 4
    for y in range(height):
        row = data[y * stride:(y + 1) * stride]
        for i in range(4):
            table = tables[(y % 4) * 4 + i]
            for channel in range(3):
                row[i * 4 + channel::16] = row[i * 4 + channel::16].translate(table)
        data[y * stride:(y + 1) * stride] = row


def _quantized(raw, width, height, palette, replace, dither):
    """ARGB32 bytes mapped to the nearest palette colour; alpha unchanged."""
    data = bytearray(raw)
    if dither == "ordered":
        _dither(data, width, height, palette)
    # Qt maps each pixel to the nearest table colour; alpha is restored after.
    image = QImage(bytes(data), width, height, width * 4, QImage.Format_RGB32).copy()
    indexed = image.convertToFormat(QImage.Format_Indexed8,
                                    [QColor(*_rgb(c)).rgba() for c in palette])
    if replace is not None:
        indexed.setColorTable([QColor(*_rgb(c)).rgba() for c in replace])
    data = bytearray(b.imaging.image_bytes(indexed.convertToFormat(QImage.Format_ARGB32)))
    data[3::4] = raw[3::4]
    return data


def _swapped(raw, palette, replace, tolerance):
    """ARGB32 bytes with colours near each palette entry shifted to its replacement."""
    alpha = raw[3::4]
    visible = alpha.translate(bytes([0]) + bytes([1]) * 255)
    claimed = 0
    data = bytearray(raw)
    counts = []
    for source, target in zip(palette, replace):
        near = _near_tables(_rgb(source), tolerance)
        hit = int.from_bytes(_bitwise([raw[2::4].translate(near[0]), raw[1::4].translate(near[1]),
                                       raw[0::4].translate(near[2]), visible],
                                      lambda x, y: x & y, len(alpha)), "big") & ~claimed
        claimed |= hit
        hit = hit.to_bytes(len(alpha), "big")
        # Shift by the difference, so shading inside the match survives.
        shifts = [bytes(min(255, max(0, v + t - s)) for v in range(256))
                  for s, t in zip(_rgb(source), _rgb(target))]
        for match in _RUN.finditer(hit):
            s, e = match.span()
            for channel, table in zip((2, 1, 0), shifts):
                data[s * 4 + channel:e * 4:4] = data[s * 4 + channel:e * 4:4].translate(table)
        counts.append(hit.count(1))
    return data, counts


def quantize_layer(params):
    doc, node, region, before = _target(params)
    mode = str(b._arg(params, "mode", "quantize"))
    if mode not in ("quantize", "swap"):
        raise b.OpError("mode must be quantize or swap")
    alpha_mode = str(b._arg(params, "alpha", "keep"))
    if alpha_mode not in ("keep", "threshold"):
        raise b.OpError("alpha must be keep or threshold")
    if alpha_mode == "threshold" and node.alphaLocked():
        raise b.OpError("The layer's alpha is locked; alpha threshold would change it")
    width, height = region.width(), region.height()
    raw = b.imaging.image_bytes(before)
    replace = _palette(params, "replace") if params.get("replace") is not None else None
    result = {"layer": node.name(), "mode": mode}
    if mode == "quantize":
        if (params.get("palette") is None) == (params.get("colors") is None):
            raise b.OpError("quantize needs palette or colors, not both")
        if params.get("palette") is not None:
            palette = _palette(params, "palette")
        else:
            count = integer(params["colors"], "colors", 2, 256)
            step = max(1, width * height // MAX_SAMPLES)
            samples = [(raw[i * 4 + 2], raw[i * 4 + 1], raw[i * 4])
                       for i in range(0, width * height, step) if raw[i * 4 + 3] >= 128]
            if not samples:
                raise b.OpError("The layer has no visible pixels to build a palette from")
            palette = _median_cut(samples, count)
        if replace is not None and len(replace) != len(palette):
            raise b.OpError("replace must have one colour per palette entry ({0})"
                            .format(len(palette)))
        dither = str(b._arg(params, "dither", "none"))
        if dither not in ("none", "ordered"):
            raise b.OpError("dither must be none or ordered")
        data = _quantized(raw, width, height, palette, replace, dither)
        result.update(palette=["#{0:02x}{1:02x}{2:02x}".format(*_rgb(c)) for c in palette],
                      dither=dither)
    else:
        if params.get("palette") is None or replace is None:
            raise b.OpError("swap needs palette and replace")
        palette = _palette(params, "palette")
        if len(palette) != len(replace) or len(palette) > MAX_SWAPS:
            raise b.OpError("swap needs palette and replace of equal length, at most {0}"
                            .format(MAX_SWAPS))
        tolerance = integer(b._arg(params, "tolerance", 32), "tolerance", 0, 255)
        data, counts = _swapped(raw, palette, replace, tolerance)
        result.update(swapped_pixels=counts, tolerance=tolerance)
    if alpha_mode == "threshold":
        data[3::4] = raw[3::4].translate(_THRESHOLD)
    steps = _commit(doc, node, region, before, data, "MCP quantize layer")
    result.update(alpha=alpha_mode, selection_respected=True, undoable=True, undo_steps=steps,
                  region={"x": region.x(), "y": region.y(), "width": width, "height": height})
    return result


OPERATIONS = [("remove_background", remove_background, 180, True),
              ("quantize_layer", quantize_layer, 180, True)]
