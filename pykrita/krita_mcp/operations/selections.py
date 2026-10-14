"""Polygon/lasso masks and native copy/cut to a new layer."""

from collections import deque

from .. import ops as b
from ..compat import QByteArray, QColor, QImage, QPainter, QPainterPath, QPointF, QRect
from .common import activate, action, check_pixels, number, paint_layer
from ..native_commands import set_selection


def polygon(params):
    from krita import Selection
    doc = b.resolve_document(params.get("document"))
    raw = b._req(params, "points")
    if not isinstance(raw, list) or not 3 <= len(raw) <= 2048:
        raise b.OpError("polygon needs 3 to 2048 [x,y] points")
    points = []
    for point in raw:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise b.OpError("polygon points must be [x,y] pairs")
        points.append(QPointF(number(point[0], "x", -doc.width(), 2*doc.width()),
                             number(point[1], "y", -doc.height(), 2*doc.height())))
    path = QPainterPath(points[0])
    for point in points[1:]:
        path.lineTo(point)
    path.closeSubpath()
    region = path.boundingRect().toAlignedRect().intersected(
        QRect(0, 0, doc.width(), doc.height()))
    if region.isEmpty():
        raise b.OpError("Polygon falls outside the canvas")
    check_pixels(region)
    value = int(number(b._arg(params, "value", 255), "value", 0, 255))
    feather = int(number(b._arg(params, "feather", 0), "feather", 0, 256))
    combine = str(b._arg(params, "combine", "replace"))
    if combine not in ("replace", "add", "subtract", "intersect"):
        raise b.OpError("combine must be replace, add, subtract or intersect")
    image = QImage(region.width(), region.height(), QImage.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing,
                          b._as_bool(b._arg(params, "antialias", True), "antialias"))
    painter.translate(-region.x(), -region.y())
    painter.fillPath(path, QColor(255, 255, 255, value))
    painter.end()
    data = b.imaging.image_bytes(image)
    selection = Selection()
    selection.setPixelData(QByteArray(data[3::4]), region.x(), region.y(),
                           region.width(), region.height())
    if feather:
        selection.feather(feather)
    current = doc.selection()
    if combine != "replace" and current is not None:
        current = current.duplicate()
        getattr(current, combine)(selection)
        selection = current
    elif combine in ("subtract", "intersect"):
        raise b.OpError("This combine mode requires an existing selection")
    activate(doc)
    set_selection(doc,selection)
    b._refresh(doc)
    return {"selection": {"x": selection.x(), "y": selection.y(),
                          "width": selection.width(), "height": selection.height()},
            "combine": combine, "undoable": True, "undo_steps": 1,
            "note": "Setting the selection creates one native Undo entry."}


def selection_to_layer(params):
    doc = b.resolve_document(params.get("document"))
    node = paint_layer(doc, params.get("layer"))
    selection = doc.selection()
    if selection is None or selection.width() <= 0 or selection.height() <= 0:
        raise b.OpError("Select a non-empty region before extracting a part")
    b.imaging.require_pixel_ops(node)
    bounds = QRect(selection.x(), selection.y(), selection.width(),
                   selection.height()).intersected(node.bounds())
    if bounds.isEmpty():
        raise b.OpError("Selection does not overlap this layer")
    check_pixels(bounds)
    # Reject a selection whose bounding box overlaps but whose mask contains
    # no nontransparent source pixels: native actions otherwise silently fail.
    pixels = b.imaging.image_bytes(b.imaging.read_node_image(node, bounds))
    mask = bytes(selection.pixelData(bounds.x(), bounds.y(),
                                     bounds.width(), bounds.height()))
    if not any(a and m for a, m in zip(pixels[3::4], mask)):
        raise b.OpError("Selection contains no painted pixels on this layer")
    mode = str(b._arg(params, "mode", "copy"))
    if mode not in ("copy", "cut"):
        raise b.OpError("mode must be copy or cut")
    clear = b._as_bool(b._arg(params, "clear_selection", True), "clear_selection")
    name = str(b._arg(params, "name", node.name() + " part")).strip()
    if not name:
        raise b.OpError("name cannot be empty")
    activate(doc, node)
    before = {b._node_uuid(n) for _, _, n in b._flatten_tree(doc.rootNode(), "", "", [])}
    action(doc, mode + "_selection_to_new_layer")
    created = [n for _, _, n in b._flatten_tree(doc.rootNode(), "", "", [])
               if n.type() == "paintlayer" and b._node_uuid(n) not in before]
    if len(created) != 1:
        raise b.OpError("Krita did not create the expected part layer; inspect "
                        "the document before retrying.", kind="native_failure")
    part = created[0]
    part.setName(name)
    doc.setActiveNode(part)
    if clear:
        # Null actually deselects; an empty Selection would block subsequent
        # paint. setSelection adds one native history entry, included below.
        set_selection(doc,None)
    b._refresh(doc)
    return {"source_layer": node.name(), "mode": mode,
            "layer": b._find_in_tree(b.layer_tree(doc), part),
            "undoable": True, "undo_steps": (1 if mode == "copy" else 2) + int(clear),
            "note": "Copy adds one native entry; cut adds two. Clearing selection "
                    "adds one more; undo_steps includes it."}


