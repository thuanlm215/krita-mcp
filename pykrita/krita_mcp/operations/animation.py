"""Raster keyframes, native frame history and bounded PNG/atlas exports."""
import json
import math
from pathlib import Path
import shutil
import base64
import hashlib

from .. import ops as b
from ..native_commands import get_time
from ..compat import QColor, QImage, QPainter, QRect
from .common import activate, action, check_pixels, number, paint_layer, stage_directory


def integer(value,name,minimum=0,maximum=100000):
    numeric=number(value,name)
    if not numeric.is_integer():raise b.OpError(name+" must be an integer")
    result=int(numeric)
    if not minimum<=result<=maximum:raise b.OpError(name+" is out of range")
    return result


def animation_frame(params):
    from ..native_commands import frames,frame,onion,push,stack,set_time
    doc=b.resolve_document(params.get("document"))
    node=paint_layer(doc,params.get("layer"),allow_masks=True,allow_animated=True)
    mode=str(b._arg(params,"mode","info"))
    if mode not in ("info","blank","duplicate","delete","settings","scrub","onion"):
        raise b.OpError("Unknown animation mode")
    time=integer(b._arg(params,"time",get_time(doc)),"time")
    activate(doc,node)
    steps=0
    if mode in ("blank","duplicate","delete"):
        stack(doc)
        keys=frames(node)
        if mode=="blank" and time==0 and not node.animated() and not node.bounds().isEmpty() and not b._as_bool(b._arg(params,"replace",False),"replace"):
            raise b.OpError("Static pixels become frame 0; choose a later blank frame or set replace=true")
        if mode=="delete":
            if time==0 or time not in keys:raise b.OpError("Delete needs an existing keyframe after frame 0")
        elif time in keys and not b._as_bool(b._arg(params,"replace",False),"replace"):
            raise b.OpError("Keyframe exists; set replace=true to overwrite it")
        source=integer(b._arg(params,"source_time",max((t for t in keys if t<=time),default=0)),"source_time")
        if mode=="duplicate" and node.animated() and (source not in keys or source==time):
            raise b.OpError("source_time must identify a different existing keyframe")
        if mode=="duplicate" and not node.animated() and source!=0:
            raise b.OpError("A static layer's source_time is 0")
        set_time(doc,time)
        if node.animated():
            frame(doc,node,time,{"blank":0,"duplicate":1,"delete":2}[mode],source)
        else:
            # Native initial-frame actions initialize animation and preserve
            # the static image as frame 0, as Krita's timeline does.
            action(doc,"add_blank_frame" if mode=="blank" else "add_duplicate_frame")
        if mode!="delete" and not node.hasKeyframeAtTime(time):
            raise b.OpError("Native action did not create the requested frame",kind="native_failure")
        steps=1
    elif mode=="scrub":
        set_time(doc,time)
    elif mode=="settings":
        before=(doc.framesPerSecond(),doc.fullClipRangeStartTime(),doc.fullClipRangeEndTime())
        after=(integer(b._arg(params,"fps",before[0]),"fps",1,120),
               integer(b._arg(params,"start",before[1]),"start"),
               integer(b._arg(params,"end",before[2]),"end"))
        if after[2]<after[1]:raise b.OpError("end must be >= start")
        def restore(document,state):
            document.setFramesPerSecond(state[0])
            document.setFullClipRangeStartTime(state[1]);document.setFullClipRangeEndTime(state[2])
        if after!=before:push(doc,restore,before,after,"MCP animation settings");steps=1
    elif mode=="onion":
        if not node.animated():raise b.OpError("Enable animation before onion skin")
        value=b._as_bool(b._req(params,"enabled"),"enabled")
        before=onion(node);target="uuid:"+b._node_uuid(node)
        def restore(document,state):onion(b.resolve_node(document,target),int(state))
        if before!=value:push(doc,restore,before,value,"MCP onion skin");steps=1
    return {"layer":node.name(),"animated":node.animated(),"keyframes":frames(node),
            "current_time":get_time(doc),"fps":doc.framesPerSecond(),
            "start":doc.fullClipRangeStartTime(),"end":doc.fullClipRangeEndTime(),
            "onion_skin":onion(node),"undoable":mode not in ("info","scrub"),"undo_steps":steps}


def _save_export_image(image,path):
    return image.save(str(path),'PNG')


