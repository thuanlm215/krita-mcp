"""Assertions shared by offline transport and real MCP preview regressions."""
import base64
import io
import json
from PIL import Image


def assert_no_image_data(value, path='metadata'):
    if isinstance(value, dict):
        for key, item in value.items():
            assert 'base64' not in key.lower(), 'Encoded data in ' + path + '.' + key
            assert_no_image_data(item, path + '.' + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_image_data(item, path + '[' + str(index) + ']')
    elif isinstance(value, str):
        assert not value.startswith(('iVBORw0KGgo', 'data:image/')), 'Image bytes in ' + path


def preview_content(blocks):
    if isinstance(blocks, dict):
        blocks=blocks['content']
    texts = [block['text'] for block in blocks if block['type'] == 'text']
    images = [block for block in blocks if block['type'] == 'image']
    assert len(texts) == len(images) == 1, 'Exactly one metadata block and requested image'
    metadata = json.loads(texts[0])
    assert_no_image_data(metadata)
    size = len(texts[0].encode('utf-8'))
    assert size < 32 * 1024, 'Preview metadata exceeds 32 KiB: ' + str(size)
    assert images[0]['mimeType'] == 'image/png'
    data = base64.b64decode(images[0]['data'], validate=True)
    image = Image.open(io.BytesIO(data))
    assert image.format == 'PNG'
    image.load()
    return metadata, image.convert('RGBA'), size
