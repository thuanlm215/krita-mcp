"""Private assertions use HTTP; production edits continue through MCP stdio."""
import os
from mcp_server import BridgeClient


def fixture(scenario, **args):
    if not os.environ.get('KRITA_MCP_TEST_ARTIFACTS'):
        raise RuntimeError('Fixed fixtures require run_isolated_tests.py')
    return BridgeClient().call('_test_fixture', dict(args, scenario=scenario))
