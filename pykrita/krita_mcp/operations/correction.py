"""Bounded, session-local frozen candidates; no arbitrary execution."""
import hashlib
import json
import time
import uuid

from .. import ops as b
from ..compat import QImage,QRect
from ..targeting import resolve,PROFILE
from .common import activate,rect_info
from .feedback import roi,mask,decode_png,compare_images,clean_frame
from .repair_preview import context,candidate_composite,visible_bytes

TRANSACTIONS={}
MAX_BYTES=64*1024*1024
TTL=600
BUSY=False


def digest(data):return hashlib.sha256(data).hexdigest()


def prune():
    for key,e in list(TRANSACTIONS.items()):
        if time.monotonic()-e['created']>TTL:
            del TRANSACTIONS[key]
        elif e['status'] in ('prepared','applied'):
            try:b.resolve_document(e['target']['document'])
            except b.OpError:
                e.update(status='expired',outcome='document_closed')
                release(e)


def release(e):
    for k in ('doc','node','before','composite_before','selection','allowed','target_mask','candidates'):
        e.pop(k,None)
    e['bytes']=0


def entry(params):
    prune()
    e=TRANSACTIONS.get(params.get('transaction'))
    if e is None:raise b.OpError('Unknown or expired repair transaction',kind='expired')
    if e['status']=='expired':raise b.OpError('Repair document session expired',kind='expired')
    return e


def fingerprint(doc,node,extent,target):
    from ..native_commands import history_state,frames,get_time
    view=activate(doc)
    selection=doc.selection()
    time_scope=target['scope'];frame_time=time_scope['time'] if time_scope['kind']=='frame' else get_time(doc)
    with clean_frame(doc,frame_time):
        pixels=bytes(node.pixelData(extent.x(),extent.y(),extent.width(),extent.height()))
    def clean(nodes):
        return [{k:clean(v) if k=='children' else v for k,v in n.items()
                 if k not in ('active','index','index_path','bounds')} for n in nodes if n['type']!='selectionmask']
    history=history_state(doc)
    context_hash,_=context(doc,frame_time)
    return {'pixels':digest(pixels),'composite_context':context_hash,'dimensions':[doc.width(),doc.height()],
            'document_color':[doc.colorModel(),doc.colorDepth(),doc.colorProfile(),doc.backgroundColor().rgba()],
            'profile':[node.colorModel(),node.colorDepth(),node.colorProfile()],
            'structure':digest(json.dumps(clean(b.layer_tree(doc)),sort_keys=True).encode()),
            'selection':None if selection is None else digest(bytes(selection.pixelData(0,0,doc.width(),doc.height()))),
            'view':[view.currentBrushPreset().name(),view.brushSize(),view.paintingOpacity(),view.eraserMode(),view.foregroundColor().components()],
            'time':get_time(doc),'frames':frames(node),'history':history,
            'timing':[doc.framesPerSecond(),doc.fullClipRangeStartTime(),doc.fullClipRangeEndTime()]}


def check(e):
    doc,node=resolve(e['target'],allow_frame=True)
    current=fingerprint(doc,node,e['extent'],e['target'])
    changed=[k for k in current if current[k]!=e['fingerprint'][k]]
    if changed:
        if 'time' in changed:b._trace('stale timeline: baseline='+str(e['fingerprint']['time'])+' current='+str(current['time']))
        raise b.OpError('Repair dependencies changed ('+', '.join(changed)+'); prepare a new baseline',kind='stale_input')
    return doc,node


