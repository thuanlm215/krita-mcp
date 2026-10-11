"""Explicit readback, color statistics and aligned measurements."""
from contextlib import contextmanager
import hashlib
import base64
import struct

from .. import ops as b
from ..compat import QImage,QRect,QPainter,QColor
from .common import number,rect_info,activate
from .animation import integer


def roi(doc, value=None):
    if value is None:return QRect(0,0,doc.width(),doc.height())
    if not isinstance(value,dict) or set(value)!={'x','y','width','height'}:raise b.OpError('ROI requires x,y,width,height')
    r=QRect(*(integer(value[k],k,0 if k in ('x','y') else 1,1048576) for k in ('x','y','width','height')))
    if not QRect(0,0,doc.width(),doc.height()).contains(r):raise b.OpError('ROI must lie inside canvas; no implicit crop')
    if r.width()*r.height()>1048576:raise b.OpError('Feedback budget is 1 megapixel',kind='resource_limit')
    return r


@contextmanager
def clean_frame(doc,time=None):
    from ..native_commands import onion,set_time,get_time
    original_doc=b._krita().activeDocument();original_node=doc.activeNode();current=get_time(doc);modified=doc.modified()
    nodes=[n for _,_,n in b._flatten_tree(doc.rootNode(),'','',[]) if n.type()=='paintlayer' and n.animated() and onion(n)]
    try:
        if time is not None:set_time(doc,time)
        for n in nodes:onion(n,0)
        b._refresh(doc)
        yield
    finally:
        for n in nodes:onion(n,1)
        if time is not None:set_time(doc,current)
        if doc.activeNode()!=original_node:doc.setActiveNode(original_node)
        b._refresh(doc);doc.setModified(modified)
        if original_doc is not None and original_doc in b._documents():activate(original_doc)


def acquire(params):
    doc=b.resolve_document(b._req(params,'document'))
    rect=roi(doc,params.get('region'))
    t=integer(params['time'],'time') if 'time' in params else None
    layer=params.get('layer','merged')
    node=None if layer=='merged' else b.resolve_node(doc,layer)
    if node is not None and node.animated() and t is None:raise b.OpError('Animated readback requires explicit time')
    if node is not None:b.imaging.require_pixel_ops(node)
    if (node.colorProfile() if node is not None else doc.colorProfile())!='sRGB-elle-V2-srgbtrc.icc':
        raise b.OpError('Feedback pilot requires sRGB-elle-V2-srgbtrc.icc',kind='unsupported')
    with clean_frame(doc,t):
        image=(doc.projection(rect.x(),rect.y(),rect.width(),rect.height()).convertToFormat(QImage.Format_ARGB32)
               if node is None else b.imaging.read_node_image(node,rect))
        raw=b.imaging.image_bytes(image)
    return doc,rect,image,raw


def sample_colors(params):
    from ..feedback import statistics
    count=integer(b._arg(params,'palette_size',8),'palette_size',1,16)
    doc,rect,image,raw=acquire(params)
    result=statistics(raw,count)
    result.update(source_rect=rect_info(rect),profile=doc.colorProfile(),channel_order='BGRA/U8 straight alpha',
                  content_hash=hashlib.sha256(raw).hexdigest(),time=params.get('time'),mutated=False)
    return result


def mask(data,w,h):
    if data is None:return None
    image=decode_png(data)
    if image.width()!=w or image.height()!=h:raise b.OpError('Mask must exactly match comparison image')
    return b.imaging.image_bytes(image.convertToFormat(QImage.Format_ARGB32))[3::4]


def decode_png(data):
    if not isinstance(data,str) or len(data)>8*1024*1024:
        raise b.OpError('PNG exceeds 8 MiB encoded budget',kind='resource_limit')
    try:raw=base64.b64decode(data,validate=True)
    except Exception:raise b.OpError('Invalid PNG base64')
    if raw[:8]!=b'\x89PNG\r\n\x1a\n' or raw[12:16]!=b'IHDR' or len(raw)<33:raise b.OpError('PNG header required')
    w,h=struct.unpack('>II',raw[16:24])
    if not w or not h or w*h>1048576:raise b.OpError('Decoded PNG exceeds 1 megapixel budget',kind='resource_limit')
    image=b.imaging.png_b64_to_qimage(data)
    if image.width()!=w or image.height()!=h:raise b.OpError('PNG dimensions disagree with header')
    return image


def compare_images(params):
    from ..feedback import measurements
    a=decode_png(b._req(params,'before')).convertToFormat(QImage.Format_ARGB32)
    z=decode_png(b._req(params,'after')).convertToFormat(QImage.Format_ARGB32)
    if a.size()!=z.size() or a.width()*a.height()>1048576:raise b.OpError('Comparison requires equal images <=1 megapixel')
    w,h=a.width(),a.height();threshold=integer(b._arg(params,'alpha_threshold',128),'alpha_threshold',1,255)
    result,diff,violations=measurements(b.imaging.image_bytes(a),b.imaging.image_bytes(z),w,h,
        mask(params.get('allowed_mask'),w,h),mask(params.get('target_mask'),w,h),threshold)
    offset=params.get('offset',{'x':0,'y':0})
    if not isinstance(offset,dict) or set(offset)!={'x','y'}:raise b.OpError('offset requires x,y')
    offset={k:integer(v,k,-100000,100000) for k,v in offset.items()}
    if result['changed_bounds']:
        result['changed_bounds']['x']+=offset['x'];result['changed_bounds']['y']+=offset['y']
    landmarks=params.get('landmarks',[])
    if not isinstance(landmarks,list) or len(landmarks)>32:raise b.OpError('At most 32 explicit landmarks')
    result['landmark_displacements']=[]
    for landmark in landmarks:
        if not isinstance(landmark,dict) or set(landmark)!={'name','before','after'}:raise b.OpError('landmark requires name,before,after')
        from ..stroke_planner import point,distance,PlanError
        try:p,q=point(landmark['before']),point(landmark['after'])
        except PlanError as exc:raise b.OpError(str(exc))
        result['landmark_displacements'].append({'name':str(landmark['name'])[:128],'dx':q[0]-p[0],'dy':q[1]-p[1],
                                                'distance':distance(p,q),'source':'caller supplied coordinates'})
    max_size=integer(b._arg(params,'max_size',1024),'max_size',64,2048)
    images=[a,z,QImage(diff,w,h,w*4,QImage.Format_ARGB32).copy(),QImage(violations,w,h,w*4,QImage.Format_ARGB32).copy()]
    images=[b.imaging.scale_to_fit(img,max_size//4)[0] for img in images]
    panel=QImage(images[0].width()*4,images[0].height(),QImage.Format_ARGB32);panel.fill(QColor(32,32,32))
    painter=QPainter(panel)
    for i,img in enumerate(images):painter.drawImage(i*img.width(),0,img)
    painter.end()
    result.update(png_base64=b.imaging.qimage_to_png_b64(panel),panels=['before','after','difference','protected violations'],
                  mapping={'offset':offset,'scale':images[0].width()/w,'source_size':[w,h]},mutated=False)
    return result


OPERATIONS=[('sample_colors',sample_colors,60,False),('compare_images',compare_images,60,False)]
