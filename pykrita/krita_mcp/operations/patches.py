"""Undoable texture placement and sampled clone strokes on RGBA8 layers."""
import math

from .. import ops as b
from ..compat import QColor, QImage, QPainter, QPointF, QRect, QtCompat as Qt
from .common import check_pixels, number, paint_layer, rect_info


def region(value, default, canvas=None):
    if value is None:
        rect = default
    elif isinstance(value, dict):
        rect = QRect(b._as_int(b._arg(value,"x",0),"x"),b._as_int(b._arg(value,"y",0),"y"),
                     b._as_int(b._req(value,"width"),"width"),b._as_int(b._req(value,"height"),"height"))
    else:
        raise b.OpError("region must be an object")
    if rect.width()<=0 or rect.height()<=0:
        raise b.OpError("region dimensions must be positive")
    check_pixels(rect)
    if canvas is not None and not canvas.contains(rect):
        raise b.OpError("region must fit inside the canvas")
    return rect


def finish(doc,node,rect,before,after,label):
    from ..native_commands import write_pixels
    after=b.imaging.constrain_to_selection(before,after,doc.selection(),rect)
    steps=write_pixels(doc,node,rect,after,label)
    return {"layer":node.name(),"region":rect_info(rect),"selection_respected":True,
            "undoable":True,"undo_steps":steps}


def texture_patch(params):
    doc=b.resolve_document(params.get("document"))
    node=paint_layer(doc,params.get("layer"))
    canvas=QRect(0,0,doc.width(),doc.height())
    data=params.get("data")
    if (data is None)==(params.get("source_layer") is None):
        raise b.OpError("Provide either data or source_layer")
    if data is not None:
        source=b.imaging.png_b64_to_qimage(data)
        check_pixels(source.rect())
        src=region(params.get("source_region"),source.rect(),source.rect())
        source=source.copy(src)
    else:
        source_node=b.resolve_node(doc,params["source_layer"])
        src=region(params.get("source_region"),source_node.bounds(),canvas)
        source=b.imaging.read_node_image(source_node,src)
    dest=region(b._req(params,"region"),canvas,canvas)
    opacity=number(b._arg(params,"opacity",1),"opacity",0,1)
    source=source.scaled(dest.width(),dest.height(),Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
    if params.get("mask_data") is not None:
        mask=b.imaging.png_b64_to_qimage(params["mask_data"])
        if mask.size()!=source.size():
            raise b.OpError("mask_data dimensions must match the destination region")
        channel=str(b._arg(params,"mask_channel","alpha"))
        if channel not in ("alpha","luminance"):
            raise b.OpError("mask_channel must be alpha or luminance")
        raw=bytearray(b.imaging.image_bytes(source))
        mask_raw=b.imaging.image_bytes(mask)
        for i in range(dest.width()*dest.height()):
            off=i*4
            weight=(mask_raw[off+3] if channel=="alpha" else
                    (mask_raw[off+2]*299+mask_raw[off+1]*587+mask_raw[off]*114+500)//1000)
            raw[off+3]=(raw[off+3]*weight+127)//255
        source=QImage(bytes(raw),dest.width(),dest.height(),dest.width()*4,QImage.Format_ARGB32).copy()
    before=b.imaging.read_node_image(node,dest)
    after=before.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    painter=QPainter(after); painter.setOpacity(opacity); painter.drawImage(0,0,source); painter.end()
    return finish(doc,node,dest,before,after,"MCP texture patch")


def clone_stamp(params):
    doc=b.resolve_document(params.get("document"))
    node=paint_layer(doc,params.get("layer"))
    source_node=b.resolve_node(doc,b._arg(params,"source_layer",params.get("layer")))
    canvas=QRect(0,0,doc.width(),doc.height()); check_pixels(canvas)
    anchor=b._req(params,"source")
    if not isinstance(anchor,dict): raise b.OpError("source must be {x,y}")
    sx=number(b._req(anchor,"x"),"source.x",0,doc.width()-1)
    sy=number(b._req(anchor,"y"),"source.y",0,doc.height()-1)
    points=b._req(params,"points")
    if not isinstance(points,list) or not 1<=len(points)<=512:
        raise b.OpError("points needs 1 to 512 objects")
    parsed=[]
    for p in points:
        if not isinstance(p,dict): raise b.OpError("points must be {x,y,pressure} objects")
        parsed.append((number(b._req(p,"x"),"x",0,doc.width()-1),
                       number(b._req(p,"y"),"y",0,doc.height()-1),
                       number(b._arg(p,"pressure",1),"pressure",0,1)))
    size=int(number(b._arg(params,"size",32),"size",1,512))
    softness=number(b._arg(params,"softness",0.5),"softness",0,1)
    opacity=number(b._arg(params,"opacity",1),"opacity",0,1)
    dabs=[parsed[0]]
    for start,end in zip(parsed,parsed[1:]):
        count=max(1,math.ceil(math.hypot(end[0]-start[0],end[1]-start[1])/max(1,size/6)))
        if len(dabs)+count>8192: raise b.OpError("Clone stroke exceeds 8192 dabs")
        dabs.extend(tuple(a+(z-a)*i/count for a,z in zip(start,end)) for i in range(1,count+1))
    if len(dabs)*size*size>16*1024*1024:
        raise b.OpError("Clone stroke exceeds the 16 megapixel dab processing limit; split it into smaller strokes")
    # A frozen source prevents the clone from reading its own newly written dabs.
    source=b.imaging.read_node_image(source_node,canvas)
    rect=QRect()
    for x,y,_ in dabs:
        rect=rect.united(QRect(round(x-size/2),round(y-size/2),size,size))
    rect=rect.intersected(canvas); check_pixels(rect)
    before=b.imaging.read_node_image(node,rect)
    after=before.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    mask=[]
    for y in range(size):
        for x in range(size):
            radius=math.hypot((x+0.5-size/2)/(size/2),(y+0.5-size/2)/(size/2))
            mask.append(0 if radius>=1 else 1 if softness==0 else min(1,(1-radius)/softness))
    painter=QPainter(after)
    for x,y,pressure in dabs:
        left,top=round(x-size/2),round(y-size/2)
        dab=QImage(size,size,QImage.Format_ARGB32); dab.fill(QColor(0,0,0,0))
        dp=QPainter(dab)
        dp.drawImage(QPointF(parsed[0][0]-sx-left,parsed[0][1]-sy-top),source)
        dp.end()
        # Source origin maps to (destination start - source anchor); sample a
        # translated source at each destination dab, keeping a fixed offset.
        raw=bytearray(b.imaging.image_bytes(dab))
        for i,w in enumerate(mask): raw[i*4+3]=round(raw[i*4+3]*w)
        dab=QImage(bytes(raw),size,size,size*4,QImage.Format_ARGB32).copy()
        painter.setOpacity(opacity*pressure)
        painter.drawImage(left-rect.x(),top-rect.y(),dab)
    painter.end()
    result=finish(doc,node,rect,before,after,"MCP sampled clone stroke")
    result.update({"dabs":len(dabs),"source_frozen":True,"engine":"sampled_pixels"})
    return result


OPERATIONS=[("texture_patch",texture_patch,120,True),("clone_stamp",clone_stamp,120,True)]
