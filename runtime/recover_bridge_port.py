#!/usr/bin/env python3
"""Fail-closed recovery of ports held by an identifiable stale TermuxBridge process.

Called only on the deliberate full-start path, not during seamless updates.
Never touches credentials, unrelated processes, or an unrecognized listener.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import socket
import sys
import time
import urllib.error
import urllib.request


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # TIME_WAIT after an HTTP health probe must not appear as a live listener.
        # bind+listen distinguishes an active server from reusable closed sockets.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
        except OSError:
            return False
    return True


def process_identity(pid: int, root: Path, port: int, kind: str) -> tuple[str, ...] | None:
    """Return a stable identity only for a same-UID, expected managed command."""
    if pid == os.getpid():
        return None
    proc = Path("/proc") / str(pid)
    try:
        if proc.stat().st_uid != os.geteuid():
            return None
        parts = [p.decode("utf-8") for p in (proc / "cmdline").read_bytes().split(b"\0") if p]
        if len(parts) < 2 or not Path(parts[0]).name.startswith("python"):
            return None
        script = Path(parts[1]).resolve(strict=True)
        if not script.is_relative_to(root.resolve(strict=True)):
            return None
        if kind == "mcp":
            if script.name == "bridge_server.py" and parts[2:] == ["--http", str(port)]:
                pass
            elif script == root.resolve() / "supervisor.py" and len(parts) == 2:
                pass
            else:
                return None
        elif kind == "proxy":
            if script != root.resolve() / "local_https_proxy.py" or len(parts) != 2:
                return None
        else:
            return None
        # Linux /proc/PID/stat field 22 (starttime) prevents PID reuse races.
        stat = (proc / "stat").read_text(encoding="utf-8")
        starttime = stat.rsplit(")", 1)[1].split()[19]
        return (str(script), starttime, *parts)
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def candidates(root: Path, port: int, kind: str) -> list[tuple[int, tuple[str, ...]]]:
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdecimal():
            ident = process_identity(int(entry.name), root, port, kind)
            if ident:
                found.append((int(entry.name), ident))
    return found


def recognizable_listener(port: int, kind: str) -> bool:
    """Check known bridge fingerprints; a bare HTTP 403 is not sufficient."""
    if kind == "mcp":
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                return response.headers.get("Server", "").startswith(
                    ("TermuxSafeBridge/", "TermuxBridgeSupervisor/")
                )
        except urllib.error.HTTPError as exc:
            return exc.code == 403 and exc.headers.get("Server", "").startswith(
                ("TermuxSafeBridge/", "TermuxBridgeSupervisor/")
            )
        except (OSError, ValueError):
            return False
    if kind == "proxy":
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                sock.settimeout(2)
                sock.sendall(b"CONNECT blocked.invalid:443 HTTP/1.1\r\nHost: blocked.invalid:443\r\n\r\n")
                return sock.recv(128).startswith(b"HTTP/1.1 403 Forbidden\r\n")
        except OSError:
            return False
    return False


def managed_role_identity(pid: int, root: Path, role: str) -> tuple[str, ...] | None:
    """Identify the exact expected executable before honoring a pidfile."""
    if pid == os.getpid() or pid <= 1:
        return None
    proc = Path("/proc") / str(pid)
    try:
        if proc.stat().st_uid != os.geteuid():
            return None
        parts = [p.decode("utf-8") for p in (proc / "cmdline").read_bytes().split(b"\0") if p]
        if not parts:
            return None
        root = root.resolve(strict=True)
        if role == "tunnel":
            if Path(parts[0]).resolve(strict=True) != (root / "bin/tunnel-client-runtime").resolve(strict=True):
                return None
            if len(parts) < 2 or parts[1] != "run":
                return None
        else:
            if len(parts) < 2 or not Path(parts[0]).name.startswith("python"):
                return None
            script = Path(parts[1]).resolve(strict=True)
            if not script.is_relative_to(root):
                return None
            if role == "backend":
                if script.name != "bridge_server.py" or len(parts) != 4:
                    return None
                if parts[2] != "--http" or parts[3] not in {"8765", "18771", "18772"}:
                    return None
            elif role == "supervisor":
                if script != root / "supervisor.py" or len(parts) != 2:
                    return None
            elif role == "proxy":
                if script != root / "local_https_proxy.py" or len(parts) != 2:
                    return None
            else:
                return None
        stat = (proc / "stat").read_text(encoding="utf-8")
        return (stat.rsplit(")", 1)[1].split()[19], *parts)
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def stop_owned_pid(root: Path, pid: int, role: str) -> int:
    identity = managed_role_identity(pid, root, role)
    if identity is None or managed_role_identity(pid, root, role) != identity:
        print(f"Refusing to stop unknown/stale {role} PID {pid}", file=sys.stderr)
        return 3
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return 0
    except PermissionError:
        return 3
    return 0


def recover(root: Path, port: int, kind: str) -> int:
    if port_free(port):
        print(f"Port {port}: free")
        return 0
    matches = candidates(root, port, kind)
    if len(matches) != 1 or not recognizable_listener(port, kind):
        print(f"ERROR: port {port} belongs to an unknown or ambiguous process; not stopping it", file=sys.stderr)
        return 3
    pid, identity = matches[0]
    if process_identity(pid, root, port, kind) != identity:
        print(f"ERROR: process identity changed on port {port}; refusing recovery", file=sys.stderr)
        return 3
    print(f"Recovering stale managed {kind} PID {pid} on port {port}", flush=True)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return 0 if port_free(port) else 3
    except PermissionError:
        return 3
    for _ in range(30):
        if port_free(port):
            print(f"Port {port}: recovered")
            return 0
        time.sleep(0.2)
    print(f"ERROR: port {port} remains occupied after graceful shutdown; no SIGKILL", file=sys.stderr)
    return 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument("--kind", choices=("mcp", "proxy"))
    parser.add_argument("--stop-pid", type=int)
    parser.add_argument("--role", choices=("tunnel", "supervisor", "backend", "proxy"))
    args = parser.parse_args()
    if args.stop_pid is not None:
        if args.role is None:
            parser.error("--role is required with --stop-pid")
        return stop_owned_pid(args.root.expanduser().resolve(), args.stop_pid, args.role)
    if args.port is None or args.kind is None:
        parser.error("--port and --kind are required")
    if not (1024 <= args.port <= 65535):
        parser.error("port out of range")
    return recover(args.root.expanduser().resolve(), args.port, args.kind)


if __name__ == "__main__":
    raise SystemExit(main())