def export_animation(params):
    from ..native_commands import onion,set_time
    doc=b.resolve_document(params.get("document"))
    activate(doc)
    start=integer(b._arg(params,"start",doc.fullClipRangeStartTime()),"start")
    end=integer(b._arg(params,"end",doc.fullClipRangeEndTime()),"end")
    step=integer(b._arg(params,"step",1),"step",1,10000)
    if end<start:raise b.OpError("end must be >= start")
    times=list(range(start,end+1,step))
    if len(times)>128:raise b.OpError("Export is limited to 128 sampled frames")
    columns=integer(b._arg(params,"columns",min(8,len(times))),"columns",1,128)
    columns=min(columns,len(times));rows=math.ceil(len(times)/columns)
    check_pixels(QRect(0,0,doc.width()*columns,doc.height()*rows))
    anchor=params.get("anchor",{"x":doc.width()/2,"y":doc.height()/2})
    if not isinstance(anchor,dict):raise b.OpError("anchor must be {x,y}")
    anchor={"x":number(b._req(anchor,"x"),"anchor.x"),"y":number(b._req(anchor,"y"),"anchor.y")}
    output=Path(str(b._req(params,"directory"))).expanduser().absolute()
    if output.exists():raise b.OpError("Export directory must be new; existing files are preserved")
    current=get_time(doc);fps=doc.framesPerSecond();modified=doc.modified()
    onion_nodes=[n for _,_,n in b._flatten_tree(doc.rootNode(),"","",[])
                 if n.type()=="paintlayer" and n.animated() and onion(n)]
    output.parent.mkdir(parents=True,exist_ok=True)
    stage=stage_directory(output.parent,".krita-animation-")
    atlas=QImage(doc.width()*columns,doc.height()*rows,QImage.Format_ARGB32)
    atlas.fill(QColor(0,0,0,0));entries=[]
    atlas_ptr=atlas.bits();atlas_ptr.setsize(atlas.sizeInBytes());atlas_bytes=memoryview(atlas_ptr)
    try:
        # Onion skins are part of native layer projection, not only canvas
        # overlays. Disable them explicitly for clean animation composites.
        for node in onion_nodes:onion(node,0)
        for i,t in enumerate(times):
            set_time(doc,t)
            image=doc.projection(0,0,doc.width(),doc.height()).convertToFormat(QImage.Format_ARGB32)
            name="frame_%06d.png"%t
            if not _save_export_image(image,stage/name):raise b.OpError("Could not save frame "+name)
            x=(i%columns)*doc.width();y=(i//columns)*doc.height()
            # Atlas cells are frame transfers, not source-over compositions.
            # Copy straight-alpha RGBA bytes, including RGB under zero alpha.
            raw=b.imaging.image_bytes(image);row_size=doc.width()*4
            for row in range(doc.height()):
                offset=((y+row)*atlas.width()+x)*4
                atlas_bytes[offset:offset+row_size]=raw[row*row_size:(row+1)*row_size]
            entries.append({"time":t,"file":name,"x":x,"y":y,"width":doc.width(),"height":doc.height(),
                            "duration_ms":min(step,end+1-t)*1000/fps,"anchor":anchor})
        if not _save_export_image(atlas,stage/'atlas.png'):raise b.OpError("Could not save atlas")
        manifest={"fps":fps,"start":start,"end":end,"step":step,"atlas":"atlas.png",
                  "width":atlas.width(),"height":atlas.height(),"frames":entries,
                  'atlas_transfer':'exact ARGB32/U8 straight-alpha frame bytes','source_profile':doc.colorProfile()}
        (stage/'manifest.json').write_text(json.dumps(manifest,indent=2)+"\n")
        stage.rename(output)
    finally:
        for node in onion_nodes:onion(node,1)
        set_time(doc,current)
        doc.setModified(modified)
        if stage.exists():shutil.rmtree(stage)
    return {"directory":str(output),"frames":len(times),"atlas":str(output/'atlas.png'),
            "manifest":str(output/'manifest.json'),"fps":fps,"restored_time":current}


def import_animation(params):
    """Validate every image before creating an owned, registered animation."""
    from .feedback import decode_png
    from ..native_commands import atomic_write_frame,set_time,frames
    from ..compat import QByteArray
    name=b._req(params,'name');entries=b._req(params,'frames')
    if not isinstance(name,str) or not 1<=len(name)<=128:raise b.OpError('name must contain 1..128 characters')
    fps=integer(b._req(params,'fps'),'fps',1,120)
    if not isinstance(entries,list) or not 1<=len(entries)<=64:raise b.OpError('Import requires 1..64 ordered frames')
    images=[];receipts=[];last=-1;size=None;budget=0
    for entry in entries:
        if not isinstance(entry,dict) or set(entry)!={'time','path'}:raise b.OpError('Frame requires explicit time and PNG path')
        t=integer(entry['time'],'time',0,99998)
        if t<=last or not images and t!=0:raise b.OpError('Times must strictly increase, beginning at 0')
        path=Path(b._abspath(entry['path']))
        if not path.is_file() or path.stat().st_size>6*1024*1024:raise b.OpError('Missing PNG or file over 6 MiB')
        raw=path.read_bytes();image=decode_png(base64.b64encode(raw).decode())
        dims=(image.width(),image.height())
        if size is not None and size!=dims:raise b.OpError('Frames must have identical padded canvas dimensions')
        size=dims;last=t;budget+=dims[0]*dims[1]*4
        if budget>64*1024*1024:raise b.OpError('Import decoded-image budget is 64 MiB',kind='resource_limit')
        images.append((t,image));receipts.append({'time':t,'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()})
    anchor=b._req(params,'anchor')
    if not isinstance(anchor,dict) or set(anchor)!={'x','y'}:raise b.OpError('anchor requires x,y in canvas pixels')
    anchor={k:number(v,'anchor.'+k,0,size[0] if k=='x' else size[1]) for k,v in anchor.items()}
    end=integer(b._arg(params,'end',last),'end',last,99999)
    original=b._krita().activeDocument();original_node=original.activeNode() if original else None
    owned=None
    try:
        b._trace('import: create owned document')
        result=b.op_create_document({'name':name,'width':size[0],'height':size[1],
                                    'color_profile':'sRGB-elle-V2-srgbtrc.icc'})
        owned=result['session_id'];doc=b.resolve_document(owned)
        b._trace('import: hide background')
        b.op_set_layer({'document':owned,'layer':'Background','visible':False})
        b._trace('import: create Frames layer')
        b.op_create_layer({'document':owned,'name':'Frames'})
        b._trace('import: write initial pixels')
        node=doc.nodeByName('Frames');canvas=QRect(0,0,*size)
        b.imaging.write_node_image(node,canvas,images[0][1])
        b._trace('import: initializing native frame 0')
        animation_frame({'document':owned,'layer':'Frames','mode':'duplicate','time':0,'replace':True})
        from ..compat import QtCore
        QtCore.QCoreApplication.sendPostedEvents(None,0)
        for t,image in images:
            b._trace('import: stage/publish exact frame '+str(t))
            atomic_write_frame(doc,node,t,'import_keyframe',canvas,b.imaging.image_bytes(image))
            b._trace('import: published exact frame '+str(t))
        b._trace('import: metadata')
        doc.setFramesPerSecond(fps);doc.setFullClipRangeStartTime(0);doc.setFullClipRangeEndTime(end)
        metadata={'version':1,'fps':fps,'anchor':anchor,'width':size[0],'height':size[1],
                  'times':[t for t,_ in images],'sources':receipts,'registration':'preserved pixels; no crop, scale or recenter'}
        doc.setAnnotation('krita-mcp-animation','MCP animation registration',QByteArray(json.dumps(metadata).encode()))
        set_time(doc,0)
        return {'document':b._doc_summary(doc),'target':{'document':owned,'layer':'uuid:'+b._node_uuid(node),
                    'scope':{'kind':'frame','time':0,'exposure_policy':'existing_keyframe'}},
                'keyframes':frames(node),'metadata':metadata,'owned_document':True}
    except Exception:
        if owned is not None:b.op_close_document({'document':owned,'discard_changes':True})
        raise
    finally:
        if original is not None and original in b._documents():activate(original,original_node)


def animation_review(params):
    from .feedback import acquire
    doc=b.resolve_document(b._req(params,'document'))
    t=integer(b._req(params,'time'),'time',0,99999)
    times=[max(doc.fullClipRangeStartTime(),t-1),t,min(doc.fullClipRangeEndTime(),t+1)]
    images=[];hashes=[];rect=None
    for current in times:
        _,rect,image,raw=acquire(dict(params,time=current))
        images.append(image);hashes.append(hashlib.sha256(raw).hexdigest())
    max_size=integer(b._arg(params,'max_size',1536),'max_size',64,2048)
    images=[b.imaging.scale_to_fit(img,max_size//3)[0] for img in images]
    panel=QImage(images[0].width()*3,images[0].height(),QImage.Format_ARGB32);panel.fill(QColor(32,32,32))
    p=QPainter(panel)
    for i,image in enumerate(images):p.drawImage(i*image.width(),0,image)
    p.end()
    annotation=bytes(doc.annotation('krita-mcp-animation'))
    metadata=json.loads(annotation) if annotation else None
    return {'times':times,'hashes':hashes,'panels':['previous','current','next'],
            'source_rect':{'x':rect.x(),'y':rect.y(),'width':rect.width(),'height':rect.height()},
            'scale':images[0].width()/rect.width(),'registration':metadata,
            'fps':doc.framesPerSecond(),'onion_skin_included':False,'mutated':False,
            'png_base64':b.imaging.qimage_to_png_b64(panel)}


OPERATIONS=[("animation_frame",animation_frame,120,True),("export_animation",export_animation,240,False),
            ('import_animation',import_animation,240,True),('animation_review',animation_review,120,False)]
