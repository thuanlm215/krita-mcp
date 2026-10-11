"""Exact sample/exposure topology and pixels, native frame transfer via MCP."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
import uuid

from PIL import Image
import tests.test_correction as correction
from tests.test_advanced import png
from tests.test_mcp import _image_of


class FrameRepairTests(unittest.TestCase):
    setUpClass=correction.CorrectionTests.__dict__['setUpClass']
    tearDownClass=correction.CorrectionTests.__dict__['tearDownClass']
    setUp=correction.CorrectionTests.setUp
    call=correction.CorrectionTests.call
    fixture=correction.CorrectionTests.fixture
    repair=correction.CorrectionTests.repair

    def sequence(self):
        self.out=Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])
        folder=self.out/('inputs-'+uuid.uuid4().hex[:8]);folder.mkdir()
        paths=[]
        for i,color in enumerate(((210,80,40,255),(20,60,150,255))):
            path=folder/(str(i)+'.png');path.write_bytes(base64.b64decode(png(64,64,color)));paths.append(path)
        self.sources={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        result=self.repair('import_animation',name='frame-fixture-'+uuid.uuid4().hex[:8],
            frames=[{'time':t,'path':str(p)} for t,p in zip((0,10),paths)],fps=12,anchor={'x':32,'y':60},end=12)
        self.doc=result['document']['session_id'];self.source=result['target'];self.layer=self.source['layer']
        self.assertEqual(result['keyframes'],[0,10])
        self.assertEqual(result['metadata']['anchor'],{'x':32,'y':60})
        for t,color in ((0,(210,80,40,255)),(10,(20,60,150,255))):
            self.assertEqual(self.pixels(t),bytes(color)*(64*64))
        return result

    def pixels(self,t):
        _,blocks=self.client.ok('get_image',{'document':self.doc,'layer':self.layer,'time':t,'max_size':0})
        return Image.open(io.BytesIO(base64.b64decode(_image_of(blocks)['data']))).convert('RGBA').tobytes()

    def prepare(self,t,policy='one_sample'):
        target=dict(self.source,scope={'kind':'frame','time':t,'exposure_policy':policy})
        token=self.repair('prepare_repair',target=target,region={'x':20,'y':20,'width':8,'height':8},
            allowed_mask=png(8,8,[0,0,0,255]),defect='known opaque stray square',intended_change='erase only one playback sample')['transaction']
        candidate=self.repair('render_repair',transaction=token,edit={'kind':'replacement',
            'profile':'sRGB-elle-V2-srgbtrc.icc','data':png(8,8,[0,0,0,0])})['candidate']
        return token,candidate

    def keys(self):return self.call('animation_frame',layer=self.layer)['keyframes']

    def test_p5_fractional_alpha_png_atlas_exact(self):
        self.out=Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])
        folder=self.out/('alpha-inputs-'+uuid.uuid4().hex[:8]);folder.mkdir()
        paths=[]
        for i,color in enumerate(((137,83,45,119),(51,109,197,71))):
            image=Image.new('RGBA',(32,32),color);image.putpixel((0,0),(83,116,97,0))
            path=folder/(str(i)+'.png');image.save(path);paths.append(path)
        result=self.repair('import_animation',name='fractional alpha atlas',
            frames=[{'time':i,'path':str(p)} for i,p in enumerate(paths)],fps=12,anchor={'x':16,'y':30},end=1)
        self.doc=result['document']['session_id'];self.layer=result['target']['layer']
        destination=self.out/('alpha-export-'+uuid.uuid4().hex[:8])
        self.call('export_animation',directory=str(destination),start=0,end=1,columns=2,anchor={'x':16,'y':30})
        atlas=Image.open(destination/'atlas.png').convert('RGBA')
        manifest=json.loads((destination/'manifest.json').read_text())
        for frame in manifest['frames']:
            image=Image.open(destination/frame['file']).convert('RGBA')
            self.assertEqual(atlas.crop((frame['x'],frame['y'],frame['x']+32,frame['y']+32)).tobytes(),image.tobytes())
            self.assertEqual(image.getpixel((1,1))[3],119 if frame['time']==0 else 71)

    def test_p5_one_sample_all_holds_undo_redo_save_registration(self):
        self.sequence()
        self.call('create_layer',name='Unrelated')
        self.call('draw',layer='Unrelated',commands=[{'type':'fill_rect','x':2,'y':2,'w':5,'h':5,'color':'green'}])
        unrelated=self.fixture('pixels',layer='Unrelated',rect=[0,0,64,64])
        self.call('animation_frame',layer=self.layer,mode='onion',enabled=True)
        before=[self.pixels(t) for t in range(13)];state=self.fixture('history',layer=self.layer)
        t,c=self.prepare(4)
        self.assertEqual([self.pixels(t) for t in range(13)],before)
        self.assertEqual(self.fixture('history',layer=self.layer),state)
        result=self.repair('commit_repair',transaction=t,candidate=c)
        self.assertEqual(self.keys(),[0,4,5,10]);self.assertEqual(result['undo_steps'],1)
        after=[self.pixels(t) for t in range(13)]
        for i in range(13):
            if i==4:self.assertNotEqual(after[i],before[i])
            else:self.assertEqual(after[i],before[i])
        self.assertEqual(self.fixture('pixels',layer='Unrelated',rect=[0,0,64,64]),unrelated)
        self.call('undo');self.assertEqual(self.keys(),[0,10])
        self.assertEqual([self.pixels(t) for t in range(13)],before)
        self.call('redo');self.assertEqual(self.keys(),[0,4,5,10])
        self.assertEqual([self.pixels(t) for t in range(13)],after)
        _,blocks=self.client.ok('animation_review',{'document':self.doc,'layer':self.layer,'time':4,
                                                  'region':{'x':16,'y':16,'width':20,'height':20}})
        (self.out/'p5-prev-current-next.png').write_bytes(base64.b64decode(_image_of(blocks)['data']))
        directory=self.out/'p5-export'
        export=self.call('export_animation',directory=str(directory),start=0,end=12,columns=4,anchor={'x':32,'y':60})
        self.assertEqual(export['fps'],12)
        manifest=json.loads((directory/'manifest.json').read_text())
        self.assertEqual(manifest['frames'][4]['anchor'],{'x':32,'y':60})
        atlas=Image.open(directory/'atlas.png').convert('RGBA')
        other=Image.frombytes('RGBA',(64,64),bytes.fromhex(unrelated),'raw','BGRA')
        for frame in manifest['frames']:
            image=Image.open(directory/frame['file']).convert('RGBA')
            x,y=frame['x'],frame['y']
            self.assertEqual(atlas.crop((x,y,x+64,y+64)).tobytes(),image.tobytes())
            expected=Image.alpha_composite(Image.frombytes('RGBA',(64,64),after[frame['time']]),other)
            # Raw layer equality was checked above. Merged caches can vary in
            # invisible RGB; every alpha/visible channel must still agree.
            for a,z in zip(image.get_flattened_data(),expected.get_flattened_data()):
                self.assertEqual(a[3],z[3])
                if a[3]:self.assertEqual(a,z)
            self.assertAlmostEqual(frame['duration_ms'],1000/12)
        path=self.out/'p5-exact-frame.kra';self.call('save_document',path=str(path))
        reopened=self.call('open_document',path=str(path));self.doc=reopened['session_id']
        self.assertEqual([self.pixels(t) for t in range(13)],after)
        review=self.call('animation_review',layer=self.layer,time=4)
        self.assertEqual(review['registration']['anchor'],{'x':32,'y':60})
        for p,h in self.sources.items():self.assertEqual(hashlib.sha256(Path(p).read_bytes()).hexdigest(),h)
        (self.out/'p5-one-sample.json').write_text(json.dumps(result,indent=2))

    def test_p5_boundary_and_existing_restore_keys(self):
        self.boundary_cases((0,9,10))

    def test_p5_last_sample_restore_outside_clip(self):
        self.boundary_cases((12,))

    def boundary_cases(self,times):
        for requested in times:
            with self.subTest(time=requested):
                self.sequence();before=[self.pixels(t) for t in range(14)]
                t,c=self.prepare(requested);self.repair('commit_repair',transaction=t,candidate=c)
                info=self.call('animation_frame',layer=self.layer)
                self.assertEqual((info['fps'],info['start'],info['end']),(12,0,12))
                after=[self.pixels(t) for t in range(14)]
                for i in range(14):
                    if i==requested:self.assertNotEqual(after[i],before[i])
                    else:self.assertEqual(after[i],before[i])
                labels=self.fixture('history_labels')
                self.call('undo');self.assertEqual(self.keys(),[0,10],labels)
                self.assertEqual([self.pixels(t) for t in range(14)],before)

    def test_p5_existing_exposure_reports_and_preserves_neighbors(self):
        self.sequence();before=[self.pixels(t) for t in range(13)]
        t,c=self.prepare(4,'existing_keyframe');r=self.repair('commit_repair',transaction=t,candidate=c)
        self.assertEqual(r['exposure']['affected_start'],0);self.assertEqual(r['exposure']['affected_end'],9)
        self.assertEqual(self.keys(),[0,10]);after=[self.pixels(t) for t in range(13)]
        for i in range(13):
            if i<10:self.assertNotEqual(after[i],before[i])
            else:self.assertEqual(after[i],before[i])
        self.call('undo');self.assertEqual([self.pixels(t) for t in range(13)],before)
        self.call('redo');self.assertEqual([self.pixels(t) for t in range(13)],after)

    def test_p5_partial_topology_failure_preserves_redo(self):
        self.sequence();before=[self.pixels(t) for t in range(13)]
        self.call('create_layer',name='Sentinel')
        self.call('draw',layer='Sentinel',commands=[{'type':'fill_rect','x':2,'y':2,'w':5,'h':5,'color':'blue'}])
        painted=self.fixture('pixels',layer='Sentinel',rect=[0,0,64,64]);self.call('undo')
        state=self.fixture('history',layer=self.layer)
        t,c=self.prepare(4);self.fixture('fault_frame_commit')
        try:
            r=self.client.call('commit_repair',{'transaction':t,'candidate':c})
            self.assertTrue(r.get('isError'),r);self.assertIn('rolled back',str(r))
        finally:self.fixture('fault_restore')
        self.assertEqual(self.keys(),[0,10]);self.assertEqual([self.pixels(t) for t in range(13)],before)
        self.assertEqual(self.fixture('history',layer=self.layer),state)
        self.call('redo');self.assertEqual(self.fixture('pixels',layer='Sentinel',rect=[0,0,64,64]),painted)
        self.repair('discard_repair',transaction=t)

    def test_p5_stale_timeline_and_invalid_import(self):
        self.sequence();t,c=self.prepare(4)
        self.call('animation_frame',layer=self.layer,mode='blank',time=6)
        self.assertIn('stale_input',str(self.client.call('commit_repair',{'transaction':t,'candidate':c})))
        self.repair('discard_repair',transaction=t)
        before=self.keys();docs=self.call('status')['open_document_count']
        bad=self.client.call('import_animation',{'name':'invalid','fps':12,'anchor':{'x':0,'y':0},
             'frames':[{'time':0,'path':'/tmp/does-not-exist-private.png'}]})
        self.assertTrue(bad.get('isError'));self.assertEqual(self.call('status')['open_document_count'],docs)
        self.assertEqual(self.keys(),before)

    def test_p5_native_static_candidate_and_full_frame_mode(self):
        from tests.stroke_spec import stroke_spec
        self.sequence();before=[self.pixels(t) for t in range(13)]
        target=dict(self.source,scope={'kind':'frame','time':4,'exposure_policy':'one_sample'})
        token=self.repair('prepare_repair',target=target,region={'x':0,'y':0,'width':64,'height':64},
            region_mode='whole_frame',
            allowed_mask=png(64,64,[0,0,0,255]),defect='full-frame local line test',
            intended_change='native erase inside owned static candidate')['transaction']
        stroke=stroke_spec(target,{'kind':'polyline','points':[[20,32],[44,32]]},size=8,erase=True)
        stroke['expected_bounds']={'x':0,'y':0,'width':64,'height':64}
        candidate=self.repair('render_repair',transaction=token,edit={'kind':'stroke','arguments':{'spec':stroke}})
        self.assertTrue(candidate['native_receipt']['native'])
        self.assertEqual([self.pixels(t) for t in range(13)],before)
        self.repair('commit_repair',transaction=token,candidate=candidate['candidate'])
        after=[self.pixels(t) for t in range(13)]
        for i in range(13):
            if i==4:self.assertNotEqual(after[i],before[i])
            else:self.assertEqual(after[i],before[i])
        self.call('undo');self.assertEqual(self.keys(),[0,10])
        self.assertEqual([self.pixels(t) for t in range(13)],before)

    def test_p5_export_partial_failure_cleans_and_restores(self):
        self.sequence();self.call('animation_frame',layer=self.layer,mode='onion',enabled=True)
        self.call('animation_frame',layer=self.layer,mode='scrub',time=4)
        before=self.pixels(4);state=self.fixture('history',layer=self.layer)
        folder=self.out/('failed-export-'+uuid.uuid4().hex[:8])
        self.fixture('fault_export')
        try:
            result=self.client.call('export_animation',dict(document=self.doc,directory=str(folder),start=0,end=2))
            self.assertTrue(result.get('isError'));self.assertIn('Could not save frame',str(result))
        finally:self.fixture('fault_restore')
        self.assertFalse(folder.exists());self.assertFalse(list(self.out.glob('.krita-animation-*')))
        self.assertEqual(self.pixels(4),before);self.assertEqual(self.fixture('history',layer=self.layer),state)
        info=self.call('animation_frame',layer=self.layer)
        self.assertEqual(info['current_time'],4);self.assertTrue(info['onion_skin'])


if __name__=='__main__':unittest.main(verbosity=2)
