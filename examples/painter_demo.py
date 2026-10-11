#!/usr/bin/env python3
"""Three public-MCP demonstrations. Requires run_isolated_tests.py.

python scripts/run_isolated_tests.py --output artifacts/ai-painter/demo -- \
 python examples/painter_demo.py --sequence /path/to/frame_by_frame \
 --reference artifacts/ai-painter/reference/met-lantern.jpg
--review pauses at image checkpoints for local JSON choices; no code accepted.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.test_mcp import Client,_image_of
from tests.stroke_spec import stroke_spec

PROFILE='sRGB-elle-V2-srgbtrc.icc'


def encoded(image):
    stream=io.BytesIO();image.save(stream,format='PNG');return base64.b64encode(stream.getvalue()).decode()


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def visible_bytes(image):
    raw=bytearray(image.convert('RGBA').tobytes())
    for i in range(0,len(raw),4):
        if not raw[i+3]:raw[i:i+3]=b'\0\0\0'
    return bytes(raw)


def sanitize(value):
    if isinstance(value,dict):return {k:sanitize(v) for k,v in value.items()}
    if isinstance(value,list):return [sanitize(v) for v in value]
    if isinstance(value,str) and len(value)>800:return {'bytes':len(value),'sha256':hashlib.sha256(value.encode()).hexdigest()}
    return value


class Demo:
    def __init__(self,args):
        self.args=args;self.out=Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])/'demos'
        self.out.mkdir(exist_ok=False);self.receipts=[];self.results={};self.doc=None;self.layer=None
        self.client=Client();self.client.request('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'painter-demo','version':'1'}})
        self.client.notify('notifications/initialized')
        self.runtime=self.call('list_capabilities',kind='workflow')
        self.runtime['checkout']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        self.runtime['helper_manifest']=json.loads((ROOT/'pykrita/krita_mcp/native_manifest.json').read_text())
        (self.out/'runtime.json').write_text(json.dumps(self.runtime,indent=2))

    def call(self,tool,**args):
        start=time.monotonic();result,blocks=self.client.ok(tool,args)
        self.receipts.append({'tool':tool,'seconds':round(time.monotonic()-start,4),'arguments':sanitize(args),'result':sanitize(result)})
        self.blocks=blocks
        return result

    def checkpoint(self,name,choices,default):
        if not self.args.review:return default
        path=self.out/(name+'-decision.json')
        (self.out/(name+'-ready.json')).write_text(json.dumps({'choices':choices,'default':default,'images':'see case folder'},indent=2))
        deadline=time.monotonic()+600
        while not path.exists():
            if time.monotonic()>deadline:raise RuntimeError('Image review timed out: '+str(path))
            time.sleep(.2)
        choice=json.loads(path.read_text())['choice']
        if choice not in choices:raise ValueError('Unsupported review choice')
        return choice

    def create(self,name,size=256):
        self.doc=self.call('create_document',name=name,width=size,height=size,color_profile=PROFILE)['session_id']
        self.call('set_layer',document=self.doc,layer='Background',visible=False)

    def new_layer(self,name):
        self.layer='uuid:'+self.call('create_layer',document=self.doc,name=name)['layer']['uuid']

    def target(self,scope=None):return {'document':self.doc,'layer':self.layer,'scope':scope or {'kind':'static'}}

    def image(self,path,layer='merged',**extra):
        self.call('get_image',document=self.doc,layer=layer,max_size=0,**extra)
        data=base64.b64decode(_image_of(self.blocks)['data']);path.write_bytes(data)
        return Image.open(io.BytesIO(data)).convert('RGBA')

    def pixels(self,**extra):
        self.call('get_image',document=self.doc,layer=self.layer,max_size=0,**extra)
        return Image.open(io.BytesIO(base64.b64decode(_image_of(self.blocks)['data']))).convert('RGBA')

    def stroke(self,geometry,size,color,route='planned',pressure=(1,1),preset='b) Basic-5 Size',opacity=1,erase=False):
        spec=stroke_spec(self.target(),geometry,size=size,color=color,erase=erase,stroke_id='demo-'+str(len(self.receipts)))
        spec['preset']=preset;spec['opacity']=opacity
        spec['expected_bounds']={'x':0,'y':0,'width':256,'height':256}
        spec['limits']['spacing']=3
        spec['pressure_profile']['samples']=[{'at':0,'value':pressure[0]},{'at':1,'value':pressure[1]}]
        if route=='planned':return self.call('paint_stroke',spec=spec)
        if geometry['kind']=='polyline':points=geometry['points']
        else:
            # Same desired cubic; the direct baseline uses four coarse vertices.
            points=[]
            for segment in geometry['segments']:
                for t in (0,1/3,2/3,1):
                    point=[sum(segment[k][c]*((1-t)**3,3*(1-t)**2*t,3*(1-t)*t*t,t**3)[k] for k in range(4)) for c in (0,1)]
                    if not points or point!=points[-1]:points.append(point)
        samples=[{'x':p[0],'y':p[1],'pressure':pressure[0]+(pressure[1]-pressure[0])*i/(len(points)-1),
                  'timestamp_ms':500*i/(len(points)-1)} for i,p in enumerate(points)]
        return self.call('draw_brush',document=self.doc,layer=self.layer,preset=preset,size=size,color=color,
                         opacity=opacity,erase=erase,mode='continuous',points=samples)

    def save_verify(self,folder,name,expected):
        tree=self.call('inspect_document',document=self.doc)['layers']
        snapshots={}
        for n in tree:
            if n['type']=='paintlayer':
                self.call('get_image',document=self.doc,layer='uuid:'+n['uuid'],max_size=0)
                snapshots[n['uuid']]=Image.open(io.BytesIO(base64.b64decode(_image_of(self.blocks)['data']))).convert('RGBA').tobytes()
        self.call('save_document',document=self.doc,path=str(folder/(name+'.kra')))
        self.call('export_document',document=self.doc,path=str(folder/(name+'.png')))
        reopened=self.call('open_document',path=str(folder/(name+'.kra')))['session_id']
        old=self.doc;self.doc=reopened
        actual=self.image(folder/(name+'-reopened.png'))
        # Native compositing caches may retain different RGB beneath alpha 0.
        # Raw paint-layer persistence below remains exact in ALL channels.
        assert visible_bytes(actual)==visible_bytes(expected),'Visible KRA composite differs'
        assert visible_bytes(Image.open(folder/(name+'.png')))==visible_bytes(expected),'Visible export differs'
        for layer,raw in snapshots.items():
            self.call('get_image',document=self.doc,layer='uuid:'+layer,max_size=0)
            image=Image.open(io.BytesIO(base64.b64decode(_image_of(self.blocks)['data']))).convert('RGBA')
            assert image.tobytes()==raw,'Raw KRA layer differs: '+layer
        (folder/(name+'-persistence.json')).write_text(json.dumps({'raw_layers_exact_rgba':list(snapshots),
            'visible_composite_exact':True,'alpha_exact':True,
            'invisible_composite_rgb_changed_pixels':sum(a!=b for a,b in zip(expected.get_flattened_data(),actual.get_flattened_data()))},indent=2))
        self.call('close_document',document=reopened,discard_changes=True);self.doc=old

    def repair(self,region,allowed,defect,intended,edit,target_mask=None,scope=None):
        args=dict(target=self.target(scope),region=region,allowed_mask=encoded(allowed),defect=defect,intended_change=intended)
        if target_mask is not None:args['target_mask']=encoded(target_mask)
        token=self.call('prepare_repair',**args)['transaction']
        result=self.call('render_repair',transaction=token,edit=edit)
        # One selected image block; JSON holds diagnostics and view selectors.
        self.call('inspect_repair',transaction=token,candidate=result['candidate'],image='panel')
        return token,result,base64.b64decode(_image_of(self.blocks)['data'])

    def benchmark(self):
        folder=self.out/'benchmark';folder.mkdir();self.create('Synthetic native contour benchmark');self.new_layer('Contour')
        geometry={'kind':'cubic','segments':[[[55,155],[65,52],[190,52],[198,155]]]}
        self.stroke(geometry,16,[83,116,97,255]);self.image(folder/'clean.png')
        self.stroke({'kind':'polyline','points':[[224,130],[238,144]]},8,[185,98,57,255])
        before=self.image(folder/'before.png');raw=self.pixels().tobytes()
        roi={'x':210,'y':118,'width':40,'height':40};allowed=Image.new('RGBA',(40,40))
        ImageDraw.Draw(allowed).rectangle((6,6,33,33),fill=(0,0,0,255));allowed.save(folder/'allowed.png')
        spec=stroke_spec(self.target(),{'kind':'polyline','points':[[224,130],[238,144]]},size=18,erase=True)
        spec['expected_bounds']={'x':0,'y':0,'width':256,'height':256}
        token,result,panel=self.repair(roi,allowed,'Synthetic isolated copper contour spur',
            'Native erase of the spur; preserve every zero-mask byte',{'kind':'stroke','arguments':{'spec':spec}},Image.new('RGBA',(40,40)))
        (folder/'preview.png').write_bytes(panel)
        assert self.pixels().tobytes()==raw
        assert result['valid'] and result['metrics']['target_iou']==1 and result['metrics']['outside_allowed_pixels']==0
        commit=self.call('commit_repair',transaction=token,candidate=result['candidate']);after=self.image(folder/'after.png')
        frozen=self.pixels().tobytes();self.call('undo',document=self.doc);assert self.pixels().tobytes()==raw
        self.call('redo',document=self.doc);assert self.pixels().tobytes()==frozen
        for y in range(256):
            for x in range(256):
                if not (roi['x']<=x<roi['x']+40 and roi['y']<=y<roi['y']+40):
                    i=(y*256+x)*4;assert raw[i:i+4]==frozen[i:i+4]
        self.save_verify(folder,'contour-repair',after)
        self.results['benchmark']={'technical_status':'pass','visual_review_by_agent':'inconclusive','human_art_acceptance':'pending',
            'human_motion_acceptance':'pending','synthetic':True,'candidate_count':1,'commit':commit,'metrics':result['metrics']}

    def sequence(self):
        source=self.args.sequence.resolve();folder=self.out/'sequence';folder.mkdir();inputs=folder/'input';inputs.mkdir()
        hashes={str(p):sha(p) for p in sorted(source.rglob('*')) if p.is_file()}
        timing=json.loads((source/'frames/timing.json').read_text())
        registration=json.loads((source.parent/'source/frame-registration.json').read_text())
        (folder/'source-timing.json').write_text(json.dumps(timing,indent=2))
        (folder/'source-registration.json').write_text(json.dumps(registration,indent=2))
        paths=[]
        for i in range(8):
            path=inputs/('attack_%02d.png'%i);shutil.copy2(source/'frames'/path.name,path);paths.append(path)
        result=self.call('import_animation',name='Experimental registered sequence — private copy',
            frames=[{'time':i,'path':str(p)} for i,p in enumerate(paths)],fps=12,anchor={'x':384,'y':440},end=7)
        self.doc=result['document']['session_id'];self.layer=result['target']['layer']
        original=[self.pixels(time=i).tobytes() for i in range(8)]
        for i,path in enumerate(paths):assert original[i]==Image.open(path).convert('RGBA').tobytes()
        self.call('export_animation',document=self.doc,directory=str(folder/'before-loop'),start=0,end=7,anchor={'x':384,'y':440})
        before=self.image(folder/'before.png',time=5);self.call('save_document',document=self.doc,path=str(folder/'baseline.kra'))
        roi={'x':176,'y':204,'width':64,'height':40};box=(176,204,240,244);crop=before.crop(box);crop.save(folder/'before-crop.png')
        allowed=Image.new('RGBA',crop.size);weights=[]
        for y in range(40):
            for x in range(64):
                r,g,b,a=crop.getpixel((x,y));edge=min(x,y,63-x,39-y)
                weight=min(255,max(0,edge)*43) if a>128 and r>g*1.65 and r>b*1.6 else 0
                allowed.putpixel((x,y),(0,0,0,weight));weights.append(weight)
        assert sum(v>0 for v in weights)>100
        allowed.save(folder/'allowed.png');scope={'kind':'frame','time':5,'exposure_policy':'one_sample'}
        token=self.call('prepare_repair',target=self.target(scope),region=roi,allowed_mask=encoded(allowed),
            defect='Small bright red cape fold in follow-through is brighter than adjacent cape masses',
            intended_change='Reduce only this fold value; preserve silhouette, alpha, folds and other samples')['transaction']
        candidates=[]
        for i,factor in enumerate((.88,.74),1):
            image=crop.copy()
            for y in range(40):
                for x in range(64):
                    r,g,b,a=crop.getpixel((x,y));image.putpixel((x,y),(round(r*factor),round(g*factor),round(b*factor),a))
            candidate=self.call('render_repair',transaction=token,edit={'kind':'replacement','profile':PROFILE,'data':encoded(image)})
            self.call('inspect_repair',transaction=token,candidate=candidate['candidate'],image='panel')
            (folder/('candidate-%d-panel.png'%i)).write_bytes(base64.b64decode(_image_of(self.blocks)['data']))
            candidates.append(candidate)
        (folder/'intent.json').write_text(json.dumps({'defect':'cape fold value mismatch','region':roi,'source_hashes':hashes,
            'scope':scope,'candidates':sanitize(candidates),'registration':'768x512 padded; ground anchor 384,440; unchanged'},indent=2))
        choice=self.checkpoint('sequence',[0,1,2],0);commit=None
        if choice:
            candidate=candidates[choice-1]
            assert candidate['metrics']['outside_allowed_pixels']==0
            commit=self.call('commit_repair',transaction=token,candidate=candidate['candidate'])
        else:self.call('discard_repair',transaction=token)
        after=self.image(folder/'after.png',time=5);after.crop(box).save(folder/'after-crop.png')
        final=[self.pixels(time=i).tobytes() for i in range(8)]
        for i in range(8):
            if i!=5:assert final[i]==original[i]
        assert [after.getpixel((x,y))[3] for y in range(512) for x in range(768)]==[before.getpixel((x,y))[3] for y in range(512) for x in range(768)]
        if choice:
            self.call('undo',document=self.doc);assert [self.pixels(time=i).tobytes() for i in range(8)]==original
            self.call('redo',document=self.doc);assert [self.pixels(time=i).tobytes() for i in range(8)]==final
        self.call('export_animation',document=self.doc,directory=str(folder/'after-loop'),start=0,end=7,anchor={'x':384,'y':440})
        self.call('animation_review',document=self.doc,layer=self.layer,time=5,region=roi)
        (folder/'prev-current-next.png').write_bytes(base64.b64decode(_image_of(self.blocks)['data']))
        self.call('save_document',document=self.doc,path=str(folder/'frame-repair.kra'))
        reopened=self.call('open_document',path=str(folder/'frame-repair.kra'))['session_id'];old=self.doc;self.doc=reopened
        assert [self.pixels(time=i).tobytes() for i in range(8)]==final
        info=self.call('animation_frame',document=self.doc,layer=self.layer);assert (info['fps'],info['end'])==(12,7)
        self.doc=old
        assert hashes=={str(p):sha(p) for p in sorted(source.rglob('*')) if p.is_file()}
        for speed,name in ((1,'timed'),(.25,'slow')):
            subprocess.run([sys.executable,str(ROOT/'examples/sequence_player.py'),str(folder/'after-loop/manifest.json'),
                '--compare',str(folder/'before-loop/manifest.json'),'--speed',str(speed),'--capture',str(folder/name),'--seconds','4'],check=True)
        self.results['sequence']={'technical_status':'pass','visual_review_by_agent':'unchanged' if not choice else 'inconclusive','human_art_acceptance':'pending',
            'human_motion_acceptance':'pending','accepted_candidate':choice,'candidate_count':2,'commit':commit,'source_hashes':hashes,
            'limitations':['Older experimental asset, no game-art acceptance','Local color transfer, not a native brush repaint','Pose extremes retained']}

    def prop(self):
        folder=self.out/'prop';folder.mkdir();shutil.copy2(self.args.reference,folder/'reference.jpg')
        brief={'subject':'worn hooded lantern','canvas':[256,256],'display_size':[64,64],
            'style':'simple painted metal and amber glass; clear cap, stout body and curved side handle; transparent padding',
            'reference':{'url':'https://www.metmuseum.org/art/collection/search/4741','title':'Lantern, ca. 1825, tin/glass',
                         'rights':'Public Domain, Met Open Access','sha256':sha(folder/'reference.jpg'),
                         'used_for':'fluted cap, stout metal volume and side handle; own amber-window design'},
            'composition':'resolved vertical prop, apex 128/48, cap shoulders 78..178/90, body 89..164/102..214, right handle 161..200/116..190',
            'values':'dark silhouette; middle-value steel; warm amber glass; few pale edge accents',
            'effort_budget':'same 36 native strokes/layers/palette per route; direct curves four coarse vertices, planned adaptive cubic samples',
            'human_interventions':0}
        (folder/'brief.json').write_text(json.dumps(brief,indent=2))
        dark=[32,35,35,255];steel=[75,80,75,255];edge=[142,149,128,255]
        warm=[171,119,54,255];light=[231,173,83,255];rust=[113,66,43,255];shade=[46,49,47,255]
        routes={};self.prop_layers={}
        line=lambda pts:{'kind':'polyline','points':pts}
        cubic=lambda pts:{'kind':'cubic','segments':[pts]}
        for route in ('direct','planned'):
            self.create('Native worn lantern '+route);start=time.monotonic();first=len(self.receipts);self.new_layer('Silhouette')
            self.stroke(line([[126,107],[126,196]]),82,dark,route)
            for y,w in ((54,7),(61,27),(69,48),(77,69),(85,90),(93,103)):
                self.stroke(line([[128-w/2,y],[128+w/2,y]]),12,dark,route)
            self.stroke(cubic([[161,116],[210,106],[211,187],[162,189]]),12,dark,route)
            self.stroke(line([[88,219],[163,219]]),13,dark,route)
            self.image(folder/(route+'-silhouette.png'))
            if route=='planned':self.checkpoint('prop-silhouette',['continue'],'continue')
            self.new_layer('Base metal')
            self.stroke(line([[119,108],[119,196]]),65,steel,route)
            for y,w in ((62,7),(70,27),(78,48),(86,71),(94,82)):
                self.stroke(line([[127-w/2,y],[127+w/2,y]]),7,steel,route)
            self.stroke(line([[91,218],[158,218]]),6,steel,route)
            self.new_layer('Glass')
            self.stroke(cubic([[115,121],[113,143],[120,172],[119,196]]),42,warm,route)
            self.stroke(cubic([[112,126],[117,146],[110,174],[118,188]]),20,light,route,pressure=(.6,.95))
            self.new_layer('Shadows')
            self.stroke(cubic([[151,110],[152,138],[158,181],[149,204]]),15,shade,route)
            self.stroke(line([[92,104],[158,104]]),8,shade,route)
            self.stroke(line([[96,206],[154,206]]),9,shade,route)
            self.stroke(cubic([[128,56],[139,67],[151,83],[155,96]]),4,shade,route)
            self.stroke(cubic([[124,60],[114,68],[102,81],[101,94]]),4,shade,route)
            self.new_layer('Edges')
            for pts,size in (([[92,111],[94,198]],3),([[147,116],[146,194]],4),([[97,112],[136,113]],3),([[97,198],[139,198]],4),([[86,94],[158,96]],3),([[91,216],[143,217]],3)):
                self.stroke(line(pts),size,edge,route,pressure=(1,.55))
            self.stroke(cubic([[170,118],[199,118],[201,174],[176,184]]),3,edge,route,pressure=(.7,.3))
            self.stroke(cubic([[125,59],[111,76],[98,86],[93,88]]),3,edge,route,pressure=(.7,.3))
            self.stroke(line([[104,129],[108,173]]),3,[249,208,135,255],route,pressure=(.6,.25),opacity=.65)
            self.new_layer('Wear')
            for pts in ([[90,201],[106,202]],[[140,208],[153,205]],[[94,100],[108,101]],[[151,88],[162,91]]):
                self.stroke(line(pts),5,rust,route,preset='g) Dry Bristles',pressure=(.7,.35),opacity=.7)
            image=self.image(folder/(route+'-full.png'));image.resize((128,128),Image.Resampling.LANCZOS).save(folder/(route+'-thumbnail.png'))
            image.resize((64,64),Image.Resampling.LANCZOS).save(folder/(route+'-game.png'))
            routes[route]={'seconds':round(time.monotonic()-start,3),'tool_calls':len(self.receipts)-first,
                'execution_seconds':round(sum(r['seconds'] for r in self.receipts[first:]),3),
                'native_strokes':sum(r['tool'] in ('draw_brush','paint_stroke') for r in self.receipts[first:]),'candidate_count':0,
                'human_interventions':0,'document':self.doc,'working_layer':self.layer}
            self.save_verify(folder,route,image)
        # Candidate on its own semantic correction layer, source layer stack retained.
        self.new_layer('Local glass repair');roi={'x':106,'y':140,'width':24,'height':48}
        allowed=Image.new('RGBA',(24,48),(0,0,0,255));allowed.save(folder/'repair-allowed.png')
        spec=stroke_spec(self.target(),cubic([[116,146],[121,152],[119,169],[115,180]]),size=4,
            color=[249,208,135,255]);spec['opacity']=.55;spec['pressure_profile']['samples'][1]['value']=.2
        spec['expected_bounds']={'x':0,'y':0,'width':256,'height':256}
        before=self.image(folder/'repair-before.png')
        token,candidate,panel=self.repair(roi,allowed,'Glass reads as a broad flat amber mass at 64 pixels',
            'Try one curved narrow reflection; preserve silhouette and existing layers',{'kind':'stroke','arguments':{'spec':spec}})
        (folder/'repair-panel.png').write_bytes(panel)
        choice=self.checkpoint('prop-repair',['retain','reflection'],'retain')
        if choice=='reflection':
            self.call('commit_repair',transaction=token,candidate=candidate['candidate']);routes['planned']['candidate_count']=1
        else:self.call('discard_repair',transaction=token)
        final=self.image(folder/'planned-final.png');final.resize((64,64),Image.Resampling.LANCZOS).save(folder/'planned-final-game.png')
        self.save_verify(folder,'lantern',final)
        self.results['prop']={'technical_status':'pass','visual_review_by_agent':'inconclusive','human_art_acceptance':'pending',
            'human_motion_acceptance':'pending','routes':routes,'candidate_count':1,'chosen_repair':choice,'brief':brief,
            'limitations':['Comparison measures stroke control; no general artistic ability claim','Textured preset pixels may vary']}

    def finish(self):
        (self.out/'tool-receipts.json').write_text(json.dumps(self.receipts,indent=2))
        (self.out/'result.json').write_text(json.dumps(self.results,indent=2))
        hashes={str(p.relative_to(self.out)):sha(p) for p in self.out.rglob('*') if p.is_file() and p.name!='SHA256SUMS.json'}
        (self.out/'SHA256SUMS.json').write_text(json.dumps(hashes,indent=2))
        self.client.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sequence',type=Path,required=True);parser.add_argument('--reference',type=Path,required=True)
    parser.add_argument('--review',action='store_true');args=parser.parse_args()
    home=Path.home()
    if not (os.environ.get('KRITA_MCP_TEST_ARTIFACTS') and home.parent.name.startswith('krita-mcp-test-')):
        parser.error('Use run_isolated_tests.py; desktop execution forbidden')
    demo=Demo(args)
    try:demo.benchmark();demo.sequence();demo.prop()
    finally:demo.finish()


if __name__=='__main__':main()
