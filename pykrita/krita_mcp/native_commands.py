"""Real KUndo2 commands and macro groups, via the ABI-pinned native helper.

Callbacks only write state while Krita's Undo barrier is held. Rendering is
queued until that barrier is released; synchronous refresh here deadlocks.
"""
import builtins
import ctypes
import hashlib
import json
from pathlib import Path
import sys
import uuid
from contextlib import contextmanager

from . import ops as b
from .compat import QObject, QImage, QtCore, QTimer, QtWidgets

STATE = getattr(builtins, "_krita_mcp_native_state", None)
if STATE is None:
    STATE = {"libraries": {}, "callbacks": {}, "bytes": 0, "errors": []}
    builtins._krita_mcp_native_state = STATE


def pointer(obj):
    from PyQt6 import sip
    return sip.unwrapinstance(obj)


def library():
    if (sys.platform != "linux" or sys.byteorder != "little" or b._krita().version() != "6.0.4"
            or not QtCore.QT_VERSION_STR.startswith("6.")):
        raise b.OpError("Native helper requires Linux/Krita 6.0.4/Qt6/little-endian.", kind="unsupported")
    folder = Path(__file__).resolve().parent
    try:
        manifest = json.loads((folder / "native_manifest.json").read_text())
        if manifest.get("abi") != "krita-6.0.4-qt6":
            raise ValueError("helper manifest ABI does not match")
        name = manifest["file"]
        if Path(name).name != name:
            raise ValueError("invalid helper file name")
        path = folder / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != manifest["sha256"]:
            raise ValueError("helper checksum mismatch")
    except (OSError, ValueError, KeyError) as exc:
        raise b.OpError("Build/install the native helper first: python build_native.py. "
                        + str(exc), kind="unsupported")
    if digest not in STATE["libraries"]:
        try:
            lib = ctypes.CDLL(str(path))
        except OSError as exc:
            raise b.OpError("Native helper could not be loaded: " + str(exc), kind="unsupported")
        lib.mcp_native_abi.restype = ctypes.c_char_p
        if lib.mcp_native_abi() != b"krita-6.0.4-qt6":
            raise b.OpError("Native helper ABI does not match.", kind="unsupported")
        for name, args, result in (
            ('mcp_selection',[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p],ctypes.c_int),
            ('mcp_image_lock',[ctypes.c_void_p,ctypes.c_int],ctypes.c_int),
            ('mcp_flush_events',[],None),
            ('mcp_ui_time',[ctypes.c_void_p],ctypes.c_int),
            ("mcp_stack", [ctypes.c_void_p], ctypes.c_void_p),
            ("mcp_index", [ctypes.c_void_p], ctypes.c_int),
            ("mcp_count", [ctypes.c_void_p], ctypes.c_int),
            ('mcp_history_label',[ctypes.c_void_p,ctypes.c_int],ctypes.c_char_p),
            ("mcp_time", [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int],ctypes.c_int),
            ("mcp_begin", [ctypes.c_void_p, ctypes.c_char_p], None),
            ("mcp_end", [ctypes.c_void_p], None),
            ("mcp_purge_redo", [ctypes.c_void_p], None),
            ("mcp_push", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p], None),
            ('mcp_push_state',[ctypes.c_void_p]*3+[ctypes.c_char_p],ctypes.c_int),
            ("mcp_frames", [ctypes.c_void_p,ctypes.POINTER(ctypes.c_int),ctypes.c_int],ctypes.c_int),
            ("mcp_frame", [ctypes.c_void_p,ctypes.c_void_p]+[ctypes.c_int]*3,ctypes.c_int),
            ("mcp_onion", [ctypes.c_void_p,ctypes.c_int],ctypes.c_int),
            ("mcp_pixels", [ctypes.c_void_p]*2+[ctypes.c_int]*4+[ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p],ctypes.c_int),
            ('mcp_stage_pixels',[ctypes.c_void_p]*2+[ctypes.c_int]*4+[ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.POINTER(ctypes.c_int)],ctypes.c_void_p),
            ('mcp_finish_pixels',[ctypes.c_void_p,ctypes.c_int],None),
            ('mcp_stage_frame',[ctypes.c_void_p]*2+[ctypes.c_int]*6+[ctypes.c_char_p,ctypes.c_int,ctypes.POINTER(ctypes.c_int)],ctypes.c_void_p),
            ('mcp_finish_frame',[ctypes.c_void_p,ctypes.c_int,ctypes.c_int],ctypes.c_int),
            ("mcp_visible", [ctypes.c_void_p]*2+[ctypes.c_int],ctypes.c_int),
            ("mcp_projection_bounds", [ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)],ctypes.c_int),
            ("mcp_stroke", [ctypes.c_void_p]*2 + [ctypes.POINTER(ctypes.c_double)]*3 +
             [ctypes.POINTER(ctypes.c_int)]*2 + [ctypes.POINTER(ctypes.c_double), ctypes.c_int], ctypes.c_int)):
            try:fn = getattr(lib, name)
            except AttributeError:
                raise b.OpError('Native helper symbols do not match this checkout; rebuild the helper',kind='unsupported')
            fn.argtypes = args; fn.restype = result
        STATE["libraries"][digest] = lib
    return STATE["libraries"][digest]