def prepare_repair(params):
    prune()
    if len([e for e in TRANSACTIONS.values() if e['status']=='prepared'])>=8 or len(TRANSACTIONS)>=32:
        raise b.OpError('Repair token budget exhausted; discard older transactions',kind='resource_limit')
    target=b._req(params,'target');doc,node=resolve(target,allow_frame=True)
    rect=roi(doc,b._req(params,'region'));w,h=rect.width(),rect.height()
    mode=params.get('region_mode','roi')
    if mode not in ('roi','whole_frame'):raise b.OpError('region_mode must be roi or whole_frame')
    if mode=='whole_frame' and (target['scope']['kind']!='frame' or rect!=QRect(0,0,doc.width(),doc.height())):
        raise b.OpError('whole_frame requires frame scope and the exact full canvas region')
    allowed=mask(b._req(params,'allowed_mask'),w,h)
    expected=mask(params.get('target_mask'),w,h)
    defect=b._req(params,'defect');change=b._req(params,'intended_change')
    if not isinstance(defect,str) or not 1<=len(defect)<=512 or not isinstance(change,str) or not 1<=len(change)<=512:
        raise b.OpError('Defect/intended change require bounded nonempty text')
    from ..native_commands import get_time
    frame_time=target['scope'].get('time') if target['scope']['kind']=='frame' else get_time(doc)
    with clean_frame(doc,frame_time):
        extent=QRect(0,0,doc.width(),doc.height()).united(node.bounds())
    if extent.width()*extent.height()>1048576:raise b.OpError('Source and off-canvas snapshot exceed 1 megapixel',kind='resource_limit')
    # Full source BGRA + composite BGRA + possible canvas selection bytes,
    # plus allowed/target ROI masks. Conservative even when masks are absent.
    cost=extent.width()*extent.height()*4+doc.width()*doc.height()*5+w*h*2
    if sum(e['bytes'] for e in TRANSACTIONS.values())+cost>MAX_BYTES:
        raise b.OpError('Repair snapshot budget exhausted',kind='resource_limit')
    original=b._krita().activeDocument();original_node=original.activeNode() if original else None
    try:
        with clean_frame(doc,frame_time):
            baseline=bytes(node.pixelData(extent.x(),extent.y(),extent.width(),extent.height()))
        selection=doc.selection();selection=None if selection is None else bytes(selection.pixelData(0,0,doc.width(),doc.height()))
        state=fingerprint(doc,node,extent,target)
        _,composite_before=context(doc,frame_time)
    finally:
        if original is not None:activate(original,original_node)
    token=uuid.uuid4().hex
    TRANSACTIONS[token]={'created':time.monotonic(),'status':'prepared','doc':doc,'node':node,'target':target,
                        'region':rect,'extent':extent,'before':baseline,'selection':selection,
                        'composite_before':composite_before,
                        'allowed':allowed,'target_mask':expected,'fingerprint':state,'defect':defect,
                        'intended_change':change,'candidates':{},'bytes':cost}
    return {'transaction':token,'status':'prepared','input_hash':state['pixels'],'target':target,
            'region':rect_info(rect),'source_extent':rect_info(extent),'profile':PROFILE,
            'region_mode':mode,'candidate_limit':3,'ttl_seconds':TTL,'mutated':False}


def source_roi(e):
    extent,rect=e['extent'],e['region'];raw=e['before']
    return b''.join(raw[((rect.y()-extent.y()+y)*extent.width()+rect.x()-extent.x())*4:
                       ((rect.y()-extent.y()+y)*extent.width()+rect.x()-extent.x()+rect.width())*4] for y in range(rect.height()))


def blend(before,after,weights):
    out=bytearray(before)
    for p,weight in enumerate(weights):
        i=p*4
        if weight==255:out[i:i+4]=after[i:i+4]
        elif weight:
            a=(before[i+3]*(255-weight)+after[i+3]*weight)/255
            out[i+3]=round(a)
            for c in range(3):
                v=(before[i+c]*before[i+3]*(255-weight)+after[i+c]*after[i+3]*weight)/255
                out[i+c]=round(v/a) if a else 0
    return bytes(out)


