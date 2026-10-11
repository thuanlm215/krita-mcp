"""Live follow-up regressions; all artwork belongs to the isolated fixtures."""
import base64
import io
import unittest
from PIL import Image
from tests.test_advanced import png
from tests.test_mcp import _image_of
import json
import os
from pathlib import Path
import tests.test_correction
import tests.test_frame_repair
from tests.preview_payload import preview_content


class RepairFixture(unittest.TestCase):
    setUpClass=test_correction.CorrectionTests.__dict__['setUpClass']
    tearDownClass=test_correction.CorrectionTests.__dict__['tearDownClass']
    setUp=test_correction.CorrectionTests.setUp
    call=test_correction.CorrectionTests.call
    fixture=test_correction.CorrectionTests.fixture
    repair=test_correction.CorrectionTests.repair
    digest=test_correction.CorrectionTests.digest
    pixel=test_correction.CorrectionTests.pixel
    target=test_correction.CorrectionTests.target
    prepare=test_correction.CorrectionTests.prepare
    candidate=test_correction.CorrectionTests.candidate


class FrameFixture(RepairFixture):
    sequence=test_frame_repair.FrameRepairTests.sequence
    pixels=test_frame_repair.FrameRepairTests.pixels
    keys=test_frame_repair.FrameRepairTests.keys
    prepare=test_frame_repair.FrameRepairTests.prepare


class PublicationTests(RepairFixture):
    # Keep this class focused; existing suite is run independently at the gate.
    def test_post_publication_static(self):
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':40,'y':40,'w':20,'h':20,'color':'red'}])
        before=self.digest();token=self.prepare();candidate=self.candidate(token)
        self.fixture('fault_post_publication')
        try:response=self.client.call('commit_repair',{'transaction':token,'candidate':candidate})
        finally:self.fixture('fault_restore')
        after=self.digest();state=self.fixture('history')
        status=self.repair('repair_status',transaction=token)
        repeated=self.client.call('commit_repair',{'transaction':token,'candidate':candidate})
        Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'],'post-publication-static.json').write_text(json.dumps(
            dict(response=response,status=status,repeated=repeated,before=before,after=after),indent=2))
        self.assertNotEqual(before,after)
        self.assertTrue(response.get('isError'));self.assertIn('completion_failure',str(response))
        self.assertEqual(status['status'],'applied')
        self.assertTrue(status['outcome']['completion_error'])
        self.assertFalse(repeated.get('isError'),repeated)
        self.assertEqual(self.fixture('history'),state)
        self.call('undo');self.assertEqual(self.digest(),before)
        self.repair('commit_repair',transaction=token,candidate=candidate)
        self.assertEqual(self.digest(),before)
        self.call('redo');self.assertEqual(self.digest(),after)


class FramePublicationTests(FrameFixture):
    def test_post_publication_frame(self):
        self.sequence();before=[self.pixels(t) for t in range(13)]
        token,candidate=self.prepare(12)
        self.fixture('fault_post_publication')
        try:response=self.client.call('commit_repair',{'transaction':token,'candidate':candidate})
        finally:self.fixture('fault_restore')
        after=[self.pixels(t) for t in range(13)];state=self.fixture('history',layer=self.layer)
        status=self.repair('repair_status',transaction=token)
        repeated=self.client.call('commit_repair',{'transaction':token,'candidate':candidate})
        Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'],'post-publication-frame.json').write_text(json.dumps(
            dict(response=response,status=status,repeated=repeated),indent=2))
        self.assertTrue(response.get('isError'));self.assertIn('completion_failure',str(response))
        self.assertEqual(status['status'],'applied')
        self.assertTrue(status['outcome']['completion_error'])
        self.assertFalse(repeated.get('isError'),repeated)
        self.assertEqual(self.fixture('history',layer=self.layer),state)
        self.assertEqual(self.keys(),[0,10,12,13])
        self.assertEqual((self.call('animation_frame',layer=self.layer)['end']),12)
        self.call('undo');self.assertEqual(self.keys(),[0,10]);self.assertEqual([self.pixels(t) for t in range(13)],before)
        self.repair('commit_repair',transaction=token,candidate=candidate)
        self.assertEqual([self.pixels(t) for t in range(13)],before)
        self.call('redo');self.assertEqual([self.pixels(t) for t in range(13)],after)


