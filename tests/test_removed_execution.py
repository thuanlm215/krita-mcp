"""Public and direct-dispatch rejection through a normal private installation."""
import json
import subprocess
import sys
import unittest

from mcp_server import BridgeClient, BridgeError
from tests.test_mcp import Client, PROTOCOL


class RemovedExecutionTests(unittest.TestCase):
    def test_removed_routes_and_console_actions(self):
        c = Client()
        try:
            c.request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                                    'clientInfo': {'name': 'removed-routes', 'version': '1'}})
            c.notify('notifications/initialized')
            specs = c.request('tools/list')['result']['tools']
            self.assertNotIn('run_python', {s['name'] for s in specs})
            self.assertNotIn('_test_fixture', {s['name'] for s in specs})
            before = c.ok('status')[0]['open_document_count']
            for tool, args in [('run_python', {'code': 'doc.close()'}),
                               ('trigger_action', {'name': 'scripter'}),
                               ('trigger_action', {'name': 'python_scripter'}),
                               ('trigger_action', {'name': 'file_open'})]:
                self.assertTrue(c.call(tool, args).get('isError'))
            for name in ('run_python', '_test_fixture'):
                with self.assertRaises(BridgeError):
                    BridgeClient().call(name, {'code': 'doc.close()'})
            cli = subprocess.run([sys.executable, 'mcp_server.py', '--call', 'run_python',
                                  '--params', json.dumps({'code': 'doc.close()'})],
                                 capture_output=True, text=True)
            self.assertNotEqual(cli.returncode, 0)
            self.assertEqual(c.ok('status')[0]['open_document_count'], before)
        finally:
            c.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
