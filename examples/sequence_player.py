#!/usr/bin/env python3
"""Offline Qt loop inspector for export_animation manifests.

python examples/sequence_player.py path/manifest.json --compare before/manifest.json
python examples/sequence_player.py path/manifest.json --speed .25
--capture DIR --seconds 4 records the real Qt elapsed clock and screen frames.
This displays exported pixels; it neither paints nor controls desktop Krita.
"""
import argparse
import json
from pathlib import Path
import subprocess

from PyQt6.QtCore import QElapsedTimer,QTimer,Qt
from PyQt6.QtGui import QColor,QPainter,QPen,QPixmap
from PyQt6.QtWidgets import QApplication,QWidget


class Player(QWidget):
    def __init__(self,paths,speed,capture,seconds):
        super().__init__()
        self.sequences=[]
        for path in paths:
            data=json.loads(path.read_text())
            frames=data['frames']
            if not frames or len(frames)>128:raise ValueError('Expected 1..128 frames')
            images=[QPixmap(str(path.parent/f['file'])) for f in frames]
            if any(img.isNull() for img in images):raise ValueError('Unreadable exported frame')
            durations=[float(f['duration_ms']) for f in frames]
            if any(d<=0 for d in durations):raise ValueError('Positive frame duration required')
            self.sequences.append((path,data,images,durations))
        self.speed=speed;self.capture=capture;self.seconds=seconds;self.events=[];self.index=0
        self.resize(min(1500,len(paths)*640),560)
        self.setWindowTitle('Krita MCP exported sequence inspector — Space pauses, +/- speed')
        self.clock=QElapsedTimer();self.clock.start();self.paused=False;self.phase=0.;self.last=0.
        self.timer=QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(33)
        if capture:capture.mkdir(parents=True,exist_ok=False)

    def sample(self,durations):
        t=self.phase%sum(durations)
        for i,d in enumerate(durations):
            if t<d:return i
            t-=d
        return len(durations)-1

    def tick(self):
        now=self.clock.elapsed()/1000
        if not self.paused:self.phase+=(now-self.last)*1000*self.speed
        self.last=now;self.update()
        if self.capture:
            self.grab().save(str(self.capture/('screen_%05d.png'%self.index)))
            self.events.append({'elapsed_seconds':now,'phase_ms':self.phase,'speed':self.speed,
                'frames':[s[1]['frames'][self.sample(s[3])]['time'] for s in self.sequences]})
            self.index+=1
            if now>=self.seconds:self.timer.stop();self.close()

    def paintEvent(self,event):
        p=QPainter(self);p.fillRect(self.rect(),QColor('#272b31'))
        cell=self.width()//len(self.sequences)
        for j,(path,data,images,durations) in enumerate(self.sequences):
            i=self.sample(durations);img=images[i];available_h=self.height()-65
            scale=min((cell-20)/img.width(),available_h/img.height())
            w,h=int(img.width()*scale),int(img.height()*scale);x=j*cell+(cell-w)//2;y=50+(available_h-h)//2
            # Checkerboard reveals transparent edges without changing pixels.
            for yy in range(y,y+h,16):
                for xx in range(x,x+w,16):
                    p.fillRect(xx,yy,min(16,x+w-xx),min(16,y+h-yy),
                               QColor('#555b64' if ((xx-x)//16+(yy-y)//16)%2 else '#414750'))
            p.drawPixmap(x,y,w,h,img)
            anchor=data['frames'][i].get('anchor')
            if anchor:
                ax=x+anchor['x']*scale;ay=y+anchor['y']*scale
                p.setPen(QPen(QColor('#70f5df'),1))
                p.drawLine(int(ax-7),int(ay),int(ax+7),int(ay));p.drawLine(int(ax),int(ay-7),int(ax),int(ay+7))
            p.setPen(QColor('#eeeeee'))
            p.drawText(j*cell+10,22,path.parent.name+'  sample '+str(data['frames'][i]['time']))
            p.drawText(j*cell+10,40,str(data['fps'])+' FPS · speed '+str(round(self.speed,2)))
        p.end()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_Space:self.paused=not self.paused
        elif event.key() in (Qt.Key.Key_Plus,Qt.Key.Key_Equal):self.speed=min(4,self.speed*2)
        elif event.key()==Qt.Key.Key_Minus:self.speed=max(.0625,self.speed/2)
        else:super().keyPressEvent(event)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest',type=Path);parser.add_argument('--compare',type=Path)
    parser.add_argument('--speed',type=float,default=1)
    parser.add_argument('--capture',type=Path);parser.add_argument('--seconds',type=float,default=4)
    args=parser.parse_args()
    if not .0625<=args.speed<=4 or not 1<=args.seconds<=30:parser.error('Bounded speed .0625..4; seconds 1..30')
    app=QApplication([])
    player=Player([p.resolve() for p in (args.manifest,args.compare) if p],args.speed,args.capture,args.seconds)
    player.show();app.exec()
    if args.capture:
        (args.capture/'clock.json').write_text(json.dumps({'events':player.events,
            'source_manifests':[str(s[0]) for s in player.sequences],'clock':'QElapsedTimer, actual Qt event loop'},indent=2))
        fps=len(player.events)/player.events[-1]['elapsed_seconds']
        subprocess.run(['ffmpeg','-y','-v','error','-framerate',str(fps),'-i',str(args.capture/'screen_%05d.png'),
            '-c:v','libx264','-pix_fmt','yuv420p',str(args.capture/'loop.mp4')],check=True)


if __name__=='__main__':main()
