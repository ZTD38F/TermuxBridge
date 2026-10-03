#!/usr/bin/env python3
"""Loopback-only CONNECT proxy that delegates DNS resolution to Android/Bionic."""
from __future__ import annotations

import select
import socket
import socketserver

ALLOWED = {"api.openai.com", "mtls.api.openai.com"}
MAX_HEADER = 65536


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < MAX_HEADER:
            chunk = self.request.recv(4096)
            if not chunk: return
            data += chunk
        try:
            first = data.split(b"\r\n", 1)[0].decode("ascii")
            method, target, _ = first.split(" ", 2)
            host, port_text = target.rsplit(":", 1)
            port = int(port_text)
        except Exception:
            self.request.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"); return
        if method != "CONNECT" or host not in ALLOWED or port != 443:
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"); return
        try:
            upstream = socket.create_connection((host, port), timeout=15)
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"); return
        with upstream:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.request.setblocking(False); upstream.setblocking(False)
            peers = {self.request: upstream, upstream: self.request}
            while peers:
                ready, _, failed = select.select(list(peers), [], list(peers), 60)
                if failed or not ready: return
                for src in ready:
                    try: chunk = src.recv(65536)
                    except OSError: return
                    if not chunk: return
                    try: peers[src].sendall(chunk)
                    except OSError: return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    Server(("127.0.0.1", 8877), Handler).serve_forever()
