"""Frozen bounded transactions through actual MCP/Krita, including native faults."""
import base64
import json
import os
from pathlib import Path
import unittest

import tests.test_painter as painter
from tests.test_advanced import png
from tests.test_mcp import _image_of
from tests.stroke_spec import stroke_spec


class CorrectionTests(unittest.TestCase):
    setUpClass=painter.PainterTests.__dict__['setUpClass']
    tearDownClass=painter.PainterTests.__dict__['tearDownClass']
    setUp=painter.PainterTests.setUp
    call=painter.PainterTests.call
    fixture=painter.PainterTests.fixture
    digest=painter.PainterTests.digest
    pixel=painter.PainterTests.pixel
    target=painter.PainterTests.target
    artifact=painter.PainterTests.artifact

    def repair(self,tool,**args):return self.client.ok(tool,args)[0]

    def test_p4_static_clone_patch_warp_candidates(self):
        self.call('draw',layer='Paint',commands=[
            {'type':'fill_rect','x':0,'y':0,'w':128,'h':128,'color':'blue'},
            {'type':'fill_rect','x':10,'y':40,'w':35,'h':30,'color':'red'}])
        before=self.digest();state=self.fixture('history');count=self.call('status')['open_document_count']
        token=self.prepare()
        edits=[('clone',{'source':{'x':20,'y':50},'points':[{'x':52,'y':50}],'size':16}),
               ('patch',{'data':png(20,20,[0,255,0,255]),'region':{'x':40,'y':40,'width':20,'height':20}}),
               ('warp',{'center':{'x':43,'y':50},'offset':{'x':3,'y':0},'radius':20})]
        for kind,args in edits:
            candidate=self.repair('render_repair',transaction=token,edit={'kind':kind,'arguments':args})
            self.assertTrue(candidate['valid']);self.assertGreater(candidate['metrics']['changed_pixels'],0)
            self.assertEqual(candidate['metrics']['outside_allowed_pixels'],0)
            self.assertEqual(self.digest(),before);self.assertEqual(self.fixture('history'),state)
            self.assertEqual(self.call('status')['open_document_count'],count)
        self.repair('discard_repair',transaction=token)

    def test_p4_expiration_and_active_budget(self):
        before=self.digest();state=self.fixture('history')
        tokens=[self.prepare() for _ in range(8)]
        result=self.client.call('prepare_repair',{'target':self.target(),
            'region':{'x':40,'y':40,'width':20,'height':20},'allowed_mask':png(20,20,[0,0,0,255]),
            'defect':'ninth token','intended_change':'budget rejection'})
        self.assertTrue(result.get('isError'));self.assertIn('resource_limit',str(result))
        self.assertEqual(self.digest(),before);self.assertEqual(self.fixture('history'),state)
        for token in tokens:self.repair('discard_repair',transaction=token)
        self.assertEqual(self.fixture('repair_resources')['bytes'],0)
        token=self.prepare();self.candidate(token)
        self.assertGreater(self.fixture('repair_resources')['bytes'],0)
        self.fixture('expire_repairs')
        result=self.client.call('repair_status',{'transaction':token})
        self.assertTrue(result.get('isError'));self.assertIn('expired',str(result))
        resources=self.fixture('repair_resources')
        self.assertEqual((resources['tokens'],resources['bytes']),(0,0))
        self.assertEqual(self.digest(),before);self.assertEqual(self.fixture('history'),state)

    def prepare(self,**changes):
        args={'target':self.target(),'region':{'x':40,'y':40,'width':20,'height':20},
              'allowed_mask':png(20,20,[0,0,0,255]),'defect':'known stray mark',
              'intended_change':'remove only the declared mark'}
        args.update(changes)
        return self.repair('prepare_repair',**args)['transaction']

    def candidate(self,t,color=(0,0,0,0)):
        return self.repair('render_repair',transaction=t,edit={'kind':'replacement','profile':'sRGB-elle-V2-srgbtrc.icc','data':png(20,20,color)})['candidate']

    def test_p4_frozen_preview_redo_commit_idempotence(self):
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':40,'y':40,'w':20,'h':20,'color':'red'}])
        before=self.digest()
        self.fixture('seed_offcanvas')
        outside=self.fixture('pixels',rect=[-4,-4,2,2])
        self.call('create_layer',name='Unrelated')
        self.call('draw',layer='Unrelated',commands=[{'type':'fill_rect','x':0,'y':0,'w':10,'h':10,'color':'green'}])
        other=self.digest('Unrelated')
        self.call('undo')
        state=self.fixture('history');tree=self.call('inspect_document')['layers']
        t=self.prepare();c=self.candidate(t)
        r,blocks=self.client.ok('inspect_repair',{'transaction':t,'candidate':c})
        self.assertEqual(r['outside_allowed_pixels'],0)
        self.assertEqual(self.digest(),before)
        self.assertEqual(self.fixture('history'),state)
        self.assertEqual(self.call('inspect_document')['layers'],tree)
        (Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])/'p4-frozen-preview.png').write_bytes(base64.b64decode(_image_of(blocks)['data']))
        result=self.repair('commit_repair',transaction=t,candidate=c)
        self.assertEqual(result['undo_steps'],1)
        after=self.digest();self.assertNotEqual(after,before)
        self.assertEqual(self.pixel(50,50)[3],0)
        self.assertEqual(self.fixture('pixels',rect=[-4,-4,2,2]),outside)
        self.call('undo');self.assertEqual(self.digest(),before)
        repeated=self.repair('commit_repair',transaction=t,candidate=c)
        self.assertTrue(repeated['repeated'])
        self.assertEqual(self.digest(),before) # Never silently Redo after Undo.
        self.assertEqual(self.repair('repair_status',transaction=t)['outcome']['operation_id'],result['operation_id'])
        self.call('redo');self.assertEqual(self.digest(),after)
        self.assertEqual(self.digest('Unrelated'),__import__('hashlib').sha256(bytes(128*128*4)).hexdigest())
        (Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])/'p4-commit.json').write_text(json.dumps(result,indent=2))

    def test_p4_partial_failure_preserves_pixels_tree_and_redo(self):
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':40,'y':40,'w':20,'h':20,'color':'red'}])
        baseline=self.digest()
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':80,'y':80,'w':10,'h':10,'color':'blue'}])
        later=self.digest();self.call('undo')
        state=self.fixture('history');tree=self.call('inspect_document')['layers']
        t=self.prepare();c=self.candidate(t)
        self.fixture('fault_commit')
        try:
            r=self.client.call('commit_repair',{'transaction':t,'candidate':c})
            self.assertTrue(r.get('isError'),r)
            self.assertIn('provisional pixels',str(r))
        finally:self.fixture('fault_restore')
        self.assertEqual(self.digest(),baseline)
        self.assertEqual(self.fixture('history'),state)
        self.assertEqual(self.call('inspect_document')['layers'],tree)
        self.call('redo');self.assertEqual(self.digest(),later)
        self.repair('discard_repair',transaction=t)

    def test_p4_stale_selection_pixels_close_and_budget(self):
        t=self.prepare();c=self.candidate(t,(255,0,0,255))
        self.call('set_selection',x=0,y=0,width=10,height=10)
        r=self.client.call('commit_repair',{'transaction':t,'candidate':c})
        self.assertTrue(r.get('isError'));self.assertIn('stale_input',str(r))
        self.repair('discard_repair',transaction=t)
        self.call('set_selection',mode='none')
        t=self.prepare()
        for color in ((255,0,0,255),(0,255,0,255),(0,0,255,255)):self.candidate(t,color)
        state=self.fixture('history');before=self.digest()
        r=self.client.call('render_repair',{'transaction':t,'edit':{'kind':'replacement','profile':'sRGB-elle-V2-srgbtrc.icc','data':png(20,20,[0,0,0,0])}})
        self.assertTrue(r.get('isError'));self.assertIn('resource_limit',str(r))
        self.assertEqual(self.fixture('history'),state);self.assertEqual(self.digest(),before)
        self.repair('discard_repair',transaction=t)
        self.assertEqual(self.repair('repair_status',transaction=t)['bytes_retained'],0)
        t=self.prepare();c=self.candidate(t,(255,0,0,255))
        self.call('draw',layer='Paint',commands=[{'type':'fill_rect','x':40,'y':40,'w':5,'h':5,'color':'white'}])
        self.assertIn('stale_input',str(self.client.call('commit_repair',{'transaction':t,'candidate':c})))
        self.call('close_document',discard_changes=True)
        self.assertIn('expired',str(self.client.call('commit_repair',{'transaction':t,'candidate':c})))

    def test_p4_native_candidate_and_soft_mask(self):
        before=self.digest();state=self.fixture('history')
        t=self.prepare(allowed_mask=png(20,20,[0,0,0,128]))
        s=stroke_spec(self.target(),{'kind':'polyline','points':[[42,50],[58,50]]},size=8)
        c=self.repair('render_repair',transaction=t,edit={'kind':'stroke','arguments':{'spec':s}})
        self.assertTrue(c['native_receipt']['native'])
        self.assertEqual(self.digest(),before);self.assertEqual(self.fixture('history'),state)
        r=self.repair('commit_repair',transaction=t,candidate=c['candidate'])
        self.assertEqual(r['metrics']['outside_allowed_pixels'],0)
        self.assertTrue(100<=self.pixel(50,50)[3]<=140)
        after=self.digest();self.artifact('p4-native-soft.png')
        self.call('undo');self.assertEqual(self.digest(),before)
        self.call('redo');self.assertEqual(self.digest(),after)

    def test_p4_holes_invisible_rgb_and_stale_brush(self):
        import io
        from PIL import Image
        self.fixture('seed_invisible')
        protected=self.fixture('pixels',rect=[49,49,1,1])
        img=Image.new('RGBA',(20,20),(0,0,0,255))
        img.putpixel((9,9),(0,0,0,0))
        buffer=io.BytesIO();img.save(buffer,format='PNG')
        t=self.prepare(allowed_mask=base64.b64encode(buffer.getvalue()).decode())
        c=self.candidate(t,(255,0,0,255))
        self.repair('commit_repair',transaction=t,candidate=c)
        self.assertEqual(self.fixture('pixels',rect=[49,49,1,1]),protected)
        self.assertEqual(self.pixel(50,50),[255,0,0,255])
        t=self.prepare();c=self.candidate(t)
        self.fixture('change_brush_size')
        self.assertIn('stale_input',str(self.client.call('commit_repair',{'transaction':t,'candidate':c})))
        self.repair('discard_repair',transaction=t)


if __name__=='__main__':unittest.main(verbosity=2)
