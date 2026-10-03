#!/usr/bin/env python3
"""Long-lived authenticated loopback router for seamless TermuxBridge runtime updates."""
from __future__ import annotations

import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time

ROOT = Path(os.environ.get("TERMUXBRIDGE_ROOT", Path.home() / "termux-mcp-bridge")).resolve()
STATE_DIR = ROOT / "state"
ROUTE_FILE = STATE_DIR / "route.json"
ROUTER_TOKEN_FILE = ROOT / "secrets" / "router_token"
BACKEND_TOKEN_FILE = ROOT / "secrets" / "backend_token"
MAX_BODY = 4_000_000
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

_inflight: dict[str, int] = {}
_guard = threading.Lock()


def _read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise RuntimeError(f"invalid local bridge secret: {path.name}")
    return value


def _read_route() -> dict:
    data = json.loads(ROUTE_FILE.read_text(encoding="utf-8"))
    generation = str(data["generation"])
    port = int(data["port"])
    if not generation or not (1024 <= port <= 65535):
        raise RuntimeError("invalid route state")
    return {"generation": generation, "port": port}


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    expected = _read_secret(ROUTER_TOKEN_FILE)
    supplied = handler.headers.get("X-Bridge-Token", "")
    return hmac.compare_digest(expected, supplied)


def _inflight_add(generation: str, delta: int) -> None:
    with _guard:
        _inflight[generation] = max(0, _inflight.get(generation, 0) + delta)


def _snapshot() -> dict:
    route = _read_route()
    with _guard:
        counts = dict(_inflight)
    return {
        "ok": True,
        "pid": os.getpid(),
        "active_generation": route["generation"],
        "active_port": route["port"],
        "inflight": counts,
        "timestamp": int(time.time()),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TermuxBridgeSupervisor/1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _auth_or_reject(self) -> bool:
        try:
            ok = _authorized(self)
        except Exception:
            ok = False
        if not ok:
            self._json(403, {"ok": False, "error": "forbidden"})
            return False
        return True

    def do_GET(self) -> None:
        # Compatibility-only health endpoint used by the legacy updater during
        # the one-time migration. It exposes no state and authorizes no MCP call.
        if self.path == "/healthz":
            self._json(200, {"ok": True})
            return
        if not self._auth_or_reject():
            return
        if self.path == "/__bridge/healthz":
            self._json(200, {"ok": True, "pid": os.getpid()})
            return
        if self.path == "/__bridge/status":
            try:
                self._json(200, _snapshot())
            except Exception as exc:
                self._json(503, {"ok": False, "error": type(exc).__name__})
            return
        self._proxy()

    def do_POST(self) -> None:
        if not self._auth_or_reject():
            return
        self._proxy()

    def do_DELETE(self) -> None:
        if not self._auth_or_reject():
            return
        self._proxy()

    def _proxy(self) -> None:
        try:
            route = _read_route()
            generation = route["generation"]
            port = route["port"]
            backend_token = _read_secret(BACKEND_TOKEN_FILE)
        except Exception as exc:
            self._json(503, {"ok": False, "error": f"route unavailable: {type(exc).__name__}"})
            return

        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid content length"})
            return
        if length < 0 or length > MAX_BODY:
            self._json(413, {"ok": False, "error": "request too large"})
            return
        body = self.rfile.read(length) if length else None

        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP or key.lower() in {"host", "x-bridge-token"}:
                continue
            headers[key] = value
        headers["Host"] = f"127.0.0.1:{port}"
        headers["X-Bridge-Backend-Token"] = backend_token

        _inflight_add(generation, 1)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3600)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            self.send_response(response.status, response.reason)
            has_length = False
            for key, value in response.getheaders():
                lower = key.lower()
                if lower in HOP_BY_HOP:
                    continue
                if lower == "content-length":
                    has_length = True
                self.send_header(key, value)
            if not has_length:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            if not self.wfile.closed:
                try:
                    self._json(502, {"ok": False, "error": "backend unavailable"})
                except Exception:
                    pass
        finally:
            conn.close()
            _inflight_add(generation, -1)


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _read_secret(ROUTER_TOKEN_FILE)
    _read_secret(BACKEND_TOKEN_FILE)
    _read_route()
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