def render_repair(params):
    e=entry(params)
    if e['status']!='prepared':raise b.OpError('Transaction is not editable',kind='conflict')
    if len(e['candidates'])>=3:raise b.OpError('At most three baseline alternatives',kind='resource_limit')
    doc,node=e['doc'],e['node'];edit=b._req(params,'edit')
    if not isinstance(edit,dict):raise b.OpError('edit must be object')
    kind=edit.get('kind');rect=e['region'];w,h=rect.width(),rect.height()
    before=source_roi(e)
    cost=w*h*4+doc.width()*doc.height()*4
    if sum(v['bytes'] for v in TRANSACTIONS.values())+cost>MAX_BYTES:
        raise b.OpError('Candidate byte budget exhausted',kind='resource_limit')
    native_receipt=None
    original=b._krita().activeDocument();original_node=original.activeNode() if original else None
    owned=None
    try:
        check(e)
        if kind=='replacement':
            if set(edit)!={'kind','data','profile'} or edit['profile']!=PROFILE:raise b.OpError('Replacement requires PNG data and exact pilot profile')
            image=decode_png(edit['data'])
            if image.width()!=w or image.height()!=h:raise b.OpError('Replacement PNG must exactly match ROI')
            rendered=b.imaging.image_bytes(image)
        elif kind in ('stroke','clone','patch','warp'):
            if set(edit)!={'kind','arguments'} or not isinstance(edit['arguments'],dict):raise b.OpError('Edit requires fixed kind and arguments')
            args=dict(edit['arguments'])
            if 'document' in args or 'layer' in args:raise b.OpError('Candidate target is owned; document/layer overrides forbidden')
            if kind=='patch' and 'data' in args:decode_png(args['data'])
            if kind=='patch' and 'mask_data' in args:decode_png(args['mask_data'])
            if kind=='stroke':
                from ..stroke_planner import plan,PlanError
                try:plan(args['spec'])
                except (PlanError,KeyError) as exc:raise b.OpError(str(exc))
                if args['spec']['target']!=e['target']:raise b.OpError('Stroke intent must address the frozen source target')
            owned=b.op_create_document({'width':doc.width(),'height':doc.height(),
                    'name':'MCP private candidate '+uuid.uuid4().hex,'color_profile':PROFILE})['session_id']
            tmp=b.resolve_document(owned)
            b.op_set_layer({'document':owned,'layer':'Background','visible':False})
            b.op_create_layer({'document':owned,'name':'Candidate'})
            work=tmp.nodeByName('Candidate');extent=e['extent']
            image=QImage(e['before'],extent.width(),extent.height(),extent.width()*4,QImage.Format_ARGB32).copy()
            b.imaging.write_node_image(work,extent,image)
            if e['selection'] is not None:
                from .selections import mask_selection
                tmp.setSelection(mask_selection(e['selection'],doc.width(),doc.height()))
            target={'document':owned,'layer':'uuid:'+b._node_uuid(work),'scope':{'kind':'static'}}
            if kind=='stroke':
                from .painting import paint_stroke
                spec=dict(args['spec']);spec['target']=target
                native_receipt=paint_stroke({'spec':spec})
            else:
                tool={'clone':'clone_stamp','patch':'texture_patch','warp':'warp_layer'}[kind]
                if kind=='warp':args.update(mode='apply',name='Candidate Warp')
                native_receipt=b.OPS[tool](dict(args,document=owned,layer=target['layer']))
                if kind=='warp':work=tmp.nodeByName('Candidate Warp')
            rendered=bytes(work.pixelData(rect.x(),rect.y(),w,h))
        else:raise b.OpError('Repair kind must be replacement, stroke, clone, patch or warp',kind='unsupported')
    finally:
        if original is not None:activate(original,original_node)
        if owned is not None:b.op_close_document({'document':owned,'discard_changes':True})
        if original is not None and original in b._documents():activate(original,original_node)
    weights=bytearray(e['allowed'])
    if e['selection'] is not None:
        for y in range(h):
            for x in range(w):
                p=y*w+x;v=e['selection'][(rect.y()+y)*doc.width()+rect.x()+x]
                if not v:weights[p]=0
                elif kind=='replacement':weights[p]=(weights[p]*v+127)//255
    candidate=blend(before,rendered,weights)
    from ..feedback import measurements
    metrics,_,_=measurements(before,candidate,w,h,e['allowed'],e['target_mask'])
    baseline,_,_=measurements(before,before,w,h,e['allowed'],e['target_mask'])
    valid=metrics['outside_allowed_pixels']==0
    # With a target silhouette, reject objective regressions against baseline.
    if e['target_mask'] is not None:
        valid=valid and metrics['target_iou']>=baseline['target_iou']
    identifier=uuid.uuid4().hex
    original=b._krita().activeDocument();original_node=original.activeNode() if original else None
    try:
        check(e)
        scope=e['target']['scope']
        composite_after=candidate_composite(doc,node,scope.get('time',e['fingerprint']['time']),rect,candidate,e['composite_before'])
    finally:
        if original is not None and original in b._documents():activate(original,original_node)
    e['candidates'][identifier]={'pixels':candidate,'metrics':metrics,'baseline_metrics':baseline,'valid':valid,
                                'composite_after':composite_after,
                                'edit_hash':digest(json.dumps(edit,sort_keys=True).encode()),'receipt':native_receipt}
    e['bytes']+=cost
    return {'transaction':params['transaction'],'candidate':identifier,'status':'rendered','valid':valid,
            'metrics':metrics,'baseline_metrics':baseline,'result_hash':digest(candidate),
            'native_receipt':native_receipt,'mutated':False}


