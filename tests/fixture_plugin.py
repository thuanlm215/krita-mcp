"""Fixed private assertions/faults. Copied ONLY by run_isolated_tests.py.

No source text, expression, module name or callable is accepted by this hook.
It is absent from the installable plugin and from MCP tool discovery.
"""
import hashlib
import os
from pathlib import Path

from . import ops as b, native_commands as native
from .compat import QByteArray, QImage, QtCore, QtWidgets

SAVED = {}
DIALOG = None


@b.op('_test_fixture', timeout=30)
def fixture(p):
    global DIALOG
    home = Path.home()
    if not (home.name == 'home' and home.parent.name.startswith('krita-mcp-test-')
            and os.environ.get('KRITA_MCP_TEST_ARTIFACTS')):
        raise b.OpError('Private fixture environment required', kind='unsupported')
    if set(p) - {'scenario', 'document', 'layer', 'rect', 'projection', 'merged'}:
        raise b.OpError('Unknown fixture argument; code and expressions are forbidden')
    scenario = p.get('scenario')
    if scenario == 'shutdown':
        QtCore.QTimer.singleShot(0, QtWidgets.QApplication.instance().quit)
        return {'quit_requested': True}
    if scenario == 'modal':
        w = QtWidgets.QApplication.activeModalWidget()
        return None if w is None else w.metaObject().className()
    if scenario == 'modal_create':
        DIALOG = QtWidgets.QDialog(b._krita().activeWindow().qwindow())
        DIALOG.setWindowTitle('Recover Files')
        DIALOG.setModal(True)
        DIALOG.show()
        return True
    if scenario == 'modal_visible':
        return DIALOG is not None and DIALOG.isVisible()
    if scenario == 'modal_close':
        if DIALOG is not None:
            DIALOG.reject()
            DIALOG.deleteLater()
            DIALOG = None
        return True
    if scenario in ('fault_status', 'fault_helper', 'fault_visibility', 'fault_commit','fault_frame_commit','fault_export', 'fault_post_publication', 'fault_restore'):
        if scenario == 'fault_restore':
            for (owner, name), value in list(SAVED.items()):
                if owner == 'ops': b.OPS[name] = value
                elif owner == 'bridge': setattr(b, name, value)
                elif owner=='animation':
                    from .operations import animation
                    setattr(animation,name,value)
                else: setattr(native, name, value)
            SAVED.clear()
        elif scenario == 'fault_post_publication':
            # Both native finish functions have consumed/published their stage
            # by the time their first projection refresh runs. Fail once here,
            # rather than pretending a prepublication failure covers this gap.
            original = b._refresh
            SAVED.setdefault(('bridge', '_refresh'), original)
            armed = [False]
            for name in ('_publish_pixels', '_publish_frame'):
                publish = getattr(native, name)
                SAVED.setdefault(('native', name), publish)
                def wrapped(*args, publish=publish):
                    result = publish(*args)
                    armed[0] = True
                    return result
                setattr(native, name, wrapped)
            def refresh(doc):
                if armed[0]:
                    armed[0] = False
                    raise b.OpError('injected recoverable error after native publication', kind='native_failure')
                return original(doc)
            b._refresh = refresh
        elif scenario=='fault_export':
            from .operations import animation
            original=animation._save_export_image
            SAVED.setdefault(('animation','_save_export_image'),original)
            calls=[0]
            def fail_second(image,path):
                calls[0]+=1
                return False if calls[0]==2 else original(image,path)
            animation._save_export_image=fail_second
        elif scenario == 'fault_status':
            SAVED.setdefault(('ops', 'status'), b.OPS['status'])
            b.OPS['status'] = lambda _: object()
        else:
            name = {'fault_helper':'library','fault_visibility':'visibility','fault_commit':'_publish_pixels','fault_frame_commit':'_publish_frame'}[scenario]
            SAVED.setdefault(('native', name), getattr(native, name))
            def failed(*args):
                if name=='_publish_frame':return args[0].mcp_finish_frame(args[1],1,1)
                raise b.OpError({'library':'test missing helper','visibility':'injected visibility failure',
                                '_publish_pixels':'injected failure after provisional pixels'}[name],kind='native_failure')
            setattr(native, name, failed)
        return True
    if scenario == 'filter_roundtrip':
        from .filter_parameters import describe, decode
        checks = []
        for name in ('colortoalpha', 'fastcoloroverlay', 'indexcolors', 'phongbumpmap'):
            flt = b._krita().filter(name)
            cfg = flt.configuration()
            for key, value in cfg.properties().items():
                encoded, info = describe(value)
                if info['writable']:
                    cfg.setProperty(key, decode(encoded, value))
                    checks.append(cfg.property(key) == value)
        return bool(checks) and all(checks)
    if scenario in ('expire_repairs','repair_resources'):
        from .operations.correction import TRANSACTIONS,prune
        if scenario=='expire_repairs':
            for e in TRANSACTIONS.values():e['created']-=601
        prune()
        return {'tokens':len(TRANSACTIONS),'bytes':sum(e['bytes'] for e in TRANSACTIONS.values())}
    doc = b.resolve_document(p.get('document'))
    doc.waitForDone()
    if scenario == 'brush_state':
        from .operations.common import activate
        v = activate(doc)
        return [v.currentBrushPreset().name(), v.brushSize(), v.paintingOpacity(),
                v.eraserMode(), v.foregroundColor().components()]
    if scenario == 'change_brush_size':
        from .operations.common import activate
        v=activate(doc)
        v.setBrushSize(v.brushSize()+1)
        return True
    if scenario == 'history':
        s = doc.selection()
        h = native.history_state(doc)
        active=doc.activeNode()
        return [h['index'], h['count'], doc.modified(), None if active is None else str(active.uniqueId()),
                None if s is None else hashlib.sha256(bytes(s.pixelData(0, 0, 128, 128))).hexdigest()]
    if scenario=='history_labels':
        lib,stack=native.stack(doc)
        doc.waitForDone();lib.mcp_flush_events()
        return [lib.mcp_history_label(stack,i).decode() for i in range(lib.mcp_count(stack))]
    if scenario == 'history_details':
        import ctypes,json
        lib,ptr=native.stack(doc)
        lib.mcp_history_details.argtypes=[ctypes.c_void_p]
        lib.mcp_history_details.restype=ctypes.c_char_p
        return json.loads(lib.mcp_history_details(ptr))
    node = b.resolve_node(doc, p.get('layer', 'Paint'))
    if scenario == 'layer_state':
        return {'animated': node.animated(), 'visible': node.visible()}
    rect = p.get('rect', [0, 0, 128, 128])
    if (not isinstance(rect, list) or len(rect) != 4 or
            any(type(v) is not int for v in rect) or not 1 <= rect[2]*rect[3] <= 1048576
            or rect[2] <= 0 or rect[3] <= 0):
        raise b.OpError('Invalid fixture rectangle')
    if scenario == 'seed_offcanvas':
        node.setPixelData(QByteArray(bytes([11, 22, 33, 44])*4), -4, -4, 2, 2)
        b._refresh(doc)
        return True
    if scenario == 'seed_invisible':
        node.setPixelData(QByteArray(bytes([11,22,33,0])),49,49,1,1)
        b._refresh(doc)
        return True
    if scenario == 'seed_underlying':
        # Fixed occluded-layer mutation bypasses history to exercise composite
        # dependencies independently of the native index and target pixels.
        node.setPixelData(QByteArray(bytes([180,90,20,255])*128*128),0,0,128,128)
        b._refresh(doc)
        return True
    if scenario == 'selection':
        s = doc.selection()
        return None if s is None else bytes(s.pixelData(*rect)).hex()
    if scenario in ('pixels', 'digest'):
        source = doc.rootNode() if p.get('merged') else node
        raw = bytes(source.projectionPixelData(*rect) if p.get('projection') or p.get('merged')
                    else source.pixelData(*rect))
        return hashlib.sha256(raw).hexdigest() if scenario == 'digest' else raw.hex()
    if scenario == 'premul_bake_equal':
        from . import imaging
        def pixels(n, projection):
            raw = bytes(n.projectionPixelData(0, 0, 128, 128) if projection else n.pixelData(0, 0, 128, 128))
            return imaging.image_bytes(QImage(raw, 128, 128, 512, QImage.Format_ARGB32)
                                       .convertToFormat(QImage.Format_ARGB32_Premultiplied))
        return pixels(node, True) == pixels(b.resolve_node(doc, 'Baked'), False)
    raise b.OpError('Unknown fixed fixture scenario')
