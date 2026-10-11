"""Grouped native edits, transparency masks, bake and image comparisons."""
import builtins
import uuid

from .. import ops as b
from ..native_commands import get_time
from ..compat import QColor, QImage, QPainter, QRect
from .common import activate, check_pixels, number
from .selections import mask_selection

ALLOWED = {"draw", "draw_brush", "select_polygon", "select_content", "set_selection",
           "selection_to_layer", "transform_layer", "texture_patch", "clone_stamp",
           "mask_layer", "bake_layer", "create_layer", "apply_filter", "animation_frame"}
SNAPSHOTS = getattr(builtins,"_krita_mcp_preview_snapshots",None)
if SNAPSHOTS is None:
    SNAPSHOTS={}; builtins._krita_mcp_preview_snapshots=SNAPSHOTS


def edit_batch(params):
    from ..native_commands import group, stack
    doc=b.resolve_document(params.get("document"))
    edits=b._req(params,"edits")
    if not isinstance(edits,list) or not 1<=len(edits)<=64:
        raise b.OpError("edits must contain 1 to 64 operations")
    prepared=[]
    for edit in edits:
        if isinstance(edit, dict) and edit.get("tool") == "warp_layer":
            raise b.OpError("warp_layer is not supported inside edit_batch; no edits were started.",
                            kind="unsupported")
        if not isinstance(edit,dict) or edit.get("tool") not in ALLOWED:
            raise b.OpError("Batch tool must be one of: "+", ".join(sorted(ALLOWED)))
        args=edit.get("arguments",{})
        if not isinstance(args,dict): raise b.OpError("arguments must be an object")
        args=dict(args)
        if "document" in args and b.resolve_document(args["document"])!=doc:
            raise b.OpError("Every edit must use the batch document")
        args["document"]=b._documents().index(doc)
        if edit["tool"]=="transform_layer" and args.get("mode","preview")!="apply":
            raise b.OpError("Batch transforms must use mode apply")
        if edit["tool"]=="animation_frame" and args.get("mode") not in ("blank","duplicate"):
            raise b.OpError("Batch animation only supports creating blank/duplicate frames")
        prepared.append((edit["tool"],args))
    lib,ptr=stack(doc);index=lib.mcp_index(ptr)
    node=doc.activeNode();time=get_time(doc)
    result=[]
    try:
        with group(doc,str(b._arg(params,"label","MCP edit batch"))[:200]):
            for tool,args in prepared:
                result.append({"tool":tool,"result":b.OPS[tool](args)})
    except Exception:
        from ..native_commands import set_time
        set_time(doc,time)
        if node is not None: doc.setActiveNode(node)
        b._refresh(doc)
        raise
    return {"results":result,"undoable":True,"undo_steps":int(lib.mcp_index(ptr)>index),
            "rollback_on_error":True}