def inspect_repair(params):
    view=params.get('view','composite')
    if view not in ('composite','raw_layer'):raise b.OpError('view must be composite or raw_layer')
    selected=params.get('image','panel')
    if selected not in ('panel','before','after'):raise b.OpError('image must be panel, before or after')
    panel_size=params.get('max_size',1024) if selected=='panel' else 1024
    e=entry(params)
    if e['status']!='prepared':return repair_status(params)
    candidate=e['candidates'].get(params.get('candidate'))
    if candidate is None:raise b.OpError('Unknown frozen candidate',kind='not_found')
    r=e['region'];w,h=r.width(),r.height()
    before=QImage(source_roi(e),w,h,w*4,QImage.Format_ARGB32).copy()
    after=QImage(candidate['pixels'],w,h,w*4,QImage.Format_ARGB32).copy()
    allowed=QImage(w,h,QImage.Format_ARGB32);allowed.fill(0)
    raw=bytearray(w*h*4)
    for i,v in enumerate(e['allowed']):raw[i*4+3]=v
    allowed=QImage(bytes(raw),w,h,w*4,QImage.Format_ARGB32).copy()
    result=compare_images({'before':b.imaging.qimage_to_png_b64(before),'after':b.imaging.qimage_to_png_b64(after),
                          'allowed_mask':b.imaging.qimage_to_png_b64(allowed),'offset':{'x':r.x(),'y':r.y()},
                          'max_size':panel_size})
    raw_layer=dict(result)
    doc=e['doc'];cw,ch=doc.width(),doc.height()
    composite_before=QImage(e['composite_before'],cw,ch,cw*4,QImage.Format_ARGB32).copy()
    composite_after=QImage(candidate['composite_after'],cw,ch,cw*4,QImage.Format_ARGB32).copy()
    composite=compare_images({'before':b.imaging.qimage_to_png_b64(composite_before),
                              'after':b.imaging.qimage_to_png_b64(composite_after),
                              'max_size':panel_size})
    composite.update(view='krita_document_composite',time=e['target']['scope'].get('time',e['fingerprint']['time']),
                     profile=PROFILE,channel_order='BGRA/U8 straight alpha',
                     before_hash=digest(visible_bytes(e['composite_before'])),
                     after_hash=digest(visible_bytes(candidate['composite_after'])),
                     source_region=rect_info(e['region']))
    raw_layer.update(before_hash=digest(source_roi(e)),after_hash=digest(candidate['pixels']))
    panel_images={name:diagnostics.pop('png_base64') for name,diagnostics in
                  (('raw_layer',raw_layer),('composite',composite))}
    for name,diagnostics in (('raw_layer',raw_layer),('composite',composite)):
        diagnostics['images']={kind:{'view':name,'image':kind} for kind in ('panel','before','after')}
    # The main image is full document context. Existing hard-constraint
    # numbers continue to describe raw layer bytes and have an explicit view.
    diagnostics=raw_layer if view=='raw_layer' else composite
    result.update(png_base64=panel_images[view],mapping=diagnostics['mapping'],
                  panels=diagnostics['panels'],diagnostic_source='raw_layer',raw_layer=raw_layer,composite=composite)
    result.update(transaction=params['transaction'],candidate=params['candidate'],valid=candidate['valid'],
                  baseline_metrics=candidate['baseline_metrics'],candidate_metrics=candidate['metrics'])
    result.update(view=view,image=selected)
    if selected!='panel':
        images=(before,after) if view=='raw_layer' else (composite_before,composite_after)
        result['png_base64']=b.imaging.qimage_to_png_b64(images[0 if selected=='before' else 1])
        result['mapping']=dict(diagnostics['mapping'],scale=1)
        result['panels']=[selected]
    return result


