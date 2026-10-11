#!/usr/bin/env python3
"""End-to-end v1.2 tests against an isolated live Krita, through MCP stdio."""
import base64
import json
from pathlib import Path
import struct
import unittest
import zlib

from tests.test_mcp import _image_of
import tests.test_workflow as workflow


def png(width,height,color):
    def chunk(kind,data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    raw=b''.join(b'\0'+bytes(color)*width for _ in range(height))
    return base64.b64encode(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',width,height,8,6,0,0,0))+
                            chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')).decode()


class AdvancedTests(unittest.TestCase):
    setUpClass=workflow.WorkflowTests.__dict__['setUpClass']
    tearDownClass=workflow.WorkflowTests.__dict__['tearDownClass']
    setUp=workflow.WorkflowTests.setUp
    call=workflow.WorkflowTests.call
    fixture=workflow.WorkflowTests.fixture
    digest=workflow.WorkflowTests.digest
    pixel=workflow.WorkflowTests.pixel
    fill=workflow.WorkflowTests.fill
    brush_state=workflow.WorkflowTests.brush_state

    def authored_tree(self):
        def clean(nodes):
            return [{k:clean(v) if k=='children' else v for k,v in n.items()
                     if k not in ('index','index_path','active')} for n in nodes if n['type']!='selectionmask']
        return clean(self.call('inspect_document')['layers'])

    def composite(self):
        return self.fixture('digest', merged=True)

    def draw_rect(self,x,y,w,h,color,layer='Paint'):
        return self.call('draw',layer=layer,commands=[{'type':'fill_rect','x':x,'y':y,'w':w,'h':h,'color':color}])

    def test_draw_native_undo_redo_and_validation(self):
        before=self.digest()
        r=self.fill('blue');painted=self.digest()
        self.assertEqual(r['undo_steps'],1)
        self.call('undo');self.assertEqual(self.digest(),before)
        self.call('redo');self.assertEqual(self.digest(),painted)
        invalid=self.client.call('draw',{'document':self.doc,'layer':'Paint','commands':[
            {'type':'fill_rect','x':0,'y':0,'w':20,'h':20,'color':'red'}, {'type':'invalid'}]})
        self.assertTrue(invalid.get('isError'));self.assertEqual(self.digest(),painted)

    def test_regional_filter_selection_and_undo(self):
        self.fill('blue');before=self.digest()
        self.call('set_selection',x=10,y=10,width=10,height=10)
        r=self.call('apply_filter',layer='Paint',filter='invert',region={'x':0,'y':0,'width':50,'height':50})
        self.assertTrue(r['undoable'])
        self.assertEqual(self.pixel(15,15),[255,255,0,255]);self.assertEqual(self.pixel(30,30),[0,0,255,255])
        self.call('undo');self.assertEqual(self.digest(),before)

    def test_whole_filter_targets_requested_document(self):
        self.fill('blue');before=self.digest()
        other=self.doc+'-other'
        self.call('create_document',name=other,width=128,height=128)
        self.call('apply_filter',layer='Paint',filter='invert')
        self.assertEqual(self.pixel(15,15),[255,255,0,255])
        self.assertEqual(self.call('get_pixel',document=other,layer='Background',x=15,y=15)['rgba'],[255,255,255,255])
        self.call('undo');self.assertEqual(self.digest(),before)

    def test_initial_blank_frame_zero_requires_replace(self):
        self.fill('red');before=self.digest()
        bad=self.client.call('animation_frame',{'document':self.doc,'layer':'Paint','mode':'blank','time':0})
        self.assertTrue(bad.get('isError'));self.assertEqual(self.digest(),before)
        self.assertFalse(self.fixture('layer_state')['animated'])

    def test_batch_native_brush_undo_redo_and_rollback(self):
        self.fill('blue');before=self.digest();state=self.brush_state()
        edits=[{'tool':'draw','arguments':{'layer':'Paint','commands':[
            {'type':'fill_rect','x':0,'y':0,'w':30,'h':30,'color':'red'}]}},
            {'tool':'draw_brush','arguments':{'layer':'Paint','preset':'b) Basic-5 Size','color':'white',
                'points':[{'x':10,'y':80,'pressure':0.2},{'x':60,'y':80,'pressure':1},{'x':110,'y':80,'pressure':0.4}]}}]
        r=self.call('edit_batch',edits=edits,label='Mixed native batch');after=self.digest()
        self.assertEqual(r['undo_steps'],1);self.assertNotEqual(before,after)
        self.call('undo');self.assertEqual(self.digest(),before)
        self.call('redo');self.assertEqual(self.digest(),after)
        failed=self.client.call('edit_batch',{'document':self.doc,'edits':edits+[
            {'tool':'transform_layer','arguments':{'layer':'Paint','mode':'apply','scale_x':0}}]})
        self.assertTrue(failed.get('isError'));self.assertEqual(self.digest(),after)
        self.assertEqual(self.brush_state(),state)
        self.call('redo');self.assertEqual(self.digest(),after)
        self.call('undo');self.assertEqual(self.digest(),before)

    def test_batch_extraction_transform_and_failure_restore_tree(self):
        self.draw_rect(30,30,40,40,'red');raw=self.digest();tree=self.authored_tree()
        edits=[{'tool':'select_polygon','arguments':{'points':[[30,30],[70,30],[70,70],[30,70]],'antialias':False}},
               {'tool':'selection_to_layer','arguments':{'layer':'Paint','name':'Part','mode':'cut'}},
               {'tool':'transform_layer','arguments':{'layer':'Part','mode':'apply','degrees':20,'pivot':{'x':50,'y':50}}}]
        r=self.call('edit_batch',edits=edits);self.assertEqual(r['undo_steps'],1)
        after=self.composite();self.call('undo')
        self.assertEqual(self.digest(),raw);self.assertEqual(self.authored_tree(),tree)
        self.assertIsNone(self.call('inspect_document')['selection'])
        self.call('redo');self.assertEqual(self.composite(),after)
        self.assertIsNone(self.call('inspect_document')['selection'])
        tree_after=self.call('inspect_document')['layers']
        bad=self.client.call('edit_batch',{'document':self.doc,'edits':[
            {'tool':'create_layer','arguments':{'name':'Temporary'}},
            {'tool':'draw','arguments':{'layer':'Temporary','commands':[{'type':'invalid'}]}}]})
        self.assertTrue(bad.get('isError'));self.assertEqual(self.call('inspect_document')['layers'],tree_after)

    def test_alpha_color_contiguous_and_imported_mask(self):
        self.draw_rect(10,10,20,20,[255,0,0,128]);self.draw_rect(80,80,20,20,[255,0,0,128])
        self.call('select_content',layer='Paint',mode='alpha')
        values=list(bytes.fromhex(self.fixture('selection',rect=[15,15,1,1])))
        self.assertEqual(values,[128])
        self.call('select_content',layer='Paint',mode='color',color=[255,0,0,128])
        self.assertEqual(list(bytes.fromhex(self.fixture('selection',rect=[85,85,1,1]))),[255])
        self.call('select_content',layer='Paint',mode='contiguous',x=15,y=15)
        self.assertEqual([bytes.fromhex(self.fixture('selection',rect=[x,y,1,1]))[0] for x,y in [(15,15),(85,85)]],[255,0])
        self.call('select_content',mode='mask',data=png(8,8,[0,0,0,128]),x=40,y=40)
        self.assertEqual(list(bytes.fromhex(self.fixture('selection',rect=[42,42,1,1]))),[128])
        self.call('undo')
        self.assertEqual(list(bytes.fromhex(self.fixture('selection',rect=[15,15,1,1]))),[255])

    def test_texture_patch_mask_selection_and_undo(self):
        self.fill('blue');before=self.digest()
        self.call('set_selection',x=10,y=10,width=10,height=10)
        r=self.call('texture_patch',layer='Paint',data=png(20,20,[255,0,0,255]),
                    region={'x':0,'y':0,'width':20,'height':20},mask_data=png(20,20,[0,0,0,128]))
        self.assertEqual(r['undo_steps'],1)
        self.assertEqual(self.pixel(15,15),[128,0,127,255]);self.assertEqual(self.pixel(5,5),[0,0,255,255])
        self.call('undo');self.assertEqual(self.digest(),before)

    def test_clone_fixed_offset_softness_and_undo(self):
        self.draw_rect(10,10,30,30,'red');self.draw_rect(40,10,30,30,'blue');before=self.digest()
        self.call('clone_stamp',layer='Paint',source={'x':25,'y':25},size=10,softness=0,
                  points=[{'x':25,'y':85},{'x':55,'y':85}])
        self.assertEqual(self.pixel(25,85),[255,0,0,255]);self.assertEqual(self.pixel(55,85),[0,0,255,255])
        self.call('undo');self.assertEqual(self.digest(),before)
        self.call('clone_stamp',layer='Paint',source={'x':25,'y':25},size=20,softness=1,
                  points=[{'x':85,'y':85,'pressure':0.5}])
        self.assertTrue(50<self.pixel(85,85)[3]<140)
        self.assertTrue(0<self.pixel(92,85)[3]<self.pixel(85,85)[3])

    def test_transparency_mask_edit_bake_and_undo(self):
        self.fill('red');raw=self.digest();before=self.composite()
        self.call('set_selection',x=10,y=10,width=20,height=20)
        r=self.call('mask_layer',layer='Paint',mode='selection',name='Mask')
        masked=self.composite();self.assertNotEqual(masked,before);self.assertEqual(self.digest(),raw)
        self.assertEqual([bytes.fromhex(self.fixture('pixels',projection=True,rect=[x,y,1,1]))[3] for x,y in [(15,15),(50,50)]],[255,0])
        self.call('mask_layer',layer='Paint',mask='Paint/Mask',mode='fill',value=128)
        filled=self.composite();self.call('undo');self.assertEqual(self.composite(),masked)
        self.call('redo');self.assertEqual(self.composite(),filled)
        self.call('set_selection',mode='none')
        self.call('bake_layer',layer='Paint',name='Baked')
        self.assertEqual(self.composite(),filled)
        self.assertFalse(self.fixture('layer_state')['visible'])
        self.call('undo');self.assertEqual(self.composite(),filled)
        self.assertTrue(self.fixture('layer_state')['visible'])
        self.assertNotIn('Baked',[n['name'] for n in self.call('inspect_document')['layers']])

    def test_group_flip_shear_composite_preview_and_bake(self):
        self.call('create_layer',name='Group',type='grouplayer')
        self.call('create_layer',name='Inside',parent='Group')
        self.draw_rect(40,30,20,20,'red',layer='Inside');before=self.composite()
        params={'document':self.doc,'layer':'Group','pivot':{'x':50,'y':40},'scale_x':-1,
                'scale_y':1.3,'shear_x':0.2,'shear_y':0.1,'degrees':15,'strategy':'NearestNeighbor'}
        preview,blocks=self.client.ok('transform_layer',params);image=_image_of(blocks)
        self.assertEqual(preview['preview_kind'],'krita_composite');self.assertEqual(self.composite(),before)
        r=self.call('transform_layer',**dict(params,mode='apply'))
        for a,z in zip(r['matrix'],r['native_matrix']):self.assertAlmostEqual(a,z,delta=1e-5)
        actual,content=self.client.ok('get_image',{'document':self.doc,'max_size':1024})
        self.assertEqual(image['data'],_image_of(content)['data'])
        transformed=self.composite();self.call('bake_layer',layer='Group',name='Flat')
        self.assertEqual(self.composite(),transformed)
        self.call('undo');self.assertEqual(self.composite(),transformed)
        self.call('undo');self.assertEqual(self.composite(),before)

    def test_preview_before_after_difference(self):
        r=self.call('preview_changes');token=r['checkpoint']
        self.draw_rect(10,20,15,10,'red')
        compared,blocks=self.client.ok('preview_changes',{'document':self.doc,'mode':'compare','checkpoint':token})
        self.assertIsNotNone(_image_of(blocks));self.assertEqual(compared['changed_pixels'],150)
        self.assertEqual(compared['changed_bounds'],{'x':10,'y':20,'width':15,'height':10})
        self.call('undo');r=self.call('preview_changes',mode='compare',checkpoint=token)
        self.assertEqual(r['changed_pixels'],0)
        self.call('preview_changes',mode='discard',checkpoint=token)

    def test_bake_preserves_pixels_outside_canvas(self):
        self.draw_rect(10,10,20,20,'red')
        self.call('transform_layer',layer='Paint',mode='apply',dx=-40,strategy='NearestNeighbor')
        result=self.call('bake_layer',layer='Paint',name='Outside baked')
        self.assertLess(result['region']['x'],0)
        self.assertEqual(list(bytes.fromhex(self.fixture('pixels',layer='Outside baked',rect=[-25,15,1,1]))),[0,0,255,255])
        self.call('undo');self.assertTrue(self.fixture('layer_state')['visible'])

    def test_animation_keyframes_native_history_export_and_save(self):
        self.fill('red');first=self.digest()
        r=self.call('animation_frame',layer='Paint',mode='blank',time=4)
        self.assertEqual(r['keyframes'],[0,4]);self.assertEqual(self.pixel(40,40)[3],0)
        self.call('draw_brush',layer='Paint',preset='b) Basic-5 Size',color='blue',size=25,
                  points=[{'x':10,'y':60},{'x':110,'y':60}]);frame4=self.digest()
        self.call('animation_frame',layer='Paint',mode='duplicate',time=8,source_time=0)
        self.assertEqual(self.digest(),first)
        self.call('animation_frame',layer='Paint',mode='delete',time=8)
        self.call('undo');self.assertIn(8,self.call('animation_frame',layer='Paint')['keyframes'])
        self.call('animation_frame',layer='Paint',mode='settings',fps=12,start=0,end=8)
        self.call('animation_frame',layer='Paint',mode='onion',enabled=True)
        self.assertTrue(self.call('animation_frame',layer='Paint')['onion_skin'])
        self.call('undo');self.assertFalse(self.call('animation_frame',layer='Paint')['onion_skin'])
        self.call('redo');self.assertTrue(self.call('animation_frame',layer='Paint')['onion_skin'])
        self.call('animation_frame',layer='Paint',mode='scrub',time=4)
        self.assertEqual(self.digest(),frame4)
        self.assertTrue(self.call('animation_frame',layer='Paint')['onion_skin'])
        target=Path(self.temp.name)/self.doc
        r=self.call('export_animation',directory=str(target),start=0,end=8,step=4,columns=3,anchor={'x':20,'y':60})
        self.assertEqual(r['frames'],3);self.assertEqual(r['restored_time'],4)
        manifest=json.loads((target/'manifest.json').read_text())
        self.assertEqual(manifest['fps'],12);self.assertEqual(manifest['frames'][1]['x'],128)
        self.assertEqual(manifest['frames'][0]['anchor'],{'x':20,'y':60})
        self.assertEqual((target/'frame_000000.png').read_bytes(),(target/'frame_000008.png').read_bytes())
        self.assertNotEqual((target/'frame_000000.png').read_bytes(),(target/'frame_000004.png').read_bytes())
        self.assertEqual(self.digest(),frame4)
        self.assertTrue(self.client.call('export_animation',{'document':self.doc,'directory':str(target)}).get('isError'))
        path=str(Path(self.temp.name)/(self.doc+'.kra'));self.call('save_document',path=path)
        reopened=self.call('open_document',path=path);self.doc=reopened['index']
        r=self.call('animation_frame',layer='Paint');self.assertEqual(r['keyframes'],[0,4,8]);self.assertEqual(r['fps'],12)


if __name__=='__main__':unittest.main(verbosity=2)
