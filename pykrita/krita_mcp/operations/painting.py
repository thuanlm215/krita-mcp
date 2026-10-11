"""Native Krita brush strokes, explicit native history, preset discovery."""

from .. import ops as b
from ..native_commands import get_time
import hashlib
import uuid
from ..compat import QPointF, QPainterPath
from .common import activate, action, number, paint_layer


def list_brushes(params):
    from ..native_commands import library
    query = str(b._arg(params, "query", "")).lower()
    presets = b._krita().resources("preset")
    names = sorted(name for name in presets if query in name.lower())
    from krita import Node
    python_api = hasattr(Node, "paintLine")
    try:
        library()
        available, reason = python_api, None if python_api else "Python paintLine API absent"
    except Exception as exc:
        available, reason = False, str(exc)
    return {"presets": names, "count": len(names),
            "python_paintLine_available": python_api, "native_stroke_available": available,
            "unsupported_reason": reason,
            "note": "native_stroke_available checks the default pressure helper/ABI, "
                    "not target layer editability. Use list_capabilities kind workflow for target guards."}


def draw_brush(params):
    from krita import ManagedColor
    doc = b.resolve_document(params.get("document"))
    node = paint_layer(doc, params.get("layer"), allow_animated=True)
    if not hasattr(node, "paintLine"):
        raise b.OpError("This Krita build lacks native painting APIs.",
                        kind="unsupported")
    raw = b._req(params, "points")
    if not isinstance(raw, list) or not 2 <= len(raw) <= 128:
        raise b.OpError("points must contain 2 to 128 {x,y,pressure} objects")
    points = []
    for point in raw:
        if not isinstance(point, dict):
            raise b.OpError("Each point must be an {x,y,pressure} object")
        points.append((number(b._req(point, "x"), "x", 0, doc.width()-1),
                       number(b._req(point, "y"), "y", 0, doc.height()-1),
                       number(b._arg(point, "pressure", 1), "pressure", 0, 1),
                       number(b._arg(point, "tilt_x", 0), "tilt_x", -60, 60),
                       number(b._arg(point, "tilt_y", 0), "tilt_y", -60, 60),
                       number(b._arg(point, "timestamp_ms", len(points)*16), "timestamp_ms", 0, 1e8)))
    if any(end[5] <= start[5] for start,end in zip(points,points[1:])):
        raise b.OpError("Stroke timestamps must increase")
    mode = str(b._arg(params, "mode", "pressure"))
    if mode not in ("pressure", "continuous", "segments", "path"):
        raise b.OpError("mode must be continuous, pressure, segments or path")
    if mode == "path" and any(p[2] != 1 for p in points):
        raise b.OpError("Native paintPath cannot accept pressure; use mode pressure")
    if mode == "path" and not hasattr(node, "paintPath"):
        raise b.OpError("This Krita build lacks paintPath.", kind="unsupported")
    size = number(b._arg(params, "size", 20), "size", 0.1, 2048)
    opacity = number(b._arg(params, "opacity", 1), "opacity", 0, 1)
    erase = b._as_bool(b._arg(params, "erase", False), "erase")
    color = b.parse_color(b._arg(params, "color", "#000000"))
    preset_name = params.get("preset")
    preset = None
    if preset_name is not None:
        preset = b._krita().resources("preset").get(preset_name)
        if preset is None:
            raise b.OpError("Unknown preset; use list_brushes for exact names.",
                            kind="not_found")
    view = activate(doc, node)
    state = (view.currentBrushPreset(), view.brushSize(),
             view.paintingOpacity(), view.foregroundColor(), view.eraserMode())
    completed = 0
    try:
        if preset is not None:
            view.setCurrentBrushPreset(preset)
        view.setBrushSize(size)
        view.setPaintingOpacity(opacity)
        view.setForeGroundColor(ManagedColor.fromQColor(color, view.canvas()))
        view.setEraserMode(erase)
        if node.paintAbility() != "PAINT":
            raise b.OpError("Krita reports this layer cannot be painted.")
        if mode == "path":
            path = QPainterPath(QPointF(points[0][0], points[0][1]))
            for x, y, *_ in points[1:]:
                path.lineTo(x, y)
            node.paintPath(path)
            doc.waitForDone()
            completed = 1
        elif mode in ("pressure", "continuous"):
            from ..native_commands import stroke
            stroke(node, points)
            doc.waitForDone()
            completed = 1
        else:
            for start, end in zip(points, points[1:]):
                # Krita 6.0's Python binding uses QPoint; newer bindings use
                # QPointF. toPoint() supplies the compatible integral form.
                node.paintLine(QPointF(start[0], start[1]).toPoint(),
                               QPointF(end[0], end[1]).toPoint(),
                               start[2], end[2])
                doc.waitForDone()
                completed += 1
        used_preset = view.currentBrushPreset().name()
    except Exception:
        # Every completed native segment has its own history entry. Revert
        # only those entries if a later segment fails.
        for _ in range(completed):
            action(doc, "edit_undo")
        raise
    finally:
        view.setCurrentBrushPreset(state[0])
        view.setBrushSize(state[1])
        view.setPaintingOpacity(state[2])
        view.setForeGroundColor(state[3])
        view.setEraserMode(state[4])
    b._refresh(doc)
    return {"layer": node.name(), "native": True, "mode": mode,
            "preset": used_preset, "undoable": True, "undo_steps": completed,
            "selection_respected": True,
            "note": ("Pressure/continuous mode uses one persistent native painting "
                     "stroke with pressure, tilt and time samples. Segments mode "
                     "keeps the legacy independent line calls. Path mode uses pressure 1.")}


