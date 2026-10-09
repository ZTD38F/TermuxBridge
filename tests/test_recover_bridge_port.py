"""Integration tests: real loopback sockets and subprocesses, no Android dependency."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from runtime.recover_bridge_port import port_free, recover


HANDLER_TEMPLATE = """import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
    server_version = {server_version!r}
    def do_GET(self):
        self.send_response(403)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{{"error":"forbidden"}}')
    def log_message(self, *args):
        pass
ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2])), Handler).serve_forever()
"""


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)

    def spawn(self, name, server_version):
        port = unused_port()
        path = self.root / name
        path.write_text(HANDLER_TEMPLATE.format(server_version=server_version), encoding="utf-8")
        child = subprocess.Popen(
            [sys.executable, str(path), "--http", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.children.append(child)
        for _ in range(100):
            if child.poll() is not None:
                self.fail("test listener exited before becoming ready")
            if not port_free(port):
                return child, port
            time.sleep(0.02)
        self.fail("test listener failed to bind")

    def test_free_port_noop(self):
        self.assertEqual(recover(self.root, unused_port(), "mcp"), 0)

    def test_owned_legacy_bridge_403_is_recovered(self):
        child, port = self.spawn("bridge_server.py", "TermuxSafeBridge/1.2.0")
        self.assertEqual(recover(self.root, port, "mcp"), 0)
        child.wait(timeout=3)
        self.assertTrue(port_free(port))

    def test_unknown_process_is_not_terminated_even_with_bridge_header(self):
        child, port = self.spawn("unrelated.py", "TermuxSafeBridge/1.2.0")
        self.assertEqual(recover(self.root, port, "mcp"), 3)
        self.assertIsNone(child.poll())

    def test_unrecognized_response_is_not_terminated(self):
        child, port = self.spawn("bridge_server.py", "AnotherServer/1.0")
        self.assertEqual(recover(self.root, port, "mcp"), 3)
        self.assertIsNone(child.poll())


if __name__ == "__main__":
    unittest.main()