def commit_repair(params):
    global BUSY
    if set(params)-{'transaction','candidate'}:raise b.OpError('Commit accepts only transaction and frozen candidate')
    e=entry(params);identifier=b._req(params,'candidate')
    if e['status']=='applied':
        if identifier!=e['committed_candidate']:raise b.OpError('Commit payload conflicts with recorded outcome',kind='conflict')
        return dict(e['outcome'],repeated=True)
    if e['status']!='prepared':raise b.OpError('Transaction is not committable',kind='expired')
    candidate=e['candidates'].get(identifier)
    if candidate is None:raise b.OpError('Unknown candidate',kind='not_found')
    if not candidate['valid']:raise b.OpError('Candidate violates hard constraints/objective',kind='invalid_candidate')
    if BUSY:raise b.OpError('Another correction is in progress',kind='busy')
    original=b._krita().activeDocument();original_node=original.activeNode() if original else None
    BUSY=True
    try:
        doc,node=check(e)
        from ..native_commands import atomic_write_raw,atomic_write_frame,frames
        scope=e['target']['scope'];exposure=None
        if scope['kind']=='frame':
            keys=frames(node);source=max(t for t in keys if t<=scope['time'])
            end=min((t-1 for t in keys if t>source),default=None)
            exposure={'requested_time':scope['time'],'source_keyframe':source,'policy':scope['exposure_policy'],
                      'affected_start':scope['time'] if scope['exposure_policy']=='one_sample' else source,
                      'affected_end':scope['time'] if scope['exposure_policy']=='one_sample' else end}
        result={'operation_id':uuid.uuid4().hex,'transaction':params['transaction'],'candidate':identifier,
                'status':'applied','execution_outcome':'known','target':e['target'],'region':rect_info(e['region']),
                'input_hash':digest(source_roi(e)),'baseline_hash':e['fingerprint']['pixels'],'result_hash':digest(candidate['pixels']),
                'undo_steps':0,'mutated':False,'metrics':candidate['metrics'],'repeated':False,
                'exposure':exposure,'completion_error':None}
        def published(steps):
            # In-process publication receipt. No Qt/event/refresh calls here.
            result.update(undo_steps=steps,mutated=bool(steps))
            if exposure is not None:
                after=set(keys)
                if steps and scope['exposure_policy']=='one_sample':after.update((scope['time'],scope['time']+1))
                exposure['keyframes_after']=sorted(after)
            e.update(status='applied',committed_candidate=identifier,outcome=result)
        try:
            if scope['kind']=='frame':
                atomic_write_frame(doc,node,scope['time'],scope['exposure_policy'],e['region'],candidate['pixels'],
                                   timing=e['fingerprint']['timing'],on_published=published)
                exposure['keyframes_after']=frames(node)
            else:
                atomic_write_raw(doc,node,e['region'],candidate['pixels'],'MCP bounded repair',on_published=published)
        except Exception as exc:
            if e['status']=='applied':
                result['completion_error']={'kind':getattr(exc,'kind','completion_failure'),'message':str(exc)}
                release(e)
                raise b.OpError('Repair was applied; completion failed. Query repair_status for the recorded outcome: '+str(exc),
                                kind='completion_failure') from exc
            raise
        release(e)
        return result
    finally:
        BUSY=False
        if original is not None and original in b._documents():activate(original,original_node)


def repair_status(params):
    e=entry(params)
    result={'transaction':params['transaction'],'status':e['status'],'bytes_retained':e['bytes'],
            'candidate_count':len(e.get('candidates',{})),'session_bound':True,'crash_durable':False}
    if e['status']=='applied':result['outcome']=e['outcome']
    return result


def discard_repair(params):
    e=entry(params)
    if e['status']=='applied':raise b.OpError('Applied outcomes remain queryable until expiration',kind='conflict')
    e['status']='discarded';release(e)
    return {'transaction':params['transaction'],'status':'discarded','bytes_retained':0,'mutated':False}


OPERATIONS=[('prepare_repair',prepare_repair,60,False),('render_repair',render_repair,180,False),
            ('inspect_repair',inspect_repair,60,False),('commit_repair',commit_repair,120,True),
            ('repair_status',repair_status,30,False),('discard_repair',discard_repair,30,False)]
