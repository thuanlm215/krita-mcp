#!/usr/bin/env python3
"""Cache pinned Krita 6.0.4 painting headers for the native bridge build."""
import concurrent.futures
import json
from pathlib import Path
import re
import urllib.request

VERSION='v6.0.4'
ROOT=Path(__file__).resolve().parent
OUT=ROOT/'build/krita_headers'
OUT.mkdir(parents=True,exist_ok=True)
(OUT/'config-memory-leak-tracker.h').write_text('#pragma once\n')
(OUT/'KoConfig.h').write_text('#pragma once\n#define WORDS_LITTLEENDIAN 1\n')
mapfile=ROOT/'build/header-map.json'
if not mapfile.exists():
 tree=json.load(urllib.request.urlopen('https://api.github.com/repos/KDE/krita/git/trees/'+VERSION+'?recursive=1'))
 mapping={}
 for entry in tree['tree']:
  path=entry['path']
  if path.startswith('libs/') and path.endswith('.h'):mapping.setdefault(Path(path).name,[]).append(path)
 mapfile.write_text(json.dumps(mapping))
mapping=json.loads(mapfile.read_text())
seen=set()
pending={'kis_figure_painting_tool_helper.h','kis_node.h','kis_image.h','Node.h',
         'kis_raster_keyframe_channel.h','kis_paint_layer.h','kis_paint_device.h',
         'kis_post_execution_undo_adapter.h','kis_image_animation_interface.h','kis_transaction.h','KisSynchronizedConnection.h',
         'commands/kis_selection_commands.h'}

def fetch(name):
 target=OUT/name;target.parent.mkdir(parents=True,exist_ok=True)
 base=Path(name).name
 if target.exists():return name,target.read_text()
 if base.endswith('_export.h'):
  macro=base[:-2].upper()
  text='#pragma once\n#include <QtCore/qglobal.h>\n#define '+macro+' Q_DECL_IMPORT\n'
 elif base in mapping:
  options=mapping[base]
  path=next((p for p in options if p.endswith('/'+name)),options[0])
  text=urllib.request.urlopen('https://raw.githubusercontent.com/KDE/krita/'+VERSION+'/'+path).read().decode()
 else:return name,''
 target.write_text(text)
 if name!=base and not (OUT/base).exists():(OUT/base).write_text(text)
 return name,text

with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
 while pending:
  batch=sorted(pending-seen);pending=set();seen.update(batch)
  for name,text in pool.map(fetch,batch):
   for inc in re.findall(r'#include\s*[<"]([^">]+)[">]',text):
    if (Path(inc).name in mapping or inc.endswith('_export.h')) and inc not in seen:pending.add(inc)
print('Cached',len(seen),'Krita header dependencies in',OUT)