def visible(data):
    out=bytearray(data)
    for i in range(0,len(out),4):
        if not out[i+3]:out[i:i+3]=b'\0\0\0'
    return bytes(out)


def decoded(data):return Image.open(io.BytesIO(base64.b64decode(data))).convert('RGBA')


class PreviewTests(RepairFixture):
    def merged(self,time=None):
        args=dict(document=self.doc,layer='merged',max_size=0)
        if time is not None:args['time']=time
        _,blocks=self.client.ok('get_image',args)
        return decoded(_image_of(blocks)['data'])

    def inspect(self,token,candidate,name):
        result,blocks=self.client.ok('inspect_repair',dict(transaction=token,candidate=candidate))
        metadata,panel,_=preview_content(blocks)
        self.assertEqual(metadata,result)
        out=Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])
        panel.save(out/(name+'-panel.png'))
        self.preview_images={('composite','panel'):panel}
        for view in ('raw_layer','composite'):
            for which in ('panel','before','after'):
                _,selected_blocks=self.client.ok('inspect_repair',dict(transaction=token,candidate=candidate,
                    **result[view]['images'][which]))
                selected,image,_=preview_content(selected_blocks)
                self.assertEqual((selected['view'],selected['image']),(view,which))
                self.preview_images[(view,which)]=image
                image.save(out/(name+'-'+view+'-'+which+'.png'))
            self.preview_images[(view,'panel')].save(out/(name+'-'+view+'.png'))
        for which in ('before','after'):
            self.preview_images[('composite',which)].save(out/(name+'-'+which+'.png'))
        return result

    def test_layered_occluded_blended_preview_matches_commit(self):
        self.call('set_layer',layer='Background',visible=True)
        self.call('draw',layer='Background',commands=[{'type':'fill_rect','x':0,'y':0,'w':128,'h':128,'color':[70,130,210,255]}])
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':40,'y':40,'w':20,'h':20,'color':[220,95,30,160]}])
        self.call('set_layer',layer='Paint',opacity=145,blending_mode='multiply')
        self.call('create_layer',name='Occluder')
        self.call('draw',layer='Occluder',commands=[{'type':'fill_rect','x':40,'y':40,'w':8,'h':20,'color':[225,200,155,255]}])
        source=self.digest();other=self.digest('Occluder');tree=self.call('inspect_document')['layers']
        before=self.merged();state=self.fixture('history');brush=self.fixture('brush_state')
        count=self.call('status')['open_document_count']
        token=self.prepare();candidate=self.candidate(token,(25,230,110,95))
        result=self.inspect(token,candidate,'layered-blended')
        raw_view,raw_blocks=self.client.ok('inspect_repair',dict(transaction=token,candidate=candidate,view='raw_layer'))
        self.assertEqual(raw_view['view'],'raw_layer')
        self.assertEqual(raw_view['mapping'],result['raw_layer']['mapping'])
        _,raw_image,_=preview_content(raw_blocks)
        self.assertEqual(raw_image.tobytes(),self.preview_images[('raw_layer','panel')].tobytes())
        self.assertEqual(result['composite']['mapping']['offset'],{'x':0,'y':0})
        self.assertEqual(result['raw_layer']['mapping']['offset'],{'x':40,'y':40})
        a=self.preview_images[('composite','before')];z=self.preview_images[('composite','after')]
        self.assertEqual(visible(a.tobytes()),visible(before.tobytes()))
        self.assertEqual(a.crop((40,40,48,60)).tobytes(),z.crop((40,40,48,60)).tobytes())
        self.assertNotEqual(a.crop((48,40,60,60)).tobytes(),z.crop((48,40,60,60)).tobytes())
        self.assertEqual(self.digest(),source);self.assertEqual(self.digest('Occluder'),other)
        self.assertEqual(self.fixture('history'),state);self.assertEqual(self.fixture('brush_state'),brush)
        self.assertEqual(self.call('inspect_document')['layers'],tree)
        self.assertEqual(self.call('status')['open_document_count'],count)
        self.repair('commit_repair',transaction=token,candidate=candidate)
        self.assertEqual(visible(self.merged().tobytes()),visible(z.tobytes()))
        for _ in range(3):
            self.call('undo');self.assertEqual(visible(self.merged().tobytes()),visible(a.tobytes()))
            self.call('redo');self.assertEqual(visible(self.merged().tobytes()),visible(z.tobytes()))
        self.assertEqual(self.digest('Occluder'),other)

    def test_editable_group_and_mask_contributors(self):
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':40,'y':40,'w':20,'h':20,'color':'red'}])
        self.call('create_layer',name='Group',type='grouplayer',opacity=160)
        self.call('create_layer',name='Veil',parent='Group',opacity=190,blending_mode='screen')
        self.call('draw',layer='Veil',commands=[{'type':'fill_rect','x':35,'y':35,'w':30,'h':30,'color':'blue'}])
        self.call('mask_layer',layer='Veil',mode='fill',value=128,name='Veil Alpha')
        tree=self.call('inspect_document')['layers'];source=self.digest();state=self.fixture('history')
        token=self.prepare();candidate=self.candidate(token,(20,230,90,160))
        result=self.inspect(token,candidate,'group-mask')
        self.assertEqual(self.call('inspect_document')['layers'],tree)
        self.assertEqual(self.fixture('history'),state);self.assertEqual(self.digest(),source)
        after=self.preview_images[('composite','after')]
        self.repair('commit_repair',transaction=token,candidate=candidate)
        self.assertEqual(visible(self.merged().tobytes()),visible(after.tobytes()))
        self.assertEqual(self.call('inspect_document')['layers'][0],tree[0])

    def test_transparent_preview_discard_keeps_real_redo_and_ui(self):
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':40,'y':40,'w':20,'h':20,'color':[205,60,30,130]}])
        self.fixture('seed_offcanvas');outside=self.fixture('pixels',rect=[-4,-4,2,2])
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':90,'y':90,'w':5,'h':5,'color':'blue'}])
        later=self.digest();self.call('undo');source=self.digest();state=self.fixture('history')
        token=self.prepare();candidate=self.candidate(token)
        result=self.inspect(token,candidate,'transparent')
        after=self.preview_images[('composite','after')]
        self.assertEqual(after.getpixel((50,50))[3],0)
        self.assertEqual(after.getpixel((0,0))[3],0)
        self.repair('discard_repair',transaction=token)
        self.assertEqual(self.digest(),source);self.assertEqual(self.fixture('history'),state)
        self.assertEqual(self.fixture('pixels',rect=[-4,-4,2,2]),outside)
        self.call('redo');self.assertEqual(self.digest(),later)

    def test_hidden_contributor_change_rejects_stale_composite(self):
        self.call('set_layer',layer='Background',visible=True)
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':0,'y':0,'w':128,'h':128,'color':'red'}])
        token=self.prepare();candidate=self.candidate(token)
        before=self.merged().tobytes();source=self.digest();state=self.fixture('history')
        self.fixture('seed_underlying',layer='Background')
        self.assertEqual(visible(self.merged().tobytes()),visible(before)) # Occluded, so merged-only hash is insufficient.
        response=self.client.call('commit_repair',dict(transaction=token,candidate=candidate))
        self.assertTrue(response.get('isError'));self.assertIn('composite_context',str(response))
        self.assertEqual(self.digest(),source);self.assertEqual(self.fixture('history'),state)
        self.repair('discard_repair',transaction=token)

    def test_opacity_blend_dependencies_reject_without_history_change(self):
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':40,'y':40,'w':20,'h':20,'color':'red'}])
        for changes in ({'opacity':125},{'blending_mode':'multiply'}):
            token=self.prepare();candidate=self.candidate(token)
            self.call('set_layer',layer='Paint',**changes)
            state=self.fixture('history');pixels=self.digest()
            response=self.client.call('commit_repair',dict(transaction=token,candidate=candidate))
            self.assertTrue(response.get('isError'));self.assertIn('stale_input',str(response))
            self.assertEqual(self.fixture('history'),state);self.assertEqual(self.digest(),pixels)
            self.repair('discard_repair',transaction=token)


