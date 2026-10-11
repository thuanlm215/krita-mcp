"""Native history regression, through actual MCP. Run in run_isolated_tests.py."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
import uuid

from PIL import Image
from tests.test_mcp import Client, PROTOCOL, _image_of


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.client.request('initialize', {'protocolVersion': PROTOCOL,
                            'capabilities': {}, 'clientInfo': {'name': 'history', 'version': '1'}})
        self.client.notify('notifications/initialized')
        self.doc = 'history-' + uuid.uuid4().hex[:8]
        self.out = Path(os.environ['KRITA_MCP_TEST_ARTIFACTS'])
        self.receipts = []

    def tearDown(self):
        (self.out / (str(self.doc) + '.json')).write_text(json.dumps(self.receipts, indent=2))
        self.client.close()

    def call(self, tool, **args):
        if tool not in ('create_document', 'open_document', 'status', 'list_capabilities'):
            args.setdefault('document', self.doc)
        value, blocks = self.client.ok(tool, args)
        self.receipts.append({'tool': tool, 'result': value})
        return value, blocks

    def frame(self, t, label):
        self.call('animation_frame', layer='Paint', mode='scrub', time=t)
        _, blocks = self.call('get_image', layer='Paint', max_size=96)
        block = _image_of(blocks)
        data = base64.b64decode(block['data'])
        (self.out / (str(self.doc) + '-' + label + '.png')).write_bytes(data)
        return Image.open(io.BytesIO(data)).convert('RGBA').tobytes()

    def test_scrub_one_undo_one_redo(self):
        self.call('create_document', width=96, height=96, name=self.doc)
        self.call('set_layer', layer='Background', visible=False)
        self.call('create_layer', name='Paint')
        self.call('draw', layer='Paint', commands=[{'type': 'ellipse', 'x': 18,
                  'y': 18, 'w': 60, 'h': 60, 'fill': '#285078'}])
        for t in (1, 2):
            self.call('animation_frame', layer='Paint', mode='duplicate', time=t, source_time=0)
        before = [self.frame(t, 'before-' + str(t)) for t in range(3)]
        self.call('animation_frame', layer='Paint', mode='scrub', time=1)
        self.call('draw_brush', layer='Paint', preset='b) Basic-5 Size', size=8,
                  color='#C46E43', points=[{'x': 35, 'y': 48, 'pressure': 1},
                                         {'x': 61, 'y': 48, 'pressure': 1}])
        edited = [self.frame(t, 'edited-' + str(t)) for t in range(3)]
        self.assertEqual(edited[0], before[0])
        self.assertEqual(edited[2], before[2])
        self.assertNotEqual(edited[1], before[1])
        self.call('animation_frame', layer='Paint', mode='scrub', time=1)
        receipt, _ = self.call('undo')
        self.assertEqual(receipt['entries_changed'], 1)
        undone = self.frame(1, 'one-undo')
        # Fail immediately here. A second Undo is never a repair workaround.
        self.assertEqual(hashlib.sha256(undone).hexdigest(), hashlib.sha256(before[1]).hexdigest())
        self.call('redo')
        self.assertEqual(self.frame(1, 'one-redo'), edited[1])
        for t in (0, 2):
            self.assertEqual(self.frame(t, 'protected-' + str(t)), before[t])
        path = str(self.out / (self.doc + '.kra'))
        self.call('save_document', path=path)
        original, _ = self.call('inspect_document')
        original = original['index']
        reopened, _ = self.call('open_document', path=path)
        self.doc = reopened['index']
        self.assertEqual(self.frame(1, 'reopened'), edited[1])
        # The two documents share filenames and persisted node UUIDs.
        self.call('undo')
        self.assertEqual(self.frame(1, 'reopened-empty-undo'), edited[1])
        self.doc = original
        for _ in range(3):
            self.call('undo')
            self.assertEqual(self.frame(1, 'repeat-undo'), before[1])
            self.call('redo')
            self.assertEqual(self.frame(1, 'repeat-redo'), edited[1])

    def test_scrub_second_fresh_fixture(self):
        self.test_scrub_one_undo_one_redo()

    def test_scrub_third_fresh_fixture(self):
        self.test_scrub_one_undo_one_redo()

    def test_static_sentinel_lifo_and_invalid(self):
        self.call('create_document', width=96, height=96, name=self.doc)
        self.call('create_layer', name='Paint')
        self.call('draw', layer='Paint', commands=[{'type': 'fill_rect', 'x': 3,
                  'y': 3, 'w': 8, 'h': 8, 'color': 'blue'}])
        def snap(label):
            _, blocks = self.call('get_image', layer='Paint', max_size=96)
            data = base64.b64decode(_image_of(blocks)['data'])
            (self.out / (self.doc + '-' + label + '.png')).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGBA').tobytes()
        sentinel = snap('sentinel')
        args = {'layer': 'Paint', 'preset': 'b) Basic-5 Size', 'size': 8,
                'color': '#C46E43', 'points': [{'x': 35, 'y': 48}, {'x': 61, 'y': 48}]}
        self.call('draw_brush', **args)
        edited = snap('static-stroke')
        self.assertNotEqual(edited, sentinel)
        self.call('draw', layer='Paint', commands=[{'type': 'fill_rect', 'x': 80,
                  'y': 80, 'w': 8, 'h': 8, 'color': 'green'}])
        later = snap('later-edit')
        self.call('undo')
        self.assertEqual(snap('later-undo'), edited)
        self.call('undo')
        self.assertEqual(snap('brush-undo'), sentinel)
        self.call('redo')
        self.assertEqual(snap('brush-redo'), edited)
        self.call('redo')
        self.assertEqual(snap('later-redo'), later)
        invalid = self.client.call('draw_brush', dict(args, document=self.doc, size=float('nan')))
        self.assertTrue(invalid.get('isError'))
        self.assertEqual(snap('invalid-no-change'), later)


if __name__ == '__main__':
    unittest.main(verbosity=2)
