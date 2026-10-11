"""Krita composites from an owned editable document clone, never the source.

Only projected PNGs leave this module. The clone retains the real layer stack,
blend modes, masks, opacity and raster keys until the candidate is rendered.
"""
import hashlib

from .. import ops as b
from ..compat import QByteArray, QImage, QRect
from ..native_commands import library, pointer, frames
from .feedback import clean_frame

MAX_CONTEXT_BYTES = 64 * 1024 * 1024


def visible_bytes(raw):
    # Krita's projection caches do not guarantee invisible RGB. Alpha and all
    # visible channels do participate in the composite dependency fingerprint.
    result = bytearray(raw)
    for i in range(0, len(result), 4):
        if not result[i+3]: result[i:i+3] = b'\0\0\0'
    return bytes(result)


def projection(doc):
    image = doc.projection(0, 0, doc.width(), doc.height()).convertToFormat(QImage.Format_ARGB32)
    return b.imaging.image_bytes(image)


def context(doc, time):
    """Fingerprint even occluded contributors that a repair could reveal.

    Bound both sampled buffers and the conservative decoded clone payload,
    including all animation keys (the clone must remain an editable document).
    Krita's native tile/cache overhead is separate from these payload limits.
    """
    canvas = QRect(0, 0, doc.width(), doc.height())
    digest = hashlib.sha256()
    sampled = canvas.width()*canvas.height()*4
    clone_cost = sampled
    with clean_frame(doc, time):
        for _, _, node in b._flatten_tree(doc.rootNode(), '', '', []):
            if node.type() == 'selectionmask': continue
            extent = canvas.united(node.bounds())
            pixels = extent.width()*extent.height()
            if pixels > 1048576:
                raise b.OpError('Composite contributor extent exceeds 1 megapixel', kind='resource_limit')
            # Non-U8 devices may retain up to 32 bytes per RGBA pixel.
            pixel_size = {'U8':4, 'U16':8, 'F16':8, 'F32':16, 'F64':32}.get(node.colorDepth(),32)
            sampled += pixels*pixel_size*2
            clone_cost += pixels*pixel_size*max(1,len(frames(node)))
            if max(sampled,clone_cost) > MAX_CONTEXT_BYTES:
                raise b.OpError('Editable composite preview exceeds 64 MiB context budget', kind='resource_limit')
            digest.update(b._node_uuid(node).encode())
            digest.update(str([extent.x(),extent.y(),extent.width(),extent.height()]).encode())
            # Raw bytes protect hidden contributors, projections protect masks,
            # generators, groups and other rendered dependencies at this time.
            digest.update(bytes(node.pixelData(extent.x(),extent.y(),extent.width(),extent.height())))
            digest.update(bytes(node.projectionPixelData(extent.x(),extent.y(),extent.width(),extent.height())))
        composite = projection(doc)
        digest.update(visible_bytes(composite))
    return digest.hexdigest(), composite


def candidate_composite(doc, node, time, rect, pixels, baseline):
    """Clone while the source image is read-locked, then modify ONLY the clone.

    No view is added: the caller's active document/node, brush and timeline UI
    stay bound to the source. Strong wrappers live through every native call.
    """
    lib = library()
    root = doc.rootNode()
    clone = None
    if not lib.mcp_image_lock(pointer(root), 1):
        raise b.OpError('Cannot lock source for editable preview clone', kind='native_failure')
    try: clone = doc.clone()
    finally: lib.mcp_image_lock(pointer(root), 0)
    if clone is None:
        raise b.OpError('Krita could not clone editable preview', kind='native_failure')
    try:
        clone.setBatchmode(True)
        clone.setAutosave(False)
        work = clone.nodeByUniqueID(node.uniqueId())
        if work is None:
            raise b.OpError('Clone lost target layer identity', kind='native_failure')
        with clean_frame(clone,time):
            before = projection(clone)
            if visible_bytes(before) != visible_bytes(baseline):
                raise b.OpError('Editable clone composite disagrees with baseline', kind='native_failure')
            if not work.setPixelData(QByteArray(pixels),rect.x(),rect.y(),rect.width(),rect.height()):
                raise b.OpError('Cannot write frozen candidate to owned clone', kind='native_failure')
            b._refresh(clone)
            return projection(clone)
    finally:
        clone.waitForDone()
        clone.setModified(False)
        clone.close()
