"""Focused workflow operations; registration also works after ops hot reload."""


def register(base):
    from . import painting, selections, transforms, patches, editing, animation, capabilities, warp, dialogs, feedback, correction, assets
    for module in (painting, selections, transforms, patches, editing, animation, capabilities, warp, dialogs, feedback, correction, assets):
        for name, function, timeout, mutates in module.OPERATIONS:
            base.op(name, timeout=timeout, mutates=mutates)(function)
