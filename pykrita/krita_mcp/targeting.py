"""Explicit, bounded pilot targets shared by painting, feedback and repairs."""
from . import ops as b
from .operations.common import paint_layer
from .operations.animation import integer

PROFILE = 'sRGB-elle-V2-srgbtrc.icc'
MAX_WORK_PIXELS = 1024*1024


def resolve(target, allow_frame=False):
    if not isinstance(target,dict) or set(target) != {'document','layer','scope'}:
        raise b.OpError('Explicit target requires document, layer and scope')
    if not isinstance(target['document'],str) or not target['document'].startswith('session:'):
        raise b.OpError('Use document session_id from inspect_document/status')
    if not isinstance(target['layer'],str) or not target['layer'].startswith('uuid:'):
        raise b.OpError('Use explicit uuid layer from inspect_document')
    doc = b.resolve_document(target['document'])
    node = paint_layer(doc,target['layer'],allow_animated=allow_frame)
    if node.alphaLocked():raise b.OpError('Pilot requires alpha-unlocked layers',kind='unsupported')
    if node.colorModel() != 'RGBA' or node.colorDepth() != 'U8' or node.colorProfile() != PROFILE:
        raise b.OpError('Pilot requires unmasked RGBA/U8 '+PROFILE,kind='unsupported')
    if doc.width()*doc.height() > MAX_WORK_PIXELS:
        raise b.OpError('Pilot canvas budget is 1 megapixel',kind='resource_limit')
    scope = target['scope']
    if scope == {'kind':'static'}:
        if node.animated(): raise b.OpError('Animated target needs explicit frame scope',kind='unsupported')
    elif isinstance(scope,dict) and set(scope) == {'kind','time','exposure_policy'} and scope['kind'] == 'frame' and allow_frame:
        from .native_commands import frames
        t = integer(scope['time'],'time',0,99999)
        if scope['exposure_policy'] not in ('existing_keyframe','one_sample'):
            raise b.OpError('Unknown exposure policy')
        if scope['exposure_policy']=='one_sample' and t==99999:
            raise b.OpError('one_sample restore key would exceed supported time range',kind='resource_limit')
        if not node.animated() or not frames(node) or t < min(frames(node)):
            raise b.OpError('No raster exposure at requested time',kind='not_found')
        if not doc.fullClipRangeStartTime() <= t <= doc.fullClipRangeEndTime():
            raise b.OpError('Requested time outside clip')
    else:
        raise b.OpError('Scope must be static or explicit supported frame')
    return doc,node
