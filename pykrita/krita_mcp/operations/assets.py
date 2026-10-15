"""Game-asset helpers: outlines, per-layer PNG export and sprite-sheet slicing."""
import bisect
import json
import math
from pathlib import Path
import re
import shutil

from .. import ops as b
from ..compat import QColor, QImage, QPainter, QPointF, QRect
from .animation import integer
from .common import check_pixels, number, stage_directory

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
    stage = stage_directory(output.parent, ".krita-layers-")
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


MAX_SLICES = 1024
MAX_SLICE_LAYERS = 256


def _corner_color(raw, width, height):
    """Median colour of 3x3 patches at the four corners, as (r, g, b, a)."""
    samples = []
    for cx, cy in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        for y in range(max(0, cy - 1), min(height, cy + 2)):
            for x in range(max(0, cx - 1), min(width, cx + 2)):
                i = (y * width + x) * 4
                samples.append((raw[i + 2], raw[i + 1], raw[i], raw[i + 3]))
    return tuple(sorted(s[k] for s in samples)[len(samples) // 2] for k in range(4))


def _near_tables(color, tolerance):
    """Per channel, 1 where a byte is within tolerance of the colour."""
    return [bytes(1 if abs(v - c) <= tolerance else 0 for v in range(256)) for c in color[:3]]


def _bitwise(parts, op, length):
    value = int.from_bytes(parts[0], "big")
    for part in parts[1:]:
        value = op(value, int.from_bytes(part, "big"))
    return value.to_bytes(length, "big")


def _content_mask(raw, width, height, background, threshold, tolerance):
    """One byte per pixel, 1 where the sheet has content."""
    alpha = raw[3::4]
    solid = bytes(0 if a < threshold else 1 for a in range(256))
    mask = alpha.translate(solid)
    if background is None:
        return mask, None
    near = _near_tables(background, tolerance)
    planes = [raw[2::4].translate(near[0]), raw[1::4].translate(near[1]), raw[0::4].translate(near[2])]
    # Content differs from the background in some channel and is not see-through.
    same = _bitwise(planes, lambda a, b: a & b, len(alpha))
    differs = same.translate(bytes([1, 0]) + bytes(254))
    return _bitwise([differs, mask], lambda a, b: a & b, len(alpha)), background


def _runs(flags, gap):
    """Occupied index runs; empty stretches shorter than ``gap`` do not split."""
    runs, start, last = [], None, None
    for index, occupied in enumerate(flags):
        if not occupied:
            continue
        if start is None:
            start = index
        elif index - last - 1 >= gap:
            runs.append((start, last + 1))
            start = index
        last = index
    if start is not None:
        runs.append((start, last + 1))
    return runs


def _xy_cut(mask, width, box, gap, cells):
    """Split a box at empty rows, then empty columns, until neither splits."""
    x0, y0, x1, y1 = box
    empty = bytes(x1 - x0)
    rows = _runs([mask[y * width + x0:y * width + x1] != empty for y in range(y0, y1)], gap)
    if len(rows) > 1:
        for top, bottom in rows:
            _xy_cut(mask, width, (x0, y0 + top, x1, y0 + bottom), gap, cells)
        return
    if not rows:
        return
    y0, y1 = y0 + rows[0][0], y0 + rows[0][1]
    occupied = 0
    for y in range(y0, y1):
        occupied |= int.from_bytes(mask[y * width + x0:y * width + x1], "big")
    columns = _runs(occupied.to_bytes(x1 - x0, "big"), gap)
    if len(columns) > 1:
        for left, right in columns:
            _xy_cut(mask, width, (x0 + left, y0, x0 + right, y1), gap, cells)
        return
    cells.append((x0 + columns[0][0], y0, x0 + columns[0][1], y1))
    if len(cells) > MAX_SLICES:
        raise b.OpError("The sheet splits into more than {0} cells; raise min_gap "
                        "or min_pixels".format(MAX_SLICES), kind="resource_limit")


def _tight(mask, width, box):
    """Box shrunk to its content, or None when the box is empty."""
    x0, y0, x1, y1 = box
    cells = []
    _xy_cut(mask, width, box, max(x1 - x0, y1 - y0) + 1, cells)
    return cells[0] if cells else None


def _column_counts(mask, width, top, bottom):
    """Content pixels in each column between two rows."""
    return [mask[top * width + x:bottom * width:width].count(1) for x in range(width)]


def _valley_cuts(counts, left, right, pieces):
    """Spans of [left, right) cut at the emptiest columns near even spacing.

    Counts are summed over a small window, so a wide valley between two
    frames wins over a one-pixel crack inside a sprite.
    """
    span = (right - left) / pieces
    radius = max(1, int(span / 40))
    prefix = [0]
    for count in counts:
        prefix.append(prefix[-1] + count)
    def load(x):
        return prefix[min(len(counts), x + radius + 1)] - prefix[max(0, x - radius)]
    cuts = [left]
    for i in range(1, pieces):
        ideal = left + i * span
        low = max(cuts[-1] + 1, int(ideal - span / 2))
        high = max(low, min(right - 1, int(ideal + span / 2)))
        cuts.append(min(range(low, high + 1), key=lambda x: (load(x), abs(x - ideal))))
    cuts.append(right)
    return list(zip(cuts, cuts[1:]))


_RUN = re.compile(rb"\x01+")
# A component is cut at the frame columns only when two frames each hold at
# least this share of a typical frame's pixels of it, as when a continuous
# ground line joins several figures. Otherwise it stays whole.
_SHARED = 0.35
# Parts smaller than this share of a typical frame count as fragments.
_FRAGMENT = 0.05


def _components(mask, width, top, bottom):
    """8-connected content between two rows, each as a list of (y, x0, x1) runs."""
    rows = [[(m.start() - y * width, m.end() - y * width)
             for m in _RUN.finditer(mask, y * width, (y + 1) * width)] for y in range(top, bottom)]
    first, parent = [], []
    for row in rows:
        first.append(len(parent))
        parent.extend(range(len(parent), len(parent) + len(row)))

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node
    for r in range(1, len(rows)):
        above = rows[r - 1]
        starts = [start for start, _ in above]
        for i, (start, end) in enumerate(rows[r]):
            # Runs above that touch [start, end) including diagonally.
            k = bisect.bisect_right(starts, end) - 1
            while k >= 0 and above[k][1] >= start:
                here, there = find(first[r] + i), find(first[r - 1] + k)
                if here != there:
                    parent[here] = there
                k -= 1
    groups = {}
    for r, row in enumerate(rows):
        for i, (start, end) in enumerate(row):
            groups.setdefault(find(first[r] + i), []).append((top + r, start, end))
    return list(groups.values())


def _bounds(component):
    return (min(r[1] for r in component), min(r[0] for r in component),
            max(r[2] for r in component), max(r[0] for r in component) + 1)


def _overlap(a, b_):
    return max(0, min(a[2], b_[2]) - max(a[0], b_[0])) * max(0, min(a[3], b_[3]) - max(a[1], b_[1]))


def _assign(components, pieces):
    """Runs of each column piece, keeping each component whole where possible.

    A small fragment, such as a chip off a slash, follows the large part
    whose bounds it overlaps most, or the nearer one on a tie, so it lands
    with the same frame.
    """
    starts = [start for start, _ in pieces]
    shares, totals = [], [0] * len(pieces)
    for component in components:
        parts, mass = [[] for _ in pieces], [0] * len(pieces)
        for y, start, end in component:
            k = max(0, bisect.bisect_right(starts, start) - 1)
            while start < end:
                stop = min(end, pieces[k][1]) if k < len(pieces) - 1 else end
                parts[k].append((y, start, stop))
                mass[k] += stop - start
                start, k = stop, k + 1
        shares.append((parts, mass, _bounds(component)))
        totals = [t + m for t, m in zip(totals, mass)]
    typical = sorted(totals)[len(totals) // 2]
    runs = [[] for _ in pieces]
    large = []
    for parts, mass, bounds in shares:
        if sum(mass) < _FRAGMENT * typical:
            continue
        if sum(1 for m in mass if m >= _SHARED * typical) >= 2:
            for k, part in enumerate(parts):
                runs[k].extend(part)
            continue
        home = mass.index(max(mass))
        runs[home].extend(run for part in parts for run in part)
        large.append((bounds, home))
    for parts, mass, bounds in shares:
        if sum(mass) >= _FRAGMENT * typical:
            continue
        # Inside two large parts' bounds, the nearer centre wins.
        middle = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
        best = max(large, default=None, key=lambda item: (
            _overlap(bounds, item[0]), -math.hypot((item[0][0] + item[0][2]) / 2 - middle[0],
                                                   (item[0][1] + item[0][3]) / 2 - middle[1])))
        home = best[1] if best and _overlap(bounds, best[0]) else mass.index(max(mass))
        runs[home].extend(run for part in parts for run in part)
    return runs


def _frame_cut(mask, width, height, gap, columns, cells):
    """Sprite rows cut into ``columns`` frames each, even where effects overlap.

    A row at least half as wide as the widest one, with fewer clean gaps
    than frames, is cut at its emptiest columns. Each connected part then
    goes whole to the frame holding most of it, so a slash or a flash stays
    with the figure that casts it. Other rows, such as labels, are split as
    in auto mode.
    """
    empty = bytes(width)
    bands = []
    for top, bottom in _runs([mask[y * width:(y + 1) * width] != empty
                              for y in range(height)], gap):
        counts = _column_counts(mask, width, top, bottom)
        bands.append((top, bottom, counts, _runs(counts, gap)))
    if not bands:
        return
    widest = max(natural[-1][1] - natural[0][0] for *_, natural in bands)
    row = 0
    for top, bottom, counts, natural in bands:
        left, right = natural[0][0], natural[-1][1]
        if len(natural) >= columns or right - left < max(widest / 2, columns):
            found = []
            _xy_cut(mask, width, (left, top, right, bottom), gap, found)
            cells.extend({"box": box, "pixels": _count(mask, width, box)} for box in found)
            continue
        pieces = _valley_cuts(counts, left, right, columns)
        for column, runs in enumerate(_assign(_components(mask, width, top, bottom), pieces)):
            if runs:
                cells.append({"box": _bounds(runs), "runs": runs, "row": row, "column": column,
                              "pixels": sum(r[2] - r[1] for r in runs)})
        row += 1


def _cell_image(sheet, cell, threshold):
    """The cell's pixels; with runs, only the parts assigned to it.

    Faint edge pixels below ``threshold`` belong to no run, so those next to
    the cell's own runs are kept too.
    """
    x0, y0, x1, y1 = cell["box"]
    width, height = x1 - x0, y1 - y0
    image = sheet.copy(QRect(x0, y0, width, height))
    if "runs" not in cell:
        return image
    keep = bytearray(width * height)
    for y, start, end in cell["runs"]:
        offset = (y - y0) * width + start - x0
        keep[offset:offset + end - start] = b"\x01" * (end - start)
    keep = bytes(keep)
    grown = _bitwise([keep, keep[1:] + b"\0", b"\0" + keep[:-1],
                      keep[width:] + bytes(width), bytes(width) + keep[:-width]],
                     lambda x, y: x | y, len(keep))
    data = bytearray(b.imaging.image_bytes(image))
    faint = bytes(data[3::4]).translate(bytes(1 if 0 < a < threshold else 0 for a in range(256)))
    keep = _bitwise([keep, _bitwise([grown, faint], lambda x, y: x & y, len(keep))],
                    lambda x, y: x | y, len(keep))
    data[3::4] = bytes(map(min, data[3::4], keep.translate(bytes([0]) + bytes([255]) * 255)))
    return QImage(bytes(data), width, height, width * 4, QImage.Format_ARGB32).copy()


def _count(mask, width, box):
    x0, y0, x1, y1 = box
    return sum((x1 - x0) - mask[y * width + x0:y * width + x1].count(0) for y in range(y0, y1))


def _sheet_source(params):
    """(raw ARGB32 bytes, width, height, origin, document, layer name) of the sheet."""
    image = b._image_input(params, "data", "path", required=False)
    if image is not None:
        if params.get("layer") is not None:
            raise b.OpError("Give a layer or an image (data/path), not both")
        image = image.convertToFormat(QImage.Format_ARGB32)
        check_pixels(image.rect())
        doc = b.resolve_document(params["document"]) if params.get("document") is not None else None
        return b.imaging.image_bytes(image), image.width(), image.height(), (0, 0), doc, None
    doc = b.resolve_document(params.get("document"))
    node = b.resolve_node(doc, params.get("layer"))
    if node.type().endswith("mask"):
        raise b.OpError("Choose a layer, not a mask, to slice")
    b.imaging.require_pixel_ops(node)
    doc.waitForDone()
    bounds = node.bounds()
    if bounds.isEmpty():
        raise b.OpError("Layer has no pixels to slice")
    check_pixels(bounds)
    return (_rendered(node, bounds), bounds.width(), bounds.height(),
            (bounds.x(), bounds.y()), doc, node.name())


def slice_sheet(params):
    from ..native_commands import group
    raw, width, height, (ox, oy), doc, source = _sheet_source(params)
    output = str(b._arg(params, "output", "layers"))
    if output not in ("layers", "files"):
        raise b.OpError("output must be layers or files")
    if output == "layers" and doc is None:
        raise b.OpError("Slicing an image file into layers needs a document")
    mode = str(b._arg(params, "mode", "auto"))
    threshold = integer(b._arg(params, "threshold", 16), "threshold", 1, 255)
    tolerance = integer(b._arg(params, "tolerance", 24), "tolerance", 0, 255)
    trim = b._as_bool(b._arg(params, "trim", True), "trim")
    raw_background = b._arg(params, "background", "auto")
    if raw_background == "transparent":
        background = None
    elif raw_background == "auto":
        # A sheet with any transparency is cut on alpha; an opaque one on
        # the colour found in its corners.
        background = None if raw[3::4].count(255) != width * height else _corner_color(raw, width, height)
    else:
        color = b.parse_color(raw_background)
        background = (color.red(), color.green(), color.blue(), color.alpha())
    mask, background = _content_mask(raw, width, height, background, threshold, tolerance)
    min_pixels = integer(b._arg(params, "min_pixels", 16), "min_pixels", 1, 1 << 24)
    cells = []
    if mode == "auto":
        gap = integer(b._arg(params, "min_gap", 2), "min_gap", 1, 4096)
        found = []
        if params.get("columns") is not None:
            _frame_cut(mask, width, height, gap, integer(params["columns"], "columns", 1, 256), found)
        else:
            _xy_cut(mask, width, (0, 0, width, height), gap, found)
            found = [{"box": box, "pixels": _count(mask, width, box)} for box in found]
        cells = [cell for cell in found if cell["pixels"] >= min_pixels]
    elif mode == "grid":
        columns = integer(b._req(params, "columns"), "columns", 1, 256)
        rows = integer(b._req(params, "rows"), "rows", 1, 256)
        margin = integer(b._arg(params, "margin", 0), "margin", 0, 4096)
        spacing = integer(b._arg(params, "spacing", 0), "spacing", 0, 4096)
        cell_w = (width - 2 * margin - (columns - 1) * spacing) // columns
        cell_h = (height - 2 * margin - (rows - 1) * spacing) // rows
        if cell_w < 1 or cell_h < 1:
            raise b.OpError("The grid does not fit the sheet")
        for row in range(rows):
            for column in range(columns):
                x = margin + column * (cell_w + spacing)
                y = margin + row * (cell_h + spacing)
                box = (x, y, x + cell_w, y + cell_h)
                if _count(mask, width, box) < min_pixels:
                    continue
                cells.append({"box": _tight(mask, width, box) if trim else box,
                              "row": row, "column": column})
    else:
        raise b.OpError("mode must be auto or grid")
    if not cells:
        raise b.OpError("No cells with content were found; check background, "
                        "threshold and min_pixels")
    sheet = QImage(raw, width, height, width * 4, QImage.Format_ARGB32).copy()
    for index, cell in enumerate(cells):
        x0, y0, x1, y1 = cell["box"]
        cell.update(index=index, x=x0 + ox, y=y0 + oy, width=x1 - x0, height=y1 - y0)
    result = {"mode": mode, "cells": len(cells), "source": source or "image",
              "background": "transparent" if background is None else
              "#{0:02x}{1:02x}{2:02x}".format(*background[:3])}
    if output == "layers":
        if len(cells) > MAX_SLICE_LAYERS:
            raise b.OpError("More than {0} cells; export them as files instead"
                            .format(MAX_SLICE_LAYERS), kind="resource_limit")
        name = str(b._arg(params, "name", (source or "Sheet") + " slices")).strip() or "Slices"
        siblings = list(doc.rootNode().childNodes())  # bottom first
        with group(doc, "MCP slice sheet"):
            folder = doc.createGroupLayer(name)
            if not doc.rootNode().addChildNode(folder, siblings[-1] if siblings else None):
                raise b.OpError("Could not attach the slices group")
            for cell in cells:
                x0, y0, x1, y1 = cell["box"]
                layer = doc.createNode("{0} {1:03d}".format(name, cell["index"] + 1), "paintlayer")
                b.imaging.write_node_image(layer, QRect(cell["x"], cell["y"], x1 - x0, y1 - y0),
                                           _cell_image(sheet, cell, threshold))
                # Each slice goes to the bottom, so the docker lists them in order.
                if not folder.addChildNode(layer, None):
                    raise b.OpError("Could not attach a slice layer")
        b._refresh(doc)
        result.update(group=b._find_in_tree(b.layer_tree(doc), folder), undoable=True, undo_steps=1,
                      boxes=[{k: c[k] for k in ("index", "x", "y", "width", "height", "row", "column")
                              if k in c} for c in cells])
        return result
    return dict(result, **_write_slices(params, sheet, cells, threshold))


def _write_slices(params, sheet, cells, threshold):
    directory = Path(str(b._req(params, "directory"))).expanduser()
    if not directory.is_absolute():
        raise b.OpError("directory must be an absolute path")
    if directory.exists():
        raise b.OpError("Export directory must be new; existing files are preserved")
    uniform = b._as_bool(b._arg(params, "uniform", False), "uniform")
    align = str(b._arg(params, "align", "center"))
    if align not in ("center", "bottom"):
        raise b.OpError("align must be center or bottom")
    padding = integer(b._arg(params, "padding", 0), "padding", 0, 512)
    prefix = re.sub(r"[^\w.-]+", "_", str(b._arg(params, "prefix", "cell"))).strip("._") or "cell"
    frame_w = max(c["width"] for c in cells) if uniform else None
    frame_h = max(c["height"] for c in cells) if uniform else None
    directory.parent.mkdir(parents=True, exist_ok=True)
    stage = stage_directory(directory.parent, ".krita-slices-")
    entries = []
    try:
        for cell in cells:
            x0, y0, x1, y1 = cell["box"]
            w, h = x1 - x0, y1 - y0
            fw, fh = (frame_w, frame_h) if uniform else (w, h)
            left = (fw - w) // 2
            top = fh - h if align == "bottom" else (fh - h) // 2
            image = QImage(fw + 2 * padding, fh + 2 * padding, QImage.Format_ARGB32)
            image.fill(QColor(0, 0, 0, 0))
            painter = QPainter(image)
            painter.setCompositionMode(QPainter.CompositionMode_Source)
            painter.drawImage(padding + left, padding + top, _cell_image(sheet, cell, threshold))
            painter.end()
            name = "{0}_{1:03d}.png".format(prefix, cell["index"])
            if not image.save(str(stage / name), "PNG"):
                raise b.OpError("Could not save " + name)
            entry = {"file": name, "index": cell["index"], "x": cell["x"], "y": cell["y"],
                     "width": w, "height": h, "image_width": image.width(),
                     "image_height": image.height(), "offset_x": padding + left,
                     "offset_y": padding + top}
            for key in ("row", "column"):
                if key in cell:
                    entry[key] = cell[key]
            entries.append(entry)
        manifest = {"cells": entries, "uniform": uniform, "align": align, "padding": padding,
                    "note": "x, y, width, height locate each cell on the sheet; offset_x, "
                            "offset_y place it inside its PNG."}
        if uniform:
            manifest["frame"] = {"width": frame_w + 2 * padding, "height": frame_h + 2 * padding}
        (stage / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        stage.rename(directory)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"directory": str(directory), "manifest": str(directory / "manifest.json"),
            "files": [e["file"] for e in entries], "mutated": False}


OPERATIONS = [("outline_layer", outline_layer, 120, True),
              ("export_layers", export_layers, 120, False),
              ("slice_sheet", slice_sheet, 180, True)]
