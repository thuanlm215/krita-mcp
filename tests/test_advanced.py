#!/usr/bin/env python3
"""End-to-end v1.2 tests against an isolated live Krita, through MCP stdio."""
import base64
import json
from pathlib import Path
import struct
import unittest
import zlib

from tests.stroke_spec import stroke_spec
from tests.test_mcp import _image_of, _text_of
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


    def failed(self,tool,**args):
        args.setdefault('document',self.doc)
        result=self.client.call(tool,args)
        self.assertTrue(result.get('isError'),tool)
        return _text_of(result)

    def png_file(self,name,width,height,color):
        path=Path(self.temp.name)/self.doc/name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(base64.b64decode(png(width,height,color)))
        return str(path)

    def test_radial_gradient_ellipses_have_no_hard_edge(self):
        fade=[[0,[255,0,0,255]],[1,[255,0,0,0]]]
        self.call('create_layer',name='Box');self.call('create_layer',name='Turned')
        self.call('draw',layer='Paint',commands=[{'type':'radial_gradient','cx':64,'cy':64,
                  'rx':40,'ry':20,'stops':fade}])
        alpha=lambda x,y,layer='Paint':self.pixel(x,y,layer)[3]
        self.assertGreater(alpha(64,64),245)
        self.assertLess(abs(alpha(100,64)-alpha(64,82)),8)
        self.assertLess(alpha(100,64),40);self.assertEqual(alpha(94,82),0)
        # The old box form now fills its inscribed ellipse, not a circle.
        self.call('draw',layer='Box',commands=[{'type':'radial_gradient','x':0,'y':32,
                  'w':128,'h':64,'stops':fade}])
        self.assertGreater(alpha(64,64,'Box'),245);self.assertEqual(alpha(1,33,'Box'),0)
        self.assertLess(alpha(126,64,'Box'),30);self.assertLess(alpha(64,94,'Box'),40)
        # angle turns the long axis clockwise: +45 points it down-right.
        self.call('draw',layer='Turned',commands=[{'type':'radial_gradient','cx':64,'cy':64,
                  'rx':40,'ry':20,'angle':45,'stops':fade}])
        self.assertTrue(40<alpha(85,85,'Turned')<90);self.assertEqual(alpha(85,43,'Turned'),0)
        self.assertIn('radius',self.failed('draw',layer='Paint',commands=[
            {'type':'radial_gradient','cx':10,'cy':10,'stops':fade}]))

    def test_linear_gradient_endpoints(self):
        self.call('draw',layer='Paint',commands=[{'type':'linear_gradient','x':0,'y':0,
                  'w':128,'h':128,'x1':0,'y1':20,'x2':0,'y2':100,
                  'stops':[[0,'red'],[1,'blue']]}])
        self.assertEqual(self.pixel(64,10),[255,0,0,255])
        self.assertEqual(self.pixel(64,110),[0,0,255,255])
        middle=self.pixel(5,60);self.assertEqual(middle,self.pixel(120,60))
        self.assertTrue(100<middle[0]<155)
        self.assertIn('x1, y1, x2, y2',self.failed('draw',layer='Paint',commands=[
            {'type':'linear_gradient','w':10,'h':10,'x1':0,'stops':[[0,'red'],[1,'blue']]}]))

    def test_images_and_masks_from_files(self):
        green=self.png_file('green.png',6,4,[0,255,0,255])
        self.call('draw',layer='Paint',commands=[{'type':'image','x':10,'y':10,'path':green},
                                                 {'type':'image','x':40,'y':40,'w':12,'path':green}])
        self.assertEqual(self.pixel(15,13),[0,255,0,255])
        self.assertEqual(self.pixel(16,13)[3],0);self.assertEqual(self.pixel(15,14)[3],0)
        self.assertEqual(self.pixel(51,47),[0,255,0,255])
        self.assertEqual(self.pixel(52,47)[3],0);self.assertEqual(self.pixel(51,48)[3],0)
        image=lambda **extra:[dict({'type':'image','x':0,'y':0},**extra)]
        self.assertIn('absolute',self.failed('draw',layer='Paint',commands=image(path='green.png')))
        self.assertIn('not both',self.failed('draw',layer='Paint',commands=image(
            path=green,data=png(1,1,[0,0,0,255]))))
        self.assertIn('does not exist',self.failed('draw',layer='Paint',commands=image(
            path=str(Path(self.temp.name)/'missing.png'))))

        self.fill('blue')
        r=self.call('texture_patch',layer='Paint',data_path=self.png_file('red.png',20,20,[255,0,0,255]),
                    region={'x':0,'y':0,'width':20,'height':20},
                    mask_path=self.png_file('half.png',20,20,[0,0,0,128]))
        self.assertEqual(r['undo_steps'],1);self.assertEqual(self.pixel(5,5),[128,0,127,255])
        self.call('select_content',mode='mask',data_path=self.png_file('sel.png',8,8,[0,0,0,128]),x=40,y=40)
        self.assertEqual(list(bytes.fromhex(self.fixture('selection',rect=[42,42,1,1]))),[128])
        self.call('set_selection',mode='none')
        self.call('mask_layer',layer='Paint',mode='image',
                  data_path=self.png_file('hide.png',128,128,[0,0,0,0]))
        self.assertEqual(bytes.fromhex(self.fixture('pixels',projection=True,rect=[50,50,1,1]))[3],0)

    def test_alpha_lock_and_inherit_alpha(self):
        self.draw_rect(10,10,20,20,[255,0,0,128])
        r=self.call('set_layer',layer='Paint',alpha_locked=True)
        self.assertIn('alpha_locked',r['changed']);self.assertTrue(r['layer']['alpha_locked'])
        r=self.fill('blue');self.assertTrue(r['alpha_locked'])
        painted=self.pixel(15,15);self.assertEqual(painted[3],128)
        self.assertLess(painted[0],3);self.assertGreater(painted[2],252)
        self.assertEqual(self.pixel(50,50),[0,0,0,0])
        r=self.call('draw',layer='Paint',commands=[{'type':'clear','x':0,'y':0,'w':128,'h':128}])
        self.assertEqual((r['drawn'],r['skipped_clear']),(0,1));self.assertEqual(self.pixel(15,15),painted)
        # Krita's own brush engine honours the lock as well. It may leave
        # colour beneath zero alpha, which never shows.
        self.call('draw_brush',layer='Paint',preset='b) Basic-5 Size',color='lime',size=10,
                  points=[{'x':5,'y':20},{'x':60,'y':20}])
        self.assertEqual(self.pixel(50,20)[3],0)
        brushed=self.pixel(20,20);self.assertEqual(brushed[3],128);self.assertGreater(brushed[1],200)
        self.call('create_layer',name='Clip',inherit_alpha=True)
        self.assertTrue(next(n for n in self.call('inspect_document')['layers']
                             if n['name']=='Clip')['inherit_alpha'])
        self.fill('lime',layer='Clip')
        merged=lambda x,y:bytes.fromhex(self.fixture('pixels',merged=True,rect=[x,y,1,1]))[3]
        self.assertEqual(merged(50,50),0);self.assertEqual(merged(15,15),128)

    def test_draw_brush_clamps_or_names_the_bad_point(self):
        points=[{'x':-20,'y':64},{'x':150,'y':64}]
        message=self.failed('draw_brush',layer='Paint',points=points)
        self.assertIn('points[0] (-20, 64) is outside the 128x128 canvas',message)
        self.assertIn('clamp: true',message)
        self.call('draw_brush',layer='Paint',preset='b) Basic-5 Size',color='blue',size=10,
                  clamp=True,points=points)
        self.assertGreater(self.pixel(0,64)[3],0);self.assertGreater(self.pixel(127,64)[3],0)

    def test_params_file_and_save_path(self):
        args=Path(self.temp.name)/(self.doc+'-draw.json')
        args.write_text(json.dumps({'layer':'Paint','commands':[
            {'type':'fill_rect','x':0,'y':0,'w':10,'h':10,'color':'red'}]}))
        self.call('draw',params_file=str(args))
        self.assertEqual(self.pixel(5,5),[255,0,0,255])
        out=Path(self.temp.name)/self.doc/'shots'/'view.png'
        payload,result=self.client.ok('get_image',{'document':self.doc,'save_path':str(out)})
        self.assertEqual(payload['saved_to'],str(out))
        self.assertEqual([block['type'] for block in result['content']],['text'])
        self.assertEqual(out.read_bytes()[:8],b'\x89PNG\r\n\x1a\n')

    def alpha_at(self,x,y,layer='Paint'):
        return bytes.fromhex(self.fixture('pixels',layer=layer,rect=[x,y,1,1]))[3]

    def layer_names(self):
        return [n['name'] for n in self.call('inspect_document')['layers']]

    def test_brush_strokes_share_one_undo(self):
        before=self.digest()
        r=self.call('draw_brush',layer='Paint',preset='b) Basic-5 Size',size=8,color='red',
                    strokes=[{'points':[{'x':10,'y':30},{'x':110,'y':30}]},
                             {'points':[{'x':10,'y':90},{'x':110,'y':90}],'color':'blue','size':16}])
        self.assertEqual((r['strokes'],r['undo_steps']),(2,1))
        top,bottom=self.pixel(60,30),self.pixel(60,90)
        self.assertGreater(top[0],200);self.assertLess(top[2],60)
        self.assertGreater(bottom[2],200);self.assertLess(bottom[0],60)
        raw=bytes.fromhex(self.fixture('pixels'))
        column=lambda y0,y1:sum(raw[(y*128+60)*4+3]>0 for y in range(y0,y1))
        self.assertLess(column(15,45),column(75,105))
        self.call('undo');self.assertEqual(self.digest(),before)
        line=[{'x':10,'y':10},{'x':20,'y':20}]
        self.assertIn('not both',self.failed('draw_brush',layer='Paint',points=line,strokes=[{'points':line}]))
        self.assertEqual(self.digest(),before)

    def test_ink_and_svg_commands(self):
        self.call('draw',layer='Paint',commands=[{'type':'ink','color':[0,0,0,128],
                  'points':[[10,64,2],[40,30,12],[80,100,12],[118,64,2]]}])
        alphas=bytes.fromhex(self.fixture('pixels'))[3::4]
        # Overlapping pieces fill once: translucent ink has no darker joints.
        self.assertEqual(max(alphas),128);self.assertGreater(alphas.count(128),300)
        svg=('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
             '<rect width="20" height="10" fill="#00f"/></svg>')
        path=Path(self.temp.name)/self.doc/'box.svg'
        path.parent.mkdir(exist_ok=True);path.write_text(svg)
        self.call('create_layer',name='Vector')
        self.call('draw',layer='Vector',commands=[{'type':'svg','x':4,'y':4,'svg':svg},
                                                  {'type':'svg','x':40,'y':4,'w':40,'path':str(path)}])
        self.assertEqual(self.pixel(23,13,'Vector'),[0,0,255,255])
        self.assertEqual(self.alpha_at(24,13,'Vector'),0);self.assertEqual(self.alpha_at(23,14,'Vector'),0)
        self.assertEqual(self.pixel(79,23,'Vector'),[0,0,255,255]);self.assertEqual(self.alpha_at(79,24,'Vector'),0)
        self.assertIn('could not be parsed',self.failed('draw',layer='Vector',commands=[{'type':'svg','svg':'<nope'}]))

    def test_outline_layer_positions_and_undo(self):
        self.draw_rect(40,40,40,40,'red')
        r=self.call('outline_layer',layer='Paint',width=3,color='blue')
        self.assertEqual((r['layer']['name'],r['undo_steps']),('Paint outline',1))
        names=self.layer_names();self.assertEqual(names[names.index('Paint')+1],'Paint outline')
        outline=lambda x,y,layer='Paint outline':self.pixel(x,y,layer)
        # Outside goes under the source and fills beneath it.
        self.assertEqual(outline(38,60),[0,0,255,255]);self.assertEqual(outline(60,60),[0,0,255,255])
        self.assertEqual(self.alpha_at(35,60,'Paint outline'),0)
        self.assertEqual(self.alpha_at(36,36,'Paint outline'),0)
        self.assertGreater(self.alpha_at(38,38,'Paint outline'),200)
        self.call('undo');self.assertNotIn('Paint outline',self.layer_names())
        self.call('outline_layer',layer='Paint',width=4,position='inside',color='lime',name='Inner')
        names=self.layer_names();self.assertEqual(names[names.index('Inner')+1],'Paint')
        self.assertEqual(outline(41,60,'Inner'),[0,255,0,255])
        self.assertEqual(self.alpha_at(60,60,'Inner'),0);self.assertEqual(self.alpha_at(38,60,'Inner'),0)
        self.call('outline_layer',layer='Paint',width=1,antialias=False,name='Pixel')
        self.assertEqual(self.alpha_at(39,60,'Pixel'),255);self.assertEqual(self.alpha_at(39,39,'Pixel'),0)
        self.call('create_layer',name='Empty')
        self.assertIn('no pixels',self.failed('outline_layer',layer='Empty'))

    def test_export_layers_trim_padding_and_manifest(self):
        from PIL import Image
        self.draw_rect(10,20,30,15,'red')
        self.call('create_layer',name='Top');self.draw_rect(70,70,8,9,'blue',layer='Top')
        out=Path(self.temp.name)/self.doc/'parts'
        r=self.call('export_layers',directory=str(out),padding=2)
        self.assertFalse(r['mutated'])
        entries={e['layer']:e for e in json.loads((out/'manifest.json').read_text())['layers']}
        self.assertEqual(set(entries),{'Paint','Top'})
        box=lambda e:[e[k] for k in ('x','y','width','height')]
        self.assertEqual(box(entries['Paint']),[8,18,34,19]);self.assertEqual(box(entries['Top']),[68,68,12,13])
        with Image.open(out/entries['Top']['file']) as image:
            rgba=image.convert('RGBA')
            self.assertEqual(rgba.size,(12,13))
            self.assertEqual(rgba.getpixel((2,2)),(0,0,255,255));self.assertEqual(rgba.getpixel((1,1))[3],0)
        self.assertIn('must be new',self.failed('export_layers',directory=str(out)))
        full=Path(self.temp.name)/self.doc/'full'
        self.call('export_layers',directory=str(full),layers=['Top'],trim=False)
        entry=json.loads((full/'manifest.json').read_text())['layers'][0]
        self.assertEqual(box(entry),[0,0,128,128])

    def test_file_layer_links_an_image(self):
        path=self.png_file('linked.png',16,8,[0,128,255,255])
        r=self.call('create_layer',name='Linked',type='filelayer',path=path)
        self.assertTrue(r['file']['loaded']);self.assertEqual(r['layer']['type'],'filelayer')
        self.assertEqual(self.pixel(15,7,'Linked'),[0,128,255,255])
        self.assertEqual(bytes.fromhex(self.fixture('pixels',layer='Linked',projection=True,rect=[16,7,1,1]))[3],0)
        self.assertIn('only to type filelayer',self.failed('create_layer',name='Wrong',path=path))
        self.assertIn('does not exist',self.failed('create_layer',name='Missing',type='filelayer',
                                                   path=str(Path(self.temp.name)/'missing.png')))

    def test_planned_stroke_on_a_canvas_over_one_megapixel(self):
        large=self.call('create_document',width=1400,height=1000,name='large-'+self.doc)['session_id']
        self.client.ok('create_layer',{'document':large,'name':'Paint'})
        layers=self.client.ok('inspect_document',{'document':large})[0]['layers']
        target={'document':large,'layer':'uuid:'+next(n['uuid'] for n in layers if n['name']=='Paint'),
                'scope':{'kind':'static'}}
        spec=stroke_spec(target,{'kind':'polyline','points':[[100,500],[1300,500]]},size=20)
        spec['expected_bounds']={'x':0,'y':0,'width':1400,'height':1000}
        spec['limits']['spacing']=12
        r=self.client.ok('paint_stroke',{'spec':spec})[0]
        self.assertGreater(r['changed_pixels'],20000)
        self.assertLess(r['actual_bounds']['x'],100);self.assertGreater(r['actual_bounds']['width'],1200)
        pixel=self.client.ok('get_pixel',{'document':large,'layer':'Paint','x':700,'y':500})[0]
        self.assertEqual(pixel['rgba'][3],255)

if __name__=='__main__':unittest.main(verbosity=2)