def stack(doc):
    from .operations.common import activate
    activate(doc)
    lib = library()
    ptr = lib.mcp_stack(pointer(b._krita().activeWindow().qwindow()))
    if not ptr:
        raise b.OpError("Krita's document history is not ready.", kind="busy")
    return lib, ptr


def get_time(doc):
    """Logical playback time, independent of background cache rendering."""
    # libkis returns an owned Node wrapper. Retain it across the C call:
    # reducing a temporary to an integer pointer destroys that wrapper first.
    root=doc.rootNode()
    try:value=library().mcp_ui_time(pointer(root))
    except b.OpError:
        if any(n.animated() for _,_,n in b._flatten_tree(doc.rootNode(),'','',[])):raise
        # A static image has no background frame-cache render. Keep ordinary
        # document/image inspection usable without an optional native helper.
        return doc.currentTime()
    if value<0:raise b.OpError('Native timeline unavailable',kind='native_failure')
    return value


def set_time(doc, time):
    """Scheduler-backed timeline navigation, deliberately absent from Undo."""
    # The node identifies its own image. Navigation/readback must not switch
    # views: the timeline dock can reactivate a stale selected keyframe cell.
    lib = library()
    root=doc.rootNode()
    if not lib.mcp_time(pointer(b._krita().activeWindow().qwindow()),
                        pointer(root), int(time)):
        raise b.OpError("Native timeline navigation failed", kind="native_failure")
    # requestTimeSwitchNonGUI queues the GUI scheduler entry. Waiting for the
    # image before delivering that entry observes the previous frame instead.
    QtCore.QCoreApplication.sendPostedEvents(None,0)
    b._refresh(doc)
    lib.mcp_flush_events()
    QtCore.QCoreApplication.sendPostedEvents(None,0)
    doc.waitForDone()
    if get_time(doc)!=int(time):
        raise b.OpError('Native timeline did not reach requested sample',kind='native_failure')


def history_state(doc):
    lib, ptr = stack(doc)
    doc.waitForDone()
    QtCore.QCoreApplication.sendPostedEvents(None, 0)
    return {"index": lib.mcp_index(ptr), "count": lib.mcp_count(ptr)}


def check_errors():
    if STATE["errors"]:
        error = STATE["errors"].pop(0)
        raise b.OpError("Native history callback failed: " + error, kind="native_failure")


class StateCommand(QObject):
    def __init__(self, doc, restore, before, after, size=0):
        super().__init__(QtCore.QCoreApplication.instance())
        self.key = uuid.uuid4().hex
        self.doc, self.restore = doc, restore
        self.before, self.after, self.size = before, after, size
        STATE["callbacks"][self.key] = self
        STATE["bytes"] += size

    def apply(self, value):
        try:
            self.restore(self.doc, value)
            # refreshProjection waits for image jobs; Undo currently owns an
            # image barrier, so defer refresh until the command returns.
            document = self.doc
            def refresh():
                if document in b._documents():
                    document.refreshProjection()
            QTimer.singleShot(0, refresh)
        except BaseException as exc:
            STATE["errors"].append(str(exc))

    @QtCore.pyqtSlot()
    def undo(self):
        self.apply(self.before)

    @QtCore.pyqtSlot()
    def redo(self):
        self.apply(self.after)

    @QtCore.pyqtSlot()
    def dispose(self):
        STATE["callbacks"].pop(self.key, None)
        STATE["bytes"] -= self.size
        self.before = self.after = self.restore = self.doc = None
        self.deleteLater()


def push(doc, restore, before, after, label, size=0):
    lib, ptr = stack(doc)
    if STATE["bytes"] + size > 256*1024*1024:
        raise b.OpError("MCP pixel history reached its 256 MiB budget. "
                        "Clear document history or use smaller edits.", kind="history_full")
    callback = StateCommand(doc, restore, before, after, size)
    root=doc.rootNode()
    if not lib.mcp_push_state(pointer(b._krita().activeWindow().qwindow()),pointer(root),
                              pointer(callback),label.encode('utf-8')):
        callback.dispose()
        raise b.OpError('Native metadata history is unavailable',kind='native_failure')
    check_errors()
    b._refresh(doc)