def mask_layer(params):
    from ..native_commands import write_raw,stack
    doc=b.resolve_document(params.get("document"))
    parent=b.resolve_node(doc,params.get("layer"))
    if parent.type() not in ("paintlayer","grouplayer") or parent.locked() or parent.animated():
        raise b.OpError("Choose an unlocked static paint or group layer")
    canvas=QRect(0,0,doc.width(),doc.height());check_pixels(canvas)
    ref=params.get("mask")
    mask=b.resolve_node(doc,ref) if ref is not None else None
    if mask is not None and (mask.type()!="transparencymask" or mask.parentNode()!=parent or mask.locked()):
        raise b.OpError("mask must be an unlocked transparency mask of this layer")
    mode=str(b._arg(params,"mode","selection"))
    if mode=="selection":
        selection=doc.selection()
        if selection is None: raise b.OpError("No active selection to use as a mask")
        data=bytes(selection.pixelData(0,0,doc.width(),doc.height()))
    elif mode=="fill":
        value=int(number(b._arg(params,"value",255),"value",0,255))
        data=bytes([value])*(doc.width()*doc.height())
    elif mode=="image":
        image=b.imaging.png_b64_to_qimage(b._req(params,"data"))
        if image.size()!=canvas.size():raise b.OpError("Mask image must match canvas size")
        channel=str(b._arg(params,"channel","alpha"))
        raw=b.imaging.image_bytes(image)
        if channel=="alpha":data=raw[3::4]
        elif channel=="luminance":
            data=bytes((raw[i+2]*299+raw[i+1]*587+raw[i]*114+500)//1000 for i in range(0,len(raw),4))
        else:raise b.OpError("channel must be alpha or luminance")
    else:raise b.OpError("mode must be selection, fill or image")
    selection=mask_selection(data,doc.width(),doc.height())
    stack(doc)
    created=mask is None
    if created:
        mask=doc.createTransparencyMask(str(b._arg(params,"name","MCP Transparency")))
        mask.setSelection(selection)
        if not parent.addChildNode(mask,None):raise b.OpError("Could not attach transparency mask")
        b._refresh(doc)
    else:
        steps=write_raw(doc,mask,canvas,data,"MCP transparency mask")
    return {"mask":b._find_in_tree(b.layer_tree(doc),mask),"created":created,
            "undoable":True,"undo_steps":1 if created else steps,"mode":mode}


def bake_layer(params):
    from ..native_commands import group,visibility,projection_bounds
    doc=b.resolve_document(params.get("document"));node=b.resolve_node(doc,params.get("layer"))
    if node.type() not in ("paintlayer","grouplayer") or node.locked() or node.animated():
        raise b.OpError("Choose an unlocked static paint/group layer")
    if any(n.animated() for _,_,n in b._flatten_tree(node,"","",[])):
        raise b.OpError("Cannot bake animated children")
    b.imaging.require_pixel_ops(node)
    doc.waitForDone()
    rect=projection_bounds(node);check_pixels(rect)
    if rect.isEmpty():raise b.OpError("Cannot bake an empty projection")
    raw=bytes(node.projectionPixelData(rect.x(),rect.y(),rect.width(),rect.height()))
    if len(raw)!=rect.width()*rect.height()*4:raise b.OpError("Invalid projection buffer")
    image=QImage(raw,rect.width(),rect.height(),rect.width()*4,QImage.Format_ARGB32).copy()
    with group(doc,"MCP bake layer"):
        baked=doc.createNode(str(b._arg(params,"name",node.name()+" baked")),"paintlayer")
        b.imaging.write_node_image(baked,rect,image)
        baked.setOpacity(node.opacity());baked.setBlendingMode(node.blendingMode())
        if not node.parentNode().addChildNode(baked,node):raise b.OpError("Could not attach baked layer")
        visibility(doc,node,False)
    doc.setActiveNode(baked)
    return {"layer":b._find_in_tree(b.layer_tree(doc),baked),"source_preserved":True,
            "undoable":True,"undo_steps":1,
            "region":{"x":rect.x(),"y":rect.y(),"width":rect.width(),"height":rect.height()}}


def preview_changes(params):
    doc=b.resolve_document(params.get("document"))
    mode=str(b._arg(params,"mode","checkpoint"))
    if mode not in ("checkpoint","compare","discard"):raise b.OpError("Unknown preview mode")
    token=params.get("checkpoint")
    if mode!="checkpoint":
        entry=SNAPSHOTS.get(token)
        if entry is None or entry[0]!=doc:raise b.OpError("Checkpoint does not belong to this document")
        if mode=="discard":
            del SNAPSHOTS[token];return {"discarded":True}
    canvas=QRect(0,0,doc.width(),doc.height());check_pixels(canvas)
    b._refresh(doc)
    current=doc.projection(0,0,doc.width(),doc.height()).convertToFormat(QImage.Format_ARGB32)
    if mode=="checkpoint":
        # Drop checkpoints of closed documents and enforce a bounded budget.
        for key,(document,image) in list(SNAPSHOTS.items()):
            if document not in b._documents():del SNAPSHOTS[key]
        used=sum(image.width()*image.height()*4 for _,image in SNAPSHOTS.values())
        if len(SNAPSHOTS)>=8 or used+canvas.width()*canvas.height()*4>128*1024*1024:
            raise b.OpError("Preview budget full; discard older checkpoints")
        token=uuid.uuid4().hex;SNAPSHOTS[token]=(doc,current.copy())
        return {"checkpoint":token,"width":doc.width(),"height":doc.height(),"mutated":False}
    before=entry[1]
    if before.size()!=current.size():raise b.OpError("Canvas size changed since checkpoint")
    old,new=b.imaging.image_bytes(before),b.imaging.image_bytes(current)
    changed=0;left=doc.width();top=doc.height();right=bottom=-1
    diff=bytearray(len(old))
    for i in range(doc.width()*doc.height()):
        start=i*4
        if old[start:start+4]!=new[start:start+4]:
            changed+=1;x=i%doc.width();y=i//doc.width()
            left=min(left,x);right=max(right,x);top=min(top,y);bottom=max(bottom,y)
            diff[start:start+4]=bytes((200,40,255,255))
    difference=QImage(bytes(diff),doc.width(),doc.height(),doc.width()*4,QImage.Format_ARGB32).copy()
    max_size=int(number(b._arg(params,"max_size",1536),"max_size",64,2048))
    # Before, after, and a magenta difference map, in that order.
    scaled=[b.imaging.scale_to_fit(img,max_size//3)[0] for img in (before,current,difference)]
    panel=QImage(scaled[0].width()*3,scaled[0].height(),QImage.Format_ARGB32);panel.fill(QColor(32,32,32))
    p=QPainter(panel)
    for i,img in enumerate(scaled):p.drawImage(i*scaled[0].width(),0,img)
    p.end();factor=scaled[0].width()/doc.width()
    return {"checkpoint":token,"changed_pixels":changed,"mutated":False,
            "changed_bounds":None if not changed else {"x":left,"y":top,"width":right-left+1,"height":bottom-top+1},
            "panels":["before","after","difference"],"png_base64":b.imaging.qimage_to_png_b64(panel),
            "width":panel.width(),"height":panel.height(),"scale":factor}


OPERATIONS=[("edit_batch",edit_batch,240,True),("mask_layer",mask_layer,120,True),
            ("bake_layer",bake_layer,120,True),("preview_changes",preview_changes,60,False)]
