"""Session identities distinguish two open copies with identical persisted IDs."""
import uuid

DOCUMENTS = {}


def prune():
    from . import ops as b
    opened = b._documents()
    for key, doc in list(DOCUMENTS.items()):
        if doc not in opened:
            del DOCUMENTS[key]


def identify(doc):
    prune()
    for key, value in DOCUMENTS.items():
        if doc == value:
            return 'session:' + key
    key = uuid.uuid4().hex
    DOCUMENTS[key] = doc
    return 'session:' + key


def resolve(ref):
    from . import ops as b
    prune()
    doc = DOCUMENTS.get(ref.removeprefix('session:'))
    if doc is None:
        raise b.OpError('Document session expired or unknown', kind='expired')
    return doc
