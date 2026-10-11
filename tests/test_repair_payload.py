"""Real-size public MCP preview regression; run in disposable Krita only."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import random
import unittest

from PIL import Image
from tests.test_advanced import png
from tests.test_mcp import Client, PROTOCOL, _image_of
from tests.preview_payload import preview_content

ROOT=Path(__file__).resolve().parents[1]


def composite_hash(image):
    raw=bytearray(image.tobytes('raw','BGRA'))
    for i in range(0,len(raw),4):
        if not raw[i+3]:raw[i:i+3]=b'\0\0\0'
    return hashlib.sha256(raw).hexdigest()


class RepairPayloadTests(unittest.TestCase):
    def test_768x512_default_and_explicit_images(self):
        out=Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])
        source=ROOT/'artifacts/ai-painter/p6-demo-03/demos/sequence/before-loop/frame_000005.png'
        if not source.exists():
            # Remain runnable on clean checkouts without ignored artwork artifacts.
            source=out/'payload-fixture.png'
            Image.frombytes('RGB',(768,512),random.Random(20261011).randbytes(768*512*3)).save(source)
        source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
        client=Client();doc=None;token=None
        def call(tool,**args):return client.ok(tool,args,timeout=240)[0]
        def merged():
            _,blocks=client.ok('get_image',dict(document=doc,layer='merged',max_size=0),timeout=240)
            return Image.open(io.BytesIO(base64.b64decode(_image_of(blocks)['data']))).convert('RGBA')
        try:
            client.request('initialize',{'protocolVersion':PROTOCOL,'capabilities':{},
                'clientInfo':{'name':'preview-payload-regression','version':'1'}})
            client.notify('notifications/initialized')
            doc=call('open_document',path=str(source))['session_id']
            info=call('inspect_document',document=doc)
            self.assertEqual((info['width'],info['height']),(768,512))
            layer=next(n for n in info['layers'] if n['type']=='paintlayer')
            target={'document':doc,'layer':'uuid:'+layer['uuid'],'scope':{'kind':'static'}}
            baseline=merged();tree=info['layers']
            token=call('prepare_repair',target=target,region={'x':360,'y':240,'width':2,'height':2},
                allowed_mask=png(2,2,[0,0,0,255]),defect='Read-only preview response measurement',
                intended_change='Preview two-pixel test patch on an owned candidate only')['transaction']
            candidate=call('render_repair',transaction=token,edit={'kind':'replacement',
                'profile':'sRGB-elle-V2-srgbtrc.icc','data':png(2,2,[150,100,50,255])})['candidate']
            receipt={'source':str(source),'source_hash':source_hash,'canvas':[768,512],'views':[]}
            previews={}
            for view in ('composite','raw_layer'):
                for selected in ('panel','before','after'):
                    args={'transaction':token,'candidate':candidate}
                    if (view,selected)!=('composite','panel'):
                        args.update(view=view,image=selected)
                    if selected!='panel':args['max_size']=64 # Exact retrieval ignores the panel limit.
                    _,blocks=client.ok('inspect_repair',args,timeout=240)
                    metadata,image,text_bytes=preview_content(blocks)
                    self.assertEqual((metadata['view'],metadata['image']),(view,selected))
                    self.assertEqual(metadata['diagnostic_source'],'raw_layer')
                    self.assertEqual(metadata['composite']['before_hash'],composite_hash(baseline))
                    self.assertEqual(metadata['composite']['mapping']['offset'],{'x':0,'y':0})
                    self.assertEqual(metadata['raw_layer']['mapping']['offset'],{'x':360,'y':240})
                    if selected!='panel':
                        self.assertEqual(image.size,(768,512) if view=='composite' else (2,2))
                        self.assertEqual(metadata['mapping']['scale'],1)
                        self.assertEqual(metadata['panels'],[selected])
                        raw=image.tobytes('raw','BGRA')
                        measured=composite_hash(image) if view=='composite' else hashlib.sha256(raw).hexdigest()
                        self.assertEqual(metadata[view][selected+'_hash'],measured)
                    else:
                        self.assertEqual(image.width/4,metadata['mapping']['source_size'][0]*metadata['mapping']['scale'])
                    previews[(view,selected)]=image
                    image.save(out/('payload-'+view+'-'+selected+'.png'))
                    (out/('payload-'+view+'-'+selected+'-metadata.json')).write_text(json.dumps(metadata,indent=2)+'\n')
                    receipt['views'].append({'view':view,'image':selected,'text_bytes':text_bytes,
                        'image_blocks':1,'image_size':list(image.size),'image_encoded_characters':len(_image_of(blocks)['data']),
                        'text_embedded_base64_characters':0})
            self.assertEqual(previews[('composite','before')].tobytes(),baseline.tobytes())
            self.assertEqual(previews[('raw_layer','before')].tobytes(),baseline.crop((360,240,362,242)).tobytes())
            self.assertEqual(previews[('raw_layer','after')].getpixel((0,0)),(150,100,50,255))
            # Repeat selection retrieves the same frozen bytes without rendering or mutation.
            _,again=client.ok('inspect_repair',dict(transaction=token,candidate=candidate,image='after'),timeout=240)
            _,after,_=preview_content(again)
            self.assertEqual(after.tobytes(),previews[('composite','after')].tobytes())
            for invalid in ({'view':'unknown'},{'image':'unknown'}):
                response=client.call('inspect_repair',dict(transaction=token,candidate=candidate,**invalid))
                self.assertTrue(response.get('isError'))
            self.assertEqual(merged().tobytes(),baseline.tobytes())
            self.assertEqual(call('inspect_document',document=doc)['layers'],tree)
            call('discard_repair',transaction=token);token=None
            self.assertEqual(merged().tobytes(),baseline.tobytes())
            receipt['source_unchanged']=source_hash==hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertTrue(receipt['source_unchanged'])
            receipt['default_text_bytes']=receipt['views'][0]['text_bytes']
            (out/'payload-after.json').write_text(json.dumps(receipt,indent=2)+'\n')
        finally:
            try:
                if token:call('discard_repair',transaction=token)
                if doc:call('close_document',document=doc,discard_changes=True)
            finally:client.close()


if __name__=='__main__':unittest.main(verbosity=2)
