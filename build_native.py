#!/usr/bin/env python3
"""Build the Linux/Krita 6.0.4 helper using Qt6, KF6I18n and Boost headers."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parent
subprocess.run([sys.executable,str(ROOT/'native/fetch_headers.py')],check=True)
subprocess.run(['cmake','-S',str(ROOT/'native'),'-B',str(ROOT/'native/build'),
                '-DCMAKE_BUILD_TYPE=Release'],check=True)
subprocess.run(['cmake','--build',str(ROOT/'native/build'),'-j2'],check=True)
source=ROOT/'native/build/libkrita_mcp_native.so'
digest=hashlib.sha256(source.read_bytes()).hexdigest()
target=ROOT/'pykrita/krita_mcp'
name='libkrita_mcp_native_'+digest[:16]+'.so'
shutil.copy2(source,target/name)
for old in target.glob('libkrita_mcp_native*.so'):
    if old.name!=name:old.unlink()
(target/'native_manifest.json').write_text(json.dumps(
    {'file':name,'sha256':digest,'abi':'krita-6.0.4-qt6'},indent=2)+'\n')
print('Prepared',name,'for the plugin installer')