def write_pixels(doc, node, rect, image, label="MCP draw", keyframe=None):
    """`keyframe` opts an animated layer in: the viewing time must already sit
    on that exact key, so the native transaction lands in its frame data."""
    b.imaging.require_pixel_ops(node)
    if node.animated() and (keyframe is None or keyframe not in frames(node)
                            or get_time(doc) != keyframe):
        raise b.OpError("Pixel snapshot edits on an animated layer need an exact keyframe; "
                        "use draw with `time`, or native draw_brush on the current frame.")
    image = image.convertToFormat(QImage.Format_ARGB32)
    after = b.imaging.image_bytes(image)
    return write_raw(doc,node,rect,after,label)


def set_selection(doc,selection):
    """Keep selection history ordered with scheduled native pixel/layer jobs."""
    lib,_=stack(doc)
    root=doc.rootNode()
    if not lib.mcp_selection(pointer(b._krita().activeWindow().qwindow()),pointer(root),
                             pointer(selection) if selection is not None else None):
        raise b.OpError('Native selection history failed',kind='native_failure')
    b._refresh(doc)
    lib.mcp_flush_events()
    QtCore.QCoreApplication.sendPostedEvents(None,0)
    doc.waitForDone()


def write_raw(doc,node,rect,data,label):
    lib,_=stack(doc)
    result=lib.mcp_pixels(pointer(b._krita().activeWindow().qwindow()),pointer(node),
                          rect.x(),rect.y(),rect.width(),rect.height(),data,len(data),label.encode("utf-8"))
    if result==-2:raise b.OpError("Native pixel history exceeds 256 MiB; use smaller edits or clear history",kind="history_full")
    if result<0:raise b.OpError("Native pixel edit could not start",kind="native_failure")
    b._refresh(doc)
    return result


def _publish_pixels(lib,stage):
    lib.mcp_finish_pixels(stage,1)


def atomic_write_raw(doc,node,rect,data,label,on_published=None):
    """Detached provisional device; publish source memento only at commit."""
    lib,_=stack(doc)
    status=ctypes.c_int(-1)
    stage=lib.mcp_stage_pixels(pointer(b._krita().activeWindow().qwindow()),pointer(node),
        rect.x(),rect.y(),rect.width(),rect.height(),data,len(data),label.encode(),ctypes.byref(status))
    if status.value==-2:raise b.OpError('Native history payload budget exhausted',kind='resource_limit')
    if status.value<0:raise b.OpError('Native repair staging failed',kind='native_failure')
    if stage:
        try:_publish_pixels(lib,stage)
        except BaseException:
            lib.mcp_finish_pixels(stage,0)
            b._refresh(doc)
            raise
    # Native finish has consumed the stage and published its history command.
    # Record that fact before any fallible refresh/receipt work, never abort
    # the consumed stage because later completion work failed.
    if on_published is not None:on_published(status.value)
    b._refresh(doc)
    return status.value


def _publish_frame(lib,stage):
    return lib.mcp_finish_frame(stage,1,0)


@contextmanager
def animation_settings_feedback_guard():
    """Programmatic range sync must not become a new manual UI edit.

    Krita 6.0.4's timeline range notification sets spinbox values without
    blocking valueChanged -> setImageAnimSettings. Keep the displayed values
    synchronized while blocking that feedback, scoped to the timeline dock.
    This guard does not block native diagnostics or image/channel signals.
    """
    blockers=[]
    for window in b._krita().windows():
        for obj in window.qwindow().findChildren(QtWidgets.QDockWidget):
            if obj.metaObject().className()=='KisAnimTimelineDocker':
                blockers.extend(QtCore.QSignalBlocker(spin) for spin in obj.findChildren(QtWidgets.QAbstractSpinBox))
    try:yield
    finally:
        for blocker in blockers:blocker.unblock()


def atomic_write_frame(doc,node,time,policy,rect,data,timing=None,on_published=None):
    with animation_settings_feedback_guard():
        return _atomic_write_frame(doc,node,time,policy,rect,data,timing,on_published)