def install_selection(doc, selection, params):
    combine = str(b._arg(params, "combine", "replace"))
    if combine not in ("replace", "add", "subtract", "intersect"):
        raise b.OpError("Unknown selection combine mode")
    feather = b._as_int(b._arg(params, "feather", 0), "feather")
    if not 0 <= feather <= 256:
        raise b.OpError("feather must be 0 to 256")
    if feather:
        selection.feather(feather)
    current = doc.selection()
    if combine != "replace":
        if current is None and combine != "add":
            raise b.OpError("This combine mode needs an existing selection")
        if current is not None:
            merged = current.duplicate()
            getattr(merged, combine)(selection)
            selection = merged
    activate(doc)
    set_selection(doc,selection)
    b._refresh(doc)
    return {"selection": {"x": selection.x(), "y": selection.y(),
                          "width": selection.width(), "height": selection.height()},
            "combine": combine, "undoable": True, "undo_steps": 1}


def mask_selection(data, width, height, x=0, y=0):
    from krita import Selection
    selection = Selection()
    selection.setPixelData(QByteArray(bytes(data)), x, y, width, height)
    return selection


def select_content(params):
    """Alpha/color range/flood selection or import an explicit raster mask."""
    doc = b.resolve_document(params.get("document"))
    mode = str(b._arg(params, "mode", "alpha"))
    canvas = QRect(0, 0, doc.width(), doc.height())
    check_pixels(canvas)
    if mode == "mask":
        image = b._image_input(params, "data", "data_path")
        check_pixels(image.rect())
        channel = str(b._arg(params, "channel", "alpha"))
        if channel not in ("alpha", "luminance"):
            raise b.OpError("channel must be alpha or luminance")
        x = b._as_int(b._arg(params, "x", 0), "x")
        y = b._as_int(b._arg(params, "y", 0), "y")
        mask = bytearray(image.width()*image.height())
        raw = b.imaging.image_bytes(image)
        for i in range(len(mask)):
            bb, gg, rr, aa = raw[i*4:i*4+4]
            mask[i] = aa if channel == "alpha" else (rr*299+gg*587+bb*114+500)//1000
        selection = mask_selection(mask,image.width(),image.height(),x,y)
    else:
        if mode not in ("alpha", "color", "contiguous"):
            raise b.OpError("mode must be alpha, color, contiguous or mask")
        doc.waitForDone()
        layer = params.get("layer")
        image = (doc.projection(0,0,doc.width(),doc.height()).convertToFormat(QImage.Format_ARGB32)
                 if layer is None else b.imaging.read_node_image(b.resolve_node(doc,layer),canvas))
        raw = b.imaging.image_bytes(image)
        mask = bytearray(doc.width()*doc.height())
        threshold = int(number(b._arg(params,"threshold",1),"threshold",0,255))
        tolerance = int(number(b._arg(params,"tolerance",0),"tolerance",0,255))
        if mode == "alpha":
            for i, alpha in enumerate(raw[3::4]):
                mask[i] = alpha if alpha >= threshold else 0
        else:
            if mode == "color":
                c = b.parse_color(b._req(params,"color"))
                target = bytes((c.blue(),c.green(),c.red(),c.alpha()))
            else:
                x = b._as_int(b._req(params,"x"),"x")
                y = b._as_int(b._req(params,"y"),"y")
                if not canvas.contains(x,y):
                    raise b.OpError("Seed lies outside the canvas")
                seed = y*doc.width()+x
                target = raw[seed*4:seed*4+4]
            def matches(i):
                return all(abs(raw[i*4+j]-target[j])<=tolerance for j in range(4))
            if mode == "color":
                for i in range(len(mask)):
                    if matches(i): mask[i] = 255
            else:
                visited = bytearray(len(mask)); visited[seed]=1
                queue = deque([seed]); width=doc.width()
                while queue:
                    i=queue.popleft()
                    if not matches(i): continue
                    mask[i]=255
                    neighbors=[]
                    if i%width: neighbors.append(i-1)
                    if i%width<width-1: neighbors.append(i+1)
                    if i>=width: neighbors.append(i-width)
                    if i+width<len(mask): neighbors.append(i+width)
                    for n in neighbors:
                        if not visited[n]: visited[n]=1; queue.append(n)
        selection = mask_selection(mask,doc.width(),doc.height())
    result = install_selection(doc,selection,params)
    result["mode"] = mode
    return result


OPERATIONS = [("select_polygon", polygon, 30, True),
              ("selection_to_layer", selection_to_layer, 60, True),
              ("select_content", select_content, 120, True)]