class FramePreviewTests(FrameFixture):
    merged=PreviewTests.merged
    inspect=PreviewTests.inspect

    def test_static_target_uses_frozen_time_of_animated_contributors(self):
        self.sequence();self.call('create_layer',name='Paint')
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':20,'y':20,'w':8,'h':8,'color':'red'}])
        self.call('animation_frame',layer=self.layer,mode='scrub',time=10)
        target=RepairFixture.target(self)
        state=self.fixture('history',layer=self.layer);before=self.merged(10)
        token=self.repair('prepare_repair',target=target,region={'x':20,'y':20,'width':8,'height':8},
            allowed_mask=png(8,8,[0,0,0,255]),defect='static mark over an animated contributor',intended_change='remove static mark at declared context')['transaction']
        candidate=self.repair('render_repair',transaction=token,edit={'kind':'replacement',
            'profile':'sRGB-elle-V2-srgbtrc.icc','data':png(8,8,[0,0,0,0])})['candidate']
        result=self.inspect(token,candidate,'static-over-animation')
        self.assertEqual(result['composite']['time'],10)
        self.assertEqual(visible(self.preview_images[('composite','before')].tobytes()),visible(before.tobytes()))
        self.assertEqual(self.fixture('history',layer=self.layer),state)
        self.repair('commit_repair',transaction=token,candidate=candidate)
        self.assertEqual(visible(self.preview_images[('composite','after')].tobytes()),visible(self.merged(10).tobytes()))
        self.assertEqual(self.keys(),[0,10])

    def test_animation_full_stack_at_explicit_sample(self):
        self.sequence()
        self.call('create_layer',name='Foreground')
        self.call('draw',layer='Foreground',commands=[{'type':'fill_rect','x':20,'y':20,'w':4,'h':8,'color':[60,220,130,255]}])
        self.call('set_layer',layer=self.layer,opacity=180,blending_mode='multiply')
        self.call('animation_frame',layer=self.layer,mode='onion',enabled=True)
        self.call('animation_frame',layer=self.layer,mode='scrub',time=10)
        before=[self.pixels(t) for t in range(13)];state=self.fixture('history',layer=self.layer)
        ui=self.call('animation_frame',layer=self.layer)
        token,candidate=self.prepare(4)
        result=self.inspect(token,candidate,'animation-sample-4')
        self.assertEqual(result['composite']['time'],4)
        a=self.preview_images[('composite','before')];z=self.preview_images[('composite','after')]
        self.assertEqual(visible(a.tobytes()),visible(self.merged(4).tobytes()))
        self.assertEqual(self.fixture('history',layer=self.layer),state)
        current=self.call('animation_frame',layer=self.layer)
        self.assertEqual({k:v for k,v in current.items() if k!='elapsed_ms'},{k:v for k,v in ui.items() if k!='elapsed_ms'})
        self.assertEqual([self.pixels(t) for t in range(13)],before)
        self.repair('commit_repair',transaction=token,candidate=candidate)
        after=[self.pixels(t) for t in range(13)]
        self.assertEqual(visible(z.tobytes()),visible(self.merged(4).tobytes()))
        self.assertEqual(self.keys(),[0,4,5,10])
        for _ in range(3):
            self.call('undo');self.assertEqual([self.pixels(t) for t in range(13)],before)
            self.call('redo');self.assertEqual([self.pixels(t) for t in range(13)],after)
        for t in range(13):
            if t!=4:self.assertEqual(after[t],before[t])