def _atomic_write_frame(doc,node,time,policy,rect,data,timing,on_published):
    lib,ptr=stack(doc);status=ctypes.c_int(-1)
    stage=lib.mcp_stage_frame(pointer(b._krita().activeWindow().qwindow()),pointer(node),time,
        {'existing_keyframe':0,'one_sample':1,'import_keyframe':2}[policy],rect.x(),rect.y(),rect.width(),rect.height(),data,len(data),ctypes.byref(status))
    if status.value==-2:raise b.OpError('Native frame history budget exhausted',kind='resource_limit')
    if status.value<0:raise b.OpError('Native exact frame staging failed',kind='native_failure')
    if stage:
        # _publish_frame consumes the stage on every native result. Private
        # fault injection calls that same finish with a fixed rollback flag.
        result=_publish_frame(lib,stage)
        if result<0:
            b._refresh(doc)
            raise b.OpError('Native frame commit rolled back before history publication',kind='native_failure')
        # Native finish opened the macro at publication, after all fixed
        # prepublication failures. Include Krita's automatic clip extension.
        try:
            if on_published is not None:on_published(status.value)
            try:
                b._refresh(doc)
            finally:
                # Refresh failure does not revoke published pixels. Still
                # settle clip-extension events and restore timing in this
                # SAME history macro before reporting the completion error.
                doc.waitForDone();lib.mcp_flush_events()
                QtCore.QCoreApplication.sendPostedEvents(None,0);doc.waitForDone()
                if timing is not None:
                    fps,start,end=timing
                    if doc.framesPerSecond()!=fps:doc.setFramesPerSecond(fps)
                    if doc.fullClipRangeStartTime()!=start:doc.setFullClipRangeStartTime(start)
                    if doc.fullClipRangeEndTime()!=end:doc.setFullClipRangeEndTime(end)
                doc.waitForDone();lib.mcp_flush_events()
                QtCore.QCoreApplication.sendPostedEvents(None,0)
        finally:lib.mcp_end(ptr)
    else:
        if on_published is not None:on_published(status.value)
        b._refresh(doc)
    return status.value


def visibility(doc,node,value):
    lib,_=stack(doc)
    result=lib.mcp_visible(pointer(b._krita().activeWindow().qwindow()),pointer(node),int(value))
    if result<0:raise b.OpError("Native visibility edit could not start")
    b._refresh(doc)
    return result


def projection_bounds(node):
    values=(ctypes.c_int*4)()
    if not library().mcp_projection_bounds(pointer(node),values):
        raise b.OpError("Could not read native projection bounds")
    from .compat import QRect
    return QRect(*values)


@contextmanager
def group(doc, label):
    lib, ptr = stack(doc)
    # Flush pending native stroke pushes before opening this history group.
    doc.waitForDone()
    QtCore.QCoreApplication.sendPostedEvents(None,0)
    before_index = lib.mcp_index(ptr)
    STATE["group_depth"]=STATE.get("group_depth",0)+1
    lib.mcp_begin(ptr, label.encode("utf-8"))
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        doc.waitForDone()
        # Stroke history is posted from Krita's image workers to the GUI.
        # Draining those pushes here keeps them inside the open macro.
        QtCore.QCoreApplication.sendPostedEvents(None,0)
        doc.waitForDone()
        QtCore.QCoreApplication.sendPostedEvents(None,0)
        lib.mcp_end(ptr)
        STATE["group_depth"]-=1
        if failed and lib.mcp_index(ptr) > before_index:
            from .operations.common import action
            action(doc, "edit_undo")
            lib.mcp_purge_redo(ptr)
        check_errors()


def stroke(node, points):
    lib = library()
    count = len(points)
    doubles, ints = ctypes.c_double*count, ctypes.c_int*count
    ok = lib.mcp_stroke(pointer(b._krita().activeWindow().qwindow()), pointer(node), doubles(*[p[0] for p in points]),
                        doubles(*[p[1] for p in points]), doubles(*[p[2] for p in points]),
                        ints(*[int(p[3]) for p in points]), ints(*[int(p[4]) for p in points]),
                        doubles(*[p[5] for p in points]), count)
    if not ok:
        raise b.OpError("Native stroke could not start", kind="native_failure")


def frames(node):
    lib=library()
    count=lib.mcp_frames(pointer(node),None,0)
    if count<0 or count>100000:raise b.OpError("Invalid animation frame count")
    values=(ctypes.c_int*count)()
    lib.mcp_frames(pointer(node),values,count)
    return list(values)


def frame(doc,node,time,mode,source=0):
    lib,_=stack(doc)
    if not lib.mcp_frame(pointer(b._krita().activeWindow().qwindow()),pointer(node),time,mode,source):
        raise b.OpError("Krita rejected the frame edit",kind="native_failure")
    b._refresh(doc)


def onion(node,value=-1):
    result=library().mcp_onion(pointer(node),value)
    if result<0:raise b.OpError("Onion skin needs a paint layer")
    return bool(result)
