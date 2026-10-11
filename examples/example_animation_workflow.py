#!/usr/bin/env python3
"""Create an isolated native-pressure animation fixture and export its atlas."""
import argparse
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_mcp import Client,PROTOCOL


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('artifacts/advanced/animation-demo'))
    output=parser.parse_args().output.resolve()
    if output.exists():raise SystemExit('Choose a new output directory; existing files are preserved.')
    client=Client();document='pressure-animation-'+uuid.uuid4().hex[:8]
    client.request('initialize',{'protocolVersion':PROTOCOL,'capabilities':{},
                               'clientInfo':{'name':'animation-example','version':'1'}})
    client.notify('notifications/initialized')
    def call(tool,**params):
        if tool!='create_document':params.setdefault('document',document)
        result,_=client.ok(tool,params)
        return result
    try:
        call('create_document',name=document,width=128,height=128)
        call('set_layer',layer='Background',visible=False)
        call('create_layer',name='Animated stroke')
        def stroke(y,color):
            return call('draw_brush',layer='Animated stroke',preset='b) Basic-5 Size',size=28,color=color,
                        points=[{'x':10,'y':y,'pressure':0.1,'timestamp_ms':0},
                                {'x':60,'y':y-10,'pressure':1,'timestamp_ms':80},
                                {'x':115,'y':y,'pressure':0.3,'timestamp_ms':160}])
        stroke(80,'#c95322')
        call('animation_frame',layer='Animated stroke',mode='blank',time=4)
        stroke(64,'#c95322')
        call('animation_frame',layer='Animated stroke',mode='blank',time=8)
        stroke(48,'#c95322')
        call('animation_frame',layer='Animated stroke',mode='settings',fps=12,start=0,end=11)
        call('animation_frame',layer='Animated stroke',mode='onion',enabled=True)
        result=call('export_animation',directory=str(output),start=0,end=11,step=4,columns=3,
                    anchor={'x':60,'y':64})
        call('save_document',path=str(output/'pressure-animation.kra'))
        print(result)
        print('Technical stroke-motion fixture; the KRA remains open for inspection.')
    finally:client.close()


if __name__=='__main__':main()
