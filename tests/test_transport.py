"""Real HTTP regressions for mutation delivery and safe rediscovery (stdlib only)."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mcp_server import BridgeClient, BridgeError


@contextmanager
def bridge_fixture(fault=None):
    """Count requests separately from authenticated, committed edits."""
    state = {"requests": [], "edits": []}
    token = "disposable-transport-fixture"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, payload):
            raw = json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                pass  # The timeout fixture intentionally loses its client.

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(body["op"])
            if self.headers.get("X-Krita-MCP-Token") != token:
                self.reply(401, {"ok": False, "error": {"type": "unauthorized"}})
                return
            state["edits"].append(body["op"])  # Mutation has now committed.
            count = len(state["edits"])
            if count == 1:
                if fault == "timeout":
                    time.sleep(.25)
                elif fault == "reset":
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                               struct.pack("ii", 1, 0))
                    self.connection.close()
                    return
                elif fault == "truncated":
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b'{"ok":')
                    return
            self.reply(200, {"ok": True, "result": {"edits_applied": count}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    thread.start()
    info = {"host": "127.0.0.1", "port": server.server_port, "token": token}
    try:
        with tempfile.TemporaryDirectory(prefix="krita-transport-") as folder:
            discovery = Path(folder) / "bridge.json"
            discovery.write_text(json.dumps(info))
            # Deliberately ignore any desktop discovery/credentials.
            with patch.dict(os.environ, {"KRITA_MCP_INFO_FILE": str(discovery),
                                         "KRITA_MCP_HOST": "127.0.0.1",
                                         "KRITA_MCP_PORT": "", "KRITA_MCP_TOKEN": ""}):
                yield info, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class TransportDeliveryTests(unittest.TestCase):
    def assert_committed_response_loss(self, fault):
        with bridge_fixture(fault) as (info, state):
            client = BridgeClient()
            client._info = info
            with self.assertRaises(BridgeError) as caught:
                client.call("draw_brush", timeout=.06)
            self.assertEqual(caught.exception.kind, "incomplete_response")
            self.assertEqual(caught.exception.detail["operation"], "draw_brush")
            self.assertEqual(caught.exception.detail["execution_outcome"], "unknown")
            self.assertEqual(state["requests"], ["draw_brush"])
            self.assertEqual(state["edits"], ["draw_brush"])

    def test_timeout_after_commit_never_replays(self):
        self.assert_committed_response_loss("timeout")

    def test_reset_after_commit_never_replays(self):
        self.assert_committed_response_loss("reset")

    def test_truncated_response_after_commit_never_replays(self):
        self.assert_committed_response_loss("truncated")

    def test_refused_connection_rediscovers_before_sending(self):
        with bridge_fixture() as (info, state), socket.socket() as stale_socket:
            # Reserve a port without listening; no HTTP server can receive it.
            stale_socket.bind(("127.0.0.1", 0))
            client = BridgeClient()
            client._info = dict(info, port=stale_socket.getsockname()[1])
            self.assertEqual(client.call("draw_brush"), {"edits_applied": 1})
            self.assertEqual(client._info, info)
            self.assertEqual(state["requests"], ["draw_brush"])
            self.assertEqual(state["edits"], ["draw_brush"])

    def test_401_rediscovers_after_rejection_before_dispatch(self):
        with bridge_fixture() as (info, state):
            client = BridgeClient()
            client._info = dict(info, token="stale-token")
            self.assertEqual(client.call("draw_brush"), {"edits_applied": 1})
            self.assertEqual(state["requests"], ["draw_brush", "draw_brush"])
            self.assertEqual(state["edits"], ["draw_brush"])

    def test_connect_timeout_can_retry_without_sending(self):
        with bridge_fixture() as (info, state):
            # Deterministic pre-send timeout; the second attempt uses real HTTP.
            import mcp_server
            real_connection = mcp_server.http.client.HTTPConnection
            failed_connection = Mock()
            failed_connection.connect.side_effect = TimeoutError("connect timeout")
            with patch("mcp_server.http.client.HTTPConnection",
                       side_effect=[failed_connection,
                                    real_connection(info["host"], info["port"], timeout=1)]):
                client = BridgeClient()
                client._info = info
                self.assertEqual(client.call("draw_brush"), {"edits_applied": 1})
            failed_connection.request.assert_not_called()
            failed_connection.close.assert_called_once()
            self.assertEqual(state["edits"], ["draw_brush"])

    def test_send_failure_cannot_prove_non_execution_and_never_retries(self):
        for fault in (TimeoutError("send timeout"), ConnectionResetError("send reset"),
                      BrokenPipeError("partial send")):
            with self.subTest(fault=type(fault).__name__):
                connection = Mock()
                connection.request.side_effect = fault
                with patch("mcp_server.http.client.HTTPConnection", return_value=connection) as factory:
                    client = BridgeClient()
                    client._info = {"host": "127.0.0.1", "port": 1, "token": "unused"}
                    with self.assertRaises(BridgeError) as caught:
                        client.call("draw_brush")
                self.assertEqual(caught.exception.detail["execution_outcome"], "unknown")
                factory.assert_called_once()
                connection.request.assert_called_once()
                connection.getresponse.assert_not_called()
                connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
