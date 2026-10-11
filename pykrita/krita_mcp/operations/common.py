"""Validation and native action helpers shared by artist workflow tools."""

import math

from .. import ops as b

MAX_PIXELS = 16 * 1024 * 1024


def number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise b.OpError(name + " must be a finite number")
    result = b._as_float(value, name)
    if not math.isfinite(result):
        raise b.OpError(name + " must be a finite number")
    if minimum is not None and result < minimum:
        raise b.OpError("{0} must be >= {1}".format(name, minimum))
    if maximum is not None and result > maximum:
        raise b.OpError("{0} must be <= {1}".format(name, maximum))
    return result


def check_pixels(rect):
    if rect.width() * rect.height() > MAX_PIXELS:
        raise b.OpError("Operation exceeds the 16 megapixel buffer limit; "
                        "use a smaller region or document.")


def paint_layer(doc, ref, allow_masks=False, allow_animated=False):
    node = b.resolve_node(doc, ref)
    if node.type() != "paintlayer" or node.locked():
        raise b.OpError("Choose an unlocked paint layer.")
    if node.animated() and not allow_animated:
        raise b.OpError("This workflow currently supports static paint layers.")
    if not allow_masks and node.childNodes():
        raise b.OpError("Choose a paint layer without masks; create a separate "
                        "Cleanup layer above the part at the document root, or use "
                        "bake_layer to edit a rendered copy while preserving its source.",
                        kind="unsupported_layer")
    parent = node.parentNode()
    while parent is not None:
        if any(child.type() == "transformmask" for child in parent.childNodes()):
            raise b.OpError("A parent group has a transform mask. Create Cleanup at the "
                            "document root, or bake the transformed ancestor and edit "
                            "its copy; raw document coordinates would be incorrect here.",
                            kind="unsupported_layer")
        if parent.locked():
            raise b.OpError("A parent group is locked; choose an editable layer.")
        parent = parent.parentNode()
    return node


def activate(doc, node=None):
    """Bind all UI-backed actions and brush resources to the requested view."""
    app = b._krita()
    from ..compat import QtCore
    # Keyframe-channel/model notifications belong to the current view. Drain
    # posted notifications before switching: Krita's curves model otherwise
    # receives the old document's channel after it has reset to the new one.
    QtCore.QCoreApplication.sendPostedEvents(None,0)
    from ..native_commands import library
    try:library().mcp_flush_events()
    except b.OpError:pass  # Basic document inspection also works without helper.
    for window in app.windows():
        for view in window.views():
            # libkis equality compares the actual document, unlike names or
            # persisted layer UUIDs which can match two reopened copies.
            if view.document() == doc:
                if app.activeDocument()!=doc or app.activeWindow()!=window:
                    window.showView(view)
                    app.setActiveDocument(doc)
                    # Settle this view's queued model notifications before
                    # subsequent UI-backed operations.
                    QtCore.QCoreApplication.sendPostedEvents(None,0)
                    doc.waitForDone()
                if node is not None and doc.activeNode()!=node:
                    doc.setActiveNode(node)
                doc.waitForDone()
                return view
    raise b.OpError("The requested document needs an open Krita view.",
                    kind="no_view")


def action(doc, name):
    activate(doc)
    result = b.op_trigger_action({"name": name})
    doc.waitForDone()
    b._refresh(doc)
    # Worker edits publish Undo commands through queued GUI signals. A later
    # synchronous libkis command must not overtake those history pushes.
    from ..native_commands import library
    from ..compat import QtCore
    library().mcp_flush_events()
    QtCore.QCoreApplication.sendPostedEvents(None, 0)
    doc.waitForDone()
    return result


def rect_info(rect):
    return {"x": rect.x(), "y": rect.y(),
            "width": rect.width(), "height": rect.height()}
