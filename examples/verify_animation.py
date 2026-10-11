#!/usr/bin/env python3
"""Reopen a saved KRA and verify its export in a private MCP/Krita instance.

python scripts/run_isolated_tests.py --no-fixture-hooks --output artifacts/reexport -- \
  python examples/verify_animation.py sequence.kra previous/manifest.json

The previous PNGs remain unchanged. All atlas/PNG RGBA bytes must agree;
save/reopen composite comparison ignores only RGB beneath zero alpha.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_mcp import Client, PROTOCOL


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def visible_bytes(image):
    raw = bytearray(image.convert('RGBA').tobytes())
    for i in range(0, len(raw), 4):
        if raw[i + 3] == 0:
            raw[i:i + 3] = b'\0\0\0'
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kra', type=Path)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    test_home = Path.home()
    if not (os.environ.get('KRITA_MCP_TEST_ARTIFACTS') and test_home.name == 'home'
            and test_home.parent.name.startswith('krita-mcp-test-')):
        parser.error('Use run_isolated_tests.py; desktop calls are forbidden')
    source = args.kra.resolve()
    previous_path = args.manifest.resolve()
    previous = json.loads(previous_path.read_text())
    files = [source, previous_path, previous_path.parent / previous['atlas']]
    files += [previous_path.parent / f['file'] for f in previous['frames']]
    hashes = {str(p): digest(p) for p in files}
    output = Path(os.environ['KRITA_MCP_TEST_ARTIFACTS']) / source.stem
    client = Client()
    document = None
    receipts = []

    def call(tool, **arguments):
        result, _ = client.ok(tool, arguments)
        receipts.append({'tool': tool, 'arguments': arguments, 'result': result})
        return result

    try:
        client.request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                       'clientInfo': {'name': 'saved-animation-verifier', 'version': '1'}})
        client.notify('notifications/initialized')
        document = call('open_document', path=str(source))['session_id']
        frame_info = call('animation_frame', document=document)
        assert frame_info['fps'] == previous['fps'], 'KRA FPS changed'
        assert (frame_info['start'], frame_info['end']) == (previous['start'], previous['end']), 'KRA clip changed'
        call('export_animation', document=document, directory=str(output),
             start=previous['start'], end=previous['end'], step=previous['step'],
             columns=max(1, previous['width'] // previous['frames'][0]['width']),
             anchor=previous['frames'][0]['anchor'])
        exported = json.loads((output / 'manifest.json').read_text())
        assert len(exported['frames']) == len(previous['frames'])
        atlas = Image.open(output / exported['atlas']).convert('RGBA')
        for old, new in zip(previous['frames'], exported['frames']):
            assert old == new, 'Frame registration/duration changed'
            image = Image.open(output / new['file']).convert('RGBA')
            expected = Image.open(previous_path.parent / old['file']).convert('RGBA')
            assert image.size == expected.size
            assert visible_bytes(image) == visible_bytes(expected), 'Visible frame or alpha changed'
            box = (new['x'], new['y'], new['x'] + new['width'], new['y'] + new['height'])
            assert atlas.crop(box).tobytes() == image.tobytes(), 'Atlas cell differs in stored RGBA'
        assert hashes == {str(p): digest(p) for p in files}, 'Source evidence changed'
        call('close_document', document=document, discard_changes=True)
        document = None
        report = {'technical_status': 'pass', 'human_art_acceptance': 'pending',
                  'human_motion_acceptance': 'pending', 'samples': len(exported['frames']),
                  'png_atlas_exact_rgba': True, 'reopened_visible_colors_and_alpha_exact': True,
                  'fps_timing_registration_exact': True, 'source_hashes': hashes,
                  'tool_receipts': receipts}
        (output / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({k: v for k, v in report.items() if k not in ('source_hashes', 'tool_receipts')}, indent=2))
    finally:
        if document is not None:
            call('close_document', document=document, discard_changes=True)
        client.close()


if __name__ == '__main__':
    main()