def history(params, redo=False):
    doc = b.resolve_document(params.get("document"))
    count = number(b._arg(params, "count", 1), "count", 1, 128)
    if not count.is_integer():
        raise b.OpError("count must be an integer")
    activate(doc)
    name = "edit_redo" if redo else "edit_undo"
    from ..native_commands import history_state
    before = history_state(doc)
    completed = 0
    for _ in range(int(count)):
        # Canvas focus can make QAction enablement stale. Use the established
        # native action helper; an empty native history is a harmless no-op.
        action(doc, name)
        completed += 1
    b._refresh(doc)
    from ..native_commands import check_errors
    check_errors()
    after = history_state(doc)
    return {"document": doc.name(), "requested": int(count),
            "actions_triggered": completed, "action": name,
            "history_before": before, "history_after": after,
            "entries_changed": abs(after["index"] - before["index"]),
            "note": "Uses Krita's native history, including manual edits. "
                    "actions_triggered counts action calls; empty history is a no-op."}


def undo(params):
    return history(params)


def redo(params):
    return history(params, redo=True)


def plan_stroke(params):
    from ..stroke_planner import plan, PlanError
    from ..targeting import resolve
    spec = b._req(params,'spec')
    try:
        planned = plan(spec)
    except PlanError as exc:
        raise b.OpError(str(exc))
    doc,node = resolve(spec['target'],allow_frame=True)
    if not b._krita().resources('preset').get(spec['preset']):
        raise b.OpError('Exact preset resource not found',kind='not_found')
    for p in planned['points']:
        if not 0 <= p['x'] < doc.width() or not 0 <= p['y'] < doc.height():
            raise b.OpError('Planned point lies outside canvas')
    planned.update(target=spec['target'],preset=spec['preset'],preset_hash=None,
                   preset_hash_reason='Resource binding does not expose embedded preset bytes',
                   profile=node.colorProfile(),mutated=False,expected_bounds=spec['expected_bounds'])
    return planned


def paint_stroke(params):
    from ..targeting import resolve
    from ..native_commands import set_time,frames,group,write_raw
    from ..compat import QRect
    planned = plan_stroke(params)
    spec = params['spec']
    doc,node = resolve(spec['target'],allow_frame=True)
    scope = spec['target']['scope']
    if scope['kind'] == 'frame':
        if scope['exposure_policy'] != 'existing_keyframe' or scope['time'] not in frames(node):
            raise b.OpError('Direct planned painting requires an exact existing keyframe; use repair for one_sample',kind='unsupported')
    # All spec/target/preset checks precede timeline/view/pixel mutations.
    current = get_time(doc)
    if scope['kind'] == 'frame': set_time(doc,scope['time'])
    before = bytes(node.pixelData(0,0,doc.width(),doc.height()))
    selection = doc.selection()
    mask = bytes(selection.pixelData(0,0,doc.width(),doc.height())) if selection else None
    try:
        with group(doc,'MCP planned stroke'):
            result = draw_brush({'document':spec['target']['document'],'layer':spec['target']['layer'],
                                'preset':spec['preset'],'color':spec['color']['rgba'],'size':spec['size'],
                                'opacity':spec['opacity'],'erase':spec['erase'],'mode':'continuous',
                                'points':planned['points']})
            after = bytes(node.pixelData(0,0,doc.width(),doc.height()))
            # Native presets can leave foreground RGB beneath zero alpha even
            # where a hard selection forbids visible paint. Restore those
            # protected bytes inside the same native history macro.
            if mask is not None:
                protected = bytearray(after)
                for i,weight in enumerate(mask):
                    if not weight:protected[i*4:i*4+4]=before[i*4:i*4+4]
                if bytes(protected) != after:
                    write_raw(doc,node,QRect(0,0,doc.width(),doc.height()),bytes(protected),'MCP protected selection bytes')
                after=bytes(protected)
    finally:
        if scope['kind'] == 'frame': set_time(doc,current)
    changed = [i//4 for i in range(0,len(before),4) if before[i:i+4] != after[i:i+4]]
    xs = [i%doc.width() for i in changed];ys = [i//doc.width() for i in changed]
    result.update(operation_id=uuid.uuid4().hex,status='applied',target=spec['target'],
                  stroke_id=spec['stroke_id'],planned_points=planned['point_count'],
                  expected_bounds=spec['expected_bounds'],changed_pixels=len(changed),
                  actual_bounds=None if not changed else {'x':min(xs),'y':min(ys),
                    'width':max(xs)-min(xs)+1,'height':max(ys)-min(ys)+1},
                  input_hash=hashlib.sha256(before).hexdigest(),result_hash=hashlib.sha256(after).hexdigest(),
                  pixel_exact_replay=False,profile=node.colorProfile(),runtime=b._krita().version())
    return result


OPERATIONS = [("list_brushes", list_brushes, 15, False),
              ("draw_brush", draw_brush, 120, True),
              ('plan_stroke', plan_stroke, 30, False), ('paint_stroke',paint_stroke,120,True),
              ("undo", undo, 60, True), ("redo", redo, 60, True)]
