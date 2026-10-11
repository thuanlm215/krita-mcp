"""New painter workflow tests: actual MCP, native Krita, private documents."""
import base64
import json
import os
from pathlib import Path
import unittest

from tests.stroke_spec import stroke_spec
from tests.test_mcp import _image_of
import tests.test_workflow as workflow


class PainterTests(unittest.TestCase):
    setUpClass=workflow.WorkflowTests.__dict__['setUpClass']
    tearDownClass=workflow.WorkflowTests.__dict__['tearDownClass']
    setUp=workflow.WorkflowTests.setUp
    call=workflow.WorkflowTests.call
    fixture=workflow.WorkflowTests.fixture
    digest=workflow.WorkflowTests.digest
    pixel=workflow.WorkflowTests.pixel
    brush_state=workflow.WorkflowTests.brush_state

    def target(self):
        d=self.call('inspect_document')
        return {'document':d['session_id'],'layer':'uuid:'+next(n['uuid'] for n in d['layers'] if n['name']=='Paint'),
                'scope':{'kind':'static'}}

    def artifact(self,name,layer='Paint'):
        _,blocks=self.client.ok('get_image',{'document':self.doc,'layer':layer,'max_size':128})
        path=Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])/name
        path.write_bytes(base64.b64decode(_image_of(blocks)['data']))

    def test_p2_round_calibration(self):
        target=self.target();before=self.digest();state=self.brush_state()
        s=stroke_spec(target,size=26)
        s['pressure_profile']['samples']=[{'at':0,'value':.1},{'at':1,'value':1}]
        planned=self.call('plan_stroke',spec=s)
        self.assertEqual(self.digest(),before)
        r=self.call('paint_stroke',spec=s)
        self.assertEqual(r['undo_steps'],1)
        self.assertEqual(self.brush_state(),state)
        raw=bytes.fromhex(self.fixture('pixels'))
        widths=[sum(raw[(y*128+x)*4+3]>0 for y in range(128)) for x in (25,60,100)]
        self.assertTrue(0<widths[0]<widths[1]<widths[2],widths)
        self.artifact('p2-taper.png')
        after=self.digest();self.call('undo');self.assertEqual(self.digest(),before)
        self.call('redo');self.assertEqual(self.digest(),after)
        (Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])/'p2-round.json').write_text(json.dumps({'spec':s,'plan':planned,'receipt':r,'widths':widths},indent=2))

    def test_p2_curve_join_selection_and_erase(self):
        s=stroke_spec(self.target(),{'kind':'cubic','segments':[[[10,80],[10,10],[100,10],[110,80]]]},size=8)
        self.call('paint_stroke',spec=s)
        self.assertGreater(self.pixel(60,28)[3],0)
        self.assertEqual(self.pixel(60,75)[3],0)
        self.artifact('p2-curve.png')
        self.call('set_selection',x=40,y=0,width=35,height=128)
        before=bytes.fromhex(self.fixture('pixels'))
        self.call('paint_stroke',spec=stroke_spec(self.target(),size=24))
        after=bytes.fromhex(self.fixture('pixels'))
        for i in range(128*128):
            if not 40<=i%128<75:self.assertEqual(before[i*4:i*4+4],after[i*4:i*4+4])
        self.call('paint_stroke',spec=stroke_spec(self.target(),size=24,erase=True))
        self.assertLess(self.pixel(60,64)[3],20)
        self.call('undo');self.assertEqual(bytes.fromhex(self.fixture('pixels')),after)
        self.call('set_selection',mode='none')
        self.call('paint_stroke',spec=stroke_spec(self.target(),{'kind':'polyline','points':[[15,110],[50,95],[100,110]]},size=7))
        self.artifact('p2-join-selection-erase.png')

    def test_p2_textured_calibration(self):
        names=self.call('list_brushes',query='Dry')['presets']
        self.assertIn('g) Dry Bristles',names)
        s=stroke_spec(self.target(),size=22,stroke_id='dry-bristles-pilot')
        s['preset']='g) Dry Bristles'
        before=self.digest();state=self.brush_state()
        r=self.call('paint_stroke',spec=s)
        self.assertGreater(r['changed_pixels'],0)
        self.assertEqual(self.brush_state(),state)
        self.artifact('p2-dry-bristles.png')
        after=self.digest();self.call('undo');self.assertEqual(self.digest(),before)
        self.call('redo');self.assertEqual(self.digest(),after)
        self.assertFalse(r['pixel_exact_replay'])
        (Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])/'p2-textured.json').write_text(json.dumps(r,indent=2))

    def test_p3_readback_sampling_metrics_and_redo(self):
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':10,'y':20,'w':20,'h':10,'color':'red'}])
        _,blocks=self.client.ok('get_image',{'document':self.doc,'layer':'Paint','max_size':0})
        before=_image_of(blocks)['data']
        full=self.call('get_image',layer='Paint',max_size=0)
        crop=self.call('get_image',layer='Paint',region={'x':10,'y':20,'width':20,'height':10},max_size=8)
        self.assertEqual(crop['mapping']['offset'],{'x':10,'y':20})
        self.assertEqual(crop['returned_size'],{'width':8,'height':4})
        self.assertEqual(crop['mapping']['scale_x'],.4)
        self.assertEqual(self.call('sample_colors',layer='Paint',region={'x':10,'y':20,'width':20,'height':10})['linear_light_mean_rgb'],[255,0,0])
        self.assertTrue(self.call('sample_colors',layer='Paint',region={'x':80,'y':80,'width':10,'height':10})['empty'])
        self.call('undo')
        state=self.fixture('history')
        _,blocks=self.client.ok('get_image',{'document':self.doc,'layer':'Paint','max_size':0})
        after=_image_of(blocks)['data']
        r,content=self.client.ok('compare_images',{'before':before,'after':after,'offset':{'x':3,'y':7}})
        self.assertEqual(r['changed_pixels'],200)
        self.assertEqual(r['changed_bounds'],{'x':13,'y':27,'width':20,'height':10})
        (Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])/'p3-panels.png').write_bytes(base64.b64decode(_image_of(content)['data']))
        self.assertEqual(self.fixture('history'),state)
        self.call('redo')
        self.assertEqual(self.call('get_image',layer='Paint',max_size=0)['content_hash'],full['content_hash'])

    def test_p3_frame_onion_timeline_history_restore(self):
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':10,'y':20,'w':20,'h':10,'color':'red'}])
        self.call('animation_frame',layer='Paint',mode='blank',time=1)
        self.call('animation_frame',layer='Paint',mode='onion',enabled=True)
        state=self.fixture('history')
        empty=self.call('sample_colors',layer='Paint',time=1)
        self.assertTrue(empty['empty'])
        image=self.call('get_image',layer='Paint',time=0,max_size=0)
        self.assertFalse(image['onion_skin_included'])
        self.assertEqual(self.call('sample_colors',layer='Paint',time=0)['visible_count'],200)
        self.assertEqual(self.fixture('history'),state)
        info=self.call('animation_frame',layer='Paint')
        self.assertEqual(info['current_time'],1)
        self.assertTrue(info['onion_skin'])


if __name__=='__main__':unittest.main(verbosity=2)