class SelectionHistoryTests(RepairFixture):
    def snapshot(self):
        def clean(nodes):
            return [{k:clean(v) if k=='children' else v for k,v in n.items() if k not in ('active','index','index_path')}
                    for n in nodes]
        tree=clean(self.call('inspect_document')['layers'])
        pixels={n['uuid']:self.fixture('pixels',layer='uuid:'+n['uuid'],rect=[-4,-4,132,132]) for n in tree if n['type']=='paintlayer'}
        return dict(tree=tree,pixels=pixels,selection=self.fixture('selection'))

    def test_grouped_selection_extraction_repeated_exact_history(self):
        out=Path(os.environ['KRITA_MCP_TEST_ARTIFACTS']);receipts=[]
        selections=[('select_polygon',{'points':[[30,30],[70,30],[70,70],[30,70]],'antialias':False}),
                    ('set_selection',{'x':30,'y':30,'width':40,'height':40}),
                    ('select_content',{'mode':'mask','data':png(40,40,[0,0,0,128]),'x':30,'y':30})]
        for tool,args in selections:
            for mode in ('copy','cut'):
                for clear in (False,True):
                    with self.subTest(tool=tool,mode=mode,clear=clear):
                        self.setUp()
                        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':30,'y':30,'w':40,'h':40,'color':'red'}])
                        self.fixture('seed_offcanvas');before=self.snapshot();state=self.fixture('history')
                        edits=[{'tool':tool,'arguments':args},
                               {'tool':'selection_to_layer','arguments':{'layer':'Paint','name':'Part','mode':mode,'clear_selection':clear}},
                               {'tool':'transform_layer','arguments':{'layer':'Part','mode':'apply','degrees':20,'pivot':{'x':50,'y':50}}}]
                        result=self.call('edit_batch',edits=edits);after=self.snapshot()
                        self.assertEqual(result['undo_steps'],1)
                        history=self.fixture('history')
                        for _ in range(4):
                            self.call('undo');self.assertEqual(self.snapshot(),before)
                            self.assertEqual(self.fixture('history')[:2],[state[0],history[1]])
                            self.call('redo');self.assertEqual(self.snapshot(),after)
                            self.assertEqual(self.fixture('history')[:2],history[:2])
                        receipts.append(dict(tool=tool,mode=mode,clear=clear,before=before,after=after))
        (out/'selection-roundtrips.json').write_text(json.dumps(receipts,indent=2))

    def test_existing_selection_identity_and_failed_extraction_batch(self):
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':0,'y':0,'w':128,'h':128,'color':'red'}])
        self.call('set_selection',x=5,y=5,width=10,height=10,value=128)
        before=self.snapshot()
        edits=[{'tool':'select_polygon','arguments':{'points':[[30,30],[70,30],[70,70],[30,70]],'antialias':False}},
               {'tool':'selection_to_layer','arguments':{'layer':'Paint','name':'Part','mode':'cut'}}]
        self.call('edit_batch',edits=edits);after=self.snapshot()
        for _ in range(4):
            self.call('undo');self.assertEqual(self.snapshot(),before)
            self.call('redo');self.assertEqual(self.snapshot(),after)
        self.call('undo');self.assertEqual(self.snapshot(),before)
        response=self.client.call('edit_batch',dict(document=self.doc,edits=edits+[
            {'tool':'transform_layer','arguments':{'layer':'Part','mode':'apply','scale_x':0}}]))
        self.assertTrue(response.get('isError'),response)
        self.assertEqual(self.snapshot(),before)
